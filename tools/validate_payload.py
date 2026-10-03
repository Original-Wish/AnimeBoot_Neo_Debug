# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
# -*- coding: utf-8 -*-
"""Read-only ordinary-file payload check; never mounts or installs anything."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from animeboot_neo.builder import verify_payload


def main():
    parser = argparse.ArgumentParser(description='校验 InstallPayload 普通启动文件')
    parser.add_argument('payload', help='制作结果中的 InstallPayload 目录')
    args = parser.parse_args()
    report = verify_payload(args.payload)
    print(json.dumps({'ok': True, 'files': len(report['files']),
                      'total_bytes': sum(record['size'] for record in report['files'].values()),
                      'grub_versions': report['grub_versions'],
                      'animation_frames': report.get('animation', {}).get('frame_count', 0)},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
