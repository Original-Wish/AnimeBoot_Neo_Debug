# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
"""Silence normal BIOS banners without touching any signed EFI resource.

Only generated BIOS files change. The locked upstream inputs remain intact.
The GRUB4DOS change is the source-level DH=0 setting documented in a20.inc,
not a bypass of A20 setup. Source patches are shipped under licenses/.
"""

# 仅在生成副本中隐藏BIOS启动横幅；不能修改任何已签名EFI或Linux内核。
# A20是传统x86寻址能力初始化，静默只关闭调试输出位，不关闭A20功能。
# 替换之前核对唯一字节特征；上游资源更新导致布局不匹配时明确失败，不能盲目在二进制中搜索相似文本覆盖。

import shutil
from pathlib import Path


A20_STARTUP = bytes.fromhex('b9ff00ba0101fae8')
GRUB_WELCOME = b'Welcome to GRUB!\n\n\0'


def silent_a20(loader):
    data = bytearray(loader)
    # asm.S: movw $0x00ff,%cx; movw $0x0101,%dx; cli; call a20.
    # DL=1 still enables A20. Only DH changes from 1 (debug) to 0 (silent).
    if data.count(A20_STARTUP) != 1:
        raise ValueError('Unsupported GRUB4DOS A20 startup layout')
    offset = data.index(A20_STARTUP)
    data[offset + 5] = 0
    return bytes(data)


def silent_bios_modules(source, target):
    source, target = Path(source), Path(target)
    original = (source / 'kernel.img').read_bytes()
    if original.count(GRUB_WELCOME) != 1:
        raise ValueError('Unsupported GRUB BIOS welcome string layout')
    shutil.copytree(source, target)
    # Same-size NUL string: grub_printf prints nothing; code, addresses and
    # module release stay unchanged. This template is unsigned BIOS code.
    quiet = original.replace(GRUB_WELCOME, bytes(len(GRUB_WELCOME)))
    (target / 'kernel.img').write_bytes(quiet)
    return target
