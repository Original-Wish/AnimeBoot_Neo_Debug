# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
# -*- coding: utf-8 -*-
"""Lossless source-size video extraction in an independent FFmpeg process.

The GUI never receives full RGBA video frames through a Python pipe.  FFmpeg
decodes and writes numbered RGBA PNG files with native frame threading.  The
existing Pillow fit operation can subsequently consume those source frames,
so resizing, alpha compositing and JPEG/QOI encoding retain their exact pixels.
"""

# FFmpeg进程编排与输出帧收集。命令使用参数列表和隐藏窗口，避免路径中空格/特殊字符被shell误解释。
# 分段并行必须遵守帧选择的全局顺序，归并时按输出帧索引而不是进程完成先后排列。
# 线程/进程预算兼顾可用CPU与内存；进程退出码、stderr、输出帧数量都需要检查，不能以启动成功代替抽帧成功。


import math
import os
import subprocess
import tempfile
import time


_STALL_SECONDS = 180.0
_POLL_SECONDS = 0.1
_PNG_END = b"\x00\x00\x00\x00IEND\xaeB`\x82"


def _source_metadata(path):
    # Import on demand: animation.py also uses this module for production.
    from .animation import AnimationError, _probe_video_metadata
    try:
        import imageio_ffmpeg
        reader = imageio_ffmpeg.read_frames(path)
        try:
            meta = next(reader)
        finally:
            reader.close()
        size = meta.get("size") or meta.get("source_size")
        if not size or len(size) != 2 or min(int(v) for v in size) <= 0:
            raise AnimationError("FFmpeg 没有返回有效的视频画面尺寸。")
        fps = float(meta.get("fps", 0.0) or 0.0)
        duration = float(meta.get("duration", 0.0) or 0.0)
        if fps <= 0.0 or duration <= 0.0:
            fallback = _probe_video_metadata(path)
            fps = float(fallback.get("fps", 0.0) or 0.0)
            duration = float(fallback.get("duration", 0.0) or 0.0)
        count = max(1, int(round(fps * duration))) if fps > 0.0 and duration > 0.0 else 0
        return int(size[0]), int(size[1]), fps, duration, count
    except AnimationError:
        raise
    except Exception as exc:
        raise AnimationError("无法读取视频信息：%s" % exc)


def _sampling_options(maximum, extraction_fps, mode, source_fps, duration,
                      source_count):
    # Keep the exact temporal selection used by _frames_from_video().  In
    # particular, native 30000/1001 video must not pass through an FPS lattice.
    from .animation import AnimationError, _even_indices, _native_video_rate_selected
    native = _native_video_rate_selected(extraction_fps, source_fps)
    kind = "video_native" if native else "video_%.3ffps" % extraction_fps
    options = []
    if native:
        if mode == "uniform" and source_count > maximum:
            indices = _even_indices(source_count, maximum)
            expression = "+".join("eq(n,%d)" % index for index in indices)
            options.extend(["-vf", "select='%s'" % expression, "-vsync", "vfr"])
        else:
            options.extend(["-vsync", "0"])
    else:
        if mode == "uniform" and duration <= 0.0:
            raise AnimationError("无法读取视频时长，不能执行全片均匀抽帧；请改用“从头连续抽帧”或更换视频文件。")
        candidates = int(math.ceil(duration * extraction_fps - 1e-9)) if duration > 0.0 else 0
        filters = "fps=fps=%.9f:round=down:eof_action=pass" % extraction_fps
        use_vfr = False
        if mode == "uniform" and candidates > maximum:
            if maximum <= 1:
                filters += ",select='eq(n,0)'"
            else:
                step = float(candidates - 1) / float(maximum - 1)
                filters += ",select='eq(n,floor(floor(n/%.12f+0.5)*%.12f+0.5))'" % (step, step)
            use_vfr = True
        options.extend(["-vf", filters])
        if use_vfr:
            options.extend(["-vsync", "vfr"])
    return options, kind


def _completed_png(path):
    # image2 creates a file before its PNG encoder finishes writing.  Count
    # only files with the final IEND chunk, never a partially written frame.
    try:
        with open(path, "rb") as stream:
            stream.seek(-len(_PNG_END), os.SEEK_END)
            return stream.read(len(_PNG_END)) == _PNG_END
    except (OSError, ValueError):
        return False


def _stop_process(process):
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5.0)


def _error_tail(stream):
    try:
        stream.flush()
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(0, size - 8192))
        return stream.read().decode("utf-8", "replace").strip()
    except Exception:
        return ""


