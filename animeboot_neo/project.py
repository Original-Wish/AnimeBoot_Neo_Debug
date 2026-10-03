# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
# -*- coding: utf-8 -*-

# 工程数据入口：default_project 创建完整默认值，normalize_project 做结构/数值边界整理，validate_project 检查制作所需资源。
# 当前画布字段放在 project 顶层供单画布编辑器使用；canvases 保存所有静图，切换和序列化前必须 sync_canvas。
# 保存工程允许暂时缺少素材，方便整理项目；正式构建必须拒绝缺失的启用资源。不要把“能够保存草稿”理解为“能够安装”。
# format_version、app_version 和二进制 magic 一起识别当前格式；不猜测或自动转换未知格式。工程文件不是可执行脚本。

from __future__ import division

import os
import copy
import plistlib
import uuid

from .constants import BASE_HEIGHT, BASE_WIDTH
from .scenes import CANVAS_FIELDS, MAX_CANVASES, capture_canvas, sync_canvas, canvas_projects


class ProjectError(Exception):
    pass


COUNTDOWN_MODES = ("none", "circle", "bar", "number", "circle_star",
                   "circle_heart", "circle_dash", "bar_texture")

_COUNTDOWN_NAMES = {
    "none": "不显示", "circle": "白点圆环", "bar": "填充进度条",
    "number": "数字倒计时", "circle_star": "星形刻度圆环",
    "circle_heart": "心形刻度圆环", "circle_dash": "短线刻度圆环",
    "bar_texture": "贴图进度条",
}


def countdown_key(mode):
    """Map a native timeout style to its existing canvas geometry object."""
    if mode in ("circle", "circle_star", "circle_heart", "circle_dash"):
        return "circle"
    if mode in ("bar", "bar_texture", "number"):
        return "bar"
    return ""


def countdown_display_name(mode):
    return _COUNTDOWN_NAMES.get(mode, "倒计时")


def new_object_id(prefix="obj"):
    return "%s_%s" % (prefix, uuid.uuid4().hex[:12])


def default_project(width=1920, height=1080):
    width = int(width)
    height = int(height)
    circle_size = int(min(width * 0.40, height * 0.70))
    circle_x = int((width - circle_size) / 2)
    circle_y = int(height * 0.055)
    logo_size = int(circle_size * 0.71)
    company_scale = min(width / float(BASE_WIDTH), height / float(BASE_HEIGHT))
    project = {
        "format_version": 261033,
        "application": "AnimeBoot Neo Debug",
        "app_version": "2610.3",
        "show_debug_info": True,
        "static_windows_transition": "blend",
        "resolution": [width, height],
        "timeout": 3,
        "fit_mode": "contain",
        "countdown_mode": "none",
        "circle": {
            "x": circle_x,
            "y": circle_y,
            "w": circle_size,
            "h": circle_size,
            "num_ticks": 100,
            "tick_size": max(8, int(round(8 * company_scale))),
            "ticks_disappear": False,
            "start_angle": -64,
            "lock_aspect": True,
            "locked": False,
        },
        "logo": {
            "x": int((width - logo_size) / 2),
            "y": int(circle_y + (circle_size - logo_size) / 2),
            "w": logo_size,
            "h": logo_size,
            "path": "",
            "visible": True,
            "contain": True,
            "lock_aspect": True,
            "locked": False,
            "removed": False,
            "rotation": 0.0,
        },
        "bar": {
            "x": int(width * 0.20),
            "y": int(height * 0.78),
            "w": int(width * 0.60),
            "h": max(14, int(height * 0.025)),
            "fg": "#FFFFFF",
            "bg": "#303030",
            "border": "#FFFFFF",
            "locked": False,
            "rotation": 0.0,
        },
        "company": {
            "x": int(18 * company_scale),
            "y": int(14 * company_scale),
            "w": max(1, int(142 * company_scale)),
            "h": max(1, int(142 * company_scale)),
            "path": "",
            "visible": True,
            "contain": True,
            "lock_aspect": True,
            "locked": False,
            "removed": False,
            "rotation": 0.0,
        },
        "prompt": {
            "x": int(width * 0.18),
            "y": int(height * 0.84),
            "w": int(width * 0.64),
            "h": max(36, int(56 * company_scale)),
            "font_size": max(18, int(28 * company_scale)),
            "font_path": "",
            "color": "#FFFFFF",
            "visible": True,
            "text": "按Esc键立刻跳过本次所有开机动画",
            "lock_aspect": False,
            "locked": False,
            "removed": False,
            "rotation": 0.0,
        },
        "extra_images": [],
        "text_boxes": [],
        # Bottom -> top. Countdown is a live GRUB control and is always above
        # this flattened static stack.
        "layer_order": ["logo", "prompt", "company"],
        "boot_animation": {
            "rotation_enabled": False,
            "playlist": [],
            "enabled": False,
            "source": "",
            "max_frames": 600,
            "extract_frame_rate": 30.0,
            "sampling_mode": "head",
            "frame_rate": 60,
            "loops": 1,
            "fit_mode": "stretch",
            # Experimental dual-codec branch: QOI keeps the existing lossless/fast path;
            # JPEG trades exact pixels for much smaller real-video payloads.
            "codec": "jpeg",
            "jpeg_quality": 90,
            # Stable policy: QOI is always Raw; JPEG always uses 32-frame Chunked-XZ.
            "jpeg_xz_mode": "chunked32",
            "stream_xz_preset": 1,
        },
        "fix_gfxterm_blank": True,
        "x64_grub_compat_mode": "new",
    }
    project["active_canvas"] = 0
    project["canvases"] = [{"name": "画布 A", "canvas": capture_canvas(project)}]
    return project


