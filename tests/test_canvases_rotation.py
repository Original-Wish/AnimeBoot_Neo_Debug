# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
import copy, json, os, tempfile, unittest
from pathlib import Path
from unittest import mock
from PIL import Image
from animeboot_neo.project import (default_project, save_project, load_project,
                                  validate_project, normalize_project, ProjectError)
from animeboot_neo.scenes import (capture_canvas, sync_canvas, select_canvas,
                                 resize_canvases, theme_paths)
from animeboot_neo.grub_config import generate_config, static_handoff_strategy, valid_grubenv, create_blank_grubenv
from animeboot_neo.theme import render_canvases
from animeboot_neo.theme_package import export_theme, import_theme
from animeboot_neo.resources import default_theme_assets
from animeboot_neo.rotation import estimate_rotation_sizes, estimate_signature
from animeboot_neo.builder import build_payload, bind_payload, verify_payload


def two_canvases(root):
    project=default_project(640,480)
    for n,color in enumerate(('red','blue')):
        Image.new('RGB',(40,30),color).save(root/('%d.png'%n))
    project['logo']['path']=str(root/'0.png')
    sync_canvas(project)
    project['canvases'].append({'name':'画布 B','canvas':capture_canvas(project)})
    select_canvas(project,1)
    project['logo']['path']=str(root/'1.png')
    project['prompt']['text']='第二张';project['countdown_mode']='circle_star'
    project['circle'].update(x=50,y=60,w=110,h=110,num_ticks=12)
    sync_canvas(project);select_canvas(project,0)
    return project


