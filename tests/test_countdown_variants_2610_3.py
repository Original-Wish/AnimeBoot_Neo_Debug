# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
"""Native signed-GRUB controls stay separate from the no-widget handoff."""
import io
from pathlib import Path
import tempfile
import unittest

from PIL import Image, ImageChops

from animeboot_neo import builder
from animeboot_neo.constants import THEME_ROOT
from animeboot_neo.grub_config import generate_config, static_handoff_strategy
from animeboot_neo.project import (COUNTDOWN_MODES, ProjectError, countdown_key,
                                  countdown_display_name, default_project,
                                  load_project, save_project, validate_project)
from animeboot_neo.resources import default_theme_assets
from animeboot_neo.theme import render_theme, render_preview_scaled


class CountdownVariantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = default_theme_assets()

    def project(self, mode):
        project = default_project(640, 480)
        project.update(countdown_mode=mode, timeout=180)
        # Keep rendering independent of local CJK fonts for these controls.
        project['prompt']['visible'] = False
        return project

    def test_every_mode_roundtrips_and_maps_to_existing_geometry(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'countdown.anbp'
            for mode in COUNTDOWN_MODES:
                project = self.project(mode)
                self.assertTrue(validate_project(project))
                save_project(project, path)
                self.assertEqual(load_project(path)['countdown_mode'], mode)
                self.assertNotEqual(countdown_display_name(mode), '倒计时')
                expected = '' if mode == 'none' else 'circle' if mode.startswith('circle') else 'bar'
                self.assertEqual(countdown_key(mode), expected)
            project['countdown_mode'] = 'video'
            with self.assertRaises(ProjectError):
                validate_project(project)

    def test_all_visible_modes_keep_the_native_timer_in_both_boot_chains(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'check.cfg'
            for mode in COUNTDOWN_MODES:
                for platform in ('uefi', 'bios'):
                    for animation in (False, True):
                        for transition in ('blend', 'black'):
                            project = self.project(mode)
                            project['static_windows_transition'] = transition
                            project['boot_animation']['enabled'] = animation
                            config = generate_config(project, platform).decode()
                            external = mode == 'none' and not animation and transition == 'blend'
                            self.assertEqual(static_handoff_strategy(project),
                                             'external_timer' if external else 'menu_timer')
                            self.assertEqual('sleep --interruptible' in config, external)
                            path.write_text(config, encoding='utf-8')
                            builder._verify_grub_script(path)
                            if animation:
                                self.assertIn('--id animeboot-default {\n  neo_black', config)
                                self.assertIn('quiet loglevel=0', config)

    def test_ring_pngs_are_distinct_transparent_native_ticks(self):
        ticks = []
        for mode in ('circle', 'circle_star', 'circle_heart', 'circle_dash'):
            project = self.project(mode)
            project['circle']['tick_size'] = 32
            files, _ = render_theme(project, self.original, include_preview=False)
            style = files[THEME_ROOT+'/style.txt'].decode()
            self.assertIn('+ circular_progress', style)
            self.assertIn('id = "__timeout__"', style)
            tick = Image.open(io.BytesIO(files[THEME_ROOT+'/tick.png']))
            self.assertEqual(tick.size, (32, 32))
            self.assertEqual(tick.mode, 'RGBA')
            self.assertLess(tick.getchannel('A').getextrema()[0], 255)
            self.assertGreater(tick.getchannel('A').getextrema()[1], 0)
            ticks.append(tick.tobytes())
        self.assertEqual(len(set(ticks)), 4)

    def test_texture_exports_complete_nine_slice_pair_and_referenced_patterns(self):
        project = self.project('bar_texture')
        files, _ = render_theme(project, self.original, include_preview=False)
        names = {Path(name).name for name in files}
        expected = {'%s_%s.png' % (prefix, position)
                    for prefix in ('bar-track', 'bar-fill')
                    for position in ('nw', 'n', 'ne', 'w', 'c', 'e', 'sw', 's', 'se')}
        self.assertTrue(expected.issubset(names))
        style = files[THEME_ROOT+'/style.txt'].decode()
        self.assertIn('bar_style = "bar-track_*.png"', style)
        self.assertIn('highlight_style = "bar-fill_*.png"', style)
        self.assertNotIn('tick.png', names)
        self.assertNotIn('circle-center.png', names)
        self.assertGreater(len(set(Image.open(io.BytesIO(files[THEME_ROOT+'/bar-fill_c.png'])).getdata())), 16)

    def test_small_shape_and_texture_geometry_remain_valid(self):
        for mode in ('circle_star', 'circle_heart', 'circle_dash', 'bar_texture'):
            project = self.project(mode)
            project['circle']['tick_size'] = 1
            project['bar'].update(w=1, h=1, x=639, y=479)
            files, preview = render_theme(project, self.original, preview_fraction=0.5)
            self.assertEqual(preview.size, (640, 480))
            if mode == 'bar_texture':
                self.assertEqual(project['bar']['w'], 4)
                self.assertEqual(project['bar']['h'], 4)
                self.assertEqual(project['bar']['x'], 636)
                self.assertEqual(project['bar']['y'], 476)
            else:
                self.assertEqual(Image.open(io.BytesIO(files[THEME_ROOT+'/tick.png'])).size, (1, 1))

    def test_number_is_native_ascii_label_and_never_loads_external_font(self):
        project = self.project('number')
        project['bar'].update(w=1, h=1, x=639, y=479)
        files, _ = render_theme(project, self.original, include_preview=False)
        self.assertEqual(project['bar']['w'], 24)
        self.assertEqual(project['bar']['h'], 16)
        self.assertEqual(project['bar']['x'], 616)
        self.assertEqual(project['bar']['y'], 464)
        style = files[THEME_ROOT+'/style.txt'].decode()
        self.assertIn('+ label', style)
        self.assertIn('font = "Unknown Regular 16"', style)
        self.assertIn('text = "%d"', style)
        self.assertNotIn('+ progress_bar', style)
        self.assertEqual({Path(name).name for name in files}, {'back.png', 'style.txt'})
        config = generate_config(project, 'uefi').decode()
        self.assertNotIn('loadfont', config)
        self.assertNotIn('.mod', config)

    def test_previews_change_controls_without_baking_them_into_background(self):
        background = None
        for mode in COUNTDOWN_MODES:
            project = self.project(mode)
            first, preview_start = render_theme(project, self.original, preview_fraction=0.0)
            last, preview_end = render_theme(project, self.original, preview_fraction=1.0)
            if background is None:
                background = first[THEME_ROOT+'/back.png']
            self.assertEqual(first[THEME_ROOT+'/back.png'], background)
            self.assertEqual(last[THEME_ROOT+'/back.png'], background)
            difference = ImageChops.difference(preview_start.convert('RGB'), preview_end.convert('RGB'))
            self.assertEqual(difference.getbbox() is None, mode == 'none')
            small = render_preview_scaled(project, self.original, 320, 240, preview_fraction=0.5)
            self.assertEqual(small.size, (320, 240))


if __name__ == '__main__':
    unittest.main()