def _ensure_ids(items, prefix):
    used = set()
    for item in items:
        object_id = str(item.get("id") or "")
        if not object_id or object_id in used:
            object_id = new_object_id(prefix)
        item["id"] = object_id
        used.add(object_id)


def object_key(item_type, item):
    return "%s:%s" % (item_type, item["id"])


def find_dynamic_item(project, key):
    if ":" not in key:
        return None
    kind, object_id = key.split(":", 1)
    collection = "extra_images" if kind == "extra" else "text_boxes" if kind == "text" else None
    if not collection:
        return None
    for item in project.get(collection, []):
        if item.get("id") == object_id:
            return item
    return None


def static_object_keys(project):
    keys = []
    for key in ("logo", "prompt", "company"):
        spec = project.get(key, {})
        if not spec.get("removed", False):
            keys.append(key)
    keys.extend(object_key("extra", item) for item in project.get("extra_images", []))
    keys.extend(object_key("text", item) for item in project.get("text_boxes", []))
    return keys


def normalize_layer_order(project):
    valid = static_object_keys(project)
    current = project.get("layer_order", [])
    result = []
    for key in current:
        if key in valid and key not in result:
            result.append(key)
    for key in valid:
        if key not in result:
            result.append(key)
    project["layer_order"] = result
    return result


