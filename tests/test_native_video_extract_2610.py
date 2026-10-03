# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
# -*- coding: utf-8 -*-
"""Native extraction retains video timing, source RGBA and cancellation."""
import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from PIL import Image
from animeboot_neo import animation
from animeboot_neo import video_extract


def _pixels(image):
    return hashlib.sha256(image.convert("RGBA").tobytes()).hexdigest()


class NativeVideoExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not animation._find_ffmpeg():
            raise unittest.SkipTest("FFmpeg not available")
        cls.root = tempfile.mkdtemp(prefix="native_video_2610_")
        cls.video = os.path.join(cls.root, "fractional.mp4")
        subprocess.run([animation._find_ffmpeg(), "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", "testsrc2=size=96x64:rate=30000/1001",
                        "-frames:v", "300", "-c:v", "libx264", "-preset", "ultrafast",
                        "-crf", "18", "-y", cls.video], check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def _compare(self, source, maximum, fps, mode):
        directory = tempfile.mkdtemp(dir=self.root)
        paths, count, kind, diagnostics = video_extract.extract_video_pngs(
            source, maximum, fps, mode, directory)
        frames, old_count, old_kind = animation._frames_from_video(source, maximum, fps, mode)
        expected = []
        for frame in frames:
            expected.append(_pixels(frame))
            frame.close()
        actual = []
        for path in paths:
            with Image.open(path) as frame:
                actual.append(_pixels(frame))
        self.assertEqual(actual, expected)
        self.assertEqual(count, old_count)
        self.assertEqual(kind, old_kind)
        self.assertEqual(diagnostics["extracted_frames"], len(actual))
        return actual

    def test_fractional_native_keeps_all_300_frames(self):
        self.assertEqual(len(self._compare(self.video, 300, 29.97, "head")), 300)

    def test_uniform_keeps_original_first_and_last(self):
        actual = self._compare(self.video, 17, 29.97, "uniform")
        all_frames, _, _ = animation._frames_from_video(self.video, 300, 29.97, "head")
        hashes = []
        for frame in all_frames:
            hashes.append(_pixels(frame))
            frame.close()
        self.assertEqual(len(actual), 17)
        self.assertEqual(actual[0], hashes[0])
        self.assertEqual(actual[-1], hashes[-1])

    def test_resampled_head_matches_existing_timing(self):
        self._compare(self.video, 180, 15, "head")

    def test_resampled_uniform_matches_existing_timing(self):
        self.assertEqual(len(self._compare(self.video, 17, 15, "uniform")), 17)

    def test_rgba_alpha_is_lossless(self):
        source_dir = tempfile.mkdtemp(dir=self.root)
        originals = []
        for index in range(12):
            frame = Image.new("RGBA", (37, 23))
            frame.putdata([((x * 3 + index * 11) % 256, (x * 7 + index) % 256,
                            (x * 13 + index * 3) % 256, (x * 17 + index * 5) % 256)
                           for x in range(37 * 23)])
            originals.append(_pixels(frame))
            frame.save(os.path.join(source_dir, "%02d.png" % index))
            frame.close()
        video = os.path.join(source_dir, "alpha.mkv")
        subprocess.run([animation._find_ffmpeg(), "-hide_banner", "-loglevel", "error",
                        "-framerate", "30", "-i", os.path.join(source_dir, "%02d.png"),
                        "-c:v", "ffv1", "-pix_fmt", "bgra", "-y", video], check=True)
        self.assertEqual(self._compare(video, 12, 30, "head"), originals)

    def test_cancel_stops_native_extraction_and_removes_frames(self):
        directory = tempfile.mkdtemp(dir=self.root)
        cancel = [False]
        def progress(_message):
            cancel[0] = True
        with self.assertRaisesRegex(animation.AnimationError, "用户已放弃制作"):
            video_extract.extract_video_pngs(self.video, 300, 29.97, "head", directory,
                                             cancel_check=lambda: cancel[0], progress=progress)
        self.assertEqual(os.listdir(directory), [])

    def test_stalled_partial_frame_is_stopped_and_removed(self):
        directory = tempfile.mkdtemp(dir=self.root)
        process = mock.Mock()
        process.poll.side_effect = [None, None]
        process.wait.return_value = -15
        def start(command, **_kwargs):
            with open(command[-1] % 1, "wb") as stream:
                stream.write(b"\x89PNG\r\n\x1a\npartial")
            return process
        with mock.patch.object(video_extract, "_source_metadata", return_value=(96, 64, 30, 10, 300)), \
             mock.patch.object(video_extract, "_STALL_SECONDS", 0), \
             mock.patch.object(video_extract.subprocess, "Popen", side_effect=start):
            with self.assertRaisesRegex(animation.AnimationError, "没有生成新的完整动画帧"):
                video_extract.extract_video_pngs(self.video, 300, 30, "head", directory)
        process.terminate.assert_called_once()
        process.wait.assert_called_once()
        self.assertEqual(os.listdir(directory), [])


if __name__ == "__main__":
    unittest.main()
