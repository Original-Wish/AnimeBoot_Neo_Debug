# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from animeboot_neo.builder import build_payload, verify_payload
from animeboot_neo.project import default_project

ROOT=Path(__file__).resolve().parents[1]

class GrubPairTests(unittest.TestCase):
    def test_both_choices_build_matching_bios_and_signed_uefi_release(self):
        pairs=json.loads((ROOT/'assets/grub/grub-pairs.json').read_text(encoding='utf-8'))
        for mode,release,folder,efi in [('new','64','i386-pc','grubx64.efi'),
                                      ('old','56','i386-pc_compat','grubx64_compat.efi')]:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                p=default_project(640,480);p['x64_grub_compat_mode']=mode
                result=build_payload(p,tmp);root=Path(result['payload_dir'])
                report=verify_payload(root)
                version='2.12-%s.fc44'%release
                self.assertEqual(report['grub_versions'],dict(bios=version,uefi=version))
                self.assertEqual(report['grub_versions'],pairs['pairs'][mode])
                signed=(ROOT/'assets/fedora'/efi).read_bytes()
                self.assertIn(('grub2,'+version+',').encode(),signed)
                self.assertEqual((root/'files/EFI/Microsoft/Boot/grubx64.efi').read_bytes(),signed)
                core=(root/'core.img').read_bytes()
                self.assertEqual((root/'files/AnimeBootNeo/grub2.lnx').read_bytes(),
                                 (ROOT/'assets/grub'/folder/'lnxboot.img').read_bytes()+core)
                self.assertGreater(len(core),32768)

    def test_signed_fedora_files_remain_the_recorded_unmodified_bytes(self):
        proof=json.loads((ROOT/'教程/BOOT_RESOURCE_PROVENANCE_2610.3.json').read_text(encoding='utf-8'))
        for rec in proof['fedora_origin']:
            if rec['file'].endswith('.efi'):
                self.assertFalse(rec['byte_modified'])
                self.assertEqual(hashlib.sha256((ROOT/rec['file']).read_bytes()).hexdigest(),rec['sha256'])

if __name__=='__main__':unittest.main()
