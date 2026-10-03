# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
"""Build current ordinary boot files and bind BIOS files before installation."""

# 制作器只向用户选择的普通输出目录生成文件，不在本模块写物理磁盘、ESP 或固件变量。
# build_payload 生成两种启动方式的未绑定 InstallPayload；bind_payload 再按实际平台复制成独立待安装包并删除另一平台文件。
# Legacy 绑定目标文件系统 UUID，UEFI 使用固件传入的设备路径。不要把 Windows 盘符写死到 GRUB 配置。
# 先在临时目录完成渲染/编码/哈希校验，再发布最终目录；取消或异常只清理本次暂存数据，避免留下看似完成的半成品。
# manifest 记录生成内容的大小和 SHA256。它用于一致性校验，不是数字签名，也不证明来源可信。

import copy,json,os,shutil,subprocess,tempfile
from pathlib import Path
from .animation import render_linux_animation_payload_file
from .project import validate_project
from .resources import default_theme_assets,validate_components,resource_root,digest
from .runtime import build_animation_initramfs
from .grub_config import (ENV_PATH,BIOS_CONFIG,UEFI_CONFIG,create_blank_grubenv,
                          generate_config,embedded_config,validate_boot_binding,
                          static_handoff_strategy)
from .theme import render_canvases
from .scenes import countdown_modes, theme_paths
from .legacy_file import make_bootmgr
from .quiet_boot import silent_bios_modules
from .rotation import animation_projects,initramfs_paths

class BuildError(Exception):pass

PAYLOAD_FORMAT='AnimeBootNeo-payload-3-canvases'
APP_VERSION='2610.3'
ANIMATION_FRAME_EXPORT_DIR='提取的每帧PNG预览图片'


def _boot_project(project):
    result = {key:copy.deepcopy(project[key]) for key in
            ('resolution','timeout','show_debug_info','fix_gfxterm_blank','boot_animation',
             'x64_grub_compat_mode','static_windows_transition','countdown_mode') if key in project}
    result['static_canvas_modes']=countdown_modes(project)
    return result


def _build_bios(root, project, boot_binding=None):
    tools=resource_root()/'assets/grub'
    mode='old' if project.get('x64_grub_compat_mode')=='old' else 'new'
    with tempfile.TemporaryDirectory(prefix='.neo-bios-',dir=str(root.parent)) as scratch:
        scratch=Path(scratch)
        modules=silent_bios_modules(tools/('i386-pc_compat' if mode=='old' else 'i386-pc'),scratch/'modules')
        # The static-only nested menu needs a normal-mode entry so normal_exit
        # works on BIOS as it does in the signed EFI interpreter. Animation
        # retains its existing configfile entry and black-to-Linux path.
        normal_menu=not project.get('boot_animation',{}).get('enabled')
        embed=scratch/'embedded.cfg';embed.write_bytes(embedded_config(
            boot_binding,project.get('fix_gfxterm_blank',True),normal_menu))
        core=root/'core.img'
        command=[str(tools/'grub-mkimage.exe'),'-d',str(modules),'-p','/AnimeBootNeo',
                 '-c',str(embed),'-o',str(core),'-O','i386-pc']
        command+='biosdisk part_msdos fat ntfs normal configfile search search_fs_uuid loadenv test echo reboot halt linux chain ntldr gfxterm gfxmenu vbe neo_vbe png jpeg font'.split()
        if static_handoff_strategy(project) == 'external_timer':
            command.append('sleep')
        result=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                              creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),timeout=60)
        if result.returncode:raise BuildError('Legacy GRUB 核心生成失败：'+result.stderr.decode('utf-8','replace'))
        shutil.copy2(resource_root()/'assets/legacy/mbr.bin',root/'boot.img')
        (root/'files'/BIOS_CONFIG).parent.mkdir(parents=True,exist_ok=True)
        (root/'files'/BIOS_CONFIG).write_bytes(generate_config(project,'bios'))
        (root/'files/AnimeBootNeo/grub2.lnx').write_bytes((modules/'lnxboot.img').read_bytes()+core.read_bytes())
        (root/'legacy.bootmgr').write_bytes(make_bootmgr((tools/'grldr').read_bytes(),boot_binding))


