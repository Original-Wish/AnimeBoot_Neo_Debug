# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
"""Construct newc archives from our own static binaries and encoded frames."""

# initramfs是Linux内存根文件系统，不是分区镜像，也不是GRUB核心。
# 第一段压缩newc存/init和静态播放器；第二段存编码动画。内核支持串接归档，GRUB在启动前把文件整体预载到RAM。
# 播放器运行依赖全在归档/内核中，没有普通系统库动态链接、磁盘驱动加载或动画后写盘步骤。
# 新增文件先写.building临时文件，完成对齐、flush/fsync后再os.replace，避免把半份归档当作最终资源。

import hashlib,lzma,os,shutil,stat,tempfile
from pathlib import Path
from .archive import CpioEntry,FileSlice,_write_newc,_parse_anbf_frame_slices,_build_jpeg_chunked_xz_container
from .resources import resource_root,validate_static_elf,digest

# 同一函数用于每一段轮换动画；每段有独立播放器/帧数据，内核文件则在启动分区共用一份。
def build_animation_initramfs(payload_path, output_path, cancel_check=None, progress=None):
    root=resource_root()/'assets/linux'
    for name in ('animeboot_init_x86_64','animeboot_player_x86_64'):validate_static_elf(root/name)
    def cancel():
        if callable(cancel_check) and cancel_check():raise RuntimeError('用户已放弃制作。')
    cancel()
    with tempfile.TemporaryDirectory(prefix='animeboot_init_') as scratch:
        scratch=Path(scratch)
        entries=[CpioEntry(n,mode=stat.S_IFDIR|0o755,ino=i+1,nlink=2) for i,n in enumerate(['dev','proc','sys','tmp','AnimeBootNeo'])]
        entries[3].mode=stat.S_IFDIR|0o1777
        entries.extend([CpioEntry('init',FileSlice(root/'animeboot_init_x86_64'),ino=6,mode=stat.S_IFREG|0o755),
                        CpioEntry('AnimeBootNeo/player',FileSlice(root/'animeboot_player_x86_64'),ino=7,mode=stat.S_IFREG|0o755)])
        # Keep runtime compressed; animation stays as raw QOI or independently
        # decompressible JPEG Chunk32, allowing fast first-frame delivery.
        base=scratch/'base.cpio'
        with base.open('wb') as out:_write_newc(entries+[CpioEntry('TRAILER!!!',ino=9)],out)
        _,codec,frames=_parse_anbf_frame_slices(FileSlice(payload_path))
        animation=Path(payload_path);name='AnimeBootNeo/animation.anbf';codec_info={'codec':'qoi','frame_count':len(frames)}
        if codec==1:
            animation=scratch/'animation.jcx'
            codec_info=_build_jpeg_chunked_xz_container(FileSlice(payload_path),str(animation),chunk_frames=32,preset=1,cancel_check=cancel_check,progress=progress)
            codec_info.pop('path',None);codec_info['codec']='jpeg-chunk32';name='AnimeBootNeo/animation.anbf.jcx'
        cancel()
        temporary=Path(str(output_path)+'.building')
        try:
            with temporary.open('wb') as out:
                compressor=lzma.LZMACompressor(format=lzma.FORMAT_XZ,check=lzma.CHECK_CRC32,preset=1)
                with base.open('rb') as stream:
                    for block in iter(lambda:stream.read(1024*1024),b''):out.write(compressor.compress(block));cancel()
                out.write(compressor.flush())
                # The kernel accepts a sequence of compressed/uncompressed newc archives.
                out.write(b'\0'*((-out.tell())%4))
                _write_newc([CpioEntry(name,FileSlice(animation),ino=8,mode=stat.S_IFREG|0o644),CpioEntry('TRAILER!!!',ino=10)],out)
                out.flush();os.fsync(out.fileno())
            cancel();os.replace(temporary,output_path)
        finally:
            if temporary.exists():temporary.unlink()
    return {'sha256':digest(output_path),'size':Path(output_path).stat().st_size,'animation':codec_info}
