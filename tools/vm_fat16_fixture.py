# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
# -*- coding: utf-8 -*-
"""FAT16 reader/rebuilder for disposable virtual-machine test fixtures.

The implementation deliberately avoids platform-specific mount tools. It reads the
existing FAT16 image into an ordered in-memory tree and can rebuild it at a larger
capacity while preserving the original boot-sector template.
"""
from __future__ import division

import math
import mmap
import os
import struct
import time
from collections import OrderedDict


class Fat16Error(Exception):
    pass


class FatNode(object):
    def __init__(self, name, is_dir=False, data=b"", attr=None):
        self.name = name
        self.is_dir = bool(is_dir)
        self.data = data if not is_dir else b""
        self.attr = attr if attr is not None else (0x10 if is_dir else 0x20)
        self.children = OrderedDict()
        self.parent = None
        self.first_cluster = 0
        self.clusters = []
        self.file_size = 0

    def add_child(self, node):
        if not self.is_dir:
            raise Fat16Error("Cannot add a child to a file")
        node.parent = self
        self.children[node.name] = node

    def get_path(self):
        parts = []
        node = self
        while node.parent is not None:
            parts.append(node.name)
            node = node.parent
        return "/" + "/".join(reversed(parts))

    def find(self, path):
        parts = [part for part in path.replace("\\", "/").split("/") if part]
        node = self
        for part in parts:
            if not node.is_dir or part not in node.children:
                raise KeyError(path)
            node = node.children[part]
        return node

    def ensure_dir(self, path):
        parts = [part for part in path.replace("\\", "/").split("/") if part]
        node = self
        for part in parts:
            child = node.children.get(part)
            if child is None:
                child = FatNode(part, True)
                node.add_child(child)
            elif not child.is_dir:
                raise Fat16Error("Path component is a file: %s" % part)
            node = child
        return node

    def set_file(self, path, data, attr=0x20):
        normalized = path.replace("\\", "/")
        parent_path, _, name = normalized.rpartition("/")
        parent = self.ensure_dir(parent_path)
        node = parent.children.get(name)
        if node is None:
            node = FatNode(name, False, data, attr)
            parent.add_child(node)
        else:
            if node.is_dir:
                raise Fat16Error("Cannot replace directory with file: %s" % path)
            node.data = data
            node.attr = attr
        return node

    def walk(self):
        for child in self.children.values():
            yield child
            if child.is_dir:
                for descendant in child.walk():
                    yield descendant


