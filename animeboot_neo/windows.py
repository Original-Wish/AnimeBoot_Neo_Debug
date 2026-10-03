# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
"""Windows platform primitives retained from the R3 baseline; no vendor runtime."""

# Windows 平台底层适配：固件模式、分区挂载、BitLocker、原始扇区、BCD和UEFI变量等。
# 这些函数中有真实系统副作用。上层必须先完成身份/权限/事务检查；测试须mock平台写入，不能用主机ESP做普通单元测试。
# BCD显示文本可能本地化，因此优先使用稳定 GUID、二进制设备路径和原生变量；固件条目删除必须匹配记录的身份。
# 本次新增固件条目以操作前快照区分；不能凭 Windows Boot Manager 名称推断是本工具创建的。
# 继续安装状态是唯一持久注册表用途，成功后删除对应项；界面主题不写注册表。



from __future__ import division


import base64


import ctypes


import hashlib


import os


import pickle


import re


import shutil


import string


import struct


import subprocess


import sys


import tempfile


import time


import uuid


import zlib


try:  # Python 3
    import winreg
except ImportError:  # pragma: no cover - Python 2 fallback
    try:
        import _winreg as winreg
    except ImportError:
        winreg = None


class DeploymentError(Exception):
    pass


class DeploymentConsentRequired(DeploymentError):
    """Raised before any boot-chain write when the user must accept a risk."""

    def __init__(self, code, title, message, diagnostic=""):
        super(DeploymentConsentRequired, self).__init__(message)
        self.code = str(code or "consent_required")
        self.title = str(title or "需要确认")
        self.message = str(message or "")
        self.diagnostic = str(diagnostic or "")


BITLOCKER_CONVERSION_STATES = {
    0: "FullyDecrypted",
    1: "FullyEncrypted",
    2: "EncryptionInProgress",
    3: "DecryptionInProgress",
    4: "EncryptionPaused",
    5: "DecryptionPaused",
}


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _read_bytes(path):
    with open(path, "rb") as stream:
        return stream.read()


def _legacy_mbr_layout(mbr0):
    if len(mbr0) != 512:
        raise DeploymentError("Legacy MBR扇区长度不是512字节。")
    if mbr0[510:512] != b"\x55\xaa":
        raise DeploymentError("Legacy MBR缺少55AA签名。")
    active = []
    starts = []
    for index in range(4):
        base = 446 + index * 16
        status = mbr0[base]
        ptype = mbr0[base + 4]
        start_lba = int.from_bytes(mbr0[base + 8:base + 12], "little")
        size_lba = int.from_bytes(mbr0[base + 12:base + 16], "little")
        if status == 0x80:
            active.append(index)
        if ptype != 0 and start_lba > 0 and size_lba > 0:
            starts.append(start_lba)
    if len(active) != 1:
        raise DeploymentError("Legacy MBR活动分区数量为%d（要求恰好1个）。" % len(active))
    if not starts:
        raise DeploymentError("Legacy MBR没有有效分区起始LBA。")
    first_partition_lba = min(starts)
    active_base = 446 + active[0] * 16
    active_start_lba = int.from_bytes(mbr0[active_base + 8:active_base + 12], "little")
    return {
        "active_partition_index": active[0] + 1,
        "active_partition_lba": active_start_lba,
        "first_partition_lba": first_partition_lba,
    }


def _windows_kernel32():
    if os.name != "nt":
        raise DeploymentError("Windows卷物理映射只能在Windows中执行。")
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetFileAttributesW.restype = wintypes.DWORD
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.DeviceIoControl.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    kernel32.DeviceIoControl.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    kernel32.GetVolumePathNameW.restype = wintypes.BOOL
    kernel32.GetDiskFreeSpaceW.argtypes = [
        wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetDiskFreeSpaceW.restype = wintypes.BOOL
    kernel32.SetFilePointerEx.argtypes = [
        wintypes.HANDLE, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD,
    ]
    kernel32.SetFilePointerEx.restype = wintypes.BOOL
    kernel32.ReadFile.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    kernel32.ReadFile.restype = wintypes.BOOL
    kernel32.WriteFile.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    kernel32.WriteFile.restype = wintypes.BOOL
    kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    kernel32.FlushFileBuffers.restype = wintypes.BOOL
    return kernel32


def _flush_physical_disk_compat(kernel32, handle, disk_path):
    """Flush a raw PhysicalDrive write without false-failing on old drivers.

    Windows 7-era raw disk/device stacks (including some virtual-disk drivers)
    can return ERROR_INVALID_FUNCTION / ERROR_NOT_SUPPORTED for
    FlushFileBuffers on a raw PhysicalDrive handle even though aligned WriteFile I/O
    itself is supported.  Treat only those two *capability* errors as
    "flush unavailable" and require the caller to perform exact read-back
    verification.  Any other flush error remains fatal.
    """
    if kernel32.FlushFileBuffers(handle):
        return True
    error = int(ctypes.get_last_error())
    if error in (1, 50):  # ERROR_INVALID_FUNCTION / ERROR_NOT_SUPPORTED
        return False
    raise DeploymentError("刷新物理磁盘%s失败（错误%d）。" % (disk_path, error))




def _raw_volume_path(volume_root):
    root = str(volume_root or "")
    if len(root) >= 2 and root[1] == ":":
        return "\\\\.\\%s:" % root[0]
    raise DeploymentError("启动卷没有可用于原始读取校验的盘符：%s" % root)


def _powershell_path():
    if os.name != "nt":
        return "powershell.exe"
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    # A 32-bit Python running on 64-bit Windows is redirected away from
    # System32. Sysnative keeps the WMI/BitLocker query architecture-neutral.
    sysnative = os.path.join(system_root, "Sysnative", "WindowsPowerShell", "v1.0", "powershell.exe")
    if os.path.isfile(sysnative):
        return sysnative
    native = os.path.join(system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    if os.path.isfile(native):
        return native
    return "powershell.exe"


def _raw_volume_boot_sector(volume_root):
    return _read_volume_boot_bytes(volume_root,4096)


def _read_volume_boot_bytes(volume_root,length):
    """Read the system volume boot sector without relying on localized tools."""
    if os.name != "nt":
        return b""
    from ctypes import wintypes
    kernel32 = _windows_kernel32()
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    FILE_SHARE_DELETE = 0x00000004
    OPEN_EXISTING = 3
    raw_path = _raw_volume_path(volume_root)
    handle = kernel32.CreateFileW(
        raw_path, GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None, OPEN_EXISTING, 0, None,
    )
    if handle in (None, 0, INVALID_HANDLE_VALUE):
        raise DeploymentError("无法读取系统卷加密状态：打开%s失败（错误%d）。" % (raw_path, ctypes.get_last_error()))
    try:
        buffer = ctypes.create_string_buffer(length)
        got = wintypes.DWORD(0)
        if not kernel32.ReadFile(handle, buffer, length, ctypes.byref(got), None):
            raise DeploymentError("无法读取系统卷加密状态：读取%s失败（错误%d）。" % (raw_path, ctypes.get_last_error()))
        if int(got.value) < 512:
            raise DeploymentError("无法读取系统卷加密状态：系统卷引导扇区读取长度不足。")
        return buffer.raw[:int(got.value)]
    finally:
        kernel32.CloseHandle(handle)


def check_system_volume_encryption():
    """Fail-closed BitLocker/Device Encryption preflight.

    Official 4.15.1.900 refuses to install its boot item while the Windows
    system volume is BitLocker/device-encrypted.  We mirror that policy and
    intentionally require ConversionStatus == FullyDecrypted; merely suspending
    protectors is not enough because the on-disk volume remains encrypted.
    """
    if os.name != "nt":
        return {"ok": True, "skipped": True, "state": "non-windows"}
    system_drive = str(os.environ.get("SystemDrive", "C:") or "C:").rstrip("\\/")
    if len(system_drive) < 2 or system_drive[1] != ":":
        raise DeploymentError("无法确定Windows系统卷，拒绝安装启动项。")

    status = None
    encryption_percentage = None
    wmi_error = ""
    ps = _powershell_path()
    drive_literal = system_drive.replace("'", "''")
    command = (
        "$ErrorActionPreference='Stop';"
        "$v=Get-WmiObject -Namespace 'root\\CIMV2\\Security\\MicrosoftVolumeEncryption' "
        "-Class Win32_EncryptableVolume -Filter \"DriveLetter='%s'\";"
        "if($null -eq $v){Write-Output 'STATUS=NO_VOLUME'}"
        "else{$r=Invoke-WmiMethod -InputObject $v -Name GetConversionStatus -ArgumentList 0;"
        "Write-Output ('STATUS=' + [int]$r.ConversionStatus);"
        "Write-Output ('PERCENT=' + [int]$r.EncryptionPercentage)}"
    ) % drive_literal
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.run(
            [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags,
            check=False, timeout=20,
        )
        out = proc.stdout.decode("utf-8", "replace")
        match = re.search(r"STATUS=(NO_VOLUME|[0-5])", out)
        percentage_match = re.search(r"PERCENT=(\d{1,3})", out)
        encryption_percentage = int(percentage_match.group(1)) if percentage_match else None
        if match and match.group(1) != "NO_VOLUME":
            status = int(match.group(1))
        elif proc.returncode != 0:
            wmi_error = proc.stderr.decode("utf-8", "replace").strip()
    except Exception as exc:
        wmi_error = str(exc)

    raw = _raw_volume_boot_sector(system_drive + "\\")
    fve_signature = b"-FVE-FS-" in raw[:128]

    if status is not None:
        state = BITLOCKER_CONVERSION_STATES.get(status, "Unknown")
        if status != 0:
            raise DeploymentError(
                "检测到Windows系统卷%s仍处于BitLocker/设备加密状态（%s）。\n\n"
                "请先在Windows的BitLocker/设备加密设置中关闭系统盘加密，并等待系统提示解密已经完成、系统盘不再处于BitLocker/设备加密状态。"
                "完成后再点击“继续安装启动项”。仅暂停保护、临时解锁或只关闭保护器并不能满足安装条件。\n\n"
                "本次已构建文件会保留，无需重新制作。" % (system_drive, state)
            )
        if fve_signature:
            # WMI says decrypted but the boot sector still advertises FVE. Do
            # not guess during a state transition; this is exactly where a
            # boot-chain write should fail closed.
            raise DeploymentError(
                "BitLocker管理接口报告系统卷已解密，但磁盘仍带有FVE加密签名。"
                "请等待Windows确认系统盘解密已经完成；如果刚刚关闭了加密，建议完成后重启Windows再试。"
            )
        return {
            "ok": True, "system_drive": system_drive,
            "conversion_status": status, "state": state,
            "encryption_percentage": encryption_percentage,
            "fve_signature": False, "method": "WMI-GetConversionStatus+raw-boot-sector",
        }

    if fve_signature:
        raise DeploymentError(
            "检测到Windows系统卷%s存在BitLocker/设备加密FVE签名。\n\n"
            "请先完整关闭并解密系统卷，再点击“继续安装启动项”。本次已构建文件会保留。" % system_drive
        )
    if not raw:
        raise DeploymentError("无法可靠确认系统卷BitLocker/设备加密状态，拒绝修改启动链。")
    return {
        "ok": True, "system_drive": system_drive,
        "conversion_status": None, "state": "NoFVE",
        "fve_signature": False, "method": "raw-boot-sector-fallback",
        "wmi_diagnostic": wmi_error,
    }


def _run_bcdedit(args, allow_failure=False, store_path=None):
    exe = _native_system_executable("bcdedit.exe")
    command = [exe]
    if store_path:
        command.extend(["/store", str(store_path)])
    command.extend(list(args))
    proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False, timeout=30)
    out = _decode_console_output(proc.stdout).strip()
    err = _decode_console_output(proc.stderr).strip()
    if proc.returncode != 0 and not allow_failure:
        raise DeploymentError("BCDEdit执行失败：%s；%s" % (" ".join(args), err or out or proc.returncode))
    return int(proc.returncode), out, err


def _bcd_guid_is_present(guid, store_path=None):
    """Postcondition probe used instead of trusting BCDEdit's exit code alone."""
    if not guid:
        return False
    rc, out, err = _run_bcdedit(["/enum", guid, "/v"], allow_failure=True, store_path=store_path)
    text = (out + "\n" + err).lower()
    return str(guid).lower() in text


def _delete_bcd_guid_robust(guid, store_path=None):
    """Delete a BCD object and verify the resulting store state.

    Some UEFI machines return ERROR_DEVICE_NOT_EXIST from BCDEdit even after
    the requested object has already been removed.  Treat the store
    postcondition as authoritative and try /cleanup before declaring residue.
    When a concrete ESP BCD path is available it is tried first; the ordinary
    system-store route remains a compatibility fallback.
    """
    attempts = []
    stores = [store_path]
    if store_path:
        stores.append(None)
    for current_store in stores:
        for args in (["/delete", guid], ["/delete", guid, "/cleanup"]):
            rc, out, err = _run_bcdedit(args, allow_failure=True, store_path=current_store)
            attempts.append({
                "args": list(args), "store_path": current_store,
                "returncode": rc, "stdout": out, "stderr": err,
            })
            if not _bcd_guid_is_present(guid, store_path=current_store):
                return {"guid": guid, "deleted": True, "attempts": attempts}
    return {"guid": guid, "deleted": False, "attempts": attempts}


def _volume_filesystem_name(root):
    """Return the filesystem name for a mounted Windows volume root."""
    if os.name != "nt":
        return ""
    try:
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_info = kernel32.GetVolumeInformationW
        get_info.argtypes = [
            wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD,
        ]
        get_info.restype = wintypes.BOOL
        fs_name = ctypes.create_unicode_buffer(64)
        serial = wintypes.DWORD(0)
        max_component = wintypes.DWORD(0)
        flags = wintypes.DWORD(0)
        if not get_info(
            str(root), None, 0, ctypes.byref(serial), ctypes.byref(max_component),
            ctypes.byref(flags), fs_name, len(fs_name),
        ):
            return ""
        return fs_name.value.strip().upper()
    except Exception:
        return ""


def _inspect_windows_esp_root(root):
    """Fail closed unless *root* is a plausible current Windows ESP.

    File-tree resemblance alone is never enough: WinRE/OEM partitions and the
    Windows system volume can contain copied EFI files.  A Windows ESP must be
    FAT/FAT32 and must contain the active Microsoft boot manager + BCD store.
    """
    root = str(root or "")
    filesystem = _volume_filesystem_name(root)
    reasons = []
    system_drive = str(os.environ.get("SystemDrive", "") or "").rstrip("\\/").upper()
    candidate_drive = root[:2].upper() if len(root) >= 2 and root[1:2] == ":" else ""
    if system_drive and candidate_drive == system_drive:
        reasons.append("目标是Windows系统卷%s，不允许作为ESP写入目标" % candidate_drive)
    if filesystem not in ("FAT", "FAT32"):
        reasons.append("文件系统=%s（Windows ESP应为FAT/FAT32）" % (filesystem or "无法读取"))
    msboot = os.path.join(root, "EFI", "Microsoft", "Boot")
    bootmgfw = os.path.join(msboot, "bootmgfw.efi")
    bcd = os.path.join(msboot, "BCD")
    if not os.path.isfile(bootmgfw):
        reasons.append(r"缺少EFI\Microsoft\Boot\bootmgfw.efi")
    if not os.path.isfile(bcd):
        reasons.append(r"缺少EFI\Microsoft\Boot\BCD")
    return {
        "ok": not reasons,
        "root": root,
        "filesystem": filesystem,
        "reasons": reasons,
        "bootmgfw": bootmgfw,
        "bcd": bcd,
    }


def _format_diagnostics(items, limit=6):
    """Render diagnostics for users instead of exposing Python list reprs."""
    lines = []
    for item in list(items or [])[:int(limit)]:
        if isinstance(item, (list, tuple)):
            parts = [str(value).strip() for value in item if str(value).strip()]
            text = " | ".join(parts)
        else:
            text = str(item or "").strip()
        # Some native tools return escaped/newline-heavy messages.  Keep one
        # readable bullet per diagnostic record.
        text = text.replace("\\r\\n", " ").replace("\\n", " ").replace("\\r", " ")
        text = " ".join(text.split())
        if text:
            lines.append("• " + text)
    return "\n".join(lines) if lines else "• 未取得额外系统诊断输出"


EFI_IMAGE_SECURITY_DATABASE_GUID = "{d719b2cb-3d3a-4596-a3bc-dad00e67656f}"


EFI_GLOBAL_VARIABLE_GUID = "{8BE4DF61-93CA-11d2-AA0D-00E098032B8C}"


EFI_CERT_X509_GUID = "a5c059a1-94e4-4aa7-87b5-ab155c2bf072"


def _enable_system_environment_privilege():
    """Best-effort enable SeSystemEnvironmentPrivilege in the elevated process."""
    if os.name != "nt":
        return False
    try:
        from ctypes import wintypes
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        TOKEN_ADJUST_PRIVILEGES = 0x0020
        TOKEN_QUERY = 0x0008
        SE_PRIVILEGE_ENABLED = 0x00000002

        class LUID(ctypes.Structure):
            _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

        class LUID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]

        class TOKEN_PRIVILEGES(ctypes.Structure):
            _fields_ = [("PrivilegeCount", wintypes.DWORD),
                        ("Privileges", LUID_AND_ATTRIBUTES * 1)]

        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
            ctypes.byref(token),
        ):
            return False
        try:
            luid = LUID()
            if not advapi32.LookupPrivilegeValueW(
                None, "SeSystemEnvironmentPrivilege", ctypes.byref(luid)
            ):
                return False
            tp = TOKEN_PRIVILEGES()
            tp.PrivilegeCount = 1
            tp.Privileges[0].Luid = luid
            tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
            ctypes.set_last_error(0)
            if not advapi32.AdjustTokenPrivileges(
                token, False, ctypes.byref(tp), 0, None, None
            ):
                return False
            return ctypes.get_last_error() != 1300  # ERROR_NOT_ALL_ASSIGNED
        finally:
            kernel32.CloseHandle(token)
    except Exception:
        return False


