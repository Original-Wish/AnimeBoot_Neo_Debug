# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
"""BIOS file entry: own MBR -> Windows VBR -> GRUB4DOS -> GRUB lnxboot.

Only MBR code and ordinary files are installed. LBA1 onward is untouched.
The preset menu, startup controls and source-documented A20 debug flag change.
"""

# BIOS 文件桥接：固件执行 LBA0，本工程微型 MBR 交给活动分区原 VBR，VBR 读取根目录 bootmgr。
# 安装时这个名字放置带内嵌菜单的 GRUB4DOS 副本；真正 Windows bootmgr 保存在 AnimeBootNeo/windows.bootmgr。
# GRUB4DOS 依据目标文件系统 UUID 找到 grub2.lnx，再进入配对版本 GRUB2。这里不播放动画、不负责轮换标记。
# 写文件前后都检查 Windows 文件身份、属性和分区布局；MBR只替换0..439字节，保留磁盘签名、分区表及55AA。
# 所有二进制替换按受支持字节布局核对；未知文件必须拒绝，不能根据名字或“看起来像”就覆盖。

import ctypes
from contextlib import contextmanager
import os
import struct
from pathlib import Path
from . import windows as w


def partition_layout(mbr, partition_offset=None):
    """Validate only the current active-partition bootstrap layout."""
    if len(mbr)!=512 or mbr[510:512]!=b'\x55\xaa':raise ValueError('MBR 签名无效')
    entries=[mbr[446+i*16:462+i*16] for i in range(4)]
    parts=[(entry[0],entry[4],*struct.unpack_from('<II',entry,8)) for entry in entries if entry[4]]
    if not parts or any(p[1] in (0xee,0x42) or not p[2] or not p[3] or p[0] not in (0,128) for p in parts):
        raise ValueError('不支持 GPT、动态磁盘或无效分区表')
    active=[p for p in parts if p[0]==128]
    if len(active)!=1:raise ValueError('必须有唯一活动分区')
    if partition_offset is not None and active[0][2]*512!=partition_offset:
        raise ValueError('系统卷与活动分区不一致')
    ordered=sorted(parts,key=lambda p:p[2])
    if any(a[2]+a[3]>b[2] for a,b in zip(ordered,ordered[1:])):
        raise ValueError('MBR 主分区范围重叠，拒绝安装')
    return {'first_lba':ordered[0][2],'partition_offset':active[0][2]*512}


def make_bootmgr(loader, boot_binding=None):
    from .quiet_boot import silent_a20
    data = bytearray(silent_a20(loader))
    stage = 0x2000
    if data[stage:stage+5] != b'\xea\x70\x82\x00\x00':
        raise ValueError('Unsupported upstream GRUB4DOS layout')
    bss = struct.unpack_from('<I', data, stage+0x6c)[0]
    offset = bss - 0x8200 + stage
    if data[offset:offset+16] != b'\xb0\x02\x1a\xce'+bytes(12):
        raise ValueError('Invalid GRUB4DOS preset-menu boundary')
    from .grub_config import validate_boot_binding
    binding = validate_boot_binding(boot_binding) if boot_binding is not None else None
    locate = ('uuid ' + binding['fs_uuid'] + '\n') if binding else 'halt\n'
    script = ("debug 0\nhiddenmenu --silent\ndefault 0\ntimeout 0\nfallback 1\n"
              "title AnimeBoot Neo\n"+
              locate+
              "kernel /AnimeBootNeo/grub2.lnx\nboot\n"
              "title Windows\n"+
              locate+
              "chainloader /AnimeBootNeo/windows.bootmgr\nboot\n").encode('ascii')
    if len(script) >= 4096:
        raise ValueError('Preset menu too large')
    # config_file is cleared, and saved_entryno=0 selects the built-in menu.
    # Disable GRUB4DOS keyboard intervention and CD scanning at this first stage.
    # GRUB2's static-menu Esc remains enabled, independently of this setting.
    data[stage+5] |= 0x16
    data[stage+0xc:stage+0x10] = bytes(4)
    data[stage+0x17:stage+0x34] = bytes(0x1d)
    return bytes(data[:offset+16]) + script.ljust(4096, b'\0')


