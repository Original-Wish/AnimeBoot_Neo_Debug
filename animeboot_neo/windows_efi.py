# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
# -*- coding: utf-8 -*-
"""Identify the original Windows EFI manager using PE, version and signature.

Microsoft's bootmgfw.efi commonly declares OriginalFilename=bootmgr.exe.
Neither a raw filename substring nor the presence of MZ is an identity check.
No online certificate lookup or modification of the inspected file is used.
"""

# 校验真正 Windows Boot Manager 的 PE结构、版本资源与系统信任信息。
# Windows 更新会改变二进制摘要，因此不采用一份固定微软文件哈希作为通用识别标准。
# 名称 bootmgfw.efi 本身不可信；本工程 shim 也使用这个部署名，必须区分原 Windows 文件与本工具入口。
# 此模块验证目标/备份身份，不负责写文件；无法确认身份时由部署事务拒绝覆盖并保留现场。

import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import struct


def inspect_pe(data):
    if len(data) < 128 or data[:2] != b'MZ':
        raise ValueError('Windows EFI 文件缺少有效的 PE 头。')
    offset = struct.unpack_from('<I', data, 0x3c)[0]
    if offset < 64 or offset + 24 > len(data) or data[offset:offset+4] != b'PE\0\0':
        raise ValueError('Windows EFI 文件的 PE 头损坏。')
    machine, sections = struct.unpack_from('<HH', data, offset+4)
    optional_size = struct.unpack_from('<H', data, offset+20)[0]
    optional = offset+24
    if machine != 0x8664 or optional_size < 152 or optional+optional_size > len(data):
        raise ValueError('Windows 启动入口必须是完整的 x64 EFI 程序。')
    if struct.unpack_from('<H', data, optional)[0] != 0x20b:
        raise ValueError('Windows 启动入口不是 x64 PE32+ 程序。')
    if struct.unpack_from('<H', data, optional+68)[0] != 10:
        raise ValueError('所选文件不是 EFI Application。')
    table = optional+optional_size
    if not sections or table+sections*40 > len(data):
        raise ValueError('Windows EFI 文件的节区表损坏。')
    for i in range(sections):
        size, pos = struct.unpack_from('<II', data, table+i*40+16)
        if size and (pos < table+sections*40 or pos+size > len(data)):
            raise ValueError('Windows EFI 文件的节区内容被截断。')
    cert_offset, cert_size = struct.unpack_from('<II', data, optional+112+4*8)
    if not cert_offset or cert_size < 8 or cert_offset+cert_size > len(data):
        raise ValueError('Windows EFI 文件缺少完整的嵌入式签名。')
    return {'machine':'x64', 'subsystem':'EFI Application'}


def _version_strings(path):
    api = ctypes.WinDLL('version', use_last_error=True)
    api.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    api.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    api.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    api.GetFileVersionInfoW.restype = wintypes.BOOL
    api.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    api.VerQueryValueW.restype = wintypes.BOOL
    unused = wintypes.DWORD()
    size = api.GetFileVersionInfoSizeW(str(path), ctypes.byref(unused))
    if not size:raise ValueError('Windows EFI 文件没有可读取的版本信息。')
    blob = ctypes.create_string_buffer(size)
    if not api.GetFileVersionInfoW(str(path), 0, size, blob):raise ctypes.WinError(ctypes.get_last_error())
    pointer, length = ctypes.c_void_p(), wintypes.UINT()
    translations = []
    if api.VerQueryValueW(blob, r'\VarFileInfo\Translation', ctypes.byref(pointer), ctypes.byref(length)):
        raw = ctypes.string_at(pointer, length.value)
        translations = [struct.unpack_from('<HH', raw, i) for i in range(0,len(raw)-3,4)]
    translations += [(0x409,0x4b0),(0x409,0x4e4)]
    result = {}
    for key in ('OriginalFilename','CompanyName','FileDescription','ProductName','FileVersion'):
        for language, codepage in translations:
            query = r'\StringFileInfo\%04x%04x\%s' % (language,codepage,key)
            if api.VerQueryValueW(blob, query, ctypes.byref(pointer), ctypes.byref(length)) and length.value:
                result[key] = ctypes.wstring_at(pointer, length.value).rstrip('\0')
                break
    return result