def _get_firmware_variable_bytes(name, vendor_guid, max_bytes=4 * 1024 * 1024):
    """Read one UEFI variable via the native Windows API.

    The V2 install stage is elevated already.  We still explicitly enable the
    system-environment privilege because firmware db access otherwise varies
    between Windows versions and process token configurations.
    """
    if os.name != "nt" or _firmware_type() != "uefi":
        raise DeploymentError("当前不是可读取UEFI固件变量的Windows UEFI环境。")
    _enable_system_environment_privilege()
    try:
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        func = getattr(kernel32, "GetFirmwareEnvironmentVariableExW", None)
        if func is None:
            raise DeploymentError("当前Windows缺少GetFirmwareEnvironmentVariableExW接口。")
        func.argtypes = [
            wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPVOID, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        func.restype = wintypes.DWORD
        size = 64 * 1024
        while size <= int(max_bytes):
            buf = (ctypes.c_ubyte * size)()
            attrs = wintypes.DWORD(0)
            ctypes.set_last_error(0)
            got = int(func(name, vendor_guid, ctypes.byref(buf), size, ctypes.byref(attrs)))
            if got > 0:
                return bytes(buf[:got]), int(attrs.value)
            error = int(ctypes.get_last_error())
            if error == 122:  # ERROR_INSUFFICIENT_BUFFER
                size *= 2
                continue
            raise DeploymentError(
                "读取UEFI变量%s失败（WinError %d）。" % (name, error)
            )
        raise DeploymentError("UEFI变量%s超过允许读取上限。" % name)
    except DeploymentError:
        raise
    except Exception as exc:
        raise DeploymentError("读取UEFI变量%s失败：%s" % (name, exc))


def _set_firmware_variable_bytes(name, vendor_guid, data):
    """Write or delete one UEFI variable using the same Win32 API family as AnimeBootNeo.

    ``data is None`` deletes the variable.  This helper is used for exact
    BootOrder/Boot#### transaction writes and rollback.  Clean auxiliary option
    creation uses the companion creation helper so its variable attributes can
    be supplied explicitly.
    """
    if os.name != "nt" or _firmware_type() != "uefi":
        raise DeploymentError("当前不是可写入UEFI固件变量的Windows UEFI环境。")
    _enable_system_environment_privilege()
    try:
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        func = getattr(kernel32, "SetFirmwareEnvironmentVariableW", None)
        if func is None:
            raise DeploymentError("当前Windows缺少SetFirmwareEnvironmentVariableW接口。")
        func.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPVOID, wintypes.DWORD]
        func.restype = wintypes.BOOL
        if data is None:
            buffer = None
            size = 0
        else:
            raw = bytes(data)
            buffer = ctypes.create_string_buffer(raw, len(raw))
            size = len(raw)
        ctypes.set_last_error(0)
        ok = bool(func(name, vendor_guid, buffer, size))
        if not ok:
            error = int(ctypes.get_last_error())
            # Deleting an already-absent firmware variable is an acceptable
            # postcondition during idempotent cleanup.
            if data is None and error in (2, 1168, 203):
                return False
            raise DeploymentError("写入UEFI变量%s失败（WinError %d）。" % (name, error))
        return True
    except DeploymentError:
        raise
    except Exception as exc:
        raise DeploymentError("写入UEFI变量%s失败：%s" % (name, exc))


def _efi_load_option_description(raw):
    """Return the UTF-16 description from an EFI_LOAD_OPTION Boot#### value."""
    data = bytes(raw or b"")
    if len(data) < 8:
        return ""
    # EFI_LOAD_OPTION: UINT32 Attributes, UINT16 FilePathListLength,
    # CHAR16 Description[] followed by the device path list.
    end = 6
    while end + 1 < len(data):
        if data[end:end + 2] == b"\x00\x00":
            break
        end += 2
    if end + 1 >= len(data):
        return ""
    try:
        return data[6:end].decode("utf-16le", "strict")
    except Exception:
        return ""


def _efi_load_option_file_paths(raw):
    """Return MEDIA_FILEPATH_DP strings from one EFI_LOAD_OPTION.

    No Boot#### number, disk GUID, partition GUID or description is assumed.
    The parser follows the EFI_LOAD_OPTION's own FilePathListLength and walks
    standard EFI_DEVICE_PATH_PROTOCOL nodes, accepting only MEDIA_FILEPATH_DP
    (Type 0x04, SubType 0x04) payloads.
    """
    data = bytes(raw or b"")
    if len(data) < 8:
        return []
    try:
        file_path_list_len = int(struct.unpack_from("<H", data, 4)[0])
    except Exception:
        return []
    desc_end = 6
    while desc_end + 1 < len(data):
        if data[desc_end:desc_end + 2] == b"\x00\x00":
            break
        desc_end += 2
    if desc_end + 1 >= len(data):
        return []
    start = desc_end + 2
    end = min(len(data), start + file_path_list_len)
    paths = []
    pos = start
    while pos + 4 <= end:
        node_type = data[pos]
        node_subtype = data[pos + 1]
        node_len = int(struct.unpack_from("<H", data, pos + 2)[0])
        if node_len < 4 or pos + node_len > end:
            break
        if node_type == 0x04 and node_subtype == 0x04:
            payload = data[pos + 4:pos + node_len]
            if len(payload) % 2:
                payload = payload[:-1]
            try:
                text = payload.decode("utf-16le", "strict").rstrip("\x00")
            except Exception:
                text = ""
            if text:
                paths.append(text)
        if node_type == 0x7F:
            break
        pos += node_len
    return paths


def _efi_load_option_optional_data(raw):
    """Return the EFI_LOAD_OPTION OptionalData bytes exactly as stored."""
    data = bytes(raw or b"")
    if len(data) < 8:
        return b""
    try:
        file_path_list_len = int(struct.unpack_from("<H", data, 4)[0])
    except Exception:
        return b""
    desc_end = 6
    while desc_end + 1 < len(data):
        if data[desc_end:desc_end + 2] == b"\x00\x00":
            break
        desc_end += 2
    if desc_end + 1 >= len(data):
        return b""
    start = desc_end + 2
    end = start + file_path_list_len
    if end > len(data):
        return b""
    return data[end:]


