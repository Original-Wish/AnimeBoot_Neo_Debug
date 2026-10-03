# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
"""Exercise repair outcomes through the installer, using a temporary ESP only."""
import contextlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from animeboot_neo import deployment
from animeboot_neo.builder import build_payload
from animeboot_neo.project import default_project
from animeboot_neo.qt_gui import MainWindow

WINDOWS_EFI = Path(os.environ.get('WINDIR', 'C:/Windows'))/'Boot/EFI/bootmgfw.efi'


@unittest.skipUnless(os.name == 'nt' and WINDOWS_EFI.is_file(), 'native Microsoft EFI needed')
class RepairInstallTransactionTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self, repair):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);root=base/'esp'
            entry=root/'EFI/Microsoft/Boot/bootmgfw.efi'
            entry.parent.mkdir(parents=True);original=WINDOWS_EFI.read_bytes();entry.write_bytes(original)
            fallback=root/'EFI/Boot/bootx64.efi';fallback.parent.mkdir();fallback.write_bytes(b'UNTOUCHED FALLBACK')
            report=build_payload(default_project(640,480),base/'build')
            manifest=dict(payload_dir=report['payload_dir'],uefi_install_preflight_complete=True,
                          fallback_bootcurrent_repair_choice='repair')
            with contextlib.ExitStack() as stack:
                for name,value in [('check_system_volume_encryption',{}),('_firmware_type','uefi'),
                    ('_secure_boot_enabled',False),('_probe_bootcurrent_boot_path',{'needs_repair':True}),
                    ('_snapshot_firmware_boot_entries',{}),('_create_animebootneo_firmware_entry_details',{'skipped':True}),
                    ('_cleanup_unexpected_new_firmware_boot_options',{}),('_unmount_root',None),
                    ('_create_repair_windows_boot_manager',repair)]:
                    stack.enter_context(mock.patch.object(deployment.w,name,return_value=value))
                stack.enter_context(mock.patch.object(deployment,'_mount',return_value=dict(root=str(root),boot_mode='uefi')))
                yield root,entry,original,manifest,fallback

    def test_verified_failed_repair_keeps_installed_payload_and_manual_steps(self):
        repair=dict(ok=False,manual_required=True,error='Firmware rejected the write',cleanup_errors=[])
        with self.fixture(repair) as (root,entry,original,manifest,fallback):
            result=deployment.install_boot_item_standalone(manifest)
            self.assertTrue(result['ok']);self.assertTrue(result['bootcurrent_repair_manual_required'])
            self.assertFalse(result['bootcurrent_repair_ok']);self.assertFalse(result['auxiliary_available'])
            self.assertNotEqual(entry.read_bytes(),original)
            state=deployment._read_state(root);self.assertEqual(state['phase'],'complete')
            self.assertTrue(deployment._verify(root,state));self.assertEqual(fallback.read_bytes(),b'UNTOUCHED FALLBACK')
            steps=MainWindow._diskgenius_manual_uefi_repair_text(result['bootcurrent_repair_error'],False,str(root))
            self.assertIn('DiskGenius',steps);self.assertIn('设置UEFI BIOS启动项',steps)
            self.assertIn(r'\EFI\Microsoft\Boot\bootmgfw.efi',steps)
            self.assertIn('本次没有建立',steps);self.assertNotIn('animebootneo.cn',steps)
            self.assertTrue(deployment.uninstall_boot_item_official_equivalent()['ok'])
            self.assertEqual(entry.read_bytes(),original)
            self.assertFalse((root/'AnimeBootNeo').exists());self.assertFalse((root/deployment.BACKUP).exists())

    def test_boot_number_zero_is_success_and_deleted_by_exact_identity(self):
        fingerprint='a'*64
        repair=dict(ok=True,boot_number=0,boot_sha256=fingerprint,cleanup_errors=[])
        with self.fixture(repair) as (root,entry,original,manifest,fallback), \
             mock.patch.object(deployment.w,'_remove_managed_native_boot_entry',return_value=dict(ok=True,boot_number=0)) as remove:
            result=deployment.install_boot_item_standalone(manifest)
            self.assertTrue(result['bootcurrent_repair_ok'])
            state=deployment._read_state(root);self.assertEqual(state['repair_number'],0)
            self.assertEqual(state['repair_sha256'],fingerprint)
            disabled=deployment.uninstall_boot_item_official_equivalent()
            self.assertTrue(disabled['windows_restored']);remove.assert_called_once_with(0,fingerprint)
            self.assertEqual(disabled['owned_directories_removed'],['AnimeBootNeo','AnimeBootNeo.Backup'])
            self.assertEqual(entry.read_bytes(),original)

    def test_unconfirmed_repair_rollback_preserves_checkpoint_and_retry_works(self):
        fingerprint='b'*64
        repair=dict(ok=False,manual_required=False,boot_number=0,boot_sha256=fingerprint,
                    error='Repair write failed',cleanup_errors=['Native deletion blocked'])
        with self.fixture(repair) as (root,entry,original,manifest,fallback):
            with mock.patch.object(deployment.w,'_remove_managed_native_boot_entry',side_effect=deployment.DeploymentError('Identity check blocked')):
                with self.assertRaisesRegex(deployment.DeploymentError,'自动回滚未能完全恢复'):
                    deployment.install_boot_item_standalone(manifest)
            state=deployment._read_state(root)
            self.assertEqual(state['phase'],'uninstalling');self.assertEqual(state['repair_sha256'],fingerprint)
            self.assertTrue(state['uninstall_progress']['windows_restored'])
            self.assertEqual(entry.read_bytes(),original)
            with mock.patch.object(deployment.w,'_remove_managed_native_boot_entry',return_value=dict(ok=True,boot_number=0)):
                self.assertTrue(deployment.uninstall_boot_item_official_equivalent()['ok'])
            self.assertFalse((root/'AnimeBootNeo').exists());self.assertFalse((root/deployment.BACKUP).exists())
            self.assertEqual(fallback.read_bytes(),b'UNTOUCHED FALLBACK')


if __name__=='__main__':unittest.main()