def extract_video_pngs(path, maximum, extraction_fps, sampling_mode,
                       output_dir, cancel_check=None, progress=None, metadata=None):
    """Return source-size RGBA PNG paths and native extraction diagnostics.

    ``output_dir`` must be a fresh staging directory: existing numbered source
    files are rejected, preventing a stale file from being counted as progress.
    Extraction cancellation and a 180-second interval without a completed frame
    both stop FFmpeg.  Errors leave no partial source-frame set behind.
    """
    from .animation import AnimationError, _check_cancel, _find_ffmpeg
    started = time.perf_counter()
    _check_cancel(cancel_check)
    path = os.path.abspath(str(path))
    maximum = max(1, int(maximum))
    extraction_fps = max(0.1, float(extraction_fps))
    mode = str(sampling_mode or "head")
    output_dir = os.path.abspath(str(output_dir))
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        raise AnimationError("没有检测到内置 FFmpeg 组件，请使用完整便携版或重新构建程序。")
    size = (metadata or {}).get('size') or []
    if len(size) == 2 and min(int(value) for value in size) > 0:
        width, height = [int(value) for value in size]
        source_fps = float(metadata.get('fps', 0.0) or 0.0)
        duration = float(metadata.get('duration', 0.0) or 0.0)
        source_count = int(metadata.get('frame_count', 0) or 0)
    else:
        width, height, source_fps, duration, source_count = _source_metadata(path)
    _check_cancel(cancel_check)
    logical = max(1, int(os.cpu_count() or 1))
    wanted = max(1, logical - 1)
    # This budget belongs to FFmpeg's independent process, rather than the
    # 32-bit Qt process.  Budget two full RGBA source frames per PNG worker.
    frame_bytes = max(1, width * height * 4)
    memory_limit = max(1, (1024 * 1024 * 1024) // (frame_bytes * 2))
    threads = max(1, min(wanted, 16, maximum, memory_limit))
    sampling, source_kind = _sampling_options(
        maximum, extraction_fps, mode, source_fps, duration, source_count)
    os.makedirs(output_dir, exist_ok=True)
    if any(name.startswith("source_") and name.lower().endswith(".png")
           for name in os.listdir(output_dir)):
        raise AnimationError("视频抽帧临时目录已有旧帧，请重新开始制作。")
    pattern = os.path.join(output_dir, "source_%08d.png")
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostats",
               "-nostdin", "-y", "-threads", str(threads),
               "-filter_threads", str(threads), "-i", path,
               "-an", "-sn", "-dn"]
    command.extend(sampling)
    command.extend(["-frames:v", str(maximum), "-c:v", "png", "-pix_fmt", "rgba",
                    "-compression_level", "1", "-threads", str(threads), pattern])
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    source_paths = []
    succeeded = False
    process = None
    if callable(progress):
        progress("FFmpeg 原生抽帧：%d 个线程，直接保存 %d×%d RGBA PNG…" % (threads, width, height))
    with tempfile.TemporaryFile(mode="w+b") as error_log:
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=error_log,
                                       creationflags=creationflags)
            last_frame = time.monotonic()
            last_progress = 0.0
            while True:
                _check_cancel(cancel_check)
                before = len(source_paths)
                while len(source_paths) < maximum:
                    candidate = os.path.join(output_dir, "source_%08d.png" % (len(source_paths) + 1))
                    if not _completed_png(candidate):
                        break
                    source_paths.append(candidate)
                now = time.monotonic()
                changed = len(source_paths) > before
                if changed:
                    last_frame = now
                finished = process.poll() is not None
                if callable(progress) and (changed and (before == 0 or now - last_progress >= 0.5) or finished):
                    progress("FFmpeg 正在抽取并保存动画帧：已完成 %d 帧…" % len(source_paths))
                    last_progress = now
                _check_cancel(cancel_check)
                if finished:
                    if process.returncode != 0:
                        raise AnimationError("FFmpeg 视频抽帧失败：%s" % (_error_tail(error_log) or process.returncode))
                    if not source_paths:
                        raise AnimationError("动画来源没有产生任何帧。")
                    # A successful exit must not leave an incomplete final PNG.
                    next_path = os.path.join(output_dir, "source_%08d.png" % (len(source_paths) + 1))
                    if os.path.exists(next_path):
                        raise AnimationError("FFmpeg 没有完整保存最后一个动画帧。")
                    succeeded = True
                    break
                if now - last_frame >= _STALL_SECONDS:
                    raise AnimationError("FFmpeg 已有 180 秒没有生成新的完整动画帧，已停止抽帧；请检查源视频和输出磁盘。")
                time.sleep(_POLL_SECONDS)
        finally:
            if process is not None:
                _stop_process(process)
            if not succeeded:
                for name in os.listdir(output_dir):
                    if name.startswith("source_") and name.lower().endswith(".png"):
                        try:
                            os.remove(os.path.join(output_dir, name))
                        except OSError:
                            pass
    diagnostics = {
        "extraction_backend": "ffmpeg_native_rgba_png",
        "ffmpeg_threads": threads,
        "ffmpeg_decode_threads": threads,
        "ffmpeg_png_threads": threads,
        "source_size": [width, height],
        "source_fps": source_fps,
        "source_duration": duration,
        "extracted_frames": len(source_paths),
        "extract_frame_rate": extraction_fps,
        "sampling_mode": mode,
        "native_source_rate": source_kind == "video_native",
        "source_frame_bytes": frame_bytes,
        "ffmpeg_frame_budget_bytes": 1024 * 1024 * 1024,
        "extraction_seconds": time.perf_counter() - started,
    }
    return source_paths, source_count, source_kind, diagnostics