def _efi_clone_load_option_with_filepath(template_raw, description, new_path):
    """Clone a same-ESP EFI_LOAD_OPTION while replacing only Description/File().

    The Windows Boot Manager Boot#### is used solely as a native device-path
    template for the machine's real ESP.  The returned option deliberately has
    *empty OptionalData*: no ``WINDOWS``/``BCDOBJECT={...}`` trailer is ever
    created.  This is the crucial difference from ``bcdedit /copy {bootmgr}``.
    """
    data = bytes(template_raw or b"")
    if len(data) < 8:
        raise DeploymentError("UEFI模板启动项长度异常。")
    load_attrs = int(struct.unpack_from("<I", data, 0)[0])
    file_path_list_len = int(struct.unpack_from("<H", data, 4)[0])
    desc_end = 6
    while desc_end + 1 < len(data):
        if data[desc_end:desc_end + 2] == b"\x00\x00":
            break
        desc_end += 2
    if desc_end + 1 >= len(data):
        raise DeploymentError("UEFI模板启动项Description损坏。")
    fp_start = desc_end + 2
    fp_end = fp_start + file_path_list_len
    if fp_end > len(data):
        raise DeploymentError("UEFI模板启动项FilePathList长度异常。")
    file_list = data[fp_start:fp_end]

    replacement_payload = str(new_path).encode("utf-16le") + b"\x00\x00"
    replacement_node = b"\x04\x04" + struct.pack("<H", 4 + len(replacement_payload)) + replacement_payload
    rebuilt = bytearray()
    pos = 0
    replaced = False
    while pos + 4 <= len(file_list):
        node_type = file_list[pos]
        node_subtype = file_list[pos + 1]
        node_len = int(struct.unpack_from("<H", file_list, pos + 2)[0])
        if node_len < 4 or pos + node_len > len(file_list):
            raise DeploymentError("UEFI模板启动项DevicePath节点损坏。")
        if node_type == 0x04 and node_subtype == 0x04 and not replaced:
            rebuilt.extend(replacement_node)
            replaced = True
        else:
            rebuilt.extend(file_list[pos:pos + node_len])
        pos += node_len
    if pos != len(file_list) or not replaced:
        raise DeploymentError("UEFI模板启动项没有可替换的MEDIA_FILEPATH节点。")

    desc = str(description).encode("utf-16le") + b"\x00\x00"
    raw = struct.pack("<IH", load_attrs, len(rebuilt)) + desc + bytes(rebuilt)
    if _efi_load_option_description(raw) != str(description):
        raise DeploymentError("新建灾备启动项Description自检失败。")
    expected = _normalized_bcd_path(new_path)
    actual_paths = [_normalized_bcd_path(v) for v in _efi_load_option_file_paths(raw)]
    if expected not in actual_paths:
        raise DeploymentError("新建灾备启动项EFI路径自检失败。")
    if _efi_load_option_optional_data(raw):
        raise DeploymentError("新建灾备启动项意外包含OptionalData。")
    return raw


def _firmware_variable_is_absent_error(exc):
    text = str(exc or "")
    return bool(re.search(r"WinError\s+(?:2|203|1168)\b", text, re.I))


def _find_free_firmware_boot_number(order):
    """Find a Boot#### number not referenced and not present as an orphan variable."""
    used = set(int(v) for v in (order or []))
    for number in range(0x10000):
        if number in used:
            continue
        try:
            _get_firmware_variable_bytes(
                "Boot%04X" % number, EFI_GLOBAL_VARIABLE_GUID, max_bytes=1024 * 1024
            )
        except DeploymentError as exc:
            if _firmware_variable_is_absent_error(exc):
                return number
            raise
    raise DeploymentError("UEFI NVRAM中没有可用的Boot####编号。")


def _set_new_firmware_variable_bytes(name, vendor_guid, data, attributes=0x00000007):
    """Create one new UEFI variable with explicit NV+BS+RT attributes.

    Windows 8+ exposes SetFirmwareEnvironmentVariableExW.  The ordinary API is
    retained only as a compatibility fallback on older Windows builds.
    """
    if os.name != "nt" or _firmware_type() != "uefi":
        raise DeploymentError("当前不是可写入UEFI固件变量的Windows UEFI环境。")
    _enable_system_environment_privilege()
    raw = bytes(data or b"")
    try:
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        func = getattr(kernel32, "SetFirmwareEnvironmentVariableExW", None)
        if func is None:
            # Older systems: the project already uses the non-Ex API for native
            # firmware writes.  If this platform cannot create a variable this
            # way, the caller rolls back and safely skips the optional item.
            return _set_firmware_variable_bytes(name, vendor_guid, raw)
        func.argtypes = [
            wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPVOID, wintypes.DWORD,
            wintypes.DWORD,
        ]
        func.restype = wintypes.BOOL
        buffer = ctypes.create_string_buffer(raw, len(raw)) if raw else None
        ctypes.set_last_error(0)
        ok = bool(func(name, vendor_guid, buffer, len(raw), int(attributes)))
        if not ok:
            raise DeploymentError(
                "创建UEFI变量%s失败（WinError %d）。" % (name, int(ctypes.get_last_error()))
            )
        return True
    except DeploymentError:
        raise
    except Exception as exc:
        raise DeploymentError("创建UEFI变量%s失败：%s" % (name, exc))


