# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
"""Owned-file journal, tiny BIOS MBR transaction, and signed EFI handoff."""

# 安装/验证/禁用的事务层。先识别引导方式、加密状态、目标分区、Windows入口身份与空间，后写启动文件。
# state.json 是所有权与回滚日志：保存每个路径原本是否存在、原始/安装哈希、原始属性、固件启动项身份和执行阶段。
# 先备份并校验 Windows，先写启动依赖，最后提交入口；失败按记录恢复。回滚未完成必须保留备份，不能只删状态文件。
# 禁用先恢复并验证真实 Windows 入口及必要 MBR 信息，再清理本工具所有目录和明确记录的共享路径，不按名称删除其它系统启动项。
# UEFI 不接管 EFI/BOOT/bootx64.efi；BIOS 仅使用普通 bootmgr 文件桥与微型 MBR，不把大资源塞进分区前空隙。

import base64,json,os,re,shutil,struct,tempfile,time
from pathlib import Path
from . import windows as w,legacy_file
from .builder import APP_VERSION,bind_payload,verify_payload
from .resources import digest,validate_components,resource_root,host_windows_architecture
from .constants import MSFT_UEFI_CA_2011_SHA1,MSFT_UEFI_CA_2023_SHA1
from .grub_config import ENV_PATH,BIOS_CONFIG,UEFI_CONFIG,create_blank_grubenv,valid_grubenv
from .windows_efi import validate_windows_efi
from .boot_space import installed_space, remove_owned_tree, tree_files
DeploymentError=w.DeploymentError
DeploymentConsentRequired=w.DeploymentConsentRequired
prepare_elevated_action=w.prepare_elevated_action
launch_elevated_action=w.launch_elevated_action
read_action_result=w.read_action_result
BACKUP='AnimeBootNeo.Backup'

def _mount():
    mode=w._firmware_type()
    if mode=='uefi':info=w._mount_system_esp()
    elif mode=='bios':info=w._find_bios_system_volume()
    else:raise DeploymentError('无法可靠识别当前启动方式，未修改启动链。')
    info['boot_mode']=mode;return info

