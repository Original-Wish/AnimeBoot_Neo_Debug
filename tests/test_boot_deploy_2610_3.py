# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
import contextlib,hashlib,json,os,struct,tempfile,unittest,uuid
from pathlib import Path
from unittest import mock
from PIL import Image
from animeboot_neo import builder,deployment,windows
from animeboot_neo.grub_config import (ENV_PATH,BIOS_CONFIG,UEFI_CONFIG,
    create_blank_grubenv,generate_config,embedded_config,validate_boot_binding)
from animeboot_neo.project import default_project

ROOT=Path(__file__).resolve().parents[1]
WINDOWS_EFI=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'Boot/EFI/bootmgfw.efi'


class CurrentBootConfigTests(unittest.TestCase):
    def test_official_grub_parser_accepts_all_current_boot_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'grub.cfg'
            for debug in (False,True):
                for animated in (False,True):
                    for platform in ('bios','uefi'):
                        for transition in ('blend','black'):
                            with self.subTest(debug=debug,animated=animated,platform=platform,transition=transition):
                                project=default_project(640,480)
                                project['show_debug_info']=debug
                                project['boot_animation']['enabled']=animated
                                project['static_windows_transition']=transition
                                path.write_bytes(generate_config(project,platform))
                                builder._verify_grub_script(path)
            path.write_text('if true; then\nfunction invalid_nested {\n echo bad\n}\nfi\n',encoding='ascii')
            with self.assertRaises(builder.BuildError):builder._verify_grub_script(path)

    def test_native_rescue_entry_targets_windows_backup_with_empty_optional_data(self):
        description='Windows Boot Manager'.encode('utf-16le')+b'\0\0'
        filename='\\EFI\\Microsoft\\Boot\\bootmgfw.efi'.encode('utf-16le')+b'\0\0'
        node=b'\x04\x04'+struct.pack('<H',4+len(filename))+filename
        partition=uuid.UUID('8f08d194-c143-4cb8-a663-e52f1cb95860')
        hd=struct.pack('<BBHIQQ16sBB',4,1,42,1,2048,204800,partition.bytes_le,2,2)
        path=hd+node+b'\x7f\xff\x04\0'
        original=struct.pack('<IH',1,len(path))+description+path+b'WINDOWS BCDOBJECT stale trailer'
        values={'BootOrder':struct.pack('<H',1),'Boot0001':original}
        def read(name,guid,max_bytes=None):
            if name not in values:
                raise windows.DeploymentError('读取%s失败（WinError 203）。'%name)
            return values[name],7
        def write(name,guid,data,**kwargs):values[name]=bytes(data)
        with mock.patch.object(windows,'_get_firmware_variable_bytes',side_effect=read), \
             mock.patch.object(windows,'_set_new_firmware_variable_bytes',side_effect=write), \
             mock.patch.object(windows,'_set_firmware_variable_bytes',side_effect=write), \
             mock.patch.object(windows,'_find_free_firmware_boot_number',return_value=2), \
             mock.patch.object(windows,'_target_esp_partition_identity',return_value={
                 'partition_guid':str(partition),'partition_guid_bytes':partition.bytes_le,
                 'partition_number':1,'starting_offset':1048576,'partition_length':104857600}):
            result=windows._create_animebootneo_firmware_entry_details('x64','Z:\\')
        self.assertFalse(result['skipped'])
        self.assertEqual(windows._efi_load_option_file_paths(values['Boot0002']),
                         [r'\EFI\Microsoft\Boot\bootmgfw.efi_ANBAK'])
        self.assertEqual(windows._efi_load_option_optional_data(values['Boot0002']),b'')
        self.assertEqual(values['Boot0001'],original)
        self.assertEqual(struct.unpack('<2H',values['BootOrder']),(1,2))

    def test_firmware_root_and_nested_escape_do_not_search_marker_files(self):
        project=default_project(640,480)
        cfg=generate_config(project,'uefi').decode()
        self.assertIn('"$fw_path"',cfg)
        self.assertNotIn('search ',cfg)
        self.assertIn('rmmod all_video\nrmmod video_bochs\nrmmod video_cirrus',cfg)
        self.assertNotIn('rmmod efi_gop',cfg)
        self.assertNotIn('rmmod efi_uga',cfg)
        self.assertNotIn('insmod ',cfg)
        self.assertNotIn('loadfont ',cfg)
        self.assertIn('configfile ($neo_root)/EFI/fedora/grub.cfg',cfg)
        escape=cfg.split('configfile ($neo_root)/EFI/fedora/grub.cfg',1)[1].split('else',1)[0]
        self.assertIn('neo_windows',escape)
        self.assertNotIn('neo_black',escape)
        self.assertEqual(cfg.count('menuentry '),1)
        self.assertNotIn('bootstate.env',cfg)
        self.assertIn('set color_normal=light-gray/black',cfg.split('function neo_windows {',1)[1].split('}',1)[0])

    def test_black_transitions_and_kernel_quiet_are_independent_of_debug(self):
        for debug in (False,True):
            for animated in (False,True):
                for platform in ('bios','uefi'):
                    with self.subTest(debug=debug,animated=animated,platform=platform):
                        p=default_project(640,480);p['show_debug_info']=debug
                        p['boot_animation']['enabled']=animated
                        cfg=generate_config(p,platform).decode()
                        blackout=cfg.split('function neo_black {',1)[1].split('}',1)[0]
                        self.assertLess(blackout.index('  clear'),blackout.index('  set color_normal=light-gray/black'))
                        if animated:
                            self.assertIn("menuentry 'Windows' --id animeboot-default {\n  neo_black",cfg)
                            self.assertIn('set neo_static_transition=black',cfg)
                            self.assertNotIn('normal_exit',cfg)
                            self.assertIn('quiet loglevel=0 fbcon=nodefer',cfg)
                            self.assertIn('AnimeBootNeoDebug=%d'%debug,cfg)
                            self.assertIn('/'+ENV_PATH,cfg)
                            self.assertLess(cfg.index('neo_arm;'),cfg.index('if linux '))
                        else:
                            self.assertNotIn('load_env',cfg)
                            self.assertIn('set neo_static_transition=blend',cfg)
                            self.assertIn('if [ "$neo_static_transition" = blend ]; then\n    normal_exit',cfg)

    def test_static_choice_is_explicit_and_never_changes_animation_entry(self):
        for platform in ('bios','uefi'):
            configs=[]
            for transition in ('blend','black'):
                project=default_project(640,480)
                project['static_windows_transition']=transition
                cfg=generate_config(project,platform).decode()
                self.assertIn('set neo_static_transition='+transition,cfg)
                self.assertIn('normal_exit\n  else\n    neo_black\n    neo_windows',cfg)
                project['boot_animation']['enabled']=True
                configs.append(generate_config(project,platform))
            self.assertEqual(configs[0],configs[1])

    def test_empty_echo_is_first_executable_command_and_can_be_disabled(self):
        project=default_project(640,480)
        for platform in ('bios','uefi'):
            executable=[line for line in generate_config(project,platform).decode().splitlines()
                        if line.strip() and not line.startswith('#')]
            self.assertEqual(executable[0],'echo ""')
        binding=dict(filesystem='FAT16',fs_uuid='2620-0001')
        self.assertTrue(embedded_config(binding).startswith(b'echo ""\n'))
        project['fix_gfxterm_blank']=False
        for platform in ('bios','uefi'):
            self.assertNotIn(b'echo ""',generate_config(project,platform))
        self.assertNotIn(b'echo ""',embedded_config(binding,False))

    def test_bios_binding_uses_target_filesystem_identity(self):
        binding=dict(filesystem='NTFS',fs_uuid='0123456789ABCDEF')
        text=embedded_config(binding).decode()
        self.assertIn('search --no-floppy --fs-uuid --set=root 0123456789ABCDEF',text)
        self.assertNotIn('--file',text)
        self.assertNotIn('hd0',text)
        self.assertNotIn('neo_black',text)
        with self.assertRaises(ValueError):validate_boot_binding(dict(filesystem='NTFS',fs_uuid='unsafe;halt'))

    def test_bpb_identity_matches_the_grub_ntfs_and_fat_formats(self):
        ntfs=bytearray(512);ntfs[3:11]=b'NTFS    ';ntfs[510:]=b'\x55\xaa'
        struct.pack_into('<Q',ntfs,72,0x1234567890abcdef)
        self.assertEqual(deployment.filesystem_boot_binding(ntfs),dict(filesystem='NTFS',fs_uuid='1234567890ABCDEF'))
        for filesystem,tag,offset in [('FAT16',54,39),('FAT32',82,67)]:
            fat=bytearray(512);fat[tag:tag+8]=(filesystem+'   ').encode();fat[510:]=b'\x55\xaa'
            struct.pack_into('<I',fat,offset,0xdeadbeef)
            self.assertEqual(deployment.filesystem_boot_binding(fat),dict(filesystem=filesystem,fs_uuid='DEAD-BEEF'))