def _read_firmware_boot_order():
    raw, _attrs = _get_firmware_variable_bytes(
        "BootOrder", EFI_GLOBAL_VARIABLE_GUID, max_bytes=64 * 1024
    )
    if len(raw) % 2:
        raise DeploymentError("UEFI BootOrder长度异常。")
    return list(struct.unpack("<%dH" % (len(raw) // 2), raw)) if raw else []


def _firmware_boot_entry_record(number):
    """Read one Boot#### as an identity-bearing EFI_LOAD_OPTION record.

    Boot#### numbers are reusable identifiers, not stable identities.  Real
    machines have shown that Windows/firmware may delete a managed Boot0000
    during our transaction and then immediately materialise a *different*
    Windows Boot Manager using the same Boot0000 number.  Therefore every
    transaction snapshot fingerprints the complete EFI_LOAD_OPTION bytes.
    """
    number = int(number)
    raw, attrs = _get_firmware_variable_bytes(
        "Boot%04X" % number, EFI_GLOBAL_VARIABLE_GUID, max_bytes=1024 * 1024
    )
    raw = bytes(raw or b"")
    return {
        "number": number,
        "name": "Boot%04X" % number,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "description": _efi_load_option_description(raw),
        "paths": _efi_load_option_file_paths(raw),
        "attributes": int(attrs),
        "raw": raw,
    }


def _snapshot_firmware_boot_entries(order=None):
    """Capture BootOrder plus the full identity of every referenced Boot####.

    This is intentionally strict: if a BootOrder-referenced entry cannot be
    read, the caller cannot safely distinguish an old entry from a same-number
    replacement later in the transaction, so the snapshot fails closed.
    """
    current_order = list(_read_firmware_boot_order() if order is None else order)
    entries = {}
    for number in current_order:
        number = int(number)
        if number not in entries:
            entries[number] = _firmware_boot_entry_record(number)
    return {"order": current_order, "entries": entries}


def _target_esp_partition_identity(root):
    """Read the mounted ESP's GPT identity; never infer it from an EFI filename."""
    if os.name != "nt":
        raise DeploymentError("ESP分区身份读取仅支持Windows。")
    from ctypes import wintypes
    kernel32 = _windows_kernel32()
    handle = kernel32.CreateFileW(_raw_volume_path(root), 0x80000000, 0x3, None, 3, 0, None)
    if handle in (None, 0, ctypes.c_void_p(-1).value):
        raise DeploymentError("无法只读打开当前ESP识别GPT分区身份。")
    try:
        # PARTITION_INFORMATION_EX has the same documented 8-byte-aligned
        # offsets in Win32 and Win64. Its GPT union begins at byte 32.
        output = ctypes.create_string_buffer(256)
        returned = wintypes.DWORD(0)
        if not kernel32.DeviceIoControl(handle, 0x00070048, None, 0, output,
                                        len(output), ctypes.byref(returned), None):
            raise DeploymentError("读取ESP的GPT分区身份失败（WinError %d）。" % ctypes.get_last_error())
        data = output.raw[:int(returned.value)]
        if len(data) < 144 or struct.unpack_from("<I", data, 0)[0] != 1:
            raise DeploymentError("当前ESP没有可安全匹配的GPT分区身份；请手动设置UEFI启动项。")
        if data[32:48] != uuid.UUID("c12a7328-f81f-11d2-ba4b-00a0c93ec93b").bytes_le:
            raise DeploymentError("当前卷的GPT分区类型不是EFI系统分区。")
        partition_id = data[48:64]
        if partition_id == b"\x00" * 16:
            raise DeploymentError("当前ESP的GPT分区GUID无效。")
        start, length = struct.unpack_from("<qq", data, 8)
        number = struct.unpack_from("<I", data, 24)[0]
        if start <= 0 or length <= 0 or number <= 0:
            raise DeploymentError("当前ESP的GPT分区范围无效。")
        return {"partition_guid": str(uuid.UUID(bytes_le=partition_id)),
                "partition_guid_bytes": partition_id, "partition_number": int(number),
                "starting_offset": int(start), "partition_length": int(length)}
    finally:
        kernel32.CloseHandle(handle)


def _efi_load_option_partition_identity(raw):
    """Parse one GPT HD()/File() path, rejecting ambiguous multi-instance paths."""
    data = bytes(raw or b"")
    if len(data) < 8:
        raise DeploymentError("UEFI启动项内容过短。")
    desc_end = 6
    while desc_end + 1 < len(data) and data[desc_end:desc_end + 2] != b"\x00\x00":
        desc_end += 2
    if desc_end + 1 >= len(data):
        raise DeploymentError("UEFI启动项Description不完整。")
    start = desc_end + 2
    end = start + struct.unpack_from("<H", data, 4)[0]
    if end > len(data):
        raise DeploymentError("UEFI启动项DevicePath长度无效。")
    pos, hard_drive, files, ended = start, None, 0, False
    while pos + 4 <= end:
        kind, subtype, size = struct.unpack_from("<BBH", data, pos)
        if size < 4 or pos + size > end:
            raise DeploymentError("UEFI启动项DevicePath节点损坏。")
        if kind == 0x04 and subtype == 0x01:
            if hard_drive is not None or size != 42:
                raise DeploymentError("UEFI启动项包含多个或无效的HD分区节点。")
            if data[pos + 40] != 2 or data[pos + 41] != 2:
                raise DeploymentError("UEFI启动项HD节点不是GPT GUID格式。")
            hard_drive = {"partition_number": struct.unpack_from("<I", data, pos + 4)[0],
                          "starting_lba": struct.unpack_from("<Q", data, pos + 8)[0],
                          "size_lba": struct.unpack_from("<Q", data, pos + 16)[0],
                          "partition_guid_bytes": data[pos + 24:pos + 40]}
        elif kind == 0x04 and subtype == 0x04:
            files += 1
        elif kind == 0x7F:
            if subtype != 0xFF or size != 4 or pos + size != end:
                raise DeploymentError("不自动复制多实例或不完整的UEFI DevicePath。")
            ended = True
        pos += size
    if pos != end or not ended or hard_drive is None or files != 1:
        raise DeploymentError("UEFI启动项缺少唯一的GPT分区及启动文件路径。")
    return hard_drive


def _native_template_for_esp(root, snapshot):
    identity = _target_esp_partition_identity(root)
    matches = []
    for number in snapshot.get("order", []):
        record = snapshot.get("entries", {}).get(int(number), {})
        try:
            candidate = _efi_load_option_partition_identity(record.get("raw", b""))
        except DeploymentError:
            continue
        if candidate["partition_guid_bytes"] != identity["partition_guid_bytes"]:
            continue
        if candidate["partition_number"] != identity["partition_number"]:
            continue
        # The ESP byte range and the HD() sector range must describe the same
        # partition. Determine the logical sector size from their exact ratio.
        lba, count = candidate["starting_lba"], candidate["size_lba"]
        if not lba or not count or identity["starting_offset"] % lba:
            continue
        sector_size = identity["starting_offset"] // lba
        if sector_size not in (512, 1024, 2048, 4096) or count * sector_size != identity["partition_length"]:
            continue
        matches.append(record)
    if not matches:
        raise DeploymentError("当前BootOrder中没有与本次ESP分区GUID及范围一致的启动项模板；请手动设置UEFI启动项。")
    wanted = _normalized_bcd_path(r"\EFI\Microsoft\Boot\bootmgfw.efi")
    template = next((record for record in matches if wanted in
                     [_normalized_bcd_path(value) for value in record.get("paths", [])]), matches[0])
    return template, identity


def _verify_native_baseline(snapshot):
    """Do not silently rewrite, remove, or accept mutations to old Boot#### values."""
    for number, record in snapshot.get("entries", {}).items():
        current = _firmware_boot_entry_record(number)
        if current["sha256"] != record["sha256"] or current["attributes"] != record["attributes"]:
            raise DeploymentError("原有Boot%04X的内容发生变化；拒绝继续自动修复。" % int(number))


def _remove_managed_native_boot_entry(number, expected_sha256):
    """Idempotently remove our exact native identity, never a reused Boot number."""
    number = int(number)
    if not 0 <= number <= 0xFFFF or not re.fullmatch(r"[0-9a-fA-F]{64}", str(expected_sha256 or "")):
        raise DeploymentError("缺少原生UEFI启动项的有效编号/所有权摘要。")
    name = "Boot%04X" % number
    absent = False
    try:
        record = _firmware_boot_entry_record(number)
    except DeploymentError as error:
        if not _firmware_variable_is_absent_error(error):
            raise
        absent = True
    if not absent and record["sha256"] != str(expected_sha256).lower():
        raise DeploymentError("%s已被其他启动项复用；拒绝删除不属于本次安装的内容。" % name)
    before = _snapshot_firmware_boot_entries()
    after_order = [value for value in before["order"] if int(value) != number]
    # A fresh snapshot may expose a concurrent replacement after the first read.
    if number in before["entries"] and before["entries"][number]["sha256"] != str(expected_sha256).lower():
        raise DeploymentError("%s在删除前发生变化；未修改UEFI启动项。" % name)
    if after_order != before["order"]:
        if not after_order:
            raise DeploymentError("拒绝删除本次UEFI启动项：操作会使BootOrder变为空。")
        _set_firmware_variable_bytes("BootOrder", EFI_GLOBAL_VARIABLE_GUID,
                                     struct.pack("<%dH" % len(after_order), *after_order))
    if not absent:
        # Check the identity again immediately before deleting the variable.
        latest = _firmware_boot_entry_record(number)
        if latest["sha256"] != str(expected_sha256).lower():
            raise DeploymentError("%s在提交删除前被其他启动项复用，已停止删除。" % name)
        _set_firmware_variable_bytes(name, EFI_GLOBAL_VARIABLE_GUID, None)
    try:
        _firmware_boot_entry_record(number)
    except DeploymentError as error:
        if not _firmware_variable_is_absent_error(error):
            raise
    else:
        raise DeploymentError("删除后仍存在%s；禁用尚未完成。" % name)
    verified_order = _read_firmware_boot_order()
    if verified_order != after_order:
        raise DeploymentError("删除%s后BootOrder复核不一致。" % name)
    others = {"entries": {key: value for key, value in before["entries"].items() if int(key) != number}}
    _verify_native_baseline(others)
    return {"ok": True, "deleted": True, "already_absent": absent, "boot_number": number,
            "removed": [] if absent else [number], "boot_order_before": before["order"],
            "boot_order_after": verified_order}


def _create_native_esp_boot_entry(root, description, target_path, first):
    """Create one clean native EFI_LOAD_OPTION with a reversible transaction."""
    before = None
    number = None
    new_hash = ""
    attempts = []
    write_attempted = False
    try:
        before = _snapshot_firmware_boot_entries()
        template, identity = _native_template_for_esp(root, before)
        new_raw = _efi_clone_load_option_with_filepath(template["raw"], description, target_path)
        # A selected same-ESP application entry may be inactive or have the
        # application category. Our new item is an active boot-category item.
        option_attributes = (struct.unpack_from("<I", new_raw, 0)[0] & ~0x00001F00) | 1
        new_raw = struct.pack("<I", option_attributes) + new_raw[4:]
        new_hash = hashlib.sha256(new_raw).hexdigest()
        number = _find_free_firmware_boot_number(before["order"])
        if _read_firmware_boot_order() != before["order"]:
            raise DeploymentError("UEFI启动顺序在修复前发生变化；请重新执行操作。")
        _verify_native_baseline(before)
        try:
            _firmware_boot_entry_record(number)
        except DeploymentError as read_error:
            if not _firmware_variable_is_absent_error(read_error):
                raise
        else:
            raise DeploymentError("本次分配的Boot%04X在创建前已被占用；未修改该启动项。" % number)
        name = "Boot%04X" % number
        write_attempted = True
        _set_new_firmware_variable_bytes(name, EFI_GLOBAL_VARIABLE_GUID, new_raw, attributes=7)
        attempts.append({"step": "native-create", "returncode": 0, "stdout": name, "stderr": ""})
        created = _firmware_boot_entry_record(number)
        if created["raw"] != new_raw or created["attributes"] != 7:
            raise DeploymentError("原生UEFI启动项写入后的内容/属性复核不一致。")
        wanted_order = ([number] + before["order"]) if first else (before["order"] + [number])
        _set_firmware_variable_bytes("BootOrder", EFI_GLOBAL_VARIABLE_GUID,
                                     struct.pack("<%dH" % len(wanted_order), *wanted_order))
        attempts.append({"step": "native-addfirst" if first else "native-addlast", "returncode": 0,
                         "stdout": name, "stderr": ""})
        final = _snapshot_firmware_boot_entries()
        if final["order"] != wanted_order:
            raise DeploymentError("原生UEFI启动项置顶/顺序复核失败。")
        _verify_native_baseline(before)
        if final["entries"][number]["sha256"] != new_hash or _efi_load_option_optional_data(final["entries"][number]["raw"]):
            raise DeploymentError("原生UEFI启动项最终内容复核失败。")
        return {"ok": True, "guid": "", "boot_number": number, "boot_name": name,
                "boot_sha256": new_hash, "raw_sha256": new_hash, "path": target_path,
                "native_nvram_verified": True, "native_clean": True, "optional_data_length": 0,
                "template_boot_name": template["name"], "esp_partition_guid": identity["partition_guid"],
                "verification": "native-exact-load-option+same-esp-gpt+unchanged-old-entries",
                "attempts": attempts, "cleanup_errors": []}
    except Exception as error:
        cleanup_errors = []
        if write_attempted and number is not None and before is not None:
            try:
                # Remove only the allocated identity. Do not guess ownership of
                # another entry which appeared concurrently under another number.
                try:
                    record = _firmware_boot_entry_record(number)
                except DeploymentError as read_error:
                    if not _firmware_variable_is_absent_error(read_error):
                        raise
                    record = None
                if record is not None and record["sha256"] != new_hash:
                    raise DeploymentError("本次Boot%04X已被其他内容替换；不能安全自动回滚。" % number)
                current_order = _read_firmware_boot_order()
                unknown = [value for value in current_order if value != number and value not in before["entries"]]
                _verify_native_baseline(before)
                if unknown:
                    raise DeploymentError("回滚期间出现未归属本次安装的其他UEFI启动项，已停止重写BootOrder。")
                _set_firmware_variable_bytes("BootOrder", EFI_GLOBAL_VARIABLE_GUID,
                                             struct.pack("<%dH" % len(before["order"]), *before["order"]))
                if record is not None:
                    _set_firmware_variable_bytes("Boot%04X" % number, EFI_GLOBAL_VARIABLE_GUID, None)
                if _read_firmware_boot_order() != before["order"]:
                    raise DeploymentError("回滚后的BootOrder与操作前不一致。")
                try:
                    _firmware_boot_entry_record(number)
                except DeploymentError as read_error:
                    if not _firmware_variable_is_absent_error(read_error):
                        raise
                else:
                    raise DeploymentError("回滚后仍残留本次Boot%04X。" % number)
                _verify_native_baseline(before)
            except Exception as cleanup_error:
                cleanup_errors.append(str(cleanup_error))
        return {"ok": False, "guid": "", "boot_number": number if cleanup_errors else None,
                "boot_sha256": new_hash if cleanup_errors else "",
                "raw_sha256": new_hash if cleanup_errors else "",
                "manual_required": not bool(cleanup_errors), "error": str(error),
                "cleanup_errors": cleanup_errors, "attempts": attempts,
                "rollback_verified": not bool(cleanup_errors)}


def _firmware_boot_snapshot_delta(before_snapshot, after_snapshot):
    """Return new-number and same-number-replaced Boot#### identities."""
    before_entries = dict((before_snapshot or {}).get("entries", {}) or {})
    after_entries = dict((after_snapshot or {}).get("entries", {}) or {})
    introduced = []
    replaced = []
    for number in (after_snapshot or {}).get("order", []) or []:
        number = int(number)
        current = after_entries.get(number)
        previous = before_entries.get(number)
        if previous is None:
            if number not in introduced:
                introduced.append(number)
            continue
        if current is None:
            continue
        if str(current.get("sha256", "")) != str(previous.get("sha256", "")):
            if number not in replaced:
                replaced.append(number)
    return introduced, replaced


def _read_bootcurrent_details():
    """Read the machine's actual BootCurrent and parse its EFI file path."""
    if os.name != "nt" or _firmware_type() != "uefi":
        return {"ok": False, "uefi": False, "error": "当前不是UEFI启动环境。"}
    try:
        raw, _attrs = _get_firmware_variable_bytes(
            "BootCurrent", EFI_GLOBAL_VARIABLE_GUID, max_bytes=64
        )
        if len(raw) < 2:
            raise DeploymentError("UEFI BootCurrent内容过短。")
        number = int(struct.unpack_from("<H", raw, 0)[0])
        name = "Boot%04X" % number
        entry, _entry_attrs = _get_firmware_variable_bytes(
            name, EFI_GLOBAL_VARIABLE_GUID, max_bytes=1024 * 1024
        )
        paths = _efi_load_option_file_paths(entry)
        return {
            "ok": True,
            "uefi": True,
            "number": number,
            "name": name,
            "description": _efi_load_option_description(entry),
            "paths": paths,
        }
    except Exception as exc:
        return {"ok": False, "uefi": True, "error": str(exc), "paths": []}


def _read_bootorder_first_details():
    """Read the real first Boot#### referenced by UEFI BootOrder.

    Unlike BootCurrent this describes the ordinary next-boot priority.  It is
    the most direct view of what the firmware setup actually changed.
    Some Windows/firmware combinations block direct variable access, so callers
    must retain the BCDEdit FIRMWARE snapshot as an independent fallback.
    """
    if os.name != "nt" or _firmware_type() != "uefi":
        return {"ok": False, "uefi": False, "error": "当前不是UEFI启动环境。", "paths": []}
    try:
        order = _read_firmware_boot_order()
        if not order:
            raise DeploymentError("UEFI BootOrder为空。")
        number = int(order[0])
        name = "Boot%04X" % number
        entry, _entry_attrs = _get_firmware_variable_bytes(
            name, EFI_GLOBAL_VARIABLE_GUID, max_bytes=1024 * 1024
        )
        paths = _efi_load_option_file_paths(entry)
        return {
            "ok": True,
            "uefi": True,
            "number": number,
            "name": name,
            "description": _efi_load_option_description(entry),
            "paths": paths,
            "source": "bootorder-native",
            "boot_order": list(order),
        }
    except Exception as exc:
        return {"ok": False, "uefi": True, "error": str(exc), "paths": [], "source": "bootorder-native"}


def _probe_bootcurrent_boot_path(arch=None, allow_bcd_fallback=False):
    r"""Classify the EFI path used by the *ordinary next boot*.

    Signal priority is deliberately:
      1. native BootOrder -> first Boot#### MEDIA_FILEPATH_DP;
      2. optionally, one BCDEdit ``/enum FIRMWARE`` snapshot -> fwbootmgr
         displayorder first object and that object's firmware path;
      3. BootCurrent only as a diagnostic, never as authority for future order.

    ``allow_bcd_fallback=False`` is the strict consent/preflight mode.  Some
    firmware implementations have been observed to materialise/synchronise a
    Windows Boot Manager entry merely while Windows enumerates FIRMWARE.  A
    consent dialog must therefore never execute BCDEdit FIRMWARE first.

    This also avoids the old false-negative where a firmware tool changed the first
    UEFI entry to ``\EFI\Boot\bootx64.efi`` during the current Windows
    session, while BootCurrent still described the bootmgfw path used to enter
    the already-running session.
    """
    expected_primary = r"\efi\microsoft\boot\bootmgfw.efi"
    expected_fallback = (
        r"\efi\boot\bootia32.efi"
        if str(arch or "").lower() == "ia32"
        else r"\efi\boot\bootx64.efi"
    )

    native_order = _read_bootorder_first_details()
    # CRITICAL CONSENT BOUNDARY:
    # Do not even *enumerate* firmware through BCDEdit before the user has had
    # a chance to consent.  Although `/enum FIRMWARE` is documented as an enum
    # operation, real firmware/Windows combinations can synchronise or create a
    # Windows Boot Manager entry as a side effect of that enumeration.  The
    # preflight caller therefore passes allow_bcd_fallback=False.
    if allow_bcd_fallback:
        bcd_primary = _probe_fwbootmgr_primary_bcd_path()
    else:
        bcd_primary = {
            "ok": False, "paths": [], "source": "bcd-fallback-disabled",
            "error": "只读授权前置阶段已禁用BCDEdit FIRMWARE枚举，避免固件同步产生新的Windows Boot Manager。",
        }
    native_current = _read_bootcurrent_details()

    # BootOrder is the actual NVRAM order and therefore wins when readable.
    # Otherwise use the Windows-supported FIRMWARE enumeration only after the
    # consent boundary. BootCurrent is intentionally not allowed to mask a
    # just-edited BootOrder.
    if native_order.get("ok") and native_order.get("paths"):
        chosen = dict(native_order)
    elif allow_bcd_fallback and bcd_primary.get("ok") and bcd_primary.get("paths"):
        chosen = dict(bcd_primary)
    elif native_order.get("ok"):
        chosen = dict(native_order)
    elif allow_bcd_fallback and bcd_primary.get("ok"):
        chosen = dict(bcd_primary)
    else:
        chosen = {
            "ok": False, "uefi": True, "paths": [], "source": "unreadable",
            "error": native_order.get("error") or bcd_primary.get("error") or native_current.get("error", ""),
        }

    details = dict(chosen or {})
    details["expected_primary"] = expected_primary
    details["expected_fallback"] = expected_fallback
    details["bcd_primary_probe"] = bcd_primary
    details["native_bootorder_probe"] = native_order
    details["native_bootcurrent_probe"] = native_current

    normalized = []
    for value in details.get("paths", []):
        text = _normalized_bcd_path(value)
        if text:
            normalized.append(text)
    points_to_bootmgfw = any(
        path == expected_primary or path.endswith(expected_primary)
        for path in normalized
    )
    is_fallback = any(
        path == expected_fallback or path.endswith(expected_fallback)
        for path in normalized
    )
    details["normalized_paths"] = normalized
    details["points_to_bootmgfw"] = bool(points_to_bootmgfw)
    details["is_fallback"] = bool(is_fallback)
    details["needs_repair"] = bool(normalized) and not bool(points_to_bootmgfw)
    details["path_status"] = (
        "primary" if points_to_bootmgfw
        else "bypass" if normalized
        else "no-filepath" if details.get("ok")
        else "unreadable"
    )
    details["ok"] = bool(details.get("ok"))
    return details


def _efi_x509_thumbprints(signature_database):
    """Parse EFI_SIGNATURE_LIST records and return SHA-1s of X.509 entries."""
    data = bytes(signature_database or b"")
    x509_type = uuid.UUID(EFI_CERT_X509_GUID).bytes_le
    result = set()
    offset = 0
    while offset + 28 <= len(data):
        sig_type = data[offset:offset + 16]
        list_size, header_size, sig_size = struct.unpack_from("<III", data, offset + 16)
        if list_size < 28 or sig_size < 16 or offset + list_size > len(data):
            break
        payload_start = offset + 28 + int(header_size)
        payload_end = offset + int(list_size)
        if payload_start > payload_end:
            break
        if sig_type == x509_type:
            cursor = payload_start
            while cursor + sig_size <= payload_end:
                cert_der = data[cursor + 16:cursor + sig_size]
                if cert_der:
                    result.add(hashlib.sha1(cert_der).hexdigest().upper())
                cursor += sig_size
        offset += int(list_size)
    return result


def _firmware_db_x509_thumbprints():
    # Reading the variable and finding no supported X.509 thumbprints are two
    # different states.  The caller needs that distinction so it can tell the
    # user whether the db itself could not be read, or was read successfully
    # but simply does not contain either supported Microsoft CA.
    raw, _attrs = _get_firmware_variable_bytes("db", EFI_IMAGE_SECURITY_DATABASE_GUID)
    return _efi_x509_thumbprints(raw)


def _secure_boot_enabled():
    """Return True/False when Windows can determine Secure Boot state, else None."""
    if os.name != "nt" or _firmware_type() != "uefi":
        return False
    if winreg is not None:
        try:
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\SecureBoot\State",
            )
            try:
                return bool(int(winreg.QueryValueEx(key, "UEFISecureBootEnabled")[0]))
            finally:
                winreg.CloseKey(key)
        except Exception:
            pass
    try:
        raw, _attrs = _get_firmware_variable_bytes("SecureBoot", EFI_GLOBAL_VARIABLE_GUID, max_bytes=4096)
        if raw:
            return bool(raw[0])
    except Exception:
        pass
    return None


def _mount_system_esp():
    if os.name != "nt" or _firmware_type() != "uefi":
        raise DeploymentError("当前不是可处理的Windows UEFI环境。")

    # Never infer the ESP from arbitrary drive letters.  Ask Windows for the
    # system ESP first, then independently validate what was mounted before any
    # boot-file write can happen.
    mountvol = _mountvol_path()
    diagnostics = []
    letters = _free_drive_letters()
    if letters:
        mount_point = letters[0] + ":"
        root = mount_point + "\\"
        proc = subprocess.run(
            [mountvol, mount_point, "/S"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False, timeout=20,
        )
        out = _decode_console_output(proc.stdout).strip()
        err = _decode_console_output(proc.stderr).strip()
        if proc.returncode == 0:
            info = {
                "root": root, "temporary": True, "mountvol": mountvol,
                "mount_point": mount_point, "source": "mountvol-/S",
                "boot_mode": "uefi",
            }
            inspected = _inspect_windows_esp_root(root)
            if inspected["ok"]:
                info["filesystem"] = inspected.get("filesystem", "")
                return info
            diagnostics.append(
                "%s：mountvol /S已挂载，但严格ESP核对未通过：%s"
                % (mount_point, "；".join(inspected["reasons"]))
            )
            _unmount_root(info)
        else:
            diagnostics.append(
                "%s：mountvol /S失败，返回码%d，%s"
                % (mount_point, int(proc.returncode), err or out or "系统未返回文字信息")
            )

    # Only as a fallback, accept an ESP that was already assigned a drive
    # letter before AnimeBootNeo started.  It must pass the exact same strict
    # FAT/Microsoft-boot identity check.  No hidden recovery/OEM scan here.
    mounted_candidates = []
    for existing_root in _drive_roots():
        inspected = _inspect_windows_esp_root(existing_root)
        if inspected["ok"]:
            mounted_candidates.append((existing_root, inspected))
    if len(mounted_candidates) == 1:
        existing_root, inspected = mounted_candidates[0]
        return {
            "root": existing_root, "temporary": False,
            "mountvol": mountvol, "source": "strict-existing-mounted-esp",
            "boot_mode": "uefi", "filesystem": inspected.get("filesystem", ""),
        }
    if len(mounted_candidates) > 1:
        diagnostics.append(
            "发现多个同时满足严格条件的已挂载ESP，拒绝猜测：%s"
            % ", ".join(item[0] for item in mounted_candidates)
        )

    raise DeploymentError(
        "无法可靠定位Windows EFI系统分区，未对任何Recovery/OEM分区执行启动文件写入。\n"
        "mountvol：%s\n诊断信息：\n%s"
        % (mountvol, _format_diagnostics(diagnostics))
    )


def _volume_disk_extent(root):
    """Return one-volume disk extent used to map a drive letter to an MBR partition."""
    if os.name != "nt":
        return None
    from ctypes import wintypes
    kernel32 = _windows_kernel32()
    INVALID = ctypes.c_void_p(-1).value
    class EXT(ctypes.Structure):
        _fields_ = [("DiskNumber", wintypes.DWORD), ("StartingOffset", ctypes.c_longlong), ("ExtentLength", ctypes.c_longlong)]
    class EXTS(ctypes.Structure):
        _fields_ = [("NumberOfDiskExtents", wintypes.DWORD), ("Extents", EXT * 1)]
    handle = kernel32.CreateFileW(_raw_volume_path(root), 0x80000000, 0x3, None, 3, 0, None)
    if handle in (None, 0, INVALID):
        return None
    try:
        out = EXTS(); returned = wintypes.DWORD(0)
        if not kernel32.DeviceIoControl(handle, 0x00560000, None, 0, ctypes.byref(out), ctypes.sizeof(out), ctypes.byref(returned), None):
            return None
        if int(out.NumberOfDiskExtents) != 1:
            return None
        ext = out.Extents[0]
        return {"disk_number": int(ext.DiskNumber), "starting_offset": int(ext.StartingOffset), "extent_length": int(ext.ExtentLength)}
    finally:
        kernel32.CloseHandle(handle)


def _read_mbr_sector_for_disk(disk_number):
    if os.name != "nt":
        raise DeploymentError("Legacy MBR识别只能在Windows中执行。")
    from ctypes import wintypes
    kernel32 = _windows_kernel32(); INVALID = ctypes.c_void_p(-1).value
    path = r"\\.\PhysicalDrive%d" % int(disk_number)
    handle = kernel32.CreateFileW(path, 0x80000000, 0x3, None, 3, 0, None)
    if handle in (None, 0, INVALID):
        raise DeploymentError("无法只读打开%s识别活动分区（错误%d）。" % (path, ctypes.get_last_error()))
    try:
        buf = ctypes.create_string_buffer(512); got = wintypes.DWORD(0)
        if not kernel32.ReadFile(handle, buf, 512, ctypes.byref(got), None) or int(got.value) != 512:
            raise DeploymentError("无法读取%s的MBR扇区。" % path)
        return buf.raw[:512]
    finally:
        kernel32.CloseHandle(handle)


def _bios_volume_record(root, source, volume_name=""):
    extent = _volume_disk_extent(root)
    if not extent:
        return None
    animebootneo = os.path.join(root, "AnimeBootNeo")
    rec = {
        "root": root, "source": source, "volume_name": volume_name,
        "temporary": bool(volume_name), "boot_mode": "bios",
        "disk_number": extent["disk_number"], "starting_offset": extent["starting_offset"],
        "has_bootmgr": os.path.isfile(os.path.join(root, "bootmgr")),
        "has_bcd": os.path.isfile(os.path.join(root, "Boot", "BCD")),
        "has_animebootneo": os.path.isdir(animebootneo),
        "has_cfg": bool(_find_grub_cfgs(root)),
    }
    return rec


def _collect_bios_volume_records():
    """Inspect visible and hidden volumes read-only; hidden volumes are unmounted after inspection."""
    records = []
    seen = set()
    for drive_root in _drive_roots():
        rec = _bios_volume_record(drive_root, "visible-volume")
        if rec:
            records.append(rec); seen.add((rec["disk_number"], rec["starting_offset"]))
    mountvol = _mountvol_path()
    for volume_name in _enumerate_volume_names():
        if _volume_mount_paths(volume_name):
            continue
        letters = _free_drive_letters()
        if not letters:
            break
        letter = letters[0]
        ok, mounted_root, diagnostic = _try_mount_volume(volume_name, letter, mountvol)
        if not ok:
            continue
        info = {"root": mounted_root, "temporary": True, "mountvol": mountvol, "mount_point": letter+":", "volume_name": volume_name}
        try:
            rec = _bios_volume_record(mounted_root, "hidden-volume", volume_name)
            if rec and (rec["disk_number"], rec["starting_offset"]) not in seen:
                records.append(rec); seen.add((rec["disk_number"], rec["starting_offset"]))
        finally:
            _unmount_root(info)
    return records


def _active_partition_offsets(records):
    result = {}
    errors = []
    for disk_number in sorted(set(int(r["disk_number"]) for r in records)):
        try:
            layout = _legacy_mbr_layout(_read_mbr_sector_for_disk(disk_number))
            result[disk_number] = int(layout["active_partition_lba"]) * 512
        except Exception as exc:
            errors.append("PhysicalDrive%d：%s" % (disk_number, exc))
    return result, errors


def _select_bios_boot_record(records, active_offsets, require_animebootneo=False):
    active = []
    for rec in records:
        if active_offsets.get(int(rec["disk_number"])) != int(rec["starting_offset"]):
            continue
        if not (rec.get("has_bootmgr") and rec.get("has_bcd")):
            continue
        if require_animebootneo and not (rec.get("has_animebootneo") and rec.get("has_cfg")):
            continue
        active.append(rec)
    if len(active) == 1:
        return active[0]
    if len(active) > 1:
        raise DeploymentError("检测到多个可作为当前Legacy活动启动分区的卷，拒绝猜测：%s" % ", ".join(r["root"] for r in active))
    if require_animebootneo:
        # The independent GRUB core can search partitions. If the active
        # partition itself has no AnimeBootNeo tree, allow one and only one
        # complete AnimeBootNeo root on a disk
        # whose MBR identity was successfully validated.
        managed = [r for r in records if int(r["disk_number"]) in active_offsets and r.get("has_animebootneo") and r.get("has_cfg")]
        if len(managed) == 1:
            return managed[0]
        if len(managed) > 1:
            raise DeploymentError("同一Legacy环境发现多个完整AnimeBoot Neo Debug启动文件卷，拒绝猜测实际生效位置：%s" % ", ".join(r["root"] for r in managed))
    return None


def _remount_bios_record(rec):
    if not rec.get("volume_name"):
        return {"root": rec["root"], "temporary": False, "source": rec.get("source", "visible-volume"), "boot_mode": "bios",
                "disk_number": rec.get("disk_number"), "starting_offset": rec.get("starting_offset")}
    letters = _free_drive_letters()
    if not letters:
        raise DeploymentError("没有可用盘符挂载已确认的Legacy活动启动分区。")
    mountvol = _mountvol_path(); letter = letters[0]
    ok, mounted_root, diagnostic = _try_mount_volume(rec["volume_name"], letter, mountvol)
    if not ok:
        raise DeploymentError("已确认Legacy启动卷，但重新挂载失败：%s" % diagnostic)
    extent = _volume_disk_extent(mounted_root)
    if not extent or int(extent["disk_number"]) != int(rec["disk_number"]) or int(extent["starting_offset"]) != int(rec["starting_offset"]):
        info={"root":mounted_root,"temporary":True,"mountvol":mountvol,"mount_point":letter+":"}
        _unmount_root(info)
        raise DeploymentError("Legacy启动卷重新挂载后的物理区段与识别结果不一致，拒绝继续。")
    return {"root": mounted_root, "temporary": True, "mountvol": mountvol, "mount_point": letter+":", "volume_name": rec["volume_name"],
            "source": "legacy-active-partition" if rec.get("source") == "hidden-volume" else rec.get("source"), "boot_mode": "bios",
            "disk_number": rec.get("disk_number"), "starting_offset": rec.get("starting_offset")}


def _find_bios_system_volume(require_animebootneo=False):
    records = _collect_bios_volume_records()
    active_offsets, errors = _active_partition_offsets(records)
    selected = _select_bios_boot_record(records, active_offsets, require_animebootneo=require_animebootneo)
    if selected:
        return _remount_bios_record(selected)
    detail = _format_diagnostics(errors) if "_format_diagnostics" in globals() else str(errors)
    if require_animebootneo:
        raise DeploymentError("没有在当前Legacy活动启动分区或唯一受管 AnimeBoot Neo Debug 启动卷中找到完整启动文件。\n诊断信息：\n%s" % detail)
    raise DeploymentError("没有找到与当前MBR活动分区物理区段一致的Legacy Windows启动卷（bootmgr + Boot\\BCD）。\n诊断信息：\n%s" % detail)


def _normalized_bcd_path(value):
    return str(value or "").replace("/", "\\").strip().lower()


def _bcd_entry_has_path(guid, expected_path):
    """Verify the path postcondition instead of trusting BCDEdit rc alone."""
    if not guid:
        return False
    rc, out, err = _run_bcdedit(["/enum", guid, "/v"], allow_failure=True, store_path=None)
    text = _normalized_bcd_path(out + "\n" + err)
    return _normalized_bcd_path(expected_path) in text


def _extract_efi_paths_from_bcd_text(text):
    """Extract EFI executable paths from BCDEdit output without locale labels."""
    found = []
    for match in re.finditer(r"(?i)\\EFI\\[^\r\n\t]+?\.efi", str(text or "")):
        value = match.group(0).strip().rstrip(" ;,）)")
        if value and value not in found:
            found.append(value)
    return found


def _bcd_object_blocks(text):
    """Return (object_identifier, block_text) pairs from BCDEdit output.

    The first brace token in each blank-line-delimited object block is the
    object's identifier.  This avoids depending on localized labels such as
    ``identifier``/``标识符``.
    """
    token_re = re.compile(r"\{(?:fwbootmgr|bootmgr|[0-9A-Fa-f-]{36})\}", re.I)
    result = []
    for block in re.split(r"(?:\r?\n){2,}", str(text or "")):
        block = block.strip()
        if not block:
            continue
        tokens = token_re.findall(block)
        if tokens:
            result.append((tokens[0].lower(), block))
    return result


def _read_fwbootmgr_displayorder_bcd():
    """Read BootOrder through one BCDEdit FIRMWARE snapshot.

    ``/enum {fwbootmgr}`` followed by ``/enum {bootmgr}`` is not sufficient:
    the latter can describe the Windows BCD boot-manager object rather than the
    firmware Boot#### filepath just edited by another firmware tool. ``/enum FIRMWARE``
    enumerates the firmware applications and lets us resolve order and filepath
    from the same snapshot.
    """
    if os.name != "nt" or _firmware_type() != "uefi":
        return {"ok": False, "error": "当前不是Windows UEFI环境。", "order": []}
    try:
        rc, out, err = _run_bcdedit(["/enum", "FIRMWARE"], allow_failure=True, store_path=None)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "order": []}
    if rc != 0:
        return {"ok": False, "error": err or out or "BCDEdit读取FIRMWARE失败。", "order": []}

    token_re = re.compile(r"\{(?:bootmgr|[0-9A-Fa-f-]{36})\}", re.I)
    fw_block = ""
    for object_id, block in _bcd_object_blocks(out):
        if object_id == "{fwbootmgr}":
            fw_block = block
            break
    if not fw_block:
        # Compatibility fallback for unusual formatting with no blank line.
        fw_block = str(out or "")

    lines = fw_block.splitlines()
    order = []
    collecting = False
    for line in lines:
        lowered = line.lower()
        is_displayorder = (
            "displayorder" in lowered
            or "显示顺序" in line
            or "显示次序" in line
        )
        if is_displayorder:
            collecting = True
            order.extend(token_re.findall(line))
            continue
        if collecting:
            stripped = line.strip()
            tokens = token_re.findall(line)
            if tokens and stripped.lower() in tuple(t.lower() for t in tokens):
                order.extend(tokens)
                continue
            break

    dedup = []
    for value in order:
        value = value.strip()
        if value and value.lower() not in [x.lower() for x in dedup]:
            dedup.append(value)
    if not dedup:
        return {
            "ok": False, "error": "BCDEdit已返回FIRMWARE，但没有解析出fwbootmgr displayorder。",
            "order": [], "stdout": out,
        }
    return {"ok": True, "order": dedup, "first": dedup[0], "stdout": out}


def _probe_fwbootmgr_primary_bcd_path():
    """Resolve the real firmware first entry path from BCDEdit FIRMWARE."""
    order = _read_fwbootmgr_displayorder_bcd()
    if not order.get("ok"):
        return {"ok": False, "error": order.get("error", ""), "paths": [], "source": "fwbootmgr-bcd-firmware"}
    first = str(order.get("first", "") or "").strip()
    if not first:
        return {"ok": False, "error": "{fwbootmgr}第一启动项为空。", "paths": [], "source": "fwbootmgr-bcd-firmware"}

    snapshot = str(order.get("stdout", "") or "")
    target = first.lower()
    block = ""
    for object_id, candidate in _bcd_object_blocks(snapshot):
        if object_id == target:
            block = candidate
            break
    paths = _extract_efi_paths_from_bcd_text(block) if block else []

    # Some firmware/BCDEdit builds format a single object oddly.  Keep the
    # direct object query only as a fallback, never as the primary source.
    direct_raw = ""
    if not paths and target != "{bootmgr}":
        rc, out, err = _run_bcdedit(["/enum", first, "/v"], allow_failure=True, store_path=None)
        if rc == 0:
            direct_raw = out + "\n" + err
            paths = _extract_efi_paths_from_bcd_text(direct_raw)

    return {
        "ok": True, "identifier": first, "paths": paths, "description": "",
        "source": "fwbootmgr-bcd-firmware", "displayorder": list(order.get("order", [])),
        "raw": block or direct_raw, "firmware_snapshot": snapshot,
    }


def _create_animebootneo_firmware_entry_details(arch, root=None):
    """Create a clean same-ESP native rescue item, without BCDEdit side effects."""
    if arch != "x64":
        raise DeploymentError("当前 Fedora 签名启动链仅支持 x64 UEFI。")
    result = _create_native_esp_boot_entry(
        root, "Boot Windows created by AnimeBoot Neo Debug",
        r"\EFI\Microsoft\Boot\bootmgfw.efi_ANBAK", first=False)
    result.update(skipped=not bool(result.get("ok")),
                  warning="" if result.get("ok") else "无法安全创建可选灾备启动项，主启动链仍可安装：" + result.get("error", ""),
                  new_bcd_guids=[],
                  new_boot_numbers=[result["boot_number"]] if result.get("boot_number") is not None else [],
                  creation_count_verified=True,
                  compatibility_device="native-same-esp-gpt-device-path")
    return result


def _rollback_new_firmware_numbers(before_order, new_numbers):
    """Rollback only Boot#### values proven not to exist before this operation."""
    errors = []
    try:
        packed = struct.pack("<%dH" % len(before_order), *before_order) if before_order else b""
        if packed:
            _set_firmware_variable_bytes("BootOrder", EFI_GLOBAL_VARIABLE_GUID, packed)
    except Exception as exc:
        errors.append("恢复原BootOrder失败：%s" % exc)
    for number in sorted(set(int(v) for v in (new_numbers or []))):
        try:
            _set_firmware_variable_bytes("Boot%04X" % number, EFI_GLOBAL_VARIABLE_GUID, None)
        except Exception as exc:
            errors.append("删除本次新建Boot%04X失败：%s" % (number, exc))
    return errors


def _remove_exact_firmware_boot_numbers(numbers):
    """Remove only the exact Boot#### numbers supplied, preserving everything else.

    This helper is intentionally native-NVRAM only.  Real-machine testing found
    that some Windows/firmware combinations can materialise an additional
    ``Windows Boot Manager`` while BCDEdit is touching the live firmware store.
    Once a Boot#### number is proven to have appeared *during our transaction*,
    removing that exact number is safer than any description/GUID guesswork.
    """
    targets = sorted(set(int(v) for v in (numbers or [])))
    if not targets:
        return {"removed": [], "boot_order_before": [], "boot_order_after": []}
    current = _read_firmware_boot_order()
    target_set = set(targets)
    new_order = [number for number in current if number not in target_set]
    if len(new_order) == 0 and current:
        raise DeploymentError("拒绝删除本次异常Boot####：操作会使UEFI BootOrder变为空。")
    if new_order != current:
        packed = struct.pack("<%dH" % len(new_order), *new_order) if new_order else b""
        _set_firmware_variable_bytes("BootOrder", EFI_GLOBAL_VARIABLE_GUID, packed)
    removed = []
    errors = []
    for number in targets:
        try:
            _set_firmware_variable_bytes("Boot%04X" % number, EFI_GLOBAL_VARIABLE_GUID, None)
            removed.append(number)
        except Exception as exc:
            errors.append("删除Boot%04X失败：%s" % (number, exc))
    try:
        verified = _read_firmware_boot_order()
    except Exception as exc:
        errors.append("删除后复核BootOrder失败：%s" % exc)
        verified = []
    residue = [number for number in verified if number in target_set]
    if residue:
        errors.append("BootOrder仍引用：%s" % ", ".join("Boot%04X" % v for v in residue))
    if errors:
        raise DeploymentError("本次额外生成的UEFI启动项未能完全清理：%s" % "; ".join(errors))
    return {
        "removed": removed,
        "boot_order_before": current,
        "boot_order_after": verified,
    }


def _cleanup_unexpected_new_firmware_boot_options(before_snapshot, allowed_new_numbers=None, phase="install"):
    """Remove every unsolicited Boot#### identity created/replaced in this phase.

    A Boot#### *number* is not an identity.  The same number may be freed by a
    legitimate delete and then reused immediately for a new Windows Boot
    Manager.  Production callers therefore pass a full snapshot from
    ``_snapshot_firmware_boot_entries``; we compare SHA-256 fingerprints of the
    complete EFI_LOAD_OPTION bytes in addition to number membership.

    ``allowed_new_numbers`` contains only exact Boot#### numbers returned by
    AnimeBootNeo's intended creations (the optional AnimeBootNeo rescue entry and,
    after explicit consent only, the repair WBM).  Everything else that is new
    *or has replaced the previous identity under the same number* is removed by
    exact native Boot#### number.
    """
    if not isinstance(before_snapshot,dict) or not isinstance(before_snapshot.get('entries'),dict):
        raise DeploymentError('UEFI 清理必须提供完整启动项身份快照。')
    baseline=before_snapshot

    allowed = set(int(v) for v in (allowed_new_numbers or []) if v is not None)
    after_snapshot = _snapshot_firmware_boot_entries()
    after = list(after_snapshot.get("order", []) or [])

    introduced, replaced = _firmware_boot_snapshot_delta(baseline, after_snapshot)

    observed_changed = []
    for number in list(introduced) + list(replaced):
        number = int(number)
        if number not in observed_changed:
            observed_changed.append(number)
    unexpected = [number for number in observed_changed if number not in allowed]

    details = []
    before_entries = dict(baseline.get("entries", {}) or {})
    after_entries = dict(after_snapshot.get("entries", {}) or {})
    for number in unexpected:
        current = after_entries.get(number, {}) or {}
        previous = before_entries.get(number, {}) or {}
        item = {
            "number": number,
            "name": "Boot%04X" % number,
            "description": str(current.get("description", "") or ""),
            "paths": list(current.get("paths", []) or []),
            "change_kind": "same-number-reused" if number in replaced else "new-number",
            "before_sha256": str(previous.get("sha256", "") or ""),
            "after_sha256": str(current.get("sha256", "") or ""),
            "before_description": str(previous.get("description", "") or ""),
            "before_paths": list(previous.get("paths", []) or []),
        }
        details.append(item)

    cleanup = {"removed": [], "boot_order_before": after, "boot_order_after": after}
    if unexpected:
        cleanup = _remove_exact_firmware_boot_numbers(unexpected)
    return {
        "phase": str(phase or "install"),
        "identity_snapshot": True,
        "baseline_order": list(baseline.get("order", []) or []),
        "observed_order": after,
        "allowed_new_numbers": sorted(allowed),
        "observed_new_numbers": list(introduced),
        "observed_reused_numbers": list(replaced),
        "unexpected_new_numbers": unexpected,
        "unexpected_reused_numbers": [n for n in unexpected if n in replaced],
        "unexpected_details": details,
        "cleaned_numbers": list(cleanup.get("removed", [])),
        "final_order": list(cleanup.get("boot_order_after", after)),
    }


def _bcd_partition_device_for_esp_root(root):
    drive_match = re.match(r"^[A-Za-z]:", str(root or ""))
    if not drive_match:
        raise DeploymentError("无法取得当前严格识别ESP的临时盘符。")
    return "partition=%s" % drive_match.group(0)


def _create_repair_windows_boot_manager(root, expected_bootmgfw_bytes):
    """Repair the first boot path through one verified native EFI_LOAD_OPTION.

    BCDEdit /copy and firmware enumeration are deliberately absent. Some real
    Windows/firmware pairs materialise a second WBM during those operations.
    A matching GPT ESP device path is required before any NVRAM mutation.
    """
    target_path = r"\EFI\Microsoft\Boot\bootmgfw.efi"
    msboot_file = os.path.join(root, "EFI", "Microsoft", "Boot", "bootmgfw.efi")
    expected = bytes(expected_bootmgfw_bytes or b"")
    if not expected or not os.path.isfile(msboot_file):
        raise DeploymentError("无法复核要置顶的EFI\\Microsoft\\Boot\\bootmgfw.efi。")
    if _read_bytes(msboot_file) != expected:
        raise DeploymentError("拒绝创建修复启动项：bootmgfw.efi不是本次刚写入的AnimeBoot Neo Debug启动文件。")
    return _create_native_esp_boot_entry(root, "Windows Boot Manager", target_path, first=True)


def _physical_disk_for_volume(volume_root):
    from ctypes import wintypes
    kernel32 = _windows_kernel32(); INVALID = ctypes.c_void_p(-1).value
    class EXT(ctypes.Structure):
        _fields_ = [("DiskNumber", wintypes.DWORD), ("StartingOffset", ctypes.c_longlong), ("ExtentLength", ctypes.c_longlong)]
    class EXTS(ctypes.Structure):
        _fields_ = [("NumberOfDiskExtents", wintypes.DWORD), ("Extents", EXT * 1)]
    handle = kernel32.CreateFileW(_raw_volume_path(volume_root), 0x80000000, 0x3, None, 3, 0, None)
    if handle in (None, 0, INVALID):
        raise DeploymentError("无法打开Legacy系统启动卷。")
    try:
        out = EXTS(); returned = wintypes.DWORD(0)
        if not kernel32.DeviceIoControl(handle, 0x00560000, None, 0, ctypes.byref(out), ctypes.sizeof(out), ctypes.byref(returned), None):
            raise DeploymentError("无法取得Legacy启动卷所属物理磁盘。")
        if int(out.NumberOfDiskExtents) != 1:
            raise DeploymentError("Legacy启动卷跨多个物理磁盘，拒绝安装。")
        return r"\\.\PhysicalDrive%d" % int(out.Extents[0].DiskNumber)
    finally:
        kernel32.CloseHandle(handle)


def _read_physical_bytes(disk_path, offset, size):
    from ctypes import wintypes
    kernel32 = _windows_kernel32(); INVALID = ctypes.c_void_p(-1).value
    handle = kernel32.CreateFileW(disk_path, 0x80000000, 0x3, None, 3, 0, None)
    if handle in (None, 0, INVALID):
        raise DeploymentError("无法读取%s。" % disk_path)
    try:
        pos = ctypes.c_longlong(0)
        if not kernel32.SetFilePointerEx(handle, ctypes.c_longlong(int(offset)), ctypes.byref(pos), 0):
            raise DeploymentError("定位物理磁盘失败。")
        buf = ctypes.create_string_buffer(size); got = wintypes.DWORD(0)
        if not kernel32.ReadFile(handle, buf, size, ctypes.byref(got), None) or int(got.value) != size:
            raise DeploymentError("读取物理磁盘失败。")
        return buf.raw[:size]
    finally:
        kernel32.CloseHandle(handle)


def _native_system_executable(name):
    """Return a native Windows system executable even from 32-bit Python."""
    if os.name != "nt":
        return name
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    sysnative = os.path.join(system_root, "Sysnative", name)
    if os.path.isfile(sysnative):
        return sysnative
    system32 = os.path.join(system_root, "System32", name)
    if os.path.isfile(system32):
        return system32
    return name


def _decode_console_output(data):
    raw = bytes(data or b"")
    for enc in ("utf-8", "mbcs", "gbk", "cp936", "utf-16le"):
        try:
            return raw.decode(enc)
        except Exception:
            pass
    return raw.decode("latin1", "replace")


def _parse_standalone_boot_number(value):
    """Parse state values such as Boot0015, 0015 or 21 into one Boot#### number."""
    value = str(value or "").strip()
    if not value:
        return None
    match = re.fullmatch(r"(?i)Boot([0-9A-F]{4})", value)
    if match:
        return int(match.group(1), 16)
    if re.fullmatch(r"[0-9A-Fa-f]{4}", value):
        return int(value, 16)
    if value.isdigit():
        number = int(value, 10)
        return number if 0 <= number <= 0xFFFF else None
    return None


def _boot_option_contains_bcd_guid(raw, guid):
    """Return True when a Windows Boot#### optional-data BCDOBJECT matches GUID."""
    token = str(guid or "").strip().lower()
    if not token:
        return False
    data = bytes(raw or b"")
    # Windows firmware entries normally encode BCDOBJECT={GUID} in UTF-16LE.
    # Check the exact GUID in both UTF-16LE and ASCII for compatibility with
    # vendor/firmware variants while remaining ownership-exact.
    bare = token.strip("{}")
    probes = [
        token.encode("ascii", "ignore"), token.encode("utf-16le"),
        bare.encode("ascii", "ignore"), bare.encode("utf-16le"),
    ]
    lowered = data.lower()
    return any(probe and probe.lower() in lowered for probe in probes)


def _find_firmware_boot_numbers_for_bcd_guid(guid, saved_number=None):
    """Resolve an exact BCD GUID to native Boot#### numbers without name guessing."""
    if os.name != "nt" or _firmware_type() != "uefi" or not guid:
        return []
    order = _read_firmware_boot_order()
    candidates = list(order)
    parsed_saved = _parse_standalone_boot_number(saved_number)
    if parsed_saved is not None and parsed_saved not in candidates:
        candidates.append(parsed_saved)
    matches = []
    for number in candidates:
        try:
            raw, _attrs = _get_firmware_variable_bytes(
                "Boot%04X" % int(number), EFI_GLOBAL_VARIABLE_GUID, max_bytes=1024 * 1024
            )
        except Exception:
            continue
        if _boot_option_contains_bcd_guid(raw, guid):
            matches.append(int(number))
    return sorted(set(matches))


def _delete_managed_repair_wbm_exact(guid, saved_boot_number=None):
    """Delete our repair WBM by exact GUID and its exact native Boot#### mapping.

    BCDEdit can report the BCD object gone while some firmware leaves the
    Boot#### variable behind.  Resolve the exact GUID->Boot#### relationship
    before deletion, then remove both layers and verify both postconditions.
    """
    guid = str(guid or "").strip()
    if not guid:
        return {"guid": "", "deleted": True, "boot_numbers": [], "native_removed": []}

    exact_numbers = _find_firmware_boot_numbers_for_bcd_guid(guid, saved_boot_number)
    bcd_cleanup = _delete_bcd_guid_robust(guid, store_path=None)
    native_cleanup = {"removed": [], "boot_order_before": [], "boot_order_after": []}
    if exact_numbers:
        native_cleanup = _remove_exact_firmware_boot_numbers(exact_numbers)

    bcd_present = _bcd_guid_is_present(guid, store_path=None)
    native_residue = _find_firmware_boot_numbers_for_bcd_guid(guid, saved_boot_number)
    if bcd_present or native_residue:
        raise DeploymentError(
            "无法完整删除本程序创建的Windows Boot Manager修复项；"
            "为避免误删机器原有同名项，本次禁用停止。GUID=%s；残留Boot####=%s"
            % (guid, ", ".join("Boot%04X" % v for v in native_residue) or "无")
        )
    if not bcd_cleanup.get("deleted"):
        raise DeploymentError(
            "无法按安装状态中记录的精确GUID删除本程序创建的Windows Boot Manager修复项；GUID=%s"
            % guid
        )
    return {
        "guid": guid,
        "deleted": True,
        "boot_numbers": exact_numbers,
        "native_removed": list(native_cleanup.get("removed", []) or []),
        "bcd_cleanup": bcd_cleanup,
        "native_cleanup": native_cleanup,
    }


def _write_physical_sector(disk_path, offset, data):
    if os.name != "nt":
        raise DeploymentError("物理磁盘恢复只支持Windows。")
    from ctypes import wintypes
    kernel32 = _windows_kernel32()
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    GENERIC_READ = 0x80000000; GENERIC_WRITE = 0x40000000
    FILE_SHARE_READ = 0x1; FILE_SHARE_WRITE = 0x2
    OPEN_EXISTING = 3; FILE_BEGIN = 0
    FILE_FLAG_WRITE_THROUGH = 0x80000000
    handle = kernel32.CreateFileW(
        disk_path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
        None, OPEN_EXISTING, FILE_FLAG_WRITE_THROUGH, None,
    )
    if handle in (None, 0, INVALID_HANDLE_VALUE):
        raise DeploymentError("无法打开物理磁盘%s进行官方卸载回滚（错误%d）。" % (disk_path, ctypes.get_last_error()))
    try:
        pos = ctypes.c_longlong(0)
        if not kernel32.SetFilePointerEx(handle, ctypes.c_longlong(int(offset)), ctypes.byref(pos), FILE_BEGIN):
            raise DeploymentError("定位物理磁盘%s偏移%d失败。" % (disk_path, int(offset)))
        buf = ctypes.create_string_buffer(bytes(data), len(data)); wrote = wintypes.DWORD(0)
        if not kernel32.WriteFile(handle, buf, len(data), ctypes.byref(wrote), None) or int(wrote.value) != len(data):
            raise DeploymentError("写回物理磁盘%s偏移%d失败（错误%d）。" % (disk_path, int(offset), ctypes.get_last_error()))
        _flush_physical_disk_compat(kernel32, handle, disk_path)
        # Read-back is authoritative when an older raw-disk driver does not
        # implement FlushFileBuffers for PhysicalDrive handles.
        if not kernel32.SetFilePointerEx(handle, ctypes.c_longlong(int(offset)), ctypes.byref(pos), FILE_BEGIN):
            raise DeploymentError("回读定位物理磁盘失败。")
        out = ctypes.create_string_buffer(len(data)); got = wintypes.DWORD(0)
        if not kernel32.ReadFile(handle, out, len(data), ctypes.byref(got), None) or int(got.value) != len(data):
            raise DeploymentError("回读物理磁盘%s失败。" % disk_path)
        if out.raw[:len(data)] != bytes(data):
            raise DeploymentError("物理磁盘恢复后逐字节校验失败：%s offset=%d" % (disk_path, int(offset)))
    finally:
        kernel32.CloseHandle(handle)


def _drive_roots():
    if os.name != "nt":
        return []
    roots = []
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    for index, letter in enumerate(string.ascii_uppercase):
        if mask & (1 << index):
            roots.append(letter + ":\\")
    return roots


def _firmware_type_via_modern_api():
    """Use Windows 8+ GetFirmwareType when the API is available."""
    try:
        value = ctypes.c_uint(0)
        getter = ctypes.windll.kernel32.GetFirmwareType
        getter.argtypes = [ctypes.POINTER(ctypes.c_uint)]
        getter.restype = ctypes.c_int
        if getter(ctypes.byref(value)):
            if value.value == 1:
                return "bios"
            if value.value == 2:
                return "uefi"
    except Exception:
        pass
    return "unknown"


def _firmware_type_via_environment_api():
    """Windows 7-compatible firmware-mode probe.

    A dummy GetFirmwareEnvironmentVariableW call returns
    ERROR_INVALID_FUNCTION (1) when Windows itself booted through Legacy BIOS.
    On UEFI it reaches the firmware-variable layer and fails with another error
    because the dummy namespace does not exist.  Only classify the latter as
    UEFI when SeSystemEnvironmentPrivilege was successfully enabled.
    """
    if os.name != "nt":
        return "unknown"
    privilege_enabled = _enable_system_environment_privilege()
    try:
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        getter = getattr(kernel32, "GetFirmwareEnvironmentVariableW", None)
        if getter is None:
            return "unknown"
        getter.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR,
                           wintypes.LPVOID, wintypes.DWORD]
        getter.restype = wintypes.DWORD
        dummy = ctypes.c_ubyte(0)
        ctypes.set_last_error(0)
        got = int(getter(
            "", "{00000000-0000-0000-0000-000000000000}",
            ctypes.byref(dummy), 1,
        ))
        if got > 0:
            return "uefi"
        error = int(ctypes.get_last_error())
        if error == 1:  # ERROR_INVALID_FUNCTION
            return "bios"
        if privilege_enabled and error:
            return "uefi"
    except Exception:
        pass
    return "unknown"


def _firmware_type():
    """Return ``uefi``, ``bios`` or ``unknown`` from Win7 through current Windows.

    Detect firmware mode directly; process bitness is not a reliable boot-mode
    test.  GetFirmwareType is Windows 8+, so Windows 7 falls back to Microsoft's
    documented dummy firmware-variable probe before the optional registry hint.
    """
    if os.name != "nt":
        return "unknown"
    mode = _firmware_type_via_modern_api()
    if mode in ("uefi", "bios"):
        return mode
    mode = _firmware_type_via_environment_api()
    if mode in ("uefi", "bios"):
        return mode
    if winreg is not None:
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control")
            try:
                value = int(winreg.QueryValueEx(key, "PEFirmwareType")[0])
                if value == 1:
                    return "bios"
                if value == 2:
                    return "uefi"
            finally:
                winreg.CloseKey(key)
        except Exception:
            pass
    return "unknown"


def _mountvol_path():
    # Must bypass WOW64 redirection when the 32-bit AnimeBootNeo build runs on
    # x64 Windows.  _native_system_executable() resolves Sysnative when needed.
    return _native_system_executable("mountvol.exe")


def _free_drive_letters():
    used = set(root[:1].upper() for root in _drive_roots())
    return [letter for letter in reversed(string.ascii_uppercase[3:]) if letter not in used]


def _enumerate_volume_names():
    """Enumerate Windows volume GUID paths using the native volume API."""
    if os.name != "nt":
        return []
    try:
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        find_first = kernel32.FindFirstVolumeW
        find_first.argtypes = [wintypes.LPWSTR, wintypes.DWORD]
        find_first.restype = wintypes.HANDLE
        find_next = kernel32.FindNextVolumeW
        find_next.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD]
        find_next.restype = wintypes.BOOL
        find_close = kernel32.FindVolumeClose
        find_close.argtypes = [wintypes.HANDLE]
        find_close.restype = wintypes.BOOL
        buffer = ctypes.create_unicode_buffer(1024)
        handle = find_first(buffer, len(buffer))
        invalid = ctypes.c_void_p(-1).value
        if handle in (None, invalid):
            return []
        result = []
        try:
            while True:
                value = buffer.value
                if value:
                    result.append(value)
                if not find_next(handle, buffer, len(buffer)):
                    break
        finally:
            find_close(handle)
        return result
    except Exception:
        return []