def _atomic(path,data,bios=False,restore_attributes=None):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);tmp=Path(str(path)+'.neo-new')
    previous_attributes=legacy_file.attributes(path) if path.exists() else None
    published=False
    try:
        with tmp.open('wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
        if bios:legacy_file.make_boot_readable(tmp)
        if tmp.read_bytes()!=data:raise DeploymentError('临时文件校验失败：'+str(tmp))
        # Set final attributes while this is still our writable staging file.
        # Do not publish BOOTMGR and then discover that its attributes fail.
        if restore_attributes is not None:legacy_file.set_attributes(tmp,restore_attributes)
        if previous_attributes is not None and previous_attributes&1:
            legacy_file.set_attributes(path,previous_attributes&~1)
        legacy_file.replace_file(tmp,path)
        published=True
        if path.read_bytes()!=data:raise DeploymentError('写入后校验失败：'+str(path))
    except Exception as error:
        if not published and previous_attributes is not None and path.exists():
            try:legacy_file.set_attributes(path,previous_attributes)
            except Exception as restore_error:
                raise DeploymentError('文件替换失败：%s；原文件属性恢复失败：%s'%(error,restore_error)) from error
        raise
    finally:
        if tmp.exists():
            legacy_file.set_attributes(tmp,legacy_file.attributes(tmp)&~1);tmp.unlink()

def _json(path,obj):_atomic(path,json.dumps(obj,ensure_ascii=False,indent=2).encode('utf-8'))

def _atomic_file(path,source,expected_sha256,bios=False,restore_attributes=None):
    """Stream large initramfs files without materialising them in 32-bit Python."""
    path=Path(path);source=Path(source);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=Path(str(path)+'.neo-new')
    previous_attributes=legacy_file.attributes(path) if path.exists() else None
    published=False
    try:
        with source.open('rb') as inp,tmp.open('wb') as out:
            shutil.copyfileobj(inp,out,1024*1024);out.flush();os.fsync(out.fileno())
        if bios:legacy_file.make_boot_readable(tmp)
        if digest(tmp)!=expected_sha256:raise DeploymentError('部署来源在复制期间发生变化：'+str(source))
        if restore_attributes is not None:legacy_file.set_attributes(tmp,restore_attributes)
        if previous_attributes is not None and previous_attributes&1:
            legacy_file.set_attributes(path,previous_attributes&~1)
        legacy_file.replace_file(tmp,path);published=True
        if digest(path)!=expected_sha256:raise DeploymentError('写入后校验失败：'+str(path))
    except Exception as error:
        if not published and previous_attributes is not None and path.exists():
            try:legacy_file.set_attributes(path,previous_attributes)
            except Exception as restore_error:
                raise DeploymentError('文件替换失败：%s；原文件属性恢复失败：%s'%(error,restore_error)) from error
        raise
    finally:
        if tmp.exists():
            legacy_file.set_attributes(tmp,legacy_file.attributes(tmp)&~1);tmp.unlink()
def _read_state(root):
    p=Path(root)/BACKUP/'state.json'
    if not p.is_file():raise DeploymentError('未找到本程序的安装/回滚记录。')
    state=json.loads(p.read_text(encoding='utf-8'))
    if state.get('format')!='AnimeBootNeo-installed-3-canvases' or state.get('app_version')!=APP_VERSION:
        raise DeploymentError('安装记录与当前版本格式不匹配；请勿覆盖未知安装。')
    if state.get('mode') not in ('bios','uefi') or state.get('legacy_method')!='bootmgr-file':
        raise DeploymentError('当前安装记录的启动架构无效。')
    if state.get('env_path') not in ('',ENV_PATH):raise DeploymentError('当前安装状态路径无效。')
    efi_paths={UEFI_CONFIG,'EFI/Microsoft/Boot/bootmgfw.efi','EFI/Microsoft/Boot/grubx64.efi',
               'EFI/Microsoft/Boot/mmx64.efi','EFI/Microsoft/Boot/bootmgfw.efi_ANBAK'}
    shared_paths={'bootmgr'} if state['mode']=='bios' else efi_paths
    for rel in state.get('files',{}):
        if rel not in shared_paths and not rel.startswith('AnimeBootNeo/'):
            raise DeploymentError('当前安装记录包含不受管理的文件路径：'+rel)
        _safe(Path(root),rel)
    return state

def _safe(root,rel):
    root=Path(root).resolve();p=(root/rel).resolve()
    if os.path.commonpath([str(root),str(p)])!=str(root):raise DeploymentError('安装记录路径越界')
    return p

def probe_boot_partition_space():
    mounted=_mount()
    try:
        usage=shutil.disk_usage(mounted['root'])
        root=Path(mounted['root'])
        state=_read_state(root) if (root/BACKUP/'state.json').is_file() else None
        space=installed_space(root,state)
        space.update(ok=True,boot_root=mounted['root'],root=mounted['root'],boot_mode=mounted['boot_mode'],
                     free_bytes=usage.free,total_bytes=usage.total,free_mib=usage.free/(1024*1024),
                     replaceable_bytes=usage.free+space['reclaimable_bytes'],source=mounted.get('source',''))
        return space
    finally:w._unmount_root(mounted)

def preflight_install_boot_item_standalone(manifest):
    verify_payload(manifest.get('payload_dir',''));validate_components()
    mode=w._firmware_type()
    if mode not in ('uefi','bios'):raise DeploymentError('无法识别 UEFI / Legacy 启动方式。')
    if mode=='uefi' and host_windows_architecture()!='x64':raise DeploymentError('Fedora 签名链要求 x64 UEFI。32 位 UEFI 不支持此签名链。')
    probe=w._probe_bootcurrent_boot_path('x64',allow_bcd_fallback=False) if mode=='uefi' else {}
    return {'ok':True,'action':'install_preflight','boot_mode':mode,'architecture':'x64','consent_needed':bool(probe.get('needs_repair')),'bootcurrent_probe':probe,'existing_repair_guid':'','existing_repair_boot_number':None,'consent_title':'UEFI 当前默认路径可能绕过动画','consent_message':'当前默认启动路径没有指向 Microsoft\\Boot\\bootmgfw.efi。是否创建一个指向本次启动入口的 Windows Boot Manager 并放到第一位？选择保留原顺序后，动画可能被绕过。','diagnostic':json.dumps(probe,ensure_ascii=False,indent=2)}

def _trust_check(manifest):
    if not w._secure_boot_enabled():return {'secure_boot':False}
    trusted=w._firmware_db_x509_thumbprints()
    if MSFT_UEFI_CA_2011_SHA1 not in trusted and MSFT_UEFI_CA_2023_SHA1 not in trusted:
        if not manifest.get('allow_unverified_secure_boot_shim'):
            raise DeploymentConsentRequired('secure_boot_shim','无法确认固件信任 Fedora shim','未在固件 db 中确认 Microsoft 第三方 UEFI CA。此签名链可能被固件拒绝；继续前请确认固件支持第三方签名。')
    return {'secure_boot':True,'db_thumbprints':sorted(trusted)}

def filesystem_boot_binding(boot_sector):
    """Convert the target BPB serial to the exact UUID used by both GRUBs."""
    bs=bytes(boot_sector)
    if len(bs)<512 or bs[510:512]!=b'\x55\xaa':raise DeploymentError('启动卷 BPB 无效。')
    if bs[3:11]==b'NTFS    ':
        return {'filesystem':'NTFS','fs_uuid':'%016X'%struct.unpack_from('<Q',bs,72)[0]}
    if bs[82:90]==b'FAT32   ':
        value=struct.unpack_from('<I',bs,67)[0];filesystem='FAT32'
    elif bs[54:62]==b'FAT16   ':
        value=struct.unpack_from('<I',bs,39)[0];filesystem='FAT16'
    else:raise DeploymentError('启动卷文件系统不受当前版本支持。')
    return {'filesystem':filesystem,'fs_uuid':'%04X-%04X'%(value>>16,value&65535)}

def _prepare_legacy_file(root):
    current=Path(root)/'bootmgr'
    if not current.is_file() or not (Path(root)/'Boot/BCD').is_file():
        raise DeploymentError('启动分区缺少 Windows bootmgr 或 Boot/BCD，请恢复 Windows 原启动入口后重试。')
    original=current.read_bytes();legacy_file.validate_windows_bootmgr(original)
    bs=w._raw_volume_boot_sector(str(root))[:512]
    if len(bs)!=512 or bs[510:512]!=b'\x55\xaa' or struct.unpack_from('<H',bs,11)[0]!=512:
        raise DeploymentError('Legacy 文件引导要求有效的 512 字节 Windows 启动扇区。')
    if bs[3:11]!=b'NTFS    ' and bs[54:62]!=b'FAT16   ' and bs[82:90]!=b'FAT32   ':
        raise DeploymentError('Legacy 文件引导仅支持 Windows NTFS/FAT 启动分区。')
    # Windows NTFS boot code looks up BOOTMGR. Other VBRs need Windows repair.
    if bs[3:11]==b'NTFS    ':
        code=w._read_volume_boot_bytes(str(root),16*512)
        if 'BOOTMGR'.encode('utf-16le') not in code and b'BOOTMGR' not in code:
            raise DeploymentError('活动分区启动扇区未指向 Windows BOOTMGR。请先用 Windows 修复环境恢复该分区的 Windows 启动代码；本程序不会覆盖未知分区引导代码。')
    extent=w._volume_disk_extent(str(root))
    if not extent:raise DeploymentError('无法确认 Windows 启动卷对应的物理磁盘。')
    disk=w._physical_disk_for_volume(str(root));mbr=w._read_physical_bytes(disk,0,512)
    legacy_file.partition_layout(mbr,int(extent['starting_offset']))
    return original,mbr,{'method':'bootmgr-file','pre_partition_sectors_written':0,
                     'mbr_sha256':__import__('hashlib').sha256(mbr).hexdigest(),
                     'boot_sector_sha256':__import__('hashlib').sha256(bs).hexdigest(),
                     'starting_offset':int(extent['starting_offset'])}

def _rollback_files(root,state,remove_owned_resources=True):
    for rel,rec in reversed(list(state['files'].items())):
        if not remove_owned_resources and rel.startswith('AnimeBootNeo/'):
            if rec['existed']:raise DeploymentError('专属资源目录包含不属于本次安装的原有文件记录。')
            continue
        target=_safe(root,rel)
        if rec['existed']:
            source=_safe(Path(root)/BACKUP/'files',rel)
            if not source.is_file() or digest(source)!=rec['original_sha256']:raise DeploymentError('回滚备份校验失败：'+rel)
            if target.is_file() and digest(target)==rec['original_sha256']:
                if 'original_attributes' in rec:legacy_file.set_attributes(target,rec['original_attributes'])
                continue
            if target.exists() and (not target.is_file() or digest(target)!=rec['installed_sha256']):raise DeploymentError('回滚目标被其他程序修改，已保留备份：'+rel)
            _atomic(target,source.read_bytes(),bios=state.get('legacy_method')=='bootmgr-file',restore_attributes=rec.get('original_attributes'))
            if digest(target)!=rec['original_sha256']:raise DeploymentError('回滚回读失败：'+rel)
        elif target.exists():
            if rel==state.get('env_path'):
                if not valid_grubenv(target.read_bytes()):raise DeploymentError('回滚状态文件损坏，已保留备份。')
            elif not target.is_file() or digest(target)!=rec['installed_sha256']:raise DeploymentError('回滚目标被其他程序修改，已保留备份：'+rel)
            legacy_file.set_attributes(target,legacy_file.attributes(target)&~1);target.unlink()

def _verify(root,state,verify_files=True):
    if state.get('phase')!='complete':raise DeploymentError('上次安装未完成，请先使用禁用功能恢复 Windows。')
    for rel,rec in state['files'].items():
        if verify_files:
            p=_safe(root,rel)
            if not p.is_file():raise DeploymentError('缺少已安装文件：'+rel)
            if rel==state.get('env_path'):
                data=p.read_bytes()
                if not valid_grubenv(data):raise DeploymentError('一次性启动状态文件损坏')
            elif digest(p)!=rec['installed_sha256']:raise DeploymentError('已安装文件被修改：'+rel)
            if state.get('legacy_method')=='bootmgr-file':legacy_file.verify_boot_readable(p)
        if rec['existed']:
            p=_safe(Path(root)/BACKUP/'files',rel)
            if not p.is_file() or digest(p)!=rec['original_sha256']:raise DeploymentError('原始启动文件备份损坏：'+rel)
    if state['mode']=='bios' and state.get('legacy_method')=='bootmgr-file':
        original,layout=_prepare_legacy_file_identity(root)
        expected=state['legacy_layout']
        installed=(Path(root)/BACKUP/'installed-mbr.bin').read_bytes()
        saved=(Path(root)/BACKUP/'original-mbr.bin').read_bytes()
        if digest(Path(root)/BACKUP/'original-mbr.bin')!=state['original_mbr_sha256'] or digest(Path(root)/BACKUP/'installed-mbr.bin')!=expected['mbr_sha256']:raise DeploymentError('MBR 备份校验失败')
        if saved[440:]!=installed[440:]:raise DeploymentError('MBR 备份身份不一致')
        if any(layout[key]!=expected[key] for key in ('mbr_sha256','boot_sector_sha256','starting_offset')):
            raise DeploymentError('Windows MBR/启动扇区或卷映射发生变化，请检查原启动链。')
    return True

def _prepare_legacy_file_identity(root):
    bs=w._raw_volume_boot_sector(str(root))[:512];extent=w._volume_disk_extent(str(root))
    if not extent:raise DeploymentError('无法确认启动卷映射')
    disk=w._physical_disk_for_volume(str(root));mbr=w._read_physical_bytes(disk,0,512)
    return mbr,{'mbr_sha256':__import__('hashlib').sha256(mbr).hexdigest(),
                'boot_sector_sha256':__import__('hashlib').sha256(bs).hexdigest(),
                'starting_offset':int(extent['starting_offset'])}

def install_boot_item_standalone(manifest):
    payload=Path(manifest.get('payload_dir','')).resolve();report=verify_payload(payload);validate_components()
    encryption=w.check_system_volume_encryption()
    mode=w._firmware_type();choice=manifest.get('fallback_bootcurrent_repair_choice','keep')
    if mode=='uefi':
        if not manifest.get('uefi_install_preflight_complete') or choice not in ('keep','repair'):raise DeploymentError('缺少已完成的 UEFI 只读预检/路径选择')
        trust=_trust_check(manifest)
        probe=w._probe_bootcurrent_boot_path('x64',allow_bcd_fallback=False)
        if manifest.get('fallback_bootcurrent_repair_choice_origin')=='auto-normal' and probe.get('needs_repair'):raise DeploymentError('启动路径在预检后发生变化，请重新安装')
    else:trust={};probe={}
    mounted=_mount();root=Path(mounted['root']);backup=root/BACKUP
    preparation=tempfile.TemporaryDirectory(prefix='animeboot-install-')
    state=None;disk=None;original=None;committed=False;replaced=False;repair={};auxiliary={}
    try:
        if backup.exists():
            previous=_read_state(root);_uninstall(root,previous);replaced=True
        if (root/'AnimeBootNeo').exists():raise DeploymentError('AnimeBootNeo 目录没有有效所有权记录，拒绝覆盖。')
        if mode=='uefi' and (root/'EFI/fedora/grub.cfg').exists():raise DeploymentError('检测到已有 Fedora 配置，拒绝覆盖其他 Linux 启动项。')
        binding=filesystem_boot_binding(w._raw_volume_boot_sector(str(root))) if mode=='bios' else None
        prepared=Path(preparation.name)/'InstallPayload'
        report=bind_payload(payload,prepared,mode,binding)
        payload=prepared
        sources={p.relative_to(payload/'files').as_posix():p for p in (payload/'files').rglob('*') if p.is_file()}
        if mode=='bios':
            if report.get('legacy_method')!='bootmgr-file':raise DeploymentError('请重新制作安装文件，以使用不占用分区前空白的 Legacy 文件引导。')
            sources={r:p for r,p in sources.items() if r.startswith('AnimeBootNeo/')}
            original_windows,original,layout=_prepare_legacy_file(root)
            legacy_file.check_readonly_access(root/'bootmgr')
            disk=w._physical_disk_for_volume(str(root))
            template=(payload/'boot.img').read_bytes()
            if len(template)!=512 or template[510:]!=b'\x55\xaa':raise DeploymentError('微型 MBR 模板损坏')
            installed_mbr=template[:440]+original[440:]
            sources['AnimeBootNeo/windows.bootmgr']=original_windows
            # Publish the entry last: every boot dependency and backup is ready.
            sources['bootmgr']=payload/'legacy.bootmgr'
        elif mode!='uefi':raise DeploymentError('启动方式不受支持')
        else:
            current=root/'EFI/Microsoft/Boot/bootmgfw.efi'
            original_windows=current.read_bytes()
            try:windows_identity=validate_windows_efi(current,original_windows)
            except (ValueError,OSError) as error:raise DeploymentError(str(error)) from error
            # Primary Windows entry is the last file committed below.
            sources.pop('EFI/Microsoft/Boot/bootmgfw.efi')
            sources['EFI/Microsoft/Boot/bootmgfw.efi_ANBAK']=original_windows
            sources['EFI/Microsoft/Boot/bootmgfw.efi']=payload/'files/EFI/Microsoft/Boot/bootmgfw.efi'
        needed=sum(len(p) if isinstance(p,bytes) else p.stat().st_size for p in sources.values())+8*1024*1024
        needed+=sum((_safe(root,r).stat().st_size if _safe(root,r).is_file() else 0) for r in sources)
        if shutil.disk_usage(root).free<needed:raise DeploymentError('启动分区空间不足：文件、原入口备份及回滚空间需要约 %.1f MiB'%(needed/(1024*1024)))
        state={'format':'AnimeBootNeo-installed-3-canvases','app_version':APP_VERSION,'phase':'prepared',
               'mode':mode,'legacy_method':'bootmgr-file','debug':report['project']['show_debug_info'],
               'files':{},'boot_numbers':[],'boot_entry_sha256':{},'repair_number':None,'repair_sha256':'',
               'boot_binding':binding,'env_path':report['env_path']}
        if mode=='uefi':state['windows_efi_identity']=windows_identity
        backup.mkdir()
        if mode=='bios':
            state['legacy_method']='bootmgr-file';state['legacy_layout']=dict(layout)
            _atomic(backup/'original-mbr.bin',original);_atomic(backup/'installed-mbr.bin',installed_mbr)
            state['original_mbr_sha256']=digest(backup/'original-mbr.bin')
            state['legacy_layout']['mbr_sha256']=digest(backup/'installed-mbr.bin')
        for rel,source in sources.items():
            target=_safe(root,rel);existed=target.is_file()
            if target.exists() and not existed:raise DeploymentError('目标不是普通文件：'+rel)
            rec={'existed':existed,'original_sha256':digest(target) if existed else ''}
            if existed:
                rec['original_attributes']=legacy_file.attributes(target)
                dest=backup/'files'/rel;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(str(target),str(dest))
                if digest(dest)!=rec['original_sha256']:raise DeploymentError('保存原始文件备份失败：'+rel)
            rec['installed_sha256']=__import__('hashlib').sha256(source).hexdigest() if isinstance(source,bytes) else digest(source)
            state['files'][rel]=rec
        _json(backup/'state.json',state)
        for rel,source in sources.items():
            target=_safe(root,rel)
            if rel=='bootmgr':
                if digest(target)!=state['files'][rel]['original_sha256']:raise DeploymentError('提交前 Windows bootmgr 发生变化，未覆盖。')
                _,identity=_prepare_legacy_file_identity(root)
                if any(identity[key]!=layout[key] for key in identity):raise DeploymentError('提交前 Windows 启动链发生变化，未覆盖 bootmgr。')
            if rel=='EFI/Microsoft/Boot/bootmgfw.efi' and digest(target)!=state['files'][rel]['original_sha256']:
                raise DeploymentError('提交前 Windows EFI 启动入口发生变化，未覆盖。')
            if isinstance(source,bytes):
                if __import__('hashlib').sha256(source).hexdigest()!=state['files'][rel]['installed_sha256']:
                    raise DeploymentError('部署来源在读取期间发生变化：'+rel)
                _atomic(target,source,bios=mode=='bios',restore_attributes=state['files'][rel].get('original_attributes'))
            else:
                _atomic_file(target,source,state['files'][rel]['installed_sha256'],bios=mode=='bios',
                             restore_attributes=state['files'][rel].get('original_attributes'))
            if digest(target)!=state['files'][rel]['installed_sha256']:raise DeploymentError('部署回读校验失败：'+rel)
        if mode=='bios':
            legacy_file.commit_mbr(lambda o,n:w._read_physical_bytes(disk,o,n),lambda o,b:w._write_physical_sector(disk,o,b),original,installed_mbr)
            committed=True;state['legacy_layout']['mbr_sha256']=digest(backup/'installed-mbr.bin')
        if mode=='uefi':
            before=w._snapshot_firmware_boot_entries()
            auxiliary=w._create_animebootneo_firmware_entry_details('x64',root=str(root))
            state['boot_numbers']=auxiliary.get('new_boot_numbers',[]) if not auxiliary.get('skipped') or auxiliary.get('cleanup_errors') else []
            if state['boot_numbers']:
                state['boot_entry_sha256']={str(n):auxiliary['boot_sha256'] for n in state['boot_numbers']}
            _json(backup/'state.json',state)
            if auxiliary.get('cleanup_errors'):
                raise DeploymentError('UEFI 辅助启动项回滚未能确认：'+'; '.join(auxiliary['cleanup_errors']))
            if choice=='repair':
                repair=w._create_repair_windows_boot_manager(str(root),(payload/'files/EFI/Microsoft/Boot/bootmgfw.efi').read_bytes())
                if repair.get('cleanup_errors') and repair.get('boot_number') is not None:
                    state['repair_number']=repair['boot_number'];state['repair_sha256']=repair['boot_sha256']
                    _json(backup/'state.json',state)
                if repair.get('ok'):
                    state['repair_number']=repair['boot_number'];state['repair_sha256']=repair['boot_sha256']
                elif not repair.get('manual_required') or repair.get('cleanup_errors'):
                    raise DeploymentError('UEFI 路径修复未能安全完成：'+str(repair.get('error','未知错误'))+
                                          ('；修复回滚异常：'+'; '.join(repair['cleanup_errors']) if repair.get('cleanup_errors') else ''))
            _json(backup/'state.json',state)
            allowed=list(state['boot_numbers'])+([state['repair_number']] if state['repair_number'] is not None else [])
            w._cleanup_unexpected_new_firmware_boot_options(before,allowed_new_numbers=allowed,phase='neo-install')
        state['phase']='complete';_json(backup/'state.json',state);_verify(root,state)
        return {'ok':True,'action':'install_full','boot_mode':mode,'boot_root':str(root),'bitlocker':encryption,'replaced_existing':replaced,'bootcurrent_probe':probe,'bootcurrent_repair_requested':choice=='repair','bootcurrent_repair_ok':state['repair_number'] is not None,'bootcurrent_repair_manual_required':bool(repair.get('manual_required')),'bootcurrent_repair_error':str(repair.get('error','')),'bcd_auxiliary_skipped':bool(auxiliary.get('skipped')),'auxiliary_available':bool(state['boot_numbers']),'shim_selection':trust,'legacy_layout':state.get('legacy_layout',{})}
    except Exception as error:
        if '自动回滚未能完全恢复' in str(error):
            raise
        if state is not None and backup.exists():
            try:_uninstall(root,state)
            except Exception as exc:raise DeploymentError('自动回滚未能完全恢复：%s；原错误：%s'%(exc,error)) from error
        raise
    finally:
        preparation.cleanup()
        w._unmount_root(mounted)

def _remove_empty(path):
    if not path.is_dir():return
    for folder in sorted((p for p in path.rglob('*') if p.is_dir()),key=lambda p:len(p.parts),reverse=True):
        if not any(folder.iterdir()):folder.rmdir()
    if not any(path.iterdir()):path.rmdir()

def _verify_windows_restored(root,state):
    """Verify restored shared files without requiring backups already reclaimed."""
    for relative,record in state['files'].items():
        if relative.startswith('AnimeBootNeo/'):
            continue
        path=_safe(root,relative)
        if record['existed']:
            if not path.is_file() or digest(path)!=record['original_sha256']:
                raise DeploymentError('Windows 原启动文件恢复后校验失败：'+relative)
        elif path.exists():
            raise DeploymentError('程序新增的共享启动文件尚未移除：'+relative)
    if state['mode']=='bios':
        disk=w._physical_disk_for_volume(str(root))
        current=w._read_physical_bytes(disk,0,512)
        if __import__('hashlib').sha256(current).hexdigest()!=state['original_mbr_sha256']:
            raise DeploymentError('原 MBR 恢复后校验失败，清理记录已保留。')

# 恢复Windows是删除素材的前提。任何恢复/验证失败均保留日志与备份以便恢复，不能在finally无条件删除整个备份目录。
def _uninstall(root,state):
    # Check every owned path before changing the entry. Extra files under these
    # dedicated roots are ours across revisions, regardless of inner layout.
    if state.get('phase')=='complete':installed_space(root,state)
    if state.get('phase')=='complete':_verify(root,state,verify_files=False)
    # Validate both complete dedicated trees before mutating any boot entry.
    tree_files(root,'AnimeBootNeo');tree_files(root,BACKUP)
    root=Path(root);backup=root/BACKUP;journal=backup/'state.json'
    progress=state.setdefault('uninstall_progress',{})
    state['phase']='uninstalling';_json(journal,state)
    if not progress.get('windows_restored'):
        if state['mode']=='bios' and state.get('legacy_method')=='bootmgr-file':
            disk=w._physical_disk_for_volume(str(root))
            original=(backup/'original-mbr.bin').read_bytes();installed=(backup/'installed-mbr.bin').read_bytes()
            if digest(backup/'original-mbr.bin')!=state['original_mbr_sha256'] or digest(backup/'installed-mbr.bin')!=state['legacy_layout']['mbr_sha256']:raise DeploymentError('MBR 恢复备份损坏')
            legacy_file.restore_mbr(lambda o,n:w._read_physical_bytes(disk,o,n),lambda o,b:w._write_physical_sector(disk,o,b),original,installed)
        _rollback_files(root,state,remove_owned_resources=False)
        _verify_windows_restored(root,state)
        progress['windows_restored']=True;_json(journal,state)
    else:
        _verify_windows_restored(root,state)
    cleanup=progress.get('repair_cleanup',{})
    if not progress.get('firmware_removed'):
        if state.get('repair_number') is not None:
            cleanup=w._remove_managed_native_boot_entry(state['repair_number'],state['repair_sha256'])
        for number in state.get('boot_numbers',[]):
            w._remove_managed_native_boot_entry(number,state['boot_entry_sha256'][str(number)])
        progress.update(firmware_removed=True,repair_cleanup=cleanup);_json(journal,state)
    clear=lambda path:legacy_file.set_attributes(path,legacy_file.attributes(path)&~1)
    remove_owned_tree(root,'AnimeBootNeo',clear)
    # The checkpoint survives partial directory cleanup; retries no longer need
    # the original backups once Windows restoration has been verified.
    remove_owned_tree(root,BACKUP,clear,preserve_files=('state.json',))
    _verify_windows_restored(root,state)
    if (root/'AnimeBootNeo').exists():raise DeploymentError('启动资源目录仍存在，禁用未完成。')
    fedora=root/'EFI/fedora'
    if fedora.is_dir() and not any(fedora.iterdir()):clear(fedora);fedora.rmdir()
    try:
        clear(journal);journal.unlink();clear(backup);backup.rmdir()
        if backup.exists():raise DeploymentError('恢复目录仍存在，禁用未完成。')
    except Exception:
        _json(journal,state)
        raise
    return {'windows_restored':True,'owned_directories_removed':['AnimeBootNeo',BACKUP],
            'repair_windows_boot_cleanup':cleanup}

def uninstall_boot_item_official_equivalent(manifest=None):
    mounted=_mount()
    try:
        root=Path(mounted['root'])
        if not (root/BACKUP).exists() and not (root/'AnimeBootNeo').exists():
            if mounted['boot_mode']=='uefi':validate_windows_efi(root/'EFI/Microsoft/Boot/bootmgfw.efi')
            else:legacy_file.validate_windows_bootmgr((root/'bootmgr').read_bytes())
            return {'ok':True,'action':'uninstall_boot_item','boot_mode':mounted['boot_mode'],'already_disabled':True}
        state=_read_state(root);cleanup=_uninstall(root,state)
        return {'ok':True,'action':'uninstall_boot_item','boot_mode':mounted['boot_mode'],**cleanup}
    finally:w._unmount_root(mounted)

def apply_final_efi_assets(manifest):
    mounted=_mount()
    try:
        state=_read_state(mounted['root']);_verify(mounted['root'],state)
        return {'ok':True,'action':'final','boot_mode':mounted['boot_mode'],'boot_root':mounted['root']}
    finally:w._unmount_root(mounted)

def set_installed_runtime_fps(manifest):
    fps=int(manifest.get('fps',0))
    if not 1<=fps<=1000:raise DeploymentError('FPS 必须为 1–1000')
    mounted=_mount()
    try:
        root=Path(mounted['root']);state=_read_state(root);_verify(root,state)
        config_path=UEFI_CONFIG if state['mode']=='uefi' else BIOS_CONFIG
        path=root/config_path;text=path.read_text(encoding='utf-8')
        if 'AnimeBootNeoMode=WindowsAnimation' not in text:raise DeploymentError('当前安装未启用动画')
        old=sorted(set(int(s) for s in re.findall(r'AnimeBootNeoPlayerFPS=(\d+)',text)))
        text,n=re.subn(r'AnimeBootNeoPlayerFPS=\d+','AnimeBootNeoPlayerFPS=%d'%fps,text)
        if not n:raise DeploymentError('未找到可修改的播放帧率')
        original=path.read_bytes();record=dict(state['files'][config_path])
        try:
            _atomic(path,text.encode('utf-8'),bios=state['mode']=='bios');state['files'][config_path]['installed_sha256']=digest(path);_json(root/BACKUP/'state.json',state)
        except Exception:
            _atomic(path,original);state['files'][config_path]=record;_json(root/BACKUP/'state.json',state);raise
        return {'ok':True,'action':'set_runtime_fps','fps':fps,'previous_fps':old,'cfg_path':str(path)}
    finally:w._unmount_root(mounted)

def cleanup_action_temp(max_age_seconds=86400):
    """Remove abandoned result files created by a cancelled elevated action."""
    import tempfile
    now = time.time()
    root = Path(tempfile.gettempdir())
    for path in root.glob("animebootneo_*.anbr"):
        try:
            if now - path.stat().st_mtime >= int(max_age_seconds):
                path.unlink()
        except OSError:
            pass

def run_payload_action(action,payload_token,result_path=''):
    operation_id=''
    try:
        manifest=w._decode_action_payload(payload_token);operation_id=manifest.get('operation_id','')
        handlers={'install':install_boot_item_standalone,'install_full':install_boot_item_standalone,'install_preflight':preflight_install_boot_item_standalone,'final':apply_final_efi_assets,'uninstall_boot_item':uninstall_boot_item_official_equivalent,'set_runtime_fps':set_installed_runtime_fps,'probe_boot_space':lambda m:probe_boot_partition_space()}
        result=handlers[action](manifest);code=0
    except DeploymentConsentRequired as exc:
        result={'ok':False,'consent_required':True,'consent_code':exc.code,'consent_title':exc.title,'consent_message':exc.message,'diagnostic':exc.diagnostic};code=2
    except Exception as exc:result={'ok':False,'error':str(exc)};code=1
    result['operation_id']=operation_id;result['action']=action
    if result_path:w._write_binary_result(result_path,result)
    return code,result
