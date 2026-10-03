# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
"""Uninstall failure/retry tests never touch firmware or a real boot volume."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from animeboot_neo import deployment, legacy_file


@contextlib.contextmanager
def disposable_installation():
    with tempfile.TemporaryDirectory(prefix='neo-uninstall-263-') as temporary:
        root = Path(temporary)
        try:
            yield root
        finally:
            for path in list(root.rglob('*')) + [root]:
                if path.exists():
                    legacy_file.set_attributes(path, legacy_file.attributes(path) & ~1)


def fixture(root):
    relative = 'EFI/Microsoft/Boot/bootmgfw.efi'
    entry = root / relative
    entry.parent.mkdir(parents=True)
    entry.write_bytes(b'NEO ENTRY')
    backup = root / deployment.BACKUP
    saved = backup / 'files' / relative
    saved.parent.mkdir(parents=True)
    saved.write_bytes(b'ORIGINAL WINDOWS')
    owned = root / 'AnimeBootNeo'
    extra = owned / 'future/deep/old-frame.bin'
    extra.parent.mkdir(parents=True)
    extra.write_bytes(b'OLD ANIMATION')
    state = dict(format='AnimeBootNeo-installed-3-canvases', app_version='2610.3', phase='complete',
                 mode='uefi', legacy_method='bootmgr-file', env_path='', boot_numbers=[],
                 repair_number=None, files={relative: dict(existed=True,
                 installed_sha256=hashlib.sha256(b'NEO ENTRY').hexdigest(),
                 original_sha256=hashlib.sha256(b'ORIGINAL WINDOWS').hexdigest())})
    journal = backup / 'state.json'
    journal.write_text(json.dumps(state), encoding='utf-8')
    return state, entry, owned, backup, journal, saved, extra


class UninstallTransactionTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Native Windows READONLY directory regression')
    def test_complete_uninstall_restores_windows_and_removes_both_readonly_trees(self):
        with disposable_installation() as root:
            state, entry, owned, backup, journal, saved, extra = fixture(root)
            for tree in (owned, backup):
                for path in list(tree.rglob('*')) + [tree]:
                    legacy_file.set_attributes(path, legacy_file.attributes(path) | 1)
            report = deployment._uninstall(root, state)
            self.assertTrue(report['windows_restored'])
            self.assertEqual(report['owned_directories_removed'], ['AnimeBootNeo', deployment.BACKUP])
            self.assertEqual(entry.read_bytes(), b'ORIGINAL WINDOWS')
            self.assertFalse(owned.exists())
            self.assertFalse(backup.exists())

    def test_resource_deletion_failure_retains_checkpoint_and_retry_finishes(self):
        with disposable_installation() as root:
            state, entry, owned, backup, journal, saved, extra = fixture(root)
            unlink = Path.unlink

            def fail_resource(path, *args, **kwargs):
                if path == extra:
                    raise PermissionError('injected resource unlink failure')
                return unlink(path, *args, **kwargs)

            with mock.patch.object(Path, 'unlink', fail_resource):
                with self.assertRaisesRegex(PermissionError, 'resource unlink failure'):
                    deployment._uninstall(root, state)
            resumed = deployment._read_state(root)
            self.assertEqual(resumed['phase'], 'uninstalling')
            self.assertTrue(resumed['uninstall_progress']['windows_restored'])
            self.assertTrue(resumed['uninstall_progress']['firmware_removed'])
            self.assertEqual(entry.read_bytes(), b'ORIGINAL WINDOWS')
            self.assertTrue(owned.exists())
            self.assertTrue(saved.exists())
            deployment._uninstall(root, resumed)
            self.assertFalse(owned.exists())
            self.assertFalse(backup.exists())

    def test_final_backup_rmdir_failure_recreates_checkpoint_after_backups_reclaimed(self):
        with disposable_installation() as root:
            state, entry, owned, backup, journal, saved, extra = fixture(root)
            rmdir = Path.rmdir

            def fail_backup(path, *args, **kwargs):
                if path == backup:
                    raise PermissionError('injected final backup rmdir failure')
                return rmdir(path, *args, **kwargs)

            with mock.patch.object(Path, 'rmdir', fail_backup):
                with self.assertRaisesRegex(PermissionError, 'final backup rmdir failure'):
                    deployment._uninstall(root, state)
            resumed = deployment._read_state(root)
            self.assertTrue(resumed['uninstall_progress']['windows_restored'])
            self.assertFalse(owned.exists())
            self.assertFalse(saved.exists())
            self.assertEqual(list(backup.iterdir()), [journal])
            self.assertEqual(entry.read_bytes(), b'ORIGINAL WINDOWS')
            # The retry must not ask for backups already safely reclaimed.
            with mock.patch.object(deployment, '_rollback_files',
                                   side_effect=AssertionError('must not restore twice')):
                report = deployment._uninstall(root, resumed)
            self.assertTrue(report['windows_restored'])
            self.assertFalse(backup.exists())

    def test_corrupt_original_backup_refuses_before_modifying_windows_or_checkpoint(self):
        with disposable_installation() as root:
            state, entry, owned, backup, journal, saved, extra = fixture(root)
            saved.write_bytes(b'CORRUPT BACKUP')
            checkpoint = journal.read_bytes()
            with self.assertRaisesRegex(deployment.DeploymentError, '备份损坏'):
                deployment._uninstall(root, state)
            self.assertEqual(entry.read_bytes(), b'NEO ENTRY')
            self.assertEqual(journal.read_bytes(), checkpoint)
            self.assertEqual(extra.read_bytes(), b'OLD ANIMATION')
            self.assertTrue(owned.exists())
            self.assertTrue(backup.exists())

    def test_retry_with_changed_windows_entry_preserves_checkpoint_and_both_trees(self):
        with disposable_installation() as root:
            state, entry, owned, backup, journal, saved, extra = fixture(root)
            state['phase'] = 'uninstalling'
            state['uninstall_progress'] = dict(windows_restored=True, firmware_removed=True)
            journal.write_text(json.dumps(state), encoding='utf-8')
            entry.write_bytes(b'EXTERNALLY CHANGED WINDOWS')
            with self.assertRaisesRegex(deployment.DeploymentError, '恢复后校验失败'):
                deployment._uninstall(root, deployment._read_state(root))
            self.assertEqual(entry.read_bytes(), b'EXTERNALLY CHANGED WINDOWS')
            self.assertTrue(deployment._read_state(root)['uninstall_progress']['windows_restored'])
            self.assertTrue(owned.exists())
            self.assertTrue(backup.exists())
            self.assertEqual(saved.read_bytes(), b'ORIGINAL WINDOWS')

    def test_missing_or_damaged_owned_resource_does_not_block_windows_restoration(self):
        for resource_state in ('missing', 'damaged'):
            with self.subTest(resource_state=resource_state), disposable_installation() as root:
                state, entry, owned, backup, journal, saved, extra = fixture(root)
                relative = extra.relative_to(root).as_posix()
                state['files'][relative] = dict(
                    existed=False, original_sha256='',
                    installed_sha256=hashlib.sha256(b'OLD ANIMATION').hexdigest())
                journal.write_text(json.dumps(state), encoding='utf-8')
                if resource_state == 'missing':
                    extra.unlink()
                else:
                    extra.write_bytes(b'DAMAGED ANIMATION')
                # A later arrangement inside the owned tree must be reclaimed
                # even though it was not present in the installation journal.
                unlisted = owned / 'another-layout.bin'
                unlisted.write_bytes(b'UNLISTED RESOURCE')
                report = deployment._uninstall(root, deployment._read_state(root))
                self.assertTrue(report['windows_restored'])
                self.assertEqual(entry.read_bytes(), b'ORIGINAL WINDOWS')
                self.assertFalse(owned.exists())
                self.assertFalse(backup.exists())


if __name__ == '__main__':
    unittest.main()
