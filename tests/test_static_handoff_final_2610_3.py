# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
"""Final static handoff choices, target binding and user confirmation."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from animeboot_neo import builder, qt_gui as gui
from animeboot_neo.grub_config import generate_config, static_handoff_strategy
from animeboot_neo.project import default_project


class StaticHandoffTests(unittest.TestCase):
    def test_compiled_strategy_matrix_keeps_animation_black_and_visible_countdown(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'test.cfg'
            for platform in ('bios', 'uefi'):
                for animation in (False, True):
                    for mode in ('none', 'circle', 'bar'):
                        for transition in ('blend', 'black'):
                            for timeout in (0, 8, 180):
                                with self.subTest(platform=platform, animation=animation, mode=mode,
                                                  transition=transition, timeout=timeout):
                                    project = default_project(640, 480)
                                    project.update(countdown_mode=mode, static_windows_transition=transition, timeout=timeout)
                                    project['boot_animation']['enabled'] = animation
                                    text = generate_config(project, platform).decode()
                                    config.write_text(text, encoding='utf-8')
                                    builder._verify_grub_script(config)
                                    expected = not animation and transition == 'blend' and mode == 'none' and timeout > 0
                                    self.assertEqual(static_handoff_strategy(project) == 'external_timer', expected)
                                    self.assertEqual('sleep --interruptible' in text, expected)
                                    pre_menu, entry = text.split("menuentry 'Windows'", 1)
                                    self.assertEqual('\nnormal_exit\n' in pre_menu, expected)
                                    if expected:
                                        self.assertIn('sleep --interruptible %d' % timeout, pre_menu)
                                        self.assertIn('set neo_static_handoff=external_timer', pre_menu)
                                    if animation:
                                        self.assertTrue(entry.lstrip().startswith('--id animeboot-default {\n  neo_black'))
                                        self.assertNotIn('normal_exit', text)
                                        self.assertIn('quiet loglevel=0', text)
                                    elif not expected:
                                        self.assertIn('set neo_static_handoff=menu_timer', pre_menu)

    def test_payload_binding_retains_the_countdown_choice_and_timer_strategy(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            file_sets = {}
            for mode in ('none', 'circle', 'bar'):
                project = default_project(640, 480)
                project['countdown_mode'] = mode
                payload = Path(builder.build_payload(project, base / mode)['payload_dir'])
                for platform in ('bios', 'uefi'):
                    bound = base / (mode + '-' + platform)
                    record = builder.bind_payload(payload, bound, platform,
                        {'filesystem': 'FAT16', 'fs_uuid': '2620-0001'} if platform == 'bios' else None)
                    self.assertEqual(record['config_project']['countdown_mode'], mode)
                    name = 'files/AnimeBootNeo/grub.cfg' if platform == 'bios' else 'files/EFI/fedora/grub.cfg'
                    text = (bound / name).read_text(encoding='utf-8')
                    self.assertEqual('sleep --interruptible' in text, mode == 'none')
                    self.assertEqual(record['project']['static_handoff_strategy'],
                                     'external_timer' if mode == 'none' else 'menu_timer')
                    file_sets[(platform, mode)] = set(record['files'])
            for platform in ('bios', 'uefi'):
                self.assertFalse(any('timer' in path for path in file_sets[(platform, 'none')]))
                self.assertFalse(any('tick.png' in path or 'circle-center.png' in path
                                     for path in file_sets[(platform, 'none')]))


class CountdownUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide2.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def make_window(self):
        window = gui.MainWindow()
        window.show()
        self.app.processEvents()
        self.addCleanup(window.close)
        return window

    def test_default_none_and_confirmation_cancel_then_accept_then_load_silently(self):
        messages = []
        def execute(message):
            messages.append((message.windowTitle(), message.text(), [b.text() for b in message.buttons()]))
            return 0
        with mock.patch.object(gui, '_read_pending_registry_path', return_value=''), \
             mock.patch.object(gui, '_font_catalog_loader', return_value=mock.Mock()), \
             mock.patch.object(gui.QMessageBox, 'exec_', execute):
            window = self.make_window()
            self.assertEqual(window.project['countdown_mode'], 'none')
            self.assertEqual(window.countdown.currentData(), 'none')
            for mode in ('circle', 'bar'):
                with mock.patch.object(gui.QMessageBox, 'clickedButton', lambda message: next(b for b in message.buttons() if b.text() == '取消')):
                    window.countdown.setCurrentIndex(window.countdown.findData(mode))
                    self.assertEqual(window.countdown.currentData(), window.project['countdown_mode'])
                with mock.patch.object(gui.QMessageBox, 'clickedButton', lambda message: next(b for b in message.buttons() if b.text() == '仍要继续')):
                    window.countdown.setCurrentIndex(window.countdown.findData(mode))
                    self.assertEqual(window.project['countdown_mode'], mode)
            self.assertEqual(len(messages), 4)
            self.assertTrue(all('黑屏闪烁' in text and '未启用动图' in text for _, text, _ in messages))
            before = len(messages)
            window._load_controls()
            self.assertEqual(len(messages), before)
            window.countdown.setCurrentIndex(window.countdown.findData('none'))
            self.assertEqual(len(messages), before)
            self.assertEqual(window.project['countdown_mode'], 'none')

    def test_static_zero_and_success_messages_describe_real_escape_behavior(self):
        texts = []
        def execute(message):
            texts.append(message.text())
            return 0
        with mock.patch.object(gui, '_read_pending_registry_path', return_value=''), \
             mock.patch.object(gui, '_font_catalog_loader', return_value=mock.Mock()), \
             mock.patch.object(gui.QMessageBox, 'exec_', execute), \
             mock.patch.object(gui.QMessageBox, 'clickedButton', lambda message: next(b for b in message.buttons() if b.text() == '确认设为0秒')):
            window = self.make_window()
            window.timeout.setValue(0)
            self.assertEqual(window.project['timeout'], 0)
            self.assertIn('无法使用 Esc 跳过本次动图动画', texts[0])
            window.build_result = {'payload': {'animation': {'enabled': True, 'frame_count': 4}}}
            success = window._production_success_message({'boot_mode': 'uefi'})
            self.assertIn('无法使用 Esc 跳过本次动图动画', success)
            self.assertNotIn('窗口', success)
            window.project['timeout'] = 8
            success = window._production_success_message({'boot_mode': 'uefi'})
            self.assertIn('显示静图时按 Esc', success)


if __name__ == '__main__':
    unittest.main()
