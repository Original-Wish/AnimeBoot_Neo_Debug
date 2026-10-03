# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
"""Cleanup regressions use disposable directories, never a boot partition."""
import contextlib
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

from animeboot_neo import boot_space, legacy_file


def clear_readonly(path):
    legacy_file.set_attributes(path, legacy_file.attributes(path) & ~1)


@contextlib.contextmanager
def disposable_tree():
    with tempfile.TemporaryDirectory(prefix='neo-cleanup-263-') as temporary:
        root = Path(temporary)
        try:
            yield root
        finally:
            # A failing test may deliberately leave READONLY directories.
            for path in list(root.rglob('*')) + [root]:
                if path.exists():
                    clear_readonly(path)


def put(root, name, contents):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)
    return path


class OwnedCleanupTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Native Windows directory attributes required')
    def test_readonly_root_nested_directories_and_files_are_completely_removed(self):
        with disposable_tree() as root:
            put(root, 'AnimeBootNeo/old/deep/frame.bin', b'animation')
            outside = put(root, 'EFI/OtherOS/original.bin', b'untouched')
            owned = root / 'AnimeBootNeo'
            for path in list(owned.rglob('*')) + [owned]:
                legacy_file.set_attributes(path, legacy_file.attributes(path) | 1)
            self.assertTrue(legacy_file.attributes(owned) & 1)
            report = boot_space.remove_owned_tree(root, 'AnimeBootNeo', clear_readonly)
            self.assertTrue(report['removed'])
            self.assertFalse(owned.exists())
            self.assertEqual(outside.read_bytes(), b'untouched')

    def test_backup_checkpoint_is_retained_until_caller_finishes_restoration(self):
        with disposable_tree() as root:
            checkpoint = put(root, 'AnimeBootNeo.Backup/state.json', b'final checkpoint')
            put(root, 'AnimeBootNeo.Backup/files/EFI/Microsoft/Boot/original.efi', b'Windows backup')
            backup = checkpoint.parent
            if os.name == 'nt':
                for path in list(backup.rglob('*')) + [backup]:
                    legacy_file.set_attributes(path, legacy_file.attributes(path) | 1)
            report = boot_space.remove_owned_tree(
                root, 'AnimeBootNeo.Backup', clear_readonly, preserve_files=('state.json',))
            self.assertFalse(report['removed'])
            self.assertEqual(report['remaining_files'], ['state.json'])
            self.assertEqual(checkpoint.read_bytes(), b'final checkpoint')
            self.assertEqual(list(backup.iterdir()), [checkpoint])
            report = boot_space.remove_owned_tree(root, 'AnimeBootNeo.Backup', clear_readonly)
            self.assertTrue(report['removed'])
            self.assertFalse(backup.exists())

    def test_attribute_failure_happens_before_any_file_or_checkpoint_is_deleted(self):
        with disposable_tree() as root:
            checkpoint = put(root, 'AnimeBootNeo.Backup/state.json', b'recovery')
            original = put(root, 'AnimeBootNeo.Backup/files/original.bin', b'original')
            before = {path: path.read_bytes() for path in (checkpoint, original)}

            def fail_on_original(path):
                if path == original:
                    raise PermissionError('injected attribute refusal')
                clear_readonly(path)

            with self.assertRaisesRegex(PermissionError, 'attribute refusal'):
                boot_space.remove_owned_tree(root, 'AnimeBootNeo.Backup', fail_on_original,
                                             preserve_files=('state.json',))
            self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_mid_deletion_failure_keeps_checkpoint_and_allows_cleanup_retry(self):
        with disposable_tree() as root:
            checkpoint = put(root, 'AnimeBootNeo.Backup/state.json', b'recovery')
            original = put(root, 'AnimeBootNeo.Backup/files/original.bin', b'original')
            unlink = Path.unlink

            def fail_on_original(path, *args, **kwargs):
                if path == original:
                    raise PermissionError('injected deletion refusal')
                return unlink(path, *args, **kwargs)

            with mock.patch.object(Path, 'unlink', fail_on_original):
                with self.assertRaisesRegex(PermissionError, 'deletion refusal'):
                    boot_space.remove_owned_tree(root, 'AnimeBootNeo.Backup', clear_readonly,
                                                 preserve_files=('state.json',))
            self.assertEqual(checkpoint.read_bytes(), b'recovery')
            report = boot_space.remove_owned_tree(root, 'AnimeBootNeo.Backup', clear_readonly,
                                                 preserve_files=('state.json',))
            self.assertEqual(report['remaining_files'], ['state.json'])

    def test_reparse_point_anywhere_rejects_entire_tree_before_mutation(self):
        with disposable_tree() as root:
            checkpoint = put(root, 'AnimeBootNeo.Backup/state.json', b'recovery')
            inside = put(root, 'AnimeBootNeo.Backup/files/original.bin', b'original')
            outside = put(root, 'other-os.bin', b'outside')
            flagged_directory = inside.parent
            lstat = Path.lstat

            def reparse_metadata(path):
                info = lstat(path)
                if path == flagged_directory:
                    return types.SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
                return info

            with mock.patch.object(Path, 'lstat', reparse_metadata), \
                 mock.patch(__name__ + '.clear_readonly') as clear:
                with self.assertRaisesRegex(ValueError, '重解析点'):
                    boot_space.remove_owned_tree(root, 'AnimeBootNeo.Backup', clear,
                                                 preserve_files=('state.json',))
                clear.assert_not_called()
            self.assertEqual(checkpoint.read_bytes(), b'recovery')
            self.assertEqual(inside.read_bytes(), b'original')
            self.assertEqual(outside.read_bytes(), b'outside')

    def test_missing_or_escaping_checkpoint_is_rejected_before_mutation(self):
        with disposable_tree() as root:
            original = put(root, 'AnimeBootNeo.Backup/files/original.bin', b'original')
            for preserved in ('../other.json', 'missing.json'):
                with self.subTest(preserved=preserved):
                    clear = mock.Mock(side_effect=clear_readonly)
                    with self.assertRaises(ValueError):
                        boot_space.remove_owned_tree(root, 'AnimeBootNeo.Backup', clear,
                                                     preserve_files=(preserved,))
                    clear.assert_not_called()
                    self.assertEqual(original.read_bytes(), b'original')


if __name__ == '__main__':
    unittest.main()