class CanvasRotationTests(unittest.TestCase):
    def test_live_edits_and_independent_countdown_survive_switch_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p=two_canvases(root)
            p['prompt']['text']='第一张编辑';p['countdown_mode']='bar';p['bar']['fg']='#3399FF'
            select_canvas(p,1)
            self.assertEqual(p['countdown_mode'],'circle_star')
            self.assertEqual(p['circle']['num_ticks'],12)
            select_canvas(p,0)
            self.assertEqual(p['bar']['fg'],'#3399FF');self.assertEqual(p['prompt']['text'],'第一张编辑')
            save_project(p,root/'p.anbp');loaded=load_project(root/'p.anbp')
            self.assertEqual(loaded,p)
            self.assertNotIn('neo_static_rotation',loaded)

    def test_all_canvases_rule_and_animation_handoff_independent(self):
        p=default_project(640,480)
        p['canvases'].append({'name':'B','canvas':capture_canvas(p)})
        self.assertEqual(static_handoff_strategy(p),'external_timer')
        p['canvases'][1]['canvas']['countdown_mode']='number'
        self.assertEqual(static_handoff_strategy(p),'menu_timer')
        p['canvases'][1]['canvas']['countdown_mode']='none'
        p['boot_animation']['enabled']=True
        self.assertEqual(static_handoff_strategy(p),'menu_timer')
        for platform in ('bios','uefi'):
            config=generate_config(p,platform).decode()
            self.assertIn('  neo_black\n  set gfxpayload=keep',config)

    def test_hidden_resources_validated_and_recoverable_on_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p=two_canvases(root);save_project(p,root/'p.anbp')
            (root/'1.png').unlink()
            with self.assertRaises(ProjectError):validate_project(p)
            loaded,missing=load_project(root/'p.anbp',tolerate_missing_images=True,return_missing_images=True)
            self.assertTrue(missing);select_canvas(loaded,1)
            self.assertTrue(loaded['logo']['removed'])

    def test_shared_resolution_scales_all_scenes(self):
        p=default_project(640,480);p['canvases'].append({'name':'B','canvas':capture_canvas(p)})
        original=copy.deepcopy(p)
        resize_canvases(p,1280,960)
        self.assertEqual(p['resolution'],[1280,960])
        for n,scene in enumerate(p['canvases']):
            self.assertEqual(scene['canvas']['logo']['w'],original['canvases'][n]['canvas']['logo']['w']*2)
        select_canvas(p,1);self.assertEqual(p['logo']['w'],p['canvases'][0]['canvas']['logo']['w'])

    def test_invalid_scene_schema_rejected_and_t1_magic_not_accepted(self):
        for field,value in (('active_canvas',-1),('active_canvas',True),('canvases',[])):
            p=default_project();p[field]=value
            with self.assertRaises(ProjectError):normalize_project(p)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'old.anbp';save_project(default_project(),path)
            path.write_bytes(path.read_bytes().replace(b'ANBR3C1',b'INVALID',1))
            with self.assertRaises(ProjectError):load_project(path)

    def test_styles_match_scene_controls_and_duplicate_pngs_are_shared(self):
        p=default_project(640,480);p['canvases'].append({'name':'B','canvas':capture_canvas(p)})
        assets=render_canvases(p,default_theme_assets())
        backs=[key for key in assets if '/back-' in key]
        self.assertEqual(len(backs),1)
        self.assertEqual(len([key for key in assets if '/style-' in key]),2)
        select_canvas(p,1);p['countdown_mode']='circle_star'
        assets=render_canvases(p,default_theme_assets())
        self.assertNotIn(b'circular_progress',assets['/AnimeBootNeo/theme/style-00.txt'])
        self.assertIn(b'circular_progress',assets['/AnimeBootNeo/theme/style-01.txt'])
        self.assertIn(b'back-00.png',assets['/AnimeBootNeo/theme/style-01.txt'])

    def test_share_all_scenes_portable_after_source_removed(self):
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source';source.mkdir();p=two_canvases(source)
            export_theme(p,root/'p.anbtheme',False);shutil.rmtree(source)
            loaded=import_theme(root/'p.anbtheme',root/'imported')
            self.assertTrue(validate_project(loaded));self.assertEqual(len(loaded['canvases']),2)
            for index in (0,1):
                select_canvas(loaded,index);self.assertTrue(Path(loaded['logo']['path']).is_file())
            self.assertEqual(loaded['countdown_mode'],'circle_star')

    def test_config_advances_static_only_in_draw_context_after_once_consumption(self):
        p=default_project(640,480);p['canvases'].append({'name':'B','canvas':capture_canvas(p)})
        p['boot_animation'].update(enabled=True,source='frames')
        for platform in ('bios','uefi'):
            text=generate_config(p,platform).decode()
            self.assertLess(text.index('if [ $neo_once = 1 ]; then'),text.index('set neo_static_next=1'))
            self.assertEqual(text.count('save_env -f $neo_env neo_static_rotation'),1)
            self.assertIn('style-01.txt',text);self.assertIn('set neo_static_next=0',text)
        p['timeout']=0
        self.assertNotIn(b'save_env -f $neo_env neo_static_rotation',generate_config(p,'uefi'))

    def test_static_state_block_rejects_invalid_and_verifies_all_counters(self):
        for n in range(8):
            data=create_blank_grubenv().replace(b'neo_static_rotation=0',('neo_static_rotation=%d'%n).encode())
            self.assertTrue(valid_grubenv(data))
        self.assertFalse(valid_grubenv(create_blank_grubenv().replace(b'neo_static_rotation=0',b'neo_static_rotation=x')))

    def test_actual_multistatic_payload_bind_has_one_config_and_no_linux(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p=two_canvases(root);result=build_payload(p,root/'out')
            source=Path(result['payload_dir']);report=verify_payload(source)
            self.assertEqual(report['env_path'],'AnimeBootNeo/bootstate.env')
            self.assertFalse(any('bzImage' in path or 'initramfs' in path for path in report['files']))
            self.assertEqual(report['config_project']['static_canvas_modes'],['none','circle_star'])
            self.assertNotIn('canvases',report['config_project'])
            for mode in ('bios','uefi'):
                bind_payload(source,root/mode,mode,None if mode=='uefi' else {'filesystem':'FAT16','fs_uuid':'2620-0001'})
                bound=verify_payload(root/mode)
                self.assertEqual(sum(path.endswith('/grub.cfg') for path in bound['files']),1)
                self.assertTrue(all('files/'+path in bound['files'] for path in theme_paths(p)))

    def test_estimate_only_selected_codec_sums_every_animation_and_preserves_items(self):
        with mock.patch('animeboot_neo.animation.estimate_animation_codec_sizes',side_effect=[
            {'jpeg_bytes':10,'planned_frame_count':3,'estimated_codecs':['jpeg']},
            {'jpeg_bytes':20,'planned_frame_count':4,'estimated_codecs':['jpeg']},
            {'jpeg_bytes':30,'planned_frame_count':5,'estimated_codecs':['jpeg']}]) as estimator:
            result=estimate_rotation_sizes(source='main',rotation_sources=['one','two'],estimate_codecs=('jpeg',))
            self.assertEqual(result['jpeg_bytes'],60);self.assertEqual(result['planned_frame_count'],12)
            self.assertEqual([item['source'] for item in result['items']],['main','one','two'])
            self.assertTrue(all(call.kwargs['estimate_codecs']==('jpeg',) for call in estimator.call_args_list))

    def test_approval_signature_changes_after_source_deleted_or_settings_edited(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p=two_canvases(root);p['boot_animation']['source']=str(root/'0.png')
            signature=estimate_signature(p);p['boot_animation']['jpeg_quality']=80
            self.assertNotEqual(signature,estimate_signature(p))
            signature=estimate_signature(p);(root/'0.png').unlink()
            self.assertNotEqual(signature,estimate_signature(p))


if __name__=='__main__':unittest.main()
