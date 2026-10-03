# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
# -*- coding: utf-8 -*-
import struct
import unittest
from unittest import mock

from animeboot_neo import windows as deployment


def _load_option(path, description="Windows Boot Manager"):
    desc = description.encode("utf-16le") + b"\x00\x00"
    payload = (path + "\x00").encode("utf-16le")
    node = bytes([0x04, 0x04]) + struct.pack("<H", 4 + len(payload)) + payload
    end = bytes([0x7F, 0xFF, 0x04, 0x00])
    fp = node + end
    return struct.pack("<IH", 1, len(fp)) + desc + fp


def _native_state(initial_order):
    state = {"order": list(initial_order), "deleted": []}

    def read_order():
        return list(state["order"])

    def set_var(name, guid, data):
        if name == "BootOrder":
            if data:
                state["order"] = list(struct.unpack("<%dH" % (len(data) // 2), data))
            else:
                state["order"] = []
            return
        if name.startswith("Boot") and data is None:
            number = int(name[4:], 16)
            state["deleted"].append(number)
            state["order"] = [n for n in state["order"] if n != number]
            return
        raise AssertionError("unexpected firmware write: %s" % name)

    return state, read_order, set_var


def test_keep_transaction_removes_one_unrequested_new_wbm_by_exact_boot_number():
    # Exact field regression: user chose KEEP. One WBM appears only after the
    # consent dialog, so it did not exist in the write-phase baseline.
    state, read_order, set_var = _native_state([0x15, 0x12, 0x01, 0x00])
    raw = _load_option(r"\EFI\Microsoft\Boot\bootmgfw.efi")
    with mock.patch.object(deployment, "_read_firmware_boot_order", side_effect=read_order), \
         mock.patch.object(deployment, "_set_firmware_variable_bytes", side_effect=set_var), \
         mock.patch.object(deployment, "_get_firmware_variable_bytes", return_value=(raw, 0)):
        result = deployment._cleanup_unexpected_new_firmware_boot_options(
            dict(order=[0x12,0x01],entries={n:_snapshot_record(n,raw) for n in (0x12,0x01)}),
            allowed_new_numbers=[0x00], phase="fresh-install"
        )
    assert result["unexpected_new_numbers"] == [0x15]
    assert result["cleaned_numbers"] == [0x15]
    assert state["deleted"] == [0x15]
    assert state["order"] == [0x12, 0x01, 0x00]


def test_repair_transaction_keeps_exact_repair_and_rescue_but_removes_extra_wbm():
    # Repair legitimately adds one WBM (Boot0016) plus one AnimeBootNeo rescue item
    # (Boot0000). A second WBM (Boot0015) is a firmware/BCDEdit side effect.
    state, read_order, set_var = _native_state([0x16, 0x15, 0x12, 0x01, 0x00])
    raw = _load_option(r"\EFI\Microsoft\Boot\bootmgfw.efi")
    with mock.patch.object(deployment, "_read_firmware_boot_order", side_effect=read_order), \
         mock.patch.object(deployment, "_set_firmware_variable_bytes", side_effect=set_var), \
         mock.patch.object(deployment, "_get_firmware_variable_bytes", return_value=(raw, 0)):
        result = deployment._cleanup_unexpected_new_firmware_boot_options(
            dict(order=[0x12,0x01],entries={n:_snapshot_record(n,raw) for n in (0x12,0x01)}),
            allowed_new_numbers=[0x00,0x16], phase="fresh-install"
        )
    assert result["unexpected_new_numbers"] == [0x15]
    assert result["cleaned_numbers"] == [0x15]
    assert state["order"] == [0x16, 0x12, 0x01, 0x00]


def test_replace_uninstall_phase_is_not_allowed_to_create_any_boot_option():
    state, read_order, set_var = _native_state([0x20, 0x12, 0x01])
    raw = _load_option(r"\EFI\Microsoft\Boot\bootmgfw.efi")
    with mock.patch.object(deployment, "_read_firmware_boot_order", side_effect=read_order), \
         mock.patch.object(deployment, "_set_firmware_variable_bytes", side_effect=set_var), \
         mock.patch.object(deployment, "_get_firmware_variable_bytes", return_value=(raw, 0)):
        result = deployment._cleanup_unexpected_new_firmware_boot_options(
            dict(order=[0x12,0x01],entries={n:_snapshot_record(n,raw) for n in (0x12,0x01)}),
            allowed_new_numbers=[], phase="replace-uninstall"
        )
    assert result["cleaned_numbers"] == [0x20]
    assert state["order"] == [0x12, 0x01]


def _snapshot_record(number, raw):
    return {
        "number": int(number),
        "name": "Boot%04X" % int(number),
        "sha256": deployment.hashlib.sha256(raw).hexdigest(),
        "description": deployment._efi_load_option_description(raw),
        "paths": deployment._efi_load_option_file_paths(raw),
        "attributes": 7,
        "raw": raw,
    }


def test_same_boot0000_number_reused_for_new_wbm_is_detected_by_identity_hash():
    # Real-machine regression: before disable, Boot0000 is the managed AnimeBootNeo
    # rescue item. It is legitimately deleted, then Windows/firmware reuses the
    # *same Boot0000 number* for a fresh Windows Boot Manager. Number-set-only
    # snapshots miss this. Full EFI_LOAD_OPTION fingerprints must catch it.
    old0 = _load_option(r"\EFI\Microsoft\Boot\bootmgfw.efi_ANBAK", "Boot Windows created by AnimeBootNeo")
    old12 = _load_option(r"\EFI\Microsoft\Boot\bootmgfw.efi", "Windows Boot Manager")
    new0 = _load_option(r"\EFI\Microsoft\Boot\bootmgfw.efi", "Windows Boot Manager")
    baseline = {
        "order": [0x00, 0x12],
        "entries": {
            0x00: _snapshot_record(0x00, old0),
            0x12: _snapshot_record(0x12, old12),
        },
    }
    state, read_order, set_var = _native_state([0x00, 0x12])

    def get_var(name, guid, max_bytes=None):
        if name == "Boot0000":
            return new0, 7
        if name == "Boot0012":
            return old12, 7
        raise AssertionError("unexpected firmware read: %s" % name)

    with mock.patch.object(deployment, "_read_firmware_boot_order", side_effect=read_order), \
         mock.patch.object(deployment, "_set_firmware_variable_bytes", side_effect=set_var), \
         mock.patch.object(deployment, "_get_firmware_variable_bytes", side_effect=get_var):
        result = deployment._cleanup_unexpected_new_firmware_boot_options(
            baseline, allowed_new_numbers=[], phase="disable-uninstall-final"
        )

    assert result["observed_new_numbers"] == []
    assert result["observed_reused_numbers"] == [0x00]
    assert result["unexpected_reused_numbers"] == [0x00]
    assert result["cleaned_numbers"] == [0x00]
    assert result["unexpected_details"][0]["change_kind"] == "same-number-reused"
    assert state["deleted"] == [0x00]
    assert state["order"] == [0x12]


def test_same_boot_number_with_unchanged_load_option_is_preserved():
    raw0 = _load_option(r"\EFI\Microsoft\Boot\bootmgfw.efi_ANBAK", "Boot Windows created by AnimeBootNeo")
    raw12 = _load_option(r"\EFI\Microsoft\Boot\bootmgfw.efi", "Windows Boot Manager")
    baseline = {
        "order": [0x00, 0x12],
        "entries": {
            0x00: _snapshot_record(0x00, raw0),
            0x12: _snapshot_record(0x12, raw12),
        },
    }
    state, read_order, set_var = _native_state([0x00, 0x12])

    def get_var(name, guid, max_bytes=None):
        return ({"Boot0000": raw0, "Boot0012": raw12}[name], 7)

    with mock.patch.object(deployment, "_read_firmware_boot_order", side_effect=read_order), \
         mock.patch.object(deployment, "_set_firmware_variable_bytes", side_effect=set_var), \
         mock.patch.object(deployment, "_get_firmware_variable_bytes", side_effect=get_var):
        result = deployment._cleanup_unexpected_new_firmware_boot_options(
            baseline, allowed_new_numbers=[], phase="identity-no-change"
        )

    assert result["observed_new_numbers"] == []
    assert result["observed_reused_numbers"] == []
    assert result["cleaned_numbers"] == []
    assert state["deleted"] == []
    assert state["order"] == [0x00, 0x12]


def test_snapshot_records_full_load_option_identity_not_just_boot_number():
    raw0 = _load_option(r"\EFI\Microsoft\Boot\bootmgfw.efi_ANBAK", "Boot Windows created by AnimeBootNeo")
    raw12 = _load_option(r"\EFI\Microsoft\Boot\bootmgfw.efi", "Windows Boot Manager")

    def get_var(name, guid, max_bytes=None):
        return ({"Boot0000": raw0, "Boot0012": raw12}[name], 7)

    with mock.patch.object(deployment, "_read_firmware_boot_order", return_value=[0x00, 0x12]), \
         mock.patch.object(deployment, "_get_firmware_variable_bytes", side_effect=get_var):
        snap = deployment._snapshot_firmware_boot_entries()

    assert snap["order"] == [0x00, 0x12]
    assert snap["entries"][0x00]["description"] == "Boot Windows created by AnimeBootNeo"
    assert snap["entries"][0x00]["paths"] == [r"\EFI\Microsoft\Boot\bootmgfw.efi_ANBAK"]
    assert snap["entries"][0x00]["sha256"] != snap["entries"][0x12]["sha256"]
    assert snap["entries"][0x00]["raw"] == raw0


class CurrentFirmwareIdentityTests(unittest.TestCase):
    """Exercise firmware state changes, including Boot number reuse."""

    def test_keep_transaction_removes_one_unrequested_new_wbm_by_exact_boot_number(self):
        test_keep_transaction_removes_one_unrequested_new_wbm_by_exact_boot_number()

    def test_repair_transaction_keeps_exact_repair_and_rescue_but_removes_extra_wbm(self):
        test_repair_transaction_keeps_exact_repair_and_rescue_but_removes_extra_wbm()

    def test_replace_uninstall_phase_is_not_allowed_to_create_any_boot_option(self):
        test_replace_uninstall_phase_is_not_allowed_to_create_any_boot_option()

    def test_same_boot0000_number_reused_for_new_wbm_is_detected_by_identity_hash(self):
        test_same_boot0000_number_reused_for_new_wbm_is_detected_by_identity_hash()

    def test_same_boot_number_with_unchanged_load_option_is_preserved(self):
        test_same_boot_number_with_unchanged_load_option_is_preserved()

    def test_snapshot_records_full_load_option_identity_not_just_boot_number(self):
        test_snapshot_records_full_load_option_identity_not_just_boot_number()

