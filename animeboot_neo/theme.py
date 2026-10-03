# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
# -*- coding: utf-8 -*-

# 静图渲染器将当前场景的图片、中文文字、对象层序合成为back PNG；倒计时控件保留为GRUB主题元素。
# 每张画布有独立style文本，因此可以独立选控件、位置、颜色；多张style仍由同一grub.cfg选择。
# 圆环需要刻度贴图与透明中心；贴图条使用九宫格，必须成组复用。无控件则不输出无用控件贴图。
# 多画布相同资源按实际PNG字节去重，不能只按文件名合并；不同画布对象最终是否同图以渲染结果为准。

from __future__ import division

import copy
import io
import math
import os

from PIL import Image, ImageDraw, ImageFont

from .constants import BASE_HEIGHT, BASE_WIDTH, THEME_ROOT
from .project import countdown_key, find_dynamic_item, normalize_project
from .fonts import installed_font_paths, resolve_font

Image.MAX_IMAGE_PIXELS = 100000000


class ThemeError(Exception):
    pass


_PATH_IMAGE_CACHE = {}
_BYTES_IMAGE_CACHE = {}
_FONT_CACHE = {}


def clear_image_cache():
    _PATH_IMAGE_CACHE.clear()
    _BYTES_IMAGE_CACHE.clear()
    _FONT_CACHE.clear()


def _png_bytes(image, optimize=True):
    buffer = io.BytesIO()
    image.save(buffer, "PNG", optimize=optimize)
    return buffer.getvalue()


def transparent_png(width=1, height=1):
    return _png_bytes(Image.new("RGBA", (max(1, width), max(1, height)), (0, 0, 0, 0)))


def _load_png(path):
    try:
        absolute = os.path.abspath(path)
        stat = os.stat(absolute)
        timestamp = int(stat.st_mtime_ns if hasattr(stat, "st_mtime_ns") else stat.st_mtime * 1000000000)
        key = (absolute, timestamp, stat.st_size)
        cached = _PATH_IMAGE_CACHE.get(key)
        if cached is not None:
            return cached
        image = Image.open(absolute)
        if image.format != "PNG":
            raise ThemeError("Only PNG can be exported to the stock GRUB theme")
        image = image.convert("RGBA")
        for old_key in list(_PATH_IMAGE_CACHE):
            if old_key[0] == absolute and old_key != key:
                del _PATH_IMAGE_CACHE[old_key]
        _PATH_IMAGE_CACHE[key] = image
        return image
    except OSError as exc:
        raise ThemeError("Cannot open PNG %s: %s" % (path, exc))


def _load_png_bytes(data):
    key = (id(data), len(data))
    cached = _BYTES_IMAGE_CACHE.get(key)
    if cached is not None:
        return cached
    image = Image.open(io.BytesIO(data)).convert("RGBA")
    _BYTES_IMAGE_CACHE[key] = image
    return image





def find_cjk_font(preferred="", text="中文字体"):
    return resolve_font(preferred, text)


def _font(size, path="", text="中文字体"):
    resolved = find_cjk_font(path, text)
    key = (resolved, max(8, int(size)))
    cached = _FONT_CACHE.get(key)
    if cached is not None:
        return cached
    if resolved:
        font = ImageFont.truetype(resolved, key[1])
    else:
        font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


