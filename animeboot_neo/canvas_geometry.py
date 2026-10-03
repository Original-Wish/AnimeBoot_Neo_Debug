# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
# -*- coding: utf-8 -*-
"""Pure canvas geometry helpers used by the Qt editor.

Keep this module free of Qt dependencies so rotation/resize boundary rules can
be regression-tested even on build hosts that do not have PySide2 installed.
"""

# 编辑器几何计算：显示坐标与对象局部坐标转换、旋转、八方向缩放及画布边界约束。
# 锁定正方形时先计算共同缩放量，再一起限制宽高；不能分别裁宽高，否则到边界后会变成长方形。
# 旋转对象沿自身坐标轴缩放，对侧边/角是锚点；界面手柄命中与实际几何必须使用同一变换。


import math


def _clamp(value, low, high):
    return max(low, min(high, value))


def rotated_corners(x, y, w, h, angle):
    """Return TL/TR/BR/BL corners after rotating around the logical centre.

    Coordinates use the editor's screen convention (positive Y points down),
    so a positive angle is clockwise, matching the Qt overlay and the rendered
    static object semantics used by AnimeBootNeo.
    """
    x, y = float(x), float(y)
    w, h = max(1.0, float(w)), max(1.0, float(h))
    angle = float(angle or 0.0)
    cx, cy = x + w / 2.0, y + h / 2.0
    pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    if abs(angle) < 1.0e-9:
        return pts
    r = math.radians(angle)
    co, si = math.cos(r), math.sin(r)
    out = []
    for px, py in pts:
        dx, dy = px - cx, py - cy
        out.append((cx + dx * co - dy * si, cy + dx * si + dy * co))
    return out


def has_corner_in_canvas(rect, angle, canvas_w, canvas_h):
    """True when at least one rotated corner is still on/in the canvas."""
    x, y, w, h = rect
    cw, ch = float(canvas_w), float(canvas_h)
    # Geometry is stored as integer pixels by the project model.  A rotated
    # corner clamped to the exact edge can therefore land a sub-pixel outside
    # after rounding; one pixel of tolerance keeps the visual invariant stable.
    eps = 1.0
    return any(-eps <= px <= cw + eps and -eps <= py <= ch + eps
               for px, py in rotated_corners(x, y, w, h, angle))


def clamp_move_delta(rect, angle, dx, dy, canvas_w, canvas_h):
    """Clamp a drag delta using the editor's visibility rule.

    At 0 degrees the legacy behavior is preserved: the whole logical rectangle
    remains inside the canvas.  Once an object is tilted, up to three rotated
    corners may leave the canvas; at least one corner must remain visible.
    The nearest legal translation is selected from the union of the four
    per-corner translation rectangles, so diagonal edge motion stays natural.
    """
    x, y, w, h = [float(v) for v in rect]
    dx, dy = float(dx), float(dy)
    cw, ch = float(canvas_w), float(canvas_h)
    if abs(float(angle or 0.0)) < 1.0e-6:
        nx = _clamp(x + dx, 0.0, max(0.0, cw - w))
        ny = _clamp(y + dy, 0.0, max(0.0, ch - h))
        return nx - x, ny - y

    best = None
    best_d2 = None
    edge_eps = 1.0e-6
    for px, py in rotated_corners(x, y, w, h, angle):
        tx = _clamp(dx, -px + edge_eps, cw - px - edge_eps)
        ty = _clamp(dy, -py + edge_eps, ch - py - edge_eps)
        d2 = (tx - dx) ** 2 + (ty - dy) ** 2
        if best is None or d2 < best_d2:
            best, best_d2 = (tx, ty), d2
    return best if best is not None else (0.0, 0.0)


def _rot_local(dx, dy, angle):
    r = math.radians(float(angle or 0.0))
    co, si = math.cos(r), math.sin(r)
    return dx * co + dy * si, -dx * si + dy * co


def _rot_world(dx, dy, angle):
    r = math.radians(float(angle or 0.0))
    co, si = math.cos(r), math.sin(r)
    return dx * co - dy * si, dx * si + dy * co


def _midpoint(a, b):
    return ((float(a[0]) + float(b[0])) / 2.0,
            (float(a[1]) + float(b[1])) / 2.0)


