# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
import plistlib
from pathlib import Path
import tempfile
import unittest
from animeboot_neo.project import default_project, load_project, save_project, normalize_project, validate_project, ProjectError
from animeboot_neo.theme import render_theme
from animeboot_neo.resources import default_theme_assets
from animeboot_neo.builder import build_payload, bind_payload


class CurrentProjectTests(unittest.TestCase):
    def test_current_roundtrip_and_earlier_projects_rejected_without_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'current.anbp'
            project = default_project(640, 480)
            project['timeout'] = 180
            save_project(project, path)
            self.assertEqual(path.read_bytes()[:8], b'ANBR3C1\0')
            self.assertEqual(load_project(path), project)
            for magic, data in ((b'ESRP100\0', dict(project, format_version=14)),
                                (b'ANBP262\0', dict(project, app_version='2610.2', format_version=26102)),
                                (b'ANBR3C1\0', dict(project, app_version='2610.1')),
                                (b'ANBR3C1\0', dict(project, format_version=14))):
                path.write_bytes(magic + plistlib.dumps(data))
                with self.assertRaises(ProjectError):
                    load_project(path)

    def test_legacy_settings_are_not_silently_translated(self):
        for key in ('hotkey', 'patch_guard', 'capacity_mib'):
            project = default_project()
            project[key] = True
            with self.assertRaises(ProjectError):
                normalize_project(project)

    def test_transition_defaults_roundtrip_and_validation(self):
        project = default_project(640, 480)
        self.assertEqual(project['static_windows_transition'], 'blend')
        for mode in ('blend', 'black'):
            with tempfile.TemporaryDirectory() as tmp:
                project['static_windows_transition'] = mode
                path = Path(tmp) / 'transition.anbp'
                save_project(project, path)
                self.assertEqual(load_project(path)['static_windows_transition'], mode)
        project.pop('static_windows_transition')
        normalize_project(project)
        self.assertEqual(project['static_windows_transition'], 'blend')
        project['static_windows_transition'] = 'invalid'
        with self.assertRaises(ProjectError):
            validate_project(project)

    def test_only_circle_emits_required_center_and_dot_assets(self):
        for mode in ('circle', 'bar', 'none'):
            project = default_project(640, 480)
            project['countdown_mode'] = mode
            files, _ = render_theme(project, default_theme_assets(), include_preview=False)
            names = {Path(path).name for path in files}
            self.assertNotIn('logo.png', names)
            self.assertEqual('circle-center.png' in names, mode == 'circle')
            self.assertEqual('tick.png' in names, mode == 'circle')
            if mode == 'circle':
                self.assertIn(b'center_bitmap = "circle-center.png"', next(data for path, data in files.items() if path.endswith('style.txt')))

    def test_static_transition_survives_target_binding_without_new_boot_files(self):
        layouts = {}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for transition in ('blend', 'black'):
                project = default_project(640, 480)
                project['static_windows_transition'] = transition
                project['fix_gfxterm_blank'] = False
                report = build_payload(project, root / transition)
                payload = Path(report['payload_dir'])
                for platform in ('bios', 'uefi'):
                    bound = root / (transition + '-' + platform)
                    metadata = bind_payload(payload, bound, platform,
                        {'filesystem': 'FAT16', 'fs_uuid': '2620-0001'} if platform == 'bios' else None)
                    config = bound / ('files/AnimeBootNeo/grub.cfg' if platform == 'bios'
                                      else 'files/EFI/fedora/grub.cfg')
                    text = config.read_text(encoding='utf-8')
                    self.assertIn('set neo_static_transition=' + transition, text)
                    self.assertNotIn('echo ""', text)
                    self.assertEqual(metadata['config_project']['static_windows_transition'], transition)
                    self.assertEqual(metadata['project']['static_windows_transition'], transition)
                    layouts[(platform, transition)] = set(metadata['files'])
            for platform in ('bios', 'uefi'):
                self.assertEqual(layouts[(platform, 'blend')], layouts[(platform, 'black')])


if __name__ == '__main__':
    unittest.main()
