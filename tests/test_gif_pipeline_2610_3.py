# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock
from PIL import Image, ImageDraw
from animeboot_neo import animation
from animeboot_neo.image_extract import extract_gif_pngs
from animeboot_neo.project import default_project


class GifPipelineTests(unittest.TestCase):
    def make_gif(self, path):
        images = []
        for index in range(5):
            frame = Image.new('RGB', (80, 48), (index * 30, 50, 200))
            ImageDraw.Draw(frame).rectangle((index * 8, 2, index * 8 + 12, 18), fill='white')
            images.append(frame)
        images[0].save(path, save_all=True, append_images=images[1:],
                       duration=[80, 160, 40, 120, 200], loop=0, disposal=2, optimize=True)

    def test_native_selection_matches_pillow_composition_and_variable_durations(self):
        if not animation._find_ffmpeg():
            self.skipTest('FFmpeg required')
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'sample.gif'
            self.make_gif(source)
            for mode in ('head', 'uniform'):
                indices, total = animation._image_source_indices(str(source), 7, 20, mode)
                paths, report = extract_gif_pngs(str(source), indices, str(Path(tmp) / mode))
                expected, _, _ = animation._frames_from_image(str(source), 7, 20, mode)
                for path, frame in zip(paths, expected):
                    with Image.open(path) as native:
                        self.assertEqual(native.convert('RGBA').tobytes(), frame.tobytes())
                    frame.close()
                expected.close()
                self.assertEqual(len(paths), len(indices))
                self.assertEqual(report['png_compression_level'], 0)
                self.assertEqual(total, 5)

    def test_gif_build_uses_native_extraction_and_independent_workers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / 'sample.gif'; self.make_gif(source)
            project = default_project(64, 48)
            project['boot_animation'].update(enabled=True, source=str(source), codec='qoi',
                                            extract_frame_rate=20, max_frames=7)
            with mock.patch.object(animation, '_load_frames', side_effect=AssertionError('serial GIF route')), \
                 mock.patch.object(animation, '_frame_process_worker_count', return_value=(3, 128)):
                path, report = animation.render_linux_animation_payload_file(project, temp_dir=tmp)
            self.assertEqual(report['frame_worker_kind'], 'independent_processes')
            self.assertEqual(report['build_qoi_workers'], 3)
            self.assertEqual(report['video_extraction']['extraction_backend'], 'ffmpeg_gif_exact_indices_rgba_png')
            self.assertEqual(Path(path).read_bytes()[:8], b'ANBF262\0')
            self.assertFalse(list(root.glob('animebootneo_gif_frames_*')))

    def test_cancel_stops_gif_without_partial_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / 'sample.gif'; self.make_gif(source)
            out = root / 'sources'
            with self.assertRaises(animation.AnimationError):
                extract_gif_pngs(str(source), [0, 1], str(out), cancel_check=lambda: True)
            self.assertFalse(list(out.glob('source_*.png')))


if __name__ == '__main__':
    unittest.main()