class Fat16Image(object):
    def __init__(self, raw, materialize_files=True):
        self.raw = raw
        self.materialize_files = bool(materialize_files)
        if len(raw) < 512 or raw[510:512] != b"\x55\xAA":
            raise Fat16Error("Not a bootable FAT image")
        self.bytes_per_sector = struct.unpack_from("<H", raw, 11)[0]
        self.sectors_per_cluster = raw[13]
        self.reserved_sectors = struct.unpack_from("<H", raw, 14)[0]
        self.fat_count = raw[16]
        self.root_entries = struct.unpack_from("<H", raw, 17)[0]
        total16 = struct.unpack_from("<H", raw, 19)[0]
        total32 = struct.unpack_from("<I", raw, 32)[0]
        self.total_sectors = total16 or total32
        self.sectors_per_fat = struct.unpack_from("<H", raw, 22)[0]
        self.root_sectors = int(math.ceil(self.root_entries * 32.0 / self.bytes_per_sector))
        self.fat_offset = self.reserved_sectors * self.bytes_per_sector
        self.root_offset = (
            self.reserved_sectors + self.fat_count * self.sectors_per_fat
        ) * self.bytes_per_sector
        self.data_offset = (
            self.reserved_sectors
            + self.fat_count * self.sectors_per_fat
            + self.root_sectors
        ) * self.bytes_per_sector
        if self.bytes_per_sector != 512 or self.fat_count < 1:
            raise Fat16Error("Unsupported FAT geometry")
        self.fat = raw[
            self.fat_offset : self.fat_offset + self.sectors_per_fat * self.bytes_per_sector
        ]
        self.root = FatNode("", True)
        root_raw = raw[
            self.root_offset : self.root_offset + self.root_sectors * self.bytes_per_sector
        ]
        self._read_directory(root_raw, self.root)

    @classmethod
    def from_file(cls, path, materialize_files=True):
        """Open a FAT16 image from disk.

        Large custom AnimeBootNeo DAT files can easily be hundreds of MiB.  When
        ``materialize_files`` is false, keep the image file memory-mapped and
        only materialize the directory metadata; callers can stream individual
        files with :meth:`iter_file_chunks`.  This avoids the old
        ``read-whole-DAT + copy every embedded file`` peak-memory explosion.
        """
        path = os.path.abspath(path)
        if materialize_files:
            with open(path, "rb") as stream:
                return cls(stream.read(), materialize_files=True)
        stream = open(path, "rb")
        try:
            raw = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
            image = cls(raw, materialize_files=False)
            image._mapped_stream = stream
            image._mapped_raw = raw
            return image
        except Exception:
            stream.close()
            raise

    def close(self):
        raw = getattr(self, "_mapped_raw", None)
        stream = getattr(self, "_mapped_stream", None)
        self._mapped_raw = None
        self._mapped_stream = None
        if raw is not None:
            try:
                raw.close()
            except (BufferError, ValueError):
                # A short-lived exported memoryview can postpone the actual
                # mapping release; the OS will reclaim it with the object.
                pass
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def read_file(self, path):
        """Read one embedded file without materializing unrelated DAT files."""
        node = self.root.find(path)
        if node.is_dir:
            raise Fat16Error("Path is a directory: %s" % path)
        if self.materialize_files:
            return bytes(node.data)
        size = int(getattr(node, "file_size", 0) or 0)
        output = bytearray(size)
        position = 0
        for chunk in self.iter_file_chunks(path):
            take = len(chunk)
            output[position:position + take] = chunk
            position += take
            del chunk
        if position != size:
            raise Fat16Error("FAT chain size mismatch: %s" % path)
        return bytes(output)

    @staticmethod
    def _align4(value):
        return (value + 3) & ~3

    def _fat_next(self, cluster):
        offset = cluster * 2
        if offset + 2 > len(self.fat):
            return 0xFFFF
        return struct.unpack_from("<H", self.fat, offset)[0]

    def _cluster_offset(self, cluster):
        return self.data_offset + (cluster - 2) * self.sectors_per_cluster * self.bytes_per_sector

    def _read_chain(self, first_cluster, size=None):
        if first_cluster < 2:
            return b""
        output = bytearray()
        seen = set()
        cluster = first_cluster
        cluster_bytes = self.sectors_per_cluster * self.bytes_per_sector
        while 2 <= cluster < 0xFFF8 and cluster not in seen:
            seen.add(cluster)
            offset = self._cluster_offset(cluster)
            output.extend(self.raw[offset : offset + cluster_bytes])
            cluster = self._fat_next(cluster)
        if size is not None:
            return bytes(output[:size])
        return bytes(output)

    @staticmethod
    def _decode_lfn(entries):
        chars = []
        for entry in reversed(entries):
            for offset, count in ((1, 5), (14, 6), (28, 2)):
                for index in range(count):
                    code = struct.unpack_from("<H", entry, offset + index * 2)[0]
                    if code in (0x0000, 0xFFFF):
                        return "".join(chars)
                    chars.append(chr(code))
        return "".join(chars)

    @staticmethod
    def _decode_short(entry):
        base = entry[0:8].decode("cp437", "replace").rstrip(" ")
        extension = entry[8:11].decode("cp437", "replace").rstrip(" ")
        return base + (("." + extension) if extension else "")

    def _read_directory(self, raw, parent):
        lfn_entries = []
        for offset in range(0, len(raw), 32):
            entry = raw[offset : offset + 32]
            if len(entry) < 32 or entry[0] == 0x00:
                break
            if entry[0] == 0xE5:
                lfn_entries = []
                continue
            attr = entry[11]
            if attr == 0x0F:
                lfn_entries.append(entry)
                continue
            name = self._decode_lfn(lfn_entries) if lfn_entries else self._decode_short(entry)
            lfn_entries = []
            if name in (".", "..") or attr & 0x08:
                continue
            low = struct.unpack_from("<H", entry, 26)[0]
            high = struct.unpack_from("<H", entry, 20)[0]
            first_cluster = low | (high << 16)
            size = struct.unpack_from("<I", entry, 28)[0]
            is_dir = bool(attr & 0x10)
            if is_dir:
                data = b""
            elif self.materialize_files:
                data = self._read_chain(first_cluster, size)
            else:
                data = b""
            node = FatNode(name, is_dir, data, attr)
            node.first_cluster = first_cluster
            node.file_size = 0 if is_dir else int(size)
            parent.add_child(node)
            if is_dir and first_cluster >= 2:
                self._read_directory(self._read_chain(first_cluster), node)

    def iter_file_chunks(self, path, chunk_size=1024 * 1024):
        node = self.root.find(path)
        if node.is_dir:
            raise Fat16Error("Path is a directory: %s" % path)
        remaining = int(getattr(node, "file_size", 0))
        if self.materialize_files and node.data:
            view = memoryview(node.data)
            position = 0
            while position < len(view):
                end = min(len(view), position + max(1, int(chunk_size)))
                yield view[position:end]
                position = end
            return
        cluster = int(node.first_cluster)
        seen = set()
        cluster_bytes = self.sectors_per_cluster * self.bytes_per_sector
        while remaining > 0 and 2 <= cluster < 0xFFF8 and cluster not in seen:
            seen.add(cluster)
            offset = self._cluster_offset(cluster)
            take = min(remaining, cluster_bytes)
            yield memoryview(self.raw)[offset:offset + take]
            remaining -= take
            cluster = self._fat_next(cluster)
        if remaining:
            raise Fat16Error("FAT chain ended before file data: %s" % path)

    def file_size(self, path):
        node = self.root.find(path)
        if node.is_dir:
            raise Fat16Error("Path is a directory: %s" % path)
        if self.materialize_files:
            return len(node.data)
        return int(getattr(node, "file_size", 0))

    def file_sha256(self, path):
        import hashlib
        digest = hashlib.sha256()
        for chunk in self.iter_file_chunks(path):
            digest.update(chunk)
        return digest.hexdigest()

    def describe(self):
        return {
            "bytes_per_sector": self.bytes_per_sector,
            "sectors_per_cluster": self.sectors_per_cluster,
            "reserved_sectors": self.reserved_sectors,
            "fat_count": self.fat_count,
            "root_entries": self.root_entries,
            "total_sectors": self.total_sectors,
            "sectors_per_fat": self.sectors_per_fat,
            "size": len(self.raw),
        }


