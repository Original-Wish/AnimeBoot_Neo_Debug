# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
# -*- coding: utf-8 -*-
"""Regression for real Microsoft identity, file payloads and stalled encoders."""
import contextlib
import os
from pathlib import Path
import struct
import sys
import tempfile
import time
import unittest
from unittest import mock
from PIL import Image

from animeboot_neo import animation, deployment
from animeboot_neo.builder import build_payload, verify_payload
from animeboot_neo.project import default_project, ProjectError
from animeboot_neo.windows_efi import validate_windows_efi

ROOT=Path(__file__).resolve().parents[1]
WINDOWS_EFI=Path(os.environ.get('WINDIR','C:/Windows'))/'Boot/EFI/bootmgfw.efi'


class WorkerAndPayloadTests(unittest.TestCase):
    def test_old_capacity_metadata_is_rejected_before_building(self):
        with tempfile.TemporaryDirectory() as tmp:
            project=default_project(640,480);project['capacity_mib']=1024
            with self.assertRaises(ProjectError):build_payload(project,tmp)
            self.assertFalse(list(Path(tmp).iterdir()))

    def test_current_payload_uses_files_without_a_dat_container(self):
        with tempfile.TemporaryDirectory() as tmp:
            project=default_project(640,480)
            result=build_payload(project,tmp)
            payload=Path(result['payload_dir']);report=verify_payload(payload)
            self.assertFalse(list(payload.rglob('*.dat')))
            self.assertNotIn('dat_path',result)
            self.assertEqual(result['actual_size'],sum(x['size'] for x in report['files'].values()))
            self.assertLess(result['actual_size'],40*1024*1024)

    def test_parallel_child_output_cannot_fill_a_pipe(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'source.png';Image.new('RGBA',(16,16),'blue').save(source)
            def command(manifest):
                return [sys.executable,'-c','import sys;sys.stdout.write("X"*1000000);from animeboot_neo.animation import qoi_batch_worker_main;sys.exit(qoi_batch_worker_main(sys.argv[1]))',manifest]
            with mock.patch.object(animation,'_qoi_parallel_worker_count',return_value=2),mock.patch.object(animation,'_qoi_worker_command',side_effect=command):
                outputs,workers=animation._encode_qoi_files_parallel([str(source)]*2,str(Path(tmp)/'qoi'))
            self.assertEqual(workers,2)
            self.assertTrue(all(Path(p).read_bytes().startswith(b'qoif') for p in outputs))

    def test_stalled_encoder_is_stopped_and_manifests_cleaned(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'source.png';Image.new('RGBA',(16,16),'blue').save(source)
            processes=[]
            native=animation.subprocess.Popen
            def spawn(*args,**kwargs):
                p=native(*args,**kwargs);processes.append(p);return p
            command=[sys.executable,'-c','import time;time.sleep(30)']
            start=time.monotonic()
            with mock.patch.object(animation,'_qoi_parallel_worker_count',return_value=2),mock.patch.object(animation,'_qoi_worker_command',return_value=command),mock.patch.object(animation,'ENCODING_IDLE_TIMEOUT',0.15),mock.patch.object(animation.subprocess,'Popen',side_effect=spawn):
                with self.assertRaisesRegex(animation.AnimationError,'没有生成新帧'):
                    animation._encode_qoi_files_parallel([str(source)]*2,str(Path(tmp)/'qoi'))
            self.assertLess(time.monotonic()-start,5)
            self.assertTrue(all(p.poll() is not None for p in processes))
            self.assertFalse(list((Path(tmp)/'qoi').glob('worker_*.json')))


@unittest.skipUnless(os.name=='nt' and WINDOWS_EFI.is_file(),'Windows native signed EFI required')
class NativeWindowsEfiTests(unittest.TestCase):
    def test_real_microsoft_boot_manager_original_filename_is_accepted(self):
        identity=validate_windows_efi(WINDOWS_EFI)
        self.assertTrue(identity['signature']['valid'])
        self.assertIn(identity['version']['OriginalFilename'].lower(),('bootmgr.exe','bootmgfw.efi','bootmgfw.exe'))

    def test_tampered_microsoft_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw=bytearray(WINDOWS_EFI.read_bytes());pe=struct.unpack_from('<I',raw,60)[0]
            optsize=struct.unpack_from('<H',raw,pe+20)[0];section=pe+24+optsize
            size,start=struct.unpack_from('<II',raw,section+16)
            self.assertGreater(size,128);raw[start+128]^=1
            file=Path(tmp)/'bootmgfw.efi';file.write_bytes(raw)
            with self.assertRaises(ValueError):validate_windows_efi(file)

    def test_shim_cannot_be_mistaken_for_original_windows(self):
        with self.assertRaises(ValueError):validate_windows_efi(ROOT/'assets/fedora/bootmgfw.efi')

    def test_uefi_install_verify_and_uninstall_using_real_signed_windows_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);root=base/'esp';entry=root/'EFI/Microsoft/Boot/bootmgfw.efi'
            entry.parent.mkdir(parents=True);original=WINDOWS_EFI.read_bytes();entry.write_bytes(original)
            (entry.parent/'BCD').write_bytes(b'test BCD, never used by host')
            fallback=root/'EFI/Boot/bootx64.efi';fallback.parent.mkdir();fallback.write_bytes(original)
            mounted=dict(root=str(root),boot_mode='uefi',temporary=False)
            result=build_payload(default_project(640,480),base/'build')
            manifest=dict(payload_dir=result['payload_dir'],uefi_install_preflight_complete=True,fallback_bootcurrent_repair_choice='keep')
            with contextlib.ExitStack() as stack:
                for name,value in [('check_system_volume_encryption',{}),('_firmware_type','uefi'),('_secure_boot_enabled',True),('_firmware_db_x509_thumbprints',{deployment.MSFT_UEFI_CA_2023_SHA1}),('_probe_bootcurrent_boot_path',{'needs_repair':False}),('_snapshot_firmware_boot_entries',{}),('_create_animebootneo_firmware_entry_details',{'skipped':True}),('_cleanup_unexpected_new_firmware_boot_options',{}),('_unmount_root',None)]:
                    stack.enter_context(mock.patch.object(deployment.w,name,return_value=value))
                stack.enter_context(mock.patch.object(deployment,'_mount',return_value=mounted))
                installed=deployment.install_boot_item_standalone(manifest)
                self.assertTrue(installed['ok']);self.assertTrue(installed['shim_selection']['secure_boot'])
                self.assertEqual(entry.read_bytes(),(ROOT/'assets/fedora/bootmgfw.efi').read_bytes())
                self.assertEqual((entry.parent/'bootmgfw.efi_ANBAK').read_bytes(),original)
                self.assertFalse((root/'AnimeBootNeo/grub2.lnx').exists())
                self.assertFalse(list(root.rglob('*.dat')))
                self.assertTrue(deployment.apply_final_efi_assets(manifest)['ok'])
                # A later layout can leave files outside the old journal, but
                # still inside our dedicated root. Space reporting and update
                # installation must agree on reclaiming this whole tree.
                future=root/'AnimeBootNeo/old-layout/deep/old-animation.bin'
                future.parent.mkdir(parents=True);future.write_bytes(b'A'*(8*1024*1024))
                state=deployment._read_state(root)
                self.assertEqual(state['env_path'],'')
                def used():
                    return sum(((p.stat().st_size+4095)//4096)*4096 for p in root.rglob('*') if p.is_file())
                capacity=used()+12*1024*1024
                def disk_usage(_path):
                    return __import__('collections').namedtuple('usage','total used free')(capacity,used(),capacity-used())
                with mock.patch.object(deployment.shutil,'disk_usage',side_effect=disk_usage):
                    space=deployment.probe_boot_partition_space()
                    self.assertEqual(space['free_bytes'],12*1024*1024)
                    self.assertGreater(space['replaceable_bytes'],space['free_bytes']+8*1024*1024)
                    self.assertTrue(deployment.install_boot_item_standalone(manifest)['ok'])
                self.assertFalse(future.exists())
                self.assertTrue(deployment.uninstall_boot_item_official_equivalent()['ok'])
                self.assertEqual(entry.read_bytes(),original);self.assertEqual(fallback.read_bytes(),original)
                self.assertFalse((root/'AnimeBootNeo').exists())
                self.assertTrue(deployment.uninstall_boot_item_official_equivalent()['already_disabled'])


if __name__=='__main__':unittest.main()