def _volume_mount_paths(volume_name):
    if os.name != "nt":
        return []
    try:
        from ctypes import wintypes
        getter = ctypes.windll.kernel32.GetVolumePathNamesForVolumeNameW
        getter.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        getter.restype = wintypes.BOOL
        needed = wintypes.DWORD(0)
        buffer = ctypes.create_unicode_buffer(4096)
        if not getter(volume_name, buffer, len(buffer), ctypes.byref(needed)):
            if needed.value <= len(buffer):
                return []
            buffer = ctypes.create_unicode_buffer(needed.value + 1)
            if not getter(volume_name, buffer, len(buffer), ctypes.byref(needed)):
                return []
        raw = buffer[:]
        result = []
        for value in raw.split("\x00"):
            value = value.strip()
            if value:
                result.append(value)
        return result
    except Exception:
        return []


def _try_mount_volume(volume_name, letter, mountvol):
    mount_point = letter + ":"
    root = mount_point + "\\"
    try:
        proc = subprocess.Popen([mountvol, mount_point, volume_name], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:out, err = proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill();out,err=proc.communicate(timeout=5)
            return False,root,b'mountvol timed out'
        time.sleep(0.25)
        return proc.returncode == 0 and os.path.isdir(root), root, out + err
    except Exception as exc:
        return False, root, str(exc).encode("utf-8", "replace")


def _unmount_root(info):
    if not info or not info.get("temporary"):
        return
    try:
        root = str(info.get("root") or "")
        mount_arg = str(info.get("mount_point") or (root[:2] if len(root) >= 2 and root[1:2] == ":" else root))
        subprocess.call(
            [info.get("mountvol") or "mountvol.exe", mount_arg, "/D"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
        )
    except Exception:
        pass


def _find_grub_cfgs(boot_root):
    result = []
    for search_root in (os.path.join(boot_root, "EFI"), os.path.join(boot_root, "AnimeBootNeo")):
        if not os.path.isdir(search_root):
            continue
        for current, dirs, files in os.walk(search_root):
            if "grub.cfg" in [name.lower() for name in files]:
                for name in files:
                    if name.lower() == "grub.cfg":
                        result.append(os.path.join(current, name))
            if current.count(os.sep) - search_root.count(os.sep) > 5:
                dirs[:] = []
    return result


def _encode_action_payload(data):
    raw = pickle.dumps(data, protocol=4)
    return base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode("ascii")


def _decode_action_payload(token):
    try:
        raw = zlib.decompress(base64.urlsafe_b64decode(token.encode("ascii")))
        data = pickle.loads(raw)
    except Exception as exc:
        raise DeploymentError("管理员操作参数损坏：%s" % exc)
    if not isinstance(data, dict):
        raise DeploymentError("管理员操作参数格式无效。")
    return data


def _write_binary_result(path, data):
    if not path:
        return
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    temp = path + ".tmp"
    with open(temp, "wb") as stream:
        stream.write(pickle.dumps(data, protocol=4))
        stream.flush()
        try:
            os.fsync(stream.fileno())
        except Exception:
            pass
    os.replace(temp, path)


def read_action_result(path, remove=True):
    with open(path, "rb") as stream:
        result = pickle.load(stream)
    if remove:
        try:
            os.remove(path)
        except OSError:
            pass
    if not isinstance(result, dict):
        raise DeploymentError("管理员操作返回格式无效。")
    return result


def prepare_elevated_action(manifest, action):
    if action not in ("install", "install_full", "install_preflight", "final", "uninstall_boot_item", "set_runtime_fps", "probe_boot_space"):
        raise DeploymentError("未知部署动作：%s" % action)
    if not isinstance(manifest, dict):
        raise DeploymentError("部署参数无效。")
    payload = dict(manifest)
    operation_id = uuid.uuid4().hex
    payload["operation_id"] = operation_id
    handle, result_path = tempfile.mkstemp(prefix="animebootneo_", suffix=".anbr")
    os.close(handle)
    try:
        os.remove(result_path)
    except OSError:
        pass
    return {
        "operation_id": operation_id,
        "payload": _encode_action_payload(payload),
        "result_path": result_path,
    }


def launch_elevated_action(action, payload_token, result_path):
    if os.name != "nt":
        raise DeploymentError("自动替换只支持 Windows。")
    try:
        if os.path.exists(result_path):
            os.remove(result_path)
    except OSError:
        pass
    if getattr(sys, "frozen", False):
        executable = sys.executable
        params = '--elevated-action %s --payload "%s" --result "%s"' % (
            action, payload_token, result_path
        )
        working_dir = os.path.dirname(executable)
    else:
        executable = sys.executable
        script = os.path.abspath(sys.argv[0])
        params = '"%s" --elevated-action %s --payload "%s" --result "%s"' % (
            script, action, payload_token, result_path
        )
        working_dir = os.path.dirname(script)
    shell_execute = ctypes.windll.shell32.ShellExecuteW
    shell_execute.restype = ctypes.c_void_p
    value = shell_execute(None, "runas", executable, params, working_dir, 1)
    numeric = int(value or 0)
    if numeric <= 32:
        raise DeploymentError("UAC 管理员启动失败，错误码：%s" % numeric)
    return True
