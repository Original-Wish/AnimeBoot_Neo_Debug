# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from animeboot_neo.fonts import common_chinese, is_chinese_font, installed_font_paths, resolve_font, supports_text
from animeboot_neo.grub_config import embedded_config, generate_config
from animeboot_neo.project import default_project, normalize_project, validate_project, ProjectError
from animeboot_neo.resources import default_theme_assets


class RevisionTests(unittest.TestCase):
    def test_180_second_current_project_and_invalid_values_rejected(self):
        p=default_project(800,600)
        p.update(timeout=180)
        normalize_project(p);validate_project(p)
        self.assertEqual(p['timeout'],180)
        self.assertNotIn('hotkey',p)
        self.assertEqual(p['prompt']['text'],'按Esc键立刻跳过本次所有开机动画')
        p['timeout']=181
        with self.assertRaises(ProjectError):validate_project(p)

    def test_build_resource_paths_are_absolute_for_both_spec_locations(self):
        path=Path(__file__).resolve().parents[1]/'build.py'
        spec=importlib.util.spec_from_file_location('build2610',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        for onefile in (False,True):
            command,exe=module.build_command(onefile)
            self.assertIn('--onefile' if onefile else '--onedir',command)
            for i,arg in enumerate(command):
                if arg=='--add-data':
                    source=Path(command[i+1].split(';')[0])
                    self.assertTrue(source.is_absolute())
                    self.assertTrue(source.exists())
                    self.assertNotEqual(source.suffix,'.rpm')
            self.assertTrue(Path(command[command.index('--icon')+1]).is_file())

    def test_nested_menu_escape_has_windows_continuation(self):
        p=default_project(800,600);p['boot_animation']['enabled']=True
        cfg=generate_config(p,'bios').decode()
        stub=embedded_config(dict(filesystem='FAT16',fs_uuid='ABCD-1234')).decode()
        self.assertNotIn('--hotkey',cfg)
        self.assertNotIn('legacy.cfg',cfg)
        self.assertEqual(cfg.count('menuentry '),1)
        self.assertNotIn('@NEO_ENV_LBA@',cfg)
        self.assertIn('save_env -f $neo_env neo_once',cfg)
        after=stub.split('configfile ($root)/AnimeBootNeo/grub.cfg',1)[1]
        self.assertIn('ntldr ($root)/AnimeBootNeo/windows.bootmgr',after)
        self.assertNotIn('linux ',after)

    def test_current_user_font_folder_is_discovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)/'Microsoft/Windows/Fonts';folder.mkdir(parents=True)
            f=folder/'UserFont.ttf';f.write_bytes(b'placeholder')
            with mock.patch.dict(os.environ,{'LOCALAPPDATA':tmp}):
                self.assertIn(str(f),installed_font_paths())

    def test_subset_fonts_rejected_and_actual_missing_glyph_falls_back(self):
        required=common_chinese()
        self.assertGreater(len(required),3700)
        with mock.patch('animeboot_neo.fonts.coverage',return_value=frozenset(map(ord,'中文字体'))):
            self.assertFalse(is_chinese_font('fake.ttf'))
        with mock.patch('animeboot_neo.fonts.coverage',return_value=required):
            self.assertTrue(is_chinese_font('fake.ttf'))
        with mock.patch('animeboot_neo.fonts.supports_text',side_effect=lambda p,t:p=='full.ttf'), mock.patch('animeboot_neo.fonts.installed_font_paths',return_value=['subset.ttf','full.ttf']):
            self.assertEqual(resolve_font('subset.ttf','中文生僻字'),'full.ttf')
        with mock.patch('animeboot_neo.fonts.supports_text',return_value=False), mock.patch('animeboot_neo.fonts.installed_font_paths',return_value=[]):
            with self.assertRaisesRegex(ValueError,'方块'):
                resolve_font('broken.ttf','中文')

    def test_bundled_logos_are_original_files_not_generated_placeholder(self):
        from animeboot_neo.constants import THEME_ROOT
        root=Path(__file__).resolve().parents[1]/'assets'
        assets=default_theme_assets()
        self.assertEqual(assets[THEME_ROOT+'/logo.png'],(root/'app_icon_source.png').read_bytes())
        self.assertEqual(assets[THEME_ROOT+'/CompanyLogo.png'],(root/'character.png').read_bytes())
        self.assertFalse(any('/F3.png' in k for k in assets))


if __name__=='__main__':unittest.main()
