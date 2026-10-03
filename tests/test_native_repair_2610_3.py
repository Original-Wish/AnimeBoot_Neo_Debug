# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
import contextlib
import ctypes
import hashlib
import struct
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from animeboot_neo import windows


ESP_GUID = uuid.UUID('8f08d194-c143-4cb8-a663-e52f1cb95860')
OTHER_GUID = uuid.UUID('52910576-6053-481c-a3a1-ea8b9661f3b5')
START = 1048576
LENGTH = 104857600


def esp_identity():
    return dict(partition_guid=str(ESP_GUID), partition_guid_bytes=ESP_GUID.bytes_le,
                partition_number=1, starting_offset=START, partition_length=LENGTH)


def load_option(path, partition=ESP_GUID, description='Windows Boot Manager', optional=b'', active=1):
    hd = struct.pack('<BBHIQQ16sBB', 4, 1, 42, 1, START // 512,
                     LENGTH // 512, partition.bytes_le, 2, 2)
    name = (path + '\0').encode('utf-16le')
    file_node = struct.pack('<BBH', 4, 4, len(name) + 4) + name
    device_path = hd + file_node + b'\x7f\xff\x04\0'
    return struct.pack('<IH', active, len(device_path)) + (description + '\0').encode('utf-16le') + device_path + optional


class NativeStore:
    def __init__(self, entries, order):
        self.values = {'Boot%04X' % number: bytes(raw) for number, raw in entries.items()}
        self.values['BootOrder'] = struct.pack('<%dH' % len(order), *order)
        self.writes = []
        self.order_failure = False
        self.delete_failure = False
        self.on_order_write = None

    def read(self, name, guid, max_bytes=None):
        if name not in self.values:
            raise windows.DeploymentError('读取%s失败（WinError 203）。' % name)
        return self.values[name], 7

    def write(self, name, guid, data, **kwargs):
        self.writes.append((name, data))
        if name == 'BootOrder' and self.order_failure:
            self.order_failure = False
            raise windows.DeploymentError('模拟BootOrder写入失败')
        if data is None:
            if self.delete_failure:
                raise windows.DeploymentError('模拟删除失败')
            self.values.pop(name, None)
        else:
            self.values[name] = bytes(data)
        if name == 'BootOrder' and self.on_order_write:
            callback, self.on_order_write = self.on_order_write, None
            callback(self)

    @contextlib.contextmanager
    def patched(self):
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(windows, '_get_firmware_variable_bytes', side_effect=self.read))
            stack.enter_context(mock.patch.object(windows, '_set_new_firmware_variable_bytes', side_effect=self.write))
            stack.enter_context(mock.patch.object(windows, '_set_firmware_variable_bytes', side_effect=self.write))
            stack.enter_context(mock.patch.object(windows, '_find_free_firmware_boot_number', return_value=2))
            stack.enter_context(mock.patch.object(windows, '_target_esp_partition_identity', return_value=esp_identity()))
            bcd = stack.enter_context(mock.patch.object(windows, '_run_bcdedit', side_effect=AssertionError('BCDEdit must never run')))
            yield bcd

    def order(self):
        raw = self.values['BootOrder']
        return list(struct.unpack('<%dH' % (len(raw) // 2), raw))


class NativeRepairTests(unittest.TestCase):
    def make_file(self, root):
        path = Path(root) / 'EFI/Microsoft/Boot/bootmgfw.efi'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'the exact installed Fedora shim')
        return path.read_bytes()

    def test_repair_uses_same_esp_bootx64_template_without_touching_it_or_bcd(self):
        old = load_option(r'\EFI\Boot\bootx64.efi', optional=b'WINDOWS BCDOBJECT legacy')
        store = NativeStore({1: old}, [1])
        with tempfile.TemporaryDirectory() as root, store.patched() as bcd:
            result = windows._create_repair_windows_boot_manager(root, self.make_file(root))
        self.assertTrue(result['ok'])
        self.assertEqual(result['boot_number'], 2)
        self.assertEqual(store.order(), [2, 1])
        self.assertEqual(store.values['Boot0001'], old)
        self.assertEqual(windows._efi_load_option_file_paths(store.values['Boot0002']),
                         [r'\EFI\Microsoft\Boot\bootmgfw.efi'])
        self.assertEqual(windows._efi_load_option_optional_data(store.values['Boot0002']), b'')
        self.assertEqual(result['boot_sha256'], hashlib.sha256(store.values['Boot0002']).hexdigest())
        self.assertTrue(all(name != 'BootCurrent' for name, _ in store.writes))
        bcd.assert_not_called()

    def test_filename_match_on_another_esp_is_never_accepted(self):
        wrong = load_option(r'\EFI\Microsoft\Boot\bootmgfw.efi', partition=OTHER_GUID)
        correct = load_option(r'\EFI\Boot\bootx64.efi')
        store = NativeStore({8: wrong, 1: correct}, [8, 1])
        with store.patched():
            result = windows._create_native_esp_boot_entry('Z:\\', 'Windows Boot Manager',
                                                         r'\EFI\Microsoft\Boot\bootmgfw.efi', True)
        self.assertTrue(result['ok'])
        self.assertEqual(result['template_boot_name'], 'Boot0001')
        self.assertEqual(store.values['Boot0008'], wrong)
        self.assertEqual(store.values['Boot0001'], correct)
        self.assertEqual(store.order(), [2, 8, 1])

    def test_inactive_application_template_becomes_active_boot_category(self):
        old = load_option(r'\EFI\Boot\bootx64.efi', active=0x100)
        store = NativeStore({1: old}, [1])
        with store.patched():
            result = windows._create_native_esp_boot_entry('Z:\\', 'Windows Boot Manager',
                                                         r'\EFI\Microsoft\Boot\bootmgfw.efi', True)
        self.assertTrue(result['ok'])
        self.assertEqual(struct.unpack_from('<I', store.values['Boot0002'], 0)[0], 1)
        self.assertEqual(store.values['Boot0001'], old)

    def test_allocated_number_occupied_before_creation_is_preserved(self):
        old = load_option(r'\EFI\Boot\bootx64.efi')
        concurrent = load_option(r'\EFI\another\loader.efi')
        store = NativeStore({1: old, 2: concurrent}, [1])
        with store.patched():
            result = windows._create_native_esp_boot_entry('Z:\\', 'Windows Boot Manager',
                                                         r'\EFI\Microsoft\Boot\bootmgfw.efi', True)
        self.assertFalse(result['ok'])
        self.assertTrue(result['manual_required'])
        self.assertEqual(store.writes, [])
        self.assertEqual(store.values['Boot0002'], concurrent)

    def test_no_matching_esp_falls_back_to_manual_without_writes(self):
        store = NativeStore({1: load_option(r'\EFI\Microsoft\Boot\bootmgfw.efi', OTHER_GUID)}, [1])
        with store.patched():
            result = windows._create_native_esp_boot_entry('Z:\\', 'Windows Boot Manager',
                                                         r'\EFI\Microsoft\Boot\bootmgfw.efi', True)
        self.assertFalse(result['ok'])
        self.assertTrue(result['manual_required'])
        self.assertTrue(result['rollback_verified'])
        self.assertEqual(store.writes, [])

    def test_order_publish_failure_rolls_back_only_our_new_entry_and_proves_result(self):
        old = load_option(r'\EFI\Boot\bootx64.efi')
        store = NativeStore({1: old}, [1])
        store.order_failure = True
        with store.patched():
            result = windows._create_native_esp_boot_entry('Z:\\', 'Windows Boot Manager',
                                                         r'\EFI\Microsoft\Boot\bootmgfw.efi', True)
        self.assertFalse(result['ok'])
        self.assertTrue(result['manual_required'])
        self.assertTrue(result['rollback_verified'])
        self.assertEqual(result['cleanup_errors'], [])
        self.assertEqual(store.order(), [1])
        self.assertNotIn('Boot0002', store.values)
        self.assertEqual(store.values['Boot0001'], old)

    def test_unconfirmed_rollback_must_not_be_reported_as_manual_safe(self):
        store = NativeStore({1: load_option(r'\EFI\Boot\bootx64.efi')}, [1])
        store.order_failure = store.delete_failure = True
        with store.patched():
            result = windows._create_native_esp_boot_entry('Z:\\', 'Windows Boot Manager',
                                                         r'\EFI\Microsoft\Boot\bootmgfw.efi', True)
        self.assertFalse(result['ok'])
        self.assertFalse(result['manual_required'])
        self.assertFalse(result['rollback_verified'])
        self.assertTrue(result['cleanup_errors'])
        self.assertEqual(result['boot_number'], 2)
        self.assertEqual(result['boot_sha256'], hashlib.sha256(store.values['Boot0002']).hexdigest())

    def test_modified_old_entry_is_detected_and_never_overwritten_or_deleted(self):
        old = load_option(r'\EFI\Boot\bootx64.efi')
        replacement = load_option(r'\EFI\other\loader.efi')
        store = NativeStore({1: old}, [1])
        store.on_order_write = lambda state: state.values.__setitem__('Boot0001', replacement)
        with store.patched():
            result = windows._create_native_esp_boot_entry('Z:\\', 'Windows Boot Manager',
                                                         r'\EFI\Microsoft\Boot\bootmgfw.efi', True)
        self.assertFalse(result['ok'])
        self.assertFalse(result['manual_required'])
        self.assertEqual(store.values['Boot0001'], replacement)
        self.assertTrue(all(name != 'Boot0001' for name, _ in store.writes))

    def test_rescue_entry_is_also_same_esp_clean_and_appended(self):
        old = load_option(r'\EFI\Boot\bootx64.efi')
        store = NativeStore({1: old}, [1])
        with store.patched():
            result = windows._create_animebootneo_firmware_entry_details('x64', 'Z:\\')
        self.assertFalse(result['skipped'])
        self.assertEqual(store.order(), [1, 2])
        self.assertEqual(windows._efi_load_option_file_paths(store.values['Boot0002']),
                         [r'\EFI\Microsoft\Boot\bootmgfw.efi_ANBAK'])
        self.assertEqual(result['new_boot_numbers'], [2])
        self.assertEqual(result['boot_sha256'], hashlib.sha256(store.values['Boot0002']).hexdigest())

    def test_rescue_partial_rollback_returns_owned_identity_for_install_journal(self):
        store = NativeStore({1: load_option(r'\EFI\Boot\bootx64.efi')}, [1])
        store.order_failure = store.delete_failure = True
        with store.patched():
            result = windows._create_animebootneo_firmware_entry_details('x64', 'Z:\\')
        self.assertTrue(result['skipped'])
        self.assertFalse(result['manual_required'])
        self.assertTrue(result['cleanup_errors'])
        self.assertEqual(result['new_boot_numbers'], [2])
        self.assertEqual(result['boot_sha256'], hashlib.sha256(store.values['Boot0002']).hexdigest())

    def test_delete_exact_identity_is_idempotent_and_preserves_original(self):
        old = load_option(r'\EFI\Microsoft\Boot\bootmgfw.efi')
        owned = load_option(r'\EFI\Microsoft\Boot\bootmgfw.efi_ANBAK', description='owned')
        store = NativeStore({1: old, 2: owned}, [2, 1])
        digest = hashlib.sha256(owned).hexdigest()
        with store.patched():
            first = windows._remove_managed_native_boot_entry(2, digest)
            second = windows._remove_managed_native_boot_entry(2, digest)
        self.assertTrue(first['deleted'])
        self.assertTrue(second['already_absent'])
        self.assertEqual(store.values['Boot0001'], old)
        self.assertEqual(store.order(), [1])

    def test_delete_reused_number_fails_before_mutation(self):
        original = load_option(r'\EFI\Microsoft\Boot\bootmgfw.efi_ANBAK')
        reused = load_option(r'\EFI\different\loader.efi')
        store = NativeStore({1: load_option(r'\EFI\Microsoft\Boot\bootmgfw.efi'), 2: reused}, [2, 1])
        with store.patched(), self.assertRaises(windows.DeploymentError):
            windows._remove_managed_native_boot_entry(2, hashlib.sha256(original).hexdigest())
        self.assertEqual(store.writes, [])
        self.assertEqual(store.values['Boot0002'], reused)

    def test_delete_failure_does_not_claim_completion_and_can_be_retried(self):
        old = load_option(r'\EFI\Microsoft\Boot\bootmgfw.efi')
        owned = load_option(r'\EFI\Microsoft\Boot\bootmgfw.efi_ANBAK')
        store = NativeStore({1: old, 2: owned}, [2, 1])
        store.delete_failure = True
        digest = hashlib.sha256(owned).hexdigest()
        with store.patched():
            with self.assertRaises(windows.DeploymentError):
                windows._remove_managed_native_boot_entry(2, digest)
            self.assertIn('Boot0002', store.values)
            store.delete_failure = False
            result = windows._remove_managed_native_boot_entry(2, digest)
        self.assertTrue(result['deleted'])
        self.assertEqual(store.order(), [1])
        self.assertEqual(store.values['Boot0001'], old)

    def test_wrong_installed_bootmgfw_is_rejected_before_native_writes(self):
        store = NativeStore({1: load_option(r'\EFI\Boot\bootx64.efi')}, [1])
        with tempfile.TemporaryDirectory() as root, store.patched():
            self.make_file(root)
            with self.assertRaises(windows.DeploymentError):
                windows._create_repair_windows_boot_manager(root, b'different shim')
        self.assertEqual(store.writes, [])

    def test_parser_rejects_multiple_instance_device_path(self):
        raw = load_option(r'\EFI\Boot\bootx64.efi')
        raw = raw[:-4] + b'\x7f\x01\x04\0'
        with self.assertRaises(windows.DeploymentError):
            windows._efi_load_option_partition_identity(raw)

    def test_gpt_ioctl_decoder_is_architecture_independent_and_read_only(self):
        blob = bytearray(144)
        struct.pack_into('<I', blob, 0, 1)
        struct.pack_into('<qqI', blob, 8, START, LENGTH, 1)
        blob[32:48] = uuid.UUID('c12a7328-f81f-11d2-ba4b-00a0c93ec93b').bytes_le
        blob[48:64] = ESP_GUID.bytes_le
        kernel = mock.Mock()
        kernel.CreateFileW.return_value = 5
        def ioctl(handle, code, incoming, size, outgoing, capacity, returned, overlap):
            self.assertEqual(code, 0x00070048)
            self.assertIsNone(incoming)
            ctypes.memmove(outgoing, bytes(blob), len(blob))
            returned._obj.value = len(blob)
            return True
        kernel.DeviceIoControl.side_effect = ioctl
        with mock.patch.object(windows.os, 'name', 'nt'), mock.patch.object(windows, '_windows_kernel32', return_value=kernel):
            result = windows._target_esp_partition_identity('Z:\\')
        self.assertEqual(result, esp_identity())
        self.assertEqual(kernel.CreateFileW.call_args.args[1], 0x80000000)
        kernel.CloseHandle.assert_called_once_with(5)


if __name__ == '__main__':
    unittest.main()
