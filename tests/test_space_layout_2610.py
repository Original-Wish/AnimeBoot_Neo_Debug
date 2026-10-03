# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
# -*- coding: utf-8 -*-
import contextlib
import hashlib
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock
from animeboot_neo import boot_space, deployment
from animeboot_neo.builder import build_payload, verify_payload
from animeboot_neo.grub_config import create_blank_grubenv
from animeboot_neo.project import default_project


def put(root, name, data):
    p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data);return p


class SpaceLayoutTests(unittest.TestCase):
    def installation(self, root, mode='uefi'):
        entry='EFI/Microsoft/Boot/bootmgfw.efi' if mode=='uefi' else 'bootmgr'
        put(root,entry,b'NEO ENTRY')
        put(root,'AnimeBootNeo/old/deep/animation.bin',b'A'*4097)
        put(root,'AnimeBootNeo/future/layout/new-name.bin',b'B'*8193)
        put(root,'AnimeBootNeo.Backup/files/'+entry,b'ORIGINAL WINDOWS')
        state=dict(format='AnimeBootNeo-installed-3-canvases',app_version='2610.3',legacy_method='bootmgr-file',
            phase='complete',mode=mode,env_path='',files={
            entry:dict(existed=True,installed_sha256=hashlib.sha256(b'NEO ENTRY').hexdigest(),
                original_sha256=hashlib.sha256(b'ORIGINAL WINDOWS').hexdigest())})
        put(root,'AnimeBootNeo.Backup/state.json',json.dumps(state).encode())
        # Never treat shared EFI folders, Windows files or another Linux as ours.
        put(root,'EFI/Microsoft/Boot/BCD',b'W'*25000)
        put(root,'EFI/fedora/foreign-kernel',b'F'*17000)
        return state,entry

    def test_full_owned_trees_net_reclaim_and_shared_paths(self):
        for mode in ('uefi','bios'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);state,entry=self.installation(root,mode)
                with mock.patch.object(boot_space,'allocated_bytes',side_effect=lambda p,u:((p.stat().st_size+u-1)//u)*u):
                    info=boot_space.installed_space(root,state,unit=4096)
                # 2 owned frames + state + original backup + installed entry;
                # deduct the original Windows entry that must exist afterwards.
                self.assertEqual(info['existing_animebootneo_bytes'],8192+12288+4096*3)
                self.assertEqual(info['restored_original_bytes'],4096)
                self.assertEqual(info['reclaimable_bytes'],8192+12288+4096*2)

    def test_probe_returns_fields_consumed_by_both_estimate_uis(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);state,_=self.installation(root)
            usage=types.SimpleNamespace(free=10000,total=1000000)
            with mock.patch.object(deployment,'_mount',return_value=dict(root=tmp,boot_mode='uefi')), \
                 mock.patch.object(deployment.shutil,'disk_usage',return_value=usage), \
                 mock.patch.object(deployment.w,'_unmount_root') as unmount:
                info=deployment.probe_boot_partition_space()
            self.assertGreater(info['reclaimable_bytes'],0)
            self.assertEqual(info['replaceable_bytes'],10000+info['reclaimable_bytes'])
            self.assertEqual(info['boot_mode'],'uefi');unmount.assert_called_once()
            from animeboot_neo.qt_gui import MainWindow
            target=types.SimpleNamespace(project={'boot_animation':{'codec':'jpeg'}})
            result={'jpeg_total_boot_bytes':8*1024*1024}
            # Same UI calculation with a previous 50 MiB installation on a
            # partition that has only 12 MiB free: replacement fits.
            replacement=dict(ok=True,free_bytes=12*1024*1024,replaceable_bytes=62*1024*1024)
            self.assertIsNone(MainWindow._space_risk_from_estimate(target,result,replacement,'jpeg'))

    def test_uninstall_reclaims_unlisted_future_layout_without_touching_other_os(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);state,entry=self.installation(root)
            before={p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}
            expected=boot_space.installed_space(root,state,unit=1)['reclaimable_bytes']
            # Native allocation is measured separately; logical bytes make this
            # exact before/after comparison independent of host cluster sizes.
            with mock.patch.object(boot_space,'allocated_bytes',side_effect=lambda p,u:p.stat().st_size):
                expected=boot_space.installed_space(root,state,unit=1)['reclaimable_bytes']
            deployment._uninstall(root,state)
            after={p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}
            self.assertEqual(sum(map(len,before.values()))-sum(map(len,after.values())),expected)
            self.assertEqual(after[entry],b'ORIGINAL WINDOWS')
            self.assertEqual(after['EFI/fedora/foreign-kernel'],b'F'*17000)
            self.assertEqual(after['EFI/Microsoft/Boot/BCD'],b'W'*25000)
            self.assertFalse((root/'AnimeBootNeo').exists());self.assertFalse((root/'AnimeBootNeo.Backup').exists())

    def test_missing_journal_does_not_claim_windows_or_unowned_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);put(root,'AnimeBootNeo/unregistered.bin',b'A'*100)
            info=boot_space.installed_space(root)
            self.assertEqual(info['reclaimable_bytes'],0)
            self.assertEqual(info['reclaim_status'],'unregistered')

    def test_changed_backup_or_escaping_paths_fail_before_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);state,_=self.installation(root)
            state['files']['../outside']={'existed':False}
            with self.assertRaises(ValueError):deployment._uninstall(root,state)
            self.assertTrue((root/'AnimeBootNeo/future/layout/new-name.bin').exists())

    def test_static_payload_has_no_handoff_state_no_marker_and_no_kernel(self):
        with tempfile.TemporaryDirectory() as tmp:
            project=default_project(640,480);project['show_debug_info']=False
            result=build_payload(project,tmp);payload=Path(result['payload_dir']);report=verify_payload(payload)
            files=payload/'files'
            self.assertEqual(report['env_path'],'')
            self.assertFalse((files/'AnimeBootNeo/bootstate.env').exists())
            self.assertFalse(list((files/'AnimeBootNeo').glob('anchor-*')))
            self.assertFalse((files/'AnimeBootNeo/grubenv').exists())
            self.assertFalse((files/'AnimeBootNeo/bzImage').exists())
            self.assertFalse((files/'EFI/Microsoft/Boot/grub.cfg').exists())
            for name in ('AnimeBootNeo/grub.cfg','EFI/fedora/grub.cfg'):
                self.assertNotIn('load_env',(files/name).read_text(encoding='utf-8'))

    def test_current_env_armed_state_verifies_and_rolls_back(self):
        relative='AnimeBootNeo/bootstate.env'
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);put(root,relative,create_blank_grubenv(True))
            state=dict(format='AnimeBootNeo-installed-3-canvases',app_version='2610.3',phase='complete',mode='uefi',
                files={relative:dict(existed=False,installed_sha256=hashlib.sha256(create_blank_grubenv()).hexdigest())},
                env_path=relative)
            self.assertTrue(deployment._verify(root,state))
            deployment._rollback_files(root,state)
            self.assertFalse((root/relative).exists())

    def test_author_title_and_visible_debug_hint_keep_subtitle_unchanged(self):
        os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
        from PySide2.QtWidgets import QApplication,QLabel
        from animeboot_neo.qt_gui import MainWindow
        app=QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch('animeboot_neo.qt_gui._read_pending_registry_path',return_value=''):
                window=MainWindow()
                self.assertIn('作者:原心',window.windowTitle())
                labels=[label.text() for label in window.findChildren(QLabel)]
                self.assertTrue(any('播放结束后显示两页诊断信息，每页 8 秒' in text for text in labels))
                subtitle=next(text for text in labels if text.startswith('Qt 可视化'))
                self.assertNotIn('作者',subtitle)
                window.show_debug_info.setChecked(False)
                self.assertFalse(window.project['show_debug_info'])
                window.close()


if __name__=='__main__':unittest.main()
