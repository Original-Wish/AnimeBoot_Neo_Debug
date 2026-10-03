# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
"""Installed font discovery and cmap-based checks for the exact Pillow face.

No Qt/system fallback is assumed: Pillow renders face 0, so the catalogue and
text coverage check must inspect that same face, including TTC collections.
"""

# 字体候选来自系统目录与当前用户目录。用fontTools读取Unicode cmap覆盖率，优先具备足够中文汉字的字体。
# 文件大小/修改时间参与缓存键，避免每次首次打开文本属性都重新遍历字体表。
# 字体枚举可后台预热；涉及Qt控件的更新只能在GUI线程进行。cmap通过不能保证每个生僻字都有字形。
# 文字最终由Windows预渲染进静图，启动时不需要把用户TTF整体复制进ESP。分享字体前需确认字体自己的许可。

import functools
import json
import os
import threading
from pathlib import Path

from fontTools.ttLib import TTFont


def installed_font_paths():
    windir = os.environ.get("WINDIR", r"C:\Windows")
    local = os.environ.get("LOCALAPPDATA", os.path.expanduser("~/AppData/Local"))
    roots = [os.path.join(windir, "Fonts"), os.path.join(local, "Microsoft", "Windows", "Fonts"),
             os.path.expanduser("~/.fonts"), os.path.expanduser("~/.local/share/fonts"),
             "/usr/share/fonts", "/usr/local/share/fonts", "/System/Library/Fonts", "/Library/Fonts"]
    candidates = []
    for root in roots:
        if os.path.isdir(root):
            for parent, _, names in os.walk(root):
                candidates.extend(os.path.join(parent, n) for n in names if n.lower().endswith((".ttf", ".ttc", ".otf", ".otc")))
    if os.name == "nt":
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts") as key:
                    for index in range(winreg.QueryInfoKey(key)[1]):
                        _, value, _ = winreg.EnumValue(key, index)
                        if not isinstance(value, str):
                            continue
                        value = os.path.expandvars(value)
                        if os.path.isabs(value):
                            candidates.append(value)
                        else:
                            candidates.extend(os.path.join(root, value) for root in roots[:2])
            except OSError:
                pass
    found = {os.path.normcase(os.path.abspath(p)): os.path.abspath(p)
             for p in candidates if os.path.isfile(p) and p.lower().endswith((".ttf", ".ttc", ".otf", ".otc"))}
    return sorted(found.values(), key=lambda p: (os.path.basename(p).casefold(), p.casefold()))


@functools.lru_cache(maxsize=256)
def _coverage(path, mtime_ns, size):
    with TTFont(path, fontNumber=0, lazy=True) as font:
        cmap = font.getBestCmap() or {}
        glyph_ids = font.getReverseGlyphMap()
        return frozenset(code for code, glyph in cmap.items()
                         if glyph not in (".notdef", ".null") and glyph_ids.get(glyph, 0) != 0)


def coverage(path):
    try:
        st = os.stat(path)
        return _coverage(os.path.abspath(path), st.st_mtime_ns, st.st_size)
    except Exception:
        # Broken/unreadable installed fonts are excluded, never accepted by name.
        return frozenset()


@functools.lru_cache(maxsize=1)
def common_chinese():
    chars = set()
    for first in range(0xB0, 0xD8):
        for second in range(0xA1, 0xFF):
            try:
                chars.add(ord(bytes((first, second)).decode("gb2312")))
            except UnicodeError:
                pass
    return frozenset(chars)


def is_chinese_font(path):
    chars = coverage(path)
    common = common_chinese()
    # Test common simplified Han coverage, not a localized family name or a
    # single sample glyph. Partial/subset and extension-only fonts are excluded.
    return len(chars & common) >= len(common) * 0.99


def font_catalog_cache_path():
    override = os.environ.get('ANIMEBOOT_NEO_CACHE_DIR')
    local = os.environ.get('LOCALAPPDATA', os.path.expanduser('~/.cache'))
    return Path(override or os.path.join(local, 'AnimeBootNeo')) / 'font-catalog-v1.json'


def scan_font_catalog(cache_path=None, cancel_check=None):
    """Scan face 0 off the GUI thread; reuse unchanged accepted/rejected faces.

    Cache entries are keyed by absolute path, nanosecond mtime and size. User
    fonts and registry installations use exactly the same coverage rules.
    Export still checks the actual text independently via supports_text().
    """
    from PIL import ImageFont
    cache_path = Path(cache_path) if cache_path is not None else font_catalog_cache_path()
    schema = 'face0-gb2312-level1-99-v1'
    previous = {}
    try:
        saved = json.loads(cache_path.read_text(encoding='utf-8'))
        if saved.get('schema') == schema and isinstance(saved.get('faces'), dict):
            previous = saved['faces']
    except (OSError, ValueError, AttributeError):
        pass
    faces, catalog = {}, []
    for path in installed_font_paths():
        if callable(cancel_check) and cancel_check():
            return None
        try:
            st = os.stat(path)
            key = os.path.normcase(os.path.abspath(path))
            stamp = [st.st_mtime_ns, st.st_size]
            rec = previous.get(key)
            if (not isinstance(rec, dict) or rec.get('stamp') != stamp
                    or not isinstance(rec.get('accepted'), bool)
                    or not isinstance(rec.get('family'), str)
                    or not isinstance(rec.get('style'), str)):
                rec = {'stamp': stamp, 'accepted': False, 'family': '', 'style': ''}
                if is_chinese_font(path):
                    family, style = ImageFont.truetype(path, 16).getname()
                    rec.update(accepted=True, family=str(family or '').strip(),
                               style=str(style or '').strip())
            faces[key] = rec
            if rec['accepted'] and rec['family']:
                catalog.append((rec['family'], rec['style'], path))
        except Exception:
            # An unreadable/broken face is excluded; it cannot freeze the UI
            # because this entire scan belongs to the background worker.
            continue
    if callable(cancel_check) and cancel_check():
        return None
    temporary = cache_path.with_name(cache_path.name + '.%s.%s.tmp' % (os.getpid(), threading.get_ident()))
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps({'schema': schema, 'faces': faces},
                                       ensure_ascii=False), encoding='utf-8')
        os.replace(str(temporary), str(cache_path))
    except OSError:
        # A read-only user profile only disables persistence, not font access.
        pass
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass
    return catalog


def supports_text(path, text):
    needed = {ord(c) for c in str(text) if not c.isspace()}
    chars = coverage(path)
    return bool(chars) and needed <= chars


def resolve_font(preferred="", text="中文字体"):
    if preferred and supports_text(preferred, text):
        return preferred
    windir = os.environ.get("WINDIR", r"C:\Windows")
    priority = [os.path.join(windir, "Fonts", name) for name in ("msyh.ttc", "simhei.ttf", "simsun.ttc")]
    for path in priority:
        if supports_text(path, text):
            return path
    for path in installed_font_paths():
        if supports_text(path, text):
            return path
    raise ValueError("没有找到能完整显示这段文字的已安装字体。请安装包含这些字形的字体或修改文字，避免导出方块。")
