# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
"""Native Windows tests use ONLY disposable files, never a real boot volume."""
import contextlib
import ctypes
import hashlib
import os
import tempfile
import unittest
from ctypes import wintypes
from pathlib import Path
from unittest import mock

from animeboot_neo import deployment, legacy_file


@contextlib.contextmanager
def deny_write_attributes(path):
    """Deny Everyone only FILE_WRITE_ATTRIBUTES, leaving ownership unchanged."""
    adv = ctypes.WinDLL('advapi32', use_last_error=True)
    adv.GetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    adv.GetFileSecurityW.restype = wintypes.BOOL
    adv.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    adv.SetFileSecurityW.restype = wintypes.BOOL
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    n = wintypes.DWORD()
    adv.GetFileSecurityW(str(path), 4, None, 0, ctypes.byref(n))
    original = ctypes.create_string_buffer(n.value)
    if not adv.GetFileSecurityW(str(path), 4, original, n.value, ctypes.byref(n)):
        raise ctypes.WinError(ctypes.get_last_error())
    descriptor = ctypes.c_void_p()
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            'D:P(D;;0x100;;;WD)(A;;FA;;;WD)', 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    api = legacy_file.w._windows_kernel32()
    api.LocalFree.argtypes = [ctypes.c_void_p]; api.LocalFree.restype = ctypes.c_void_p
    try:
        if not adv.SetFileSecurityW(str(path), 4 | 0x80000000, descriptor):
            raise ctypes.WinError(ctypes.get_last_error())
        yield
    finally:
        # Restore only this test file's DACL. No real boot file is accessed.
        if path.exists() and not adv.SetFileSecurityW(str(path), 4 | 0x80000000, original):
            raise ctypes.WinError(ctypes.get_last_error())
        api.LocalFree(descriptor)