def _write_manifest(root, report):
    report=copy.deepcopy(report);report['files']={}
    for path in root.rglob('*'):
        if path.is_file() and path.name!='manifest.json':
            report['files'][path.relative_to(root).as_posix()]={'size':path.stat().st_size,'sha256':digest(path)}
    (root/'manifest.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report


def _verify_grub_script(path):
    checker=resource_root()/'assets/grub/grub-script-check.exe'
    try:
        result=subprocess.run([str(checker),str(path)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                              creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),timeout=15)
    except (OSError,subprocess.TimeoutExpired) as error:
        raise BuildError('GRUB 配置语法检查未完成：'+str(error)) from error
    if result.returncode:
        detail=(result.stdout+result.stderr).decode('utf-8','replace').strip()
        raise BuildError('GRUB 配置语法检查失败：'+str(path)+'\n'+detail)


# 复制已校验的通用制作结果，在独立目录按实际引导方式裁剪和绑定。原结果保持可重试，目标绑定失败时删除本次副本。
def bind_payload(source_payload,destination,mode,boot_binding=None):
    """Prepare and verify a target-specific package before changing boot files.

    The original build stays untouched. A BIOS target is identified by its
    filesystem serial, never a generated marker filename or Windows disk index.
    EFI configuration obtains its actual device from Fedora's firmware fw_path.
    """
    source=Path(source_payload).resolve();report=verify_payload(source)
    if report.get('bound_boot_mode'):raise BuildError('安装源必须是当前版本未绑定制作结果。')
    if mode not in ('uefi','bios'):raise BuildError('不支持的启动方式。')
    binding=validate_boot_binding(boot_binding) if mode=='bios' else None
    target=Path(destination).resolve()
    if target==source or source in target.parents or target in source.parents:
        raise BuildError('安装准备目录不能覆盖制作结果。')
    if target.exists():raise BuildError('安装准备目录已经存在。')
    shutil.copytree(str(source),str(target))
    try:
        if mode=='bios':
            shutil.rmtree(str(target/'files/EFI'))
            _build_bios(target,report['config_project'],binding)
        else:
            for name in ('boot.img','core.img','legacy.bootmgr','files/'+BIOS_CONFIG,
                         'files/AnimeBootNeo/grub2.lnx','files/AnimeBootNeo/unicode.pf2'):
                (target/name).unlink()
        report['bound_boot_mode']=mode
        report['boot_binding']=binding
        report['locator']='filesystem-uuid' if mode=='bios' else 'firmware-device'
        report=_write_manifest(target,report);verify_payload(target)
        return report
    except Exception:
        shutil.rmtree(str(target),ignore_errors=True)
        raise


# 制作入口只接受完整有效工程。顺序先静图后各段动画，全部成功才发布输出目录；进度回调同时承担取消检查。
def build_payload(project,output_dir,font_path='',cancel_check=None,progress=None):
    validate_project(project);validate_components()
    output=Path(output_dir).resolve();output.mkdir(parents=True,exist_ok=True)
    def cancel():
        if callable(cancel_check) and cancel_check():raise BuildError('用户已放弃生成。')
    def status(message):
        cancel()
        if callable(progress):progress(message)
    stage=Path(tempfile.mkdtemp(prefix='.animeboot-build-',dir=str(output.parent)));payload_path=None
    try:
        status('正在检查内置组件并生成静图资源…')
        root=stage/'InstallPayload';root.mkdir();files=root/'files';files.mkdir()
        assets=render_canvases(project,default_theme_assets(),font_path=font_path,progress=status)
        animation_enabled=bool(project.get('boot_animation',{}).get('enabled'))
        assets['/'+UEFI_CONFIG]=generate_config(project,'uefi')
        state_required=animation_enabled or len(theme_paths(project))>1
        if state_required:assets['/'+ENV_PATH]=create_blank_grubenv()
        for virtual,data in assets.items():
            target=files/virtual.lstrip('/');target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
        fedora=resource_root()/'assets/fedora'
        mode='old' if project.get('x64_grub_compat_mode')=='old' else 'new'
        for src,dest in [('bootmgfw.efi','EFI/Microsoft/Boot/bootmgfw.efi'),
                         ('grubx64_compat.efi' if mode=='old' else 'grubx64.efi','EFI/Microsoft/Boot/grubx64.efi'),
                         ('mmx64.efi','EFI/Microsoft/Boot/mmx64.efi'),
                         ('bzImage','AnimeBootNeo/bzImage'),('unicode.pf2','AnimeBootNeo/unicode.pf2')]:
            if src=='bzImage' and not animation_enabled:continue
            target=files/dest;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(fedora/src,target)
        pairs=json.loads((resource_root()/'assets/grub/grub-pairs.json').read_text(encoding='utf-8'))['pairs'][mode]
        if pairs['bios']!=pairs['uefi']:raise BuildError('UEFI 与 Legacy GRUB 发布版本不一致')
        status('正在生成传统启动核心…');_build_bios(root,project)
        frames=stage/ANIMATION_FRAME_EXPORT_DIR
        status('正在读取动画素材…' if animation_enabled else '正在整理静图启动文件…')
        runtime=None;animation={'enabled':False};items=[]
        variants=animation_projects(project)
        paths=initramfs_paths(project)
        for index,variant in enumerate(variants):
            status('正在制作动画 %d/%d…'%(index+1,len(variants)))
            payload_path,item=render_linux_animation_payload_file(variant,None,
                frame_export_dir=str(frames/('%02d'%index)) if len(variants)>1 else str(frames),
                cancel_check=cancel_check,progress=progress)
            if not payload_path:raise BuildError('动画没有生成播放数据')
            destination=files/paths[index];destination.parent.mkdir(parents=True,exist_ok=True)
            status('正在生成动画 initramfs…')
            current=build_animation_initramfs(payload_path,destination,cancel_check,progress=progress)
            item.pop('payload_path',None);item['initramfs_path']=paths[index]
            item['runtime']=current;items.append(item)
            os.remove(payload_path);payload_path=None;cancel()
        if items:
            animation=copy.deepcopy(items[0]);runtime=items[0]['runtime']
            if len(items)>1:
                animation['rotation']='sequential';animation['items']=items
                animation['frame_count']=sum(item.get('frame_count',0) for item in items)
        status('正在校验普通启动文件…')
        report={'format':PAYLOAD_FORMAT,'app_version':APP_VERSION,'application':'AnimeBoot Neo Debug',
                'legacy_method':'bootmgr-file','locator':'binding-required','bound_boot_mode':'',
                'config_project':_boot_project(project),
                'project':{'show_debug_info':bool(project.get('show_debug_info',True)),'timeout':int(project['timeout']),
                           'countdown_mode':project.get('countdown_mode','none'),
                           'static_canvas_modes':countdown_modes(project),
                           'static_canvas_count':len(theme_paths(project)),
                           'static_handoff_strategy':static_handoff_strategy(project),
                           'static_windows_transition':project.get('static_windows_transition','blend')},
                'animation':animation,'runtime':runtime,'grub_mode':mode,'grub_versions':pairs,
                'env_path':ENV_PATH if state_required else '',
                'boot_ui':{'grub4dos_silent_menu':True,'grub4dos_a20_debug':False,
                           'bios_welcome_removed':True,'signed_efi_modified':False}}
        report=_write_manifest(root,report)
        size=sum(rec['size'] for rec in report['files'].values())
        deployed={platform:sum(rec['size'] for name,rec in report['files'].items()
            if name.startswith('files/') and
            (platform=='uefi' and name not in ('files/'+BIOS_CONFIG,'files/AnimeBootNeo/grub2.lnx','files/AnimeBootNeo/unicode.pf2')
             or platform=='bios' and name.startswith('files/AnimeBootNeo/')))
            for platform in ('uefi','bios')}
        deployed['bios']+=(root/'legacy.bootmgr').stat().st_size
        verify_payload(root);cancel()
        final=output/'InstallPayload';backup=output/'InstallPayload.previous'
        if backup.exists():raise BuildError('上次构建备份仍存在，请先检查 InstallPayload.previous。')
        if final.exists():os.replace(final,backup)
        try:os.replace(root,final)
        except Exception:
            if backup.exists():os.replace(backup,final)
            raise
        if backup.exists():shutil.rmtree(backup)
        if frames.exists():
            target=output/ANIMATION_FRAME_EXPORT_DIR
            if target.exists():shutil.rmtree(target)
            os.replace(frames,target)
        status('构建完成，准备安装…')
        return {'payload_dir':str(final),'asset_root':str(output),'actual_size':size,
                'actual_mib':size/(1024*1024),'sha256':digest(final/'manifest.json'),
                'deployed_bytes':deployed,'animation':animation,
                'animation_frames_dir':str(output/ANIMATION_FRAME_EXPORT_DIR),
                'animation_frames_count':animation.get('frame_count',0),'initramfs':runtime,
                'animation_initramfs':runtime,'efi_files':[],'architecture':'x64 UEFI / Legacy BIOS',
                'install_mode':'standalone','x64_grub_compat_mode':mode}
    finally:
        if payload_path and os.path.isfile(payload_path):os.remove(payload_path)
        shutil.rmtree(stage,ignore_errors=True)


# 从磁盘重新读取manifest并逐文件核对，不沿用构建时内存中的哈希；格式、平台文件组合及环境块都必须符合当前规范。
def verify_payload(root):
    root=Path(root).resolve();report=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    if report.get('format')!=PAYLOAD_FORMAT or report.get('app_version')!=APP_VERSION:
        raise BuildError('仅支持当前 2610.3 制作结果，请用旧版本禁用后重新制作。')
    if report.get('legacy_method')!='bootmgr-file' or not isinstance(report.get('config_project'),dict):
        raise BuildError('当前安装包配置无效。')
    mode=report.get('bound_boot_mode','')
    if mode not in ('','bios','uefi'):raise BuildError('安装包绑定模式无效。')
    if mode=='bios':validate_boot_binding(report.get('boot_binding'))
    for name,rec in report['files'].items():
        path=(root/name).resolve()
        if os.path.commonpath([str(root),str(path)])!=str(root) or not path.is_file():
            raise BuildError('安装包路径无效：'+name)
        if path.stat().st_size!=rec['size'] or digest(path)!=rec['sha256']:
            raise BuildError('安装包完整性校验失败：'+name)
    actual={path.relative_to(root).as_posix() for path in root.rglob('*') if path.is_file()}-{'manifest.json'}
    if actual!=set(report['files']):raise BuildError('安装包存在未登记文件')
    paths=theme_paths(report['config_project'])
    required={'files/'+path for path in paths}
    if len(paths)==1:required.add('files/AnimeBootNeo/theme/back.png')
    if mode in ('','bios'):
        required.update(('boot.img','legacy.bootmgr','core.img','files/AnimeBootNeo/grub2.lnx','files/'+BIOS_CONFIG,
                         'files/AnimeBootNeo/unicode.pf2'))
    if mode in ('','uefi'):
        required.update(('files/EFI/Microsoft/Boot/bootmgfw.efi','files/EFI/Microsoft/Boot/grubx64.efi',
                         'files/EFI/Microsoft/Boot/mmx64.efi','files/'+UEFI_CONFIG))
    animated=bool(report.get('animation',{}).get('enabled'))
    if animated or len(paths)>1:
        if report.get('env_path')!=ENV_PATH:raise BuildError('一次性状态路径无效。')
        required.add('files/'+ENV_PATH)
        from .grub_config import valid_grubenv
        if not valid_grubenv((root/'files'/ENV_PATH).read_bytes()):raise BuildError('轮换状态文件无效。')
    elif report.get('env_path') or 'files/'+ENV_PATH in actual:
        raise BuildError('单张静图制作结果不应包含轮换接力状态。')
    if animated:
        required.add('files/AnimeBootNeo/bzImage')
        paths=initramfs_paths(report['config_project'])
        required.update('files/'+path for path in paths)
        items=report['animation'].get('items',[])
        if len(paths)>1 and (len(items)!=len(paths) or [i.get('initramfs_path') for i in items]!=paths):
            raise BuildError('轮换动画清单与启动配置不一致')
    if not required<=actual:raise BuildError('安装包缺少必要启动文件')
    if any(name.startswith('files/EFI/Boot/') or name=='files/EFI/Microsoft/Boot/grub.cfg' for name in actual):
        raise BuildError('当前版本不管理 fallback EFI 或额外配置副本。')
    for rel in (BIOS_CONFIG,UEFI_CONFIG):
        if 'files/'+rel in actual:_verify_grub_script(root/'files'/rel)
    return report
