# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
"""Sequential playlist; selection lives entirely in the shared GRUB layer."""

# 动画列表按“主动画 + 追加素材”排列，最多8段，共用编码、分辨率、抽帧/播放参数。
# 制作时每段独立 initramfs；GRUB 只读取本次选中的那段，空间估算却必须累计所有段。Linux 不更新轮换状态。
# neo_rotation 是下一段的零基序号。只有真正准备启动 Linux 时才与 neo_once 一起保存；Esc 直接进 Windows 不推进。
# 估算签名包含制作参数、画布与资源修改信息，排除启用复选框本身；复选框变化不能导致重复编码估算。

import copy,os,json

MAX_ANIMATIONS=8

def animation_sources(project):
    spec=project.get('boot_animation',{})
    sources=[spec.get('source','')]
    if spec.get('rotation_enabled'):
        sources += [item.get('source','') for item in spec.get('playlist',[])]
    return sources if spec.get('enabled') else []

def animation_projects(project):
    result=[]
    for source in animation_sources(project):
        p=copy.deepcopy(project)
        p['boot_animation'].update(source=source,rotation_enabled=False,playlist=[])
        result.append(p)
    return result

def validate_rotation(project, check_resources=True):
    spec=project.get('boot_animation',{})
    if not isinstance(spec.get('rotation_enabled',False),bool):raise ValueError('轮换开关无效')
    items=spec.get('playlist',[])
    if not isinstance(items,list) or len(items)>=MAX_ANIMATIONS:raise ValueError('轮换最多8段动画（含主动画）')
    for item in items:
        if not isinstance(item,dict) or set(item)!= {'source'} or not isinstance(item['source'],str):
            raise ValueError('轮换列表格式无效')
    sources=animation_sources(project)
    if spec.get('enabled') and spec.get('rotation_enabled') and not items:raise ValueError('顺序轮换至少需要2段动画')
    if check_resources and any(not source or not os.path.exists(source) for source in sources):
        raise ValueError('主动画或轮换素材不存在，请重新选择素材或删除失效项')
    normalized=[os.path.normcase(os.path.realpath(source)) for source in sources]
    if len(normalized)!=len(set(normalized)):raise ValueError('轮换列表中有重复素材')

def initramfs_paths(project):
    count=len(animation_sources(project))
    if count<2:return ['AnimeBootNeo/anim_initramfs'] if count else []
    return ['AnimeBootNeo/animations/%02d.initramfs'%i for i in range(count)]

def rotation_script(project):
    paths=initramfs_paths(project)
    if len(paths)<2:return []
    # Explicit finite cases avoid arithmetic modules or unsigned dependencies.
    s=['set neo_selected=0','set neo_next=1']
    for i in range(1,len(paths)):
        s+=['if [ "$neo_rotation" = %d ]; then'%i,
            '  set neo_selected=%d'%i,'  set neo_next=%d'%((i+1)%len(paths)),'fi']
    s+=['set neo_initrd=($neo_root)/'+paths[0]]
    for i,path in enumerate(paths[1:],1):
        s+=['if [ "$neo_selected" = %d ]; then'%i,'  set neo_initrd=($neo_root)/'+path,'fi']
    return s

def estimate_rotation_sizes(*args,**kwargs):
    from .animation import estimate_animation_codec_sizes
    extra=kwargs.pop('rotation_sources',[])
    progress=kwargs.pop('rotation_progress',None)
    if callable(progress):progress('正在估算主动画（1/%d）…'%(1+len(extra)))
    first=estimate_animation_codec_sizes(*args,**kwargs)
    result=copy.deepcopy(first)
    source=args[0] if args else kwargs.get('source','')
    result['items']=[dict(first,source=source)]
    for index,source in enumerate(extra,1):
        if callable(progress):progress('正在估算轮换动画（%d/%d）…'%(index+1,1+len(extra)))
        if args:
            following=estimate_animation_codec_sizes(source,*args[1:],**kwargs)
        else:
            parameters=dict(kwargs,source=source)
            following=estimate_animation_codec_sizes(**parameters)
        result['items'].append(dict(following,source=source))
        for key in ('qoi_bytes','jpeg_bytes','jpeg_esra_bytes','frame_count','planned_frame_count','sample_count'):
            if key in result:result[key]+=following.get(key,0)
    result['rotation_count']=1+len(extra)
    return result


