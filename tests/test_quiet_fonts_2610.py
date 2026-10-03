# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from animeboot_neo import fonts, quiet_boot
from animeboot_neo.project import default_project

ROOT = Path(__file__).resolve().parents[1]


class QuietBootTests(unittest.TestCase):
    def test_a20_setup_keeps_enable_and_retry_code_while_only_debug_byte_changes(self):
        source = (ROOT / 'assets/grub/grldr').read_bytes()
        result = quiet_boot.silent_a20(source)
        changes = [i for i, (old, new) in enumerate(zip(source, result)) if old != new]
        self.assertEqual(len(source), len(result))
        self.assertEqual(changes, [0x23a9])
        self.assertEqual(result[0x23a4:0x23ac], bytes.fromhex('b9ff00ba0100fae8'))
        with self.assertRaises(ValueError): quiet_boot.silent_a20(b'unknown upstream')
        with self.assertRaises(ValueError): quiet_boot.silent_a20(source + quiet_boot.A20_STARTUP)

    def test_both_bios_templates_remove_only_the_banner_without_mutating_locked_inputs(self):
        for folder in ('i386-pc', 'i386-pc_compat'):
            source = ROOT / 'assets/grub' / folder
            original = (source / 'kernel.img').read_bytes()
            original_hash = hashlib.sha256(original).hexdigest()
            with tempfile.TemporaryDirectory() as tmp:
                target = quiet_boot.silent_bios_modules(source, Path(tmp) / 'modules')
                result = (target / 'kernel.img').read_bytes()
                self.assertEqual(len(result), len(original))
                self.assertNotIn(b'Welcome to GRUB!', result)
                self.assertEqual(result, original.replace(quiet_boot.GRUB_WELCOME, bytes(len(quiet_boot.GRUB_WELCOME))))
                self.assertEqual((target / 'linux.mod').read_bytes(), (source / 'linux.mod').read_bytes())
            self.assertEqual(hashlib.sha256((source / 'kernel.img').read_bytes()).hexdigest(), original_hash)


class FontCatalogTests(unittest.TestCase):
    def test_persistent_cache_reuses_accepted_and_rejected_faces_and_rescans_changed_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / 'full.ttf', Path(tmp) / 'subset.ttf'
            a.write_bytes(b'full'); b.write_bytes(b'subset')
            cache = Path(tmp) / 'catalog.json'
            face = mock.Mock(); face.getname.return_value = ('Full CJK', 'Regular')
            with mock.patch.object(fonts, 'installed_font_paths', return_value=[str(a), str(b)]), \
                 mock.patch.object(fonts, 'is_chinese_font', side_effect=lambda path: path == str(a)) as check, \
                 mock.patch('PIL.ImageFont.truetype', return_value=face):
                first = fonts.scan_font_catalog(cache)
                self.assertEqual(check.call_count, 2)
                self.assertEqual(first, [('Full CJK', 'Regular', str(a))])
                check.reset_mock()
                self.assertEqual(fonts.scan_font_catalog(cache), first)
                check.assert_not_called()
                b.write_bytes(b'changed subset file')
                fonts.scan_font_catalog(cache)
                check.assert_called_once_with(str(b))
                a.unlink()
                with mock.patch.object(fonts, 'installed_font_paths', return_value=[str(b)]):
                    self.assertEqual(fonts.scan_font_catalog(cache), [])
                self.assertEqual(len(json.loads(cache.read_text(encoding='utf-8'))['faces']), 1)

    def test_invalid_cache_or_readonly_cache_directory_does_not_remove_font_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / 'full.ttf'; a.write_bytes(b'full')
            cache = Path(tmp) / 'catalog.json'; cache.write_text('{broken', encoding='utf-8')
            face = mock.Mock(); face.getname.return_value = ('Full CJK', 'Regular')
            with mock.patch.object(fonts, 'installed_font_paths', return_value=[str(a)]), \
                 mock.patch.object(fonts, 'is_chinese_font', return_value=True), \
                 mock.patch('PIL.ImageFont.truetype', return_value=face), \
                 mock.patch.object(fonts.os, 'replace', side_effect=PermissionError('read only')):
                self.assertEqual(fonts.scan_font_catalog(cache), [('Full CJK', 'Regular', str(a))])
            self.assertFalse(list(Path(tmp).glob('*.tmp')))

    def test_cold_properties_open_without_waiting_for_font_scan_and_keep_text_and_selection(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide2.QtWidgets import QApplication
        from PySide2.QtCore import QTimer
        from animeboot_neo import qt_gui as gui
        app = QApplication.instance() or QApplication([])
        release, started = threading.Event(), threading.Event()
        def slow_scan(**kwargs):
            started.set(); release.wait(5)
            return [('Test CJK', 'Regular', 'selected.ttf')]
        saved_cache, saved_loader = gui._FONT_CATALOG_CACHE, gui._FONT_CATALOG_LOADER
        gui._FONT_CATALOG_CACHE = None; gui._FONT_CATALOG_LOADER = None
        dialog = None
        try:
            with mock.patch.object(gui, 'scan_font_catalog', side_effect=slow_scan):
                project = default_project(640, 480)
                project['prompt']['font_path'] = 'selected.ttf'
                start = time.perf_counter()
                dialog = gui.ObjectPropertiesDialog(project, 'prompt')
                elapsed = time.perf_counter() - start
                self.assertLess(elapsed, 0.75)
                self.assertTrue(started.wait(1)); self.assertFalse(release.is_set())
                dialog.text.setPlainText('保留正在输入的文字')
                heartbeat = []
                QTimer.singleShot(0, lambda: heartbeat.append(True)); app.processEvents()
                self.assertEqual(heartbeat, [True])
                release.set()
                until = time.monotonic() + 3
                while gui._FONT_CATALOG_LOADER.running and time.monotonic() < until:
                    app.processEvents(); time.sleep(.01)
                app.processEvents()
                self.assertEqual(dialog.text.toPlainText(), '保留正在输入的文字')
                self.assertEqual(dialog.font_path.currentData(), 'selected.ttf')
                self.assertEqual(dialog.font_path.itemText(1), 'Test CJK')
                self.assertTrue(dialog.font_status.isHidden())
        finally:
            release.set()
            if dialog is not None: dialog.close()
            if gui._FONT_CATALOG_LOADER is not None: gui._FONT_CATALOG_LOADER.cancelled.set()
            gui._FONT_CATALOG_CACHE, gui._FONT_CATALOG_LOADER = saved_cache, saved_loader


if __name__ == '__main__': unittest.main()
