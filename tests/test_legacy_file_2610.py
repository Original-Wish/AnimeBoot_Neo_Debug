# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
import hashlib,tempfile,unittest
from pathlib import Path
from unittest import mock
from animeboot_neo import legacy_file,deployment
from animeboot_neo.grub_config import generate_config,embedded_config
from animeboot_neo.project import default_project

ROOT=Path(__file__).resolve().parents[1]

class LegacyFileTests(unittest.TestCase):
    def test_loader_only_changes_a20_diagnostic_flag_startup_controls_and_menu(self):
        data=(ROOT/'assets/grub/grldr').read_bytes()
        binding=dict(filesystem='FAT16',fs_uuid='ABCD-1234')
        made=legacy_file.make_bootmgr(data,binding)
        self.assertEqual(made[:0x2005],data[:0x2005])
        expected=bytearray(data)
        self.assertEqual(data[0x23a4:0x23ac],bytes.fromhex('b9ff00ba0101fae8'))
        expected[0x23a9]=0  # A20 stays enabled; only DH's debug byte changes.
        self.assertEqual(made[0x2034:0x50eb8],bytes(expected[0x2034:0x50eb8]))
        self.assertIn(b'hiddenmenu --silent',made[0x50eb8:])
        self.assertIn(b'kernel /AnimeBootNeo/grub2.lnx',made[0x50eb8:])
        self.assertNotIn(b'menu.lst',made[0x50eb8:])
        self.assertNotIn(b'http:',made[0x50eb8:])
        self.assertEqual(made[0x2017:0x2034],bytes(29))
        with self.assertRaises(ValueError):legacy_file.make_bootmgr(bytes(32),binding)
        with self.assertRaises(ValueError):legacy_file.make_bootmgr(data,dict(filesystem='FAT16',fs_uuid='invalid\nvalue'))

    def test_file_windows_handoff_does_not_chain_back_to_the_vbr(self):
        project=default_project(640,480);project['boot_animation']['enabled']=True
        cfg=generate_config(project,'bios')
        for data in (cfg,embedded_config(dict(filesystem='FAT16',fs_uuid='ABCD-1234'))):
            self.assertIn(b'ntldr (',data)
            self.assertIn(b'/AnimeBootNeo/windows.bootmgr',data)
            self.assertNotIn(b')+1',data)
            self.assertNotIn(b'@NEO_ENV_LBA@',data)
        self.assertIn(b'save_env -f $neo_env neo_once',cfg)

    def test_bootmgr_validation_rejects_unknown_and_marked_loaders(self):
        good=b'BOOTMGR PE\0\0'+ 'Windows'.encode('utf-16le')+bytes(32768)
        legacy_file.validate_windows_bootmgr(good)
        for bad in (b'X'*32768,good+b'eas'+b'sos',good+b'er'+b'ldr'):
            with self.assertRaises(deployment.DeploymentError):legacy_file.validate_windows_bootmgr(bad)

    def test_rollback_preserves_concurrently_changed_bootmgr_and_backup(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);backup=root/deployment.BACKUP/'files';backup.mkdir(parents=True)
            original=b'ORIGINAL';(backup/'bootmgr').write_bytes(original)
            (root/'bootmgr').write_bytes(b'FOREIGN CHANGE')
            state={'legacy_method':'bootmgr-file','files':{'bootmgr':{'existed':True,'original_sha256':hashlib.sha256(original).hexdigest(),'installed_sha256':hashlib.sha256(b'INSTALLED').hexdigest()}}}
            with self.assertRaisesRegex(deployment.DeploymentError,'其他程序修改'):deployment._rollback_files(root,state)
            self.assertEqual((root/'bootmgr').read_bytes(),b'FOREIGN CHANGE')
            self.assertEqual((backup/'bootmgr').read_bytes(),original)

    def test_failed_atomic_replace_restores_readonly_attributes(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'bootmgr';p.write_bytes(b'WINDOWS')
            with mock.patch.object(legacy_file,'attributes',return_value=7),mock.patch.object(legacy_file,'set_attributes') as setter,mock.patch.object(deployment.os,'replace',side_effect=OSError('injected')):
                with self.assertRaises(OSError):deployment._atomic(p,b'NEW')
            self.assertIn(mock.call(p,7),setter.call_args_list)
            self.assertEqual(p.read_bytes(),b'WINDOWS')

    def test_tiny_mbr_transaction_preserves_identity_and_only_writes_lba0(self):
        original=bytes(440)+b'IDENTITY'+bytes(62)+b'\x55\xaa'
        installed=(ROOT/'assets/legacy/mbr.bin').read_bytes()[:440]+original[440:]
        disk=bytearray(original+b'OCCUPIED'*128);before=bytes(disk);writes=[]
        def read(o,n):return bytes(disk[o:o+n])
        def write(o,d):writes.append((o,len(d)));disk[o:o+len(d)]=d
        legacy_file.commit_mbr(read,write,original,installed)
        self.assertEqual(disk[440:],before[440:])
        legacy_file.restore_mbr(read,write,original,installed)
        self.assertEqual(bytes(disk),before)
        self.assertEqual(writes,[(0,512),(0,512)])

    def test_partial_mbr_write_failure_restores_the_original_mbr(self):
        original=bytes(510)+b'\x55\xaa';installed=b'I'*440+original[440:]
        disk=bytearray(original);failed=[False]
        def read(o,n):return bytes(disk[o:o+n])
        def write(o,d):
            if not failed[0]:
                failed[0]=True;disk[:128]=d[:128];raise OSError('partial write')
            disk[o:o+len(d)]=d
        with self.assertRaisesRegex(OSError,'partial write'):legacy_file.commit_mbr(read,write,original,installed)
        self.assertEqual(bytes(disk),original)

    def test_mbr_commit_does_not_replace_a_concurrent_foreign_change(self):
        original=bytes(510)+b'\x55\xaa';installed=b'I'*440+original[440:];foreign=b'F'+original[1:]
        with mock.patch.object(legacy_file.w,'_write_physical_sector') as writer:
            with self.assertRaisesRegex(deployment.DeploymentError,'发生变化'):legacy_file.commit_mbr(lambda o,n:foreign,writer,original,installed)
            writer.assert_not_called()

if __name__=='__main__':unittest.main()
