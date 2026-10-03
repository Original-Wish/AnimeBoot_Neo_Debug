# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心

# 动画二进制容器与Linux newc归档。FileSlice表示磁盘文件区间，避免把整段动画读入32位Python内存。
# newc使用ASCII十六进制头和4字节对齐；大小/偏移需检查，跨段读写不能信任输入声明的长度。
# JPEG按32帧一块独立XZ压缩并保存索引，播放器可提前解压后续块；QOI采用无损逐帧数据。
# 编码、索引、帧序、对齐必须与 assets/linux/animeboot_player.c 的读取规则同步变更，否则可能能构建却不能播放。

import concurrent.futures, hashlib, io, lzma, os, shutil, stat, struct, tempfile, time
_COPY_CHUNK=1024*1024
JPEG_JCX_MAGIC=b"ANBJCX2\0"
JPEG_JCX_VERSION=2
JPEG_JCX_ENTRY_SIZE=32
JPEG_JCX_CHUNK_FRAMES=32
JPEG_JCX_FLAG_XZ=1
class InitramfsPatchError(Exception):
    pass


def _align4(value):
    return (value + 3) & ~3


class FileSlice(object):
    __slots__ = ("path", "offset", "size")

    def __init__(self, path, offset=0, size=None):
        self.path = os.path.abspath(path)
        self.offset = int(offset)
        if size is None:
            size = os.path.getsize(self.path) - self.offset
        self.size = int(size)


class CpioEntry(object):
    __slots__ = (
        "name", "data", "ino", "mode", "uid", "gid", "nlink", "mtime",
        "devmajor", "devminor", "rdevmajor", "rdevminor", "check",
    )

    def __init__(self, name, data=b"", ino=0, mode=0, uid=0, gid=0, nlink=1,
                 mtime=0, devmajor=0, devminor=0, rdevmajor=0, rdevminor=0, check=0):
        self.name = name
        self.data = data
        self.ino = int(ino)
        self.mode = int(mode)
        self.uid = int(uid)
        self.gid = int(gid)
        self.nlink = int(nlink)
        self.mtime = int(mtime)
        self.devmajor = int(devmajor)
        self.devminor = int(devminor)
        self.rdevmajor = int(rdevmajor)
        self.rdevminor = int(rdevminor)
        self.check = int(check)


def _read_exact(stream, size):
    chunks = []
    remaining = int(size)
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise InitramfsPatchError("CPIO数据意外结束。")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def parse_newc(raw):
    """Compatibility parser for callers that already have a CPIO byte string."""
    entries = []
    position = 0
    while position + 110 <= len(raw):
        header = raw[position:position + 110]
        if header[:6] != b"070701":
            raise InitramfsPatchError("CPIO newc header missing at 0x%X" % position)
        values = [int(header[6 + index * 8:14 + index * 8], 16) for index in range(13)]
        ino, mode, uid, gid, nlink, mtime, file_size = values[:7]
        devmajor, devminor, rdevmajor, rdevminor, name_size, check = values[7:]
        name_start = position + 110
        name_end = name_start + name_size
        if name_size < 1 or name_end > len(raw) or raw[name_end - 1] != 0:
            raise InitramfsPatchError("CPIO entry name is invalid at 0x%X" % position)
        name = bytes(raw[name_start:name_end - 1]).decode("utf-8", "surrogateescape")
        data_start = _align4(name_end)
        data_end = data_start + file_size
        if data_end > len(raw):
            raise InitramfsPatchError("CPIO entry exceeds archive: %s" % name)
        entries.append(CpioEntry(
            name, bytes(raw[data_start:data_end]), ino, mode, uid, gid, nlink,
            mtime, devmajor, devminor, rdevmajor, rdevminor, check,
        ))
        position = _align4(data_end)
        if name == "TRAILER!!!":
            return entries
    raise InitramfsPatchError("CPIO TRAILER!!! was not found")


