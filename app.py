# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
# -*- coding: utf-8 -*-
import argparse
import sys

# Kept as an explicit packaging/test inventory only; this tuple is NOT used by
# argparse for validation.  deployment.py remains the single action validator.
KNOWN_ELEVATED_ACTIONS = (
    "install",
    "install_full",
    "install_preflight",
    "final",
    "uninstall_boot_item",
    "set_runtime_fps",
    "probe_boot_space",
)



def _run_qoi_worker(argv):
    if "--qoi-batch-worker" not in argv:
        return None
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--qoi-batch-worker")
    args, _ = parser.parse_known_args(argv)
    if not args.qoi_batch_worker:
        return 2
    from animeboot_neo.animation import qoi_batch_worker_main
    return qoi_batch_worker_main(args.qoi_batch_worker)


def _run_jpeg_worker(argv):
    if "--jpeg-batch-worker" not in argv:
        return None
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--jpeg-batch-worker")
    args, _ = parser.parse_known_args(argv)
    if not args.jpeg_batch_worker:
        return 2
    from animeboot_neo.animation import jpeg_batch_worker_main
    return jpeg_batch_worker_main(args.jpeg_batch_worker)

def _run_elevated(argv):
    parser = argparse.ArgumentParser(add_help=False)
    # Do not duplicate the deployment action whitelist here.  The elevated child is
    # a GUI/--windowed executable, so argparse trying to print an invalid-choice
    # usage message can itself crash when sys.stderr is None.  deployment.py is
    # the single source of truth and performs the controlled action validation.
    parser.add_argument("--elevated-action")
    parser.add_argument("--payload")
    parser.add_argument("--result", default="")
    args, _ = parser.parse_known_args(argv)
    if not args.elevated_action:
        return None
    from animeboot_neo.deployment import run_payload_action
    code, _result = run_payload_action(args.elevated_action, args.payload or "", args.result)
    return code


def _run_frame_pipeline(argv):
    if '--frame-pipeline-worker' not in argv:
        return None
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--frame-pipeline-worker')
    args, _ = parser.parse_known_args(argv)
    from animeboot_neo.animation import frame_pipeline_worker_main
    return frame_pipeline_worker_main(args.frame_pipeline_worker)


def _run_gui():
    try:
        from animeboot_neo.qt_gui import main
    except ModuleNotFoundError as exc:
        if exc.name == "PySide2" or str(exc.name).startswith("PySide2."):
            sys.stderr.write(
                "当前 Python 未安装 PySide2。请确认使用 Python 3.8.10 32 位，然后执行：\n"
                "python -m pip install PySide2==5.15.2.1 Pillow imageio-ffmpeg PyInstaller\n"
            )
            return 2
        raise
    return main()


if __name__ == "__main__":
    code = _run_frame_pipeline(sys.argv[1:])
    if code is not None:
        raise SystemExit(code)
    code = _run_qoi_worker(sys.argv[1:])
    if code is not None:
        raise SystemExit(code)
    code = _run_jpeg_worker(sys.argv[1:])
    if code is not None:
        raise SystemExit(code)
    code = _run_elevated(sys.argv[1:])
    if code is not None:
        raise SystemExit(code)
    from animeboot_neo.deployment import cleanup_action_temp
    cleanup_action_temp()
    raise SystemExit(_run_gui())