def normalize_project(project):
    """Normalize the current edition; never migrate an earlier project."""
    if not isinstance(project, dict) or (
        project.get("format_version") != 261033
        or project.get("application") != "AnimeBoot Neo Debug"
        or project.get("app_version") != "2610.3"
    ):
        raise ProjectError("仅支持 AnimeBoot Neo Debug (2610.3) 当前版本工程，请重新创建工程。")
    forbidden = {"hotkey", "capacity_mib", "overlay_mode", "patch_initramfs",
                 "patch_guard", "patch_animebootneo_background", "patch_animebootneo_templates"}
    if forbidden.intersection(project):
        raise ProjectError("工程含非当前版本配置字段，请重新创建当前版本工程。")
    width, height = project.get("resolution", [1920, 1080])
    defaults = default_project(width, height)
    for key, value in defaults.items():
        if key not in project:
            project[key] = copy.deepcopy(value)
        elif isinstance(value, dict):
            if not isinstance(project[key], dict):
                raise ProjectError("工程对象格式无效：" + key)
            for name, field in value.items():
                project[key].setdefault(name, copy.deepcopy(field))
    animation = project["boot_animation"]
    if {"frame_seconds", "runtime_fps_override", "tile_columns", "color_bands"}.intersection(animation):
        raise ProjectError("工程含旧动画配置字段，请重新创建当前版本工程。")
    scenes = project['canvases']
    index = project['active_canvas']
    if (not isinstance(scenes, list) or not 1 <= len(scenes) <= MAX_CANVASES
            or type(index) is not int or not 0 <= index < len(scenes)):
        raise ProjectError('画布列表或当前画布序号无效（最多8张画布）')
    for scene in scenes:
        if (not isinstance(scene, dict) or set(scene) != {'name', 'canvas'}
                or not isinstance(scene['name'], str) or not scene['name'].strip()
                or len(scene['name']) > 40 or not isinstance(scene['canvas'], dict)
                or set(scene['canvas']) - set(CANVAS_FIELDS)):
            raise ProjectError('画布内容格式无效')
    _normalize_canvas_fields(project)
    sync_canvas(project)
    for n, scene in enumerate(scenes):
        if n == index:
            continue
        view = {'resolution': project['resolution']}
        for key in CANVAS_FIELDS:
            view[key] = copy.deepcopy(scene['canvas'].get(key, defaults[key]))
        _normalize_canvas_fields(view)
        scene['canvas'] = capture_canvas(view)
    return project


