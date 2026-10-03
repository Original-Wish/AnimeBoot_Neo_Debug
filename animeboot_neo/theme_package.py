# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
"""Portable editable theme bundles. Data only; never boot binaries or scripts."""

# 分享包是 ZIP 数据容器，携带工程及外部素材；普通工程只引用本机素材路径。
# 导出遍历全部画布、字体、主动画、追加动画，把引用映射到包内资源；导入后再映射为本地解包路径。
# 所有成员先检查路径、重复项、数量/解压大小和 SHA256，再在暂存目录解包、验证，最后发布到目标目录。
# 不从分享包执行 Python、GRUB 配置或任意命令。运行时启动序号不随分享包传递，安装总是从序列第一项开始。

import copy,hashlib,json,os,re,shutil,stat,tempfile,zipfile
from pathlib import Path
from .project import validate_project
from .scenes import sync_canvas

FORMAT='AnimeBootNeo-theme-3-canvases'
MAX_FILES=10000
MAX_BYTES=4*1024**3
MAX_METADATA=4*1024**2

class ThemePackageError(ValueError):pass

def _hash(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()

def _slots(project):
    sync_canvas(project)
    for scene in project.get('canvases',[]):
        for item,key in _canvas_slots(scene['canvas']):yield item,key
    for item,key in _canvas_slots(project):yield item,key
    spec=project.get('boot_animation',{})
    yield spec,'source'
    for item in spec.get('playlist',[]):yield item,'source'


def _canvas_slots(project):
    for key in ('logo','company','prompt'):
        item=project.get(key,{})
        yield item,'font_path' if key=='prompt' else 'path'
    for item in project.get('extra_images',[]):
        if not item.get('source_builtin'):yield item,'path'
    for item in project.get('text_boxes',[]):yield item,'font_path'

def export_theme(project,destination,include_animation=True):
    p=copy.deepcopy(project)
    if not include_animation:
        p['boot_animation'].update(enabled=False,source='',rotation_enabled=False,playlist=[])
    validate_project(p)
    paths={};files={};sources={};references={}
    for item,key in _slots(p):
        raw=item.get(key,'')
        if not raw:continue
        source=Path(raw).resolve()
        if not source.exists() or source.is_symlink():raise ThemePackageError('素材不存在或是链接：'+str(source))
        marker=str(source)
        if marker not in paths:
            ident='r%04d'%len(paths);prefix='media/'+ident
            if source.is_dir():
                from .animation import _natural_key
                members=sorted(source.iterdir(),key=lambda x:_natural_key(str(x)))
                if not members:raise ThemePackageError('PNG序列为空')
                for n,member in enumerate(members):
                    if not member.is_file() or member.is_symlink() or member.suffix.lower()!='.png':
                        raise ThemePackageError('序列目录只允许普通 PNG 文件：'+str(member))
                    name=prefix+'/%06d.png'%n
                    sources[name]=member
                references[ident]={'path':prefix,'kind':'directory'}
            else:
                suffix=source.suffix.lower()
                if suffix not in ('.png','.jpg','.jpeg','.gif','.webp','.apng','.mp4','.mkv','.webm','.avi','.mov','.m4v','.mpg','.mpeg','.ttf','.otf','.ttc'):
                    raise ThemePackageError('主题包不支持该素材类型：'+suffix)
                name=prefix+suffix;sources[name]=source
                references[ident]={'path':name,'kind':'file'}
            paths[marker]=ident
        item[key]='bundle:'+paths[marker]
    total=sum(p.stat().st_size for p in sources.values())
    if len(sources)>MAX_FILES or total>MAX_BYTES:raise ThemePackageError('主题包超过文件数或4GiB限制')
    for name,path in sources.items():files[name]={'size':path.stat().st_size,'sha256':_hash(path)}
    metadata={'format':FORMAT,'project':p,'references':references,'files':files}
    raw=json.dumps(metadata,ensure_ascii=False).encode('utf-8')
    if len(raw)>MAX_METADATA:raise ThemePackageError('主题元数据太大')
    dest=Path(destination).absolute();dest.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.theme-',dir=str(dest.parent));os.close(fd)
    try:
        with zipfile.ZipFile(tmp,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=1) as z:
            z.writestr('theme.json',raw)
            for name,path in sources.items():
                z.write(path,name)
                if _hash(path)!=files[name]['sha256']:raise ThemePackageError('导出过程中素材发生变化')
        os.replace(tmp,dest)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    return {'files':len(files),'bytes':total,'animation_included':bool(include_animation)}

def import_theme(package,destination):
    dest=Path(destination).absolute()
    if dest.exists():raise ThemePackageError('导入目标必须是全新文件夹')
    dest.parent.mkdir(parents=True,exist_ok=True)
    scratch=Path(tempfile.mkdtemp(prefix='.theme-import-',dir=str(dest.parent)))
    try:
        with zipfile.ZipFile(package) as z:
            infos=z.infolist();names=[i.filename for i in infos]
            if len(names)!=len(set(names)) or len(infos)>MAX_FILES+1:raise ThemePackageError('重复成员或文件过多')
            if 'theme.json' not in names:raise ThemePackageError('缺少主题清单')
            info=z.getinfo('theme.json')
            if info.file_size>MAX_METADATA:raise ThemePackageError('元数据过大')
            meta=json.loads(z.read(info).decode('utf-8'))
            if meta.get('format')!=FORMAT:raise ThemePackageError('不是当前版本分享包')
            files=meta['files'];refs=meta['references'];p=meta['project']
            if set(names)!=set(files)|{'theme.json'}:raise ThemePackageError('存在未登记成员')
            total=0
            for i in infos:
                if i.filename=='theme.json':continue
                if not re.fullmatch(r'media/r[0-9]{4}(?:\.[a-z0-9]+|/[0-9]{6}\.png)',i.filename):
                    raise ThemePackageError('非法素材路径')
                if Path(i.filename).suffix.lower() not in ('.png','.jpg','.jpeg','.gif','.webp','.apng','.mp4','.mkv','.webm','.avi','.mov','.m4v','.mpg','.mpeg','.ttf','.otf','.ttc'):
                    raise ThemePackageError('不允许的素材类型')
                if stat.S_ISLNK(i.external_attr>>16) or i.is_dir():raise ThemePackageError('禁止链接或目录成员')
                rec=files[i.filename];total+=i.file_size
                if total>MAX_BYTES or i.file_size!=rec['size']:raise ThemePackageError('大小校验失败')
                target=scratch/i.filename;target.parent.mkdir(parents=True,exist_ok=True)
                h=hashlib.sha256()
                with z.open(i) as src,open(target,'wb') as out:
                    for chunk in iter(lambda:src.read(1024*1024),b''):h.update(chunk);out.write(chunk)
                if h.hexdigest()!=rec['sha256']:raise ThemePackageError('素材校验失败')
            for item,key in _slots(p):
                value=item.get(key,'')
                if not value:continue
                if not value.startswith('bundle:') or value[7:] not in refs:raise ThemePackageError('禁止引用包外路径')
                ref=refs[value[7:]];rel=ref['path']
                if ref['kind'] not in ('file','directory'):raise ThemePackageError('引用类型无效')
                if ref['kind']=='file' and rel not in files:raise ThemePackageError('文件引用无效')
                if ref['kind']=='directory' and not re.fullmatch(r'media/r[0-9]{4}',rel):raise ThemePackageError('目录引用无效')
                candidate=scratch/rel
                if not candidate.exists():raise ThemePackageError('素材引用缺失')
                item[key]=str(candidate)
            validate_project(p)
        # Validate before publishing, then replace temporary paths with final paths.
        for item,key in _slots(p):
            if item.get(key):item[key]=str(dest/Path(item[key]).relative_to(scratch))
        from .project import save_project
        save_project(p,str(scratch/'theme.anbp'))
        os.replace(scratch,dest)
        return p
    finally:
        if scratch.exists():shutil.rmtree(scratch)
