# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
"""Measure replaceable files by owned trees, independent of their inner layout."""

# 只读容量诊断：统计启动分区空闲字节，以及当前受管理安装在替换时能够净回收的字节。
# 固定所有目录按树统计，避免内部资源布局变化造成漏算；共享 EFI 路径只按安装记录确认归属。
# Windows 原入口与恢复所需备份不能当作可随意删除的动画空间；必须扣除保留/回滚开销。
# 估算供界面决策，安装阶段仍以绑定后的真实文件大小和当前剩余容量作最后检查。

import ctypes
import os
from pathlib import Path
import stat

OWNED_TREE = 'AnimeBootNeo'
BACKUP_TREE = 'AnimeBootNeo.Backup'


def allocation_unit(root):
    if os.name == 'nt':
        sectors, size, free, total = (ctypes.c_ulong() for _ in range(4))
        volume = Path(root).anchor
        if ctypes.windll.kernel32.GetDiskFreeSpaceW(str(volume), ctypes.byref(sectors),
                ctypes.byref(size), ctypes.byref(free), ctypes.byref(total)):
            return max(1, sectors.value * size.value)
    return 1


def safe_path(root, relative):
    root = Path(root).resolve()
    relative = Path(relative)
    if relative.is_absolute() or not relative.parts or any(p in ('.', '..') for p in relative.parts):
        raise ValueError('启动文件路径越界：' + str(relative))
    path = root
    for part in relative.parts:
        path = path / part
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('启动专属目录中存在链接或重解析点：' + str(path))
    if os.path.commonpath([str(root), str(path.resolve())]) != str(root):
        raise ValueError('启动文件路径越界：' + str(relative))
    return path


def _tree_entries(root, relative):
    """Snapshot every file and directory without following reparse points."""
    root = Path(root).resolve()
    start = safe_path(root, relative)
    if not start.exists():
        return [], []
    if not start.is_dir():
        raise ValueError('启动专属目录不是文件夹：' + str(start))
    found, directories = [], []
    pending = [start]
    while pending:
        directory = safe_path(root, pending.pop().relative_to(root))
        if not directory.is_dir():
            raise ValueError('启动专属目录在检查期间发生变化：' + str(directory))
        directories.append(directory)
        for item in directory.iterdir():
            item = safe_path(root, item.relative_to(root))
            if item.is_dir():
                pending.append(item)
            elif item.is_file():
                found.append(item)
            else:
                raise ValueError('启动专属目录包含非常规文件：' + str(item))
    return found, directories


def tree_files(root, relative):
    """Validate the entire tree before either accounting for or deleting it."""
    return _tree_entries(root, relative)[0]


def allocated_bytes(path, unit):
    path = Path(path)
    size = path.stat().st_size
    if os.name == 'nt':
        high = ctypes.c_ulong()
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.GetCompressedFileSizeW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
        api.GetCompressedFileSizeW.restype = ctypes.c_ulong
        ctypes.set_last_error(0)
        low = api.GetCompressedFileSizeW(str(path), ctypes.byref(high))
        if low != 0xffffffff or ctypes.get_last_error() == 0:
            size = (high.value << 32) | low
    return ((size + unit - 1) // unit) * unit


def installed_space(root, state=None, unit=None):
    root = Path(root).resolve()
    unit = unit or allocation_unit(root)
    paths = tree_files(root, OWNED_TREE) + tree_files(root, BACKUP_TREE)
    occupied = sum(allocated_bytes(path, unit) for path in paths)
    if state is None:
        return dict(existing_animebootneo_bytes=occupied, reclaimable_bytes=0,
                    restored_original_bytes=0, reclaim_status='unregistered' if occupied else 'not_installed',
                    allocation_unit=unit)
    if (state.get('format') != 'AnimeBootNeo-installed-3-canvases' or state.get('app_version') != '2610.3'
            or state.get('phase') != 'complete'):
        raise ValueError('已有安装记录不完整，请先禁用并恢复 Windows 启动入口后重新估算。')
    restored = 0
    scratch = 0
    for relative, record in state['files'].items():
        path = safe_path(root, relative)
        dedicated = str(relative).replace('\\', '/').startswith(OWNED_TREE + '/')
        if dedicated:
            if record.get('existed'):
                raise ValueError('专属目录记录包含原有外部文件，不能整目录回收。')
        elif path.is_file():
            # EFI/Microsoft, EFI/fedora and bootmgr are shared locations: only
            # the exact journaled files count, never their containing folders.
            occupied += allocated_bytes(path, unit)
        if record.get('existed'):
            original = safe_path(root, BACKUP_TREE + '/files/' + relative)
            if not original.is_file():
                raise ValueError('原启动入口备份缺失，不能计算可靠的替换空间：' + relative)
            # Restoration writes ordinary bytes; a compressed backup does not
            # imply the restored Windows entry can occupy its compressed size.
            size = ((original.stat().st_size + unit - 1) // unit) * unit
            restored += size
            scratch = max(scratch, size)
    return dict(existing_animebootneo_bytes=occupied,
                reclaimable_bytes=occupied-restored, restored_original_bytes=restored,
                rollback_scratch_bytes=scratch, reclaim_status='owned', allocation_unit=unit)


def remove_owned_tree(root, relative, clear_readonly, preserve_files=()):
    """Remove an owned tree, optionally keeping a final recovery checkpoint.

    All paths and attributes are checked before the first deletion. A preserved
    checkpoint and its parent directories survive every cleanup attempt; the
    caller can remove that checkpoint only after validating Windows restoration.
    """
    if relative not in (OWNED_TREE, BACKUP_TREE):
        raise ValueError('不是程序专属目录')
    root = Path(root).resolve()
    paths, directories = _tree_entries(root, relative)
    target = safe_path(root, relative)
    preserved = set()
    keep_directories = set()
    for name in preserve_files:
        name = Path(name)
        if name.is_absolute() or not name.parts or any(part in ('.', '..') for part in name.parts):
            raise ValueError('恢复记录保留路径越界：' + str(name))
        checkpoint = safe_path(root, Path(relative) / name)
        if checkpoint not in paths:
            raise ValueError('待保留的恢复记录不是现有普通文件：' + str(checkpoint))
        preserved.add(checkpoint)
        directory = checkpoint.parent
        while directory != target.parent:
            keep_directories.add(directory)
            directory = directory.parent

    # READONLY also prevents RemoveDirectoryW from deleting an empty directory.
    # Clear every directory before unlinking anything, so an attribute failure
    # cannot consume state.json or an original Windows-entry backup.
    for path in directories + paths:
        clear_readonly(safe_path(root, path.relative_to(root)))
    removed_files, removed_directories = 0, 0
    for path in paths:
        if path in preserved:
            continue
        path = safe_path(root, path.relative_to(root))
        path.unlink()
        removed_files += 1
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        if directory in keep_directories:
            continue
        directory = safe_path(root, directory.relative_to(root))
        directory.rmdir()
        removed_directories += 1

    remaining_files, remaining_directories = _tree_entries(root, relative)
    if set(remaining_files) != preserved or set(remaining_directories) != keep_directories:
        raise OSError('启动专属目录清理后仍有未移除内容：' + str(target))
    return dict(removed=not target.exists(), removed_files=removed_files,
                removed_directories=removed_directories,
                remaining_files=sorted(path.relative_to(target).as_posix() for path in remaining_files))
