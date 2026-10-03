# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
# -*- coding: utf-8 -*-
"""Build the Windows debug executable for AnimeBoot Neo Debug."""
from __future__ import print_function

import os
import argparse
import shutil
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"


def _check_runtime():
    if sys.version_info[:2] != (3, 8):
        raise SystemExit("请使用 Python 3.8.x 构建 Win7 兼容 Debug 版。")
    if struct.calcsize("P") * 8 != 32:
        raise SystemExit("当前 Python 不是 32 位，无法生成Win7 x86 兼容构建。")
    try:
        import PySide2  # noqa: F401
        import PIL  # noqa: F401
        import imageio_ffmpeg  # noqa: F401
        import PyInstaller  # noqa: F401
        import fontTools  # noqa: F401
    except ImportError as exc:
        raise SystemExit(
            "缺少构建依赖，请执行：python -m pip install -r requirements-build.txt"
        ) from exc
    import PySide2
    if getattr(PySide2, "__version__", "") != "5.15.2.1":
        raise SystemExit("PySide2 必须使用 5.15.2.1。")


def build_command(onefile=False):
    output_name = "AnimeBoot Neo Debug"
    # Keep the ordinary directory build at the familiar ``dist`` location so
    # a user running ``python build.py`` can find the executable immediately.
    # The single-file build is isolated under ``dist/single`` because PyInstaller
    # otherwise leaves a directory with the same application name beside it.
    mode = "single" if onefile else "folder"
    work = BUILD / mode
    dist = DIST / "single" if onefile else DIST
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--onefile" if onefile else "--onedir",
        "--windowed", "--name", output_name,
        "--distpath", str(dist), "--workpath", str(work),
        "--specpath", str(work),
        "--icon", str(ROOT / "assets" / "app_icon.ico"),
        "--add-data", str(ROOT / "assets") + ";assets",
        # 许可正文随程序，SRPM/源码压缩包属于源码交付材料，不嵌入EXE。
        # 否则一份内核源码会被单文件程序每次启动重新解包，毫无运行用途。
        "--add-data", str(ROOT / "LICENSE") + ";licenses",
        "--collect-submodules", "animeboot_neo",
        "--collect-all", "imageio_ffmpeg",
        str(ROOT / "app.py"),
    ]
    for path in sorted((ROOT / "licenses").rglob("*")):
        if path.is_file() and 'sources' not in path.relative_to(ROOT / 'licenses').parts and not path.name.endswith(('.rpm','.tar.gz','.tar.xz','.tar.bz2')):
            target=Path('licenses')/path.parent.relative_to(ROOT/'licenses')
            command[-1:-1]=['--add-data',str(path)+';'+str(target)]
    exe = dist / (output_name + ".exe") if onefile else dist / output_name / (output_name + ".exe")
    return command, exe


def main():
    parser = argparse.ArgumentParser(description="AnimeBoot Neo Debug 打包工具")
    parser.add_argument("--onefile", action="store_true", help="生成单文件 EXE；默认生成目录版")
    args = parser.parse_args()
    if os.name != "nt":
        raise SystemExit("请在 Windows 中构建。")
    _check_runtime()
    command, exe = build_command(args.onefile)
    print("开始构建 AnimeBoot Neo Debug …")
    subprocess.check_call(command, cwd=str(ROOT))
    if not exe.is_file():
        raise SystemExit("PyInstaller 完成但没有找到输出 EXE：%s" % exe)
    print("构建完成：%s" % exe)


if __name__ == "__main__":
    main()