def _signature(path):
    class GUID(ctypes.Structure):
        _fields_=[('Data1',wintypes.DWORD),('Data2',wintypes.WORD),('Data3',wintypes.WORD),('Data4',ctypes.c_ubyte*8)]
    class FILE_INFO(ctypes.Structure):
        _fields_=[('cbStruct',wintypes.DWORD),('pcwszFilePath',wintypes.LPCWSTR),('hFile',wintypes.HANDLE),('pgKnownSubject',ctypes.c_void_p)]
    class TRUST_DATA(ctypes.Structure):
        _fields_=[('cbStruct',wintypes.DWORD),('pPolicyCallbackData',ctypes.c_void_p),('pSIPClientData',ctypes.c_void_p),
                  ('dwUIChoice',wintypes.DWORD),('fdwRevocationChecks',wintypes.DWORD),('dwUnionChoice',wintypes.DWORD),
                  ('pFile',ctypes.POINTER(FILE_INFO)),('dwStateAction',wintypes.DWORD),('hWVTStateData',wintypes.HANDLE),
                  ('pwszURLReference',wintypes.LPCWSTR),('dwProvFlags',wintypes.DWORD),('dwUIContext',wintypes.DWORD)]
    # Prefixes of the native provider structures: only the first certificate is
    # accessed, before WinVerifyTrust closes the state. No array stride is used.
    class CERT_PREFIX(ctypes.Structure):
        _fields_=[('cbStruct',wintypes.DWORD),('pCert',ctypes.c_void_p)]
    class SIGNER_PREFIX(ctypes.Structure):
        _fields_=[('cbStruct',wintypes.DWORD),('sftVerifyAsOf',wintypes.FILETIME),('csCertChain',wintypes.DWORD),
                  ('pasCertChain',ctypes.POINTER(CERT_PREFIX))]
    trust = ctypes.WinDLL('wintrust', use_last_error=True)
    crypto = ctypes.WinDLL('crypt32', use_last_error=True)
    trust.WinVerifyTrust.argtypes=[wintypes.HWND,ctypes.POINTER(GUID),ctypes.POINTER(TRUST_DATA)]
    trust.WinVerifyTrust.restype=wintypes.LONG
    trust.WTHelperProvDataFromStateData.argtypes=[wintypes.HANDLE]
    trust.WTHelperProvDataFromStateData.restype=ctypes.c_void_p
    trust.WTHelperGetProvSignerFromChain.argtypes=[ctypes.c_void_p,wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    trust.WTHelperGetProvSignerFromChain.restype=ctypes.POINTER(SIGNER_PREFIX)
    crypto.CertGetNameStringW.argtypes=[ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.LPWSTR,wintypes.DWORD]
    crypto.CertGetNameStringW.restype=wintypes.DWORD
    action=GUID(0x00aac56b,0xcd44,0x11d0,(ctypes.c_ubyte*8)(0x8c,0xc2,0,0xc0,0x4f,0xc2,0x95,0xee))
    fileinfo=FILE_INFO(ctypes.sizeof(FILE_INFO),str(path),None,None)
    data=TRUST_DATA();data.cbStruct=ctypes.sizeof(data);data.dwUIChoice=2;data.dwUnionChoice=1
    data.pFile=ctypes.pointer(fileinfo);data.dwStateAction=1
    data.dwProvFlags=0x1000|0x10  # local cache only; no network revocation lookup
    try:
        code=trust.WinVerifyTrust(wintypes.HWND(-1),ctypes.byref(action),ctypes.byref(data)) & 0xffffffff
        if code:raise ValueError('Windows EFI 文件签名验证未通过（0x%08X）；未改动启动入口。'%code)
        provider=trust.WTHelperProvDataFromStateData(data.hWVTStateData)
        signer=trust.WTHelperGetProvSignerFromChain(provider,0,False,0) if provider else None
        if not signer or not signer.contents.csCertChain:raise ValueError('Windows EFI 签名证书无法读取。')
        cert=signer.contents.pasCertChain.contents.pCert
        size=crypto.CertGetNameStringW(cert,4,0,None,None,0)
        name=ctypes.create_unicode_buffer(max(1,size))
        crypto.CertGetNameStringW(cert,4,0,None,name,len(name))
        if not name.value.lower().startswith('microsoft windows'):
            raise ValueError('EFI 入口不是微软 Windows 签名的启动管理器。')
        return {'valid':True,'signer':name.value,'verification':'WinVerifyTrust offline'}
    finally:
        if data.hWVTStateData:
            data.dwStateAction=2;trust.WinVerifyTrust(wintypes.HWND(-1),ctypes.byref(action),ctypes.byref(data))


def validate_windows_efi(path, expected_bytes=None):
    path=Path(path).resolve()
    raw=path.read_bytes()
    if expected_bytes is not None and raw!=expected_bytes:
        raise ValueError('检查期间 Windows EFI 入口发生变化；未改动启动链。')
    result=inspect_pe(raw)
    if os.name!='nt':raise ValueError('微软 EFI 签名检查需要 Windows 环境。')
    version=_version_strings(path)
    original=version.get('OriginalFilename','').lower()
    if original not in ('bootmgr.exe','bootmgfw.efi','bootmgfw.exe') or version.get('CompanyName','').lower()!='microsoft corporation':
        raise ValueError('EFI 入口的版本信息不属于 Microsoft Windows Boot Manager。')
    result.update(version=version,signature=_signature(path),sha256=hashlib.sha256(raw).hexdigest())
    if path.read_bytes()!=raw:raise ValueError('签名检查期间 Windows EFI 入口发生变化。')
    return result