_ALLOWED_SHORT = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789$%'-_@~`!(){}^#&"


def _sanitize_short(value):
    output = []
    for char in value.upper():
        output.append(char if char in _ALLOWED_SHORT else "_")
    return "".join(output)


def _is_simple_83(name):
    if name in (".", ".."):
        return True
    if name.count(".") > 1:
        return False
    base, dot, ext = name.rpartition(".")
    if not dot:
        base, ext = name, ""
    if not base or len(base) > 8 or len(ext) > 3:
        return False
    return _sanitize_short(base) == base.upper() and _sanitize_short(ext) == ext.upper()


def _short_alias(name, used):
    base, dot, ext = name.rpartition(".")
    if not dot:
        base, ext = name, ""
    clean_base = _sanitize_short(base).strip(" .") or "FILE"
    clean_ext = _sanitize_short(ext).strip(" .")[:3]
    direct = (clean_base[:8].ljust(8) + clean_ext[:3].ljust(3)).encode("ascii")
    if _is_simple_83(name) and direct not in used:
        used.add(direct)
        return direct, False
    stem = clean_base[:6]
    for number in range(1, 1000000):
        suffix = "~%d" % number
        candidate_base = (stem[: 8 - len(suffix)] + suffix).ljust(8)
        candidate = (candidate_base + clean_ext.ljust(3)).encode("ascii")
        if candidate not in used:
            used.add(candidate)
            return candidate, True
    raise Fat16Error("Unable to allocate an 8.3 alias for %s" % name)


