# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
# -*- coding: utf-8 -*-
"""Rebuild only AnimeBoot Neo's own static Linux player/PID1 and hash entries.

Pass an existing compatible Zig toolchain and existing libjpeg-turbo/liblzma
build directories; no signed Fedora file is rebuilt or modified.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--toolchain',required=True,type=Path)
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    assets=root/'assets/linux'
    toolchain=args.toolchain.resolve()
    zig=toolchain/'zig/zig-windows-x86_64-0.13.0/zig.exe'
    env=os.environ.copy();env['ZIG_GLOBAL_CACHE_DIR']=str(toolchain/'zig-cache')
    subprocess.run([str(zig),'cc','-target','x86_64-linux-musl','-O3','-static','-pthread','-s',
                    '-I'+str(toolchain/'jpeg/libjpeg-turbo-3.1.2/src'),'-I'+str(toolchain/'jpeg-build'),
                    '-I'+str(toolchain/'xz/xz-5.8.1/src/liblzma/api'),
                    str(assets/'animeboot_player.c'),str(toolchain/'jpeg-build/libjpeg.a'),
                    str(toolchain/'xz-build/liblzma.a'),'-lm','-o',str(assets/'animeboot_player_x86_64')],env=env,check=True)
    subprocess.run([str(zig),'cc','-target','x86_64-linux-musl','-O2','-static','-s',
                    str(assets/'animeboot_init.c'),'-o',str(assets/'animeboot_init_x86_64')],env=env,check=True)
    lock_path=root/'assets/components.lock.json'
    lock=json.loads(lock_path.read_text(encoding='utf-8'))
    for name in ('animeboot_player.c','animeboot_init.c','animeboot_debug_font.h',
                 'animeboot_player_x86_64','animeboot_init_x86_64'):
        path=assets/name
        lock['files']['assets/linux/'+name]={'size':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    lock_path.write_text(json.dumps(lock,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({name:lock['files']['assets/linux/'+name] for name in
                     ('animeboot_player_x86_64','animeboot_init_x86_64')},indent=2))

if __name__=='__main__':main()
