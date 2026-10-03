# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
import hashlib
import json
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

class BootProvenanceTests(unittest.TestCase):
    def test_boot_components_match_recorded_independent_sources(self):
        proof=json.loads((ROOT/'教程/BOOT_RESOURCE_PROVENANCE_2610.3.json').read_text(encoding='utf-8'))
        for rec in proof['fedora_origin']+proof['legacy_grub']['files']+[proof['legacy_file_loader']]:
            p=ROOT/rec['file']
            self.assertEqual(p.stat().st_size,rec['size'])
            self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(),rec['sha256'])
        for rec in proof['authenticode_byte_verification']:
            for signature in rec['signatures']:
                self.assertTrue(signature['pe_digest_matches'])
                for signer in signature['signers']:
                    self.assertTrue(signer['cms_signature_valid'])
                    self.assertTrue(signer['cms_content_digest_matches'])
                    self.assertNotIn('eassos',signer['subject'].lower())

    def test_no_old_vendor_boot_resources_or_markers_are_bundled(self):
        forbidden={'erldr','erldr.mbr','grubmbr','esrboot.dat'}
        markers=[b'eassos',b'erldr','易数'.encode('utf-8'),'易数'.encode('utf-16le')]
        for folder in ('assets','animeboot_neo'):
            for p in (ROOT/folder).rglob('*'):
                if not p.is_file() or '__pycache__' in p.parts:continue
                self.assertNotIn(p.name.lower(),forbidden)
                self.assertFalse(any(m in p.read_bytes().lower() for m in markers),str(p))

if __name__=='__main__':unittest.main()