def _normalize_canvas_fields(project):
    width, height = project['resolution']
    for items, prefix in ((project["extra_images"], "img"), (project["text_boxes"], "txt")):
        _ensure_ids(items, prefix)
        for item in items:
            item.setdefault("visible", True)
            item.setdefault("lock_aspect", False)
            item.setdefault("locked", False)
            item.setdefault("rotation", 0.0)
            if prefix == "img":
                item.setdefault("contain", False)
                item.setdefault("source_builtin", "")
                item.setdefault("display_name", "")
            else:
                item.setdefault("text", "文本")
                item.setdefault("font_path", "")
                item.setdefault("font_size", 32)
                item.setdefault("color", "#FFFFFF")
                item.setdefault("align", "left")
    for spec in [project[key] for key in ("logo", "company", "prompt", "bar")] + project["extra_images"] + project["text_boxes"]:
        try:
            value = float(spec.get("rotation", 0.0) or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        spec["rotation"] = ((value + 180.0) % 360.0) - 180.0
    if project.get("countdown_mode") in ("number", "bar_texture"):
        # Native GRUB uses its embedded 8x16 ASCII glyphs. Its label must fit
        # at least three digits even when a former progress-bar box was thin.
        spec = project["bar"]
        canvas_w, canvas_h = int(width), int(height)
        number = project.get("countdown_mode") == "number"
        minimum_w, minimum_h = (24, 16) if number else (4, 4)
        scale = float(project.get("_countdown_preview_scale", 1.0))
        spec["w"] = min(canvas_w, max(max(1, int(round(minimum_w*scale))), int(spec.get("w", minimum_w))))
        spec["h"] = min(canvas_h, max(max(1, int(round(minimum_h*scale))), int(spec.get("h", minimum_h))))
        spec["x"] = max(0, min(canvas_w - spec["w"], int(spec.get("x", 0))))
        spec["y"] = max(0, min(canvas_h - spec["h"], int(spec.get("y", 0))))
    normalize_layer_order(project)


def prune_missing_image_objects(project):
    """Remove canvas image objects whose external source files disappeared.

    Project loading remains strict for non-image resources (for example an
    enabled boot-animation source or a custom font).  Only visual image
    objects are recoverable because removing them leaves a coherent canvas.
    Returns a list of dictionaries describing the removed objects.
    """
    normalize_project(project)
    missing = []
    for index, view in enumerate(canvas_projects(project)):
        found = _prune_canvas_images(view)
        for item in found:
            item['label'] = project['canvases'][index]['name'] + '：' + item['label']
        missing.extend(found)
        project['canvases'][index]['canvas'] = capture_canvas(view)
    project.update(copy.deepcopy(project['canvases'][project['active_canvas']]['canvas']))
    return missing


def _prune_canvas_images(project):
    missing = []

    for key, label in (("logo", "Logo 图片"), ("company", "角色图片")):
        spec = project.get(key, {})
        path = str(spec.get("path", "") or "")
        if path and not os.path.isfile(path):
            missing.append({"kind": key, "label": label, "path": path})
            spec["path"] = ""
            spec["visible"] = False
            spec["removed"] = True

    kept = []
    for item in project.get("extra_images", []):
        # Built-in artwork is supplied by this edition and has no external path.
        if item.get("source_builtin") in ("logo", "company"):
            kept.append(item)
            continue
        path = str(item.get("path", "") or "")
        if path and not os.path.isfile(path):
            label = str(item.get("display_name", "") or item.get("id", "额外图片"))
            missing.append({"kind": "extra", "label": label, "path": path})
            continue
        kept.append(item)
    project["extra_images"] = kept
    normalize_layer_order(project)
    return missing


def prune_missing_animation_source(project):
    """Recover a saved project when its external animation source disappeared.

    Canvas image objects already have a tolerant load path.  Treat the boot
    animation source the same way: preserve codec/FPS/quality settings, clear
    only the stale external path, and disable animation when the saved project
    had it enabled.  Returns a description dictionary, or None when the source
    is still valid (or empty).
    """
    normalize_project(project)
    animation = project.setdefault("boot_animation", {})
    missing_playlist=[item['source'] for item in animation.get('playlist',[]) if not os.path.exists(item['source'])]
    if missing_playlist:
        animation['playlist']=[item for item in animation['playlist'] if os.path.exists(item['source'])]
        animation['rotation_enabled']=False
    source = str(animation.get("source", "") or "").strip()
    if not source or os.path.exists(source):
        if missing_playlist:return {'path':'\n'.join(missing_playlist),'was_enabled':False,'source_kind':'轮换素材（轮换已关闭）'}
        return None

    was_enabled = bool(animation.get("enabled", False))
    suffix = os.path.splitext(source)[1].lower()
    video_suffixes = (".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".mpg", ".mpeg", ".wmv", ".flv")
    source_kind = "视频文件" if suffix in video_suffixes else "PNG图片帧文件夹/动画来源"
    animation["source"] = ""
    if was_enabled:
        animation["enabled"] = False
    return {
        "path": source,
        "was_enabled": was_enabled,
        "source_kind": source_kind,
    }


PROJECT_MAGIC = b"ANBR3C1\0"


def save_project(project, path):
    normalize_project(project)
    payload = plistlib.dumps(project, fmt=plistlib.FMT_BINARY, sort_keys=False)
    with open(path, "wb") as stream:
        stream.write(PROJECT_MAGIC)
        stream.write(payload)


def load_project(
    path,
    tolerate_missing_images=False,
    return_missing_images=False,
    tolerate_missing_animation=False,
    return_missing_animation=False,
):
    with open(path, "rb") as stream:
        raw = stream.read()
    if not raw.startswith(PROJECT_MAGIC):
        raise ProjectError("不是有效的 AnimeBoot Neo Debug 工程文件")
    project = plistlib.loads(raw[len(PROJECT_MAGIC):])
    normalize_project(project)
    missing_images = []
    missing_animation = None
    if tolerate_missing_images:
        missing_images = prune_missing_image_objects(project)
    if tolerate_missing_animation:
        missing_animation = prune_missing_animation_source(project)
    validate_project(project)
    if return_missing_images and return_missing_animation:
        return project, missing_images, missing_animation
    if return_missing_images:
        return project, missing_images
    if return_missing_animation:
        return project, missing_animation
    return project


def validate_project(project):
    normalize_project(project)
    from .rotation import validate_rotation
    try:validate_rotation(project)
    except ValueError as error:raise ProjectError(str(error))
    try:
        width, height = project["resolution"]
        width = int(width)
        height = int(height)
        timeout = int(project["timeout"])
    except (KeyError, TypeError, ValueError):
        raise ProjectError("Project has missing or invalid basic fields")
    if not (320 <= width <= 8192 and 200 <= height <= 8192):
        raise ProjectError("Resolution is outside 320x200 to 8192x8192")
    if not (0 <= timeout <= 180):
        raise ProjectError("Timeout must be between 0 and 180 seconds")
    if project.get("countdown_mode") not in COUNTDOWN_MODES:
        raise ProjectError("Unknown countdown mode")
    if project.get("static_windows_transition") not in ("blend", "black"):
        raise ProjectError("Unknown static Windows transition")
    animation = project.get("boot_animation", {})
    if animation.get("enabled"):
        source = str(animation.get("source", ""))
        if not source or not os.path.exists(source):
            raise ProjectError("Boot animation source does not exist: %s" % source)
        try:
            max_frames = int(animation.get("max_frames", 600))
            extract_frame_rate = float(animation.get("extract_frame_rate", animation.get("frame_rate", 60)))
            frame_rate = int(animation.get("frame_rate", 60))
            loops = int(animation.get("loops", 1))
        except (TypeError, ValueError):
            raise ProjectError("Boot animation numeric settings are invalid")
        if not (1 <= max_frames <= 6000):
            raise ProjectError("Boot animation max frames must be 1 through 6000")
        if not (0.1 <= extract_frame_rate <= 1000.0):
            raise ProjectError("Boot animation extraction frame rate must be 0.1 through 1000 FPS")
        if animation.get("sampling_mode") not in ("head", "uniform"):
            raise ProjectError("Unknown boot animation sampling mode")
        if not (1 <= frame_rate <= 1000):
            raise ProjectError("Boot animation frame rate must be an integer from 1 through 1000 FPS")
        if not (1 <= loops <= 10):
            raise ProjectError("Boot animation loops must be 1 through 10")
        if animation.get("fit_mode") not in ("stretch", "contain", "cover"):
            raise ProjectError("Unknown boot animation fit mode")
        if str(animation.get("codec", "jpeg") or "jpeg").lower() not in ("qoi", "jpeg"):
            raise ProjectError("Unknown boot animation codec")
        try:
            jpeg_quality = int(animation.get("jpeg_quality", 90))
        except (TypeError, ValueError):
            raise ProjectError("Boot animation JPEG quality is invalid")
        if not (60 <= jpeg_quality <= 100):
            raise ProjectError("Boot animation JPEG quality must be 60 through 100")
        if int(animation.get("stream_xz_preset", 1)) != 1:
            raise ProjectError("Boot animation payload compression preset is fixed at 1 in this build")
        if str(animation.get("jpeg_xz_mode", "chunked32") or "chunked32").lower() != "chunked32":
            raise ProjectError("JPEG XZ payload mode is fixed at chunked32")
    for view in canvas_projects(project):
        if view.get('countdown_mode') not in COUNTDOWN_MODES:
            raise ProjectError('画布含无效倒计时样式')
        _validate_canvas_resources(view)
    return True


def _validate_canvas_resources(project):
    for image_spec in (project.get("logo", {}), project.get("company", {})):
        if image_spec.get("path") and not os.path.isfile(image_spec["path"]):
            raise ProjectError("Image does not exist: %s" % image_spec["path"])
    for extra in project.get("extra_images", []):
        if extra.get("source_builtin") in ("logo", "company"):
            continue
        if extra.get("path") and not os.path.isfile(extra["path"]):
            raise ProjectError("Image does not exist: %s" % extra["path"])
    for text in project.get("text_boxes", []):
        font_path = text.get("font_path", "")
        if font_path and not os.path.isfile(font_path):
            raise ProjectError("Font does not exist: %s" % font_path)
    font_path = project.get('prompt', {}).get('font_path', '')
    if font_path and not os.path.isfile(font_path):
        raise ProjectError('Font does not exist: %s' % font_path)
    normalize_layer_order(project)
    return True