def parse_newc_file(path):
    """Parse metadata while keeping every member as a slice of a disk file."""
    entries = []
    file_size_total = os.path.getsize(path)
    with open(path, "rb") as stream:
        position = 0
        while position + 110 <= file_size_total:
            stream.seek(position)
            header = _read_exact(stream, 110)
            if header[:6] != b"070701":
                raise InitramfsPatchError("CPIO newc header missing at 0x%X" % position)
            values = [int(header[6 + index * 8:14 + index * 8], 16) for index in range(13)]
            ino, mode, uid, gid, nlink, mtime, member_size = values[:7]
            devmajor, devminor, rdevmajor, rdevminor, name_size, check = values[7:]
            if name_size < 1:
                raise InitramfsPatchError("CPIO entry name is invalid at 0x%X" % position)
            name_bytes = _read_exact(stream, name_size)
            if name_bytes[-1:] != b"\0":
                raise InitramfsPatchError("CPIO entry name is invalid at 0x%X" % position)
            name = name_bytes[:-1].decode("utf-8", "surrogateescape")
            name_end = position + 110 + name_size
            data_start = _align4(name_end)
            data_end = data_start + member_size
            if data_end > file_size_total:
                raise InitramfsPatchError("CPIO entry exceeds archive: %s" % name)
            entries.append(CpioEntry(
                name, FileSlice(path, data_start, member_size), ino, mode, uid,
                gid, nlink, mtime, devmajor, devminor, rdevmajor, rdevminor, check,
            ))
            position = _align4(data_end)
            if name == "TRAILER!!!":
                return entries
    raise InitramfsPatchError("CPIO TRAILER!!! was not found")


def _hex8(value):
    return ("%08X" % (int(value) & 0xFFFFFFFF)).encode("ascii")


def _source_size(data):
    if isinstance(data, FileSlice):
        return data.size
    return len(data)


def _source_sha256(data):
    digest = hashlib.sha256()
    if isinstance(data, FileSlice):
        with open(data.path, "rb") as stream:
            stream.seek(data.offset)
            remaining = data.size
            while remaining:
                chunk = stream.read(min(_COPY_CHUNK, remaining))
                if not chunk:
                    raise InitramfsPatchError("读取临时数据文件失败：%s" % data.path)
                digest.update(chunk)
                remaining -= len(chunk)
    else:
        digest.update(data)
    return digest.hexdigest()


def _source_to_bytes(data):
    if not isinstance(data, FileSlice):
        return bytes(data)
    with open(data.path, "rb") as stream:
        stream.seek(data.offset)
        return _read_exact(stream, data.size)


def _write_source(output, data):
    if isinstance(data, FileSlice):
        with open(data.path, "rb") as stream:
            stream.seek(data.offset)
            remaining = data.size
            while remaining:
                chunk = stream.read(min(_COPY_CHUNK, remaining))
                if not chunk:
                    raise InitramfsPatchError("读取临时数据文件失败：%s" % data.path)
                output.write(chunk)
                remaining -= len(chunk)
    else:
        output.write(data)


def build_newc(entries):
    """Compatibility in-memory builder; production uses streaming XZ output."""
    output = io.BytesIO()
    _write_newc(entries, output)
    return output.getvalue()


def _write_padding(output, count):
    if count:
        output.write(b"\0" * count)


def _write_newc(entries, output):
    position = 0
    for entry in entries:
        name_bytes = entry.name.encode("utf-8", "surrogateescape") + b"\0"
        member_size = _source_size(entry.data)
        fields = (
            entry.ino, entry.mode, entry.uid, entry.gid, entry.nlink, entry.mtime,
            member_size, entry.devmajor, entry.devminor, entry.rdevmajor,
            entry.rdevminor, len(name_bytes), entry.check,
        )
        header = b"070701" + b"".join(_hex8(value) for value in fields)
        output.write(header)
        output.write(name_bytes)
        position += len(header) + len(name_bytes)
        padding = _align4(position) - position
        _write_padding(output, padding)
        position += padding
        _write_source(output, entry.data)
        position += member_size
        padding = _align4(position) - position
        _write_padding(output, padding)
        position += padding
    return position


def _ensure_file_slice(source, work_dir):
    if isinstance(source, FileSlice):
        return source
    path = os.path.join(work_dir, "animation_source.anbf")
    with open(path, "wb") as stream:
        _write_source(stream, source)
    return FileSlice(path)


