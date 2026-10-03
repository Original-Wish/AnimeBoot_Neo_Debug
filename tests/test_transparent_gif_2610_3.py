# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
"""Exact transparent palette pixels must survive GIF disposal and frame encoding."""
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

from PIL import Image, ImageDraw
from animeboot_neo import animation
from animeboot_neo.image_extract import extract_gif_pngs
from animeboot_neo.project import default_project


def make_transparent_gif(path, background, disposal):
    palette = list(background) + [255, 40, 20, 20, 255, 40, 40, 20, 255] + [0, 0, 0] * 252
    frames = []
    for index in range(4):
        frame = Image.new('P', (48, 32), 0)
        frame.putpalette(palette)
        ImageDraw.Draw(frame).rectangle((index * 8 + 2, 2, index * 8 + 10, 18), fill=index % 3 + 1)
        frames.append(frame)
    frames[0].save(path, save_all=True, append_images=frames[1:], transparency=0,
                   duration=[30, 70, 110, 50], disposal=list(disposal), optimize=False, loop=0)
    for frame in frames:
        frame.close()


class TransparentGifTests(unittest.TestCase):
    def check_build(self, background, disposal, codec='jpeg', mode='head', opaque=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'transparent.gif'
            if opaque:
                frames = []
                for index in range(4):
                    frame = Image.new('RGB', (48, 32), background)
                    ImageDraw.Draw(frame).rectangle((index * 8 + 2, 2, index * 8 + 10, 18),
                                                   fill=(40, 80 + index * 40, 180))
                    frames.append(frame)
                frames[0].save(source, save_all=True, append_images=frames[1:],
                    duration=[30, 70, 110, 50], disposal=list(disposal), optimize=False, loop=0)
                for frame in frames:
                    frame.close()
            else:
                make_transparent_gif(source, background, disposal)
            indices, count = animation._image_source_indices(str(source), 7, 40, mode)
            self.assertEqual(count, 4)
            self.assertEqual(indices, [0, 0, 1, 1, 2, 2, 2] if mode == 'head' else [0, 1, 1, 2, 2, 2, 3])
            oracle_rgba = []
            oracle_encoded = []
            with Image.open(source) as image:
                for index in indices:
                    image.seek(index)
                    rgba = image.convert('RGBA')
                    oracle_rgba.append(rgba.tobytes())
                    oracle_encoded.append(animation.encode_jpeg_rgb(rgba, 90) if codec == 'jpeg'
                                          else animation.encode_qoi_rgba(rgba))
                    rgba.close()
            project = default_project(48, 32)
            project['boot_animation'].update(enabled=True, source=str(source), codec=codec,
                jpeg_quality=90, extract_frame_rate=40, max_frames=7, sampling_mode=mode, fit_mode='stretch')
            original_save = animation._save_raw_source_frame
            staged_rgba = []
            def inspect_raw(image, path):
                staged_rgba.append(image.tobytes())
                return original_save(image, path)
            with mock.patch.object(animation.os, 'cpu_count', return_value=4), \
                 mock.patch.object(animation, '_save_raw_source_frame', side_effect=inspect_raw), \
                 mock.patch('animeboot_neo.image_extract.extract_gif_pngs', side_effect=AssertionError('transparent GIF sent to FFmpeg')):
                payload, report = animation.render_linux_animation_payload_file(project,
                    frame_export_dir=str(root / 'preview'), temp_dir=tmp)
            self.assertEqual(report['frame_worker_kind'], 'independent_processes')
            self.assertEqual(staged_rgba, oracle_rgba)
            self.assertEqual(report['build_jpeg_workers'] if codec == 'jpeg' else report['build_qoi_workers'], 3)
            body = Path(payload).read_bytes()
            self.assertEqual(body[:8], b'ANBF262\0')
            offset = 44
            for index, expected in enumerate(oracle_encoded):
                length = struct.unpack_from('<I', body, offset)[0]
                offset += 4
                self.assertEqual(body[offset:offset + length], expected)
                offset += length
                with Image.open(root / 'preview' / ('frame_%05d.png' % (index + 1))) as preview:
                    self.assertEqual(preview.convert('RGBA').tobytes(), oracle_rgba[index])
            self.assertEqual(offset, len(body))
            self.assertFalse(list(root.glob('animebootneo_source_frames_*')))
            self.assertFalse(list(root.glob('animebootneo_*_frames_*')))
            self.assertFalse(list(root.rglob('*.rgba')))

    def test_black_palette_disposal_keep(self):
        self.check_build((0, 0, 0), (1, 1, 1, 1))

    def test_black_palette_disposal_background(self):
        self.check_build((0, 0, 0), (2, 2, 2, 2))

    def test_black_palette_disposal_restore(self):
        self.check_build((0, 0, 0), (1, 2, 3, 1))

    def test_colored_palette_disposal_keep(self):
        self.check_build((17, 29, 83), (1, 1, 1, 1))

    def test_colored_palette_disposal_background(self):
        self.check_build((17, 29, 83), (2, 2, 2, 2))

    def test_colored_palette_disposal_restore(self):
        self.check_build((17, 29, 83), (1, 2, 3, 1))

    def test_qoi_uniform_sampling_preserves_first_last_and_hidden_rgb(self):
        self.check_build((17, 29, 83), (1, 2, 3, 1), codec='qoi', mode='uniform')

    def test_opaque_disposal_restore_uses_exact_pillow_composition(self):
        # FFmpeg 4.2.2 restores an incorrect background for this RGB GIF even
        # though there is no transparent color. Disposal 3 must use raw staging.
        self.check_build((20, 30, 40), (3, 3, 3, 3), opaque=True)

    def test_transparency_in_later_frame_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'late-transparency.gif'
            make_transparent_gif(source, (17, 29, 83), (1, 2, 3, 1))
            body = bytearray(source.read_bytes())
            first_gce = body.find(b'\x21\xf9\x04')
            self.assertGreaterEqual(first_gce, 0)
            body[first_gce + 3] &= 254
            source.write_bytes(body)
            stat = source.stat()
            durations, transparent, restore_previous = animation._gif_control_info(str(source), stat.st_size, stat.st_mtime_ns)
            self.assertEqual(durations, [30., 70., 110., 50.])
            self.assertTrue(transparent)
            self.assertTrue(restore_previous)

    def test_cancelling_serial_composition_removes_raw_sources_and_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'transparent.gif'
            make_transparent_gif(source, (17, 29, 83), (1, 2, 3, 1))
            project = default_project(48, 32)
            project['boot_animation'].update(enabled=True, source=str(source), codec='jpeg',
                extract_frame_rate=40, max_frames=7, fit_mode='stretch')
            cancelled = [False]
            def progress(message):
                if '已保存' in message:
                    cancelled[0] = True
            with self.assertRaises(animation.AnimationError):
                animation.render_linux_animation_payload_file(project,
                    frame_export_dir=str(root / 'preview'), temp_dir=tmp,
                    cancel_check=lambda: cancelled[0], progress=progress)
            self.assertTrue(cancelled[0])
            self.assertFalse((root / 'preview').exists())
            self.assertFalse(list(root.glob('animebootneo_*')))
            self.assertFalse(list(root.rglob('*.rgba')))

    def test_cancellation_from_completed_native_progress_removes_png_sources(self):
        if not animation._find_ffmpeg():
            self.skipTest('FFmpeg required')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'opaque.gif'
            frames = [Image.new('RGB', (24, 24), (index * 70, 40, 80)) for index in range(3)]
            frames[0].save(source, save_all=True, append_images=frames[1:], duration=50, loop=0)
            for frame in frames:
                frame.close()
            cancelled = [False]
            def progress(message):
                if '已完成' in message:
                    cancelled[0] = True
            output = root / 'native'
            with self.assertRaises(animation.AnimationError):
                extract_gif_pngs(str(source), [0, 1, 2], str(output),
                                cancel_check=lambda: cancelled[0], progress=progress)
            self.assertTrue(cancelled[0])
            self.assertFalse(list(output.glob('source_*.png')))

    def test_failed_native_extraction_removes_complete_and_partial_sources(self):
        if not animation._find_ffmpeg():
            self.skipTest('FFmpeg required')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'short.gif'
            Image.new('RGB', (24, 24), 'red').save(source)
            output = root / 'native'
            with self.assertRaises(animation.AnimationError):
                extract_gif_pngs(str(source), [0, 999], str(output))
            self.assertFalse(list(output.glob('source_*.png')))


if __name__ == '__main__':
    unittest.main()
