# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
# -*- coding: utf-8 -*-
import os
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]

from animeboot_neo.animation import (
    ANBF_CODEC_JPEG,
    ANBF_CODEC_QOI,
    ANBF_VERSION,
    estimate_animation_codec_sizes,
    render_linux_animation_payload_file,
)
from animeboot_neo.project import default_project, normalize_project, validate_project


class DualAnimationCodecTests(unittest.TestCase):
    def _frames(self, root):
        folder = Path(root) / "frames"
        folder.mkdir()
        for index in range(3):
            image = Image.new("RGB", (320, 200), (25 + index * 20, 35, 55))
            draw = ImageDraw.Draw(image)
            for x in range(0, 320, 4):
                draw.line((x, 0, (x + index * 9) % 320, 199),
                          fill=((x * 3 + index * 17) % 256, (x * 7) % 256, (x * 11) % 256))
            image.save(str(folder / ("%03d.png" % index)))
        return str(folder)

    def _project(self, source, codec):
        project = default_project()
        project["resolution"] = [320, 200]
        project["boot_animation"].update({
            "enabled": True,
            "source": source,
            "max_frames": 3,
            "extract_frame_rate": 30.0,
            "sampling_mode": "head",
            "frame_rate": 30,
            "loops": 1,
            "fit_mode": "stretch",
            "codec": codec,
            "jpeg_quality": 90,
        })
        return project

    def test_project_defaults_and_validation(self):
        project = default_project()
        self.assertEqual(project["boot_animation"]["codec"], "jpeg")
        self.assertEqual(project["boot_animation"]["jpeg_quality"], 90)
        legacy = default_project()
        legacy["boot_animation"].pop("codec", None)
        legacy["boot_animation"].pop("jpeg_quality", None)
        normalize_project(legacy)
        self.assertEqual(legacy["boot_animation"]["codec"], "jpeg")
        self.assertEqual(legacy["boot_animation"]["jpeg_quality"], 90)

    def test_esra_v2_codec_flag_and_size_estimator(self):
        self.assertEqual(ANBF_VERSION, 2)
        with tempfile.TemporaryDirectory() as temp:
            source = self._frames(temp)
            sizes = {}
            outputs = {}
            for codec, codec_id in (("qoi", ANBF_CODEC_QOI), ("jpeg", ANBF_CODEC_JPEG)):
                project = self._project(source, codec)
                validate_project(project)
                path, report = render_linux_animation_payload_file(project, temp_dir=temp)
                outputs[codec] = Path(temp) / (codec + ".anbf")
                os.replace(path, str(outputs[codec]))
                sizes[codec] = report["payload_size"]
                with open(str(outputs[codec]), "rb") as stream:
                    header = stream.read(44)
                values = struct.unpack("<8s9I", header)
                self.assertEqual(values[0], b"ANBF262\0")
                self.assertEqual(values[1], 2)
                self.assertEqual(values[-1], codec_id)
                self.assertEqual(report["codec"], codec)
            self.assertLess(sizes["jpeg"], sizes["qoi"])
            estimate = estimate_animation_codec_sizes(source, 320, 200, "stretch", 3, 30.0, "head", 90)
            self.assertEqual(estimate["planned_frame_count"], 3)
            self.assertLess(estimate["jpeg_bytes"], estimate["qoi_bytes"])
            self.assertIn("jpeg_xz_ratio", estimate)
            self.assertNotIn("recommendation", estimate)
            self.assertNotIn("content_metrics", estimate)

            player = ROOT / "assets" / "linux" / "animeboot_player_x86_64"
            if os.name != "nt" and os.access(str(player), os.X_OK):
                for codec in ("qoi", "jpeg"):
                    completed = subprocess.run([str(player), "--selftest", str(outputs[codec])],
                                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                               text=True, check=False)
                    self.assertEqual(completed.returncode, 0, completed.stdout)
                    self.assertIn("SELFTEST OK v=2 codec=" + codec.upper(), completed.stdout)
                    self.assertRegex(completed.stdout, r"checksum=[0-9a-f]{16}")

    def test_estimator_does_not_auto_choose_codec_from_content(self):
        with tempfile.TemporaryDirectory() as temp:
            source = self._frames(temp)
            result = estimate_animation_codec_sizes(source, 320, 200, "stretch", 3, 30.0, "head", 90)
            self.assertNotIn("recommendation", result)
            self.assertNotIn("content_metrics", result)
            self.assertGreater(result["qoi_bytes"], 0)
            self.assertGreater(result["jpeg_bytes"], 0)
            self.assertGreater(result["jpeg_xz_ratio"], 0.0)

    def test_source_and_ui_expose_both_codecs(self):
        source = (ROOT / "assets" / "linux" / "animeboot_player.c").read_text(encoding="utf-8")
        self.assertIn("decode_jpeg_native", source)
        self.assertIn("ANBF_CODEC_JPEG", source)
        self.assertIn('#define ANBF_MAGIC "ANBF262\\0"', source)
        self.assertIn('#define JCX_MAGIC "ANBJCX2\\0"', source)
        self.assertIn("SELFTEST OK v=%u codec=%s", source)
        build = (ROOT / "assets" / "linux" / "build_player_mt.sh").read_text(encoding="utf-8")
        self.assertIn("-ljpeg", build)
        gui = (ROOT / "animeboot_neo" / "qt_gui.py").read_text(encoding="utf-8")
        self.assertIn("QOI 无损高速", gui)
        self.assertIn("JPEG 高压缩", gui)
        self.assertIn("点我来估算编码体积", gui)
        self.assertIn("自动建议", gui)
        # Black-screen exploration branch intentionally uses the widest-compatible
        # direct framebuffer path: no fbdev/DRM synchronization and no active
        # presentation skipping. Every encoded frame is presented in order.
        self.assertNotIn("FBIO_WAITFORVSYNC", source)
        self.assertNotIn("FBIOGET_VBLANK", source)
        self.assertNotIn("FBIOPAN_DISPLAY", source)
        self.assertNotIn("FB_ACTIVATE_VBL", source)
        self.assertNotIn("DRM_IOCTL_", source)
        self.assertIn("FASTFIRST-NODROP-DYNAMIC-JCX2-FBDEBUG8S", source)
        self.assertIn("Absolute no-drop schedule", source)
        self.assertIn("No sequence is deliberately skipped", source)
        self.assertNotIn("skipped_presentations", source)
        self.assertNotIn("ctx->period_ns=ctx->refresh_period_ns", source)
        self.assertNotIn("AnimeBootNeoBootTiming", source)
        self.assertIn("当前 Windows 主屏幕约", gui)
        self.assertIn("屏幕不会完整显示更多整帧", gui)
        self.assertIn("撕裂或残影", gui)
        self.assertIn("不主动跳过动画帧", gui)
        win_display = (ROOT / "animeboot_neo" / "win_display.py").read_text(encoding="utf-8")
        self.assertIn("get_current_display_refresh_rate", win_display)
        self.assertIn("dmDisplayFrequency", win_display)


if __name__ == "__main__":
    unittest.main()