def validate_windows_bootmgr(data):
    # PCAT BOOTMGR has a real-mode stub, not an MZ header at byte zero.
    if len(data) < 32768 or len(data) > 8*1024*1024:
        raise w.DeploymentError('当前 bootmgr 大小异常，请恢复 Windows 原启动入口后重试。')
    if any(marker in data.lower() for marker in (b'eas'+b'sos',b'er'+b'ldr',b'\xe6\x98\x93\xe6\x95\xb0',b'\x13\x66\x70\x65')):
        raise w.DeploymentError('当前 bootmgr 含有第三方旧启动器标记，请先恢复 Windows 原入口。')
    if (b'BOOTMGR' not in data or b'PE\0\0' not in data
            or 'Windows'.encode('utf-16le') not in data):
        raise w.DeploymentError('当前 bootmgr 不是可识别的 Windows Boot Manager，请恢复 Windows 原入口后重试。')


def attributes(path):
    if os.name != 'nt':
        return 0
    result = int(w._windows_kernel32().GetFileAttributesW(str(Path(path))))
    if result == 0xffffffff:
        raise _file_error('读取文件属性', path, ctypes.get_last_error())
    return result


ATTRIBUTE_MASK = 0x1 | 0x2 | 0x4 | 0x20 | 0x100 | 0x1000 | 0x2000


def _file_error(operation, path, error):
    error = int(error)
    detail = ctypes.FormatError(error).strip() if os.name == 'nt' else str(error)
    return w.DeploymentError('%s失败：%s（Windows 错误 %d：%s）' %
                             (operation, path, error, detail))


@contextmanager
def _restore_privilege():
    """Enable an existing elevated token's restore right, then restore it.

    This never changes a file's owner/DACL and cannot grant a missing privilege.
    SeRestorePrivilege + FILE_FLAG_BACKUP_SEMANTICS opens protected boot files
    with FILE_WRITE_ATTRIBUTES/DELETE even when their DACL grants only read.
    """
    from ctypes import wintypes
    class LUID(ctypes.Structure):
        _fields_ = [('LowPart', wintypes.DWORD), ('HighPart', wintypes.LONG)]
    class PRIVILEGES(ctypes.Structure):
        _fields_ = [('Count', wintypes.DWORD), ('Luid', LUID),
                    ('Attributes', wintypes.DWORD)]
    api = w._windows_kernel32()
    api.GetCurrentProcess.argtypes = []
    api.GetCurrentProcess.restype = wintypes.HANDLE
    adv = ctypes.WinDLL('advapi32', use_last_error=True)
    adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                     ctypes.POINTER(wintypes.HANDLE)]
    adv.OpenProcessToken.restype = wintypes.BOOL
    adv.LookupPrivilegeValueW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR,
                                         ctypes.POINTER(LUID)]
    adv.LookupPrivilegeValueW.restype = wintypes.BOOL
    adv.AdjustTokenPrivileges.argtypes = [wintypes.HANDLE, wintypes.BOOL,
        ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
    adv.AdjustTokenPrivileges.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not adv.OpenProcessToken(api.GetCurrentProcess(), 0x28, ctypes.byref(token)):
        raise _file_error('打开恢复权限令牌', '当前安装进程', ctypes.get_last_error())
    previous = PRIVILEGES()
    changed = False
    try:
        requested = PRIVILEGES(); requested.Count = 1; requested.Attributes = 2
        if not adv.LookupPrivilegeValueW(None, 'SeRestorePrivilege', ctypes.byref(requested.Luid)):
            raise _file_error('查询恢复权限', '当前安装进程', ctypes.get_last_error())
        length = wintypes.DWORD()
        ctypes.set_last_error(0)
        ok = adv.AdjustTokenPrivileges(token, False, ctypes.byref(requested),
                ctypes.sizeof(previous), ctypes.byref(previous), ctypes.byref(length))
        error = ctypes.get_last_error()
        changed = bool(ok and previous.Count)
        if not ok or error:
            raise _file_error('启用恢复权限，请通过程序的管理员安装操作重试',
                              '当前安装进程', error)
        yield
    finally:
        if changed:
            adv.AdjustTokenPrivileges(token, False, ctypes.byref(previous), 0, None, None)
        api.CloseHandle(token)


def _handle_information(api, handle, info_class, info, operation, path):
    from ctypes import wintypes
    api.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                               ctypes.c_void_p, wintypes.DWORD]
    api.SetFileInformationByHandle.restype = wintypes.BOOL
    if not api.SetFileInformationByHandle(handle, info_class, ctypes.byref(info), ctypes.sizeof(info)):
        raise _file_error(operation, path, ctypes.get_last_error())


