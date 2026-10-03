# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
# -*- coding: utf-8 -*-
"""Build the current full-colour Linux-initramfs animation payload.

The signed GRUB no longer attempts to render animation frames.  GRUB first
shows the normal static menu.  Only the ``System`` menuentry boots the existing
signed Linux kernel with ``AnimeBootNeoMode=WindowsAnimation``.  The initramfs PID
1 wrapper then displays this payload through Linux framebuffer/GOP, warm-reboots.  The next GRUB pass consumes its own pre-saved one-shot flag and
uses the install-time literal Windows chainloader path.

The stable path uses QOI because it is lossless and extremely fast to decode.
This experimental branch also supports per-frame JPEG inside ANBF v2 for much
smaller photographic/video payloads; the static Linux player contains both decoders.
"""

# Windows端动画处理流水线：探测素材时间轴 → 选择帧 → 解码/缩放合成 → QOI或JPEG编码 → 顺序索引容器。
# 视频优先FFmpeg；GIF/APNG需尊重帧时长、透明和dispose规则，不能仅按帧编号等间隔抽样。
# CPU密集步骤用受内存上限约束的进程并行，I/O按流处理；32位打包进程不能一次持有整段高分辨率帧。
# 子进程通过 app.py 的专用worker参数进入，绝不能再次打开GUI；取消要终止并回收所有子进程和临时文件。
# 估算只抽代表帧，不等同实际大小；正式编码按最终输出累计统计。仅当前编码的估算不可顺带编码另一种格式。

from __future__ import division

import glob
import io
import json
import lzma
import os
import re
import shutil
import sys
import struct
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

from PIL import Image


class AnimationError(Exception):
    pass


def _check_cancel(cancel_check):
    if callable(cancel_check) and cancel_check():
        raise AnimationError("用户已放弃制作。")


