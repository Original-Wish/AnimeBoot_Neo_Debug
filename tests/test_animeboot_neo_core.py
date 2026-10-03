# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
# -*- coding: utf-8 -*-
import hashlib
import tempfile
import unittest
from pathlib import Path
from PIL import Image

from animeboot_neo.builder import build_payload, verify_payload
from animeboot_neo.grub_config import create_blank_grubenv, generate_config
from animeboot_neo.project import default_project


class AnimeBootNeoCoreTests(unittest.TestCase):
    def test_legacy_state_format_accepts_clear_and_armed_only(self):
        blank = create_blank_grubenv()
        armed = create_blank_grubenv(True)
        self.assertEqual(len(blank), 1024)
        self.assertEqual(len(armed), 1024)
        self.assertNotEqual(blank, armed)
        self.assertTrue(blank.startswith(b"# GRUB Environment Block\n"))

    def test_builder_emits_verified_uefi_and_legacy_payload(self):
        project = default_project(640, 480)
        project["show_debug_info"] = True
        with tempfile.TemporaryDirectory() as directory:
            frames = Path(directory) / "frames"
            frames.mkdir()
            for index in range(2):
                Image.new("RGB", (640, 480), (30 + index * 40, 80, 160)).save(frames / ("%03d.png" % index))
            project["boot_animation"].update({
                "enabled": True, "source": str(frames), "max_frames": 2,
                "extract_frame_rate": 2.0, "frame_rate": 2, "loops": 1,
            })
            result = build_payload(project, directory)
            report = verify_payload(result["payload_dir"])
            root = Path(result["payload_dir"])
            self.assertEqual(report["application"], "AnimeBoot Neo Debug")
            self.assertTrue((root / "core.img").is_file())
            self.assertTrue((root / "boot.img").is_file())
            cfg = (root / "files/AnimeBootNeo/grub.cfg").read_text(encoding="utf-8")
            self.assertIn("AnimeBootNeoMode=WindowsAnimation", cfg)
            self.assertIn("AnimeBootNeoDebug=1", cfg)
            self.assertTrue((root / "files/EFI/Microsoft/Boot/bootmgfw.efi").is_file())

    def test_debug_checkbox_controls_player_pages_without_kernel_white_text(self):
        project = default_project(800, 600)
        project["boot_animation"]["enabled"] = True
        project["show_debug_info"] = False
        cfg = generate_config(project, "bios").decode("utf-8")
        self.assertIn("AnimeBootNeoDebug=0", cfg)
        self.assertIn("quiet loglevel=0", cfg)
        project["show_debug_info"] = True
        cfg = generate_config(project, "bios").decode("utf-8")
        self.assertIn("AnimeBootNeoDebug=1", cfg)
        self.assertIn("quiet loglevel=0", cfg)


if __name__ == "__main__":
    unittest.main()
