# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
"""One live editor canvas, with independent saved static scenes."""

# 多静图采用一个可见编辑器：顶层是当前画布的工作副本，canvases 是有序保存槽。
# 分辨率、静图时长、启动方式、动画列表全局共用；图片/文字/层序/倒计时类型、位置、颜色属于各自画布。
# select_canvas 先保存当前槽再加载目标槽；render 时通过 canvas_projects 得到彼此独立的工程副本，避免串用对象。
# 启动序号与编辑序号完全分离。GRUB 的 neo_static_rotation 表示下一张，安装时从0开始；一次正常显示推进一次。

import copy

MAX_CANVASES = 8
CANVAS_FIELDS = ('fit_mode', 'countdown_mode', 'circle', 'bar', 'logo', 'company',
                 'prompt', 'extra_images', 'text_boxes', 'layer_order')


def capture_canvas(project):
    return {key: copy.deepcopy(project[key]) for key in CANVAS_FIELDS if key in project}


def sync_canvas(project):
    """Commit the live flat fields before switching, saving, sharing or building."""
    if not project.get('canvases'):
        project['canvases'] = [{'name': '画布 A', 'canvas': capture_canvas(project)}]
        project['active_canvas'] = 0
    index = project.get('active_canvas', 0)
    project['canvases'][index]['canvas'] = capture_canvas(project)
    return project


# 写回旧工作副本后再切换索引与顶层字段；直接覆盖顶层会丢失当前画布尚未保存的编辑。
def select_canvas(project, index):
    sync_canvas(project)
    if not 0 <= index < len(project['canvases']):
        raise ValueError('画布序号无效')
    project.update(copy.deepcopy(project['canvases'][index]['canvas']))
    project['active_canvas'] = index


def canvas_projects(project):
    sync_canvas(project)
    result = []
    for scene in project['canvases']:
        view = copy.deepcopy(project)
        view.update(copy.deepcopy(scene['canvas']))
        view['canvases'] = [copy.deepcopy(scene)]
        view['active_canvas'] = 0
        result.append(view)
    return result


def countdown_modes(project):
    # Bound boot manifests only retain the per-canvas modes, not editable paths.
    if 'static_canvas_modes' in project:
        return list(project['static_canvas_modes'])
    if 'canvases' not in project:
        return [project.get('countdown_mode', 'none')]
    index = project.get('active_canvas', 0)
    return [project.get('countdown_mode', 'none') if n == index else
            scene['canvas'].get('countdown_mode', 'none')
            for n, scene in enumerate(project['canvases'])]


def theme_paths(project):
    count = len(countdown_modes(project))
    if count == 1:
        return ['AnimeBootNeo/theme/style.txt']
    return ['AnimeBootNeo/theme/style-%02d.txt' % n for n in range(count)]


def resize_canvases(project, width, height):
    """Resolution is shared; resize every stored scene, including hidden ones."""
    sync_canvas(project)
    old_w, old_h = project['resolution']
    sx, sy = width / float(old_w), height / float(old_h)
    for scene in project['canvases']:
        canvas = scene['canvas']
        objects = [canvas[key] for key in ('circle', 'bar', 'logo', 'company', 'prompt')]
        objects += canvas.get('extra_images', []) + canvas.get('text_boxes', [])
        for spec in objects:
            for key, scale in (('x', sx), ('w', sx), ('y', sy), ('h', sy)):
                if key in spec:
                    value = int(round(spec[key] * scale))
                    spec[key] = max(1, value) if key in ('w', 'h') else value
            if spec.get('lock_aspect'):
                spec['w'] = spec['h'] = max(1, min(spec.get('w', 1), spec.get('h', 1)))
            if 'font_size' in spec:
                spec['font_size'] = max(8, int(round(spec['font_size'] * min(sx, sy))))
            if 'tick_size' in spec:
                spec['tick_size'] = max(1, int(round(spec['tick_size'] * min(sx, sy))))
    project['resolution'] = [int(width), int(height)]
    project.update(copy.deepcopy(project['canvases'][project['active_canvas']]['canvas']))


# 用有限if分支映射序号而不是依赖额外算术模块，保证现成签名GRUB的内置命令可以执行；选择与推进只在真正显示静图时发生。
def static_selection_script(project):
    paths = theme_paths(project)
    if len(paths) == 1:
        return ['set neo_theme=($neo_root)/' + paths[0]]
    lines = ['set neo_static_rotation=0',
             'if ! load_env -f $neo_env neo_static_rotation; then', '  neo_windows', 'fi',
             'set neo_static_next=1', 'set neo_theme=($neo_root)/' + paths[0]]
    for index, path in enumerate(paths[1:], 1):
        lines += ['if [ "$neo_static_rotation" = %d ]; then' % index,
                  '  set neo_static_next=%d' % ((index + 1) % len(paths)),
                  '  set neo_theme=($neo_root)/' + path, 'fi']
    if int(project.get('timeout', 3)) > 0:
        # Reached only in the drawable menu context, after neo_once consumption.
        # Esc still counts the static scene that was actually shown. Linux does
        # not write the state; its reboot never reaches this block a second time.
        lines += ['set neo_static_rotation=$neo_static_next',
                  'if ! save_env -f $neo_env neo_static_rotation; then', '  neo_windows', 'fi']
    return lines
