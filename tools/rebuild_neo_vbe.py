# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
"""Optional rebuild of the included BIOS-only Neo VBE module.

Normal build.py uses the locked, prebuilt .mod files and needs no C compiler.
Extract the included Fedora source RPM, then its grub-2.12.tar.xz, and pass the
grub-2.12 source directory plus a Zig 0.13.0 executable to this script.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT=Path(__file__).resolve().parents[1]

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--zig',required=True)
    p.add_argument('--grub-source',required=True)
    args=p.parse_args()
    source=Path(args.grub_source).resolve()
    with tempfile.TemporaryDirectory(prefix='neo-grub-include-') as tmp:
        inc=Path(tmp)/'include';shutil.copytree(source/'include',inc)
        shutil.copytree(inc/'grub/i386',inc/'grub/cpu')
        shutil.copytree(inc/'grub/i386/pc',inc/'grub/machine')
        for mode in ('i386-pc','i386-pc_compat'):
            mods=ROOT/'assets/grub'/mode
            subprocess.run([args.zig,'cc','-target','x86-freestanding','-c','-Oz',
                '-ffreestanding','-fno-stack-protector','-fno-pic','-fno-pie',
                '-fno-asynchronous-unwind-tables','-fno-unwind-tables',
                '-mregparm=3','-mrtd','-mno-sse','-mno-mmx','-msoft-float',
                '-DGRUB_MACHINE_PCBIOS=1','-DGRUB_MACHINE=I386_PC',
                '-I'+str(mods),'-I'+str(inc),str(ROOT/'assets/grub/neo_vbe.c'),
                '-o',str(mods/'neo_vbe.mod')],check=True)
    lockpath=ROOT/'assets/components.lock.json'
    lock=json.loads(lockpath.read_text(encoding='utf-8'))
    proofpath=ROOT/'教程/BOOT_RESOURCE_PROVENANCE_2610.3.json'
    proof=json.loads(proofpath.read_text(encoding='utf-8'))
    for mode in ('i386-pc','i386-pc_compat'):
        rel='assets/grub/'+mode+'/neo_vbe.mod';data=(ROOT/rel).read_bytes()
        rec=dict(size=len(data),sha256=hashlib.sha256(data).hexdigest())
        lock['files'][rel]=rec
        for old in proof['legacy_grub']['files']:
            if old['file']==rel:old.update(rec)
    lockpath.write_text(json.dumps(lock,ensure_ascii=False,indent=2),encoding='utf-8')
    proofpath.write_text(json.dumps(proof,ensure_ascii=False,indent=2),encoding='utf-8')
    print('Rebuilt both BIOS Neo VBE modules; source and other boot files unchanged.')

if __name__=='__main__':main()