class CurrentPayloadTests(unittest.TestCase):
    def test_static_binding_prunes_inapplicable_platform_and_has_no_animation_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);result=builder.build_payload(default_project(640,480),base/'build')
            source=Path(result['payload_dir']);report=builder.verify_payload(source)
            source_manifest=(source/'manifest.json').read_bytes()
            self.assertEqual(report['format'],'AnimeBootNeo-payload-3-canvases');self.assertEqual(report['app_version'],'2610.3')
            self.assertEqual(report['env_path'],'');self.assertNotIn('anchor',report)
            self.assertFalse((source/'files'/ENV_PATH).exists())
            self.assertFalse((source/'files/EFI/Microsoft/Boot/grub.cfg').exists())
            efi=base/'efi';builder.bind_payload(source,efi,'uefi')
            self.assertEqual(sorted(p.relative_to(efi/'files').as_posix() for p in (efi/'files').rglob('grub.cfg')),[UEFI_CONFIG])
            self.assertFalse((efi/'boot.img').exists());self.assertFalse((efi/'files/AnimeBootNeo/grub2.lnx').exists())
            bios=base/'bios';binding=dict(filesystem='FAT16',fs_uuid='DEAD-BEEF')
            bound=builder.bind_payload(source,bios,'bios',binding)
            self.assertEqual(bound['boot_binding'],binding)
            self.assertEqual(sorted(p.relative_to(bios/'files').as_posix() for p in (bios/'files').rglob('grub.cfg')),[BIOS_CONFIG])
            loader=(bios/'legacy.bootmgr').read_bytes()
            self.assertIn(b'uuid DEAD-BEEF\n',loader)
            self.assertNotIn(b'find --set-root',loader)
            self.assertFalse((bios/'files/EFI').exists())
            self.assertEqual((source/'manifest.json').read_bytes(),source_manifest)

    def test_animation_only_has_fixed_state_and_current_payload_rejects_old_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);frames=base/'frames';frames.mkdir()
            Image.new('RGB',(640,480),'red').save(frames/'001.png')
            project=default_project(640,480);project['boot_animation'].update(enabled=True,source=str(frames),max_frames=1)
            result=builder.build_payload(project,base/'build');source=Path(result['payload_dir'])
            report=builder.verify_payload(source)
            self.assertEqual(report['env_path'],ENV_PATH)
            self.assertEqual((source/'files'/ENV_PATH).read_bytes(),create_blank_grubenv())
            self.assertTrue((source/'files/AnimeBootNeo/anim_initramfs').is_file())
            report['format']='AnimeBootNeo-1';(source/'manifest.json').write_text(json.dumps(report),encoding='utf-8')
            with self.assertRaisesRegex(builder.BuildError,'2610.3'):builder.verify_payload(source)

    def test_old_installation_record_is_rejected_without_touching_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/deployment.BACKUP;folder.mkdir()
            old=dict(format='AnimeBootNeo-installed-1',phase='complete')
            (folder/'state.json').write_text(json.dumps(old))
            with self.assertRaisesRegex(deployment.DeploymentError,'当前版本格式不匹配'):deployment._read_state(root)
            self.assertEqual(json.loads((folder/'state.json').read_text()),old)

    def test_large_file_stream_copy_verifies_before_publishing(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);source=base/'initramfs';target=base/'installed'
            source.write_bytes(b'FRAME'*1000000);target.write_bytes(b'OLD')
            digest=hashlib.sha256(source.read_bytes()).hexdigest()
            with mock.patch.object(Path,'read_bytes',side_effect=AssertionError('whole-file read prohibited')):
                deployment._atomic_file(target,source,digest)
            self.assertEqual(target.stat().st_size,5000000)
            with self.assertRaises(deployment.DeploymentError):deployment._atomic_file(target,source,'0'*64)
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(),digest)
            self.assertFalse((base/'installed.neo-new').exists())