def _parse_anbf_frame_slices(source):
    """Return (header_bytes, codec, frame_slices) without materializing ANBF."""
    source = source if isinstance(source, FileSlice) else None
    if source is None:
        raise InitramfsPatchError("内部错误：ANBF帧切片要求文件源")
    with open(source.path, "rb") as stream:
        stream.seek(source.offset)
        base = _read_exact(stream, 40)
        if base[:8] != b"ANBF262\0":
            raise InitramfsPatchError("animation.anbf magic不正确")
        version = struct.unpack_from("<I", base, 8)[0]
        if version != 2:
            raise InitramfsPatchError("animation.anbf版本不支持：%d" % version)
        extra = _read_exact(stream, 4)
        header = base + extra
        codec = struct.unpack_from("<I", extra, 0)[0]
        if codec not in (0, 1):
            raise InitramfsPatchError("animation.anbf codec不支持：%d" % codec)
        frame_count = struct.unpack_from("<I", base, 20)[0]
        if not frame_count or frame_count > 100000:
            raise InitramfsPatchError("animation.anbf帧数不正确")
        pos = source.offset + len(header)
        end = source.offset + source.size
        frames = []
        for index in range(frame_count):
            if pos + 4 > end:
                raise InitramfsPatchError("animation.anbf帧表提前结束：%d" % index)
            stream.seek(pos)
            size = struct.unpack("<I", _read_exact(stream, 4))[0]
            pos += 4
            if not size or pos + size > end:
                raise InitramfsPatchError("animation.anbf帧尺寸不正确：%d" % index)
            frames.append(FileSlice(source.path, pos, size))
            pos += size
        return header, codec, frames


def _compress_jpeg_chunk(args):
    chunk_frames, preset = args
    raw = io.BytesIO()
    for frame_slice in chunk_frames:
        raw.write(struct.pack("<I", frame_slice.size))
        with open(frame_slice.path, "rb") as stream:
            stream.seek(frame_slice.offset)
            remaining = frame_slice.size
            while remaining:
                data = stream.read(min(_COPY_CHUNK, remaining))
                if not data:
                    raise InitramfsPatchError("JPEG chunk读取提前结束")
                raw.write(data)
                remaining -= len(data)
    raw_bytes = raw.getvalue()
    compressed = lzma.compress(raw_bytes, format=lzma.FORMAT_XZ, check=lzma.CHECK_NONE, preset=preset)
    if len(compressed) >= len(raw_bytes):
        return raw_bytes, 0, len(raw_bytes)
    return compressed, JPEG_JCX_FLAG_XZ, len(raw_bytes)


