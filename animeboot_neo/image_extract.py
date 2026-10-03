# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
"""Exact GIF source-frame selection with native, lossless FFmpeg decoding."""

# 动态图片解码与时间轴处理。GIF帧可能仅包含变化矩形，必须正确合成前帧并应用透明/清除规则。
# PNG文件夹按自然数字序读取；处理时保留用户选择的抽帧上限、采样方式和输出顺序。
# 解码工作不能阻塞Qt GUI事件循环；取消检查要贯穿迭代，避免用户取消后后台仍不断写帧。

import os
import subprocess
import tempfile
import time
from PIL import Image

from .video_extract import _completed_png, _error_tail, _stop_process


def extract_gif_pngs(path, source_indices, output_dir, cancel_check=None, progress=None):
    from .animation import AnimationError, _check_cancel, _find_ffmpeg
    wanted = sorted(set(int(index) for index in source_indices))
    if not wanted or wanted[0] < 0:
        raise AnimationError('GIF 取样帧编号无效。')
    executable = _find_ffmpeg()
    if not executable:
        raise AnimationError('缺少内置 FFmpeg，无法处理 GIF。')
    os.makedirs(output_dir, exist_ok=True)
    if any(name.startswith('source_') and name.endswith('.png') for name in os.listdir(output_dir)):
        raise AnimationError('GIF 临时目录已有旧帧，请使用新的制作目录。')
    with Image.open(path) as image:
        width, height = image.size
    threads = max(1, min(16, int(os.cpu_count() or 1) - 1, len(wanted),
                         (512 * 1024 * 1024) // max(1, width * height * 8)))
    expression = '+'.join('eq(n,%d)' % index for index in wanted)
    pattern = os.path.join(output_dir, 'source_%08d.png')
    command = [executable, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
               '-threads', str(threads), '-filter_threads', str(threads),
               '-ignore_loop', '1', '-i', os.path.abspath(path), '-an', '-sn', '-dn',
               '-vf', "select='%s'" % expression, '-vsync', '0',
               '-frames:v', str(len(wanted)), '-c:v', 'png', '-pix_fmt', 'rgba',
               '-compression_level', '0', '-threads', str(threads), pattern]
    _check_cancel(cancel_check)
    if callable(progress):
        progress('GIF 原生解码与低开销帧保存：FFmpeg 使用 %d 个线程；随后多进程缩放及编码…' % threads)
    _check_cancel(cancel_check)
    started = time.perf_counter()
    completed = []
    process = None
    success = False
    with tempfile.TemporaryFile(mode='w+b') as errors:
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=errors, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            last_frame = time.monotonic()
            last_progress = 0.0
            while True:
                _check_cancel(cancel_check)
                before = len(completed)
                while len(completed) < len(wanted):
                    candidate = os.path.join(output_dir, 'source_%08d.png' % (len(completed) + 1))
                    if not _completed_png(candidate):
                        break
                    completed.append(candidate)
                now = time.monotonic()
                if len(completed) > before:
                    last_frame = now
                    if callable(progress) and now - last_progress >= .5:
                        progress('GIF 正在抽取并保存动画帧：已完成 %d/%d 个独立源帧…' % (len(completed), len(wanted)))
                        last_progress = now
                _check_cancel(cancel_check)
                if process.poll() is not None:
                    if process.returncode or len(completed) != len(wanted):
                        raise AnimationError('GIF 原生抽帧失败：' + (_error_tail(errors) or '帧数不完整'))
                    _check_cancel(cancel_check)
                    success = True
                    break
                if now - last_frame > 180:
                    raise AnimationError('GIF 抽帧 180 秒没有进展，已停止。')
                time.sleep(.05)
        finally:
            if process is not None:
                _stop_process(process)
            if not success:
                for name in os.listdir(output_dir):
                    if name.startswith('source_') and name.endswith('.png'):
                        try:
                            os.remove(os.path.join(output_dir, name))
                        except OSError:
                            pass
    by_index = dict(zip(wanted, completed))
    return [by_index[index] for index in source_indices], {
        'extraction_backend': 'ffmpeg_gif_exact_indices_rgba_png',
        'ffmpeg_threads': threads, 'png_compression_level': 0,
        'source_size': [width, height], 'unique_source_frames': len(wanted),
        'selected_frames': len(source_indices), 'extraction_seconds': time.perf_counter() - started,
    }