@unittest.skipUnless(os.name == 'nt', 'Native Windows filesystem required')
class WindowsBootFileTests(unittest.TestCase):
    def test_native_protected_sh_bootmgr_installs_without_touching_original_attributes(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'bootmgr';p.write_bytes(b'WINDOWS')
            try:
                legacy_file.set_attributes(p,6)
                with deny_write_attributes(p):
                    api=legacy_file.w._windows_kernel32()
                    api.SetFileAttributesW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD]
                    api.SetFileAttributesW.restype=wintypes.BOOL
                    # Reproduce the previous installer on a real restricted
                    # file: even writing the SAME attributes is denied.
                    flags=legacy_file.attributes(p)
                    self.assertFalse(api.SetFileAttributesW(str(p),flags&~1))
                    self.assertEqual(ctypes.get_last_error(),5)
                    # The corrected installer must work without any privileged
                    # attribute path, because no original attribute need change.
                    with mock.patch.object(legacy_file,'_restore_privilege',side_effect=AssertionError('unnecessary privilege')):
                        deployment._atomic(p,b'NEO',bios=True,restore_attributes=flags)
                        self.assertEqual(p.read_bytes(),b'NEO')
                        self.assertEqual(legacy_file.attributes(p)&7,6)
                        deployment._atomic(p,b'WINDOWS',bios=True,restore_attributes=flags)
                        self.assertEqual(p.read_bytes(),b'WINDOWS')
            finally:legacy_file.set_attributes(p,0x80)

    def test_native_readonly_permission_preflight_is_mutation_free(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'bootmgr';p.write_bytes(b'WINDOWS')
            try:
                legacy_file.set_attributes(p,7)
                with deny_write_attributes(p), mock.patch.object(legacy_file,
                        '_restore_privilege',side_effect=deployment.DeploymentError('restore privilege denied')):
                    with self.assertRaisesRegex(deployment.DeploymentError,'restore privilege denied'):
                        legacy_file.check_readonly_access(p)
                self.assertEqual(p.read_bytes(),b'WINDOWS')
                self.assertEqual(legacy_file.attributes(p)&~0x20,7)
                self.assertEqual(len(list(Path(d).iterdir())),1)
            finally:legacy_file.set_attributes(p,0x80)

    def test_native_rhs_atomic_install_and_restore(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'bootmgr'; p.write_bytes(b'WINDOWS')
            try:
                legacy_file.set_attributes(p, 7)
                deployment._atomic(p, b'NEO', bios=True, restore_attributes=7)
                self.assertEqual(p.read_bytes(), b'NEO')
                self.assertEqual(legacy_file.attributes(p)&~0x20, 7)
                deployment._atomic(p, b'WINDOWS', bios=True, restore_attributes=7)
                self.assertEqual(p.read_bytes(), b'WINDOWS')
                self.assertEqual(legacy_file.attributes(p)&~0x20, 7)
            finally:
                legacy_file.set_attributes(p, 0x80)

    def test_native_denied_noop_attributes_and_rollback_require_no_write(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); p = root/'bootmgr'; original = b'WINDOWS'
            p.write_bytes(original); backup = root/deployment.BACKUP/'files'
            backup.mkdir(parents=True); (backup/'bootmgr').write_bytes(original)
            state = {'legacy_method':'bootmgr-file', 'files':{'bootmgr':{
                'existed':True, 'original_attributes':7,
                'original_sha256':hashlib.sha256(original).hexdigest(),
                'installed_sha256':hashlib.sha256(b'NEO').hexdigest()}}}
            try:
                legacy_file.set_attributes(p, 7)
                with deny_write_attributes(p):
                    api = legacy_file.w._windows_kernel32()
                    api.SetFileAttributesW.argtypes = [wintypes.LPCWSTR,wintypes.DWORD]
                    api.SetFileAttributesW.restype = wintypes.BOOL
                    self.assertFalse(api.SetFileAttributesW(str(p), 7))
                    self.assertEqual(ctypes.get_last_error(), 5)
                    with mock.patch.object(legacy_file, '_set_attributes_protected') as protected:
                        legacy_file.set_attributes(p, 7)
                        deployment._rollback_files(root, state)
                        protected.assert_not_called()
                    self.assertEqual(p.read_bytes(), original)
                    self.assertEqual(legacy_file.attributes(p)&~0x20, 7)
            finally:
                legacy_file.set_attributes(p, 0x80)

    def test_native_handle_attributes_on_a_dacl_protected_surrogate(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'bootmgr'; p.write_bytes(b'WINDOWS')
            api = legacy_file.w._windows_kernel32()
            legacy_file.set_attributes(p, 7)
            # Obtain a real granted handle BEFORE the test DACL denies access.
            # This verifies FileBasicInfo natively; privilege acquisition is a
            # separate contract test, not an assertion of elevated testing.
            handle = api.CreateFileW(str(p), 0x100, 7, None, 3, 0x02000000, None)
            self.assertNotIn(handle, (None, 0, ctypes.c_void_p(-1).value))
            consumed = []
            class GrantedAPI:
                def __getattr__(self, name):return getattr(api, name)
                def CreateFileW(self, path, access, share, security, creation, flags, template):
                    consumed.append((path,access,share,creation,flags));return handle
            try:
                with deny_write_attributes(p), mock.patch.object(legacy_file,
                        '_restore_privilege', return_value=contextlib.nullcontext()), mock.patch.object(
                        legacy_file.w, '_windows_kernel32', return_value=GrantedAPI()):
                    legacy_file.set_attributes(p, 6)
                    self.assertEqual(legacy_file.attributes(p), 6)
                    self.assertEqual(consumed, [(str(p),0x100,7,3,0x02000000)])
                    # The protected-file path did not grant a broader DACL.
                    self.assertFalse(api.SetFileAttributesW(str(p), 7))
                    self.assertEqual(ctypes.get_last_error(), 5)
            finally:
                if not consumed:api.CloseHandle(handle)
                legacy_file.set_attributes(p, 0x80)

    def test_native_handle_rename_fallback_preserves_rhs(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'窗口-bootmgr'; p.write_bytes(b'WINDOWS')
            source=Path(d)/'new'; source.write_bytes(b'NEO')
            error=PermissionError('injected path rename denial'); error.winerror=5
            try:
                legacy_file.set_attributes(source,7)
                with mock.patch.object(legacy_file.os,'replace',side_effect=error), mock.patch.object(
                        legacy_file,'_restore_privilege',return_value=contextlib.nullcontext()):
                    legacy_file.replace_file(source,p)
                self.assertFalse(source.exists()); self.assertEqual(p.read_bytes(),b'NEO')
                self.assertEqual(legacy_file.attributes(p)&~0x20,7)
            finally:
                for path in (p,source):
                    if path.exists():legacy_file.set_attributes(path,0x80)

    def test_attribute_failure_before_publication_keeps_windows_and_cleans_staging(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'bootmgr';p.write_bytes(b'WINDOWS')
            try:
                legacy_file.set_attributes(p,7)
                with deny_write_attributes(p), mock.patch.object(legacy_file,
                        '_restore_privilege',side_effect=deployment.DeploymentError('token denied')):
                    with self.assertRaisesRegex(deployment.DeploymentError,'Windows 错误 5'):
                        deployment._atomic(p,b'NEO',bios=True,restore_attributes=7)
                self.assertEqual(p.read_bytes(),b'WINDOWS')
                self.assertEqual(legacy_file.attributes(p)&~0x20,7)
                self.assertFalse(Path(str(p)+'.neo-new').exists())
            finally:
                legacy_file.set_attributes(p,0x80)

    def test_staging_attributes_failure_does_not_touch_original_bootmgr(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'bootmgr';p.write_bytes(b'WINDOWS');setter=legacy_file.set_attributes
            def fail_staged(path,value):
                if Path(path).name.endswith('.neo-new') and value==7:
                    raise deployment.DeploymentError('injected staging attribute failure')
                setter(path,value)
            try:
                legacy_file.set_attributes(p,7)
                with mock.patch.object(legacy_file,'set_attributes',side_effect=fail_staged):
                    with self.assertRaisesRegex(deployment.DeploymentError,'staging attribute'):
                        deployment._atomic(p,b'NEO',restore_attributes=7)
                self.assertEqual(p.read_bytes(),b'WINDOWS');self.assertEqual(legacy_file.attributes(p),7)
                self.assertFalse(Path(str(p)+'.neo-new').exists())
            finally:legacy_file.set_attributes(p,0x80)

    def test_failed_native_rename_restores_rhs_and_does_not_mask_original_error(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'bootmgr';p.write_bytes(b'WINDOWS')
            try:
                legacy_file.set_attributes(p,7)
                error=OSError('injected share violation');error.winerror=32
                with mock.patch.object(legacy_file.os,'replace',side_effect=error):
                    with self.assertRaisesRegex(OSError,'share violation'):
                        deployment._atomic(p,b'NEO',restore_attributes=7)
                self.assertEqual(p.read_bytes(),b'WINDOWS');self.assertEqual(legacy_file.attributes(p),7)
                self.assertFalse(Path(str(p)+'.neo-new').exists())
            finally:legacy_file.set_attributes(p,0x80)


@unittest.skipUnless(os.name=='nt','Windows token API contract')
class RestorePrivilegeContractTests(unittest.TestCase):
    def _fake_apis(self, missing=False):
        calls=[]
        class Function:
            def __init__(self,fn):self.fn=fn
            def __call__(self,*args):return self.fn(*args)
        def open_token(process,access,out):
            ctypes.cast(out,ctypes.POINTER(wintypes.HANDLE))[0]=123
            calls.append(('open',access));return 1
        def lookup(system,name,out):
            ctypes.cast(out,ctypes.POINTER(wintypes.DWORD))[0]=17
            self.assertEqual(name,'SeRestorePrivilege');return 1
        def adjust(token,disabled,requested,size,previous,length):
            attrs=ctypes.cast(requested,ctypes.POINTER(wintypes.DWORD))[3]
            calls.append(('adjust',attrs,size))
            if size:
                ctypes.memmove(previous,requested,16)
                ctypes.cast(previous,ctypes.POINTER(wintypes.DWORD))[3]=0
                ctypes.cast(length,ctypes.POINTER(wintypes.DWORD))[0]=16
            ctypes.set_last_error(1300 if missing and size else 0)
            return 1
        class Adv:pass
        adv=Adv();adv.OpenProcessToken=Function(open_token)
        adv.LookupPrivilegeValueW=Function(lookup);adv.AdjustTokenPrivileges=Function(adjust)
        class Kernel:pass
        api=Kernel();api.GetCurrentProcess=Function(lambda:456)
        api.CloseHandle=Function(lambda handle:calls.append(('close',handle.value)) or 1)
        return adv,api,calls

    def test_enabled_restore_privilege_is_restored_after_a_file_failure(self):
        adv,api,calls=self._fake_apis()
        with mock.patch.object(legacy_file.ctypes,'WinDLL',return_value=adv), mock.patch.object(
                legacy_file.w,'_windows_kernel32',return_value=api):
            with self.assertRaisesRegex(OSError,'injected file failure'):
                with legacy_file._restore_privilege():
                    raise OSError('injected file failure')
        self.assertEqual(calls,[('open',0x28),('adjust',2,16),('adjust',0,0),('close',123)])

    def test_bool_success_with_not_all_assigned_is_a_real_failure(self):
        adv,api,calls=self._fake_apis(missing=True)
        with mock.patch.object(legacy_file.ctypes,'WinDLL',return_value=adv), mock.patch.object(
                legacy_file.w,'_windows_kernel32',return_value=api):
            with self.assertRaisesRegex(deployment.DeploymentError,'Windows 错误 1300'):
                with legacy_file._restore_privilege():self.fail('Missing privilege must not enter body')
        self.assertEqual(calls[-1],('close',123))


if __name__=='__main__':unittest.main()