def _lfn_checksum(short_name):
    result = 0
    for value in bytearray(short_name):
        result = (((result & 1) << 7) | (result >> 1)) + value
        result &= 0xFF
    return result


def _lfn_entries(name, short_name):
    encoded = name.encode("utf-16le")
    units = list(struct.unpack("<%dH" % (len(encoded) // 2), encoded))
    units.append(0x0000)
    while len(units) % 13:
        units.append(0xFFFF)
    count = len(units) // 13
    checksum = _lfn_checksum(short_name)
    output = []
    for index in range(count, 0, -1):
        sequence = index | (0x40 if index == count else 0)
        chunk = units[(index - 1) * 13 : index * 13]
        entry = bytearray(32)
        entry[0] = sequence
        entry[11] = 0x0F
        entry[12] = 0
        entry[13] = checksum
        entry[26:28] = b"\x00\x00"
        positions = list(range(1, 11, 2)) + list(range(14, 26, 2)) + list(range(28, 32, 2))
        for position, code in zip(positions, chunk):
            struct.pack_into("<H", entry, position, code)
        output.append(bytes(entry))
    return output


def _dos_timestamp(now=None):
    if now is None:
        now = time.localtime()
    year = max(1980, min(2107, now.tm_year))
    date = ((year - 1980) << 9) | (now.tm_mon << 5) | now.tm_mday
    clock = (now.tm_hour << 11) | (now.tm_min << 5) | (now.tm_sec // 2)
    return date, clock


def _normal_entry(short_name, node, first_cluster, size):
    entry = bytearray(32)
    entry[0:11] = short_name
    entry[11] = node.attr & 0x3F
    date, clock = _dos_timestamp()
    struct.pack_into("<H", entry, 14, clock)
    struct.pack_into("<H", entry, 16, date)
    struct.pack_into("<H", entry, 18, date)
    struct.pack_into("<H", entry, 22, clock)
    struct.pack_into("<H", entry, 24, date)
    struct.pack_into("<H", entry, 20, (first_cluster >> 16) & 0xFFFF)
    struct.pack_into("<H", entry, 26, first_cluster & 0xFFFF)
    struct.pack_into("<I", entry, 28, size)
    return bytes(entry)


def _dot_entry(name, cluster, parent_cluster):
    entry = bytearray(32)
    entry[0:11] = (name.ljust(11)).encode("ascii")
    entry[11] = 0x10
    date, clock = _dos_timestamp()
    struct.pack_into("<H", entry, 14, clock)
    struct.pack_into("<H", entry, 16, date)
    struct.pack_into("<H", entry, 18, date)
    struct.pack_into("<H", entry, 22, clock)
    struct.pack_into("<H", entry, 24, date)
    target = cluster if name == "." else parent_cluster
    struct.pack_into("<H", entry, 26, target & 0xFFFF)
    return bytes(entry)


def _entries_for_children(directory):
    used = set()
    output = []
    for child in directory.children.values():
        short_name, needs_lfn = _short_alias(child.name, used)
        if needs_lfn or child.name != Fat16Image._decode_short(short_name + b"\x00" * 21):
            output.extend(_lfn_entries(child.name, short_name))
        size = 0 if child.is_dir else len(child.data)
        output.append(_normal_entry(short_name, child, child.first_cluster, size))
    return output


def _required_clusters(root, cluster_bytes):
    """Return the exact cluster count required by ``root`` at this cluster size."""
    required = 0
    for node in root.walk():
        if node.is_dir:
            entry_count = _count_directory_entries(node, root=False)
            required += max(1, int(math.ceil(entry_count * 32.0 / cluster_bytes)))
        elif len(node.data):
            required += int(math.ceil(len(node.data) / float(cluster_bytes)))
    return required


def _geometry_for_spc(target_sectors, template, minimum_clusters_needed, spc):
    """Plan one FAT16 geometry without ever building a throw-away image.

    ``target_sectors`` is the user's requested minimum capacity.  The final file
    tree is already in memory when this function runs, so we can calculate the
    exact cluster demand for each candidate cluster size and solve the FAT/data
    overhead as a tiny fixed-point calculation.  Only the selected geometry is
    materialized by :func:`rebuild_fat16`.
    """
    bps = 512
    reserved = template.reserved_sectors
    fats = template.fat_count
    root_sectors = int(math.ceil(template.root_entries * 32.0 / bps))
    minimum_clusters = max(4085, int(minimum_clusters_needed))
    if minimum_clusters > 65524:
        return None

    # Seed from the exact content requirement, then fold in FAT overhead and the
    # user's requested minimum.  This converges in a handful of integer steps;
    # no image buffers are allocated here.
    spf = max(1, int(math.ceil((minimum_clusters + 2) * 2.0 / bps)))
    sectors = max(
        int(target_sectors),
        reserved + fats * spf + root_sectors + minimum_clusters * spc,
    )
    for _ in range(16):
        data_sectors = sectors - reserved - fats * spf - root_sectors
        clusters = max(0, data_sectors // spc)
        if clusters > 65524:
            return None
        addressed = max(minimum_clusters, clusters)
        new_spf = max(1, int(math.ceil((addressed + 2) * 2.0 / bps)))
        new_sectors = max(
            int(target_sectors),
            reserved + fats * new_spf + root_sectors + minimum_clusters * spc,
        )
        if new_spf == spf and new_sectors == sectors:
            data_sectors = sectors - reserved - fats * spf - root_sectors
            clusters = max(0, data_sectors // spc)
            if 4085 <= clusters <= 65524 and clusters >= minimum_clusters_needed:
                return sectors, spc, spf, clusters
            return None
        spf, sectors = new_spf, new_sectors
    raise Fat16Error("Unable to converge FAT16 geometry")


def _choose_geometry(target_bytes, template, root):
    bps = 512
    target_sectors = int(math.ceil(target_bytes / float(bps)))
    # 2/4/8/16/32 KiB clusters.  Select the first geometry that can represent
    # both the requested minimum volume size and the already-finalized content.
    for spc in (4, 8, 16, 32, 64):
        minimum_clusters_needed = _required_clusters(root, spc * bps)
        planned = _geometry_for_spc(
            target_sectors, template, minimum_clusters_needed, spc
        )
        if planned is not None:
            return planned
    raise Fat16Error("Requested FAT16 image is too large or cannot fit the content")

def _count_directory_entries(directory, root=False):
    count = 0 if root else 2
    used = set()
    for child in directory.children.values():
        short_name, needs_lfn = _short_alias(child.name, used)
        if needs_lfn or child.name != Fat16Image._decode_short(short_name + b"\x00" * 21):
            utf16_units = len(child.name.encode("utf-16le")) // 2 + 1
            count += int(math.ceil(utf16_units / 13.0))
        count += 1
    return count


def rebuild_fat16(template_image, root, target_bytes):
    """Rebuild a FAT16 image and return ``(raw, actual_size, expanded)``.

    ``target_bytes`` is treated as a minimum capacity. If the content does not fit,
    the output is automatically enlarged to the smallest practical FAT16 geometry.
    """
    if not isinstance(template_image, Fat16Image):
        raise TypeError("template_image must be Fat16Image")
    requested = max(int(target_bytes), len(template_image.raw))
    sectors, spc, spf, total_clusters = _choose_geometry(requested, template_image, root)
    bps = 512
    cluster_bytes = spc * bps
    root_sectors = int(math.ceil(template_image.root_entries * 32.0 / bps))

    # The geometry was planned from the final in-memory tree, so allocation is
    # guaranteed to fit before the only output image buffer is created.
    nodes = list(root.walk())
    required = _required_clusters(root, cluster_bytes)
    for node in nodes:
        node.clusters = []
    if required > total_clusters:
        raise Fat16Error("Internal FAT16 geometry planning error")

    next_cluster = 2
    for node in nodes:
        if node.is_dir:
            entry_count = _count_directory_entries(node, root=False)
            cluster_count = max(1, int(math.ceil(entry_count * 32.0 / cluster_bytes)))
        else:
            cluster_count = 0 if len(node.data) == 0 else int(math.ceil(len(node.data) / float(cluster_bytes)))
        if cluster_count:
            node.clusters = list(range(next_cluster, next_cluster + cluster_count))
            node.first_cluster = node.clusters[0]
            next_cluster += cluster_count
        else:
            node.first_cluster = 0

    total_size = sectors * bps
    output = bytearray(total_size)
    reserved_bytes = template_image.reserved_sectors * bps
    output[: min(reserved_bytes, len(template_image.raw))] = template_image.raw[:reserved_bytes]
    output[13] = spc
    if sectors <= 0xFFFF:
        struct.pack_into("<H", output, 19, sectors)
        struct.pack_into("<I", output, 32, 0)
    else:
        struct.pack_into("<H", output, 19, 0)
        struct.pack_into("<I", output, 32, sectors)
    struct.pack_into("<H", output, 22, spf)
    output[510:512] = b"\x55\xAA"

    fat_offset = template_image.reserved_sectors * bps
    root_offset = (template_image.reserved_sectors + template_image.fat_count * spf) * bps
    data_offset = (
        template_image.reserved_sectors + template_image.fat_count * spf + root_sectors
    ) * bps
    fat = bytearray(spf * bps)
    struct.pack_into("<H", fat, 0, 0xFFF8)
    struct.pack_into("<H", fat, 2, 0xFFFF)
    for node in nodes:
        for index, cluster in enumerate(node.clusters):
            value = node.clusters[index + 1] if index + 1 < len(node.clusters) else 0xFFFF
            struct.pack_into("<H", fat, cluster * 2, value)
    for copy_index in range(template_image.fat_count):
        start = fat_offset + copy_index * spf * bps
        output[start : start + len(fat)] = fat

    root_entries = _entries_for_children(root)
    root_blob = b"".join(root_entries)
    if len(root_blob) > root_sectors * bps:
        raise Fat16Error("Root directory contains too many entries")
    output[root_offset : root_offset + len(root_blob)] = root_blob

    def cluster_offset(cluster):
        return data_offset + (cluster - 2) * cluster_bytes

    for node in nodes:
        if not node.clusters:
            continue
        capacity = len(node.clusters) * cluster_bytes
        if node.is_dir:
            parent_cluster = 0 if node.parent is root else node.parent.first_cluster
            entries = [
                _dot_entry(".", node.first_cluster, parent_cluster),
                _dot_entry("..", node.first_cluster, parent_cluster),
            ]
            entries.extend(_entries_for_children(node))
            content = b"".join(entries)
            if len(content) > capacity:
                raise Fat16Error("Internal allocation error for %s" % node.get_path())
            # Directory blobs are tiny; the image buffer is already zero-filled.
            for index, cluster in enumerate(node.clusters):
                start = cluster_offset(cluster)
                chunk = content[index * cluster_bytes : (index + 1) * cluster_bytes]
                if chunk:
                    output[start : start + len(chunk)] = chunk
        else:
            content = node.data
            if len(content) > capacity:
                raise Fat16Error("Internal allocation error for %s" % node.get_path())
            # Do not create ``content + zero_padding`` for large initramfs files.
            # On 32-bit Python that temporary copy alone can consume hundreds of
            # MiB of scarce virtual address space.  Clusters are allocated
            # contiguously and ``output`` starts zero-filled, so copy only the
            # actual bytes and leave the tail padding untouched.
            view = memoryview(content)
            position = 0
            for cluster in node.clusters:
                if position >= len(view):
                    break
                take = min(cluster_bytes, len(view) - position)
                start = cluster_offset(cluster)
                output[start : start + take] = view[position:position + take]
                position += take

    # Keep the one final image buffer instead of duplicating it with bytes().
    # bytearray implements the buffer protocol used by hashlib/file.write and
    # saves one full-DAT allocation in 32-bit builds.
    return output, total_size, total_size > requested