def _set_attributes_protected(path, value):
    from ctypes import wintypes
    class BASIC_INFO(ctypes.Structure):
        _fields_ = [('CreationTime', ctypes.c_longlong), ('LastAccessTime', ctypes.c_longlong),
                    ('LastWriteTime', ctypes.c_longlong), ('ChangeTime', ctypes.c_longlong),
                    ('FileAttributes', wintypes.DWORD)]
    api = w._windows_kernel32()
    with _restore_privilege():
        handle = api.CreateFileW(str(Path(path)), 0x100, 7, None, 3, 0x02000000, None)
        if handle in (None, 0, ctypes.c_void_p(-1).value):
            raise _file_error('打开受保护文件属性句柄', path, ctypes.get_last_error())
        try:
            info = BASIC_INFO(); info.FileAttributes = value
            _handle_information(api, handle, 0, info, '设置受保护文件属性', path)
        finally:
            api.CloseHandle(handle)


def check_readonly_access(path):
    """Read-only preflight: require attribute access before deploying resources."""
    if os.name != 'nt' or not attributes(path)&1:
        return
    api=w._windows_kernel32()
    handle=api.CreateFileW(str(Path(path)),0x100,7,None,3,0,None)
    if handle not in (None,0,ctypes.c_void_p(-1).value):
        api.CloseHandle(handle);return
    error=ctypes.get_last_error()
    if error!=5:raise _file_error('预检启动文件属性权限',path,error)
    with _restore_privilege():
        handle=api.CreateFileW(str(Path(path)),0x100,7,None,3,0x02000000,None)
        if handle in (None,0,ctypes.c_void_p(-1).value):
            raise _file_error('预检受保护启动文件属性权限',path,ctypes.get_last_error())
        api.CloseHandle(handle)


def set_attributes(path, value):
    if os.name != 'nt':
        return
    from ctypes import wintypes
    api = w._windows_kernel32()
    api.SetFileAttributesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    api.SetFileAttributesW.restype = wintypes.BOOL
    desired = int(value) & ATTRIBUTE_MASK
    # A rename can set ARCHIVE even after it was cleared on the staging file.
    # That bookkeeping flag does not affect boot/read/delete access; an
    # otherwise-restored file must not need a write solely to clear ARCHIVE.
    if attributes(path) & (ATTRIBUTE_MASK & ~0x20) == desired & ~0x20:
        return
    if not api.SetFileAttributesW(str(Path(path)), desired or 0x80):
        error = ctypes.get_last_error()
        if error != 5:
            raise _file_error('设置文件属性', path, error)
        try:
            _set_attributes_protected(path, desired or 0x80)
        except Exception as exc:
            raise w.DeploymentError('%s；受保护文件处理：%s' %
                                     (_file_error('设置文件属性', path, error), exc)) from exc
    if attributes(path) & ATTRIBUTE_MASK != desired:
        raise w.DeploymentError('文件属性回读不一致：'+str(path))