@unittest.skipUnless(os.name=='nt' and WINDOWS_EFI.is_file(),'Windows signed boot manager required')
class CurrentUefiInstallTests(unittest.TestCase):
    def test_install_replace_disable_preserve_unmanaged_fallback_and_single_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);root=base/'esp';entry=root/'EFI/Microsoft/Boot/bootmgfw.efi'
            entry.parent.mkdir(parents=True);original=WINDOWS_EFI.read_bytes();entry.write_bytes(original)
            (entry.parent/'BCD').write_bytes(b'fixture only')
            fallback=root/'EFI/Boot/bootx64.efi';fallback.parent.mkdir();fallback.write_bytes(b'OEM FALLBACK UNMANAGED')
            result=builder.build_payload(default_project(640,480),base/'build')
            manifest=dict(payload_dir=result['payload_dir'],uefi_install_preflight_complete=True,fallback_bootcurrent_repair_choice='keep')
            with contextlib.ExitStack() as stack:
                for name,value in [('check_system_volume_encryption',{}),('_firmware_type','uefi'),
                    ('_secure_boot_enabled',False),('_probe_bootcurrent_boot_path',{'needs_repair':False}),
                    ('_snapshot_firmware_boot_entries',{}),('_create_animebootneo_firmware_entry_details',{'skipped':True}),
                    ('_cleanup_unexpected_new_firmware_boot_options',{}),('_unmount_root',None)]:
                    stack.enter_context(mock.patch.object(deployment.w,name,return_value=value))
                stack.enter_context(mock.patch.object(deployment,'_mount',return_value=dict(root=str(root),boot_mode='uefi')))
                self.assertTrue(deployment.install_boot_item_standalone(manifest)['ok'])
                state=deployment._read_state(root)
                self.assertFalse(any(path.startswith('EFI/Boot/') for path in state['files']))
                self.assertEqual(sorted(p.relative_to(root).as_posix() for p in root.rglob('grub.cfg')),[UEFI_CONFIG])
                self.assertFalse((root/ENV_PATH).exists())
                self.assertEqual(fallback.read_bytes(),b'OEM FALLBACK UNMANAGED')
                self.assertTrue(deployment.install_boot_item_standalone(manifest)['replaced_existing'])
                self.assertTrue(deployment.uninstall_boot_item_official_equivalent()['ok'])
            self.assertEqual(entry.read_bytes(),original)
            self.assertEqual(fallback.read_bytes(),b'OEM FALLBACK UNMANAGED')
            self.assertFalse((root/'EFI/fedora').exists())


if __name__=='__main__':unittest.main()
