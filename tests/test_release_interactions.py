# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
import copy, tempfile, unittest, time
from pathlib import Path
from unittest import mock
from PIL import Image
from PySide2.QtCore import Qt
from PySide2.QtWidgets import QApplication,QDialog
from animeboot_neo import qt_gui as gui
from animeboot_neo.project import default_project
from animeboot_neo.rotation_dialog import RotationDialog

class ReleaseInteractions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def wait(self,d):
        deadline=time.monotonic()+30
        while d.thread is not None and time.monotonic()<deadline:
            self.app.processEvents();time.sleep(.01)
        self.assertIsNone(d.thread)

    def test_manual_estimate_reused_on_enable_and_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=default_project(640,480)
            for n in ('a','b'):Image.new('RGB',(32,32),'green').save(Path(tmp)/(n+'.png'))
            # Direct PNG input is not accepted as animation; use frame folders.
            for n in ('a','b'):
                folder=Path(tmp)/n;folder.mkdir();(Path(tmp)/(n+'.png')).replace(folder/'0.png')
            p['boot_animation'].update(enabled=True,source=str(Path(tmp)/'a'),playlist=[{'source':str(Path(tmp)/'b')}],max_frames=1)
            d=RotationDialog(p)
            with mock.patch.object(gui,'_probe_boot_partition_space_background',return_value=dict(ok=True,boot_mode='uefi',replaceable_bytes=1024**3)) as probe:
                d.start_estimate();self.wait(d)
                self.assertIsNotNone(d.approved_signature)
                cache=d.estimate_cache();self.assertIsNotNone(cache)
                d.enabled.setChecked(True);self.app.processEvents()
                self.assertIsNone(d.thread);self.assertEqual(probe.call_count,1)
                d.apply();self.assertEqual(d.result(),QDialog.Accepted)
                p['boot_animation']=d.result_spec
                reopened=RotationDialog(p,estimate_cache=cache);self.app.processEvents()
                self.assertIsNone(reopened.thread);self.assertEqual(probe.call_count,1)
                reopened.enabled.setChecked(False);reopened.enabled.setChecked(True)
                self.assertIsNone(reopened.thread)
                # A real source change must invalidate the successful estimate.
                Image.new('RGB',(33,33),'red').save(Path(tmp)/'b/1.png')
                reopened.enabled.setChecked(False);reopened.enabled.setChecked(True)
                self.wait(reopened);self.assertEqual(probe.call_count,2)
                reopened.reject()

    def test_empty_primary_is_not_invalid_and_disabled_playlist_reminds(self):
        p=default_project();p['boot_animation']['playlist']=[{'source':'missing.gif'}]
        d=RotationDialog(p)
        self.assertNotIn('已失效',d.primary_label.text());self.assertEqual(d.primary_label.styleSheet(),'')
        with mock.patch.object(d,'_confirm_disabled',return_value='back') as prompt:
            d.apply();prompt.assert_called_once();self.assertNotEqual(d.result(),QDialog.Accepted)
        with mock.patch.object(d,'_confirm_disabled',return_value='disabled'):
            d.apply();self.assertEqual(d.result(),QDialog.Accepted);self.assertFalse(d.result_spec['rotation_enabled'])

    def test_canvas_switch_while_busy_and_success_uses_build_snapshot(self):
        with mock.patch.object(gui,'_read_pending_registry_path',return_value=''),mock.patch.object(gui,'_font_catalog_loader',return_value=mock.Mock()):
            w=gui.MainWindow();w.setAttribute(Qt.WA_DontShowOnScreen,True)
            w.project['prompt']['text']='A';w._add_canvas(True);w.project['prompt']['text']='B'
            worker=gui.BuildWorker(w.project,'unused');snapshot=copy.deepcopy(worker.project)
            w._set_task_busy(True);w._canvas_changed(0)
            self.assertEqual(w.project['active_canvas'],0);self.assertEqual(w.project['prompt']['text'],'A')
            self.assertEqual(worker.project,snapshot)
            snapshot['timeout']=17
            w.build_result=dict(project_snapshot=snapshot,payload=dict(payload_dir='fake',asset_root='fake',animation=dict(enabled=True,rotation='sequential',frame_count=30,items=[dict(frame_count=10),dict(frame_count=20)])))
            w.project['timeout']=99
            self.assertEqual(w._base_manifest()['expected_timeout'],17)
            text=w._production_success_message(dict(boot_mode='uefi'))
            self.assertIn('17 秒',text);self.assertIn('共 2 段',text);self.assertIn('主动画 10 帧',text);self.assertIn('30 帧',text)
            w.project['boot_animation'].update(enabled=True,rotation_enabled=True,playlist=[{'source':'x'}]);w._load_controls()
            self.assertIn('已启用顺序轮换',w.rotation_btn.text());self.assertNotIn('测试',w.rotation_btn.text())
            w._set_task_busy(False);w.build_result=None;w.close()