_IMAGE_EXTENSIONS = (".gif", ".webp", ".png", ".apng")
_VIDEO_EXTENSIONS = (".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".mpg", ".mpeg")
ANBF_MAGIC = b"ANBF262\0"
ANBF_VERSION = 2
ANBF_CODEC_QOI = 0
ANBF_CODEC_JPEG = 1
ANBF_CODECS = {"qoi": ANBF_CODEC_QOI, "jpeg": ANBF_CODEC_JPEG}
FIT_MODES = {"cover": 0, "contain": 1, "stretch": 2}


def animation_enabled(project):
    spec = project.get("boot_animation", {})
    return bool(spec.get("enabled") and spec.get("source"))


def _natural_key(path):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", os.path.basename(path))]



def _even_indices(total, maximum):
    total, maximum = int(total), max(1, int(maximum))
    if total <= maximum:
        return list(range(total))
    if maximum == 1:
        return [0]
    return [int(round(index * (total - 1) / float(maximum - 1))) for index in range(maximum)]


def _selected_indices(total, maximum, sampling_mode):
    total, maximum = max(0, int(total)), max(1, int(maximum))
    if total <= maximum:
        return list(range(total))
    if str(sampling_mode or "head") == "uniform":
        return _even_indices(total, maximum)
    return list(range(maximum))


def _image_timeline(path):
    info = os.stat(path)
    return _cached_image_timeline(os.path.abspath(path), info.st_size, info.st_mtime_ns)


@lru_cache(maxsize=32)
def _cached_image_timeline(path, file_size, modified_ns):
    """Return (frame_count, durations_ms, duration_seconds, average_fps)."""
    if os.path.splitext(path)[1].lower() == '.gif':
        durations = _gif_frame_durations(path)
        total = len(durations)
        seconds = sum(durations) / 1000.0 if total > 1 else 0.0
        return total, durations, seconds, total / seconds if seconds else 0.0
    try:
        with Image.open(path) as image:
            total = int(getattr(image, "n_frames", 1) or 1)
            if total <= 1:
                return total, [0.0], 0.0, 0.0
            durations = []
            for index in range(total):
                image.seek(index)
                try:
                    duration = float(image.info.get("duration", 0.0) or 0.0)
                except (TypeError, ValueError):
                    duration = 0.0
                # Animated GIF/APNG/WebP normally carries per-frame duration.
                # 100 ms is a conservative fallback when a malformed frame omits it.
                if duration <= 0.0:
                    duration = 100.0
                durations.append(duration)
    except Exception as exc:
        raise AnimationError("无法打开动画图片：%s" % exc)
    duration_seconds = sum(durations) / 1000.0
    fps = (float(total) / duration_seconds) if duration_seconds > 0.0 else 0.0
    return total, durations, duration_seconds, fps


def _gif_frame_durations(path):
    info = os.stat(path)
    return _gif_control_info(os.path.abspath(path), info.st_size, info.st_mtime_ns)[0]


@lru_cache(maxsize=32)
def _gif_control_info(path, file_size, modified_ns):
    """Read GIF control blocks without decoding/compositing its image pixels."""
    durations = []
    transparent = False
    restore_previous = False
    pending = 100.0
    with open(path, 'rb') as stream:
        def exact(count):
            data = stream.read(count)
            if len(data) != count:
                raise AnimationError('GIF 数据提前结束。')
            return data
        def blocks():
            while True:
                count = exact(1)[0]
                if not count:
                    return
                exact(count)
        header = exact(13)
        if header[:6] not in (b'GIF87a', b'GIF89a'):
            raise AnimationError('GIF 文件头无效。')
        packed = header[10]
        if packed & 128:
            exact(3 * (1 << ((packed & 7) + 1)))
        while True:
            marker = exact(1)[0]
            if marker == 0x3b:
                break
            if marker == 0x21:
                label = exact(1)[0]
                if label == 0xf9:
                    if exact(1)[0] != 4:
                        raise AnimationError('GIF 图像控制块无效。')
                    control = exact(4)
                    transparent = transparent or bool(control[0] & 1)
                    restore_previous = restore_previous or ((control[0] >> 2) & 7) >= 3
                    pending = float(struct.unpack_from('<H', control, 1)[0] * 10) or 100.0
                    if exact(1) != b'\0':
                        raise AnimationError('GIF 控制块没有结束。')
                else:
                    blocks()
            elif marker == 0x2c:
                descriptor = exact(9)
                if descriptor[8] & 128:
                    exact(3 * (1 << ((descriptor[8] & 7) + 1)))
                exact(1)
                blocks()
                durations.append(pending)
                pending = 100.0
            else:
                raise AnimationError('GIF 包含无效的数据块。')
    if not durations:
        raise AnimationError('GIF 不包含可播放图像。')
    return durations, transparent, restore_previous


def _probe_video_metadata(path):
    """Probe FPS/duration without decoding the whole video."""
    metadata = {"fps": 0.0, "duration": 0.0, "frame_count": 0}
    try:
        import imageio_ffmpeg
        reader = imageio_ffmpeg.read_frames(path)
        try:
            meta = next(reader)
            metadata["fps"] = float(meta.get("fps", 0.0) or 0.0)
            metadata["duration"] = float(meta.get("duration", 0.0) or 0.0)
            metadata["size"] = list(meta.get("size") or meta.get("source_size") or [])
        finally:
            close = getattr(reader, "close", None)
            if callable(close):
                close()
    except Exception:
        pass
    if metadata["fps"] <= 0.0 or metadata["duration"] <= 0.0:
        ffmpeg = _find_ffmpeg()
        if ffmpeg:
            try:
                process = subprocess.Popen(
                    [ffmpeg, "-hide_banner", "-i", path],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                _out, err = process.communicate(timeout=15.0)
                text = err.decode("utf-8", "replace")
                if metadata["fps"] <= 0.0:
                    match = re.search(r"(?:,|\\s)([0-9]+(?:\\.[0-9]+)?)\\s+fps(?:,|\\s)", text)
                    if match:
                        metadata["fps"] = float(match.group(1))
                if metadata["duration"] <= 0.0:
                    match = re.search(r"Duration:\\s*(\\d+):(\\d+):([0-9]+(?:\\.[0-9]+)?)", text)
                    if match:
                        metadata["duration"] = int(match.group(1)) * 3600.0 + int(match.group(2)) * 60.0 + float(match.group(3))
            except Exception:
                pass
    if metadata["fps"] > 0.0 and metadata["duration"] > 0.0:
        metadata["frame_count"] = max(1, int(round(metadata["fps"] * metadata["duration"])))
    return metadata


def probe_animation_source(path):
    """Return lightweight source metadata for the animation settings UI."""
    path = os.path.abspath(str(path or ""))
    if not path or not os.path.exists(path):
        raise AnimationError("动画来源不存在。")
    if os.path.isdir(path):
        names = [name for name in os.listdir(path) if os.path.isfile(os.path.join(path, name)) and name.lower().endswith(".png")]
        names.sort(key=_natural_key)
        if not names:
            raise AnimationError("所选文件夹中没有PNG帧。")
        return {"kind": "png_sequence", "frame_count": len(names), "fps": 0.0, "duration": 0.0}
    suffix = os.path.splitext(path)[1].lower()
    if suffix in _IMAGE_EXTENSIONS:
        total, _durations, duration, fps = _image_timeline(path)
        return {"kind": "animated_image" if total > 1 else "image", "frame_count": total, "fps": fps, "duration": duration}
    if suffix in _VIDEO_EXTENSIONS:
        meta = _probe_video_metadata(path)
        return {"kind": "video", "frame_count": int(meta.get("frame_count", 0)), "fps": float(meta.get("fps", 0.0)), "duration": float(meta.get("duration", 0.0))}
    raise AnimationError("不支持的动画来源格式：%s" % suffix)


def _image_source_indices(path, maximum, extraction_fps, sampling_mode):
    total, durations, duration_seconds, source_fps = _image_timeline(path)
    if total <= 1 or duration_seconds <= 0.0:
        candidate_source_indices = [0]
    else:
        extraction_fps = max(0.1, float(extraction_fps))
        candidate_count = max(1, int(__import__('math').ceil(duration_seconds * extraction_fps - 1e-9)))
        cumulative = []
        running = 0.0
        for duration in durations:
            running += float(duration) / 1000.0
            cumulative.append(running)
        import bisect
        candidate_source_indices = []
        for candidate_index in range(candidate_count):
            when = min(max(0.0, candidate_index / extraction_fps), max(0.0, duration_seconds - 1e-9))
            source_index = bisect.bisect_right(cumulative, when + 1e-9)
            candidate_source_indices.append(min(total - 1, source_index))
    chosen = _selected_indices(len(candidate_source_indices), maximum, sampling_mode)
    source_indices = [candidate_source_indices[index] for index in chosen]
    return source_indices, total


def _frames_from_image(path, maximum, extraction_fps, sampling_mode, cancel_check=None):
    source_indices, total = _image_source_indices(path, maximum, extraction_fps, sampling_mode)

    def iterate():
        try:
            with Image.open(path) as image:
                for index in source_indices:
                    _check_cancel(cancel_check)
                    image.seek(index)
                    yield image.convert("RGBA").copy()
        except Exception as exc:
            raise AnimationError("无法读取动画图片帧：%s" % exc)

    kind = "animated_image" if total > 1 else "image"
    return iterate(), total, kind


def _frames_from_directory(path, maximum, sampling_mode, cancel_check=None):
    names = []
    for name in os.listdir(path):
        full = os.path.join(path, name)
        if os.path.isfile(full) and name.lower().endswith(".png"):
            names.append(full)
    names.sort(key=_natural_key)
    if not names:
        raise AnimationError("所选文件夹中没有PNG帧。")
    selected = [names[index] for index in _selected_indices(len(names), maximum, sampling_mode)]

    def iterate():
        for full in selected:
            _check_cancel(cancel_check)
            try:
                with Image.open(full) as image:
                    yield image.convert("RGBA").copy()
            except Exception as exc:
                raise AnimationError("无法打开PNG帧 %s：%s" % (full, exc))

    return iterate(), len(names), "png_sequence"


def _find_ffmpeg():
    """Locate bundled FFmpeg, then the installed source-run dependency."""
    candidates = []

    # PyInstaller one-folder/one-file extraction root. collect_all(imageio_ffmpeg)
    # keeps the wheel's platform-specific executable below this directory.
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        candidates.extend(glob.glob(os.path.join(frozen_root, "imageio_ffmpeg", "binaries", "ffmpeg*.exe")))
        candidates.extend(glob.glob(os.path.join(frozen_root, "**", "imageio_ffmpeg", "binaries", "ffmpeg*.exe"), recursive=True))

    # The imageio-ffmpeg wheel is a required build dependency and its
    # Win32 wheel contains the FFmpeg binary used by the packaged application.
    try:
        import imageio_ffmpeg
        candidates.append(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        pass

    found = shutil.which("ffmpeg")
    if found:
        candidates.append(found)

    seen = set()
    for path in candidates:
        if not path:
            continue
        path = os.path.abspath(path)
        key = os.path.normcase(path)
        if key in seen:
            continue
        seen.add(key)
        if os.path.isfile(path):
            return path
    return None



def _native_video_rate_selected(extraction_fps, source_fps):
    """Return True when the UI value is effectively the video's native rate.

    The UI stores three decimals (29.970/23.976/59.940), while the source may
    really be a rational such as 30000/1001.  Treating that rounded decimal as a
    brand-new CFR and feeding it back through FFmpeg's fps filter can shift the
    first frame and drop the EOF frame.  Native-rate extraction therefore
    bypasses the fps filter and preserves the decoder's original frame order.
    """
    try:
        extraction_fps = float(extraction_fps)
        source_fps = float(source_fps)
    except (TypeError, ValueError):
        return False
    if extraction_fps <= 0.0 or source_fps <= 0.0:
        return False
    tolerance = max(0.0005, source_fps * 0.00005)
    return abs(extraction_fps - source_fps) <= tolerance


def _frames_from_video(path, maximum, extraction_fps, sampling_mode, cancel_check=None, metadata=None):
    """Decode video frames through a raw RGBA pipe instead of temporary PNGs.

    Native-rate mode deliberately avoids FFmpeg's fps filter.  This fixes the
    30000/1001 -> 29.970000 round-trip that could turn a real 300-frame clip
    into 299 frames and could replace the true first decoded frame.  When the
    user chooses a genuinely different extraction FPS we still resample, but
    keep more precision and explicitly pass the EOF action.
    """
    try:
        import imageio_ffmpeg
    except Exception:
        raise AnimationError("没有检测到内置 FFmpeg 组件，请使用完整便携版或重新构建程序。")

    maximum = max(1, int(maximum))
    extraction_fps = max(0.1, float(extraction_fps))
    metadata = metadata or _probe_video_metadata(path)
    duration = float(metadata.get("duration", 0.0) or 0.0)
    source_total = int(metadata.get("frame_count", 0) or 0)
    source_fps = float(metadata.get("fps", 0.0) or 0.0)
    mode = str(sampling_mode or "head")
    native_rate = _native_video_rate_selected(extraction_fps, source_fps)

    output_params = []
    source_kind = "video_native" if native_rate else "video_%.3ffps" % extraction_fps

    if native_rate:
        # Original/native mode: one decoded source frame in -> one candidate
        # frame out.  No fps lattice means no rational-rate rounding, no
        # start_time=0 padding/trimming, and no EOF off-by-one from fps filter.
        if mode == "uniform" and source_total > maximum:
            indices = _even_indices(source_total, maximum)
            select_expr = "+".join("eq(n,%d)" % index for index in indices)
            output_params.extend(["-vf", "select='%s'" % select_expr, "-vsync", "vfr"])
        else:
            output_params.extend(["-vsync", "0"])
    else:
        if mode == "uniform" and duration <= 0.0:
            raise AnimationError("无法读取视频时长，不能执行全片均匀抽帧；请改用“从头连续抽帧”或更换视频文件。")
        candidate_count = int(__import__('math').ceil(duration * extraction_fps - 1e-9)) if duration > 0.0 else 0
        fps_filter = "fps=fps=%.9f:round=down:eof_action=pass" % extraction_fps
        filter_text = fps_filter
        use_vfr = False
        if mode == "uniform" and candidate_count > maximum:
            if maximum <= 1:
                filter_text += ",select='eq(n,0)'"
            else:
                step = float(candidate_count - 1) / float(maximum - 1)
                filter_text += ",select='eq(n,floor(floor(n/%.12f+0.5)*%.12f+0.5))'" % (step, step)
            use_vfr = True
        output_params.extend(["-vf", filter_text])
        if use_vfr:
            output_params.extend(["-vsync", "vfr"])

    try:
        reader = imageio_ffmpeg.read_frames(
            path,
            pix_fmt="rgba",
            bits_per_pixel=32,
            output_params=output_params,
        )
        meta = next(reader)
        size = meta.get("size") or meta.get("source_size")
        if not size or len(size) != 2:
            close = getattr(reader, "close", None)
            if callable(close):
                close()
            raise AnimationError("FFmpeg 没有返回有效的视频画面尺寸。")
        frame_size = (int(size[0]), int(size[1]))
    except AnimationError:
        raise
    except Exception as exc:
        raise AnimationError("ffmpeg转换视频失败：%s" % exc)

    def iterate():
        produced = 0
        try:
            for raw in reader:
                _check_cancel(cancel_check)
                if produced >= maximum:
                    break
                produced += 1
                try:
                    yield Image.frombytes("RGBA", frame_size, raw)
                except Exception as exc:
                    raise AnimationError("无法读取 FFmpeg 输出帧：%s" % exc)
        finally:
            close = getattr(reader, "close", None)
            if callable(close):
                close()

    return iterate(), source_total, source_kind


def _load_frames(path, maximum, extraction_fps, sampling_mode="head", cancel_check=None):
    if os.path.isdir(path):
        return _frames_from_directory(path, maximum, sampling_mode, cancel_check)
    suffix = os.path.splitext(path)[1].lower()
    if suffix in _IMAGE_EXTENSIONS:
        return _frames_from_image(path, maximum, extraction_fps, sampling_mode, cancel_check)
    if suffix in _VIDEO_EXTENSIONS:
        return _frames_from_video(path, maximum, extraction_fps, sampling_mode, cancel_check)
    raise AnimationError("不支持的动画来源格式：%s" % suffix)


def _fit_frame(image, width, height, fit_mode, background=None):
    image = image.convert("RGBA")
    mode = str(fit_mode or "cover").lower()
    if mode == "stretch":
        if image.size == (width, height):
            return image
        return image.resize((width, height), Image.Resampling.LANCZOS)
    scale = (min if mode == "contain" else max)(width / float(image.width), height / float(image.height))
    size = (max(1, int(round(image.width * scale))), max(1, int(round(image.height * scale))))
    resized = image.resize(size, Image.Resampling.LANCZOS)
    # The animation is an independent full-screen source.  "contain" uses
    # neutral black letterboxing; it must never composite the static GRUB
    # canvas/logo/countdown into every Linux animation frame.
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 255))
    canvas.alpha_composite(resized, ((width - resized.width) // 2, (height - resized.height) // 2))
    return canvas



def render_first_frame_preview(source, width, height, fit_mode, extraction_fps=30.0, static_background=None):
    """Return the first source frame after the exact build-time fit operation."""
    source = os.path.abspath(str(source or ""))
    if not source or not os.path.exists(source):
        raise AnimationError("请先选择有效的动画文件或PNG帧文件夹。")
    frame_iter, _source_count, _source_kind = _load_frames(
        source, 1, max(0.1, float(extraction_fps)), "head"
    )
    iterator = iter(frame_iter)
    try:
        try:
            frame = next(iterator)
        except StopIteration:
            raise AnimationError("动画来源没有产生可预览的首帧。")
        return _fit_frame(
            frame,
            max(1, int(width)),
            max(1, int(height)),
            fit_mode,
            None,
        )
    finally:
        close = getattr(iterator, "close", None)
        if callable(close):
            close()

def _qoi_hash(pixel):
    return (pixel[0] * 3 + pixel[1] * 5 + pixel[2] * 7 + pixel[3] * 11) % 64


def encode_qoi_rgba(image, cancel_check=None):
    """Encode one RGBA Pillow image as a standards-compatible QOI stream.

    v1.1.3 keeps the exact QOI byte stream used by earlier builds.  On the
    supported little-endian Windows targets it walks 32-bit RGBA words instead
    of allocating a Python tuple for every pixel, reducing per-frame CPU time.
    """
    image = image.convert("RGBA")
    width, height = image.size
    raw = image.tobytes()
    pixel_count = width * height
    output = bytearray(b"qoif" + struct.pack(">IIBB", width, height, 4, 0))
    append = output.append
    extend = output.extend

    if sys.byteorder == "little":
        pixels = memoryview(raw).cast("I")  # native AABBGGRR integer for RGBA bytes
        index = [0] * 64
        previous_packed = 0xFF000000
        previous_r = previous_g = previous_b = 0
        previous_a = 255
        run = 0
        for position, packed in enumerate(pixels):
            if (position & 0xFFFF) == 0:
                _check_cancel(cancel_check)
            if packed == previous_packed:
                run += 1
                if run == 62 or position == pixel_count - 1:
                    append(0xC0 | (run - 1))
                    run = 0
                continue
            if run:
                append(0xC0 | (run - 1))
                run = 0
            r = packed & 0xFF
            g = (packed >> 8) & 0xFF
            b = (packed >> 16) & 0xFF
            a = (packed >> 24) & 0xFF
            slot = (r * 3 + g * 5 + b * 7 + a * 11) & 63
            if index[slot] == packed:
                append(slot)
            else:
                index[slot] = packed
                if a == previous_a:
                    dr = r - previous_r
                    dg = g - previous_g
                    db = b - previous_b
                    if -2 <= dr <= 1 and -2 <= dg <= 1 and -2 <= db <= 1:
                        append(0x40 | ((dr + 2) << 4) | ((dg + 2) << 2) | (db + 2))
                    else:
                        dr_dg = dr - dg
                        db_dg = db - dg
                        if -32 <= dg <= 31 and -8 <= dr_dg <= 7 and -8 <= db_dg <= 7:
                            extend((0x80 | (dg + 32), ((dr_dg + 8) << 4) | (db_dg + 8)))
                        else:
                            extend((0xFE, r, g, b))
                else:
                    extend((0xFF, r, g, b, a))
            previous_r, previous_g, previous_b, previous_a = r, g, b, a
            previous_packed = packed
    else:
        # Portable fallback retained for completeness; AnimeBootNeo's supported
        # x86/x64 Windows targets always take the faster branch above.
        pixels = image.get_flattened_data() if hasattr(image, "get_flattened_data") else image.getdata()
        index = [(0, 0, 0, 0)] * 64
        previous = (0, 0, 0, 255)
        run = 0
        for position, pixel in enumerate(pixels):
            if (position & 0xFFFF) == 0:
                _check_cancel(cancel_check)
            if pixel == previous:
                run += 1
                if run == 62 or position == pixel_count - 1:
                    append(0xC0 | (run - 1))
                    run = 0
                continue
            if run:
                append(0xC0 | (run - 1))
                run = 0
            slot = _qoi_hash(pixel)
            if index[slot] == pixel:
                append(slot)
            else:
                index[slot] = pixel
                if pixel[3] == previous[3]:
                    dr = pixel[0] - previous[0]
                    dg = pixel[1] - previous[1]
                    db = pixel[2] - previous[2]
                    if -2 <= dr <= 1 and -2 <= dg <= 1 and -2 <= db <= 1:
                        append(0x40 | ((dr + 2) << 4) | ((dg + 2) << 2) | (db + 2))
                    else:
                        dr_dg = dr - dg
                        db_dg = db - dg
                        if -32 <= dg <= 31 and -8 <= dr_dg <= 7 and -8 <= db_dg <= 7:
                            extend((0x80 | (dg + 32), ((dr_dg + 8) << 4) | (db_dg + 8)))
                        else:
                            extend((0xFE, pixel[0], pixel[1], pixel[2]))
                else:
                    extend((0xFF, pixel[0], pixel[1], pixel[2], pixel[3]))
            previous = pixel
    output.extend(b"\x00\x00\x00\x00\x00\x00\x00\x01")
    return output

def encode_jpeg_rgb(image, quality=90):
    """Encode one fitted frame as baseline JPEG for the experimental codec.

    The fitted animation canvas is always opaque, so dropping alpha does not
    lose meaningful pixels.  4:2:0 chroma subsampling is intentional: the JPEG
    mode exists for photographic/video material where payload size matters
    more than pixel-exact reproduction.  QOI remains available for lossless UI.
    """
    quality = max(60, min(100, int(quality)))
    output = io.BytesIO()
    image.convert("RGB").save(
        output, format="JPEG", quality=quality, subsampling=2,
        optimize=False, progressive=False,
    )
    return output.getvalue()


def _planned_frame_count(metadata, maximum, extraction_fps):
    maximum = max(1, int(maximum))
    extraction_fps = max(0.1, float(extraction_fps))
    kind = str(metadata.get("kind", ""))
    source_frames = max(0, int(metadata.get("frame_count", 0) or 0))
    duration = max(0.0, float(metadata.get("duration", 0.0) or 0.0))
    if kind == "png_sequence":
        return min(maximum, max(1, source_frames))
    if kind == "image":
        return 1
    if duration > 0.0:
        import math
        candidates = max(1, int(math.ceil(duration * extraction_fps - 1e-9)))
        return min(maximum, candidates)
    if source_frames > 0:
        return min(maximum, source_frames)
    return maximum


def _frame_content_metrics(image):
    """Return lightweight spatial-complexity metrics for codec recommendation.

    This is deliberately a transparent heuristic, not semantic AI: it measures
    how much of a representative frame is locally flat, how much fine texture
    it contains, how many strong edges exist, and how broad the quantized color
    palette is.  QOI tends to excel on large flat/repeated areas and hard UI
    edges; JPEG tends to excel when texture/color variation is spread across
    most of the image.
    """
    rgb = image.convert("RGB")
    max_w, max_h = 192, 108
    if rgb.width > max_w or rgb.height > max_h:
        scale = min(max_w / float(rgb.width), max_h / float(rgb.height))
        size = (max(2, int(round(rgb.width * scale))), max(2, int(round(rgb.height * scale))))
        try:
            resample = Image.Resampling.BILINEAR
        except AttributeError:
            resample = Image.BILINEAR
        rgb = rgb.resize(size, resample)
    w, h = rgb.size
    pixels = list(rgb.getdata())
    if w < 2 or h < 2 or not pixels:
        return {"flat_ratio": 1.0, "texture_ratio": 0.0, "strong_edge_ratio": 0.0, "palette_ratio": 0.0}

    flat = texture = strong = total = 0
    # Adjacent-pixel differences capture both broad flat fills and distributed
    # photographic texture without depending on OCR or face detection.
    for y in range(h):
        row = y * w
        for x in range(w - 1):
            a, b = pixels[row + x], pixels[row + x + 1]
            d = (abs(a[0]-b[0]) + abs(a[1]-b[1]) + abs(a[2]-b[2])) / 3.0
            total += 1
            if d <= 3.0: flat += 1
            if d >= 10.0: texture += 1
            if d >= 42.0: strong += 1
    for y in range(h - 1):
        row = y * w
        next_row = row + w
        for x in range(w):
            a, b = pixels[row + x], pixels[next_row + x]
            d = (abs(a[0]-b[0]) + abs(a[1]-b[1]) + abs(a[2]-b[2])) / 3.0
            total += 1
            if d <= 3.0: flat += 1
            if d >= 10.0: texture += 1
            if d >= 42.0: strong += 1

    # 3 bits/channel -> at most 512 buckets.  UI/anime typically occupies a
    # much smaller fraction of this coarse palette than photography/video.
    buckets = set(((r >> 5) << 6) | ((g >> 5) << 3) | (b >> 5) for r, g, b in pixels)
    denom = float(max(1, min(512, len(pixels))))
    return {
        "flat_ratio": flat / float(max(1, total)),
        "texture_ratio": texture / float(max(1, total)),
        "strong_edge_ratio": strong / float(max(1, total)),
        "palette_ratio": len(buckets) / denom,
    }


def _recommend_animation_codec(qoi_bytes, jpeg_bytes, metrics):
    ratio = (float(jpeg_bytes) / float(qoi_bytes)) if qoi_bytes else 1.0
    flat = float(metrics.get("flat_ratio", 0.0))
    texture = float(metrics.get("texture_ratio", 0.0))
    strong = float(metrics.get("strong_edge_ratio", 0.0))
    palette = float(metrics.get("palette_ratio", 0.0))

    if ratio <= 0.48:
        return "jpeg", "JPEG 试压体积显著更小，素材更适合高压缩模式。"
    if flat >= 0.52 and texture <= 0.28 and palette <= 0.34 and ratio >= 0.58:
        return "qoi", "代表帧存在大量平坦/重复区域且纹理较少，偏向 UI、动漫、大色块，QOI 更稳妥。"
    if strong >= 0.08 and flat >= 0.38 and texture <= 0.38 and ratio >= 0.62:
        return "qoi", "强边缘较集中、平坦区域较多，像文字/线稿/UI；若要求边缘完全无损，建议 QOI。"
    if texture >= 0.42 or palette >= 0.48:
        return "jpeg", "细碎纹理或颜色变化分布较广，偏向真人/摄影/复杂 CG，JPEG 通常更划算。"
    if ratio <= 0.68:
        return "jpeg", "JPEG 实测仍有明显体积优势，建议优先 JPEG。"
    return "qoi", "两种编码体积差距不大，优先保留 QOI 的完全无损与最高解码速度。"


def estimate_animation_codec_sizes(source, width, height, fit_mode, maximum,
                                   extraction_fps, sampling_mode="head",
                                   jpeg_quality=90, sample_limit=6,
                                   cancel_check=None, estimate_codecs=None):
    """Estimate Raw-QOI and/or JPEG Chunk32 payload sizes without stalling UI.

    ``estimate_codecs`` defaults to both codecs for the user-triggered estimate
    dialog.  Build preflight may request only the currently selected codec, so
    the common JPEG path no longer wastes time doing six pure-Python QOI
    encodes that cannot affect the decision.

    QOI sampling uses the same multi-process encoder used by the real build.
    JPEG sampling runs concurrently in native Pillow worker threads while QOI
    subprocesses are active.  This keeps the estimate representative while
    using the machine's available CPU instead of serially encoding six frames.
    """
    source = os.path.abspath(str(source or ""))
    if not source or not os.path.exists(source):
        raise AnimationError("请先选择有效的动画文件或PNG帧文件夹。")
    requested = estimate_codecs
    if requested is None:
        requested = ("qoi", "jpeg")
    requested = {str(value or "").lower() for value in requested}
    requested &= {"qoi", "jpeg"}
    if not requested:
        raise AnimationError("没有指定需要估算的动画编码。")

    metadata = probe_animation_source(source)
    planned = _planned_frame_count(metadata, maximum, extraction_fps)
    sample_count = max(1, min(int(sample_limit), planned))
    frame_iter, _source_count, _source_kind = _load_frames(
        source, sample_count, extraction_fps, "uniform", cancel_check
    )
    fitted_frames = []
    iterator = iter(frame_iter)
    try:
        for source_frame in iterator:
            _check_cancel(cancel_check)
            fitted = _fit_frame(source_frame, int(width), int(height), fit_mode, None)
            fitted_frames.append(fitted)
            del source_frame
    finally:
        close = getattr(iterator, "close", None)
        if callable(close):
            close()
    if not fitted_frames:
        raise AnimationError("动画来源没有产生可用于估算的帧。")

    qoi_sizes = []
    jpeg_blobs = []
    qoi_workers = 0
    jpeg_workers = 0
    temp_root = ""
    try:
        # Pure-Python QOI is the expensive part and cannot scale with threads
        # because of the GIL.  Save the <=6 fitted samples as fast BMP files and
        # feed them to the existing multi-process build encoder.
        qoi_future = None
        jpeg_future = None
        with ThreadPoolExecutor(max_workers=2) as outer:
            if "qoi" in requested:
                temp_root = tempfile.mkdtemp(prefix="animebootneo_estimate_")
                frame_paths = []
                for index, image in enumerate(fitted_frames):
                    _check_cancel(cancel_check)
                    path = os.path.join(temp_root, "sample_%02d.bmp" % index)
                    image.save(path, format="BMP")
                    frame_paths.append(path)
                qoi_dir = os.path.join(temp_root, "qoi")
                qoi_future = outer.submit(
                    _encode_qoi_files_parallel, frame_paths, qoi_dir, cancel_check
                )

            if "jpeg" in requested:
                def encode_jpeg_samples():
                    # When QOI is estimated at the same time, its subprocess
                    # pool already occupies the available CPU while leaving one
                    # logical core for Windows/Qt.  Keep JPEG on one helper
                    # thread in that case to avoid oversubscribing the machine.
                    # JPEG-only build preflight can use all safe workers.
                    workers = 1 if "qoi" in requested else _qoi_parallel_worker_count(len(fitted_frames))
                    if workers <= 1:
                        return [encode_jpeg_rgb(image, jpeg_quality) for image in fitted_frames], max(1, workers)
                    with ThreadPoolExecutor(max_workers=workers) as pool:
                        futures = [pool.submit(encode_jpeg_rgb, image, jpeg_quality) for image in fitted_frames]
                        return [future.result() for future in futures], workers
                jpeg_future = outer.submit(encode_jpeg_samples)

            if qoi_future is not None:
                qoi_paths, qoi_workers = qoi_future.result()
                qoi_sizes = [os.path.getsize(path) for path in qoi_paths]
            if jpeg_future is not None:
                jpeg_blobs, jpeg_workers = jpeg_future.result()
    finally:
        for image in fitted_frames:
            try:
                image.close()
            except Exception:
                pass
        if temp_root:
            shutil.rmtree(temp_root, ignore_errors=True)

    esra_header = 44
    qoi_est = 0
    if qoi_sizes:
        qoi_est = esra_header + planned * 4 + int(round(sum(qoi_sizes) / float(len(qoi_sizes)) * planned))

    jpeg_esra_est = 0
    jpeg_est = 0
    xz_ratio = 1.0
    if jpeg_blobs:
        jpeg_esra_est = esra_header + planned * 4 + int(round(sum(len(x) for x in jpeg_blobs) / float(len(jpeg_blobs)) * planned))

        # Mirror the stable JPEG container: frame0 is its own chunk, then
        # 32-frame groups.  With <=6 samples we cannot model every transition,
        # but selected-quality JPEG bytes are passed through preset-1 XZ just
        # like the real Chunk32 container.
        sample_groups = [jpeg_blobs[:1]]
        if len(jpeg_blobs) > 1:
            sample_groups.append(jpeg_blobs[1:])
        sample_raw = 0
        sample_stored = 0
        for group in sample_groups:
            if not group:
                continue
            raw = b"".join(struct.pack("<I", len(blob)) + blob for blob in group)
            packed = lzma.compress(raw, format=lzma.FORMAT_XZ, check=lzma.CHECK_NONE, preset=1)
            sample_raw += len(raw)
            sample_stored += min(len(raw), len(packed))
        xz_ratio = (float(sample_stored) / float(sample_raw)) if sample_raw else 1.0
        jpeg_frame_stream = planned * 4 + int(round(sum(len(x) for x in jpeg_blobs) / float(len(jpeg_blobs)) * planned))
        chunk_count = 1 if planned <= 1 else 1 + int((planned - 1 + 31) // 32)
        jcx_overhead = struct.calcsize("<8s6IQ") + esra_header + chunk_count * 32
        jpeg_est = jcx_overhead + int(round(jpeg_frame_stream * xz_ratio))

    return {
        "planned_frame_count": planned,
        "sample_count": len(fitted_frames),
        "qoi_bytes": qoi_est,
        "jpeg_bytes": jpeg_est,
        "jpeg_esra_bytes": jpeg_esra_est,
        "jpeg_xz_ratio": xz_ratio,
        "jpeg_quality": max(60, min(100, int(jpeg_quality))),
        "source_kind": str(metadata.get("kind", "") or ""),
        "estimate_qoi_workers": int(qoi_workers),
        "estimate_jpeg_workers": int(jpeg_workers),
        "estimated_codecs": sorted(requested),
    }


def _available_worker_memory_limit():
    """Return a conservative process-count cap derived from physical RAM."""
    total_mib = 0
    if os.name == "nt":
        try:
            import ctypes
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            status = MEMORYSTATUSEX()
            status.dwLength = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                total_mib = int(status.ullTotalPhys // (1024 * 1024))
        except Exception:
            total_mib = 0
    else:
        try:
            total_mib = int(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") // (1024 * 1024))
        except Exception:
            total_mib = 0
    if total_mib <= 0:
        return 16
    # Each 1080p Pillow/QOI worker has its own process address space.  Keeping
    # roughly 512 MiB for the GUI/OS and budgeting 128 MiB per worker avoids
    # turning high CPU utilization into paging on low-memory old PCs.
    return max(1, min(16, max(1, (total_mib - 512) // 128)))


def _qoi_parallel_worker_count(job_count):
    """Choose a CPU-saturating but memory-safe number of QOI encoders."""
    jobs = max(0, int(job_count))
    if jobs <= 1:
        return jobs
    logical = max(1, int(os.cpu_count() or 1))
    if logical <= 2:
        wanted = logical
    else:
        # Leave one logical processor for the GUI/Explorer/OS on normal PCs.
        wanted = logical - 1
    return max(1, min(jobs, wanted, _available_worker_memory_limit(), 16))


def _qoi_worker_command(manifest_path):
    if getattr(sys, "frozen", False):
        return [sys.executable, "--qoi-batch-worker", manifest_path]
    app_path = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "app.py"))
    return [sys.executable, app_path, "--qoi-batch-worker", manifest_path]


def qoi_batch_worker_main(manifest_path):
    """Private subprocess entry point used only while *building* animations."""
    try:
        with open(manifest_path, "r", encoding="utf-8") as stream:
            manifest = json.load(stream)
        jobs = list(manifest.get("jobs", []))
        for source_path, output_path in jobs:
            parent = os.path.dirname(output_path)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent)
            with Image.open(source_path) as image:
                encoded = encode_qoi_rgba(image.convert("RGBA"))
            with open(output_path, "wb") as stream:
                stream.write(encoded)
        return 0
    except BaseException as exc:
        try:
            sys.stderr.write("QOI worker failed: %s\n" % exc)
            sys.stderr.flush()
        except Exception:
            pass
        return 2


def _jpeg_worker_command(manifest_path):
    if getattr(sys, "frozen", False):
        return [sys.executable, "--jpeg-batch-worker", manifest_path]
    app_path = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "app.py"))
    return [sys.executable, app_path, "--jpeg-batch-worker", manifest_path]


def jpeg_batch_worker_main(manifest_path):
    """Private subprocess entry point for build-time JPEG frame encoding."""
    try:
        with open(manifest_path, "r", encoding="utf-8") as stream:
            manifest = json.load(stream)
        jobs = list(manifest.get("jobs", []))
        quality = max(60, min(100, int(manifest.get("quality", 90))))
        for source_path, output_path in jobs:
            parent = os.path.dirname(output_path)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent)
            with Image.open(source_path) as image:
                encoded = encode_jpeg_rgb(image, quality)
            with open(output_path, "wb") as stream:
                stream.write(encoded)
        return 0
    except BaseException as exc:
        try:
            sys.stderr.write("JPEG worker failed: %s\n" % exc)
            sys.stderr.flush()
        except Exception:
            pass
        return 2


def _terminate_processes(processes):
    for process in processes:
        if process.poll() is None:
            try:
                process.terminate()
            except Exception:
                pass
    deadline = time.time() + 1.0
    for process in processes:
        if process.poll() is None:
            try:
                process.wait(timeout=max(0.0, deadline - time.time()))
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass


ENCODING_IDLE_TIMEOUT = 180.0


def _wait_encoding_workers(processes, outputs, logs, cancel_check=None, progress=None):
    """Bound stalled children and drain their output to files, never full pipes."""
    last_change=time.monotonic();last_scan=0.0;completed=-1
    while True:
        _check_cancel(cancel_check)
        now=time.monotonic()
        if now-last_scan>=0.5:
            count=sum(os.path.isfile(path) for path in outputs)
            if count!=completed:
                completed=count;last_change=now
                if callable(progress):progress('正在编码动画：已完成 %d/%d 帧…'%(count,len(outputs)))
            last_scan=now
        for process,log in zip(processes,logs):
            if process.poll() not in (None,0):
                log.seek(0);details=log.read(8192).decode('utf-8','replace').strip()
                raise AnimationError('动画编码子进程失败：'+(details or '返回码 %s'%process.returncode))
        if all(process.poll() is not None for process in processes):return
        if now-last_change>ENCODING_IDLE_TIMEOUT:
            raise AnimationError('动画编码子进程超过 %d 秒没有生成新帧，已停止。请检查素材文件和安全软件是否拦截编码子进程后重试。'%ENCODING_IDLE_TIMEOUT)
        time.sleep(0.05)


def _encode_qoi_files_parallel(frame_paths, qoi_dir, cancel_check=None, progress=None):
    """Encode fitted PNG frames in independent worker processes.

    Pure-Python QOI encoding is the dominant CPU stage for large animations.
    Threads cannot scale it because of the GIL, therefore v1.1.3 launches a
    small set of the same AnimeBootNeo executable in private worker mode.  This
    leaves the Linux payload/player completely unchanged while allowing all
    available CPU cores to participate in the *build*.
    """
    frame_paths = list(frame_paths)
    workers = _qoi_parallel_worker_count(len(frame_paths))
    if workers <= 0:
        return [], 0
    if not os.path.isdir(qoi_dir):
        os.makedirs(qoi_dir)
    qoi_paths = [os.path.join(qoi_dir, "frame_%05d.qoi" % (index + 1)) for index in range(len(frame_paths))]
    if workers == 1:
        for source_path, output_path in zip(frame_paths, qoi_paths):
            _check_cancel(cancel_check)
            with Image.open(source_path) as image:
                encoded = encode_qoi_rgba(image.convert("RGBA"), cancel_check)
            with open(output_path, "wb") as stream:
                stream.write(encoded)
        return qoi_paths, 1

    groups = [[] for _ in range(workers)]
    for index, pair in enumerate(zip(frame_paths, qoi_paths)):
        groups[index % workers].append(pair)
    manifest_paths = []
    processes = []
    logs = []
    creationflags = 0
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags = subprocess.CREATE_NO_WINDOW
    try:
        for worker_index, jobs in enumerate(groups):
            if not jobs:
                continue
            manifest_path = os.path.join(qoi_dir, "worker_%02d.json" % worker_index)
            with open(manifest_path, "w", encoding="utf-8") as stream:
                json.dump({"jobs": jobs}, stream, ensure_ascii=False)
            manifest_paths.append(manifest_path)
            log=tempfile.TemporaryFile();logs.append(log)
            process = subprocess.Popen(
                _qoi_worker_command(manifest_path),
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
            processes.append(process)
        _wait_encoding_workers(processes,qoi_paths,logs,cancel_check,progress)
        missing = [path for path in qoi_paths if not os.path.isfile(path)]
        if missing:
            raise AnimationError("并行QOI编码没有生成完整帧数据。")
        return qoi_paths, len(processes)
    except Exception:
        _terminate_processes(processes)
        raise
    finally:
        for log in logs:log.close()
        for manifest_path in manifest_paths:
            try:
                os.remove(manifest_path)
            except OSError:
                pass

def _encode_jpeg_files_parallel(frame_paths, jpeg_dir, quality=90, cancel_check=None, progress=None):
    """Encode fitted PNG frames as baseline JPEG in independent processes."""
    frame_paths = list(frame_paths)
    quality = max(60, min(100, int(quality)))
    workers = _qoi_parallel_worker_count(len(frame_paths))
    if workers <= 0:
        return [], 0
    if not os.path.isdir(jpeg_dir):
        os.makedirs(jpeg_dir)
    jpeg_paths = [os.path.join(jpeg_dir, "frame_%05d.jpg" % (index + 1)) for index in range(len(frame_paths))]
    if workers == 1:
        for source_path, output_path in zip(frame_paths, jpeg_paths):
            _check_cancel(cancel_check)
            with Image.open(source_path) as image:
                encoded = encode_jpeg_rgb(image, quality)
            with open(output_path, "wb") as stream:
                stream.write(encoded)
        return jpeg_paths, 1

    groups = [[] for _ in range(workers)]
    for index, pair in enumerate(zip(frame_paths, jpeg_paths)):
        groups[index % workers].append(pair)
    manifest_paths = []
    processes = []
    logs = []
    creationflags = 0
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags = subprocess.CREATE_NO_WINDOW
    try:
        for worker_index, jobs in enumerate(groups):
            if not jobs:
                continue
            manifest_path = os.path.join(jpeg_dir, "worker_%02d.json" % worker_index)
            with open(manifest_path, "w", encoding="utf-8") as stream:
                json.dump({"jobs": jobs, "quality": quality}, stream, ensure_ascii=False)
            manifest_paths.append(manifest_path)
            log=tempfile.TemporaryFile();logs.append(log)
            process = subprocess.Popen(
                _jpeg_worker_command(manifest_path),
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
            processes.append(process)
        _wait_encoding_workers(processes,jpeg_paths,logs,cancel_check,progress)
        missing = [path for path in jpeg_paths if not os.path.isfile(path)]
        if missing:
            raise AnimationError("并行JPEG编码没有生成完整帧数据。")
        return jpeg_paths, len(processes)
    except Exception:
        _terminate_processes(processes)
        raise
    finally:
        for log in logs:log.close()
        for manifest_path in manifest_paths:
            try:
                os.remove(manifest_path)
            except OSError:
                pass


def _sha256_file(path, chunk_size=1024 * 1024):
    import hashlib
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _png_preview_worker_count(width, height):
    """Use a few concurrent Pillow PNG writers without exploding 32-bit RAM."""
    cpus = max(1, int(os.cpu_count() or 1))
    if cpus <= 2:
        workers = 1
    elif cpus <= 4:
        workers = 2
    elif cpus <= 8:
        workers = 4
    elif cpus <= 16:
        workers = 6
    else:
        workers = 8
    # Very large frames already consume substantial memory per pending Image.
    frame_bytes = max(1, int(width) * int(height) * 4)
    memory_limited = max(1, int((96 * 1024 * 1024) // frame_bytes))
    return max(1, min(workers, memory_limited))


def _save_preview_png(image, path):
    try:
        image.save(path, format="PNG", optimize=False, compress_level=1)
        return path
    finally:
        try:
            image.close()
        except Exception:
            pass


def _save_raw_source_frame(image, path):
    """Temporary exact RGBA pixels; no serial PNG compression or alpha loss."""
    try:
        rgba = image if image.mode == 'RGBA' else image.convert('RGBA')
        with open(path, 'wb') as stream:
            stream.write(struct.pack('<8sII', b'ANBR262\0', rgba.width, rgba.height))
            stream.write(rgba.tobytes())
        if rgba is not image:
            rgba.close()
        return path
    finally:
        image.close()


def _source_frame_size(path):
    if path.endswith('.rgba'):
        with open(path, 'rb') as stream:
            magic, width, height = struct.unpack('<8sII', stream.read(16))
        if magic != b'ANBR262\0' or not (0 < width <= 65535 and 0 < height <= 65535):
            raise AnimationError('临时 RGBA 源帧无效。')
        if os.path.getsize(path) != 16 + width * height * 4:
            raise AnimationError('临时 RGBA 源帧不完整。')
        return width, height
    with Image.open(path) as image:
        return image.size


def _open_source_frame(path):
    if not path.endswith('.rgba'):
        return Image.open(path)
    width, height = _source_frame_size(path)
    with open(path, 'rb') as stream:
        stream.seek(16)
        pixels = stream.read(width * height * 4)
    return Image.frombytes('RGBA', (width, height), pixels)


def _finish_oldest_png_future(pending, cancel_check=None):
    if not pending:
        return
    _check_cancel(cancel_check)
    future = pending.pop(0)
    try:
        future.result()
    except Exception as exc:
        raise AnimationError("动画帧处理失败：%s" % exc)


def _fit_and_write_frame(source, width, height, fit_mode, background,
                         preview_path, jpeg_path, quality, codec='jpeg'):
    """Fit once; native Pillow workers encode JPEG without rereading PNG."""
    fitted = None
    try:
        fitted = _fit_frame(source, width, height, fit_mode, background)
        if jpeg_path:
            with open(jpeg_path + '.building', 'wb') as stream:
                stream.write(encode_jpeg_rgb(fitted, quality) if codec == 'jpeg' else encode_qoi_rgba(fitted))
        if preview_path:
            fitted.save(preview_path + '.building', format='PNG', optimize=False, compress_level=1)
            os.replace(preview_path + '.building', preview_path)
        if jpeg_path:
            os.replace(jpeg_path + '.building', jpeg_path)
    finally:
        source.close()
        if fitted is not None:
            fitted.close()


def frame_pipeline_worker_main(manifest_path):
    """Independent 32-bit address space for large-frame fit/PNG/codec jobs."""
    try:
        with open(manifest_path, 'r', encoding='utf-8') as stream:
            spec = json.load(stream)
        for source_path, preview_path, encoded_path in spec['jobs']:
            # _fit_and_write_frame owns and closes this image. A second close
            # from Image.__exit__ can fail on Pillow 10's closed PNG image.
            image = _open_source_frame(source_path)
            _fit_and_write_frame(image, int(spec['width']), int(spec['height']),
                spec['fit_mode'], None, preview_path, encoded_path,
                int(spec['jpeg_quality']), spec['codec'])
        return 0
    except Exception as exc:
        try:
            sys.stderr.write('Frame pipeline failed: %s\n' % exc)
        except Exception:
            pass
        return 2


def _frame_worker_command(manifest_path):
    if getattr(sys, 'frozen', False):
        return [sys.executable, '--frame-pipeline-worker', manifest_path]
    return [sys.executable, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, 'app.py')),
            '--frame-pipeline-worker', manifest_path]


def _frame_process_worker_count(job_count, source_pixels, target_pixels):
    # Each child has its own virtual address space; do not apply the former
    # single-process 96/256 MiB budgets to the whole machine's worker pool.
    memory_mib = _available_worker_memory_limit() * 128
    if os.name == 'nt':
        try:
            import ctypes
            class Memory(ctypes.Structure):
                _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong)] + [
                    (name, ctypes.c_ulonglong) for name in ('total', 'available', 'total_page',
                    'available_page', 'total_virtual', 'available_virtual', 'extended')]
            status = Memory();status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                memory_mib = max(128, status.available // (1024 * 1024) - 512)
        except Exception:
            pass
    per_worker = max(128, (source_pixels * 8 + target_pixels * 12) // (1024 * 1024) + 64)
    workers = min(max(1, int(job_count)), max(1, int(os.cpu_count() or 1) - 1), 16,
                  max(1, int(memory_mib) // per_worker))
    return workers, per_worker


# 每个工作进程只接收自己的帧列表与设置；父进程检查结果后按原帧序合并，避免以完成顺序产生动画跳帧。
def _process_frame_files(paths, width, height, fit_mode, codec, quality,
                         preview_dir, encoded_dir, cancel_check=None, progress=None):
    source_pixels = 0
    for path in paths:
        _check_cancel(cancel_check)
        source_width, source_height = _source_frame_size(path)
        source_pixels = max(source_pixels, source_width * source_height)
    workers, memory_mib = _frame_process_worker_count(len(paths), source_pixels, width * height)
    extension = 'jpg' if codec == 'jpeg' else 'qoi'
    outputs = [os.path.join(encoded_dir, 'frame_%05d.%s' % (i + 1, extension)) for i in range(len(paths))]
    groups = [[] for _ in range(workers)]
    for index, (source, output) in enumerate(zip(paths, outputs)):
        preview = os.path.join(preview_dir, 'frame_%05d.png' % (index + 1)) if preview_dir else ''
        groups[index % workers].append((source, preview, output))
    processes = [];logs = [];manifests = []
    if callable(progress):
        progress('并行缩放、%s 编码及保存预览：%d 个独立进程，每进程预计 %d MiB…' % (codec.upper(), workers, memory_mib))
    try:
        for index, jobs in enumerate(groups):
            _check_cancel(cancel_check)
            manifest = os.path.join(encoded_dir, 'pipeline_%02d.json' % index)
            with open(manifest, 'w', encoding='utf-8') as stream:
                json.dump(dict(jobs=jobs, width=width, height=height, fit_mode=fit_mode,
                               codec=codec, jpeg_quality=quality), stream, ensure_ascii=False)
            manifests.append(manifest)
            log = tempfile.TemporaryFile();logs.append(log)
            processes.append(subprocess.Popen(_frame_worker_command(manifest), stdout=log,
                stderr=subprocess.STDOUT, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)))
        def report(message):
            if callable(progress):
                progress(message.replace('正在编码动画', '正在并行处理并保存动画帧'))
        _wait_encoding_workers(processes, outputs, logs, cancel_check, report)
        if not all(os.path.isfile(path) for path in outputs):
            raise AnimationError('并行帧处理没有生成全部编码文件。')
        return outputs, workers
    except BaseException:
        _terminate_processes(processes)
        raise
    finally:
        for log in logs:log.close()
        for manifest in manifests:
            try:os.remove(manifest)
            except OSError:pass


# 此函数返回编码容器路径和实际统计，调用者消费后负责删除暂存容器。通过有限并行与文件切片约束32位进程内存峰值。
def render_linux_animation_payload_file(project, static_background=None, frame_export_dir=None, temp_dir=None, cancel_check=None, progress=None):
    """Render experimental ANBF v2 with per-frame QOI or JPEG payloads."""
    started = time.perf_counter()
    _check_cancel(cancel_check)
    spec = project.get("boot_animation", {})
    if not animation_enabled(project):
        return "", {
            "enabled": False,
            "delivery": "linux_initramfs_framebuffer",
            "reason": "disabled_or_missing_source",
            "exported_frame_dir": "",
            "exported_frame_count": 0,
            "payload_path": "",
            "payload_size": 0,
            "payload_sha256": "",
            "build_qoi_workers": 0,
            "build_jpeg_workers": 0,
        }
    width, height = [int(value) for value in project["resolution"]]
    maximum = int(spec.get("max_frames", 600))
    frame_rate = int(spec.get("frame_rate", 60))
    extraction_fps = float(spec.get("extract_frame_rate", frame_rate))
    sampling_mode = str(spec.get("sampling_mode", "head"))
    loops = int(spec.get("loops", 1))
    fit_mode = str(spec.get("fit_mode", "stretch"))
    codec = str(spec.get("codec", "jpeg") or "jpeg").lower()
    if codec not in ANBF_CODECS:
        codec = "jpeg"
    jpeg_quality = max(60, min(100, int(spec.get("jpeg_quality", 90))))
    source = os.path.abspath(str(spec.get("source", "")))
    source_suffix = os.path.splitext(source)[1].lower()
    video_source = source_suffix in _VIDEO_EXTENSIONS and not os.path.isdir(source)
    native_gif_pipeline = source_suffix == '.gif' and not os.path.isdir(source)
    transparent_gif = False
    gif_requires_pillow = False
    if native_gif_pipeline:
        info = os.stat(source)
        controls = _gif_control_info(source, info.st_size, info.st_mtime_ns)
        transparent_gif = controls[1]
        gif_requires_pillow = transparent_gif or controls[2]
        native_gif_pipeline = not gif_requires_pillow
    native_video_pipeline = video_source
    video_metadata = None
    if video_source:
        video_metadata = _probe_video_metadata(source)
        source_size = video_metadata.get('size') or []
        if len(source_size) == 2:
            source_pixels = int(source_size[0]) * int(source_size[1])
            target_pixels = width * height
            wanted = _png_preview_worker_count(width, height)
            queue = min(wanted * 2, max(1, (256 * 1024 * 1024) //
                                       max(1, (source_pixels + target_pixels) * 12)))
            # Retain the fast fused in-memory JPEG path for small frames that
            # can keep all native writer threads busy. File jobs remove the
            # address-space bottleneck only where the GUI queue limits them.
            native_video_pipeline = (codec == 'qoi' or max(source_pixels, target_pixels) >= 3840 * 2160
                                     or (queue <= 2 and queue < wanted))
    file_sources = None
    large_image_source = False
    source_count, source_kind = 0, 'video'
    iterator = None
    if os.path.isdir(source):
        names = [os.path.join(source, name) for name in os.listdir(source)
                 if name.lower().endswith('.png') and os.path.isfile(os.path.join(source, name))]
        names.sort(key=_natural_key)
        if not names:
            raise AnimationError('所选文件夹中没有PNG帧。')
        selected = [names[index] for index in _selected_indices(len(names), maximum, sampling_mode)]
        source_pixels = 0
        for path in selected:
            _check_cancel(cancel_check)
            with Image.open(path) as image:
                source_pixels = max(source_pixels, image.width * image.height)
        if max(source_pixels, width * height) >= 1920 * 1080:
            file_sources, source_count, source_kind = selected, len(names), 'png_sequence'
    elif not video_source:
        with Image.open(source) as image:
            large_image_source = gif_requires_pillow or max(image.width * image.height, width * height) >= 1920 * 1080
    if video_source and not native_video_pipeline:
        frame_iter, source_count, source_kind = _frames_from_video(
            source, maximum, extraction_fps, sampling_mode, cancel_check, metadata=video_metadata)
        iterator = iter(frame_iter)
    elif not video_source and not native_gif_pipeline and file_sources is None:
        frame_iter, source_count, source_kind = _load_frames(
            source, maximum, extraction_fps, sampling_mode, cancel_check)
        iterator = iter(frame_iter)
    last_progress = 0.0

    export_dir = ""
    staging_dir = ""
    staging_is_export = False
    if frame_export_dir:
        export_dir = os.path.abspath(frame_export_dir)
        if os.path.isdir(export_dir):
            shutil.rmtree(export_dir)
        os.makedirs(export_dir)
        staging_dir = export_dir
        staging_is_export = True
    else:
        staging_dir = tempfile.mkdtemp(prefix="animebootneo_fitted_frames_", dir=temp_dir)

    encoded_dir = tempfile.mkdtemp(prefix="animebootneo_%s_frames_" % codec, dir=temp_dir)
    fd, payload_path = tempfile.mkstemp(prefix="animebootneo_animation_", suffix=".anbf", dir=temp_dir)
    os.close(fd)
    frame_paths = []
    encoded_paths = []
    frame_sizes = []
    frame_count = 0
    codec_workers = 0
    png_workers = _png_preview_worker_count(width, height)
    # Keep only a bounded number of full-resolution PIL images waiting for PNG
    # compression.  Pillow/zlib releases the GIL in this path, so several writer
    # threads use multiple cores while avoiding the former one-frame-at-a-time
    # 10-minute preview bottleneck.
    pending_limit = max(1, png_workers * 2)
    png_executor = None
    pending_png = []
    source_dir = ''
    extraction_diagnostics = ({'extraction_backend': 'ffmpeg_raw_rgba_pipe',
        'source_size': video_metadata.get('size'), 'reason': 'small_frames_preserve_fused_native_thread_speed'}
        if video_source and not native_video_pipeline else {})
    file_pipeline = native_video_pipeline or native_gif_pipeline or file_sources is not None or large_image_source
    frame_worker_kind = 'independent_processes' if file_pipeline else 'native_threads'
    try:
        if native_video_pipeline:
            from .video_extract import extract_video_pngs
            source_dir = tempfile.mkdtemp(prefix='animebootneo_source_frames_', dir=temp_dir)
            file_sources, source_count, source_kind, extraction_diagnostics = extract_video_pngs(
                source, maximum, extraction_fps, sampling_mode, source_dir, cancel_check, progress,
                metadata=video_metadata)
        elif native_gif_pipeline:
            from .image_extract import extract_gif_pngs
            source_dir = tempfile.mkdtemp(prefix='animebootneo_gif_frames_', dir=temp_dir)
            source_indices, source_count = _image_source_indices(source, maximum, extraction_fps, sampling_mode)
            file_sources, extraction_diagnostics = extract_gif_pngs(
                source, source_indices, source_dir, cancel_check, progress)
            source_kind = 'animated_image' if source_count > 1 else 'image'
        elif large_image_source:
            source_dir = tempfile.mkdtemp(prefix='animebootneo_source_frames_', dir=temp_dir)
            file_sources = []
            # Composition/disposal is ordered. Preserve exact RGBA (including
            # transparent palette RGB) without compressing intermediate PNGs.
            for source_frame in iterator:
                _check_cancel(cancel_check)
                path = os.path.join(source_dir, 'frame_%05d.rgba' % (len(file_sources) + 1))
                _save_raw_source_frame(source_frame, path)
                file_sources.append(path)
                now = time.monotonic()
                if callable(progress) and (len(file_sources) == 1 or now - last_progress >= 0.5):
                    progress('正在读取动画图片并暂存原始帧（无需 PNG 压缩）：已保存 %d 帧…' % len(file_sources))
                    last_progress = now
            extraction_diagnostics = {'extraction_backend': 'pillow_ordered_rgba_raw',
                                      'transparent_gif': transparent_gif,
                                      'gif_exact_composition_required': gif_requires_pillow,
                                      'intermediate_compression': 'none',
                                      'selected_frames': len(file_sources)}
        if file_pipeline:
            if not file_sources:
                raise AnimationError('动画来源没有产生任何帧。')
            encoded_paths, codec_workers = _process_frame_files(
                file_sources, width, height, fit_mode, codec, jpeg_quality,
                export_dir, encoded_dir, cancel_check, progress)
            frame_count = len(file_sources)
            png_workers = codec_workers if export_dir else 0
            pending_limit = 0  # No full-size image queue in the GUI process.
            frame_pipeline_seconds = time.perf_counter() - started
            encoding_seconds = 0.0  # Encoding is fused with fitting in each child.
        else:
            png_executor = ThreadPoolExecutor(max_workers=png_workers)
            if callable(progress):
                progress('%s；%d 个帧处理线程并行缩放、%s…' % (
                    'FFmpeg 视频抽帧' if video_source else '读取动画帧', png_workers,
                    'JPEG 编码及预览保存' if codec == 'jpeg' else '预览保存'))
            try:
                for source_frame in iterator:
                    _check_cancel(cancel_check)
                    frame_count += 1
                    now=time.monotonic()
                    if callable(progress) and (frame_count==1 or now-last_progress>=0.5):
                        progress('正在抽取并保存动画帧：已处理 %d 帧…'%frame_count)
                        last_progress=now
                    frame_path = os.path.join(staging_dir, "frame_%05d.png" % frame_count)
                    frame_paths.append(frame_path)
                    jpeg_path = os.path.join(encoded_dir, 'frame_%05d.jpg' % frame_count) if codec == 'jpeg' else ''
                    if jpeg_path:
                        encoded_paths.append(jpeg_path)
                    # Bound both source and fitted images in the 32-bit process,
                    # including temporary copies made by Pillow during resizing.
                    job_bytes = (source_frame.width * source_frame.height + width * height) * 4 * 3
                    pending_limit = min(pending_limit, max(1, (256 * 1024 * 1024) // max(1, job_bytes)))
                    pending_png.append(png_executor.submit(
                        _fit_and_write_frame, source_frame, width, height, fit_mode,
                        static_background, frame_path if export_dir or codec == 'qoi' else '',
                        jpeg_path, jpeg_quality))
                    del source_frame
                    while len(pending_png) >= pending_limit:
                        _finish_oldest_png_future(pending_png, cancel_check)
                while pending_png:
                    _finish_oldest_png_future(pending_png, cancel_check)
            finally:
                close = getattr(iterator, "close", None)
                if callable(close):
                    close()
                png_executor.shutdown(wait=True)
            if frame_count <= 0:
                raise AnimationError("动画来源没有产生任何帧。")

            _check_cancel(cancel_check)
            frame_pipeline_seconds = time.perf_counter() - started
            encoding_started = time.perf_counter()
            if codec == "jpeg":
                codec_workers = min(png_workers, pending_limit, frame_count)
                if callable(progress):progress('JPEG 并行处理完成：%d 帧，%.2f 秒，无需重新读取 PNG。' % (frame_count, frame_pipeline_seconds))
            else:
                if callable(progress):progress('正在编码 %d 帧 QOI：最多 %d 个编码子进程…' % (frame_count, _qoi_parallel_worker_count(frame_count)))
                encoded_paths, codec_workers = _encode_qoi_files_parallel(
                    frame_paths, encoded_dir, cancel_check=cancel_check,progress=progress
                )
            _check_cancel(cancel_check)
            encoding_seconds = time.perf_counter() - encoding_started

        if callable(progress):progress('正在整理动画帧数据…')

        delay_ms = max(1, int(round(1000.0 / frame_rate)))
        timing_millihz = max(1000, int(round(float(frame_rate) * 1000.0)))
        header = struct.pack(
            "<8s9I", ANBF_MAGIC, ANBF_VERSION, width, height, frame_count,
            delay_ms, loops, FIT_MODES.get(fit_mode, 0), timing_millihz,
            ANBF_CODECS[codec],
        )
        with open(payload_path, "wb") as body:
            body.write(header)
            for encoded_path in encoded_paths:
                _check_cancel(cancel_check)
                size = os.path.getsize(encoded_path)
                frame_sizes.append(size)
                body.write(struct.pack("<I", size))
                with open(encoded_path, "rb") as source_stream:
                    shutil.copyfileobj(source_stream, body, length=1024 * 1024)
            body.flush()
            try:
                os.fsync(body.fileno())
            except OSError:
                pass
    except MemoryError:
        if export_dir:
            shutil.rmtree(export_dir, ignore_errors=True)
        try:
            os.remove(payload_path)
        except OSError:
            pass
        raise AnimationError("处理动画图像时32位程序虚拟地址空间不足。这不代表电脑物理内存不足，单纯增加物理内存通常不能解决。建议优先改用JPEG高压缩；如必须使用QOI，请降低动画分辨率、最大抽帧数量或缩短动画时长。若本次已经发生过一次虚拟地址空间不足，降低参数后仍再次失败，请先关闭并重新打开AnimeBoot Neo Debug后再制作，以重新获得干净的进程地址空间。")
    except Exception:
        if export_dir:
            shutil.rmtree(export_dir, ignore_errors=True)
        try:
            os.remove(payload_path)
        except OSError:
            pass
        raise
    finally:
        if iterator is not None:
            close = getattr(iterator, "close", None)
            if callable(close):close()
        if source_dir:shutil.rmtree(source_dir, ignore_errors=True)
        shutil.rmtree(encoded_dir, ignore_errors=True)
        if staging_dir and not staging_is_export:
            shutil.rmtree(staging_dir, ignore_errors=True)

    payload_size = os.path.getsize(payload_path)
    payload_sha256 = _sha256_file(payload_path)
    delay_ms = max(1, int(round(1000.0 / frame_rate)))
    return payload_path, {
        "enabled": True,
        "delivery": "linux_initramfs_framebuffer",
        "source": source,
        "source_kind": source_kind,
        "source_frame_count": source_count,
        "frame_count": frame_count,
        "frame_rate": frame_rate,
        "extract_frame_rate": extraction_fps,
        "sampling_mode": sampling_mode,
        "frame_delay_ms": delay_ms,
        "timing_millihz": max(1000, int(round(float(frame_rate) * 1000.0))),
        "player_timing": "CLOCK_MONOTONIC absolute schedule, ordered presentation without frame dropping",
        "loops": loops,
        "fit_mode": fit_mode,
        "codec": codec,
        "jpeg_quality": jpeg_quality if codec == "jpeg" else None,
        "target_resolution": [width, height],
        "frame_sizes": frame_sizes,
        "payload_path": payload_path,
        "payload_size": payload_size,
        "payload_sha256": payload_sha256,
        "duration_seconds": frame_count * loops * delay_ms / 1000.0,
        "format": "ANBF262 v2 + per-frame %s" % ("JPEG" if codec == "jpeg" else "QOI RGBA"),
        "exported_frame_dir": export_dir,
        "exported_frame_count": frame_count if export_dir else 0,
        "exported_frame_format": "PNG（写入%s前的最终画面；JPEG模式会有损压缩）" % codec.upper() if export_dir else "",
        "build_qoi_workers": codec_workers if codec == "qoi" else 0,
        "build_qoi_parallel": bool(codec == "qoi" and codec_workers > 1),
        "build_jpeg_workers": codec_workers if codec == "jpeg" else 0,
        "build_jpeg_parallel": bool(codec == "jpeg" and codec_workers > 1),
        "png_preview_workers": png_workers,
        "jpeg_worker_kind": frame_worker_kind if codec == 'jpeg' else None,
        "frame_worker_kind": frame_worker_kind,
        "video_extraction": extraction_diagnostics,
        "frame_pipeline_queue_limit": pending_limit,
        "build_timings": {"frame_pipeline_seconds": frame_pipeline_seconds,
                          "qoi_encoding_seconds": encoding_seconds if codec == 'qoi' else 0.0,
                          "total_seconds": time.perf_counter() - started},
    }