def estimate_parameters(project, all_entries=False):
    spec=project['boot_animation'];codec=spec.get('codec','jpeg')
    return dict(source=spec.get('source',''), width=int(project['resolution'][0]),
                height=int(project['resolution'][1]),fit_mode=spec.get('fit_mode','stretch'),
                maximum=int(spec.get('max_frames',600)),extraction_fps=float(spec.get('extract_frame_rate',30)),
                sampling_mode=spec.get('sampling_mode','head'),jpeg_quality=int(spec.get('jpeg_quality',90)),
                sample_limit=6,estimate_codecs=(codec,),
                rotation_sources=[item['source'] for item in spec.get('playlist',[])]
                if all_entries or spec.get('rotation_enabled') else [])


# 启用状态不是体积输入；路径内容时间戳、编码/尺寸/采样参数以及所有静图才是。制作仍有最终空间检查，缓存不是写盘通行证。
def estimate_signature(project):
    """Invalidates cached approval after edits or deleted/replaced source files."""
    # 不在签名计算里修改调用者；GUI、worker及缓存各持有独立数据。
    project=copy.deepcopy(project)
    from .scenes import sync_canvas
    sync_canvas(project)
    spec=project['boot_animation']
    sources=[spec.get('source','')]+[item['source'] for item in spec.get('playlist',[])]
    def scene_files(value):
        if isinstance(value,dict):
            for key,child in value.items():
                if key in ('path','font_path') and isinstance(child,str) and child:
                    yield child
                else:yield from scene_files(child)
        elif isinstance(value,list):
            for child in value:yield from scene_files(child)
    # 同一路径的静图/字体被外部替换也会改变主题体积，需让批准失效。
    sources+=sorted(set(scene_files(project['canvases'])))
    stamps=[]
    for source in sources:
        members=[]
        try:
            if os.path.isdir(source):
                for name in sorted(os.listdir(source)):
                    if name.lower().endswith('.png'):
                        st=os.stat(os.path.join(source,name));members.append((name,st.st_size,st.st_mtime_ns))
            else:
                st=os.stat(source);members=[(st.st_size,st.st_mtime_ns)]
        except OSError:members=['missing']
        stamps.append((source,members))
    return json.dumps([estimate_parameters(project,True),project['canvases'],stamps,
                       project.get('x64_grub_compat_mode','new')],sort_keys=True,ensure_ascii=True)


def estimated_boot_bytes(result, codec, project, theme_bytes, boot_mode):
    """Count selected frames, every initramfs runtime and shared boot resources."""
    from .resources import resource_root
    root=resource_root()/'assets'
    animation=int(result.get(codec+'_bytes',0))
    runtimes=int(result.get('rotation_count',1))*sum((root/'linux'/name).stat().st_size
        for name in ('animeboot_init_x86_64','animeboot_player_x86_64'))
    mode='grubx64_compat.efi' if project.get('x64_grub_compat_mode')=='old' else 'grubx64.efi'
    efi=sum((root/'fedora'/name).stat().st_size for name in ('bootmgfw.efi',mode,'mmx64.efi'))
    bios=(root/'grub/grldr').stat().st_size+(root/'fedora/unicode.pf2').stat().st_size+512*1024
    shared=(root/'fedora/bzImage').stat().st_size+int(theme_bytes)+runtimes
    # Archive headers/config/environment blocks plus a little allocation slack.
    return animation+shared+(bios if boot_mode=='bios' else efi)+64*1024*int(result.get('rotation_count',1))