def _resize_contain(image, width, height, resample=Image.Resampling.LANCZOS):
    width, height = max(1, int(width)), max(1, int(height))
    scale = min(width / float(image.width), height / float(image.height))
    size = (max(1, int(round(image.width * scale))), max(1, int(round(image.height * scale))))
    resized = image.resize(size, resample)
    result = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    result.alpha_composite(resized, ((width - resized.width) // 2, (height - resized.height) // 2))
    return result


def _resize_stretch(image, width, height, resample=Image.Resampling.LANCZOS):
    return image.resize((max(1, int(width)), max(1, int(height))), resample)


def _rotation_degrees(spec):
    try:
        return float(spec.get("rotation", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _composite_rotated_layer(canvas, layer, rect, resample=Image.Resampling.LANCZOS):
    """Composite a local object layer around the center of its nominal x/y/w/h box."""
    x, y = int(rect.get("x", 0)), int(rect.get("y", 0))
    w, h = max(1, int(rect.get("w", layer.width))), max(1, int(rect.get("h", layer.height)))
    angle = _rotation_degrees(rect)
    if abs(angle) > 0.0001:
        # Pillow rotate() supports nearest/bilinear/bicubic, not LANCZOS.
        # Keep high-quality LANCZOS for scaling, then use bicubic for arbitrary rotation.
        rotate_resample = resample if resample in (Image.Resampling.NEAREST, Image.Resampling.BILINEAR, Image.Resampling.BICUBIC) else Image.Resampling.BICUBIC
        # Pillow uses positive = counter-clockwise; the GUI exposes positive = clockwise.
        layer = layer.rotate(-angle, expand=True, resample=rotate_resample)
    px = int(round(x + w / 2.0 - layer.width / 2.0))
    py = int(round(y + h / 2.0 - layer.height / 2.0))
    canvas.alpha_composite(layer, (px, py))


def _composite_rect(canvas, image, rect, contain=False, resample=Image.Resampling.LANCZOS):
    w, h = max(1, int(rect.get("w", image.width))), max(1, int(rect.get("h", image.height)))
    resized = _resize_contain(image, w, h, resample) if contain else _resize_stretch(image, w, h, resample)
    _composite_rotated_layer(canvas, resized, rect, resample)


def _hex_color(value, default="#FFFFFF"):
    value = str(value or default)
    if len(value) == 7 and value.startswith("#"):
        try:
            return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5)) + (255,)
        except ValueError:
            pass
    return (255, 255, 255, 255)


def _countdown_tick(mode, size, original_tick, resample=Image.Resampling.LANCZOS):
    """Create the single PNG used by the stock circular_progress widget."""
    size = max(1, int(size))
    if mode == "circle":
        return original_tick.resize((size, size), resample)
    scale = 4
    side = size * scale
    image = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    color = (255, 255, 255, 255)
    center = (side - 1) / 2.0
    radius = max(0.5, (side - 1) / 2.0)
    if mode == "circle_star":
        vertices = []
        for index in range(10):
            angle = -math.pi / 2.0 + index * math.pi / 5.0
            r = radius * (1.0 if index % 2 == 0 else 0.43)
            vertices.append((center + math.cos(angle) * r,
                             center + math.sin(angle) * r))
        draw.polygon(vertices, fill=color)
    elif mode == "circle_heart":
        vertices = []
        for index in range(80):
            t = index * 2.0 * math.pi / 80.0
            xx = 16.0 * math.sin(t) ** 3
            yy = 13.0 * math.cos(t) - 5.0 * math.cos(2*t) - 2.0 * math.cos(3*t) - math.cos(4*t)
            vertices.append((center + xx * radius / 16.0,
                             (12.0 - yy) * (side - 1) / 29.0))
        draw.polygon(vertices, fill=color)
    elif mode == "circle_dash":
        half = max(0.5, side * 0.16)
        top, bottom = max(0, int(center-half)), min(side-1, int(center+half))
        cap = bottom-top+1
        # Use integer capsule geometry. Older supported Pillow builds have
        # inconsistent rounded_rectangle handling for fractional thin boxes.
        draw.rectangle((cap//2, top, side-1-cap//2, bottom), fill=color)
        draw.ellipse((0, top, cap-1, bottom), fill=color)
        draw.ellipse((side-cap, top, side-1, bottom), fill=color)
    return image.resize((size, size), resample)


_BOX_POSITIONS = (("nw", 0, 0), ("n", 1, 0), ("ne", 2, 0),
                  ("w", 0, 1), ("c", 1, 1), ("e", 2, 1),
                  ("sw", 0, 2), ("s", 1, 2), ("se", 2, 2))


def _bar_texture_images(spec):
    """Two project-owned nine-slice skins, independent of signed binaries."""
    track = Image.new("RGBA", (32, 16), _hex_color(spec.get("bg"), "#303030"))
    ImageDraw.Draw(track).rectangle((0, 0, 31, 15), outline=_hex_color(spec.get("border")))
    fill = Image.new("RGBA", (128, 32))
    foreground = _hex_color(spec.get("fg"))
    pixels = fill.load()
    for y in range(fill.height):
        # Gloss and alternating diagonal stripes are static pixels. Only the
        # filled width changes; no arbitrary frame animation is claimed.
        gloss = 0.72 + 0.28 * (1.0 - y / float(fill.height - 1))
        for x in range(fill.width):
            shade = gloss * (1.0 if ((x+y)//12) % 2 == 0 else 0.70)
            pixels[x, y] = tuple(int(component * shade) for component in foreground[:3]) + (255,)
    return track, fill


def _nine_slice_parts(image):
    xs, ys = (0, 1, image.width-1, image.width), (0, 1, image.height-1, image.height)
    return {name: image.crop((xs[col], ys[row], xs[col+1], ys[row+1]))
            for name, col, row in _BOX_POSITIONS}


def _resize_nine_slice(image, width, height, resample=Image.Resampling.LANCZOS):
    width, height = max(2, int(width)), max(2, int(height))
    result = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    parts = _nine_slice_parts(image)
    xs, ys = (0, 1, width-1, width), (0, 1, height-1, height)
    for name, col, row in _BOX_POSITIONS:
        w, h = xs[col+1]-xs[col], ys[row+1]-ys[row]
        if w > 0 and h > 0:
            result.alpha_composite(parts[name].resize((w, h), resample), (xs[col], ys[row]))
    return result


def _draw_preview_countdown(canvas, project, fraction, original_tick, resample=Image.Resampling.LANCZOS):
    """Draw elapsed progress: 0 -> empty, 1 -> complete.

    GRUB's timeout component updates once per second. The GUI supplies stepped
    fractions, so a six-second timeout shows exactly 0/6, 1/6 ... 6/6.
    """
    mode = project.get("countdown_mode", "circle")
    fraction = max(0.0, min(1.0, float(fraction)))
    draw = ImageDraw.Draw(canvas)
    if countdown_key(mode) == "circle":
        spec = project["circle"]
        x, y, w, h = [int(spec[key]) for key in ("x", "y", "w", "h")]
        tick_size = max(1, int(spec.get("tick_size", 8)))
        num_ticks = max(1, int(spec.get("num_ticks", 100)))
        tick = _countdown_tick(mode, tick_size, original_tick, resample)
        radius_x = max(0.0, w / 2.0 - tick_size / 2.0 - 1)
        radius_y = max(0.0, h / 2.0 - tick_size / 2.0 - 1)
        cx, cy = x + w / 2.0, y + h / 2.0
        visible = int(round(num_ticks * fraction))
        indices = range(visible, num_ticks) if spec.get("ticks_disappear") else range(visible)
        start = int(spec.get("start_angle", -64))
        # Positive angles move clockwise in image coordinates because Y grows down.
        for index in indices:
            angle = (start + index * 256.0 / num_ticks) * 2.0 * math.pi / 256.0
            px = int(round(cx + math.cos(angle) * radius_x - tick_size / 2.0))
            py = int(round(cy + math.sin(angle) * radius_y - tick_size / 2.0))
            canvas.alpha_composite(tick, (px, py))
    elif mode in ("bar", "bar_texture"):
        spec = project["bar"]
        x, y, w, h = [int(spec[key]) for key in ("x", "y", "w", "h")]
        if mode == "bar_texture":
            track, fill = _bar_texture_images(spec)
            canvas.alpha_composite(_resize_nine_slice(track, w, h, resample), (x, y))
            fill_w = max(0, int((w-2) * fraction))
            if fill_w >= 2 and h >= 4:
                canvas.alpha_composite(_resize_nine_slice(fill, fill_w, h-2, resample), (x+1, y+1))
            return
        draw.rectangle((x, y, x + w, y + h), fill=_hex_color(spec.get("bg"), "#303030"), outline=_hex_color(spec.get("border"), "#FFFFFF"))
        fill_w = max(0, int(round((w - 2) * fraction)))
        if fill_w:
            draw.rectangle((x + 1, y + 1, x + fill_w, y + h - 1), fill=_hex_color(spec.get("fg"), "#FFFFFF"))
    elif mode == "number":
        spec = project["bar"]
        x, y, w = [int(spec[key]) for key in ("x", "y", "w")]
        remaining = max(0, int(round(int(project.get("timeout", 3)) * (1.0-fraction))))
        # The signed image's built-in ASCII fallback is fixed at 16px. GUI
        # thumbnails scale its preview along with the complete target canvas.
        font = _font(16, text=str(remaining))
        text = str(remaining)
        box = draw.textbbox((0, 0), text, font=font)
        text_w = box[2]-box[0]
        label = Image.new("RGBA", (max(1, text_w), 16), (0, 0, 0, 0))
        ImageDraw.Draw(label).text((-box[0], -box[1]), text, font=font,
                                   fill=_hex_color(spec.get("fg")))
        scale = float(project.get("_countdown_preview_scale", 1.0))
        if abs(scale-1.0) > 0.0001:
            label = label.resize((max(1, int(round(label.width*scale))),
                                  max(1, int(round(label.height*scale)))), resample)
        canvas.alpha_composite(label, (x+(w-label.width)//2, y))


def _render_prompt(canvas, project, font_path=""):
    spec = dict(project.get("prompt", {}))
    spec.setdefault("text", "按Esc键立刻跳过本次所有开机动画")
    spec["align"] = "center"
    _render_text_box(canvas, spec, font_path)



def _wrap_text(draw, text, font, max_width):
    lines = []
    for paragraph in str(text).splitlines() or [""]:
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for char in paragraph:
            trial = current + char
            box = draw.textbbox((0, 0), trial, font=font)
            if current and box[2] - box[0] > max_width:
                lines.append(current)
                current = char
            else:
                current = trial
        lines.append(current)
    return lines


def _render_text_box(canvas, spec, fallback_font=""):
    if not spec.get("visible", True):
        return
    x, y, w, h = [int(spec.get(name, 0)) for name in ("x", "y", "w", "h")]
    if w <= 0 or h <= 0:
        return
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    font = _font(spec.get("font_size", 32), spec.get("font_path") or fallback_font, spec.get("text", ""))
    lines = _wrap_text(draw, spec.get("text", ""), font, w)
    box = draw.textbbox((0, 0), "国Ag", font=font)
    line_h = max(1, box[3] - box[1] + max(1, int(spec.get("font_size", 32) * 0.18)))
    align = spec.get("align", "left")
    color = _hex_color(spec.get("color"), "#FFFFFF")
    yy = 0
    for line in lines:
        if yy + line_h > h:
            break
        line_box = draw.textbbox((0, 0), line, font=font)
        line_w = line_box[2] - line_box[0]
        xx = (w - line_w) // 2 if align == "center" else w - line_w if align == "right" else 0
        draw.text((xx, yy - line_box[1]), line, font=font, fill=color)
        yy += line_h
    _composite_rotated_layer(canvas, layer, spec, Image.Resampling.LANCZOS)


def _render_static_object(canvas, project, key, original_files, font_path="", resample=Image.Resampling.LANCZOS):
    if key == "logo":
        spec = project.get("logo", {})
        if not spec.get("visible", True):
            return
        original = _load_png_bytes(original_files[THEME_ROOT + "/logo.png"])
        source = _load_png(spec["path"]) if spec.get("path") else original
        _composite_rect(canvas, source, spec, contain=bool(spec.get("contain", True)), resample=resample)
    elif key == "company":
        spec = project.get("company", {})
        if not spec.get("visible", True):
            return
        original = _load_png_bytes(original_files[THEME_ROOT + "/CompanyLogo.png"])
        source = _load_png(spec["path"]) if spec.get("path") else original
        _composite_rect(canvas, source, spec, contain=bool(spec.get("contain", True)), resample=resample)
    elif key == "prompt":
        _render_prompt(canvas, project, font_path)
    elif key.startswith("extra:"):
        spec = find_dynamic_item(project, key)
        if spec and spec.get("visible", True):
            builtin = spec.get("source_builtin", "")
            if builtin == "logo":
                source = _load_png_bytes(original_files[THEME_ROOT + "/logo.png"])
            elif builtin == "company":
                source = _load_png_bytes(original_files[THEME_ROOT + "/CompanyLogo.png"])
            else:
                source = _load_png(spec["path"])
            _composite_rect(canvas, source, spec, contain=bool(spec.get("contain", False)), resample=resample)
    elif key.startswith("text:"):
        spec = find_dynamic_item(project, key)
        if spec:
            _render_text_box(canvas, spec, font_path)


def _compose_static(project, original_files, font_path="", draft=False):
    normalize_project(project)
    width, height = [int(value) for value in project["resolution"]]
    resample = Image.Resampling.BILINEAR if draft else Image.Resampling.LANCZOS
    base = Image.new("RGBA", (width, height), (0, 0, 0, 255))
    for key in project.get("layer_order", []):
        _render_static_object(base, project, key, original_files, font_path, resample)
    return base




def _grub_rect(project, spec):
    """Return target-canvas pixel geometry, clamped to the selected resolution."""
    canvas_w, canvas_h = [max(1, int(value)) for value in project["resolution"]]
    x = max(0, int(round(float(spec.get("x", 0)))))
    y = max(0, int(round(float(spec.get("y", 0)))))
    w = max(1, int(round(float(spec.get("w", 1)))))
    h = max(1, int(round(float(spec.get("h", 1)))))
    if x + w > canvas_w:
        w = max(1, canvas_w - x)
    if y + h > canvas_h:
        h = max(1, canvas_h - y)
    return x, y, w, h


def _proportional_expression(pixels, total):
    """Return a GRUB proportional expression valid in the stock parser.

    GRUB's theme parser accepts *integer* percentage terms and integer pixel
    terms in the same expression (for example ``30%+5``); it does not accept
    decimal percentages.  Choose the nearest whole percentage, then add a
    small pixel residual so the preferred canvas lands on the editor's pixel
    coordinate while lower GOP fallbacks stay approximately proportional.
    """
    pixels = int(pixels)
    total = max(1, int(total))
    percent = max(0, min(100, int(round(pixels * 100.0 / total))))
    # GRUB fixed-point multiplication truncates to an integer screen pixel.
    proportional_pixels = (total * percent) // 100
    residual = pixels - proportional_pixels
    if residual > 0:
        return "%d%%+%d" % (percent, residual)
    if residual < 0:
        return "%d%%-%d" % (percent, -residual)
    return "%d%%" % percent


def _theme_rect(project, spec):
    """Return pixel geometry plus fallback-safe GRUB expressions.

    The editor stores exact pixels on the preferred Windows/native canvas.
    Runtime expressions use an integer percentage plus a small pixel residual,
    e.g. ``30%+5``.  This is exact on the preferred canvas and remains close to
    the same relative location if firmware selects a lower GOP fallback.
    """
    x, y, w, h = _grub_rect(project, spec)
    canvas_w, canvas_h = [max(1, int(value)) for value in project["resolution"]]
    return (
        (x, y, w, h),
        (
            _proportional_expression(x, canvas_w),
            _proportional_expression(y, canvas_h),
            _proportional_expression(w, canvas_w),
            _proportional_expression(h, canvas_h),
        ),
    )


def _grub_tick_size(project, tick_size):
    value = max(1, int(round(float(tick_size))))
    return value, value


def generate_style(project):
    mode = project.get("countdown_mode", "circle")
    lines = [
        'title-text:""',
        'desktop-image: "back.png"',
        'desktop-image-scale-method: "stretch"',
        'terminal-left: "0"',
        'terminal-top: "0"',
        'terminal-width: "100%"',
        'terminal-height: "100%"',
        'terminal-border: "0"',
        "",
    ]
    if countdown_key(mode) == "circle":
        spec = project["circle"]
        pixels, values = _theme_rect(project, spec)
        px, py, pw, ph = pixels
        left, top, width, height = values
        lines.extend([
            "# Preferred canvas: %dx%d; pixel rect: left=%d top=%d width=%d height=%d"
            % (int(project["resolution"][0]), int(project["resolution"][1]), px, py, pw, ph),
            "# Runtime uses integer % plus pixel residual (for example 30%+5); decimal % is invalid in stock GRUB.",
            "+ circular_progress", "{", '    id = "__timeout__"',
            "    left = %s" % left, "    top = %s" % top,
            "    width = %s" % width, "    height = %s" % height,
            "    num_ticks = %d" % int(spec.get("num_ticks", 100)),
            "    ticks_disappear = %s" % ("true" if spec.get("ticks_disappear") else "false"),
            "    start_angle = %d" % int(spec.get("start_angle", -64)),
            '    tick_bitmap = "tick.png"', '    center_bitmap = "circle-center.png"', "}", "",
        ])
    elif mode in ("bar", "bar_texture"):
        spec = project["bar"]
        pixels, values = _theme_rect(project, spec)
        px, py, pw, ph = pixels
        left, top, width, height = values
        lines.extend([
            "# Preferred canvas: %dx%d; pixel rect: left=%d top=%d width=%d height=%d"
            % (int(project["resolution"][0]), int(project["resolution"][1]), px, py, pw, ph),
            "# Runtime uses integer % plus pixel residual (for example 30%+5); decimal % is invalid in stock GRUB.",
            "+ progress_bar", "{", '    id = "__timeout__"',
            "    left = %s" % left, "    top = %s" % top,
            "    width = %s" % width, "    height = %s" % height,
            "    show_text = false", '    fg_color = "%s"' % spec.get("fg", "#FFFFFF"),
            '    bg_color = "%s"' % spec.get("bg", "#303030"),
            '    border_color = "%s"' % spec.get("border", "#FFFFFF"),
        ])
        if mode == "bar_texture":
            lines.extend(['    bar_style = "bar-track_*.png"',
                          '    highlight_style = "bar-fill_*.png"',
                          '    highlight_overlay = false'])
        lines.extend(["}", ""])
    elif mode == "number":
        spec = project["bar"]
        _pixels, values = _theme_rect(project, spec)
        left, top, width, height = values
        lines.extend([
            "+ label", "{", '    id = "__timeout__"',
            "    left = %s" % left, "    top = %s" % top,
            "    width = %s" % width, "    height = %s" % height,
            '    font = "Unknown Regular 16"', '    color = "%s"' % spec.get("fg", "#FFFFFF"),
            '    align = "center"', '    text = "%d"', "}", "",
        ])
    return "\n".join(line for line in lines if not line.lstrip().startswith("#")).encode("utf-8")


def _compose_preview(project, original_files, preview_fraction=1.0, font_path="", draft=False):
    base = _compose_static(project, original_files, font_path, draft=draft)
    tick = _load_png_bytes(original_files[THEME_ROOT + "/tick.png"])
    _draw_preview_countdown(base, project, preview_fraction, tick, Image.Resampling.BILINEAR if draft else Image.Resampling.LANCZOS)
    return base


def _scaled_preview_project(project, target_width, target_height):
    normalize_project(project)
    source_width, source_height = [float(value) for value in project["resolution"]]
    sx, sy = float(target_width) / source_width, float(target_height) / source_height
    scaled = copy.deepcopy(project)
    scaled["resolution"] = [max(1, int(round(target_width))), max(1, int(round(target_height)))]
    if project.get("countdown_mode") in ("number", "bar_texture"):
        scaled["_countdown_preview_scale"] = min(sx, sy)
    for key in ("circle", "logo", "bar", "company", "prompt"):
        spec = scaled.get(key, {})
        for name, factor in (("x", sx), ("y", sy), ("w", sx), ("h", sy)):
            if name in spec:
                value = int(round(spec[name] * factor))
                # Signed x/y are intentional for rotated objects near the top or
                # left edge.  Only dimensions are constrained positive.
                spec[name] = max(1, value) if name in ("w", "h") else value
        if "font_size" in spec:
            spec["font_size"] = max(8, int(round(spec["font_size"] * min(sx, sy))))
    if "tick_size" in scaled.get("circle", {}):
        scaled["circle"]["tick_size"] = max(1, int(round(scaled["circle"]["tick_size"] * min(sx, sy))))
    for collection in ("extra_images", "text_boxes"):
        for item in scaled.get(collection, []):
            for name, factor in (("x", sx), ("y", sy), ("w", sx), ("h", sy)):
                value = int(round(item[name] * factor))
                item[name] = max(1, value) if name in ("w", "h") else value
            if "font_size" in item:
                item["font_size"] = max(8, int(round(item["font_size"] * min(sx, sy))))
    return scaled


def render_preview_scaled(project, original_files, target_width, target_height, preview_fraction=1.0, font_path="", draft=False):
    scaled = _scaled_preview_project(project, target_width, target_height)
    return _compose_preview(scaled, original_files, preview_fraction, font_path, draft=draft)


def render_theme(project, original_files, preview_fraction=1.0, font_path="", include_preview=True):
    normalize_project(project)
    width, height = [int(value) for value in project["resolution"]]
    base = _compose_static(project, original_files, font_path, draft=False)
    back = base.convert("RGB")
    original_tick = _load_png_bytes(original_files[THEME_ROOT + "/tick.png"])
    tick_size = max(1, int(project.get("circle", {}).get("tick_size", 8)))
    grub_tick_w, grub_tick_h = _grub_tick_size(project, tick_size)
    mode = project.get("countdown_mode", "none")
    tick_image = _countdown_tick(mode, grub_tick_w, original_tick)
    transparent = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    assets = {
        THEME_ROOT + "/back.png": _png_bytes(back),
        THEME_ROOT + "/style.txt": generate_style(project),
    }
    if countdown_key(mode) == "circle":
        # Stock circular_progress needs both bitmaps, including a transparent
        # center. Actual canvas logos are already baked into back.png.
        assets[THEME_ROOT + "/circle-center.png"] = _png_bytes(transparent)
        assets[THEME_ROOT + "/tick.png"] = _png_bytes(tick_image)
    elif mode == "bar_texture":
        for prefix, image in zip(("bar-track", "bar-fill"), _bar_texture_images(project["bar"])):
            for position, part in _nine_slice_parts(image).items():
                assets[THEME_ROOT + "/%s_%s.png" % (prefix, position)] = _png_bytes(part)
    preview = _compose_preview(project, original_files, preview_fraction, font_path, draft=False) if include_preview else None
    return assets, preview


def render_canvases(project, original_files, font_path="", progress=None):
    """Export one style per scene and share identical PNGs within one theme dir."""
    from .scenes import canvas_projects
    views = canvas_projects(project)
    if len(views) == 1:
        return render_theme(views[0], original_files, font_path=font_path, include_preview=False)[0]
    assets = {}; shared = {}; texture_groups = {}
    for index, view in enumerate(views):
        if callable(progress):progress('正在合成静图画布 %d/%d…' % (index+1, len(views)))
        current, _ = render_theme(view, original_files, font_path=font_path, include_preview=False)
        style = current.pop(THEME_ROOT+'/style.txt').decode('utf-8')
        replacements = {}
        for prefix in ('bar-track', 'bar-fill'):
            members = sorted((name,data) for name,data in current.items() if '/'+prefix+'_' in name)
            if not members:continue
            signature = tuple((name.rsplit('_',1)[1],data) for name,data in members)
            target_prefix = texture_groups.setdefault(signature, prefix+'-%02d' % index)
            style = style.replace(prefix+'_*.png', target_prefix+'_*.png')
            for name, data in members:
                basename = name.rsplit('/',1)[1]
                target = target_prefix+basename[len(prefix):]
                replacements[basename] = target
                assets[THEME_ROOT+'/'+target] = data
        for name, data in current.items():
            basename = name.rsplit('/',1)[1]
            if basename in replacements:continue
            # Name uses the first referring scene. No random locator or copy.
            stem, suffix = basename.rsplit('.',1)
            target = shared.setdefault(data, '%s-%02d.%s' % (stem,index,suffix))
            style = style.replace('"'+basename+'"','"'+target+'"')
            assets[THEME_ROOT+'/'+target] = data
        assets[THEME_ROOT+'/style-%02d.txt' % index] = style.encode('utf-8')
    return assets
