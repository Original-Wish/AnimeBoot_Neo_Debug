# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
"""Offline, hash-locked resources. No vendor DAT or network fallback."""

# 统一定位源码运行与PyInstaller单文件解包目录。所有资源均从当前安装随包数据读取，不自动下载替代启动器。
# components.lock.json逐项锁定大小和SHA256，防止漏包、损坏和误混版本；哈希清单不等同公钥签名。
# 默认画布图标/角色是编辑素材，合成背景后无需作为独立启动文件再部署。

import hashlib, io, json, os, struct, sys
from pathlib import Path
from PIL import Image, ImageDraw
from .constants import THEME_ROOT

def resource_root():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))

def digest(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()

def validate_components():
    root=resource_root()
    manifest=json.loads((root/'assets/components.lock.json').read_text(encoding='utf-8'))
    for relative,expected in manifest['files'].items():
        p=root/relative
        if os.path.commonpath([str(p.resolve()), str(root.resolve())]) != str(root.resolve()):
            raise ValueError('Invalid component path')
        if not p.is_file() or p.stat().st_size!=expected['size'] or digest(p)!=expected['sha256']:
            raise ValueError('组件缺失或校验失败：'+relative)
    return manifest

def validate_static_elf(path):
    data=Path(path).read_bytes()
    if data[:7]!=b'\x7fELF\x02\x01\x01' or struct.unpack_from('<H',data,18)[0]!=62:
        raise ValueError('Expected x86-64 little-endian ELF')
    off=struct.unpack_from('<Q',data,32)[0]
    size,count=struct.unpack_from('<HH',data,54)
    if size<56 or off+size*count>len(data):raise ValueError('Invalid ELF program headers')
    for i in range(count):
        kind=struct.unpack_from('<I',data,off+i*size)[0]
        if kind in (2,3):raise ValueError('Runtime must have no PT_DYNAMIC or PT_INTERP')
    return True

def default_theme_assets():
    """Original neutral artwork generated in code; never extracted from DAT."""
    def png(im):
        f=io.BytesIO();im.save(f,format='PNG');return f.getvalue()
    transparent=png(Image.new('RGBA',(1,1),(0,0,0,0)))
    tick=Image.new('RGBA',(32,32));ImageDraw.Draw(tick).ellipse((2,2,29,29),fill='white')
    root=resource_root()/'assets'
    result={THEME_ROOT+'/back.png':png(Image.new('RGB',(16,16),'#101827')),
            THEME_ROOT+'/logo.png':(root/'app_icon_source.png').read_bytes(),
            THEME_ROOT+'/CompanyLogo.png':(root/'character.png').read_bytes(),
            THEME_ROOT+'/tick.png':png(tick)}
    return result

import platform
def host_windows_architecture():
    """Return the target architecture for the current Windows installation.

    AnimeBoot Neo Debug is packaged as a 32-bit Python/Qt application for
    compatibility, so pointer size cannot be used to choose the x64 Fedora
    signed boot components.
    PROCESSOR_ARCHITEW6432 exposes the native OS architecture when a 32-bit
    process runs under WOW64.
    """
    arch = (
        os.environ.get("PROCESSOR_ARCHITEW6432")
        or os.environ.get("PROCESSOR_ARCHITECTURE")
        or platform.machine()
        or ""
    ).strip().lower()
    if arch in ("amd64", "x86_64", "x64", "arm64", "aarch64"):
        return "x64"
    return "x86"


def validate_bundled_animebootneo(path=None):
    validate_components()
    return str(resource_root()/'assets/components.lock.json')