def replace_file(source, target):
    """Atomically rename an owned, verified temporary file over its target."""
    try:
        os.replace(str(source), str(target))
        return
    except OSError as exc:
        if os.name != 'nt' or getattr(exc, 'winerror', None) != 5:
            raise
        original = exc
    from ctypes import wintypes
    # FileRenameInfo is supported on Windows 7; no Win10-only rename flags.
    name = str(Path(target).absolute())
    encoded = name.encode('utf-16le')
    class RENAME_INFO(ctypes.Structure):
        _fields_ = [('ReplaceIfExists', wintypes.BOOL), ('RootDirectory', wintypes.HANDLE),
                    ('FileNameLength', wintypes.DWORD), ('FileName', wintypes.WCHAR*(len(encoded)//2+1))]
    api = w._windows_kernel32()
    try:
        with _restore_privilege():
            handle = api.CreateFileW(str(Path(source)), 0x10000, 7, None, 3, 0x02000000, None)
            if handle in (None, 0, ctypes.c_void_p(-1).value):
                raise _file_error('打开启动替换文件句柄', source, ctypes.get_last_error())
            try:
                info = RENAME_INFO(); info.ReplaceIfExists = True
                info.FileNameLength = len(encoded); info.FileName = name
                _handle_information(api, handle, 3, info, '原子替换受保护启动文件', target)
            finally:
                api.CloseHandle(handle)
    except Exception as exc:
        raise w.DeploymentError('原子替换启动文件失败：%s；%s；受保护文件处理：%s' %
                                 (target, original, exc)) from exc


def make_boot_readable(path):
    """Normalize an owned temporary file before publishing a BIOS boot file."""
    if os.name != 'nt':
        return
    from ctypes import wintypes
    api = w._windows_kernel32()
    flags = attributes(path)
    if flags & (0x200 | 0x4000):
        raise w.DeploymentError('启动文件不能是稀疏文件或 EFS 加密文件：'+str(path))
    if flags & 0x800:
        handle = api.CreateFileW(str(Path(path)), 0xc0000000, 3, None, 3, 0, None)
        if handle in (None, 0, ctypes.c_void_p(-1).value):
            raise w.DeploymentError('无法解除启动文件的 NTFS 压缩：'+str(path))
        try:
            no_compression = ctypes.c_ushort(0)
            returned = wintypes.DWORD(0)
            if not api.DeviceIoControl(handle, 0x9c040, ctypes.byref(no_compression), 2,
                                       None, 0, ctypes.byref(returned), None):
                raise w.DeploymentError('解除 NTFS 压缩失败：'+str(path))
            if not api.FlushFileBuffers(handle):
                raise w.DeploymentError('刷新启动文件失败：'+str(path))
        finally:
            api.CloseHandle(handle)
    if attributes(path) & (0x800 | 0x200 | 0x4000):
        raise w.DeploymentError('启动文件属性不适合 BIOS 文件读取：'+str(path))
    if Path(path).name in ('bootstate.env','bootstate.env.neo-new'):
        if not _env_has_disk_allocation(path):
            # Large-MFT NTFS can keep a 1 KiB file resident. Force allocation
            # before shrinking back; never let save_env edit an MFT record.
            with open(path,'r+b') as f:
                size=f.seek(0,2);f.write(bytes(65536));f.flush();os.fsync(f.fileno())
                f.truncate(size);f.flush();os.fsync(f.fileno())
            if not _env_has_disk_allocation(path):
                raise w.DeploymentError('一次性状态文件没有独立数据簇，未切换启动入口。')


def verify_boot_readable(path):
    if attributes(path) & (0x800 | 0x200 | 0x4000):
        raise w.DeploymentError('启动文件后来被压缩、加密或稀疏化：'+str(path))
    if Path(path).name=='bootstate.env' and os.name=='nt' and not _env_has_disk_allocation(path):
        raise w.DeploymentError('一次性状态文件被改为 NTFS 驻留文件，请重新制作。')


def _env_has_disk_allocation(path):
    from ctypes import wintypes
    api=w._windows_kernel32()
    handle=api.CreateFileW(str(Path(path)),0x80000000,7,None,3,0,None)
    if handle in (None,0,ctypes.c_void_p(-1).value):raise w.DeploymentError('无法读取状态文件数据分配。')
    try:
        vcn=ctypes.c_longlong(0);buf=ctypes.create_string_buffer(1024);returned=wintypes.DWORD(0)
        ok=api.DeviceIoControl(handle,0x90073,ctypes.byref(vcn),8,buf,1024,ctypes.byref(returned),None)
        if not ok:
            error=int(ctypes.get_last_error())
            if error==38:return False  # resident data: ERROR_HANDLE_EOF
            if error in (1,50):return True  # FAT implementation has no such ioctl
            raise w.DeploymentError('读取状态文件分配失败（错误%d）。'%error)
        if returned.value<32 or struct.unpack_from('<I',buf.raw)[0]<1:
            return False
        return struct.unpack_from('<q',buf.raw,24)[0]>=0
    finally:api.CloseHandle(handle)


def commit_mbr(read, write, original, installed):
    if original[440:] != installed[440:] or len(original)!=512 or len(installed)!=512:
        raise ValueError('MBR identity differs')
    if read(0,512)!=original:
        raise w.DeploymentError('提交前 MBR 发生变化，未覆盖。')
    if original==installed:return
    try:
        write(0,installed)
        if read(0,512)!=installed:raise w.DeploymentError('微型 MBR 写入后回读不一致。')
    except Exception:
        current=read(0,512)
        if current[440:]!=original[440:]:
            raise w.DeploymentError('微型 MBR 写入失败且磁盘身份变化，已保留备份。')
        write(0,original)
        if read(0,512)!=original:raise w.DeploymentError('微型 MBR 回滚失败，已保留备份。')
        raise


def restore_mbr(read, write, original, installed):
    current=read(0,512)
    if current==original:return
    if current!=installed:
        raise w.DeploymentError('MBR 已被其他程序修改，拒绝覆盖；原始备份已保留。')
    write(0,original)
    if read(0,512)!=original:raise w.DeploymentError('原 MBR 恢复后回读失败。')
