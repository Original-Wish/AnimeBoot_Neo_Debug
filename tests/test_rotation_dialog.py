# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
import os, tempfile, time, unittest
from pathlib import Path
from unittest import mock
from PIL import Image
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide2.QtCore import Qt
from PySide2.QtWidgets import QApplication, QDialog
from animeboot_neo.project import default_project
from animeboot_neo.rotation_dialog import RotationDialog
from animeboot_neo import qt_gui as gui


class RotationDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        patcher=mock.patch.object(RotationDialog,'_confirm_disabled',return_value='disabled')
        patcher.start();self.addCleanup(patcher.stop)

    def make_project(self, root):
        p=default_project(640,480)
        for n in (0,1,2):
            folder=root/str(n);folder.mkdir()
            Image.new('RGB',(24,16),'blue').save(folder/'0.png')
        p['boot_animation'].update(enabled=True,source=str(root/'0'),max_frames=1,codec='jpeg',
            playlist=[{'source':str(root/'1')},{'source':str(root/'2')}],rotation_enabled=False)
        return p

    def wait(self, dialog):
        deadline=time.monotonic()+20
        while dialog.thread is not None and time.monotonic()<deadline:
            self.app.processEvents();time.sleep(.01)
        self.assertIsNone(dialog.thread,'estimate worker did not finish')
        self.app.processEvents()

    def test_disable_rotation_and_save_when_primary_and_extra_missing(self):
        p=default_project(640,480)
        p['boot_animation'].update(enabled=True,source='deleted-primary.gif',rotation_enabled=True,
            playlist=[{'source':'deleted-extra.gif'}])
        dialog=RotationDialog(p);dialog.enabled.setChecked(False);dialog.apply()
        self.assertEqual(dialog.result(),QDialog.Accepted)
        self.assertFalse(dialog.result_spec['rotation_enabled'])
        self.assertTrue(dialog.result_spec['enabled'])
        self.assertEqual(dialog.result_spec['source'],'deleted-primary.gif')
        dialog.deleteLater();self.app.processEvents()

    def test_delete_invalid_last_entry_can_save_without_disabling_primary(self):
        p=default_project(640,480)
        p['boot_animation'].update(enabled=True,source='missing.gif',rotation_enabled=True,
            playlist=[{'source':'missing-extra.gif'}])
        dialog=RotationDialog(p);dialog.listing.setCurrentRow(0);dialog.delete_source();dialog.apply()
        self.assertEqual(dialog.result(),QDialog.Accepted);self.assertEqual(dialog.result_spec['playlist'],[])
        self.assertFalse(dialog.result_spec['rotation_enabled']);self.assertTrue(dialog.result_spec['enabled'])
        dialog.deleteLater();self.app.processEvents()

    def test_missing_resources_show_inline_without_modal_or_worker(self):
        p=default_project(640,480);p['boot_animation']['playlist']=[{'source':'missing.gif'}]
        dialog=RotationDialog(p)
        with mock.patch.object(gui.QMessageBox,'warning') as popup:
            dialog.enabled.setChecked(True)
            self.assertFalse(dialog.enabled.isChecked());self.assertIsNone(dialog.thread)
            self.assertIn('不存在',dialog.capacity.text());self.assertFalse(popup.called)
        dialog.reject()

    def test_check_auto_estimates_primary_plus_all_selected_codec_and_gates_capacity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p=self.make_project(root);dialog=RotationDialog(p)
            space=dict(ok=True,boot_mode='uefi',free_bytes=1024**3,reclaimable_bytes=10*1024**2,replaceable_bytes=1024**3)
            with mock.patch.object(gui,'_probe_boot_partition_space_background',return_value=space):
                dialog.enabled.setChecked(True)
                self.assertTrue(dialog.enabled.isChecked())
                self.assertIsNone(dialog.approved_signature)
                self.assertIsNotNone(dialog.thread)
                self.wait(dialog)
            self.assertTrue(dialog.enabled.isChecked())
            result,_=dialog.last_result
            self.assertEqual(len(result['items']),3);self.assertEqual(result['estimated_codecs'],['jpeg'])
            self.assertEqual(result['qoi_bytes'],0)
            self.assertIn('主动画',dialog.capacity.text());self.assertIn('净可回收',dialog.capacity.text())
            dialog.apply();self.assertEqual(dialog.result(),QDialog.Accepted)

    def test_insufficient_space_never_enables_rotation_and_can_save_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=self.make_project(Path(tmp));dialog=RotationDialog(p)
            with mock.patch.object(gui,'_probe_boot_partition_space_background',return_value=dict(ok=True,boot_mode='bios',replaceable_bytes=1)):
                dialog.enabled.setChecked(True);self.wait(dialog)
            self.assertFalse(dialog.enabled.isChecked());self.assertIsNone(dialog.approved_signature)
            self.assertIn('空间不足',dialog.capacity.text());dialog.apply()
            self.assertEqual(dialog.result(),QDialog.Accepted)

    def test_disable_during_estimation_is_respected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=self.make_project(Path(tmp));dialog=RotationDialog(p)
            with mock.patch.object(gui,'_probe_boot_partition_space_background',return_value=dict(ok=True,boot_mode='uefi',replaceable_bytes=1024**3)):
                dialog.enabled.setChecked(True)
                # Checking initiates an estimate; unchecking revokes the
                # pending enable request before the result comes back.
                dialog.enabled.setChecked(True);dialog.enabled.setChecked(False)
                self.wait(dialog)
            self.assertFalse(dialog.enabled.isChecked())
            dialog.apply();self.assertFalse(dialog.result_spec['rotation_enabled'])

    def test_source_deletion_invalidates_estimate_without_modal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p=self.make_project(root);dialog=RotationDialog(p)
            with mock.patch.object(gui,'_probe_boot_partition_space_background',return_value=dict(ok=True,boot_mode='uefi',replaceable_bytes=1024**3)):
                dialog.enabled.setChecked(True);self.wait(dialog)
            import shutil
            shutil.rmtree(root/'1');dialog.apply()
            self.assertNotEqual(dialog.result(),QDialog.Accepted)
            self.assertFalse(dialog.enabled.isChecked())
            dialog.listing.setCurrentRow(0);dialog.delete_source();dialog.apply()
            self.assertEqual(dialog.result(),QDialog.Accepted)

    def test_main_canvas_crud_and_share_button_visible_text(self):
        with mock.patch.object(gui,'_read_pending_registry_path',return_value=''),mock.patch.object(gui,'_font_catalog_loader',return_value=mock.Mock()):
            window=gui.MainWindow();window.setAttribute(Qt.WA_DontShowOnScreen,True)
            window.project['prompt']['text']='A edits';window._add_canvas(True)
            self.assertEqual(len(window.project['canvases']),2)
            window.project['prompt']['text']='B edits';window._canvas_changed(0)
            self.assertEqual(window.project['prompt']['text'],'A edits')
            window._canvas_changed(1);self.assertEqual(window.project['prompt']['text'],'B edits')
            with mock.patch.object(gui.QMessageBox,'question',return_value=gui.QMessageBox.Yes):window._delete_canvas()
            self.assertEqual(len(window.project['canvases']),1)
            self.assertEqual(window.theme_package_btn.text(),'分享包')
            self.assertFalse(window.theme_package_btn.icon().isNull())
            self.assertEqual([a.text() for a in window.theme_package_btn.menu().actions()],['导入分享包','导出分享包'])
            window.close()


if __name__=='__main__':unittest.main()