def _build_jpeg_chunked_xz_container(source, output_path, chunk_frames=JPEG_JCX_CHUNK_FRAMES, preset=1, cancel_check=None, progress=None):
    """Build JPEG chunks that can be inflated concurrently on Linux.

    Chunk 0 intentionally contains only frame 0 so FASTFIRST never waits for a
    full multi-frame chunk. Remaining frames use fixed-size chunks (default 23)
    to preserve useful inter-frame XZ redundancy while allowing parallel XZ.
    """
    header, codec, frames = _parse_anbf_frame_slices(source)
    if codec != 1:
        raise InitramfsPatchError("JPEG Chunked-XZ容器只能用于JPEG ANBF")
    chunk_frames = max(4, min(128, int(chunk_frames)))
    groups = [frames[:1]]
    for start in range(1, len(frames), chunk_frames):
        groups.append(frames[start:start + chunk_frames])
    fixed_size = struct.calcsize("<8s6IQ")
    index_size = len(groups) * JPEG_JCX_ENTRY_SIZE
    data_base = fixed_size + len(header) + index_size
    compression_t0 = time.perf_counter()
    cpu = max(1, int(os.cpu_count() or 1))
    largest = max(sum(4 + frame.size for frame in group) for group in groups)
    workers = min(6, cpu, len(groups), max(1, (192 * 1024 * 1024) // max(1, largest * 3)))
    def cancel():
        if callable(cancel_check) and cancel_check():
            raise InitramfsPatchError('用户已放弃制作。')
    entries = []
    rel = 0
    xz_chunks = raw_chunks = 0
    first_frame = 0
    blob_path = output_path + ".blobs"
    with open(blob_path, "wb") as blob, concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        pending = []
        next_group = 0
        try:
            for index, group in enumerate(groups):
                cancel()
                while next_group < len(groups) and len(pending) < workers * 2:
                    pending.append(pool.submit(_compress_jpeg_chunk, (groups[next_group], preset)))
                    next_group += 1
                future = pending.pop(0)
                while True:
                    cancel()
                    try:
                        stored, flags, raw_size = future.result(timeout=0.1)
                        break
                    except concurrent.futures.TimeoutError:
                        continue
                blob.write(stored)
                entries.append((data_base + rel, len(stored), raw_size, first_frame, len(group), flags, 0))
                rel += len(stored)
                first_frame += len(group)
                if flags & JPEG_JCX_FLAG_XZ:
                    xz_chunks += 1
                else:
                    raw_chunks += 1
                del stored, future
                if callable(progress):
                    progress('正在压缩动画块：%d/%d，%d 个并行线程…' % (index + 1, len(groups), workers))
        finally:
            for future in pending:
                future.cancel()
    compression_ms = (time.perf_counter() - compression_t0) * 1000.0
    raw_frame_stream_bytes = sum(4 + item.size for item in frames)
    with open(output_path, "wb") as out:
        out.write(struct.pack("<8s6IQ", JPEG_JCX_MAGIC, JPEG_JCX_VERSION, len(header), len(frames),
                              len(entries), JPEG_JCX_ENTRY_SIZE, chunk_frames, raw_frame_stream_bytes))
        out.write(header)
        for entry in entries:
            out.write(struct.pack("<Q6I", *entry))
        with open(blob_path, "rb") as blob:
            shutil.copyfileobj(blob, out, length=_COPY_CHUNK)
    os.remove(blob_path)
    return {
        "path": output_path,
        "stored_size": os.path.getsize(output_path),
        "raw_frame_stream_bytes": raw_frame_stream_bytes,
        "frame_count": len(frames),
        "chunk_count": len(entries),
        "chunk_frames": chunk_frames,
        "xz_chunks": xz_chunks,
        "raw_chunks": raw_chunks,
        "compression_ms": compression_ms,
        "preset": preset,
        "compression_workers": workers,
        "max_pending_chunks": workers * 2,
    }


def _verify_jpeg_chunked_xz_matches_source(container_path, animation_size, animation_sha256):
    digest = hashlib.sha256()
    total = 0
    with open(container_path, "rb") as stream:
        fixed_size = struct.calcsize("<8s6IQ")
        fixed = _read_exact(stream, fixed_size)
        magic, version, header_size, frame_count, chunk_count, entry_size, chunk_frames, raw_stream_bytes = struct.unpack("<8s6IQ", fixed)
        if magic != JPEG_JCX_MAGIC or version != JPEG_JCX_VERSION or entry_size != JPEG_JCX_ENTRY_SIZE:
            raise InitramfsPatchError("JPEG Chunked-XZ容器头校验失败")
        header = _read_exact(stream, header_size)
        if len(header) != 44 or header[:8] != b"ANBF262\0" or struct.unpack_from("<I", header, 20)[0] != frame_count:
            raise InitramfsPatchError("JPEG Chunked-XZ内ANBF头校验失败")
        if len(header) == 44 and struct.unpack_from("<I", header, 40)[0] != 1:
            raise InitramfsPatchError("JPEG Chunked-XZ内codec不是JPEG")
        digest.update(header); total += len(header)
        entries = [struct.unpack("<Q6I", _read_exact(stream, entry_size)) for _ in range(chunk_count)]
        expected_frame = 0
        raw_seen = 0
        for offset, stored_size, raw_size, first_frame, count, flags, reserved in entries:
            if first_frame != expected_frame or not count or (flags & ~JPEG_JCX_FLAG_XZ):
                raise InitramfsPatchError("JPEG Chunked-XZ索引顺序/flags校验失败")
            stream.seek(offset)
            stored = _read_exact(stream, stored_size)
            if flags & JPEG_JCX_FLAG_XZ:
                try:
                    raw = lzma.decompress(stored, format=lzma.FORMAT_XZ)
                except lzma.LZMAError as exc:
                    raise InitramfsPatchError("JPEG Chunked-XZ块解压校验失败：%s" % exc)
            else:
                raw = stored
            if len(raw) != raw_size:
                raise InitramfsPatchError("JPEG Chunked-XZ块长度校验失败")
            pos = 0
            for _ in range(count):
                if pos + 4 > len(raw):
                    raise InitramfsPatchError("JPEG Chunked-XZ块帧表提前结束")
                size = struct.unpack_from("<I", raw, pos)[0]; pos += 4
                if not size or pos + size > len(raw):
                    raise InitramfsPatchError("JPEG Chunked-XZ块帧尺寸错误")
                digest.update(struct.pack("<I", size)); digest.update(raw[pos:pos+size])
                total += 4 + size; raw_seen += 4 + size; pos += size
                expected_frame += 1
            if pos != len(raw):
                raise InitramfsPatchError("JPEG Chunked-XZ块尾部存在多余数据")
        if expected_frame != frame_count or raw_seen != raw_stream_bytes:
            raise InitramfsPatchError("JPEG Chunked-XZ帧数/原始字节数校验失败")
    if total != animation_size or digest.hexdigest() != animation_sha256:
        raise InitramfsPatchError("JPEG Chunked-XZ还原后的ANBF size/hash不一致")


