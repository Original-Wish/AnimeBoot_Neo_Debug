# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
import copy,hashlib,json,os,tempfile,unittest,zipfile
from pathlib import Path
from unittest import mock
from PIL import Image
from animeboot_neo.project import default_project,validate_project,load_project,ProjectError
from animeboot_neo.theme_package import export_theme,import_theme,ThemePackageError
from animeboot_neo.rotation import initramfs_paths,estimate_rotation_sizes
from animeboot_neo.grub_config import generate_config,create_blank_grubenv,valid_grubenv
from animeboot_neo.builder import build_payload,bind_payload,verify_payload

class ThemeRotationTests(unittest.TestCase):
    def make_project(self,root):
        p=default_project(640,480)
        Image.new('RGB',(48,32),'red').save(root/'logo.png')
        p['logo']['path']=str(root/'logo.png')
        for n,color in enumerate(('red','blue')):
            folder=root/('frames%d'%n);folder.mkdir()
            for i in (10,2,1):Image.new('RGB',(48,32),color).save(folder/('%d.png'%i))
        p['boot_animation'].update(enabled=True,source=str(root/'frames0'),rotation_enabled=True,
            playlist=[{'source':str(root/'frames1')}],max_frames=3,codec='qoi',frame_rate=2)
        return p

    def test_portable_roundtrip_after_original_deleted(self):
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);origin=root/'original';origin.mkdir();p=self.make_project(origin)
            original=copy.deepcopy(p);archive=root/'theme.anbtheme'
            export_theme(p,archive);self.assertEqual(p,original)
            shutil.rmtree(origin)
            loaded=import_theme(archive,root/'imported')
            self.assertTrue(validate_project(loaded));self.assertEqual(loaded,load_project(root/'imported/theme.anbp'))
            self.assertEqual(len(initramfs_paths(loaded)),2)
            with zipfile.ZipFile(archive) as z:
                metadata=z.read('theme.json').decode()
                self.assertNotIn(str(origin),metadata)
                self.assertTrue(all(name=='theme.json' or name.startswith('media/') for name in z.namelist()))

    def test_static_export_excludes_animation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p=self.make_project(root);archive=root/'static.anbtheme'
            export_theme(p,archive,False);loaded=import_theme(archive,root/'imported')
            self.assertFalse(loaded['boot_animation']['enabled'])
            self.assertEqual(loaded['boot_animation']['playlist'],[])
            self.assertEqual(len(list((root/'imported/media').rglob('*.png'))),1)

    def test_sequence_keeps_natural_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p=self.make_project(root)
            source=Path(p['boot_animation']['source'])
            for i,color in ((1,'red'),(2,'green'),(10,'blue')):Image.new('RGB',(2,2),color).save(source/('%d.png'%i))
            archive=root/'theme.anbtheme';export_theme(p,archive)
            loaded=import_theme(archive,root/'loaded')
            folder=Path(loaded['boot_animation']['source'])
            pixels=[Image.open(file).getpixel((0,0)) for file in sorted(folder.iterdir())]
            self.assertEqual(pixels,[(255,0,0),(0,128,0),(0,0,255)])

    def test_reject_unknown_files_and_corruption_without_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p=self.make_project(root);archive=root/'good.anbtheme';export_theme(p,archive)
            with zipfile.ZipFile(archive) as z:entries={n:z.read(n) for n in z.namelist()}
            for label in ('extra','corrupt','external'):
                values=dict(entries)
                if label=='extra':values['../escape.py']=b'bad'
                elif label=='corrupt':values[next(n for n in values if n.startswith('media/'))]=b'bad'
                else:
                    m=json.loads(values['theme.json']);m['project']['logo']['path']='C:/Windows/system.ini';values['theme.json']=json.dumps(m).encode()
                bad=root/(label+'.anbtheme')
                with zipfile.ZipFile(bad,'w') as z:
                    for n,v in values.items():z.writestr(n,v)
                with self.assertRaises((ValueError,KeyError)):import_theme(bad,root/label)
                self.assertFalse((root/label).exists())

    def test_duplicate_zip_entries_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);bad=root/'duplicate.anbtheme'
            with zipfile.ZipFile(bad,'w') as z:z.writestr('theme.json',b'{}');z.writestr('theme.json',b'{}')
            with self.assertRaises(ThemePackageError):import_theme(bad,root/'out')

    def test_playlist_validation_and_static_is_unchanged(self):
        p=default_project(640,480)
        self.assertNotIn(b'neo_rotation',generate_config(p,'uefi'))
        with tempfile.TemporaryDirectory() as tmp:
            p=self.make_project(Path(tmp))
            for invalid in ([{'source':p['boot_animation']['source']}],[{'source':'missing'}],[{'source':'missing'}]*8):
                candidate=copy.deepcopy(p);candidate['boot_animation']['playlist']=invalid
                with self.assertRaises(ProjectError):validate_project(candidate)

    def test_configs_save_once_before_kernel_and_return_consumes_flag_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=self.make_project(Path(tmp))
            for platform in ('bios','uefi'):
                config=generate_config(p,platform).decode()
                self.assertIn('initrd $neo_initrd',config)
                self.assertIn('save_env -f $neo_env neo_once neo_rotation',config)
                self.assertLess(config.index('if [ $neo_once = 1 ]; then'),config.index('set neo_selected=0'))
                self.assertIn('set neo_next=0',config)
            self.assertEqual(len(create_blank_grubenv()),1024)

    def test_rotated_state_verifies_and_can_be_reclaimed(self):
        from animeboot_neo import deployment
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);relative='AnimeBootNeo/bootstate.env';target=root/relative;target.parent.mkdir()
            state={'phase':'complete','mode':'uefi','env_path':relative,
                'files':{relative:{'existed':False,'installed_sha256':hashlib.sha256(create_blank_grubenv()).hexdigest()}}}
            for number in range(8):
                data=create_blank_grubenv(True).replace(b'neo_rotation=0\n',('neo_rotation=%d\n'%number).encode())
                target.write_bytes(data);self.assertTrue(valid_grubenv(data))
                self.assertTrue(deployment._verify(root,state))
            deployment._rollback_files(root,state);self.assertFalse(target.exists())
            self.assertFalse(valid_grubenv(create_blank_grubenv().replace(b'neo_rotation=0',b'neo_rotation=x')))
            self.assertFalse(valid_grubenv(b'X'*1024))

    def test_estimate_accumulates_all_sources(self):
        with mock.patch('animeboot_neo.animation.estimate_animation_codec_sizes',side_effect=[{'qoi_bytes':10,'jpeg_bytes':20,'frame_count':3},{'qoi_bytes':30,'jpeg_bytes':40,'frame_count':5}]) as estimator:
            result=estimate_rotation_sizes(source='a',width=640,height=480,rotation_sources=['b'])
            self.assertEqual(result['qoi_bytes'],40);self.assertEqual(result['jpeg_bytes'],60)
            self.assertEqual(result['frame_count'],8);self.assertEqual(estimator.call_args.kwargs['source'],'b')

    def test_real_build_bind_and_manifest_both_platforms(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p=self.make_project(root)
            report=build_payload(p,root/'output');source=Path(report['payload_dir'])
            manifest=verify_payload(source)
            self.assertEqual(manifest['animation']['rotation'],'sequential')
            self.assertNotIn('files/AnimeBootNeo/anim_initramfs',manifest['files'])
            for platform in ('bios','uefi'):
                target=root/platform
                bind_payload(source,target,platform,None if platform=='uefi' else {'filesystem':'FAT16','fs_uuid':'2620-0001'})
                bound=verify_payload(target)
                self.assertTrue(all('files/'+n in bound['files'] for n in initramfs_paths(p)))

if __name__=='__main__':unittest.main()
