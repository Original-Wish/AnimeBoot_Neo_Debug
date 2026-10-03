# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
# -*- coding: utf-8 -*-
"""Resolution ordering for the custom GRUB configuration."""
from __future__ import division

from .constants import COMMON_RESOLUTIONS


def _area(item):
    return int(item[0]) * int(item[1])


def build_fallback_resolutions(width, height):
    """Selected mode first, then every known lower mode from large to small.

    GRUB will try the comma-separated modes in this exact order. A Windows mode
    is only a design preference; UEFI GOP support is decided by firmware.
    """
    target = (int(width), int(height))
    values = set((int(w), int(h)) for w, h in COMMON_RESOLUTIONS)
    values.add(target)
    lower = [item for item in values if item != target and item[0] <= target[0] and item[1] <= target[1]]
    target_ratio = target[0] / float(max(1, target[1]))
    lower.sort(
        key=lambda item: (
            _area(item),
            -abs(item[0] / float(max(1, item[1])) - target_ratio),
            item[0],
            item[1],
        ),
        reverse=True,
    )
    result = [target] + lower
    # 1024x768 remains the last compatibility floor when it fits below target.
    floor = (1024, 768)
    if floor in result:
        result = [item for item in result if item != floor] + [floor]
    seen = set()
    return [item for item in result if not (item in seen or seen.add(item))]


def gfxmode_value(width, height):
    modes = []
    for w, h in build_fallback_resolutions(width, height):
        modes.extend(["%dx%dx32" % (w, h), "%dx%d" % (w, h)])
    return ",".join(modes)