def resize_from_rotated_handle(rect, angle, handle, point, canvas_w, canvas_h,
                               lock_aspect=False, min_size=12.0):
    """Resize from one of eight rotated handles: TL/T/TR/R/BR/B/BL/L.

    Corner handles keep the opposite corner fixed.  Edge handles keep the
    opposite edge midpoint fixed.  Mouse movement is inverse-rotated into the
    object's local axes, so rotated resizing does not skew or jump.

    ``lock_aspect`` is the editor's “保持正方形” switch.  When enabled, *every*
    handle keeps width and height equal.  This is intentionally stricter than
    the old implementation, which could independently clip width/height at a
    0-degree canvas edge and silently turn a square into a rectangle.

    At 0 degrees the whole logical rectangle remains inside the canvas.  For a
    tilted object the existing visibility rule is preserved: up to three
    rotated corners may leave the canvas, but at least one must remain visible.
    """
    x, y, ow, oh = [float(v) for v in rect]
    ow, oh = max(1.0, ow), max(1.0, oh)
    angle = float(angle or 0.0)
    cw, ch = max(1.0, float(canvas_w)), max(1.0, float(canvas_h))
    px, py = float(point[0]), float(point[1])
    min_size = max(1.0, float(min_size))
    handle = str(handle or "").lower()

    corners = rotated_corners(x, y, ow, oh, angle)
    top_mid = _midpoint(corners[0], corners[1])
    right_mid = _midpoint(corners[1], corners[2])
    bottom_mid = _midpoint(corners[3], corners[2])
    left_mid = _midpoint(corners[0], corners[3])

    # handle: (anchor point, local X sign, local Y sign, resizing axes)
    data = {
        'tl': (corners[2], -1.0, -1.0, 'both'),
        'tr': (corners[3],  1.0, -1.0, 'both'),
        'br': (corners[0],  1.0,  1.0, 'both'),
        'bl': (corners[1], -1.0,  1.0, 'both'),
        't':  (bottom_mid, 0.0, -1.0, 'y'),
        'b':  (top_mid,    0.0,  1.0, 'y'),
        'l':  (right_mid, -1.0, 0.0, 'x'),
        'r':  (left_mid,   1.0, 0.0, 'x'),
    }
    if handle not in data:
        return x, y, ow, oh

    (ax, ay), sx, sy, axes = data[handle]
    lx, ly = _rot_local(px - ax, py - ay, angle)

    if lock_aspect:
        # “保持正方形” means exactly square, including objects loaded from an
        # older project whose saved dimensions may have already drifted.
        if axes == 'both':
            desired = (sx * lx + sy * ly) / 2.0
        elif axes == 'x':
            desired = sx * lx
        else:
            desired = sy * ly
        side = max(min_size, desired)
        # Avoid pathological allocations when a pointer is dragged far outside.
        side = min(side, max(cw, ch) * 4.0)
        nw = nh = side
    elif axes == 'both':
        nw = max(min_size, sx * lx)
        nh = max(min_size, sy * ly)
        nw = min(nw, max(min_size, cw * 4.0))
        nh = min(nh, max(min_size, ch * 4.0))
    elif axes == 'x':
        nw = max(min_size, sx * lx)
        nw = min(nw, max(min_size, cw * 4.0))
        nh = oh
    else:
        nw = ow
        nh = max(min_size, sy * ly)
        nh = min(nh, max(min_size, ch * 4.0))

    def make_rect(width, height):
        if axes == 'both':
            dlx, dly = sx * width, sy * height
        elif axes == 'x':
            dlx, dly = sx * width, 0.0
        else:
            dlx, dly = 0.0, sy * height
        wdx, wdy = _rot_world(dlx, dly, angle)
        cx, cy = ax + wdx / 2.0, ay + wdy / 2.0
        return (cx - width / 2.0, cy - height / 2.0, width, height)

    if abs(angle) < 1.0e-6:
        # At zero degrees, preserve the fixed opposite handle and stop exactly
        # at the canvas edge.  Crucially, a locked square is limited by one
        # shared side length instead of clipping width/height independently.
        if lock_aspect:
            if axes == 'both':
                cap_x = ax if sx < 0.0 else cw - ax
                cap_y = ay if sy < 0.0 else ch - ay
                max_side = min(cap_x, cap_y)
            elif axes == 'x':
                cap_axis = ax if sx < 0.0 else cw - ax
                cap_perp = 2.0 * min(ay, ch - ay)
                max_side = min(cap_axis, cap_perp)
            else:
                cap_axis = ay if sy < 0.0 else ch - ay
                cap_perp = 2.0 * min(ax, cw - ax)
                max_side = min(cap_axis, cap_perp)
            side = max(1.0, min(nw, nh, max(0.0, max_side)))
            return make_rect(side, side)

        if axes in ('both', 'x'):
            cap_w = ax if sx < 0.0 else cw - ax
            nw = max(1.0, min(nw, max(0.0, cap_w)))
        if axes in ('both', 'y'):
            cap_h = ay if sy < 0.0 else ch - ay
            nh = max(1.0, min(nh, max(0.0, cap_h)))
        proposal = make_rect(nw, nh)
        # Fixed dimensions on an edge handle come from a valid starting rect,
        # but clamp tiny floating-point drift defensively.
        rx, ry, rw, rh = proposal
        rx = _clamp(rx, 0.0, max(0.0, cw - rw))
        ry = _clamp(ry, 0.0, max(0.0, ch - rh))
        return rx, ry, rw, rh

    proposal = make_rect(nw, nh)
    if has_corner_in_canvas(proposal, angle, cw, ch):
        return proposal

    # Original geometry is expected to satisfy the invariant.  Walk from it
    # toward the proposal and stop at the last geometry with a visible corner.
    # This keeps the rotated boundary feeling like a physical stop.
    if not has_corner_in_canvas((x, y, ow, oh), angle, cw, ch):
        return x, y, ow, oh
    low, high = 0.0, 1.0
    best = (x, y, ow, oh)
    for _ in range(48):
        mid = (low + high) / 2.0
        if lock_aspect:
            # A normal locked object is already square; keep all intermediate
            # candidates square too so the boundary search can never re-create
            # the old “square becomes rectangle” failure.
            start_side = min(ow, oh)
            target_side = min(nw, nh)
            mw = mh = start_side + (target_side - start_side) * mid
        else:
            mw = ow + (nw - ow) * mid
            mh = oh + (nh - oh) * mid
        candidate = make_rect(mw, mh)
        if has_corner_in_canvas(candidate, angle, cw, ch):
            best = candidate
            low = mid
        else:
            high = mid
    return best


def resize_from_rotated_corner(rect, angle, corner, point, canvas_w, canvas_h,
                               lock_aspect=False, min_size=12.0):
    """Backward-compatible four-corner wrapper used by older tests/plugins."""
    return resize_from_rotated_handle(
        rect, angle, corner, point, canvas_w, canvas_h,
        lock_aspect=lock_aspect, min_size=min_size,
    )
