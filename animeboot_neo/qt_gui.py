# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
# -*- coding: utf-8 -*-

# 界面协调层：收集编辑设置、管理一个可见画布、启动后台制作/容量估算/安装，并呈现结构化结果。
# Qt控件只在主线程操作。BuildWorker深复制工程；工作期间切换画布仅改变编辑器，结果提示及安装参数从构建快照读取。
# 轮换估算缓存只驻留内存，依据素材与制作设置签名复用；关闭或重新打开窗口不应无理由重复抽样。
# 构建和安装是两个阶段：构建产物完整但安装失败时保留可继续安装记录，避免重复编码；安装成功清除该记录。
# GUI显示的安装日志不同于开机播放器诊断页；调试开关只控制动图结束后的两页，普通Linux/GRUB启动保持安静。

import copy
import ctypes
import json
import io
import math
import os
import shutil
import sys
import threading
import time
import subprocess
import tempfile

from PIL import Image, ImageFont

from PySide2 import QtCore
from PySide2.QtCore import QEvent, QMimeData, QObject, QPointF, QRectF, QSize, Qt, QThread, QTimer, Signal
from PySide2.QtGui import QColor, QCursor, QDrag, QFont, QFontDatabase, QFontMetrics, QIcon, QImage, QKeySequence, QLinearGradient, QPainter, QPainterPath, QPalette, QPen, QPixmap, QPolygonF
from PySide2.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QButtonGroup,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGraphicsItem,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QLayout,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QScrollBar,
    QStyleOptionSlider,
    QShortcut,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedLayout,
    QStyle,
    QStyleOptionComboBox,
    QTabWidget,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .animation import AnimationError, estimate_animation_codec_sizes, probe_animation_source, render_first_frame_preview
from .canvas_geometry import clamp_move_delta, has_corner_in_canvas, resize_from_rotated_corner, resize_from_rotated_handle, rotated_corners
from .builder import (
    BuildError,
    build_payload,
)
from .constants import APP_EDITION, APP_VERSION, THEME_ROOT
from .deployment import (
    DeploymentError,
    probe_boot_partition_space,
    launch_elevated_action,
    prepare_elevated_action,
    read_action_result,
)
from .project import (
    COUNTDOWN_MODES,
    countdown_key,
    countdown_display_name,
    default_project,
    find_dynamic_item,
    load_project,
    new_object_id,
    normalize_layer_order,
    normalize_project,
    save_project,
    validate_project,
)
from .theme import installed_font_paths, render_preview_scaled, render_theme, render_canvases
from .theme_package import export_theme,import_theme
from .rotation import animation_sources,estimate_rotation_sizes,estimated_boot_bytes
from .scenes import MAX_CANVASES, capture_canvas, sync_canvas, select_canvas, resize_canvases, countdown_modes
from .canvas_manager import CanvasManagerBar
from .rotation_dialog import RotationDialog
from .fonts import scan_font_catalog
from .win_display import get_current_display_refresh_rate, get_current_display_resolution, get_display_resolutions
from .ui_effects import (
    THEME_AERO,
    THEME_DARK,
    THEME_LIQUID,
    ThemeBackdrop,
    ThemeProxyStyle,
    apply_native_titlebar,
    build_palette,
    configure_opengl_format,
    install_messagebox_taskbar_notifications,
)


# Apply one notification policy to every QMessageBox in the UI.  The helper
# only flashes the taskbar when another application is foreground.
QMessageBox = install_messagebox_taskbar_notifications(QMessageBox)


THEME_OPTIONS = (
    ("炫彩果冻界面", THEME_LIQUID),
    ("湛蓝风光界面", THEME_AERO),
    ("暗黑夜间界面", THEME_DARK),
)


def _application_icon_path():
    root = getattr(sys, "_MEIPASS", "")
    if not root:
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    return os.path.join(root, "assets", "app_icon.ico")


_ELEVATED_BUILD_STATE_ARG = "--elevated-build-state"


def _pending_registry_key():
    return "Software\\AnimeBoot Neo Debug\\" + APP_VERSION


def _read_pending_registry_path():
    """Read only this version's failed-install continuation pointer."""
    if os.name != "nt":
        return ""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _pending_registry_key(), 0, winreg.KEY_QUERY_VALUE) as key:
            value, kind = winreg.QueryValueEx(key, "pending_install_manifest")
        return str(value) if kind == winreg.REG_SZ else ""
    except OSError:
        return ""


def _write_pending_registry_path(path):
    if os.name != "nt":
        return
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _pending_registry_key(), 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "pending_install_manifest", 0, winreg.REG_SZ, str(path))


def _clear_pending_registry_path():
    """Remove the retry value and empty current-version keys, never old keys."""
    if os.name != "nt":
        return
    import winreg
    current = _pending_registry_key()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, current, 0, winreg.KEY_SET_VALUE) as key:
            try:
                winreg.DeleteValue(key, "pending_install_manifest")
            except FileNotFoundError:
                pass
    except FileNotFoundError:
        pass
    for path in (current, r"Software\AnimeBoot Neo Debug"):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ) as key:
                children, values, _ = winreg.QueryInfoKey(key)
            if children or values:
                break
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
        except FileNotFoundError:
            continue








def _is_process_elevated():
    """Return True when the current Windows GUI already has admin rights."""
    if os.name != "nt":
        return True
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _startup_elevated_build_state_path(argv):
    values = list(argv or [])
    for index, value in enumerate(values):
        value = str(value or "")
        if value == _ELEVATED_BUILD_STATE_ARG and index + 1 < len(values):
            path = str(values[index + 1] or "").strip().strip('"')
            return os.path.abspath(path) if path else ""
        prefix = _ELEVATED_BUILD_STATE_ARG + "="
        if value.startswith(prefix):
            path = value[len(prefix):].strip().strip('"')
            return os.path.abspath(path) if path else ""
    return ""


def _write_elevated_build_state(payload):
    fd, path = tempfile.mkstemp(prefix="AnimeBootNeo_build_resume_", suffix=".json")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
        return path
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        raise


def _read_elevated_build_state(path):
    if not path or not os.path.isfile(path):
        raise RuntimeError("管理员制作接续状态不存在或已失效。")
    try:
        with open(path, "r", encoding="utf-8") as stream:
            payload = json.load(stream)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    if not isinstance(payload, dict) or int(payload.get("version", 0) or 0) != 1:
        raise RuntimeError("管理员制作接续状态格式无效。")
    if str(payload.get("app_version", "") or "") != str(APP_VERSION):
        raise RuntimeError("管理员制作接续状态与当前程序版本不匹配。")
    if str(payload.get("app_edition", "") or "") != str(APP_EDITION):
        raise RuntimeError("管理员制作接续状态与当前程序版本线不匹配。")
    if not isinstance(payload.get("project"), dict):
        raise RuntimeError("管理员制作接续状态缺少工程数据。")
    return payload


def _launch_self_elevated_for_build(state_path):
    if os.name != "nt":
        raise RuntimeError("管理员权限接续仅支持 Windows。")
    if getattr(sys, "frozen", False):
        executable = sys.executable
        arguments = [_ELEVATED_BUILD_STATE_ARG, state_path]
        working_dir = os.path.dirname(os.path.abspath(executable))
    else:
        executable = sys.executable
        script = os.path.abspath(sys.argv[0])
        arguments = [script, _ELEVATED_BUILD_STATE_ARG, state_path]
        working_dir = os.path.dirname(script)
    params = subprocess.list2cmdline(arguments)
    shell_execute = ctypes.windll.shell32.ShellExecuteW
    shell_execute.restype = ctypes.c_void_p
    value = shell_execute(None, "runas", executable, params, working_dir, 1)
    numeric = int(value or 0)
    if numeric <= 32:
        raise RuntimeError("未获得管理员权限，制作没有开始（UAC 错误码：%s）。" % numeric)
    return True


COMMON_QSS = r"""
* { font-size: 9pt; }
QMainWindow, QDialog, QMessageBox, QFileDialog { border: 0; }
QWidget#Root, QWidget#ContentRoot { background: transparent; border: 0; }
QFrame#TopGlass, QFrame#Panel, QFrame#CenterPanel { border-radius: 11px; }
QGroupBox {
    margin-top: 15px;
    padding: 14px 9px 9px 9px;
    font-weight: 600;
    border-radius: 9px;
}
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 7px; }
QTabWidget { background: transparent; }
QTabWidget::pane { border-radius: 8px; }
QTabBar { background: transparent; }
QTabBar::tab { min-width: 44px; padding: 7px 8px; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextBrowser {
    min-height: 26px;
    padding: 3px 9px;
    border-radius: 5px;
}
QComboBox::drop-down { width: 27px; border: 0; background: transparent; }
QComboBox::down-arrow { width: 0px; height: 0px; }
QComboBox { padding-left: 9px; padding-right: 35px; }
QSpinBox, QDoubleSpinBox { padding-left: 9px; padding-right: 34px; }
QSpinBox::up-button, QDoubleSpinBox::up-button {
    subcontrol-origin: border; subcontrol-position: top right; width: 27px;
    border: 0; background: transparent;
}
QSpinBox::down-button, QDoubleSpinBox::down-button {
    subcontrol-origin: border; subcontrol-position: bottom right; width: 27px;
    border: 0; background: transparent;
}
QSpinBox::up-arrow, QSpinBox::down-arrow,
QDoubleSpinBox::up-arrow, QDoubleSpinBox::down-arrow { width: 0px; height: 0px; }
QComboBox QAbstractItemView {
    outline: 0;
    padding: 3px;
    border-radius: 5px;
}
QComboBox QAbstractItemView::item { min-height: 26px; padding: 3px 7px; }
QAbstractScrollArea { border: 0; background: transparent; }
QAbstractScrollArea::viewport { background: transparent; }
QAbstractScrollArea::corner { border: 0; }
QListWidget, QListView, QTreeView, QTableView {
    border-radius: 7px;
    padding: 4px;
    outline: 0;
}
QListWidget::item, QListView::item, QTreeView::item { min-height: 28px; border-radius: 5px; padding: 3px 6px; }
QCheckBox { spacing: 7px; padding-left: 3px; }
QProgressBar { border-radius: 5px; text-align: center; min-height: 17px; }
QProgressBar::chunk { border-radius: 4px; }
QStatusBar::item { border: 0; }
QMenu { padding: 5px; }
QMenu::item { min-height: 23px; padding: 4px 24px 4px 9px; border-radius: 4px; }
QToolTip { padding: 5px 7px; border-radius: 4px; }
QScrollBar:vertical { width: 14px; margin: 2px 1px 2px 1px; border-radius: 6px; }
QScrollBar:horizontal { height: 14px; margin: 1px 2px 1px 2px; border-radius: 6px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; border: 0; background: transparent; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; border: 0; }
"""

AERO_QSS = COMMON_QSS + r"""
QMainWindow, QDialog, QMessageBox, QFileDialog { background: #d7e7f3; color: #20384d; }
QFrame#TopGlass {
    border: 1px solid rgba(76,118,151,205);
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
        stop:0 rgba(234,248,255,218), stop:0.36 rgba(198,229,248,205),
        stop:0.70 rgba(143,195,226,202), stop:1 rgba(104,164,204,215));
}
QFrame#Panel, QFrame#CenterPanel {
    background: rgba(247,252,255,224);
    border: 1px solid rgba(104,144,176,210);
}
QGroupBox {
    color: #23445e;
    background: rgba(250,253,255,232);
    border: 1px solid #91adc4;
}
QGroupBox::title { background: rgba(238,248,254,245); color: #174f75; }
QTabWidget::pane { border: 1px solid #91adc4; background: rgba(247,252,255,228); }
QTabBar::tab {
    border: 1px solid #97b2c7;
    border-bottom: 0;
    color: #365c79;
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #f7fcff, stop:1 #c8deed);
}
QTabBar::tab:selected { color: #075d9f; background: #f8fcff; font-weight: 600; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextBrowser {
    color: #20384d;
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #ffffff, stop:1 #edf6fb);
    border: 1px solid #789ab4;
    selection-color: #ffffff;
    selection-background-color: #278ac6;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus, QTextBrowser:focus { border-color: #1887c5; }
QComboBox QAbstractItemView, QMenu {
    color: #20384d;
    background: #f7fcff;
    border: 1px solid #7398b5;
    selection-color: #173f5d;
    selection-background-color: #bde5fa;
}
QComboBox QAbstractItemView::item:selected, QMenu::item:selected { color: #173f5d; background: #bde5fa; }
QListWidget, QListView, QTreeView, QTableView { color: #20384d; background: rgba(255,255,255,238); border: 1px solid #91adc4; }
QListWidget::item:selected, QListView::item:selected, QTreeView::item:selected { color: #173f5d; background: #bde5fa; border: 1px solid #62a9d1; }
QProgressBar { color: #20384d; border: 1px solid #829fb6; background: #d7e5ef; }
QProgressBar::chunk { background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #1686c7,stop:1 #68c9ed); }
QSplitter::handle { background: rgba(102,143,175,150); }
QStatusBar { background: rgba(232,243,250,235); color: #29465e; border-top: 1px solid #9eb6c8; }
QLabel#MutedLabel { color: #56758f; }

QToolTip { color: #15344e; background: #eef8ff; border: 1px solid #6b9bbd; }
QScrollBar { background: rgba(214,231,242,190); border: 0; border-radius: 6px; }
QScrollBar::handle { background: #8fb2cb; border: 1px solid #6f93ad; border-radius: 6px; min-height: 24px; min-width: 24px; }
QScrollBar::handle:hover { background: #68a9cf; }
QAbstractScrollArea::corner { background: #d9e8f2; }
"""

LIQUID_QSS = COMMON_QSS + r"""
QMainWindow, QDialog, QMessageBox, QFileDialog { background: #dfe9f6; color: #17243a; }
QFrame#TopGlass {
    border: 1px solid rgba(102,132,198,92);
    background: rgba(235,245,255,78);
}
QFrame#Panel, QFrame#CenterPanel {
    background: rgba(236,245,255,58);
    border: 1px solid rgba(105,132,195,76);
}
QGroupBox {
    color: #243654;
    background: rgba(241,247,255,76);
    border: 1px solid rgba(110,137,199,82);
}
QGroupBox::title { color: #334a6c; background: rgba(229,240,255,176); }
QTabWidget::pane { border: 1px solid rgba(108,135,198,78); background: rgba(239,247,255,54); }
QTabBar::tab { border: 0; margin: 3px; color: #596b84; background: rgba(255,255,255,64); border-radius: 8px; }
QTabBar::tab:selected { color: #172a4b; background: rgba(255,255,255,155); font-weight: 600; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextBrowser {
    color: #17304f;
    background: rgba(225,238,255,224);
    border: 1px solid rgba(105,137,205,215);
    selection-color: #ffffff;
    selection-background-color: #657ff0;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus, QTextBrowser:focus { border-color: rgba(100,132,238,230); }
QComboBox QAbstractItemView, QMenu {
    color: #1d2e47;
    background: rgba(244,249,255,244);
    border: 1px solid rgba(134,158,205,215);
    selection-color: #ffffff;
    selection-background-color: #687ff0;
}
QComboBox QAbstractItemView::item:selected, QMenu::item:selected { color: #ffffff; background: #687ff0; }
QListWidget, QListView, QTreeView, QTableView { color: #20304a; background: rgba(255,255,255,92); border: 1px solid rgba(255,255,255,168); }
QListWidget::item:selected, QListView::item:selected, QTreeView::item:selected { color: #172747; background: rgba(210,227,255,190); border: 1px solid rgba(115,157,228,180); }
QProgressBar { color: #22334c; border: 1px solid rgba(255,255,255,174); background: rgba(214,226,245,115); }
QProgressBar::chunk { background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #6c91ff,stop:1 #a47df0); }
QSplitter::handle { background: rgba(255,255,255,94); }
QStatusBar { background: rgba(247,251,255,112); color: #34465f; border-top: 1px solid rgba(255,255,255,148); }
QLabel#MutedLabel { color: #5c6e88; }

QToolTip { color: #273750; background: rgba(250,252,255,246); border: 1px solid rgba(144,170,216,210); }
QScrollBar { background: rgba(184,204,242,150); border: 0; border-radius: 6px; }
QScrollBar::handle { background: #7894dc; border: 1px solid #5f78bc; border-radius: 6px; min-height: 24px; min-width: 24px; }
QScrollBar::handle:hover { background: #657fd5; }
QScrollBar::handle:pressed { background: #536bc4; }
QAbstractScrollArea::corner { background: #C8D8F3; }
"""

DARK_QSS = COMMON_QSS + r"""
QMainWindow, QDialog, QMessageBox, QFileDialog { background: #1f1f1f; color: #f0f0f0; }
QWidget#Root, QWidget#ContentRoot { background: transparent; color: #f0f0f0; }
QFrame#TopGlass { border: 1px solid #404040; background: #252526; }
QFrame#Panel, QFrame#CenterPanel { background: #202020; border: 1px solid #3d3d3d; }
QGroupBox { color: #e8e8e8; background: #242424; border: 1px solid #424242; }
QGroupBox::title { color: #e8e8e8; background: #242424; }
QTabWidget::pane { border: 1px solid #414141; background: #202020; }
QTabBar::tab { border: 1px solid #424242; border-bottom: 0; color: #bdbdbd; background: #2b2b2b; }
QTabBar::tab:selected { color: #ffffff; background: #202020; font-weight: 600; }
QScrollArea, QAbstractScrollArea, QAbstractScrollArea::viewport { background: #202020; color: #f0f0f0; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextBrowser {
    color: #f2f2f2;
    background: #181818;
    border: 1px solid #565656;
    selection-color: #ffffff;
    selection-background-color: #0078d4;
}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled { color: #858585; background: #202020; border-color: #3c3c3c; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus, QTextBrowser:focus { border-color: #168bd2; }
QComboBox QAbstractItemView, QMenu {
    color: #f2f2f2;
    background: #252525;
    border: 1px solid #505050;
    selection-color: #ffffff;
    selection-background-color: #0078d4;
}
QComboBox QAbstractItemView::item { color: #f2f2f2; background: #252525; }
QComboBox QAbstractItemView::item:selected, QMenu::item:selected { color: #ffffff; background: #0078d4; }
QListWidget, QListView, QTreeView, QTableView { color: #ededed; background: #181818; alternate-background-color: #202020; border: 1px solid #414141; }
QListWidget::item:selected, QListView::item:selected, QTreeView::item:selected { color: #ffffff; background: #075985; border: 1px solid #168bd2; }
QProgressBar { color: #f0f0f0; border: 1px solid #4c4c4c; background: #242424; }
QProgressBar::chunk { background: #0078d4; }
QSplitter::handle { background: #363636; }
QStatusBar { background: #1d1d1d; color: #dedede; border-top: 1px solid #383838; }
QLabel { color: #f0f0f0; background: transparent; }
QLabel#MutedLabel { color: #a7a7a7; }

QToolTip { color: #f2f2f2; background: #2c2c2c; border: 1px solid #5b5b5b; }
QScrollBar { background: #202020; border: 0; border-radius: 6px; }
QScrollBar::handle { background: #555555; border: 1px solid #626262; border-radius: 6px; min-height: 24px; min-width: 24px; }
QScrollBar::handle:hover { background: #686868; }
QAbstractScrollArea::corner { background: #202020; }
"""


# Final coverage rules.  QScrollArea pages and QTabWidget pages are ordinary
# QWidget instances, so theme them explicitly instead of relying on palette
# inheritance (which is where the white islands in dark mode came from).
AERO_QSS += r"""
QScrollArea > QWidget > QWidget, QTabWidget QStackedWidget, QTabWidget QStackedWidget > QWidget { background: transparent; }
QStackedWidget, QSplitter, QSplitter > QWidget { background: transparent; }
QComboBox { color: #20384d; background: #f7fcff; }
QComboBox:disabled { color: #8394a3; background: #dfe8ee; }
"""

LIQUID_QSS += r"""
QScrollArea > QWidget > QWidget, QTabWidget QStackedWidget, QTabWidget QStackedWidget > QWidget { background: transparent; }
QStackedWidget, QSplitter, QSplitter > QWidget { background: transparent; }
QComboBox { color: #17304f; background: rgba(225,238,255,232); border-color: rgba(105,137,205,220); }
QComboBox:disabled { color: #7d8ba1; background: rgba(210,222,241,190); }
"""

DARK_QSS += r"""
QWidget { color: #f0f0f0; }
QWidget#Root, QWidget#ContentRoot { background: transparent; }
QScrollArea, QAbstractScrollArea, QScrollArea > QWidget, QScrollArea > QWidget > QWidget,
QAbstractScrollArea::viewport, QTabWidget QStackedWidget, QTabWidget QStackedWidget > QWidget,
QStackedWidget, QSplitter, QSplitter > QWidget { background: #202020; color: #f0f0f0; }
QFrame#TopGlass, QFrame#Panel, QFrame#CenterPanel { color: #f0f0f0; }
QComboBox { color: #f2f2f2; background: #181818; }
QComboBox:disabled { color: #858585; background: #202020; }
QComboBox:on { color: #ffffff; background: #252525; }
QComboBox QLineEdit { color: #f2f2f2; background: transparent; }
"""

THEME_STYLES = {
    THEME_AERO: AERO_QSS,
    THEME_LIQUID: LIQUID_QSS,
    THEME_DARK: DARK_QSS,
}



class BuildLogEdit(QPlainTextEdit):
    """Follow new log entries while leaving a held scrollbar under user control."""

    def append_log(self, text):
        bar = self.verticalScrollBar()
        held = bar.isSliderDown()
        position = bar.value()
        self.appendPlainText(text)
        # QPlainTextEdit can itself follow an append when the thumb started at
        # the bottom. Restore that position as well as skipping our own follow.
        bar.setValue(position if held else bar.maximum())


class RoundedScrollBarPaintFilter(QObject):
    """Paint scrollbar tracks/thumbs ourselves so rounded end-caps stay round.

    Qt 5's stylesheet scrollbar implementation can flatten the slider cap when
    the thumb reaches the first/last position, especially when a styled
    QAbstractItemView or combo popup supplies its own page subcontrols.  This
    filter suppresses only the scrollbar paint event and uses Qt's own slider
    geometry for hit-testing/position, then draws a 6 px rounded track and
    handle.  Scroll behavior, wheel handling and drag hitboxes stay native.
    """

    def eventFilter(self, watched, event):
        if isinstance(watched, QScrollBar) and event.type() == QEvent.Paint:
            self._paint_scrollbar(watched)
            return True
        return super().eventFilter(watched, event)

    @staticmethod
    def _theme_colors(scrollbar):
        theme_id = QApplication.instance().property("uiTheme") if QApplication.instance() else THEME_AERO
        if theme_id == THEME_DARK:
            return QColor("#202020"), QColor("#555555"), QColor("#686868"), QColor("#626262")
        if theme_id == THEME_LIQUID:
            return QColor("#C8D8F3"), QColor("#7894DC"), QColor("#657FD5"), QColor("#5F78BC")
        return QColor("#D6E7F2"), QColor("#8FB2CB"), QColor("#68A9CF"), QColor("#6F93AD")

    @staticmethod
    def _surround_color(scrollbar):
        """Return an opaque colour for the four pixels outside rounded caps.

        Qt 5's raster backing store does not preserve real alpha for an ordinary
        QScrollBar widget.  Clearing those pixels to transparent therefore turns
        them black on some light themes.  Paint them with the surrounding widget
        background instead; this keeps the end caps visually round without the
        black corner artefact introduced when the OpenGL backdrop was removed.
        """
        app = QApplication.instance()
        theme_id = app.property("uiTheme") if app else THEME_AERO
        fallback = {
            THEME_DARK: QColor("#202020"),
            THEME_LIQUID: QColor("#DFE9F6"),
            THEME_AERO: QColor("#D7E7F3"),
        }.get(theme_id, QColor("#D7E7F3"))
        parent = scrollbar.parentWidget()
        if parent is None:
            return fallback
        try:
            palette = parent.palette()
            # Base best matches item views/combo popups; Window is a safer
            # fallback for scroll-area containers.  Ignore invalid/transparent
            # colours because an opaque raster scrollbar cannot reveal them.
            for role in (QPalette.Base, QPalette.Window):
                color = palette.color(role)
                if color.isValid() and color.alpha() >= 240:
                    return color
        except Exception:
            pass
        return fallback

    def _paint_scrollbar(self, scrollbar):
        painter = QPainter(scrollbar)
        painter.setRenderHint(QPainter.Antialiasing, True)
        track, handle, hover, border = self._theme_colors(scrollbar)

        # Do not clear to transparent here.  On Qt 5's CPU/raster backing store
        # transparent scrollbar pixels can become opaque black.  Fill the small
        # area outside the rounded track with the surrounding themed background.
        painter.setCompositionMode(QPainter.CompositionMode_Source)
        painter.fillRect(scrollbar.rect(), self._surround_color(scrollbar))
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)

        track_rect = QRectF(scrollbar.rect()).adjusted(0.75, 0.75, -0.75, -0.75)
        painter.setPen(QPen(border, 1.0))
        painter.setBrush(track)
        painter.drawRoundedRect(track_rect, 6.0, 6.0)

        slider_rect = None
        try:
            option = QStyleOptionSlider()
            scrollbar.initStyleOption(option)
            slider_rect = scrollbar.style().subControlRect(
                QStyle.CC_ScrollBar, option, QStyle.SC_ScrollBarSlider, scrollbar
            )
        except Exception:
            slider_rect = None

        if slider_rect is None or slider_rect.isEmpty():
            # Conservative fallback for unusual platform styles.
            minimum = scrollbar.minimum(); maximum = scrollbar.maximum()
            span = max(1, maximum - minimum)
            value_ratio = (scrollbar.value() - minimum) / float(span)
            if scrollbar.orientation() == Qt.Vertical:
                length = max(1.0, track_rect.height())
                handle_len = max(24.0, length * scrollbar.pageStep() / float(span + max(1, scrollbar.pageStep())))
                handle_len = min(length, handle_len)
                y = track_rect.top() + (length - handle_len) * value_ratio
                slider_rect = QtCore.QRect(int(track_rect.left()), int(y), int(track_rect.width()), int(handle_len))
            else:
                length = max(1.0, track_rect.width())
                handle_len = max(24.0, length * scrollbar.pageStep() / float(span + max(1, scrollbar.pageStep())))
                handle_len = min(length, handle_len)
                x = track_rect.left() + (length - handle_len) * value_ratio
                slider_rect = QtCore.QRect(int(x), int(track_rect.top()), int(handle_len), int(track_rect.height()))

        thumb = QRectF(slider_rect).adjusted(1.1, 1.1, -1.1, -1.1)
        if thumb.width() > 1.0 and thumb.height() > 1.0:
            painter.setPen(QPen(border, 1.0))
            painter.setBrush(hover if scrollbar.underMouse() else handle)
            painter.drawRoundedRect(thumb, 6.0, 6.0)
        painter.end()


def apply_application_theme(window, theme_id):
    theme_id = theme_id if theme_id in THEME_STYLES else THEME_AERO
    app = QApplication.instance()
    palette = build_palette(theme_id)
    if app:
        app.setProperty("uiTheme", theme_id)
        app.setPalette(palette)
        app.setStyleSheet(THEME_STYLES[theme_id])
    window.setProperty("uiTheme", theme_id)
    if hasattr(window, "backdrop"):
        window.backdrop.set_theme(theme_id)
    if hasattr(window, "canvas"):
        window.canvas.set_theme(theme_id)

    if theme_id == THEME_DARK:
        text_color, base_color, border_color = "#F2F2F2", "#181818", "#565656"
        selected_text, selected_bg = "#FFFFFF", "#0078D4"
        disabled_text, disabled_bg = "#858585", "#202020"
        scroll_track, scroll_handle, scroll_hover, scroll_border = "#202020", "#555555", "#686868", "#626262"
    elif theme_id == THEME_LIQUID:
        text_color, base_color, border_color = "#17304F", "#E1EEFF", "#6989CD"
        selected_text, selected_bg = "#FFFFFF", "#6079E8"
        disabled_text, disabled_bg = "#7D8BA1", "#D2DEF1"
        scroll_track, scroll_handle, scroll_hover, scroll_border = "#C8D8F3", "#7894DC", "#657FD5", "#5F78BC"
    else:
        text_color, base_color, border_color = "#20384D", "#F7FCFF", "#7398B5"
        selected_text, selected_bg = "#173F5D", "#BDE5FA"
        disabled_text, disabled_bg = "#8394A3", "#DFE8EE"
        scroll_track, scroll_handle, scroll_hover, scroll_border = "#D6E7F2", "#8FB2CB", "#68A9CF", "#6F93AD"

    combo_style = (
        "QComboBox{color:%s;background:%s;border:1px solid %s;border-radius:5px;"
        "min-height:26px;padding:3px 28px 3px 9px;}"
        "QComboBox:disabled{color:%s;background:%s;}"
        "QComboBox::drop-down{width:24px;border:0;}"
        "QComboBox QAbstractItemView{color:%s;background:%s;border:1px solid %s;"
        "selection-color:%s;selection-background-color:%s;outline:0;padding:3px;}"
        "QComboBox QAbstractItemView::item{color:%s;background:%s;min-height:26px;padding:3px 7px;}"
        "QComboBox QAbstractItemView::item:selected{color:%s;background:%s;}"
        "QScrollBar:vertical{width:14px;margin:2px 1px 2px 1px;background:%s;border:0;border-radius:6px;}"
        "QScrollBar:horizontal{height:14px;margin:1px 2px 1px 2px;background:%s;border:0;border-radius:6px;}"
        "QScrollBar::handle:vertical,QScrollBar::handle:horizontal{background:%s;border:1px solid %s;border-radius:6px;min-height:24px;min-width:24px;}"
        "QScrollBar::handle:hover{background:%s;}"
        "QScrollBar::add-line,QScrollBar::sub-line{width:0;height:0;border:0;}"
        "QScrollBar::add-page,QScrollBar::sub-page{background:transparent;border:0;}"
    ) % (
        text_color, base_color, border_color,
        disabled_text, disabled_bg,
        text_color, base_color, border_color,
        selected_text, selected_bg,
        text_color, base_color,
        selected_text, selected_bg,
        scroll_track,
        scroll_track,
        scroll_handle, scroll_border,
        scroll_hover,
    )

    # QComboBox popups are separate native top-level views on Windows.  Give
    # the combo, its line editor, the popup view, viewport and popup window the
    # same palette and direct style so current text never becomes transparent
    # or inherits a stale color after a theme switch.
    for combo in window.findChildren(QComboBox):
        combo_palette = QPalette(palette)
        for role in (QPalette.Text, QPalette.ButtonText, QPalette.WindowText):
            combo_palette.setColor(role, QColor(text_color))
        combo_palette.setColor(QPalette.Base, QColor(base_color) if not base_color.startswith("rgba") else palette.color(QPalette.Base))
        combo_palette.setColor(QPalette.Highlight, QColor(selected_bg))
        combo_palette.setColor(QPalette.HighlightedText, QColor(selected_text))
        combo.setPalette(combo_palette)
        combo.setStyleSheet(combo_style)
        line_edit = combo.lineEdit()
        if line_edit is not None:
            line_edit.setPalette(combo_palette)
            line_edit.setStyleSheet("color:%s;background:transparent;border:0;" % text_color)
        view = combo.view()
        if view is not None:
            view.setPalette(combo_palette)
            view.setStyleSheet(combo_style)
            view.viewport().setPalette(combo_palette)
            view.viewport().setStyleSheet("color:%s;background:%s;" % (text_color, base_color))
            try:
                view.verticalScrollBar().setStyleSheet(combo_style)
                view.horizontalScrollBar().setStyleSheet(combo_style)
            except Exception:
                pass
            try:
                view.window().setPalette(combo_palette)
                view.window().setStyleSheet(combo_style)
            except Exception:
                pass

    # Re-polish all existing controls so tab pages, scroll viewports, dialogs,
    # buttons and combo boxes cannot retain brushes from the previous theme.
    for widget in [window] + window.findChildren(QWidget):
        try:
            widget.style().unpolish(widget)
            widget.style().polish(widget)
            widget.update()
        except Exception:
            pass
    QTimer.singleShot(0, lambda: apply_native_titlebar(window, theme_id))
    window.update()


def _icon(widget: QWidget, standard) -> QIcon:
    """Return stable vector-like icons independent of the active Qt theme.

    Windows 10 can return empty or low-contrast standard icons after a proxy
    style and stylesheet are combined.  These compact application icons are
    painted once and remain visible in all three themes.
    """
    size = 20
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)

    def pen(color, width=1.4):
        painter.setPen(QPen(QColor(color), width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))

    def no_pen():
        painter.setPen(Qt.NoPen)

    if standard == "logo":
        pen("#356A96", 1.2)
        painter.setBrush(QColor("#EAF6FF"))
        painter.drawRoundedRect(QRectF(2.5, 3.0, 15.0, 13.5), 2.0, 2.0)
        no_pen(); painter.setBrush(QColor("#62B8E8")); painter.drawEllipse(QRectF(5.0, 5.0, 3.0, 3.0))
        pen("#3C8DBC", 1.2); painter.drawPolyline(QPolygonF([QPointF(4.0, 14.0), QPointF(8.0, 10.0), QPointF(10.7, 12.5), QPointF(14.0, 8.5), QPointF(17.0, 13.0)]))
        pen("#6C5CE7", 1.5); painter.drawArc(QRectF(11.0, 1.5, 7.0, 7.0), 30 * 16, 230 * 16)
        painter.drawLine(QPointF(16.8, 2.3), QPointF(17.8, 5.0)); painter.drawLine(QPointF(16.8, 2.3), QPointF(14.2, 2.8))
    elif standard == "share":
        # A curved forward arrow, drawn in the same icon system as the other
        # title-bar actions so it remains visible in every application theme.
        path=QPainterPath();path.moveTo(2.6,16.7)
        path.cubicTo(2.9,8.2,6.5,6.2,12.0,6.2)
        path.lineTo(12.0,2.8);path.lineTo(18.0,8.7);path.lineTo(12.0,14.3)
        path.lineTo(12.0,10.9);path.cubicTo(7.8,10.9,5.0,12.5,2.6,16.7);path.closeSubpath()
        gradient=QLinearGradient(QPointF(2,3),QPointF(18,17))
        gradient.setColorAt(0,QColor('#72C4EF'));gradient.setColorAt(1,QColor('#397DB9'))
        pen('#315F84',1.1);painter.setBrush(gradient);painter.drawPath(path)
    elif standard == "company":
        pen("#365F7D", 1.2); painter.setBrush(QColor("#DDF2FC")); painter.drawRoundedRect(QRectF(3.0, 5.0, 14.0, 12.5), 1.7, 1.7)
        painter.setBrush(QColor("#7EC4E8")); painter.drawRoundedRect(QRectF(6.5, 2.0, 7.0, 5.0), 1.4, 1.4)
        pen("#3F7FA5", 1.0)
        for y in (9.0, 13.0): painter.drawLine(QPointF(6.0, y), QPointF(14.0, y))
    elif standard == "image":
        pen("#3B6F96", 1.2); painter.setBrush(QColor("#EAF7FF")); painter.drawRoundedRect(QRectF(2.5, 3.0, 15.0, 14.0), 2.0, 2.0)
        no_pen(); painter.setBrush(QColor("#F7C85D")); painter.drawEllipse(QRectF(5.0, 5.0, 3.2, 3.2))
        pen("#2E8C70", 1.3); painter.setBrush(QColor("#74C69D")); painter.drawPolygon(QPolygonF([QPointF(3.8, 15.5), QPointF(8.2, 10.0), QPointF(10.5, 12.6), QPointF(13.5, 8.6), QPointF(17.0, 15.5)]))
    elif standard == "text":
        pen("#315D83", 1.8); painter.drawLine(QPointF(4.0, 4.5), QPointF(16.0, 4.5)); painter.drawLine(QPointF(10.0, 4.5), QPointF(10.0, 16.0)); painter.drawLine(QPointF(6.5, 16.0), QPointF(13.5, 16.0))
    elif standard == "top":
        pen("#37749D", 1.4); painter.drawLine(QPointF(3.0, 3.5), QPointF(17.0, 3.5))
        painter.setBrush(QColor("#6CC5EB")); painter.drawRoundedRect(QRectF(4.0, 12.5, 12.0, 4.0), 1.0, 1.0)
        painter.setBrush(QColor("#B6E4F7")); painter.drawRoundedRect(QRectF(5.5, 9.0, 9.0, 3.5), 0.8, 0.8)
        pen("#176FA3", 1.7); painter.drawLine(QPointF(10.0, 13.0), QPointF(10.0, 5.5)); painter.drawLine(QPointF(6.8, 8.4), QPointF(10.0, 5.2)); painter.drawLine(QPointF(13.2, 8.4), QPointF(10.0, 5.2))
    elif standard in ("zoom_in", "zoom_out"):
        pen("#315F84", 1.5); painter.setBrush(QColor(235, 247, 255, 150)); painter.drawEllipse(QRectF(2.5, 2.5, 11.5, 11.5)); painter.drawLine(QPointF(12.2, 12.2), QPointF(17.0, 17.0))
        painter.drawLine(QPointF(5.5, 8.2), QPointF(11.0, 8.2))
        if standard == "zoom_in": painter.drawLine(QPointF(8.25, 5.5), QPointF(8.25, 11.0))
    elif standard == "grid":
        pen("#315F84", 1.25)
        painter.setBrush(QColor(235, 247, 255, 118))
        painter.drawRoundedRect(QRectF(2.5, 2.5, 15.0, 15.0), 2.0, 2.0)
        painter.drawLine(QPointF(10.0, 3.8), QPointF(10.0, 16.2))
        painter.drawLine(QPointF(3.8, 10.0), QPointF(16.2, 10.0))
    elif standard == QStyle.SP_DialogOpenButton:
        pen("#8A5A12", 1.1)
        painter.setBrush(QColor("#F3B53F"))
        painter.drawRoundedRect(QRectF(2.2, 6.0, 15.5, 10.5), 2.0, 2.0)
        painter.setBrush(QColor("#FFD66E"))
        painter.drawRoundedRect(QRectF(3.0, 4.0, 7.8, 5.0), 1.6, 1.6)
        no_pen(); painter.setBrush(QColor(255, 247, 190, 120)); painter.drawRoundedRect(QRectF(3.5, 7.0, 13.0, 3.4), 1.3, 1.3)
    elif standard == QStyle.SP_DialogSaveButton:
        pen("#24517A", 1.1); painter.setBrush(QColor("#4E88B8")); painter.drawRoundedRect(QRectF(3.0, 2.5, 14.0, 15.0), 1.8, 1.8)
        no_pen(); painter.setBrush(QColor("#EAF4FA")); painter.drawRect(QRectF(6.0, 3.4, 7.8, 5.0)); painter.setBrush(QColor("#BFD9E8")); painter.drawRoundedRect(QRectF(6.0, 11.0, 8.0, 5.0), 1.0, 1.0)
        painter.setBrush(QColor("#224969")); painter.drawRect(QRectF(11.2, 4.0, 1.5, 3.7))
    elif standard == QStyle.SP_FileIcon:
        pen("#58718A", 1.2); painter.setBrush(QColor("#F8FBFD")); painter.drawRoundedRect(QRectF(4.0, 2.0, 11.5, 16.0), 1.5, 1.5)
        painter.setBrush(QColor("#D9EAF5")); painter.drawPolygon(QPolygonF([QPointF(11.0, 2.0), QPointF(15.5, 6.5), QPointF(11.0, 6.5)]))
        pen("#4D8BC1", 1.2); painter.drawLine(QPointF(6.3, 10.0), QPointF(13.2, 10.0)); painter.drawLine(QPointF(6.3, 13.0), QPointF(12.0, 13.0))
    elif standard == QStyle.SP_DialogApplyButton:
        pen("#FFFFFF", 2.2); painter.setBrush(QColor("#34A853")); painter.drawEllipse(QRectF(2.0, 2.0, 16.0, 16.0)); painter.drawLine(QPointF(5.8, 10.2), QPointF(8.7, 13.0)); painter.drawLine(QPointF(8.7, 13.0), QPointF(14.5, 6.7))
    elif standard == QStyle.SP_MessageBoxInformation:
        no_pen(); painter.setBrush(QColor("#2687D8")); painter.drawEllipse(QRectF(2.0, 2.0, 16.0, 16.0)); pen("#FFFFFF", 2.0); painter.drawLine(QPointF(10.0, 8.5), QPointF(10.0, 14.0)); painter.drawPoint(QPointF(10.0, 5.6))
    elif standard in (QStyle.SP_FileDialogDetailedView, QStyle.SP_FileDialogListView):
        pen("#3C6D99", 1.3)
        if standard == QStyle.SP_FileDialogDetailedView:
            for y in (5.0, 10.0, 15.0):
                painter.setBrush(QColor("#7FB6D8")); painter.drawRoundedRect(QRectF(2.5, y-1.4, 2.8, 2.8), 0.7, 0.7); painter.drawLine(QPointF(7.0, y), QPointF(17.0, y))
        else:
            painter.setBrush(QColor("#BFDDED"))
            for y in (3.0, 10.5):
                for x in (3.0, 10.5): painter.drawRoundedRect(QRectF(x, y, 6.3, 6.3), 1.0, 1.0)
    elif standard == QStyle.SP_ComputerIcon:
        pen("#315C7B", 1.2); painter.setBrush(QColor("#70B6DE")); painter.drawRoundedRect(QRectF(2.5, 3.0, 15.0, 10.5), 1.6, 1.6); painter.setBrush(QColor("#DFF6FF")); painter.drawRect(QRectF(4.1, 4.6, 11.8, 7.2)); painter.drawLine(QPointF(10.0, 13.5), QPointF(10.0, 16.3)); painter.drawLine(QPointF(6.5, 17.0), QPointF(13.5, 17.0))
    elif standard == QStyle.SP_MediaPlay:
        pen("#27733C", 1.0); painter.setBrush(QColor("#58BE67")); painter.drawEllipse(QRectF(2.0, 2.0, 16.0, 16.0)); no_pen(); painter.setBrush(QColor("#FFFFFF")); painter.drawPolygon(QPolygonF([QPointF(8.0, 6.0), QPointF(14.5, 10.0), QPointF(8.0, 14.0)]))
    elif standard in (QStyle.SP_ArrowUp, QStyle.SP_ArrowDown, QStyle.SP_ArrowForward):
        pen("#FFFFFF", 1.8); painter.setBrush(QColor("#46A94E")); painter.drawEllipse(QRectF(2.0, 2.0, 16.0, 16.0))
        if standard == QStyle.SP_ArrowUp:
            painter.drawLine(QPointF(10.0, 14.0), QPointF(10.0, 6.0)); painter.drawLine(QPointF(6.8, 9.0), QPointF(10.0, 5.8)); painter.drawLine(QPointF(13.2, 9.0), QPointF(10.0, 5.8))
        elif standard == QStyle.SP_ArrowDown:
            painter.drawLine(QPointF(10.0, 6.0), QPointF(10.0, 14.0)); painter.drawLine(QPointF(6.8, 11.0), QPointF(10.0, 14.2)); painter.drawLine(QPointF(13.2, 11.0), QPointF(10.0, 14.2))
        else:
            painter.drawLine(QPointF(6.0, 10.0), QPointF(14.0, 10.0)); painter.drawLine(QPointF(11.0, 6.8), QPointF(14.2, 10.0)); painter.drawLine(QPointF(11.0, 13.2), QPointF(14.2, 10.0))
    elif standard == QStyle.SP_TitleBarMaxButton:
        pen("#4E7291", 1.3); painter.setBrush(QColor(225, 240, 249, 220)); painter.drawRect(QRectF(4.0, 4.0, 12.0, 12.0)); painter.drawRect(QRectF(6.5, 6.5, 7.0, 7.0))
    elif standard == QStyle.SP_FileDialogNewFolder:
        pen("#52748E", 1.2); painter.setBrush(QColor("#ECF5FA")); painter.drawRoundedRect(QRectF(5.0, 3.0, 11.0, 12.0), 1.3, 1.3); painter.setBrush(QColor("#BBD7E8")); painter.drawRoundedRect(QRectF(2.5, 6.0, 11.0, 12.0), 1.3, 1.3)
    elif standard == QStyle.SP_TrashIcon:
        pen("#566674", 1.2); painter.setBrush(QColor("#C7D4DC")); painter.drawRoundedRect(QRectF(5.0, 6.0, 10.0, 11.0), 1.3, 1.3); painter.drawLine(QPointF(3.8, 5.2), QPointF(16.2, 5.2)); painter.drawLine(QPointF(7.6, 2.8), QPointF(12.4, 2.8)); painter.drawLine(QPointF(8.0, 8.0), QPointF(8.0, 14.5)); painter.drawLine(QPointF(12.0, 8.0), QPointF(12.0, 14.5))
    elif standard == QStyle.SP_FileDialogInfoView:
        pen("#526B84", 1.4); painter.setBrush(QColor("#EEF5FA")); painter.drawRoundedRect(QRectF(3.0, 3.0, 14.0, 14.0), 2.0, 2.0); pen("#347FC1", 1.7); painter.drawLine(QPointF(6.0, 7.0), QPointF(14.0, 7.0)); painter.drawLine(QPointF(10.0, 7.0), QPointF(10.0, 14.0)); painter.drawLine(QPointF(7.5, 14.0), QPointF(12.5, 14.0))
    else:
        painter.end()
        if isinstance(standard, str):
            return QIcon()
        fallback = widget.style().standardIcon(standard)
        return fallback

    painter.end()
    return QIcon(pixmap)


def _file_map(root):
    result = {}
    for node in root.walk():
        if not node.is_dir:
            result[node.get_path()] = node.data
    return result


def _pil_to_pixmap(image: Image.Image) -> QPixmap:
    rgba = image.convert("RGBA")
    raw = rgba.tobytes("raw", "RGBA")
    qimage = QImage(raw, rgba.width, rgba.height, QImage.Format_RGBA8888).copy()
    return QPixmap.fromImage(qimage)


def _spec_for_key(project, key):
    if key in ("logo", "company", "prompt", "circle", "bar"):
        return project.get(key)
    return find_dynamic_item(project, key)


def _object_name(project: dict, key: str) -> str:
    if key in ("circle", "bar"):
        mode = project.get("countdown_mode", "none")
        label = countdown_display_name(mode) if countdown_key(mode) == key else countdown_display_name(key)
        return label + "（始终强制置顶）"
    if key == "logo":
        return "主 Logo"
    if key == "company":
        return "左上角小logo"
    if key == "prompt":
        return "底部跳过提示"
    spec = find_dynamic_item(project, key) or {}
    if key.startswith("text:"):
        text = str(spec.get("text", "文字")).replace("\n", " ").strip()
        return "文字：%s" % (text[:18] or "未命名")
    display_name = str(spec.get("display_name", "")).strip()
    return "图片：%s" % (display_name or os.path.basename(spec.get("path", "")) or "未命名")


class _ThemedIndicatorMixin(object):
    _CONTROL_RADIUS = 5.0

    def _indicator_theme(self):
        app = QApplication.instance()
        value = str(app.property("uiTheme") if app is not None else THEME_LIQUID)
        return value if value in THEME_STYLES else THEME_LIQUID

    def _indicator_colors(self, active=False, pressed=False):
        theme = self._indicator_theme()
        enabled = self.isEnabled()
        if theme == THEME_DARK:
            if not enabled:
                return QColor("#252525"), QColor("#3C3C3C"), QColor("#777777"), QColor("#494949")
            if pressed:
                return QColor("#104B70"), QColor("#0B6CA8"), QColor("#FFFFFF"), QColor("#168BD2")
            if active:
                return QColor("#373737"), QColor("#2D2D2D"), QColor("#FFFFFF"), QColor("#168BD2")
            return QColor("#303030"), QColor("#282828"), QColor("#E2E2E2"), QColor("#505050")
        if theme == THEME_AERO:
            if not enabled:
                return QColor(226, 237, 244, 205), QColor(176, 198, 212, 180), QColor("#748D9E"), QColor(113, 148, 171, 150)
            if pressed:
                return QColor(167, 219, 243, 240), QColor(75, 157, 199, 235), QColor("#153F59"), QColor(29, 121, 169, 220)
            if active:
                return QColor(231, 250, 255, 245), QColor(105, 193, 230, 235), QColor("#174A69"), QColor(35, 145, 194, 215)
            return QColor(244, 252, 255, 236), QColor(157, 207, 230, 215), QColor("#31566F"), QColor(92, 135, 162, 185)
        if not enabled:
            return QColor(218, 228, 244, 210), QColor(191, 205, 231, 195), QColor("#718198"), QColor(137, 155, 190, 185)
        if pressed:
            return QColor(176, 215, 255, 248), QColor(121, 155, 235, 242), QColor("#17385F"), QColor(82, 112, 193, 235)
        if active:
            return QColor(231, 247, 255, 252), QColor(155, 202, 255, 244), QColor("#153B68"), QColor(88, 130, 213, 230)
        return QColor(216, 237, 255, 246), QColor(169, 205, 251, 235), QColor("#1D426D"), QColor(102, 135, 196, 220)

    @staticmethod
    def _draw_indicator_chevron(painter, center, down, color):
        painter.setPen(QPen(color, 1.55, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        if down:
            painter.drawLine(QPointF(center.x() - 3.4, center.y() - 1.5), QPointF(center.x(), center.y() + 1.9))
            painter.drawLine(QPointF(center.x(), center.y() + 1.9), QPointF(center.x() + 3.4, center.y() - 1.5))
        else:
            painter.drawLine(QPointF(center.x() - 3.4, center.y() + 1.5), QPointF(center.x(), center.y() - 1.9))
            painter.drawLine(QPointF(center.x(), center.y() - 1.9), QPointF(center.x() + 3.4, center.y() + 1.5))

    def _outer_control_path(self):
        outer = QRectF(0.5, 0.5, max(1.0, self.width() - 1.0), max(1.0, self.height() - 1.0))
        path = QPainterPath()
        path.addRoundedRect(outer, self._CONTROL_RADIUS, self._CONTROL_RADIUS)
        return outer, path

    def _paint_indicator_segment(self, painter, rect, active=False, pressed=False):
        top, bottom, arrow, border = self._indicator_colors(active, pressed)
        gradient = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        gradient.setColorAt(0.0, top)
        gradient.setColorAt(1.0, bottom)
        _outer, clip_path = self._outer_control_path()
        painter.save()
        painter.setClipPath(clip_path)
        painter.fillRect(rect, gradient)
        painter.restore()
        painter.setPen(QPen(border, 1.0))
        painter.drawLine(QPointF(rect.left(), rect.top() + 1.0), QPointF(rect.left(), rect.bottom() - 1.0))
        return arrow, border

    def _paint_control_outline(self, painter, border):
        outer, _path = self._outer_control_path()
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(border, 1.0))
        painter.drawRoundedRect(outer, self._CONTROL_RADIUS, self._CONTROL_RADIUS)


class ThemedComboBox(QComboBox, _ThemedIndicatorMixin):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("esrCustomIndicator", True)
        self.setMouseTracking(True)
        self.setMinimumContentsLength(8)

    def setEditable(self, editable):
        super().setEditable(editable)
        line_edit = self.lineEdit()
        if line_edit is not None:
            line_edit.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            line_edit.setTextMargins(5, 0, 2, 0)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        width = min(31.0, max(26.0, self.height() - 1.0))
        rect = QRectF(self.width() - width, 0.5, width - 0.5, self.height() - 1.0)
        active = self.underMouse() or self.hasFocus()
        pressed = bool(self.view() is not None and self.view().isVisible())
        arrow, border = self._paint_indicator_segment(painter, rect, active, pressed)
        self._draw_indicator_chevron(painter, rect.center(), True, arrow)
        self._paint_control_outline(painter, border)
        painter.end()


class _ThemedSpinMixin(_ThemedIndicatorMixin):
    def _init_spin_indicator(self):
        self._hover_indicator = None
        self._pressed_indicator = None
        self.setMouseTracking(True)
        line_edit = self.lineEdit()
        if line_edit is not None:
            line_edit.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            line_edit.setTextMargins(5, 0, 2, 0)

    def _indicator_geometry(self):
        width = min(28.0, max(23.0, self.height() - 1.0))
        half = (self.height() - 1.0) / 2.0
        up_rect = QRectF(self.width() - width, 0.5, width - 0.5, half)
        down_rect = QRectF(self.width() - width, 0.5 + half, width - 0.5, half)
        return up_rect, down_rect

    def _indicator_at(self, position):
        up_rect, down_rect = self._indicator_geometry()
        point = QPointF(position)
        if up_rect.contains(point):
            return "up"
        if down_rect.contains(point):
            return "down"
        return None

    def _spin_mouse_move(self, event):
        value = self._indicator_at(event.pos())
        if value != self._hover_indicator:
            self._hover_indicator = value
            self.update()

    def _spin_mouse_press(self, event):
        self._pressed_indicator = self._indicator_at(event.pos()) if event.button() == Qt.LeftButton else None
        self.update()

    def _spin_mouse_release(self, event):
        self._pressed_indicator = None
        self._hover_indicator = self._indicator_at(event.pos())
        self.update()

    def _spin_leave(self):
        self._hover_indicator = None
        self._pressed_indicator = None
        self.update()

    def _paint_spin_indicators(self):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        up_rect, down_rect = self._indicator_geometry()
        up_active = self._hover_indicator == "up"
        down_active = self._hover_indicator == "down"
        up_pressed = self._pressed_indicator == "up"
        down_pressed = self._pressed_indicator == "down"
        up_arrow, up_border = self._paint_indicator_segment(painter, up_rect, up_active, up_pressed)
        down_arrow, down_border = self._paint_indicator_segment(painter, down_rect, down_active, down_pressed)
        border = down_border if (down_active or down_pressed) else up_border
        painter.setPen(QPen(border, 1.0))
        painter.drawLine(QPointF(down_rect.left(), down_rect.top()), QPointF(down_rect.right() - 1.0, down_rect.top()))
        self._draw_indicator_chevron(painter, up_rect.center(), False, up_arrow)
        self._draw_indicator_chevron(painter, down_rect.center(), True, down_arrow)
        self._paint_control_outline(painter, border)
        painter.end()


class ThemedSpinBox(QSpinBox, _ThemedSpinMixin):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("esrCustomIndicator", True)
        self._init_spin_indicator()

    def paintEvent(self, event):
        super().paintEvent(event)
        self._paint_spin_indicators()

    def mouseMoveEvent(self, event):
        self._spin_mouse_move(event)
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        self._spin_mouse_press(event)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self._spin_mouse_release(event)

    def leaveEvent(self, event):
        self._spin_leave()
        super().leaveEvent(event)


class ThemedDoubleSpinBox(QDoubleSpinBox, _ThemedSpinMixin):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("esrCustomIndicator", True)
        self._init_spin_indicator()

    def paintEvent(self, event):
        super().paintEvent(event)
        self._paint_spin_indicators()

    def mouseMoveEvent(self, event):
        self._spin_mouse_move(event)
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        self._spin_mouse_press(event)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self._spin_mouse_release(event)

    def leaveEvent(self, event):
        self._spin_leave()
        super().leaveEvent(event)

_FONT_CATALOG_CACHE = None
_FONT_CATALOG_LOADER = None
_FONT_NAME_ALIASES = {
    "Microsoft YaHei UI": "微软雅黑 UI",
    "Microsoft YaHei": "微软雅黑",
    "Microsoft JhengHei UI": "微软正黑体 UI",
    "Microsoft JhengHei": "微软正黑体",
    "SimSun": "宋体",
    "NSimSun": "新宋体",
    "SimSun-ExtB": "宋体扩展 B",
    "SimHei": "黑体",
    "KaiTi": "楷体",
    "KaiTi_GB2312": "楷体 GB2312",
    "FangSong": "仿宋",
    "FangSong_GB2312": "仿宋 GB2312",
    "DengXian": "等线",
    "DengXian Light": "等线 Light",
    "YouYuan": "幼圆",
    "LiSu": "隶书",
    "MingLiU": "细明体",
    "PMingLiU": "新细明体",
    "MingLiU-ExtB": "细明体扩展 B",
    "PMingLiU-ExtB": "新细明体扩展 B",
    "DFKai-SB": "标楷体",
    "STCaiyun": "华文彩云",
    "STFangsong": "华文仿宋",
    "STHupo": "华文琥珀",
    "STKaiti": "华文楷体",
    "STLiti": "华文隶书",
    "STSong": "华文宋体",
    "STXihei": "华文细黑",
    "STXingkai": "华文行楷",
    "STXinwei": "华文新魏",
    "FZShuTi": "方正舒体",
    "FZYaoTi": "方正姚体",
    "Noto Sans CJK SC": "思源黑体",
    "Noto Serif CJK SC": "思源宋体",
    "Source Han Sans SC": "思源黑体",
    "Source Han Serif SC": "思源宋体",
    "PingFang SC": "苹方",
    "HarmonyOS Sans SC": "鸿蒙黑体",
    "WenQuanYi Zen Hei": "文泉驿正黑",
    "WenQuanYi Micro Hei": "文泉驿微米黑",
    "LXGW WenKai": "霞鹜文楷",
    "LXGW WenKai Lite": "霞鹜文楷 Lite",
    "Sarasa Gothic SC": "更纱黑体 SC",
    "MiSans": "小米兰亭",
    "OPPO Sans": "OPPO 黑体",
    "Alibaba PuHuiTi": "阿里巴巴普惠体",
}
_FONT_PRIORITY = (
    "微软雅黑 UI", "微软雅黑", "等线", "宋体", "黑体", "楷体", "仿宋",
    "思源黑体", "思源宋体", "苹方", "鸿蒙黑体",
)


def _contains_chinese_name(value):
    for char in str(value or ""):
        if ("\u3400" <= char <= "\u4dbf") or ("\u4e00" <= char <= "\u9fff") or ("\uf900" <= char <= "\ufaff"):
            return True
    return False


def _font_catalog(cancel_check=None):
    """Build the filtered list on the background worker, never in a dialog."""
    global _FONT_CATALOG_CACHE
    if _FONT_CATALOG_CACHE is not None:
        return list(_FONT_CATALOG_CACHE)

    catalog = []
    seen = set()
    entries = scan_font_catalog(cancel_check=cancel_check)
    if entries is None:
        return None
    for family, style, path in entries:
        localized = _FONT_NAME_ALIASES.get(family, family)
        style_lower = style.lower()
        if style and style_lower not in ("regular", "normal", "book", "roman", "常规"):
            display = "%s · %s" % (localized, style)
        else:
            display = localized
        key = display.casefold()
        if key in seen:
            continue
        seen.add(key)
        catalog.append((display, path))

    priority = {name: index for index, name in enumerate(_FONT_PRIORITY)}
    catalog.sort(key=lambda item: (priority.get(item[0].split(" · ", 1)[0], 999), item[0].casefold()))
    _FONT_CATALOG_CACHE = tuple(catalog)
    return list(_FONT_CATALOG_CACHE)


class FontCatalogLoader(QObject):
    ready = Signal(object)

    def __init__(self):
        super().__init__(QApplication.instance())
        self.cancelled = threading.Event()
        self.running = False
        self.ready.connect(self._completed, Qt.QueuedConnection)
        QApplication.instance().aboutToQuit.connect(self.cancelled.set)

    def start(self):
        if self.running or self.cancelled.is_set() or _FONT_CATALOG_CACHE is not None:
            return
        self.running = True
        # A daemon worker cannot block application shutdown. No widget or
        # QFontDatabase calls occur on it; only file metadata/cmap is read.
        threading.Thread(target=self._scan, name='AnimeBoot-FontCatalog', daemon=True).start()

    def _scan(self):
        try:
            result = _font_catalog(self.cancelled.is_set)
        except Exception:
            result = []
        if result is not None and not self.cancelled.is_set():
            try:
                self.ready.emit(result)
            except RuntimeError:
                pass  # Qt may already be shutting down.

    def _completed(self, entries):
        global _FONT_CATALOG_CACHE
        self.running = False
        _FONT_CATALOG_CACHE = tuple(entries)


def _font_catalog_loader():
    global _FONT_CATALOG_LOADER
    if _FONT_CATALOG_LOADER is None:
        _FONT_CATALOG_LOADER = FontCatalogLoader()
    return _FONT_CATALOG_LOADER



def _localized_dialog_buttons(parent):
    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent)
    ok_button = buttons.button(QDialogButtonBox.Ok)
    cancel_button = buttons.button(QDialogButtonBox.Cancel)
    if ok_button is not None:
        ok_button.setText("确定")
    if cancel_button is not None:
        cancel_button.setText("取消")
    return buttons


def _localize_color_dialog(dialog):
    translations = {
        "basic colors:": "基本颜色：",
        "custom colors:": "自定义颜色：",
        "add to custom colors": "添加到自定义颜色",
        "pick screen color": "拾取屏幕颜色",
        "hue:": "色相：",
        "sat:": "饱和度：",
        "saturation:": "饱和度：",
        "val:": "亮度：",
        "value:": "亮度：",
        "red:": "红色：",
        "green:": "绿色：",
        "blue:": "蓝色：",
        "alpha channel:": "透明度：",
        "alpha:": "透明度：",
        "html:": "十六进制：",
    }
    for label in dialog.findChildren(QLabel):
        key = str(label.text() or "").replace("&", "").strip().lower()
        if key in translations:
            label.setText(translations[key])
    for button in dialog.findChildren(QPushButton):
        key = str(button.text() or "").replace("&", "").strip().lower()
        if key in translations:
            button.setText(translations[key])
    button_box = dialog.findChild(QDialogButtonBox)
    if button_box is not None:
        ok_button = button_box.button(QDialogButtonBox.Ok)
        cancel_button = button_box.button(QDialogButtonBox.Cancel)
        if ok_button is not None:
            ok_button.setText("确定")
        if cancel_button is not None:
            cancel_button.setText("取消")


class DialogLocalizationFilter(QObject):
    BUTTON_TEXT = {
        "ok": "确定",
        "cancel": "取消",
        "yes": "是",
        "no": "否",
        "close": "关闭",
        "save": "保存",
        "discard": "放弃",
        "retry": "重试",
        "abort": "中止",
        "ignore": "忽略",
        "apply": "应用",
        "open": "打开",
        "reset": "重置",
        "restore defaults": "恢复默认值",
    }

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Show and isinstance(watched, QDialog):
            if isinstance(watched, QColorDialog):
                _localize_color_dialog(watched)
            for button in watched.findChildren(QPushButton):
                key = str(button.text() or "").replace("&", "").strip().lower()
                if key in self.BUTTON_TEXT:
                    button.setText(self.BUTTON_TEXT[key])
        return False


class MultiLineTextDialog(QDialog):
    def __init__(self, title, label, text, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        prompt = QLabel(label)
        layout.addWidget(prompt)
        self.editor = QPlainTextEdit(str(text))
        self.editor.setMinimumHeight(110)
        layout.addWidget(self.editor)
        buttons = _localized_dialog_buttons(self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def value(self):
        return self.editor.toPlainText()


class ItemChoiceDialog(QDialog):
    def __init__(self, title, label, items, current_index=0, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self.setMinimumWidth(500)
        layout = QVBoxLayout(self)
        prompt = QLabel(label)
        prompt.setWordWrap(True)
        layout.addWidget(prompt)
        self.combo = ThemedComboBox(self)
        self.combo.addItems([str(item) for item in items])
        self.combo.setCurrentIndex(max(0, min(int(current_index), self.combo.count() - 1)))
        layout.addWidget(self.combo)
        buttons = _localized_dialog_buttons(self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def value(self):
        return self.combo.currentText()









class LayerListWidget(QListWidget):
    orderChanged = Signal(object)
    leftDoubleClicked = Signal(str)
    contextMenuForItem = Signal(str, object)
    MIME_TYPE = "application/x-animebootneo-layer-key"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        # Do not use QListWidget.InternalMove here.  On Qt 5/Windows, dropping
        # directly over another row can make QListWidget interpret the native
        # move as an item replacement.  The target row then disappears until
        # the view is rebuilt.  We use a private MIME type and perform one
        # explicit take/insert operation instead, so no target row is removed.
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDragDropOverwriteMode(False)
        self.setDefaultDropAction(Qt.MoveAction)
        self._dragged_key = ""

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            item = self.itemAt(event.pos())
            if item is not None:
                self.leftDoubleClicked.emit(str(item.data(Qt.UserRole) or ""))
                event.accept()
                return
        event.accept()

    def contextMenuEvent(self, event):
        item = self.itemAt(event.pos())
        if item is not None:
            self.setCurrentItem(item)
            item.setSelected(True)
            self.contextMenuForItem.emit(str(item.data(Qt.UserRole) or ""), event.globalPos())
            event.accept()
            return
        super().contextMenuEvent(event)

    def startDrag(self, supported_actions):
        item = self.currentItem()
        if item is None:
            return
        key = str(item.data(Qt.UserRole) or "")
        if not key or key in ("circle", "bar"):
            return
        mime = QMimeData()
        mime.setData(self.MIME_TYPE, key.encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        rect = self.visualItemRect(item)
        if rect.isValid() and not rect.isEmpty():
            pixmap = self.viewport().grab(rect)
            if not pixmap.isNull():
                drag.setPixmap(pixmap)
                drag.setHotSpot(QPointF(12, min(12, max(0, pixmap.height() - 1))).toPoint())
        self._dragged_key = key
        try:
            drag.exec_(Qt.MoveAction, Qt.MoveAction)
        finally:
            self._dragged_key = ""

    def dragEnterEvent(self, event):
        if event.source() is self and event.mimeData().hasFormat(self.MIME_TYPE):
            event.setDropAction(Qt.MoveAction)
            event.accept()
            return
        event.ignore()

    def dragMoveEvent(self, event):
        if event.source() is self and event.mimeData().hasFormat(self.MIME_TYPE):
            event.setDropAction(Qt.MoveAction)
            event.accept()
            self.viewport().update()
            return
        event.ignore()

    def _row_for_key(self, key):
        for row in range(self.count()):
            item = self.item(row)
            if item is not None and str(item.data(Qt.UserRole) or "") == key:
                return row
        return -1

    def _countdown_present(self):
        if self.count() <= 0:
            return False
        item = self.item(0)
        return item is not None and str(item.data(Qt.UserRole) or "") in ("circle", "bar")

    def _insertion_row(self, pos):
        target = self.itemAt(pos)
        if target is None:
            row = self.count()
        else:
            target_row = self.row(target)
            rect = self.visualItemRect(target)
            # Dropping over the top half inserts before; dropping over the
            # bottom half inserts after.  It never replaces the target item.
            row = target_row if pos.y() < rect.center().y() else target_row + 1
        if self._countdown_present():
            row = max(1, row)
        return max(0, min(row, self.count()))

    def dropEvent(self, event):
        if event.source() is not self or not event.mimeData().hasFormat(self.MIME_TYPE):
            event.ignore()
            return
        try:
            key = bytes(event.mimeData().data(self.MIME_TYPE)).decode("utf-8")
        except Exception:
            key = str(self._dragged_key or "")
        if not key or key in ("circle", "bar"):
            event.ignore()
            return

        current_keys = []
        for row in range(self.count()):
            item = self.item(row)
            item_key = str(item.data(Qt.UserRole) or "") if item is not None else ""
            if item_key:
                current_keys.append(item_key)
        if key not in current_keys:
            event.ignore()
            return

        source_row = current_keys.index(key)
        insertion_row = self._insertion_row(event.pos())
        if source_row < insertion_row:
            insertion_row -= 1

        # Never mutate QListWidget's item model inside dropEvent.  Qt 5 on
        # Windows may still perform native move cleanup after the callback;
        # takeItem()/insertItem() here can therefore delete the row under the
        # pointer.  Compute the requested order only, then let MainWindow update
        # the project model and rebuild the list after the drag transaction.
        reordered = list(current_keys)
        reordered.pop(source_row)
        minimum = 1 if reordered and reordered[0] in ("circle", "bar") else 0
        insertion_row = max(minimum, min(insertion_row, len(reordered)))
        reordered.insert(insertion_row, key)

        event.setDropAction(Qt.MoveAction)
        event.accept()
        QTimer.singleShot(0, lambda order=list(reordered): self.orderChanged.emit(order))


class CanvasView(QGraphicsView):
    selectionChanged = Signal(str)
    geometryChanged = Signal()
    editRequested = Signal(str)
    copyRequested = Signal(str)
    deleteRequested = Signal(str)
    layerMoveRequested = Signal(str, str)
    alignmentRequested = Signal(str, str)

    def __init__(self, project_getter, originals_getter, parent=None):
        super().__init__(parent)
        self._project_getter = project_getter
        self._originals_getter = originals_getter
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setViewportUpdateMode(QGraphicsView.BoundingRectViewportUpdate)
        self.setBackgroundBrush(QColor("#0b1522"))
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.viewport().setFocusPolicy(Qt.StrongFocus)
        self._pixmap_item = None
        self._selection_item = None
        self._handle_items = {}
        self._lock_label = None
        self._guide_items = []
        self.selected_key = "circle"
        self._fit_mode = True
        self._drag = None
        self._last_drag_preview = 0.0
        self._empty = True
        self._theme = THEME_AERO
        self._guides_visible = True
        self._keyboard_geometry_dirty = False
        self._last_key_repeat_move = 0.0

    def set_theme(self, theme_id):
        self._theme = theme_id if theme_id in THEME_STYLES else THEME_AERO
        background = {THEME_AERO: "#0b1522", THEME_LIQUID: "#171a2a", THEME_DARK: "#101010"}[self._theme]
        self.setBackgroundBrush(QColor(background))
        if self.scene():
            self.refresh(draft=True)

    def set_guides_visible(self, visible):
        self._guides_visible = bool(visible)
        self._draw_guides()

    def guides_visible(self):
        return self._guides_visible

    def set_selected(self, key):
        self.selected_key = key or ""
        self._draw_selection()

    def refresh(self, draft=False, keep_view=True):
        project = self._project_getter()
        originals = self._originals_getter()
        width, height = [int(v) for v in project["resolution"]]
        center = self.mapToScene(self.viewport().rect().center()) if keep_view else QPointF(width / 2, height / 2)
        old_scale = self.transform().m11()
        self._scene.clear()
        self._pixmap_item = None
        self._selection_item = None
        self._handle_items = {}
        self._lock_label = None
        self._guide_items = []
        self._scene.setSceneRect(0, 0, width, height)

        if originals:
            factor = min(1.0, 1600.0 / max(1, width), 1000.0 / max(1, height))
            target_w = max(1, int(round(width * factor)))
            target_h = max(1, int(round(height * factor)))
            image = render_preview_scaled(
                project,
                originals,
                target_w,
                target_h,
                preview_fraction=1.0,
                font_path="",
                draft=draft,
            )
            pixmap = _pil_to_pixmap(image)
            self._pixmap_item = self._scene.addPixmap(pixmap)
            self._pixmap_item.setScale(1.0 / factor)
            self._empty = False
        else:
            empty_colors = {
                THEME_AERO: ("#38536d", "#101c2c", "#ecf7ff", "#92abc1"),
                THEME_LIQUID: ("#6a79a7", "#171a2a", "#f6f1ff", "#aeb7d4"),
                THEME_DARK: ("#4a4a4a", "#151515", "#f0f0f0", "#999999"),
            }
            border_color, fill_color, title_color, hint_color = empty_colors.get(self._theme, empty_colors[THEME_AERO])
            canvas = self._scene.addRect(0, 0, width, height, QPen(QColor(border_color), 2), QColor(fill_color))
            canvas.setZValue(-1)
            font_scale = max(0.55, min(4.0, height / 768.0))
            title_font = QFont(QApplication.font().family(), max(17, int(round(31 * font_scale))), QFont.DemiBold)
            hint_font = QFont(QApplication.font().family(), max(8, int(round(11 * font_scale))))
            title = self._scene.addText("画布预览区", title_font)
            title.setDefaultTextColor(QColor(title_color))
            title.setPos(width / 2 - title.boundingRect().width() / 2, height * 0.34)
            hint = self._scene.addText("编辑背景、Logo、文字与倒计时", hint_font)
            hint.setDefaultTextColor(QColor(hint_color))
            hint.setPos(width / 2 - hint.boundingRect().width() / 2, height * 0.44)
            self._empty = True

        self._draw_guides()
        self._draw_selection()
        if self._fit_mode or not keep_view:
            self.fit_canvas()
        elif old_scale > 0:
            self.resetTransform()
            self.scale(old_scale, old_scale)
            self.centerOn(center)

    def _update_drag_preview(self, force=False):
        originals = self._originals_getter()
        if not originals or self._pixmap_item is None:
            return
        now = time.monotonic()
        if not force and now - self._last_drag_preview < 0.025:
            return
        self._last_drag_preview = now
        project = self._project_getter()
        width, height = [int(v) for v in project["resolution"]]
        factor = min(1.0, 1280.0 / max(1, width), 800.0 / max(1, height))
        image = render_preview_scaled(
            project,
            originals,
            max(1, int(round(width * factor))),
            max(1, int(round(height * factor))),
            preview_fraction=1.0,
            font_path="",
            draft=True,
        )
        self._pixmap_item.setPixmap(_pil_to_pixmap(image))
        self._pixmap_item.setScale(1.0 / factor)

    def _clear_selection_artifacts(self):
        if self._selection_item is not None:
            try:
                self._scene.removeItem(self._selection_item)
            except Exception:
                pass
            self._selection_item = None
        for item in list(self._handle_items.values()):
            try:
                self._scene.removeItem(item)
            except Exception:
                pass
        self._handle_items = {}
        if self._lock_label is not None:
            try:
                self._scene.removeItem(self._lock_label)
            except Exception:
                pass
            self._lock_label = None

    def _draw_guides(self):
        for item in self._guide_items:
            try:
                self._scene.removeItem(item)
            except Exception:
                pass
        self._guide_items = []
        if not self._guides_visible or self._scene.sceneRect().isEmpty():
            return
        rect = self._scene.sceneRect()
        color = QColor(114, 210, 255, 185) if self._theme != THEME_DARK else QColor(85, 170, 235, 185)
        pen = QPen(color, 1.0, Qt.DashLine)
        pen.setCosmetic(True)
        vertical = self._scene.addLine(rect.center().x(), rect.top(), rect.center().x(), rect.bottom(), pen)
        horizontal = self._scene.addLine(rect.left(), rect.center().y(), rect.right(), rect.center().y(), pen)
        vertical.setZValue(900)
        horizontal.setZValue(900)
        self._guide_items = [vertical, horizontal]

    def _selection_rect(self, key=None):
        spec = _spec_for_key(self._project_getter(), key or self.selected_key)
        if not spec or not spec.get("visible", True):
            return None
        return QRectF(float(spec.get("x", 0)), float(spec.get("y", 0)), float(spec.get("w", 1)), float(spec.get("h", 1)))

    def _selection_polygon(self, key=None):
        """Return the visible selection outline using the renderer's rotation."""
        actual_key = key or self.selected_key
        rect = self._selection_rect(actual_key)
        if rect is None:
            return None
        spec = _spec_for_key(self._project_getter(), actual_key) or {}
        try:
            angle = 0.0 if actual_key == "circle" else float(spec.get("rotation", 0.0) or 0.0)
        except (TypeError, ValueError):
            angle = 0.0
        points = rotated_corners(rect.x(), rect.y(), rect.width(), rect.height(), angle)
        return QPolygonF([QPointF(px, py) for px, py in points])

    def _handle_rects(self, rect, key=None):
        """Return eight resize handles: four corners plus four edge midpoints."""
        hs = max(8.0, 10.0 / max(0.1, self.transform().m11()))
        polygon = self._selection_polygon(key)
        if polygon is not None and polygon.count() >= 4:
            tl, tr, br, bl = [polygon.at(index) for index in range(4)]
        else:
            tl, tr, br, bl = rect.topLeft(), rect.topRight(), rect.bottomRight(), rect.bottomLeft()

        def midpoint(a, b):
            return QPointF((a.x() + b.x()) / 2.0, (a.y() + b.y()) / 2.0)

        centers = {
            "tl": tl, "t": midpoint(tl, tr), "tr": tr,
            "r": midpoint(tr, br),
            "br": br, "b": midpoint(bl, br), "bl": bl,
            "l": midpoint(tl, bl),
        }
        return dict((name, QRectF(point.x() - hs / 2, point.y() - hs / 2, hs, hs)) for name, point in centers.items())

    def _draw_selection(self):
        self._clear_selection_artifacts()
        if self._empty or not self.selected_key:
            return
        project = self._project_getter()
        spec = _spec_for_key(project, self.selected_key)
        if not spec or not spec.get("visible", True):
            return
        rect = self._selection_rect()
        polygon = self._selection_polygon()
        locked = bool(spec.get("locked", False))
        pen = QPen(QColor("#ffb84d" if locked else "#62c8ff"), 2, Qt.DashLine if locked else Qt.SolidLine)
        pen.setCosmetic(True)
        self._selection_item = self._scene.addPolygon(polygon, pen)
        self._selection_item.setZValue(1000)
        if locked:
            label = self._scene.addText("已锁定", QFont(QApplication.font().family(), 9, QFont.DemiBold))
            label.setDefaultTextColor(QColor("#ffb84d"))
            label.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
            bounds = polygon.boundingRect()
            label.setPos(bounds.left() + 2.0, bounds.top() + 2.0)
            label.setZValue(1002)
            self._lock_label = label
            return
        for name, handle_rect in self._handle_rects(rect).items():
            handle = self._scene.addRect(handle_rect, QPen(QColor("#ffffff"), 1), QColor("#1f91d0"))
            handle.setZValue(1001)
            self._handle_items[name] = handle

    def fit_canvas(self):
        self._fit_mode = True
        if not self._scene.sceneRect().isEmpty():
            self.fitInView(self._scene.sceneRect(), Qt.KeepAspectRatio)
            self._draw_guides()
            self._draw_selection()

    def zoom_in(self):
        self._fit_mode = False
        self.scale(1.18, 1.18)
        self._draw_guides()
        self._draw_selection()

    def zoom_out(self):
        self._fit_mode = False
        self.scale(1 / 1.18, 1 / 1.18)
        self._draw_guides()
        self._draw_selection()

    def _reflow_top_bar(self):
        if not hasattr(self,'top_bar_grid'):return
        threshold=self.top_brand.minimumSizeHint().width()+self.top_actions.minimumSizeHint().width()+36
        narrow=self.top_bar.width()<threshold
        if narrow==self._top_bar_wrapped:return
        self.top_bar_grid.removeWidget(self.top_actions)
        if narrow:
            self.top_bar_grid.addWidget(self.top_actions,1,0,1,2)
            self.top_bar_grid.setColumnStretch(0,1);self.top_bar_grid.setColumnStretch(1,0)
        else:
            self.top_bar_grid.addWidget(self.top_actions,0,1)
            self.top_bar_grid.setColumnStretch(0,0);self.top_bar_grid.setColumnStretch(1,1)
        self._top_bar_wrapped=narrow;self.top_actions.show();self.top_bar.updateGeometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self,'top_bar'):QTimer.singleShot(0,self._reflow_top_bar)
        if self._fit_mode:
            QTimer.singleShot(0, self.fit_canvas)

    def _hit_test(self, point):
        project = self._project_getter()
        keys = []
        family = countdown_key(project.get("countdown_mode", "none"))
        if family:
            keys.append(family)
        keys.extend(reversed(project.get("layer_order", [])))
        for key in keys:
            spec = _spec_for_key(project, key)
            if not spec or not spec.get("visible", True):
                continue
            polygon = self._selection_polygon(key)
            if polygon is not None and polygon.containsPoint(point, Qt.OddEvenFill):
                return key
        return ""

    def _selected_hit(self, point):
        rect = self._selection_rect()
        if rect is None:
            return False
        polygon = self._selection_polygon()
        if polygon is not None and polygon.containsPoint(point, Qt.OddEvenFill):
            return True
        for handle_rect in self._handle_rects(rect).values():
            if handle_rect.contains(point):
                return True
        return False

    def _resize_handle_at(self, point):
        rect = self._selection_rect()
        if rect is None:
            return ""
        for name, handle_rect in self._handle_rects(rect).items():
            if handle_rect.contains(point):
                return name
        return ""

    def _update_cursor(self, viewport_pos):
        if self._empty:
            self.viewport().setCursor(Qt.OpenHandCursor)
            return
        point = self.mapToScene(viewport_pos)
        handle = self._resize_handle_at(point)
        if handle in ("tl", "br"):
            self.viewport().setCursor(Qt.SizeFDiagCursor)
        elif handle in ("tr", "bl"):
            self.viewport().setCursor(Qt.SizeBDiagCursor)
        elif handle in ("t", "b"):
            self.viewport().setCursor(Qt.SizeVerCursor)
        elif handle in ("l", "r"):
            self.viewport().setCursor(Qt.SizeHorCursor)
        elif self._hit_test(point):
            self.viewport().setCursor(Qt.ArrowCursor)
        else:
            self.viewport().setCursor(Qt.OpenHandCursor)

    def _nudge_selected(self, dx, dy):
        project = self._project_getter()
        spec = _spec_for_key(project, self.selected_key)
        if not spec or not spec.get("visible", True) or spec.get("locked", False):
            return False
        canvas_w, canvas_h = [int(v) for v in project["resolution"]]
        old_x, old_y = int(spec.get("x", 0)), int(spec.get("y", 0))
        rect = (float(old_x), float(old_y), float(spec.get("w", 1)), float(spec.get("h", 1)))
        try:
            angle = 0.0 if self.selected_key == "circle" else float(spec.get("rotation", 0.0) or 0.0)
        except (TypeError, ValueError):
            angle = 0.0
        move_x, move_y = clamp_move_delta(rect, angle, float(dx), float(dy), canvas_w, canvas_h)
        # Keyboard nudging is exact: at a boundary we stop rather than sliding in
        # the other axis to satisfy the mouse-drag nearest-point rule.
        if abs(move_x - float(dx)) > 1.0e-6 or abs(move_y - float(dy)) > 1.0e-6:
            return False
        spec["x"] = int(round(old_x + move_x))
        spec["y"] = int(round(old_y + move_y))
        if spec["x"] == old_x and spec["y"] == old_y:
            return False
        self._keyboard_geometry_dirty = True
        self._update_drag_preview(force=True)
        self._draw_selection()
        return True

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_Delete and self.selected_key:
            self.deleteRequested.emit(self.selected_key)
            event.accept()
            return
        directions = {
            Qt.Key_Left: (-1, 0), Qt.Key_Right: (1, 0),
            Qt.Key_Up: (0, -1), Qt.Key_Down: (0, 1),
        }
        if key in directions and self.selected_key:
            # A short press moves exactly 1 px. Holding a key uses Qt/Windows
            # auto-repeat, throttled to a calm ~20 px/s instead of racing.
            if event.isAutoRepeat():
                now = time.monotonic()
                if now - self._last_key_repeat_move < 0.05:
                    event.accept()
                    return
                self._last_key_repeat_move = now
            else:
                self._last_key_repeat_move = 0.0
            self._nudge_selected(*directions[key])
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down):
            if not event.isAutoRepeat() and self._keyboard_geometry_dirty:
                self._keyboard_geometry_dirty = False
                self.geometryChanged.emit()
            event.accept()
            return
        super().keyReleaseEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and not self._empty:
            self.setFocus(Qt.MouseFocusReason)
            point = self.mapToScene(event.pos())
            key = self.selected_key if self._selected_hit(point) else self._hit_test(point)
            if key:
                if key != self.selected_key:
                    self.selected_key = key
                    self.selectionChanged.emit(key)
                spec = _spec_for_key(self._project_getter(), key)
                if spec and not spec.get("locked", False):
                    rect = self._selection_rect(key)
                    handle = self._resize_handle_at(point)
                    mode = "resize_" + handle if handle else "move"
                    self._drag = (mode, point, rect)
                    self.setDragMode(QGraphicsView.NoDrag)
                    self.viewport().setCursor(Qt.SizeAllCursor if mode == "move" else self.viewport().cursor())
                    event.accept()
                    return
        super().mousePressEvent(event)

    def _apply_resize(self, spec, mode, point, original, canvas_w, canvas_h):
        corner = mode.split("_", 1)[1]
        try:
            angle = 0.0 if self.selected_key == "circle" else float(spec.get("rotation", 0.0) or 0.0)
        except (TypeError, ValueError):
            angle = 0.0
        x, y, width, height = resize_from_rotated_handle(
            (original.x(), original.y(), original.width(), original.height()),
            angle, corner, (point.x(), point.y()), canvas_w, canvas_h,
            bool(spec.get("lock_aspect", False)),
            16.0 if self.selected_key == "bar" and self._project_getter().get("countdown_mode") == "number" else 12.0,
        )
        candidate = (int(round(x)), int(round(y)), max(1, int(round(width))), max(1, int(round(height))))
        if self.selected_key == "bar" and self._project_getter().get("countdown_mode") == "number":
            numeric_w, numeric_h = max(24, candidate[2]), max(16, candidate[3])
            candidate = (max(0, min(candidate[0], canvas_w-numeric_w)),
                         max(0, min(candidate[1], canvas_h-numeric_h)), numeric_w, numeric_h)
        if abs(angle) >= 1.0e-6 and not has_corner_in_canvas(candidate, angle, canvas_w, canvas_h):
            return
        spec["x"], spec["y"], spec["w"], spec["h"] = candidate

    def mouseMoveEvent(self, event):
        if self._drag:
            project = self._project_getter()
            spec = _spec_for_key(project, self.selected_key)
            if not spec:
                return
            mode, start, original = self._drag
            point = self.mapToScene(event.pos())
            dx, dy = point.x() - start.x(), point.y() - start.y()
            canvas_w, canvas_h = [int(v) for v in project["resolution"]]
            if mode == "move":
                try:
                    angle = 0.0 if self.selected_key == "circle" else float(spec.get("rotation", 0.0) or 0.0)
                except (TypeError, ValueError):
                    angle = 0.0
                move_x, move_y = clamp_move_delta(
                    (original.x(), original.y(), original.width(), original.height()),
                    angle, dx, dy, canvas_w, canvas_h,
                )
                spec["x"] = int(round(original.x() + move_x))
                spec["y"] = int(round(original.y() + move_y))
            else:
                self._apply_resize(spec, mode, point, original, canvas_w, canvas_h)
            self._update_drag_preview()
            self._draw_selection()
            event.accept()
            return
        self._update_cursor(event.pos())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag:
            self._update_drag_preview(force=True)
            self._drag = None
            self.setDragMode(QGraphicsView.ScrollHandDrag)
            self._update_cursor(event.pos())
            self.geometryChanged.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event):
        if not self._drag:
            self.viewport().setCursor(Qt.OpenHandCursor)
        super().leaveEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton and not self._empty:
            point = self.mapToScene(event.pos())
            key = self.selected_key if self._selected_hit(point) else self._hit_test(point)
            if key:
                self._drag = None
                self.setDragMode(QGraphicsView.ScrollHandDrag)
                self.selected_key = key
                self.selectionChanged.emit(key)
                self.editRequested.emit(key)
                event.accept()
                return
        event.accept()

    def contextMenuEvent(self, event):
        if self._empty:
            super().contextMenuEvent(event)
            return
        point = self.mapToScene(event.pos())
        key = self.selected_key if self._selected_hit(point) else self._hit_test(point)
        if not key:
            super().contextMenuEvent(event)
            return
        self.selected_key = key
        self.selectionChanged.emit(key)
        menu = QMenu(self)
        properties = menu.addAction("属性")
        copy_action = menu.addAction("复制")
        copy_action.setEnabled(key not in ("circle", "bar"))
        delete_action = menu.addAction("删除")
        menu.addSeparator()
        horizontal_action = menu.addAction("水平居中")
        vertical_action = menu.addAction("垂直居中")
        fullscreen_action = menu.addAction("一键全屏居中")
        menu.addSeparator()
        top_action = menu.addAction("置顶")
        up_action = menu.addAction("上移一层")
        down_action = menu.addAction("下移一层")
        is_countdown = key in ("circle", "bar")
        top_action.setEnabled(not is_countdown)
        up_action.setEnabled(not is_countdown)
        down_action.setEnabled(not is_countdown)
        chosen = menu.exec_(event.globalPos())
        if chosen is properties:
            self.editRequested.emit(key)
        elif chosen is copy_action:
            self.copyRequested.emit(key)
        elif chosen is delete_action:
            self.deleteRequested.emit(key)
        elif chosen is horizontal_action:
            self.alignmentRequested.emit(key, "horizontal")
        elif chosen is vertical_action:
            self.alignmentRequested.emit(key, "vertical")
        elif chosen is fullscreen_action:
            self.alignmentRequested.emit(key, "fullscreen")
        elif chosen is top_action:
            self.layerMoveRequested.emit(key, "top")
        elif chosen is up_action:
            self.layerMoveRequested.emit(key, "up")
        elif chosen is down_action:
            self.layerMoveRequested.emit(key, "down")
        event.accept()


class ObjectPropertiesDialog(QDialog):
    def __init__(self, project, key, parent=None):
        super().__init__(parent)
        self.setWindowTitle("对象属性")
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self.setMinimumWidth(450)
        self.project = project
        self.key = key
        self.spec = copy.deepcopy(_spec_for_key(project, key) or {})
        numeric_countdown = key == "bar" and project.get("countdown_mode") == "number"
        textured_countdown = key == "bar" and project.get("countdown_mode") == "bar_texture"
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.x = ThemedSpinBox(); self.y = ThemedSpinBox(); self.w = ThemedSpinBox(); self.h = ThemedSpinBox()
        # x/y are the top-left of the *unrotated logical box*.  A tilted object
        # may legitimately have a negative logical x/y while one of its rotated
        # corners remains visible.  At exactly 0 degrees keep the legacy rule
        # that the logical rectangle itself cannot start outside the canvas.
        try:
            initial_rotation = float(self.spec.get("rotation", 0.0) or 0.0)
        except (TypeError, ValueError):
            initial_rotation = 0.0
        signed_position = key not in ("circle", "bar") and abs(initial_rotation) >= 1.0e-6
        coordinate_minimum = -8192 if signed_position else 0
        self.x.setRange(coordinate_minimum, 8192); self.y.setRange(coordinate_minimum, 8192)
        self.w.setRange(0, 8192); self.h.setRange(0, 8192)
        if numeric_countdown:
            self.w.setMinimum(24); self.h.setMinimum(16)
        elif textured_countdown:
            self.w.setMinimum(4); self.h.setMinimum(4)
        self.x.setValue(int(self.spec.get("x", 0)))
        self.y.setValue(int(self.spec.get("y", 0)))
        self.w.setValue(int(self.spec.get("w", 1)))
        self.h.setValue(int(self.spec.get("h", 1)))
        self._syncing_square = False
        self.w.valueChanged.connect(lambda value: self._sync_square_size("w", value))
        self.h.valueChanged.connect(lambda value: self._sync_square_size("h", value))
        form.addRow("X", self.x); form.addRow("Y", self.y); form.addRow("宽度", self.w); form.addRow("高度", self.h)
        self.visible = None
        if key not in ("circle", "bar"):
            self.visible = QCheckBox("显示对象")
            self.visible.setChecked(bool(self.spec.get("visible", True)))
            form.addRow(self.visible)
        else:
            self.spec["visible"] = True
        self.locked = QCheckBox("锁定位置与尺寸")
        self.locked.setChecked(bool(self.spec.get("locked", False)))
        form.addRow(self.locked)

        self.rotation = None
        if key not in ("circle", "bar"):
            self.rotation = ThemedDoubleSpinBox()
            self.rotation.setRange(-180.0, 180.0)
            self.rotation.setDecimals(1)
            self.rotation.setSingleStep(1.0)
            self.rotation.setSuffix("°")
            self.rotation.setValue(float(self.spec.get("rotation", 0.0) or 0.0))
            self.rotation.valueChanged.connect(self._rotation_coordinate_range_changed)
            form.addRow("旋转角度（正值顺时针）", self.rotation)

        self.contain = None
        self.aspect = None
        self.text = None
        self.font_size = None
        self.font_path = None
        self.align = None
        self.color = None
        self.bar_fg = None
        self.bar_bg = None
        self.bar_border = None
        if key in ("logo", "company") or key.startswith("extra:"):
            self.contain = QCheckBox("保持图像宽高比")
            self.contain.setChecked(bool(self.spec.get("contain", False)))
            form.addRow(self.contain)
        if key != "bar":
            self.aspect = QCheckBox("保持正方形")
            self.aspect.setChecked(bool(self.spec.get("lock_aspect", key in ("logo", "circle"))))
            self.aspect.toggled.connect(self._square_toggled)
            form.addRow(self.aspect)
        if key == "prompt" or key.startswith("text:"):
            if key == "prompt" or key.startswith("text:"):
                self.text = QPlainTextEdit(str(self.spec.get("text", "")))
                self.text.setMaximumHeight(100)
                form.addRow("文字内容", self.text)
            self.font_size = ThemedSpinBox(); self.font_size.setRange(8, 300); self.font_size.setValue(int(self.spec.get("font_size", 32)))
            form.addRow("字号", self.font_size)
            self.font_path = ThemedComboBox()
            self.font_path.setMaxVisibleItems(24)
            self.font_path.addItem("自动选择中文字体（推荐）", "")
            current = self.spec.get("font_path", "")
            if current:
                self.font_path.addItem("当前字体：" + os.path.basename(current), current)
            index = self.font_path.findData(current)
            self.font_path.setCurrentIndex(max(0, index))
            form.addRow("中文字体", self.font_path)
            self.font_status = QLabel("正在检查已安装字体…")
            self.font_status.setObjectName("MutedLabel")
            form.addRow(self.font_status)
            font_loader = _font_catalog_loader()
            font_loader.ready.connect(self._populate_fonts, Qt.QueuedConnection)
            if _FONT_CATALOG_CACHE is not None:
                self._populate_fonts(_FONT_CATALOG_CACHE)
            else:
                font_loader.start()
            if key.startswith("text:"):
                self.align = ThemedComboBox(); self.align.addItem("左对齐", "left"); self.align.addItem("居中", "center"); self.align.addItem("右对齐", "right")
                idx = self.align.findData(self.spec.get("align", "left")); self.align.setCurrentIndex(max(0, idx))
                form.addRow("对齐方式", self.align)
            color_row = QHBoxLayout()
            self.color = QLineEdit(str(self.spec.get("color", "#FFFFFF")))
            color_btn = QPushButton("选择文字颜色")
            color_btn.clicked.connect(self._choose_color)
            color_row.addWidget(self.color); color_row.addWidget(color_btn)
            form.addRow("文字颜色", color_row)
        if key == "bar":
            def bar_color_row(value, title):
                holder = QWidget(self)
                row = QHBoxLayout(holder)
                row.setContentsMargins(0, 0, 0, 0)
                edit = QLineEdit(str(value))
                button = QPushButton("选择颜色")
                button.clicked.connect(lambda _checked=False, e=edit, t=title: self._choose_color_for(e, t))
                row.addWidget(edit, 1); row.addWidget(button)
                return holder, edit
            holder, self.bar_fg = bar_color_row(self.spec.get("fg", "#FFFFFF"), "选择数字颜色" if numeric_countdown else "选择填充颜色")
            form.addRow("数字颜色" if numeric_countdown else "填充颜色", holder)
            if numeric_countdown:
                hint = QLabel("数字使用 GRUB 内置固定 16px 字体，不能调整字号。宽度用于水平居中，高度至少为 16px。")
                hint.setWordWrap(True)
                hint.setObjectName("MutedLabel")
                form.addRow(hint)
            else:
                holder, self.bar_bg = bar_color_row(self.spec.get("bg", "#303030"), "选择背景颜色")
                form.addRow("背景颜色", holder)
                holder, self.bar_border = bar_color_row(self.spec.get("border", "#FFFFFF"), "选择边框颜色")
                form.addRow("边框颜色", holder)

        if key == "circle":
            self.tick_size = ThemedSpinBox(); self.tick_size.setRange(1, 128); self.tick_size.setValue(int(self.spec.get("tick_size", 8)))
            self.tick_count = ThemedSpinBox(); self.tick_count.setRange(8, 360); self.tick_count.setValue(int(self.spec.get("num_ticks", 100)))
            form.addRow("刻度尺寸", self.tick_size); form.addRow("刻度数量", self.tick_count)
        else:
            self.tick_size = self.tick_count = None

        layout.addLayout(form)
        buttons = _localized_dialog_buttons(self)
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _populate_fonts(self, entries):
        if self.font_path is None:
            return
        # A late scan must preserve the user's selection and typed text.
        current = self.font_path.currentData() or ""
        self.font_path.blockSignals(True)
        try:
            self.font_path.clear()
            self.font_path.addItem("自动选择中文字体（推荐）", "")
            for display_name, path in entries:
                self.font_path.addItem(display_name, path)
            index = self.font_path.findData(current)
            if current and index < 0:
                self.font_path.addItem("当前字体：" + os.path.basename(current), current)
                index = self.font_path.count() - 1
            self.font_path.setCurrentIndex(max(0, index))
        finally:
            self.font_path.blockSignals(False)
        self.font_status.hide()

    def _rotation_coordinate_range_changed(self, value):
        minimum = -8192 if abs(float(value or 0.0)) >= 1.0e-6 else 0
        # setMinimum intentionally clamps a negative x/y back to zero when the
        # user returns an object to 0 degrees, preserving the non-rotated rule.
        self.x.setMinimum(minimum)
        self.y.setMinimum(minimum)

    def _sync_square_size(self, source, value):
        if self._syncing_square or not self.aspect or not self.aspect.isChecked():
            return
        self._syncing_square = True
        try:
            if source == "w":
                self.h.setValue(max(1, int(value)))
            else:
                self.w.setValue(max(1, int(value)))
        finally:
            self._syncing_square = False

    def _square_toggled(self, checked):
        if checked:
            self._sync_square_size("w", self.w.value())

    def _choose_color_for(self, editor, title):
        dialog = QColorDialog(QColor(editor.text()), self)
        dialog.setWindowTitle(title)
        dialog.setOption(QColorDialog.DontUseNativeDialog, True)
        _localize_color_dialog(dialog)
        if dialog.exec_() == QDialog.Accepted:
            chosen = dialog.currentColor()
            if chosen.isValid():
                editor.setText(chosen.name().upper())

    def _choose_color(self):
        self._choose_color_for(self.color, "选择文字颜色")

    def apply(self):
        target = _spec_for_key(self.project, self.key)
        if not target:
            return
        target_width = max(1, self.w.value())
        target_height = max(1, self.h.value())
        if self.aspect and self.aspect.isChecked():
            target_size = max(1, min(target_width, target_height))
            target_width = target_height = target_size
        target.update({
            "x": self.x.value(), "y": self.y.value(), "w": target_width, "h": target_height,
            "locked": self.locked.isChecked(),
        })
        target["visible"] = True if self.key in ("circle", "bar") else bool(self.visible and self.visible.isChecked())
        if self.rotation is not None:
            target["rotation"] = float(self.rotation.value())
        if self.contain:
            target["contain"] = self.contain.isChecked()
        if self.bar_fg is not None:
            target["fg"] = self.bar_fg.text().strip().upper() or "#FFFFFF"
        if self.bar_bg is not None:
            target["bg"] = self.bar_bg.text().strip().upper() or "#303030"
        if self.bar_border is not None:
            target["border"] = self.bar_border.text().strip().upper() or "#FFFFFF"
        if self.aspect:
            target["lock_aspect"] = self.aspect.isChecked()
        if self.text:
            target["text"] = self.text.toPlainText()
        if self.font_size:
            target["font_size"] = self.font_size.value()
        if self.font_path:
            target["font_path"] = self.font_path.currentData() or ""
        if self.align:
            target["align"] = self.align.currentData()
        if self.color:
            target["color"] = self.color.text().strip() or "#FFFFFF"
        if self.tick_size:
            target["tick_size"] = self.tick_size.value()
        if self.tick_count:
            target["num_ticks"] = self.tick_count.value()


class FirstFramePreviewDialog(QDialog):
    def __init__(self, image, mode_name, parent=None):
        super().__init__(parent)
        self.setWindowTitle("首帧处理预览 - %s" % mode_name)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        layout = QVBoxLayout(self)
        info = QLabel(
            "下面是按照当前启动分辨率与“%s”方式处理后的首帧预览。\n"
            "实际写入动画载荷的画面会使用相同的缩放、裁切或拉伸逻辑。" % mode_name
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        buffer = io.BytesIO()
        image.convert("RGBA").save(buffer, format="PNG")
        pixmap = QPixmap()
        pixmap.loadFromData(buffer.getvalue(), "PNG")
        available = QApplication.primaryScreen().availableGeometry()
        max_width = max(420, min(980, int(available.width() * 0.76)))
        max_height = max(280, min(680, int(available.height() * 0.68)))
        shown = pixmap.scaled(max_width, max_height, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        preview = QLabel()
        preview.setAlignment(Qt.AlignCenter)
        preview.setPixmap(shown)
        preview.setMinimumSize(min(shown.width() + 18, max_width + 18), min(shown.height() + 18, max_height + 18))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(preview)
        layout.addWidget(scroll, 1)

        dimensions = QLabel("目标画面：%d × %d" % image.size)
        dimensions.setObjectName("MutedLabel")
        layout.addWidget(dimensions)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        close_button = buttons.button(QDialogButtonBox.Close)
        if close_button is not None:
            close_button.setText("关闭")
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
        self.resize(min(max_width + 50, available.width() - 40), min(max_height + 130, available.height() - 40))


class AnimationDialog(QDialog):
    WARNING_TEXT = (
        "启用后会在静图阶段结束后播放所选的全彩无声动画，播放结束后自动继续进入 Windows。\n\n"
        "支持 GIF、视频和 PNG 帧序列，可设置抽帧帧率、最大帧数与 QOI / JPEG 编码。"
    )

    def __init__(self, project, parent=None):
        super().__init__(parent)
        self.setWindowTitle("启动动画设置")
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self.setMinimumWidth(860)
        self.animation = copy.deepcopy(project.get("boot_animation", {}))
        self.target_resolution = tuple(int(value) for value in project.get("resolution", [1920, 1080]))
        self._source_native_fps = 0.0
        self._source_kind = ""
        self._last_estimate = None
        self._last_estimate_signature = None
        self._last_space_info = None
        self._estimate_thread = None
        self._estimate_worker = None
        self._estimate_running_signature = None
        self.capacity_mib = float(project.get("capacity_mib", 32.0) or 32.0)
        layout = QVBoxLayout(self)
        form_widget = QWidget(self)
        form = QFormLayout(form_widget)
        form.setSizeConstraint(QLayout.SetMinimumSize)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self._animation_form = form
        self.enabled = QCheckBox("启用 Linux 全彩启动动画")
        self.enabled.setChecked(bool(self.animation.get("enabled", False)))
        self._initial_enabled = self.enabled.isChecked()
        self.enabled.clicked.connect(self._enabled_clicked)
        form.addRow(self.enabled)

        source_row = QHBoxLayout()
        self.source = QLineEdit(str(self.animation.get("source", "")))
        self.source.setReadOnly(True)
        self.file_btn = QPushButton("选择动画文件")
        self.folder_btn = QPushButton("选择 PNG 文件夹")
        self.file_btn.clicked.connect(self._file)
        self.folder_btn.clicked.connect(self._folder)
        source_row.addWidget(self.source, 1)
        source_row.addWidget(self.file_btn)
        source_row.addWidget(self.folder_btn)
        form.addRow("动画来源选择", source_row)

        self.source_info = QLabel("资源原始帧率：尚未检测")
        self.source_info.setObjectName("MutedLabel")
        self.source_info.setWordWrap(True)
        self.source_info.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        form.addRow("来源信息", self.source_info)

        self.codec = ThemedComboBox()
        self.codec.addItem("QOI 无损高速（适合开屏 / ACG / Logo）", "qoi")
        self.codec.addItem("JPEG 高压缩（适合真人 / 影视 / 复杂纹理）", "jpeg")
        self.codec.setCurrentIndex(max(0, self.codec.findData(str(self.animation.get("codec", "jpeg") or "jpeg").lower())))
        self.codec.currentIndexChanged.connect(self._sync_dynamic_rows)
        codec_holder = QWidget(self)
        codec_row = QVBoxLayout(codec_holder)
        codec_row.setContentsMargins(0, 0, 0, 0)
        codec_row.setSpacing(4)
        codec_row.addWidget(self.codec)
        codec_note = QLabel("QOI 完全无损，适合纯底色的高清动画等，但制作耗时较长，空间占用大；\nJPEG 为有损高压缩模式，适合真人视频、影视片段等，制作又快又省空间！")
        codec_note.setObjectName("MutedLabel")
        codec_note.setWordWrap(True)
        codec_row.addWidget(codec_note)
        form.addRow("动画编码模式", codec_holder)


        self.jpeg_quality = ThemedSpinBox()
        self.jpeg_quality.setRange(60, 100)
        self.jpeg_quality.setValue(int(self.animation.get("jpeg_quality", 90)))
        self.jpeg_quality.valueChanged.connect(self._estimate_inputs_changed)
        self.quality_holder = QWidget(self)
        quality_row = QVBoxLayout(self.quality_holder)
        quality_row.setContentsMargins(0, 0, 0, 0)
        quality_row.setSpacing(4)
        self.jpeg_quality.setMinimumWidth(150)
        quality_row.addWidget(self.jpeg_quality, 0, Qt.AlignLeft)
        quality_hint = QLabel("只在 JPEG 模式下显示并生效。范围 60～100，默认 90。90+ 更接近原画；60～80 更省空间。质量越接近 100，体积通常会涨得越来越快，尤其 95～100 会明显占用更多空间；建议先看体积估算再调整。")
        quality_hint.setObjectName("MutedLabel")
        quality_hint.setWordWrap(True)
        quality_hint.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.MinimumExpanding)
        quality_row.addWidget(quality_hint)
        form.addRow("JPEG 质量", self.quality_holder)

        estimate_holder = QWidget(self)
        estimate_row = QVBoxLayout(estimate_holder)
        estimate_row.setContentsMargins(0, 0, 0, 0)
        estimate_row.setSpacing(5)
        self.estimate_btn = QPushButton("点我来估算编码体积")
        self.estimate_btn.clicked.connect(self._estimate_codecs)
        estimate_row.addWidget(self.estimate_btn, 0, Qt.AlignLeft)
        self.estimate_label = QLabel("尚未估算；会抽取最多 6 个代表帧估算 QOI Raw 与当前质量 JPEG Chunk32 的体积，并尽量结合本机 EFI / Legacy 启动分区可用空间给出建议。")
        self.estimate_label.setObjectName("MutedLabel")
        self.estimate_label.setWordWrap(True)
        self.estimate_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.MinimumExpanding)
        estimate_row.addWidget(self.estimate_label)
        form.addRow("编码体积估算", estimate_holder)

        self.extract_fps = ThemedDoubleSpinBox()
        self.extract_fps.setRange(0.1, 1000.0)
        self.extract_fps.setDecimals(3)
        self.extract_fps.setSingleStep(1.0)
        self.extract_fps.setValue(float(self.animation.get("extract_frame_rate", self.animation.get("frame_rate", 60))))
        self.extract_fps.valueChanged.connect(self._update_extraction_hint)
        self.extract_fps.valueChanged.connect(self._estimate_inputs_changed)
        self.max_frames = ThemedSpinBox()
        self.max_frames.setRange(1, 6000)
        self.max_frames.setValue(int(self.animation.get("max_frames", 600)))
        self.max_frames.valueChanged.connect(self._estimate_inputs_changed)
        self.fps = ThemedSpinBox()
        self.fps.setRange(1, 1000)
        self.fps.setValue(int(self.animation.get("frame_rate", 60)))
        self.loops = ThemedSpinBox()
        self.loops.setRange(1, 10)
        self.loops.setValue(int(self.animation.get("loops", 1)))
        self.fit = ThemedComboBox()
        self.fit.addItem("拉伸铺满", "stretch")
        self.fit.addItem("完整显示", "contain")
        self.fit.addItem("裁切铺满", "cover")
        self.fit.setCurrentIndex(max(0, self.fit.findData(self.animation.get("fit_mode", "stretch"))))
        self.fit.activated.connect(self._preview_fit_mode)
        self.fit.currentIndexChanged.connect(self._estimate_inputs_changed)

        def ranged_editor(editor, range_text):
            holder = QWidget(self)
            row = QVBoxLayout(holder)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(4)
            editor.setMinimumWidth(150)
            row.addWidget(editor, 0, Qt.AlignLeft)
            hint = QLabel(range_text)
            hint.setObjectName("MutedLabel")
            hint.setWordWrap(True)
            hint.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.MinimumExpanding)
            row.addWidget(hint)
            holder.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.MinimumExpanding)
            return holder

        self.extract_holder = QWidget(self)
        extract_row = QHBoxLayout(self.extract_holder)
        extract_row.setContentsMargins(0, 0, 0, 0)
        extract_row.setSpacing(8)
        self.extract_fps.setMinimumWidth(150)
        extract_row.addWidget(self.extract_fps)
        self.extract_hint = QLabel("")
        self.extract_hint.setObjectName("MutedLabel")
        self.extract_hint.setWordWrap(True)
        self.extract_hint.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        extract_row.addWidget(self.extract_hint, 1)
        form.addRow("抽帧帧率", self.extract_holder)

        max_holder = QWidget(self)
        max_outer = QVBoxLayout(max_holder)
        max_outer.setContentsMargins(0, 0, 0, 0)
        max_outer.setSpacing(4)
        max_top = QHBoxLayout()
        self.max_frames.setMinimumWidth(150)
        max_top.addWidget(self.max_frames)
        max_hint = QLabel("可用范围：1～6000 帧；达到数量上限时按下方所选方式决定取帧范围。")
        max_hint.setObjectName("MutedLabel")
        max_hint.setWordWrap(True)
        max_hint.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        max_top.addWidget(max_hint, 1)
        max_outer.addLayout(max_top)
        mode_row = QHBoxLayout()
        mode_row.setContentsMargins(0, 0, 0, 0)
        mode_row.addWidget(QLabel("抽帧方式："))
        self.sample_head = QRadioButton("从头连续抽帧")
        self.sample_uniform = QRadioButton("全片均匀抽帧（从头到尾间隔取帧）")
        mode = str(self.animation.get("sampling_mode", "head"))
        self.sample_uniform.setChecked(mode == "uniform")
        self.sample_head.setChecked(mode != "uniform")
        self.sample_head.toggled.connect(self._estimate_inputs_changed)
        self.sample_uniform.toggled.connect(self._estimate_inputs_changed)
        mode_row.addWidget(self.sample_head)
        mode_row.addWidget(self.sample_uniform)
        mode_row.addStretch(1)
        max_outer.addLayout(mode_row)
        form.addRow("最大抽帧数量", max_holder)

        primary_refresh = get_current_display_refresh_rate()
        if primary_refresh:
            refresh_label = ("%g" % primary_refresh)
            fps_hint = (
                "可用范围：1～1000 FPS；检测到当前 Windows 主屏幕约 %s Hz，建议最高播放帧率优先设为不高于约 %s FPS。"
                "设置得更高仍可用于测试本机动画解码/处理能达到多高帧率，但屏幕不会完整显示更多整帧，并可能出现撕裂或残影。"
                "播放器不主动跳过动画帧；实际帧率受动画来源与硬件性能影响，可能低于设定值。"
            ) % (refresh_label, refresh_label)
        else:
            fps_hint = (
                "可用范围：1～1000 FPS；未能可靠读取当前 Windows 主屏幕刷新率。设置较高数值仍可测试本机动画解码/处理上限，"
                "但屏幕不会完整显示更多整帧，并可能出现撕裂或残影。播放器不主动跳过动画帧；"
                "实际帧率受动画来源与硬件性能影响，可能低于设定值。"
            )
        form.addRow("最高播放帧率", ranged_editor(self.fps, fps_hint))
        form.addRow("循环播放次数", ranged_editor(self.loops, "可用范围：1～10 次"))
        fit_holder = QWidget(self)
        fit_layout = QVBoxLayout(fit_holder)
        fit_layout.setContentsMargins(0, 0, 0, 0)
        fit_layout.setSpacing(4)
        fit_layout.addWidget(self.fit)
        fit_hint = QLabel(
            "拉伸铺满：强制拉伸到目标分辨率，不留黑边；原素材比例不同时画面会被拉伸变形。\n"
            "完整显示：保持原素材比例并完整显示，不裁切；比例不同时四周可能出现黑边。\n"
            "裁切铺满：保持原素材比例放大到铺满，不变形、不留黑边；四周可能会被裁掉一部分。"
        )
        fit_hint.setObjectName("MutedLabel")
        fit_hint.setWordWrap(True)
        fit_hint.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.MinimumExpanding)
        fit_layout.addWidget(fit_hint)
        form.addRow("适配屏幕比例", fit_holder)
        form_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.MinimumExpanding)
        form_scroll = QScrollArea(self)
        form_scroll.setWidgetResizable(True)
        form_scroll.setFrameShape(QFrame.NoFrame)
        form_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        form_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        form_scroll.verticalScrollBar().setSingleStep(32)
        form_scroll.setWidget(form_widget)
        self._animation_form_widget = form_widget
        self._animation_scroll = form_scroll
        # Keep the dialog on the native Qt scrolling path.  In particular, do
        # not install per-editor wheel filters or custom scrollbar paint hooks.
        layout.addWidget(form_scroll, 1)
        available = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1040, max(900, int(available.width() * 0.72))), min(820, max(650, int(available.height() * 0.82))))
        self._sync_source_controls()
        self._refresh_source_metadata(reset_extraction=False)
        self._sync_dynamic_rows()
        buttons = _localized_dialog_buttons(self)
        self._dialog_buttons = buttons
        buttons.accepted.connect(self._accept_with_animation_reminder)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def reject(self):
        if self._estimate_worker is not None:
            self.estimate_label.setText("正在后台并行估算，请等待结果返回后再关闭设置窗口。")
            return
        super().reject()

    def closeEvent(self, event):
        if self._estimate_worker is not None:
            self.estimate_label.setText("正在后台并行估算，请等待结果返回后再关闭设置窗口。")
            event.ignore()
            return
        super().closeEvent(event)

    def _accept_with_animation_reminder(self):
        # Saving settings must stay instant.  The potentially expensive six-frame
        # codec/space estimate is user-triggered here and is repeated as a strict
        # preflight by MainWindow immediately before the real build.
        if self._animation_source_is_valid() and not self.enabled.isChecked():
            message = QMessageBox(self)
            message.setIcon(QMessageBox.Information)
            message.setWindowTitle("动画尚未启用")
            message.setText(
                "您已经选择了有效的动画文件或PNG帧文件夹，但尚未勾选“启用 Linux 全彩启动动画”。\n\n"
                "如果只是忘记启用，请选择“现在启用”；如果本次确实不需要动画，可以选择“保持关闭并确定”。"
            )
            enable = message.addButton("现在启用", QMessageBox.AcceptRole)
            keep_disabled = message.addButton("保持关闭并确定", QMessageBox.ActionRole)
            back = message.addButton("返回设置", QMessageBox.RejectRole)
            message.setDefaultButton(enable)
            message.exec_()
            clicked = message.clickedButton()
            if clicked is enable:
                if self._confirm_enable_animation(set_checked=True):
                    self.accept()
                return
            if clicked is keep_disabled:
                self.accept()
                return
            if clicked is back:
                return
            return
        self.accept()

    def _animation_source_is_valid(self):
        source = self.source.text().strip()
        return bool(source) and os.path.exists(source)

    def _set_enabled_checked(self, checked):
        self.enabled.blockSignals(True)
        try:
            self.enabled.setChecked(bool(checked))
        finally:
            self.enabled.blockSignals(False)
        self._sync_source_controls()

    def _confirm_enable_animation(self, set_checked=False):
        if not self._animation_source_is_valid():
            QMessageBox.warning(self, "无法启用动画", "请先选择有效的动画文件或PNG帧文件夹，再启用开机动画。")
            self._set_enabled_checked(False)
            return False
        message = QMessageBox(self)
        message.setIcon(QMessageBox.Information)
        message.setWindowTitle("确认启用开机动画")
        message.setText(self.WARNING_TEXT)
        cancel = message.addButton("取消", QMessageBox.RejectRole)
        confirm = message.addButton("确定开启", QMessageBox.AcceptRole)
        message.setDefaultButton(cancel)
        message.exec_()
        if message.clickedButton() is not confirm:
            self._set_enabled_checked(False)
            return False
        if set_checked or not self.enabled.isChecked():
            self._set_enabled_checked(True)
        else:
            self._sync_source_controls()
        return True

    def _enabled_clicked(self, checked):
        # clicked() fires for every real user click, while programmatic
        # setChecked() during initialization does not.  Cancelling therefore
        # genuinely unchecks the box and a second user attempt always asks again.
        if not checked:
            self._sync_source_controls()
            return
        self._confirm_enable_animation(set_checked=True)

    def _sync_source_controls(self):
        # Source selection remains available while animation is disabled so the
        # valid workflow is: choose a source first, then tick “启用动画”.
        self.source.setEnabled(True)
        self.file_btn.setEnabled(True)
        self.folder_btn.setEnabled(True)

    @staticmethod
    def _format_fps(value):
        value = float(value or 0.0)
        if value <= 0.0:
            return "未知"
        text = ("%.3f" % value).rstrip("0").rstrip(".")
        return "%s FPS" % text

    def _refresh_source_metadata(self, reset_extraction=False):
        source = self.source.text().strip()
        self._source_native_fps = 0.0
        self._source_kind = ""
        if not source or not os.path.exists(source):
            self.source_info.setText("资源原始帧率：尚未检测")
            self._update_extraction_hint()
            return
        try:
            meta = probe_animation_source(source)
        except Exception as exc:
            self.source_info.setText("来源信息读取失败：%s" % exc)
            self._update_extraction_hint()
            return
        fps = float(meta.get("fps", 0.0) or 0.0)
        self._source_native_fps = fps
        frames = int(meta.get("frame_count", 0) or 0)
        duration = float(meta.get("duration", 0.0) or 0.0)
        kind = str(meta.get("kind", ""))
        self._source_kind = kind
        if kind == "png_sequence":
            self.source_info.setText("PNG 帧文件夹：%d 帧；文件夹本身没有原始 FPS 信息。" % frames)
        elif kind == "image" and frames <= 1:
            self.source_info.setText("静态图像：1 帧；没有原始 FPS 信息。")
        else:
            pieces = ["资源原始帧率：%s" % self._format_fps(fps)]
            if frames > 0:
                pieces.append("约 %d 帧" % frames)
            if duration > 0.0:
                pieces.append("时长约 %.3f 秒" % duration)
            self.source_info.setText("；".join(pieces))
        if reset_extraction and fps > 0.0:
            self.extract_fps.setValue(max(self.extract_fps.minimum(), min(self.extract_fps.maximum(), fps)))
        self._update_extraction_hint()
        self._sync_dynamic_rows()

    def _update_extraction_hint(self, _value=None):
        current = float(self.extract_fps.value())
        native = float(self._source_native_fps or 0.0)
        base = "抽帧帧率只决定从动图/视频建立候选帧的频率，与下面的最高播放帧率互相独立。"
        if native > 0.0:
            if current > native + 0.01:
                warning = " 当前 %.3f FPS 已超过资源原始 %.3f FPS，会产生较多重复帧，增加体积并延长播放耗时，不建议这样设置。" % (current, native)
            else:
                warning = " 建议不要超过资源原始 %.3f FPS，否则会产生重复帧、增加体积并延长播放耗时。" % native
            self.extract_hint.setText(base + warning)
        else:
            self.extract_hint.setText(base + " 当前来源没有可读取的原始 FPS，PNG 帧文件夹不会按此值重复生成帧。")

    @staticmethod
    def _format_estimated_size(value):
        value = float(max(0, int(value)))
        if value < 1024.0 * 1024.0:
            return "%.1f KiB" % (value / 1024.0)
        if value < 1024.0 * 1024.0 * 1024.0:
            return "%.1f MiB" % (value / (1024.0 * 1024.0))
        return "%.2f GiB" % (value / (1024.0 * 1024.0 * 1024.0))

    def _set_form_row_visible(self, field, visible):
        field.setVisible(bool(visible))
        label = self._animation_form.labelForField(field)
        if label is not None:
            label.setVisible(bool(visible))

    def _sync_dynamic_rows(self, *_args):
        is_jpeg = str(self.codec.currentData() or "jpeg") == "jpeg"
        self._set_form_row_visible(self.quality_holder, is_jpeg)
        is_png_folder = self._source_kind == "png_sequence" or os.path.isdir(self.source.text().strip())
        self._set_form_row_visible(self.extract_holder, not is_png_folder)
        self._estimate_inputs_changed()

    def _estimate_inputs_changed(self, *_args):
        self._last_estimate_signature = None

    def _current_estimate_signature(self):
        return (self.source.text().strip(), str(self.codec.currentData() or "jpeg"),
                int(self.jpeg_quality.value()), int(self.max_frames.value()),
                round(float(self.extract_fps.value()), 4),
                "uniform" if self.sample_uniform.isChecked() else "head",
                str(self.fit.currentData() or "stretch"), tuple(self.target_resolution))

    def _estimated_total_boot_bytes(self, payload_bytes):
        # Ordinary EFI/kernel/theme files plus the encoded animation. No DAT
        # minimum capacity or filesystem padding is generated anymore.
        base = 34 * 1024 * 1024
        return int(payload_bytes) + base

    def _probe_boot_partition_space_for_estimate(self):
        """Read real boot-space information only after administrator approval.

        The estimate must know whether an existing AnimeBootNeo/AnimeBootNeo tree is
        already installed.  That tree can include a previous animation and is
        replaced by the next install, so its occupied bytes must be counted as
        replaceable space rather than treated as permanently unavailable.
        """
        if _is_process_elevated():
            space = probe_boot_partition_space()
        else:
            prepared = prepare_elevated_action({}, "probe_boot_space")
            launch_elevated_action("probe_boot_space", prepared["payload"], prepared["result_path"])
            deadline = time.time() + 120.0
            while not os.path.isfile(prepared["result_path"]):
                if time.time() >= deadline:
                    raise DeploymentError("等待管理员权限读取启动分区空间超时，请重新点击估算或直接以管理员身份运行程序。")
                QApplication.processEvents()
                time.sleep(0.05)
            space = read_action_result(prepared["result_path"], remove=True)
            if str(space.get("operation_id", "") or "") != str(prepared.get("operation_id", "") or ""):
                raise DeploymentError("管理员空间检测返回标识不一致，请重新估算。")
        if not space or not space.get("ok"):
            raise DeploymentError(_boot_space_probe_failure_message(space))
        return space

    def _calculate_codec_estimate(self):
        source = self.source.text().strip()
        if not source or not os.path.exists(source):
            raise AnimationError("请先选择有效的动画文件或PNG帧文件夹。")
        # UAC/boot-space probe comes first.  Do not spend time encoding samples
        # and do not present a partial estimate when existing installed payload
        # usage cannot be identified reliably.
        space = self._probe_boot_partition_space_for_estimate()
        result = estimate_rotation_sizes(
            source, self.target_resolution[0], self.target_resolution[1],
            str(self.fit.currentData() or "stretch"),
            int(self.max_frames.value()), float(self.extract_fps.value()),
            "uniform" if self.sample_uniform.isChecked() else "head",
            int(self.jpeg_quality.value()), sample_limit=6,
            rotation_sources=[item['source'] for item in self.animation.get('playlist',[])] if self.animation.get('rotation_enabled') else [],
        )
        result["qoi_total_boot_bytes"] = self._estimated_total_boot_bytes(result.get("qoi_bytes", 0))
        result["jpeg_total_boot_bytes"] = self._estimated_total_boot_bytes(result.get("jpeg_bytes", 0))
        self._last_space_info = space
        self._last_estimate = result
        self._last_estimate_signature = self._current_estimate_signature()
        return result, space

    def _selected_space_risk(self, result, space):
        if not space or not space.get("ok"):
            return False, 0, 0
        codec = str(self.codec.currentData() or "jpeg")
        needed = int(result.get("qoi_total_boot_bytes" if codec == "qoi" else "jpeg_total_boot_bytes", 0))
        available = int(space.get("replaceable_bytes", space.get("free_bytes", 0)) or 0)
        reserve = 8 * 1024 * 1024
        return needed + reserve > available, needed, available

    def _update_estimate_text(self, result, space):
        qoi = int(result.get("qoi_bytes", 0)); jpeg = int(result.get("jpeg_bytes", 0))
        planned = int(result.get("planned_frame_count", 0)); samples = int(result.get("sample_count", 0))
        quality = int(result.get("jpeg_quality", 90)); ratio = float(result.get("jpeg_xz_ratio", 1.0) or 1.0)
        lines = [
            "预计 %d 帧：QOI Raw 动画约 %s；JPEG(Q=%d，Chunk32) 约 %s。" % (planned, self._format_estimated_size(qoi), quality, self._format_estimated_size(jpeg)),
            "JPEG 估算已计入当前质量和 Chunk32 XZ 的抽样压缩效果（样本 %d 帧，二次压缩后约为 JPEG 帧流的 %.1f%%）；最终实际大小仍会因整段画面变化而有偏差。" % (samples, ratio * 100.0),
        ]
        if space and space.get("ok"):
            mode = {"uefi": "UEFI / EFI 系统分区", "bios": "传统 BIOS / Legacy 启动分区"}.get(str(space.get("boot_mode", "")), "启动分区")
            lines.append("管理员检测到 %s：总容量 %s，当前空闲 %s；已有 AnimeBoot Neo Debug 文件及回滚备份占用 %s，扣除需恢复的原启动文件后净可回收 %s，合计 %s 可用于本次替换。" % (
                mode, self._format_estimated_size(space.get("total_bytes", 0)), self._format_estimated_size(space.get("free_bytes", 0)),
                self._format_estimated_size(space.get("existing_animebootneo_bytes", 0)), self._format_estimated_size(space.get("reclaimable_bytes", 0)), self._format_estimated_size(space.get("replaceable_bytes", 0))))
            if space.get('reclaim_status')=='unregistered':
                lines.append('检测到专属目录但缺少有效安装记录；这些文件尚未计入可回收空间，需要先恢复本程序的安装/回滚记录。')
        else:
            lines.append("管理员空间检测没有返回可靠结果，本次编码体积估算不会作为有效空间结论；请重新授权后再估算。")
        if str(self.codec.currentData() or "jpeg") == "qoi":
            lines.append("自动建议：您已主动选择 QOI，保持 QOI；它完全无损且播放最快，但空间占用明显更大。")
        else:
            lines.append("自动建议：JPEG 高压缩。默认不再根据画面内容自动改成 QOI；需要完全无损时请由您主动选择 QOI。")
        risk, needed, available = self._selected_space_risk(result, space)
        if risk:
            if str(self.codec.currentData() or "jpeg") == "qoi":
                lines.append("⚠ 当前 QOI 预计总占用约 %s，启动分区大概率放不下。建议改用 JPEG，或减少最大抽帧数量。" % self._format_estimated_size(needed))
            else:
                lines.append("⚠ 当前 JPEG(Q=%d) 预计总占用约 %s，启动分区大概率放不下。建议降低 JPEG 质量或减少最大抽帧数量。" % (quality, self._format_estimated_size(needed)))
        self.estimate_label.setText("\n".join(lines))
        return risk

    def _estimate_codecs(self):
        if self._estimate_worker is not None:
            return
        source = self.source.text().strip()
        if not source or not os.path.exists(source):
            self.estimate_label.setText("估算失败：请先选择有效的动画文件或PNG帧文件夹。")
            return
        self._estimate_running_signature = self._current_estimate_signature()
        self.estimate_btn.setEnabled(False)
        if hasattr(self, "_dialog_buttons"):
            self._dialog_buttons.setEnabled(False)
        self.estimate_btn.setText("正在并行估算，请稍候…")
        self.estimate_label.setText(
            "正在后台请求管理员权限并读取实际启动分区空间；随后会并行抽取/编码最多6个代表帧。界面可以继续正常响应，请稍候……"
        )
        parameters = {
            "source": source,
            "width": int(self.target_resolution[0]),
            "height": int(self.target_resolution[1]),
            "fit_mode": str(self.fit.currentData() or "stretch"),
            "maximum": int(self.max_frames.value()),
            "extraction_fps": float(self.extract_fps.value()),
            "sampling_mode": "uniform" if self.sample_uniform.isChecked() else "head",
            "jpeg_quality": int(self.jpeg_quality.value()),
            "sample_limit": 6,
            "estimate_codecs": ("qoi", "jpeg"),
        }
        self._estimate_thread = QThread(self)
        self._estimate_worker = CodecEstimateWorker(parameters, self.capacity_mib)
        self._estimate_worker.moveToThread(self._estimate_thread)
        self._estimate_thread.started.connect(self._estimate_worker.run)
        self._estimate_worker.finished.connect(self._estimate_codecs_done)
        self._estimate_worker.failed.connect(self._estimate_codecs_failed)
        self._estimate_worker.finished.connect(self._estimate_thread.quit)
        self._estimate_worker.failed.connect(self._estimate_thread.quit)
        self._estimate_worker.finished.connect(self._estimate_worker.deleteLater)
        self._estimate_worker.failed.connect(self._estimate_worker.deleteLater)
        self._estimate_thread.finished.connect(self._estimate_thread_finished)
        self._estimate_thread.finished.connect(self._estimate_thread.deleteLater)
        self._estimate_thread.start()

    def _estimate_codecs_done(self, result, space):
        if self._estimate_running_signature != self._current_estimate_signature():
            self.estimate_label.setText("估算期间设置已发生变化；刚才的结果已丢弃，请按当前设置重新点击估算。")
            return
        self._last_space_info = dict(space or {})
        self._last_estimate = dict(result or {})
        self._last_estimate_signature = self._current_estimate_signature()
        risk = self._update_estimate_text(self._last_estimate, self._last_space_info)
        if risk:
            self._show_space_warning(self._last_estimate, self._last_space_info)

    def _estimate_codecs_failed(self, message):
        text = str(message or "未知错误")
        if text.startswith("固件启动模式无法识别：") or text.startswith("启动分区容量读取失败："):
            self.estimate_label.setText("估算未完成：%s" % text)
        else:
            self.estimate_label.setText("估算失败：%s" % text)

    def _estimate_thread_finished(self):
        thread = self.sender()
        if self._estimate_thread is thread:
            self._estimate_worker = None
            self._estimate_thread = None
        self._estimate_running_signature = None
        self.estimate_btn.setEnabled(True)
        if hasattr(self, "_dialog_buttons"):
            self._dialog_buttons.setEnabled(True)
        self.estimate_btn.setText("点我来估算编码体积")

    def _show_space_warning(self, result, space):
        risk, needed, available = self._selected_space_risk(result, space)
        if not risk:
            return
        codec = str(self.codec.currentData() or "jpeg")
        if codec == "qoi":
            text = ("按当前抽帧数量估算，QOI Raw 启动组件总占用约 %s，而当前启动分区可用于本次替换的空间约 %s，存在很高的放不下风险。\n\n"
                    "建议改用 JPEG 编码，或者减少最大抽帧数量后重新估算。") % (self._format_estimated_size(needed), self._format_estimated_size(available))
        else:
            text = ("按当前 JPEG 质量和抽帧数量估算，启动组件总占用约 %s，而当前启动分区可用于本次替换的空间约 %s，存在很高的放不下风险。\n\n"
                    "建议降低 JPEG 质量，或者减少最大抽帧数量后重新估算。") % (self._format_estimated_size(needed), self._format_estimated_size(available))
        QMessageBox.warning(self, "启动分区空间不足", text)

    def _space_preflight_before_accept(self):
        # Dialog acceptance must never synchronously encode representative
        # frames.  If the user already clicked the estimate button, reuse that
        # result; otherwise defer the strict check to MainWindow before build.
        if not self.enabled.isChecked() or not self._animation_source_is_valid():
            return True
        if self._last_estimate_signature != self._current_estimate_signature():
            return True
        result = self._last_estimate or {}
        space = self._last_space_info or {}
        risk, _needed, _available = self._selected_space_risk(result, space)
        if risk:
            self._show_space_warning(result, space)
            return False
        return True

    def _file(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择动画", "", "动画文件 (*.gif *.apng *.webp *.mp4 *.mkv *.mov *.avi *.webm *.m4v *.mpg *.mpeg);;所有文件 (*)")
        if path:
            self.source.setText(path)
            self._refresh_source_metadata(reset_extraction=True)

    def _folder(self):
        path = QFileDialog.getExistingDirectory(self, "选择编号 PNG 文件夹")
        if path:
            self.source.setText(path)
            self._refresh_source_metadata(reset_extraction=False)

    def _preview_fit_mode(self, _index):
        source = self.source.text().strip()
        if not source:
            QMessageBox.information(self, "首帧预览", "请先选择动画文件或PNG帧文件夹。")
            return
        mode = str(self.fit.currentData() or "cover")
        mode_name = self.fit.currentText()
        try:
            image = render_first_frame_preview(
                source,
                self.target_resolution[0],
                self.target_resolution[1],
                mode,
                self.extract_fps.value(),
                None,
            )
        except Exception as exc:
            QMessageBox.warning(self, "首帧预览失败", str(exc))
            return
        FirstFramePreviewDialog(image, mode_name, self).exec_()

    def apply(self, project):
        source = self.source.text().strip()
        enabled = self.enabled.isChecked() and self._animation_source_is_valid()
        previous=project.get('boot_animation',{})
        project["boot_animation"] = {
            "rotation_enabled":bool(previous.get('rotation_enabled',False)),
            "playlist":copy.deepcopy(previous.get('playlist',[])),
            "enabled": enabled,
            "source": source,
            "max_frames": self.max_frames.value(),
            "extract_frame_rate": float(self.extract_fps.value()),
            "sampling_mode": "uniform" if self.sample_uniform.isChecked() else "head",
            "frame_rate": self.fps.value(),
            "loops": self.loops.value(),
            "fit_mode": self.fit.currentData(),
            "codec": str(self.codec.currentData() or "jpeg"),
            "jpeg_quality": int(self.jpeg_quality.value()),
            "jpeg_xz_mode": "chunked32",
            "stream_xz_preset": 1,
        }




def _is_no_space_error(exc):
    """Recognize disk-full errors from Python, Windows, and wrapped tool text."""
    if isinstance(exc, OSError):
        if getattr(exc, "errno", None) == 28:
            return True
        if getattr(exc, "winerror", None) in (39, 112):
            return True
    text = str(exc or "").lower()
    return (
        "no space left on device" in text
        or "not enough space on the disk" in text
        or "there is not enough space on the disk" in text
        or "磁盘空间不足" in text
        or "设备上没有空间" in text
    )


def _format_free_space(path):
    try:
        probe = os.path.abspath(path)
        while probe and not os.path.exists(probe):
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            probe = parent
        usage = shutil.disk_usage(probe or os.getcwd())
        free_gib = usage.free / float(1024 ** 3)
        return "%.2f GiB" % free_gib
    except Exception:
        return "无法读取"


def _build_no_space_message(output_dir):
    temp_dir = tempfile.gettempdir()
    return (
        "制作所需磁盘空间不足，程序已停止。\n\n"
        "这不是 EFI 系统分区的前期检测：当前还在制作启动动画资源，尚未进入启动项安装阶段，"
        "因此本次错误没有修改 EFI 分区或 Windows 启动链。\n\n"
        "制作过程中会同时写入预览 PNG 帧、编码帧、initramfs 和普通启动文件；"
        "动画帧较多或分辨率较高时，临时占用可能明显大于实际安装的文件总量。\n\n"
        "请先释放程序所在磁盘和 Windows 临时目录的可用空间后重新制作；也可以减少动画帧数、"
        "降低动画分辨率或减少素材容量。\n"
        "构建目录所在磁盘当前剩余：%s\n"
        "Windows 临时目录所在磁盘当前剩余：%s\n\n"
        "只有在启动资源已制作完成、进入“安装启动项”阶段后另行提示 EFI/启动分区空间不足时，"
        "才需要自行扩容对应启动分区，或者进一步减少动画帧数、分辨率与素材容量后重新制作。"
        % (_format_free_space(output_dir), _format_free_space(temp_dir))
    )



def _estimate_total_boot_bytes(payload_bytes, capacity_mib):
    base = 34 * 1024 * 1024
    return int(payload_bytes or 0) + base


def _boot_space_probe_failure_message(space):
    """Explain *why* real boot-space capacity could not be read.

    The tool may still build the boot resource when capacity is unknown, but it must not
    write a boot chain when the firmware mode itself is still unknown.
    """
    data = dict(space or {})
    kind = str(data.get("error_kind", "") or "")
    mode = str(data.get("boot_mode", "") or "")
    error = str(data.get("error", "") or "").strip()
    if kind == "firmware_unknown" or mode not in ("uefi", "bios"):
        return (
            "固件启动模式无法识别：无法可靠判断本机当前使用 UEFI 还是传统 BIOS/Legacy，"
            "因此无法定位真正的启动分区并读取可用容量。"
            "仍可强行继续完成启动资源构建；但程序在无法确认启动方式时不会自动写入启动链，"
            "构建结果会保留，可在确认系统启动方式后继续安装。"
        )
    mode_name = "UEFI" if mode == "uefi" else "传统 BIOS/Legacy"
    detail = (" 原因：%s" % error) if error else ""
    return (
        "启动分区容量读取失败：已识别当前启动方式为 %s，但无法可靠定位或读取实际启动分区的可用容量。%s"
        "因此无法判断当前动画是否一定放得下；仍可强行继续制作。真正安装启动项时程序会再次尝试，"
        "若实际空间不足则按现有安全回滚逻辑保留构建结果，并可在处理空间问题后继续安装。"
    ) % (mode_name, detail)


def _probe_boot_partition_space_background():
    """Read boot-space information without blocking the Qt GUI thread."""
    if _is_process_elevated():
        space = probe_boot_partition_space()
    else:
        prepared = prepare_elevated_action({}, "probe_boot_space")
        launch_elevated_action("probe_boot_space", prepared["payload"], prepared["result_path"])
        deadline = time.time() + 120.0
        while not os.path.isfile(prepared["result_path"]):
            if time.time() >= deadline:
                raise DeploymentError("等待管理员权限读取启动分区空间超时，请重新估算或直接以管理员身份运行程序。")
            time.sleep(0.05)
        space = read_action_result(prepared["result_path"], remove=True)
        if str(space.get("operation_id", "") or "") != str(prepared.get("operation_id", "") or ""):
            raise DeploymentError("管理员空间检测返回标识不一致，请重新估算。")
    if not space or not space.get("ok"):
        raise DeploymentError(_boot_space_probe_failure_message(space))
    return space


class CodecEstimateWorker(QObject):
    """Background CPU/IO worker shared by manual estimate and build preflight."""
    finished = Signal(object, object)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, parameters, capacity_mib):
        super().__init__()
        self.parameters = dict(parameters or {})
        self.capacity_mib = float(capacity_mib or 32.0)

    def run(self):
        try:
            # Administrator space discovery may involve UAC and waiting for a
            # helper process.  Keep all of that outside the GUI thread.
            space = _probe_boot_partition_space_background()
            parameters=dict(self.parameters)
            project=parameters.pop('static_project',None)
            result = estimate_rotation_sizes(rotation_progress=self.progress.emit,**parameters)
            result["qoi_total_boot_bytes"] = _estimate_total_boot_bytes(result.get("qoi_bytes", 0), self.capacity_mib) if result.get("qoi_bytes") else 0
            result["jpeg_total_boot_bytes"] = _estimate_total_boot_bytes(result.get("jpeg_bytes", 0), self.capacity_mib) if result.get("jpeg_bytes") else 0
            if project is not None:
                self.progress.emit('正在计入全部静图及共享启动组件…')
                from .resources import default_theme_assets
                theme_bytes=sum(len(data) for data in render_canvases(project,default_theme_assets()).values())
                for codec in result.get('estimated_codecs',[]):
                    result[codec+'_total_boot_bytes']=estimated_boot_bytes(result,codec,project,theme_bytes,space.get('boot_mode','uefi'))
                result['static_theme_bytes']=theme_bytes
            self.finished.emit(result, space)
        except BaseException as exc:
            text = str(exc).strip() or "%s（异常未提供文字信息）" % type(exc).__name__
            self.failed.emit(text)


class ThemePackageWorker(QObject):
    finished=Signal(object)
    def __init__(self,task):
        super().__init__();self.task=task
    def run(self):
        try:self.finished.emit((True,self.task()))
        except Exception as exc:self.finished.emit((False,str(exc)))


class ThemePackageTaskDialog(QDialog):
    def __init__(self,title,task,parent):
        super().__init__(parent);self.setWindowTitle(title);self.result_data=None
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        layout=QVBoxLayout(self);layout.addWidget(QLabel('正在处理主题素材，请稍候…'))
        progress=QProgressBar();progress.setRange(0,0);layout.addWidget(progress)
        self.thread=QThread(self);self.worker=ThemePackageWorker(task)
        self.worker.moveToThread(self.thread);self.thread.started.connect(self.worker.run)
        self.worker.finished.connect(self._done);self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.accept)
        self.thread.start()
    def _done(self,data):self.result_data=data
    def reject(self):
        if not self.thread.isRunning():super().reject()
    def closeEvent(self,event):
        if self.thread.isRunning():event.ignore()
        else:super().closeEvent(event)


class BuildWorker(QObject):
    finished = Signal(dict)
    failed = Signal(str)
    cancelled = Signal()
    progress = Signal(str)

    def __init__(self, project, output_dir):
        super().__init__()
        self.project = copy.deepcopy(project)
        self.output_dir = output_dir
        self._cancel_event = threading.Event()

    def cancel(self):
        self._cancel_event.set()

    def is_cancelled(self):
        return self._cancel_event.is_set()

    def run(self):
        stage = "生成启动文件"
        try:
            payload_info = build_payload(
                self.project,
                self.output_dir,
                cancel_check=self.is_cancelled,
                progress=self.progress.emit,
            )
            if self.is_cancelled():
                raise BuildError("用户已放弃制作。")
            # 结果附带构建时的设置，避免用户切换/编辑画布后，安装校验或
            # 成功提示误读当前编辑器数据。副本仅驻留内存，不进入启动分区。
            self.finished.emit({"payload": payload_info, "project_snapshot": copy.deepcopy(self.project)})
        except BaseException as exc:
            if self.is_cancelled() or "用户已放弃制作" in str(exc):
                details = ""
            elif isinstance(exc, MemoryError):
                details = "32位程序的虚拟地址空间不足。这不代表电脑物理内存不足，单纯增加物理内存通常不能解决。建议优先改用 JPEG 高压缩；如果必须使用 QOI，请降低动画分辨率、最大抽帧数量或缩短动画时长后重试。若本次已经发生过一次虚拟地址空间不足，当前32位进程可能已经处于地址空间高水位或碎片化状态；此时即使已经降低参数仍再次失败，请先关闭并重新打开 AnimeBoot Neo Debug 后再制作，以重新获得干净的进程地址空间。"
            elif _is_no_space_error(exc):
                # Capture free-space figures before partial build cleanup frees
                # space, otherwise the diagnostic can misleadingly look healthy.
                details = _build_no_space_message(self.output_dir)
            else:
                details = str(exc).strip()
                if not details:
                    details = "%s（异常未提供文字信息）" % type(exc).__name__

            for name in ("InstallPayload", "AnimeBootNeo", "提取的每帧PNG预览图片"):
                partial = os.path.join(self.output_dir, name)
                try:
                    if os.path.isdir(partial):
                        shutil.rmtree(partial)
                    elif os.path.exists(partial):
                        os.remove(partial)
                except Exception:
                    pass
            if self.is_cancelled() or "用户已放弃制作" in str(exc):
                self.cancelled.emit()
                return
            self.failed.emit("%s失败：%s" % (stage, details))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        if not QApplication.windowIcon().isNull():
            self.setWindowIcon(QApplication.windowIcon())
        self.setWindowTitle("AnimeBoot Neo Debug (%s) 作者:原心 免费工具 开源测试 供后续并入新架构 谨慎使用 严禁倒卖" % APP_VERSION)
        available = QApplication.primaryScreen().availableGeometry()
        target_width = min(1440, max(1040, int(available.width() * 0.94)))
        target_height = min(900, max(680, int(available.height() * 0.90)))
        self.resize(target_width, target_height)
        self.setMinimumSize(920, 600)
        self.setObjectName("MainWindow")
        self.current_theme = THEME_LIQUID
        sw = QApplication.primaryScreen().availableGeometry().width()
        sh = QApplication.primaryScreen().availableGeometry().height()
        current_w, current_h = get_current_display_resolution((sw, sh))
        self.project = default_project(current_w, current_h)
        self.original_files = None
        self.selected_key = "circle"
        self.output_dir = ""
        self.build_result = None
        self.pending_install_manifest = None
        self._build_thread = None
        self._build_worker = None
        self._build_preflight_thread = None
        self._build_preflight_worker = None
        self._forced_space_warning = None
        self._deploy_timer = QTimer(self)
        self._deploy_timer.timeout.connect(self._poll_deploy)
        self._deploy_context = None
        self._frame_watch_timer = QTimer(self)
        self._frame_watch_timer.setInterval(250)
        self._frame_watch_timer.timeout.connect(self._poll_animation_frame_dir)
        self._frame_folder_opened = False
        self._last_nonzero_timeout = max(1, int(self.project.get("timeout", 3)))
        self._make_ui()
        self._load_default_assets()
        self._restore_pending_install_manifest()
        self._load_controls()
        self.set_theme(self.current_theme)
        QTimer.singleShot(0, self._refresh_panel_toggle_visuals)
        QTimer.singleShot(0,self._reflow_top_bar)
        QTimer.singleShot(50, lambda: self.canvas.refresh(keep_view=False))
        QTimer.singleShot(0, _font_catalog_loader().start)

    def _make_ui(self):
        root = QWidget(); root.setObjectName("Root")
        stack = QStackedLayout(root)
        stack.setContentsMargins(0, 0, 0, 0)
        stack.setStackingMode(QStackedLayout.StackAll)
        self.backdrop = ThemeBackdrop(root)
        content = QWidget(root); content.setObjectName("ContentRoot")
        content.setAttribute(Qt.WA_TranslucentBackground, True)
        stack.addWidget(self.backdrop)
        stack.addWidget(content)
        root_layout = QVBoxLayout(content); root_layout.setContentsMargins(8, 8, 8, 6); root_layout.setSpacing(7)
        self.setCentralWidget(root)
        self.backdrop.lower()
        content.raise_()

        top = QFrame(); top.setObjectName("TopGlass")
        self.top_bar=top
        self.top_bar_grid=QGridLayout(top);self.top_bar_grid.setContentsMargins(12,7,12,7)
        self.top_bar_grid.setHorizontalSpacing(8);self.top_bar_grid.setVerticalSpacing(5)
        self.top_brand=QWidget(top)
        brand = QVBoxLayout(self.top_brand); brand.setContentsMargins(0,0,0,0);brand.setSpacing(0)
        title = QLabel("AnimeBoot Neo Debug (%s)" % APP_VERSION)
        title.setFont(QFont(QApplication.font().family(), 13, QFont.Bold))
        subtitle = QLabel("Qt 可视化启动界面制作工具 (%s)" % APP_VERSION)
        subtitle.setObjectName("MutedLabel")
        subtitle.setWordWrap(True)
        subtitle.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        subtitle.setMinimumWidth(QFontMetrics(subtitle.font()).horizontalAdvance(subtitle.text()) + 6)
        brand.addWidget(title); brand.addWidget(subtitle)
        self.top_actions=QWidget(top)
        top_layout=QHBoxLayout(self.top_actions);top_layout.setContentsMargins(0,0,0,0);top_layout.setSpacing(6)
        self.top_bar_grid.addWidget(self.top_brand,0,0)
        self.top_bar_grid.addWidget(self.top_actions,0,1)
        self._top_bar_wrapped=False

        self.new_btn = self._button("重建DIY画布", QStyle.SP_FileIcon, self.new_project)
        self.open_btn = self._button("载入本地工程", QStyle.SP_DialogOpenButton, self.open_project)
        self.save_btn = self._button("保存当前工程", QStyle.SP_DialogSaveButton, self.save_project)
        self.theme_package_btn=QPushButton('分享包')
        self.theme_package_btn.setIcon(_icon(self,'share'));self.theme_package_btn.setIconSize(QSize(18,18))
        self.theme_package_btn.setMinimumWidth(max(130,QFontMetrics(self.theme_package_btn.font()).horizontalAdvance('分享包')+84))
        self.theme_package_btn.setSizePolicy(QSizePolicy.Minimum,QSizePolicy.Fixed)
        theme_menu=QMenu(self.theme_package_btn)
        theme_menu.addAction('导入分享包',self.import_theme_package)
        theme_menu.addAction('导出分享包',self.export_theme_package)
        self.theme_package_btn.setMenu(theme_menu)
        for button, width in ((self.new_btn, 142), (self.open_btn, 136), (self.save_btn, 136)):
            button.setMinimumWidth(max(button.minimumWidth(), width))
        top_layout.addWidget(self.new_btn); top_layout.addWidget(self.open_btn); top_layout.addWidget(self.save_btn)
        top_layout.addWidget(self.theme_package_btn)
        top_layout.addStretch(1)

        theme_label = QLabel("主题")
        theme_label.setObjectName("MutedLabel")
        self.theme_selector = ThemedComboBox()
        self.theme_selector.setObjectName("ThemeSelector")
        for label_text, theme_id in THEME_OPTIONS:
            self.theme_selector.addItem(label_text, theme_id)
        theme_width=max(QFontMetrics(self.theme_selector.font()).horizontalAdvance(text) for text,_ in THEME_OPTIONS)+48
        self.theme_selector.setFixedWidth(max(128,theme_width))
        theme_index = self.theme_selector.findData(self.current_theme)
        self.theme_selector.setCurrentIndex(max(0, theme_index))
        self.theme_selector.currentIndexChanged.connect(self._theme_changed)
        top_layout.addWidget(theme_label)
        top_layout.addWidget(self.theme_selector)

        about_button = QToolButton()
        about_button.setIcon(_icon(self, QStyle.SP_MessageBoxInformation))
        about_button.setToolTip("关于")
        about_button.clicked.connect(self.show_about)
        top_layout.addWidget(about_button)

        self.left_toggle = self._button("制作面板", QStyle.SP_FileDialogDetailedView, self.toggle_left)
        self.right_toggle = self._button("图层面板", QStyle.SP_FileDialogListView, self.toggle_right)
        for panel_toggle in (self.left_toggle, self.right_toggle):
            panel_toggle.setObjectName("PanelToggle")
            panel_toggle.setCheckable(True)
            panel_toggle.setChecked(True)
            panel_toggle.setToolTip("点击显示或收起对应侧边栏")
        top_layout.addWidget(self.left_toggle)
        top_layout.addWidget(self.right_toggle)
        root_layout.addWidget(top)

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.left_panel = self._make_left_panel()
        self.center_panel = self._make_center_panel()
        self.right_panel = self._make_right_panel()
        self.splitter.addWidget(self.left_panel); self.splitter.addWidget(self.center_panel); self.splitter.addWidget(self.right_panel)
        self.splitter.setStretchFactor(0, 0); self.splitter.setStretchFactor(1, 1); self.splitter.setStretchFactor(2, 0)
        self.splitter.setSizes([self.left_panel.minimumWidth() + 10, 698, 250])
        root_layout.addWidget(self.splitter, 1)
        for glass_panel in (top, self.left_panel, self.center_panel, self.right_panel):
            self.backdrop.register_panel(glass_panel)

        self.status_label = QLabel("就绪")
        self.statusBar().addWidget(self.status_label, 1)
        self.progress = QProgressBar(); self.progress.setRange(0, 100); self.progress.setValue(0); self.progress.setFixedWidth(180); self.progress.hide()
        self.statusBar().addPermanentWidget(self.progress)
        self._task_busy = False
        self._busy_progress_repaint_timer = QTimer(self)
        self._busy_progress_repaint_timer.setInterval(45)
        self._busy_progress_repaint_timer.timeout.connect(self._refresh_busy_progress_animation)

        save_shortcut = QShortcut(QKeySequence(QKeySequence.Save), self)
        save_shortcut.activated.connect(self.save_project)
        open_shortcut = QShortcut(QKeySequence(QKeySequence.Open), self)
        open_shortcut.activated.connect(self.open_project)

    def _button(self, text, standard, slot):
        button = QPushButton(text)
        button.setIcon(_icon(self, standard))
        button.setIconSize(QtCore.QSize(18, 18))
        metrics = QFontMetrics(button.font())
        required = metrics.horizontalAdvance(str(text)) + (26 if not button.icon().isNull() else 0) + 24
        button.setMinimumWidth(max(54, required))
        button.setSizePolicy(QSizePolicy.MinimumExpanding, QSizePolicy.Fixed)
        button.clicked.connect(slot)
        return button

    def _card_scroll(self):
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget(); body.setMinimumWidth(0)
        body.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout = QVBoxLayout(body); layout.setContentsMargins(6, 6, 6, 6); layout.setSpacing(8); layout.addStretch(1)
        scroll.setWidget(body)
        return scroll, body, layout

    def _make_left_panel(self):
        panel = QFrame(); panel.setObjectName("Panel")
        # Measure the longest form label using the active font. Qt's logical
        # geometry already accounts for the screen's DPI scaling.
        label_width = QFontMetrics(panel.font()).horizontalAdvance("倒计时(0-180秒)") + 6
        required_width = label_width + 170 + 80
        panel.setMinimumWidth(max(375, required_width))
        panel.setMaximumWidth(max(480, panel.minimumWidth() + 96))
        layout = QVBoxLayout(panel); layout.setContentsMargins(7, 7, 7, 7)
        header = QHBoxLayout(); label = QLabel("制作设置"); label.setFont(QFont(QApplication.font().family(), 11, QFont.DemiBold)); hint = QLabel("按步骤完成"); hint.setObjectName("MutedLabel")
        header.addWidget(label); header.addStretch(); header.addWidget(hint); layout.addLayout(header)
        tabs = QTabWidget(); tabs.tabBar().setExpanding(True); tabs.tabBar().setUsesScrollButtons(False); layout.addWidget(tabs, 1)
        self.workflow_tabs = tabs

        basic_scroll, _, basic = self._card_scroll(); basic.takeAt(basic.count()-1)
        display = QGroupBox("设置静图启动画面"); form = QFormLayout(display)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        form.setHorizontalSpacing(8)
        form.setContentsMargins(10, 14, 10, 10)
        self.resolution = ThemedComboBox(); self.resolution.setEditable(True); self.resolution.setMinimumContentsLength(10); self.resolution.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLength); self.resolution.addItems(["%dx%d" % r for r in get_display_resolutions()]); self.resolution.currentTextChanged.connect(self.apply_basic)
        if self.resolution.lineEdit() is not None:
            self.resolution.lineEdit().setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            self.resolution.lineEdit().setTextMargins(5, 0, 2, 0)
            # The application-wide input padding otherwise applies again to
            # this embedded editor, cutting off the last resolution digits.
            self.resolution.lineEdit().setStyleSheet("padding:0px; border:0px; background:transparent;")
        self.timeout = ThemedSpinBox(); self.timeout.setRange(0, 180); self.timeout.setSuffix(" 秒"); self.timeout.valueChanged.connect(self._timeout_changed)
        self.countdown = ThemedComboBox()
        for mode in COUNTDOWN_MODES:
            self.countdown.addItem(countdown_display_name(mode), mode)
        self.countdown.setToolTip("数字倒计时使用 GRUB 内置固定 16px 字体，不能自定义字号；可以在画布上调整位置和居中区域。所有可见倒计时样式都会随剩余秒数更新。")
        self.countdown.currentIndexChanged.connect(self._countdown_changed)
        for field in (self.resolution, self.timeout, self.countdown):
            field.setMinimumWidth(170)
            field.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        for text, field in (("屏幕分辨率", self.resolution), ("倒计时(0-180秒)", self.timeout), ("倒计时样式", self.countdown)):
            label = QLabel(text)
            label.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Preferred)
            form.addRow(label, field)
        basic.addWidget(display)

        beautify = QGroupBox("主体内容设计"); grid = QGridLayout(beautify)
        grid.addWidget(self._button("换主 Logo", "logo", self.replace_logo), 0, 0)
        grid.addWidget(self._button("换角色造型", "company", self.replace_company), 0, 1)
        grid.addWidget(self._button("添加图片", "image", self.add_image), 1, 0)
        grid.addWidget(self._button("添加文字", "text", self.add_text), 1, 1)
        png_hint = QLabel("图片仅支持选择 PNG 格式，PNG 格式支持透明图层。请将非 PNG 格式的图片自行转换到 PNG 格式再添加。")
        png_hint.setWordWrap(True); png_hint.setObjectName("MutedLabel")
        grid.addWidget(png_hint, 2, 0, 1, 2)
        basic.addWidget(beautify)
        basic.addStretch(1)
        basic_next = self._button("下一步：设置启动动画", QStyle.SP_ArrowForward, lambda: self.workflow_tabs.setCurrentIndex(1))
        basic_next.setObjectName("PrimaryButton")
        basic.addWidget(basic_next)
        tabs.addTab(basic_scroll, "静图")

        anim_scroll, _, anim = self._card_scroll(); anim.takeAt(anim.count()-1)
        anim_box = QGroupBox("是否要在静图结束后播放无声视频动画？"); anim_l = QVBoxLayout(anim_box)
        anim_l.setContentsMargins(10, 12, 10, 10); anim_l.setSpacing(7)
        anim_intro = QLabel("开启后会由签名 Linux 内核路径播放全彩无声动画，播放结束后自动继续进入系统。")
        anim_intro.setWordWrap(True)
        anim_intro.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        anim_l.addWidget(anim_intro)
        self.anim_btn = self._button("去添加并启用动画", QStyle.SP_MediaPlay, self.configure_animation)
        self.anim_btn.setMinimumWidth(0)
        self.anim_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.anim_btn.setToolTip("选择 GIF、视频或 PNG 序列")
        anim_l.addWidget(self.anim_btn)
        self.rotation_btn=self._button('设置多动画顺序轮换',QStyle.SP_MediaPlay,self.configure_rotation)
        anim_l.addWidget(self.rotation_btn)
        format_hint = QLabel("支持 GIF / 视频 / PNG 序列")
        format_hint.setWordWrap(True); format_hint.setObjectName("MutedLabel")
        anim_l.addWidget(format_hint)
        self.animation_status = QLabel("当前未启用动画"); self.animation_status.setWordWrap(True); self.animation_status.setObjectName("MutedLabel")
        anim_l.addWidget(self.animation_status)
        animation_advantage = QLabel(
            "当前动图功能已采用 QOI 无损高速 / JPEG 高压缩双编码，并会根据本机 CPU 自动使用多线程解码与 JPEG 分块并行解压；"
            "配合精简动画 Linux 环境、快速首帧与稳定完整帧调度，可明显缩短动画加载准备时间，并尽量发挥不同机器的播放性能。"
        )
        animation_advantage.setWordWrap(True)
        animation_advantage.setObjectName("MutedLabel")
        animation_advantage.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        anim_l.addSpacing(8)
        anim_l.addWidget(animation_advantage)
        anim.addWidget(anim_box); anim.addStretch(1)
        anim_next = self._button("下一步：开始制作", QStyle.SP_ArrowForward, lambda: self.workflow_tabs.setCurrentIndex(2))
        anim_next.setObjectName("PrimaryButton")
        anim.addWidget(anim_next)
        tabs.addTab(anim_scroll, "动图")

        deploy_scroll, _, deploy = self._card_scroll(); deploy.takeAt(deploy.count()-1)
        parameters = QGroupBox("① 可定制参数（建议保持默认不动）"); parameter_form = QVBoxLayout(parameters)
        parameter_form.setContentsMargins(10, 14, 10, 10)
        parameter_form.setSpacing(9)
        self.fix_gfxterm_blank = QCheckBox()
        self.fix_gfxterm_blank.setChecked(True)
        self.fix_gfxterm_blank.setToolTip('在最外层 grub.cfg 开头写入空输出 echo ""，避免部分 Fedora GRUB 新版 UEFI 设备进入 gfxterm 后黑屏。')
        self.fix_gfxterm_blank.stateChanged.connect(self.apply_basic)
        fix_row = QWidget()
        fix_layout = QHBoxLayout(fix_row)
        fix_layout.setContentsMargins(0, 0, 0, 0)
        fix_layout.setSpacing(5)
        fix_link = QLabel('修复某些情况下静图<a href="https://savannah.gnu.org/bugs/?65503#comment2">无法显示的问题</a>')
        fix_link.setTextFormat(Qt.RichText)
        fix_link.setWordWrap(True)
        fix_link.setTextInteractionFlags(Qt.TextBrowserInteraction)
        fix_link.setOpenExternalLinks(True)
        fix_link.setToolTip("点击查看 GNU GRUB Bug #65503 的相关讨论与解决办法")
        fix_layout.addWidget(self.fix_gfxterm_blank, 0, Qt.AlignVCenter)
        fix_layout.addWidget(fix_link, 1, Qt.AlignVCenter)
        parameter_form.addWidget(fix_row)
        compat_row = QWidget()
        compat_layout = QVBoxLayout(compat_row)
        compat_layout.setContentsMargins(0, 0, 0, 0)
        compat_layout.setSpacing(4)
        compat_hint = QLabel("默认与备用 GRUB 均为 UEFI/Legacy 配对版本。显示异常时可尝试备用版。")
        compat_hint.setWordWrap(True)
        compat_choices = QHBoxLayout()
        compat_choices.setContentsMargins(0, 0, 0, 0)
        compat_choices.setSpacing(16)
        self.grub_compat_new = QRadioButton("默认 GRUB")
        self.grub_compat_old = QRadioButton("备用 GRUB")
        self.grub_compat_group = QButtonGroup(self)
        self.grub_compat_group.setExclusive(True)
        self.grub_compat_group.addButton(self.grub_compat_new)
        self.grub_compat_group.addButton(self.grub_compat_old)
        self.grub_compat_new.setChecked(True)
        self.grub_compat_new.setToolTip("Fedora GRUB 2.12-64.fc44，默认选择；shim 16.1-7 保持一致。")
        self.grub_compat_old.setToolTip("Fedora GRUB 2.12-56.fc44，UEFI 与 Legacy 同时使用配对版本；shim 保持一致。备用版可能被固件 dbx 或 shim SBAT 撤销策略拒绝。")
        self.grub_compat_new.toggled.connect(self._grub_compat_toggled)
        self.grub_compat_old.toggled.connect(self._grub_compat_toggled)
        compat_choices.addWidget(self.grub_compat_new)
        compat_choices.addWidget(self.grub_compat_old)
        compat_choices.addStretch(1)
        compat_layout.addLayout(compat_choices)
        compat_hint.setObjectName("GrubCompatibilityHint")
        compat_layout.addWidget(compat_hint)
        parameter_form.addWidget(compat_row)
        self.show_debug_info = QCheckBox("显示调试信息")
        self.show_debug_info.setChecked(True)
        self.show_debug_info.setToolTip("仅在启用动图时生效：动画播放结束后显示两页实测诊断信息，每页 8 秒。取消勾选后不显示这两页。静图及 Linux 启动过程不显示正常诊断日志；GRUB4DOS 菜单、A20 调试和 GRUB 欢迎文字固定隐藏。固件自身提示及签名验证等故障提示不由此开关控制。当前制作/安装信息始终独立显示。")
        self.show_debug_info.toggled.connect(self._debug_info_changed)
        parameter_form.addWidget(self.show_debug_info)
        debug_hint=QLabel("仅在启用动图时，播放结束后显示两页诊断信息，每页 8 秒；取消后不显示。")
        debug_hint.setWordWrap(True)
        debug_hint.setObjectName("DebugPlaybackHint")
        parameter_form.addWidget(debug_hint)
        self.static_windows_transition_row = QWidget()
        transition_layout = QVBoxLayout(self.static_windows_transition_row)
        transition_layout.setContentsMargins(0, 0, 0, 0)
        transition_layout.setSpacing(4)
        transition_heading = QLabel("静图进入系统的过渡方式：")
        transition_heading.setObjectName("StaticWindowsTransitionTitle")
        transition_layout.addWidget(transition_heading)
        transition_choices = QHBoxLayout()
        transition_choices.setContentsMargins(0, 0, 0, 0)
        transition_choices.setSpacing(16)
        self.static_windows_blend = QRadioButton("无感融入")
        self.static_windows_black = QRadioButton("清屏过渡")
        self.static_windows_transition_group = QButtonGroup(self)
        self.static_windows_transition_group.setExclusive(True)
        for button in (self.static_windows_blend, self.static_windows_black):
            self.static_windows_transition_group.addButton(button)
            transition_choices.addWidget(button)
            button.toggled.connect(self._static_windows_transition_changed)
        self.static_windows_blend.setToolTip("未启用动图时，不显示倒计时控件可保持静图背景交接 Windows；选择任何可见倒计时样式时会先短暂黑闪，再恢复静图背景。Windows 接管后的显示由系统控制。")
        self.static_windows_black.setToolTip("倒计时结束后先清为黑屏，再引导 Windows。")
        self.static_windows_blend.setChecked(True)
        transition_choices.addStretch(1)
        transition_layout.addLayout(transition_choices)
        self.static_windows_transition_hint = QLabel("仅在未启用动图时生效；开启动图后使用动画交接方式。")
        self.static_windows_transition_hint.setWordWrap(True)
        self.static_windows_transition_hint.setObjectName("StaticWindowsTransitionHint")
        transition_layout.addWidget(self.static_windows_transition_hint)
        parameter_form.addWidget(self.static_windows_transition_row)
        deploy.addWidget(parameters)
        self.debug_output = BuildLogEdit()
        self.debug_output.setReadOnly(True)
        self.debug_output.setMaximumBlockCount(2000)
        self.debug_output.setMaximumHeight(120)
        build_box = QGroupBox("② 开始制作吧："); build_l = QVBoxLayout(build_box)
        self.build_action_btn = self._button("开始制作", QStyle.SP_DialogApplyButton, self.start_build); self.build_action_btn.setObjectName("PrimaryButton")
        build_l.addWidget(self.build_action_btn)
        information_title = QLabel("当前制作/安装信息(不是开机日志回传)：")
        information_title.setObjectName("BuildInfoTitle")
        information_title.setWordWrap(True)
        build_l.addWidget(information_title)
        build_l.addWidget(self.debug_output)
        self.continue_install_btn = self._button("制作后期遇到问题点我继续", QStyle.SP_DialogApplyButton, self.continue_pending_install); self.continue_install_btn.setEnabled(False)
        self.continue_install_btn.setToolTip("制作已经完成但设备加密、UAC取消或安装阶段临时失败时，可直接继续后期安装；不会重新编码动画。")
        build_l.addWidget(self.continue_install_btn)
        deploy.addWidget(build_box)

        final_box = QGroupBox("③ 自动安装完成后可再次验证：")
        final_l = QVBoxLayout(final_box)
        final_notice = QLabel("制作完成后程序会自动完成启动项安装和最终资源部署；下面的按钮只用于需要时再次执行完整校验。")
        final_notice.setWordWrap(True)
        final_notice.setObjectName("MutedLabel")
        self.final_btn = self._button("(可选)点我来验证制作好坏", QStyle.SP_DialogApplyButton, self.deploy_final); self.final_btn.setEnabled(False)
        help_btn = self._button("帮助与注意事项", QStyle.SP_MessageBoxInformation, self.show_build_help)
        self.direct_fps_btn = self._button("我要直接修改当前动图最高播放帧率", QStyle.SP_ComputerIcon, self.direct_modify_runtime_fps)
        self.direct_fps_btn.setToolTip("无需重新制作动画：直接寻找本机已安装的 AnimeBoot Neo Debug 动画 CFG，并修改运行时 FPS 覆盖参数。")
        final_l.addWidget(final_notice)
        final_l.addWidget(self.final_btn)
        final_l.addWidget(help_btn)
        final_l.addWidget(self.direct_fps_btn)
        deploy.addWidget(final_box)
        deploy.addStretch(1)
        tabs.addTab(deploy_scroll, "开始制作！")

        disable_scroll, _, disable_layout = self._card_scroll(); disable_layout.takeAt(disable_layout.count()-1)
        disable_layout.addStretch(1)
        self.disable_all_animations_btn = self._button(
            "点我禁用本机所有开机动画",
            QStyle.SP_DialogCancelButton,
            self.disable_boot_animations,
        )
        self.disable_all_animations_btn.setObjectName("PrimaryButton")
        self.disable_all_animations_btn.setMinimumHeight(48)
        self.disable_all_animations_btn.setToolTip(
            "恢复安装前的 Windows 启动链并移除 AnimeBoot Neo Debug 的启动资源。"
        )
        disable_layout.addWidget(self.disable_all_animations_btn)
        disable_layout.addStretch(1)
        tabs.addTab(disable_scroll, "我要禁用")
        return panel

    def _make_center_panel(self):
        panel = QFrame(); panel.setObjectName("CenterPanel")
        layout = QVBoxLayout(panel); layout.setContentsMargins(7, 7, 7, 7); layout.setSpacing(5)
        header = QHBoxLayout(); title = QLabel("画布预览"); title.setFont(QFont(QApplication.font().family(), 11, QFont.DemiBold)); subtitle = QLabel("右键对象以查看更多调整选项"); subtitle.setObjectName("MutedLabel")
        header.addWidget(title); header.addWidget(subtitle); header.addStretch()
        play = self._button("预览", QStyle.SP_MediaPlay, self.preview_countdown)
        self.guides_button = QToolButton(); self.guides_button.setIcon(_icon(self, "grid")); self.guides_button.setToolTip("显示四分线"); self.guides_button.setCheckable(True); self.guides_button.setChecked(True); self.guides_button.setObjectName("PanelToggle")
        self.guides_button.toggled.connect(self._toggle_guides)
        zoom_out = QToolButton(); zoom_out.setIcon(_icon(self, "zoom_out")); zoom_out.setToolTip("缩小"); zoom_out.clicked.connect(lambda: self.canvas.zoom_out())
        zoom_in = QToolButton(); zoom_in.setIcon(_icon(self, "zoom_in")); zoom_in.setToolTip("放大"); zoom_in.clicked.connect(lambda: self.canvas.zoom_in())
        fit = self._button("适应窗口", QStyle.SP_TitleBarMaxButton, lambda: self.canvas.fit_canvas())
        header.addWidget(play); header.addWidget(self.guides_button); header.addWidget(zoom_out); header.addWidget(zoom_in); header.addWidget(fit); layout.addLayout(header)
        self.canvas_manager=CanvasManagerBar(panel)
        self.canvas_selector=self.canvas_manager.selector
        self.canvas_selector.currentIndexChanged.connect(self._canvas_changed)
        self.canvas_manager.add_button.clicked.connect(lambda:self._add_canvas(False))
        self.canvas_manager.copy_button.clicked.connect(lambda:self._add_canvas(True))
        self.canvas_manager.delete_button.clicked.connect(self._delete_canvas)
        layout.addWidget(self.canvas_manager)
        self.canvas = CanvasView(lambda: self.project, lambda: self.original_files)
        self.canvas.set_guides_visible(True)
        self.canvas.selectionChanged.connect(self.select_key)
        self.canvas.geometryChanged.connect(self.refresh_all)
        self.canvas.editRequested.connect(self._edit_key)
        self.canvas.copyRequested.connect(self._copy_key)
        self.canvas.deleteRequested.connect(self._delete_key)
        self.canvas.layerMoveRequested.connect(self._move_key)
        self.canvas.alignmentRequested.connect(self._align_key)
        layout.addWidget(self.canvas, 1)
        return panel

    def _refresh_canvas_manager(self):
        sync_canvas(self.project)
        self.canvas_selector.blockSignals(True)
        self.canvas_selector.clear()
        for index,scene in enumerate(self.project['canvases']):
            self.canvas_selector.addItem(scene['name'],index)
        self.canvas_selector.setCurrentIndex(self.project['active_canvas'])
        self.canvas_selector.blockSignals(False)
        managing=not self._task_busy
        self.canvas_manager.add_button.setEnabled(managing and len(self.project['canvases'])<MAX_CANVASES)
        self.canvas_manager.copy_button.setEnabled(managing and len(self.project['canvases'])<MAX_CANVASES)
        self.canvas_manager.delete_button.setEnabled(managing and len(self.project['canvases'])>1)

    def _canvas_changed(self,index):
        if index<0 or index==self.project.get('active_canvas',0):return
        # 构建使用独立快照；切换的是编辑画布，不改变正在输出的内容。
        select_canvas(self.project,index)
        self.selected_key=''
        self._load_controls();self.canvas.refresh(keep_view=True)
        self.status_label.setText('正在编辑 '+self.project['canvases'][index]['name']+
            ('；本次制作继续使用开始时的设置' if self._task_busy else '；多个画布按次顺序轮换'))

    def _add_canvas(self,duplicate=False):
        if self._task_busy:return
        sync_canvas(self.project)
        if len(self.project['canvases'])>=MAX_CANVASES:return
        existing={scene['name'] for scene in self.project['canvases']}
        name=next('画布 '+chr(65+n) for n in range(MAX_CANVASES) if '画布 '+chr(65+n) not in existing)
        content=capture_canvas(self.project if duplicate else default_project(*self.project['resolution']))
        self.project['canvases'].append({'name':name,'canvas':content})
        select_canvas(self.project,len(self.project['canvases'])-1)
        self.selected_key='';self._load_controls();self.canvas.refresh(keep_view=True)

    def _delete_canvas(self):
        if self._task_busy or len(self.project['canvases'])<2:return
        index=self.project['active_canvas'];name=self.project['canvases'][index]['name']
        if QMessageBox.question(self,'删除画布','确认删除 %s？其他画布保留。'%name)!=QMessageBox.Yes:return
        self.project['canvases'].pop(index)
        index=min(index,len(self.project['canvases'])-1)
        self.project['active_canvas']=index
        self.project.update(copy.deepcopy(self.project['canvases'][index]['canvas']))
        self.selected_key='';self._load_controls();self.canvas.refresh(keep_view=True)

    def _make_right_panel(self):
        panel = QFrame(); panel.setObjectName("Panel"); panel.setMinimumWidth(235); panel.setMaximumWidth(360)
        layout = QVBoxLayout(panel); layout.setContentsMargins(7, 7, 7, 7); layout.setSpacing(6)
        header = QHBoxLayout(); title = QLabel("画面对象"); title.setFont(QFont(QApplication.font().family(), 11, QFont.DemiBold)); hint = QLabel("拖拽可快捷调整顺序"); hint.setObjectName("MutedLabel"); header.addWidget(title); header.addWidget(hint); header.addStretch(); layout.addLayout(header)
        add = QHBoxLayout(); add.addWidget(self._button("添加图片", "image", self.add_image)); add.addWidget(self._button("添加文字", "text", self.add_text)); layout.addLayout(add)
        self.layers = LayerListWidget(); self.layers.itemSelectionChanged.connect(self.layer_selected); self.layers.itemChanged.connect(self.layer_visibility_changed); self.layers.leftDoubleClicked.connect(self._edit_key); self.layers.orderChanged.connect(self.layer_order_changed); self.layers.contextMenuForItem.connect(self._show_layer_context_menu); layout.addWidget(self.layers, 1)
        tools = QGridLayout()
        tools.addWidget(self._button("属性", QStyle.SP_FileDialogDetailedView, self.edit_selected), 0, 0)
        tools.addWidget(self._button("复制", QStyle.SP_FileDialogNewFolder, self.copy_selected), 0, 1)
        tools.addWidget(self._button("删除", QStyle.SP_TrashIcon, self.delete_selected), 0, 2)
        tools.addWidget(self._button("置顶", "top", lambda: self.move_layer("top")), 1, 0)
        tools.addWidget(self._button("上移", QStyle.SP_ArrowUp, lambda: self.move_layer("up")), 1, 1)
        tools.addWidget(self._button("下移", QStyle.SP_ArrowDown, lambda: self.move_layer("down")), 1, 2)
        layout.addLayout(tools)
        return panel

    def _load_controls(self):
        self._refresh_canvas_manager()
        self._countdown_preview_generation = getattr(self, "_countdown_preview_generation", 0) + 1
        for countdown_family in ("circle", "bar"):
            if isinstance(self.project.get(countdown_family), dict):
                self.project[countdown_family]["visible"] = True
        self.show_debug_info.blockSignals(True)
        self.show_debug_info.setChecked(bool(self.project.get("show_debug_info", True)))
        self.show_debug_info.blockSignals(False)

        controls = (self.resolution, self.timeout, self.countdown, self.fix_gfxterm_blank,
                    self.grub_compat_new, self.grub_compat_old,
                    self.static_windows_blend, self.static_windows_black)
        for control in controls:
            control.blockSignals(True)
        self.resolution.setCurrentText("%dx%d" % tuple(self.project["resolution"]))
        timeout_value = int(self.project.get("timeout", 3))
        self.timeout.setValue(timeout_value)
        if timeout_value > 0:
            self._last_nonzero_timeout = timeout_value
        self.countdown.setCurrentIndex(max(0, self.countdown.findData(self.project.get("countdown_mode", "none"))))
        self.fix_gfxterm_blank.setChecked(bool(self.project.get("fix_gfxterm_blank", True)))
        grub_compat_mode = str(self.project.get("x64_grub_compat_mode", "new") or "new").lower()
        self.grub_compat_old.setChecked(grub_compat_mode == "old")
        self.grub_compat_new.setChecked(grub_compat_mode != "old")
        transition = self.project.get("static_windows_transition", "blend")
        self.static_windows_black.setChecked(transition == "black")
        self.static_windows_blend.setChecked(transition != "black")
        for control in controls:
            control.blockSignals(False)
        animation = self.project.get("boot_animation", {})
        self._sync_static_windows_transition()
        if animation.get("enabled"):
            codec_name = "JPEG Q=%d" % int(animation.get("jpeg_quality", 90)) if str(animation.get("codec", "jpeg")) == "jpeg" else "QOI 无损"
            status_text = "已启用：%s；%s；抽帧 %.3f FPS；播放上限 %d FPS；最大 %d 帧（%s）" % (
                os.path.basename(animation.get("source", "")),
                codec_name,
                float(animation.get("extract_frame_rate", animation.get("frame_rate", 60))),
                int(animation.get("frame_rate", 60)),
                int(animation.get("max_frames", 600)),
                "全片均匀" if animation.get("sampling_mode") == "uniform" else "从头连续",
            )
            self.animation_status.setText(status_text)
            if animation.get('rotation_enabled'):
                self.animation_status.setText(status_text+'\n顺序轮换：共%d段（共享当前制作参数）'%len(animation_sources(self.project)))
            if hasattr(self, "anim_btn"):
                self.anim_btn.setText("已启用动画（点我修改）")
        else:
            self.animation_status.setText("当前未启用动画")
            if hasattr(self, "anim_btn"):
                self.anim_btn.setText("去添加并启用动画")
        if animation.get('rotation_enabled') and animation.get('enabled'):
            self.rotation_btn.setText('已启用顺序轮换（%d段，点我修改）' % len(animation_sources(self.project)))
        elif animation.get('rotation_enabled'):
            self.rotation_btn.setText('已设置顺序轮换（请先启用主动画）')
        else:
            self.rotation_btn.setText('设置多动画顺序轮换')
        self.refresh_layers()

    def _debug_info_changed(self, checked):
        self.project["show_debug_info"] = bool(checked)

    def _static_windows_transition_changed(self, checked):
        if checked:
            self.project["static_windows_transition"] = "black" if self.static_windows_black.isChecked() else "blend"

    def _sync_static_windows_transition(self):
        # Animation owns the boot handoff. Disable the static-only preference
        # without overwriting it, so turning animation off restores the choice.
        animation_enabled = bool(self.project.get("boot_animation", {}).get("enabled"))
        self.static_windows_transition_row.setEnabled(not animation_enabled)


    def _debug_log(self, text):
        if hasattr(self, "debug_output"):
            self.debug_output.append_log(time.strftime("%H:%M:%S ") + str(text))

    def _countdown_changed(self, _index):
        mode = self.countdown.currentData()
        previous = self.project.get("countdown_mode", "none")
        if countdown_key(mode) and mode != previous:
            message = QMessageBox(self)
            message.setIcon(QMessageBox.Question)
            message.setWindowTitle("确认显示伪动态倒计时动画")
            message.setText(
                "确认要显示伪动态倒计时动画吗？请先了解它带来的额外代价。\n\n"
                "如果未启用动图动画，且进入系统的过渡方式选择“无感融入”，选择任何可见倒计时样式都会使静图倒计时结束时出现一次短暂的黑屏闪烁，随后恢复静图背景并继续启动 Windows。\n\n"
                "多画布轮换时，只有全部画布都选择“不显示”才会使用无黑闪交接；任一画布显示倒计时，整套工程都会使用普通倒计时交接。\n\n"
                "这是显示倒计时动画对“无感融入”的唯一额外代价。“不显示”仍会按设定秒数等待，并保持背景连续过渡。开启动图或选择“清屏过渡”时，原有交接方式不受影响。\n\n"
                "是否仍要继续？"
            )
            confirm = message.addButton("仍要继续", QMessageBox.AcceptRole)
            cancel = message.addButton("取消", QMessageBox.RejectRole)
            message.setDefaultButton(cancel)
            message.exec_()
            if message.clickedButton() is not confirm:
                self.countdown.blockSignals(True)
                self.countdown.setCurrentIndex(max(0, self.countdown.findData(previous)))
                self.countdown.blockSignals(False)
                return
        self.apply_basic()

    def _timeout_changed(self, value):
        value = int(value)
        if value == 0:
            message = QMessageBox(self)
            message.setIcon(QMessageBox.Question)
            message.setWindowTitle("确认关闭静图显示")
            message.setText(
                "将启动倒计时设为0秒，开机时将不显示静图画面。若启用了动图，将直接播放动图，再启动 Windows；也代表您将无法使用 Esc 跳过本次动图动画。\n\n"
                "这个功能主要供只想显示动图的用户使用。若未启用动图，将直接启动 Windows。是否确认设为0秒？"
            )
            confirm = message.addButton("确认设为0秒", QMessageBox.AcceptRole)
            cancel = message.addButton("取消", QMessageBox.RejectRole)
            message.setDefaultButton(cancel)
            message.exec_()
            if message.clickedButton() is not confirm:
                self.timeout.blockSignals(True)
                self.timeout.setValue(max(1, self._last_nonzero_timeout))
                self.timeout.blockSignals(False)
                return
        else:
            self._last_nonzero_timeout = value
        self.apply_basic()

    def _toggle_guides(self, checked):
        if hasattr(self, "canvas"):
            self.canvas.set_guides_visible(bool(checked))
        if hasattr(self, "guides_button"):
            self.guides_button.setToolTip("隐藏四分线" if checked else "显示四分线")

    def _grub_compat_toggled(self, _checked=False):
        # Changing only the GRUB compatibility resource does not affect canvas
        # geometry or the preview. Avoid apply_basic()/canvas.refresh(), which
        # made the radio button feel frozen for a moment on large projects.
        if self.grub_compat_old.isChecked():
            self.project["x64_grub_compat_mode"] = "old"
        elif self.grub_compat_new.isChecked():
            self.project["x64_grub_compat_mode"] = "new"

    def apply_basic(self):
        text = self.resolution.currentText().lower().replace("×", "x")
        try:
            w, h = [int(v.strip()) for v in text.split("x", 1)]
            if 320 <= w <= 8192 and 200 <= h <= 8192:
                old_w, old_h = self.project["resolution"]
                if [w, h] != [old_w, old_h]:
                    resize_canvases(self.project,w,h)
        except Exception:
            return
        self.project["timeout"] = self.timeout.value()
        self.project["countdown_mode"] = self.countdown.currentData()
        for countdown_family in ("circle", "bar"):
            if isinstance(self.project.get(countdown_family), dict):
                self.project[countdown_family]["visible"] = True
        if self.project["countdown_mode"] == "number":
            spec = self.project["bar"]
            spec["w"] = max(24, int(spec.get("w", 24)))
            spec["h"] = max(16, int(spec.get("h", 16)))
            spec["x"] = max(0, min(int(spec.get("x", 0)), self.project["resolution"][0] - spec["w"]))
            spec["y"] = max(0, min(int(spec.get("y", 0)), self.project["resolution"][1] - spec["h"]))
        elif self.project["countdown_mode"] == "bar_texture":
            spec = self.project["bar"]
            spec["w"] = max(4, int(spec.get("w", 4)))
            spec["h"] = max(4, int(spec.get("h", 4)))
        self.project["fix_gfxterm_blank"] = self.fix_gfxterm_blank.isChecked()
        self.project["x64_grub_compat_mode"] = "old" if self.grub_compat_old.isChecked() else "new"
        self.project["static_windows_transition"] = "black" if self.static_windows_black.isChecked() else "blend"
        self._sync_static_windows_transition()
        family = countdown_key(self.project["countdown_mode"])
        if family:
            self.selected_key = family
        elif self.selected_key in ("circle", "bar"):
            order = self.project.get("layer_order", [])
            self.selected_key = order[-1] if order else ""
        self.refresh_all()

    def _program_directory(self):
        if getattr(sys, "frozen", False):
            return os.path.dirname(os.path.abspath(sys.executable))
        return os.path.dirname(os.path.abspath(sys.argv[0]))

    def _load_default_assets(self):
        from .resources import default_theme_assets
        try:
            self.original_files = default_theme_assets()
            return True
        except Exception as exc:
            self.original_files = None
            QMessageBox.critical(self, "内置图片无法读取", str(exc))
            return False



    def _pending_manifest_file(self, output_dir=None):
        root = os.path.abspath(output_dir or self.output_dir or "") if (output_dir or self.output_dir) else ""
        return os.path.join(root, "AnimeBootNeo_待安装.json") if root else ""

    def _remember_pending_install_manifest(self, manifest, retry_allowed=True):
        self.pending_install_manifest = dict(manifest or {})
        self._pending_retry_allowed = bool(retry_allowed)
        if hasattr(self, "continue_install_btn"):
            self.continue_install_btn.setEnabled(bool(retry_allowed))

    def _persist_pending_install_manifest(self, manifest, retry_allowed=True):
        manifest = dict(manifest or {})
        if not self._pending_manifest_valid(manifest):
            return False
        output_dir = str(manifest.get("asset_root", "") or "")
        path = self._pending_manifest_file(output_dir)
        if not path:
            return False
        record = {"version": 2, "app_version": APP_VERSION, "app_edition": APP_EDITION,
                  "manifest": manifest, "retry_allowed": bool(retry_allowed), "created_at": time.time()}
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
        _write_pending_registry_path(path)
        self._remember_pending_install_manifest(manifest, retry_allowed)
        return True

    def _keep_pending_after_install_problem(self, manifest=None, retry_allowed=True):
        """Persist only after a completed build encounters an install problem."""
        try:
            manifest = dict(manifest or self._base_manifest())
            return self._persist_pending_install_manifest(manifest, retry_allowed)
        except Exception as exc:
            # Retry in this session remains available if persistence is denied.
            if manifest and self._pending_manifest_valid(manifest):
                self._remember_pending_install_manifest(manifest, retry_allowed)
            self._debug_log("无法保存下次继续安装的状态，本次构建文件仍保留：%s" % exc)
            return False

    def _pending_manifest_valid(self, manifest):
        manifest = dict(manifest or {})
        if manifest.get("app_version") != APP_VERSION:
            return False
        payload = str(manifest.get("payload_dir", "") or "")
        asset_root = str(manifest.get("asset_root", "") or "")
        if not payload or not asset_root or not os.path.isdir(asset_root):
            return False
        if not os.path.isfile(os.path.join(payload, "manifest.json")):
            return False
        try:
            with open(os.path.join(payload, "manifest.json"), "r", encoding="utf-8") as stream:
                report = json.load(stream)
            return report.get("app_version") == APP_VERSION and report.get("format") == "AnimeBootNeo-payload-3-canvases"
        except (OSError, ValueError, AttributeError):
            return False

    def _restore_pending_install_manifest(self):
        path = _read_pending_registry_path()
        if not path or not os.path.isfile(path):
            if path:
                _clear_pending_registry_path()
            return False
        try:
            with open(path, "r", encoding="utf-8") as stream:
                record = json.load(stream)
            if not isinstance(record, dict) or record.get("version") != 2 or record.get("app_version") != APP_VERSION or record.get("app_edition") != APP_EDITION:
                raise ValueError("待安装状态不属于当前程序版本")
            manifest = dict(record.get("manifest", {}) or {})
            if not self._pending_manifest_valid(manifest):
                raise ValueError("待安装构建产物已不存在")
            if os.path.normcase(os.path.abspath(path)) != os.path.normcase(self._pending_manifest_file(manifest["asset_root"])):
                raise ValueError("待安装状态路径无效")
            retry_allowed = bool(record.get("retry_allowed", True))
            self._remember_pending_install_manifest(manifest, retry_allowed)
            self.output_dir = str(manifest.get("asset_root", "") or "")
            self.final_btn.setEnabled(True)
            self.status_label.setText("发现上次安装未完成的构建结果，可直接继续安装" if retry_allowed else "上次安装回滚未完全完成，请先检查启动分区；构建文件仍保留")
            return True
        except Exception:
            _clear_pending_registry_path()
            return False

    def _clear_pending_install_manifest(self):
        path = _read_pending_registry_path()
        _clear_pending_registry_path()
        self.pending_install_manifest = None
        self._pending_retry_allowed = False
        if hasattr(self, "continue_install_btn"):
            self.continue_install_btn.setEnabled(False)
        if path and os.path.basename(path) == "AnimeBootNeo_待安装.json" and os.path.isfile(path):
            try:
                os.remove(path)
            except Exception:
                pass

    def _choose_png(self, title):
        path, _ = QFileDialog.getOpenFileName(self, title, "", "PNG 图片 (*.png)")
        return path

    def replace_logo(self):
        path = self._choose_png("选择主 Logo")
        if path:
            spec = self.project.setdefault("logo", {})
            spec["path"] = path
            spec["visible"] = True
            spec["removed"] = False
            if "logo" not in self.project.get("layer_order", []):
                self.project.setdefault("layer_order", []).append("logo")
            self.selected_key = "logo"
            self.refresh_all()

    def replace_company(self):
        path = self._choose_png("选择角色造型")
        if path:
            spec = self.project.setdefault("company", {})
            spec["path"] = path
            spec["visible"] = True
            spec["removed"] = False
            if "company" not in self.project.get("layer_order", []):
                self.project.setdefault("layer_order", []).append("company")
            self.selected_key = "company"
            self.refresh_all()

    def add_image(self):
        path = self._choose_png("添加 PNG 图层")
        if not path:
            return
        w, h = self.project["resolution"]
        try:
            with Image.open(path) as image:
                ratio = image.width / float(max(1, image.height))
        except Exception:
            ratio = 1.0
        width = max(80, int(w * 0.28)); height = max(60, int(width / ratio))
        item = {
            "id": new_object_id("img"), "path": path, "source_builtin": "", "display_name": "",
            "x": int((w-width)/2), "y": int((h-height)/2), "w": width, "h": height,
            "visible": True, "contain": False, "lock_aspect": False, "locked": False,
        }
        self.project["extra_images"].append(item)
        key = "extra:" + item["id"]
        self.project["layer_order"].append(key)
        self.selected_key = key
        self.refresh_all()

    def add_text(self):
        dialog = MultiLineTextDialog(
            "添加文字",
            "文字内容",
            "双击或打开属性继续编辑（示例文字）",
            self,
        )
        if dialog.exec_() != QDialog.Accepted:
            return
        text = dialog.value()
        w, h = self.project["resolution"]
        item = {
            "id": new_object_id("txt"), "text": text,
            "x": int(w*0.25), "y": int(h*0.42), "w": int(w*0.5), "h": max(70, int(h*0.12)),
            "font_path": "", "font_size": max(24, int(h*0.04)), "color": "#FFFFFF",
            "align": "center", "visible": True, "lock_aspect": False, "locked": False,
        }
        self.project["text_boxes"].append(item)
        key = "text:" + item["id"]
        self.project["layer_order"].append(key)
        self.selected_key = key
        self.refresh_all()

    def configure_animation(self):
        dialog = AnimationDialog(self.project, self)
        if dialog.exec_() == QDialog.Accepted:
            dialog.apply(self.project)
            self._load_controls()

    def refresh_layers(self):
        self.layers.blockSignals(True)
        self.layers.clear()
        family = countdown_key(self.project.get("countdown_mode", "none"))
        keys = list(reversed(self.project.get("layer_order", [])))
        if family:
            keys.insert(0, family)
        if self.selected_key not in keys:
            self.selected_key = keys[0] if keys else ""
        for key in keys:
            spec = _spec_for_key(self.project, key) or {}
            is_countdown = key in ("circle", "bar")
            if is_countdown:
                spec["visible"] = True
            item = QListWidgetItem(_object_name(self.project, key))
            item.setData(Qt.UserRole, key)
            if is_countdown:
                flags = item.flags() | Qt.ItemIsSelectable | Qt.ItemIsEnabled
                flags &= ~Qt.ItemIsUserCheckable
                flags &= ~Qt.ItemIsDragEnabled
                flags &= ~Qt.ItemIsDropEnabled
                item.setFlags(flags)
            else:
                flags = item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsDragEnabled
                flags &= ~Qt.ItemIsDropEnabled
                item.setFlags(flags)
                item.setCheckState(Qt.Checked if spec.get("visible", True) else Qt.Unchecked)
            self.layers.addItem(item)
            if key == self.selected_key:
                item.setSelected(True)
                self.layers.setCurrentItem(item)
        self.layers.blockSignals(False)
        self.canvas.set_selected(self.selected_key)

    def layer_order_changed(self, requested_order=None):
        if isinstance(requested_order, (list, tuple)):
            top_to_bottom = [
                str(key) for key in requested_order
                if str(key) and str(key) not in ("circle", "bar")
            ]
        else:
            top_to_bottom = []
            for row in range(self.layers.count()):
                item = self.layers.item(row)
                if item is None:
                    continue
                key = str(item.data(Qt.UserRole) or "")
                if key and key not in ("circle", "bar"):
                    top_to_bottom.append(key)
        self.project["layer_order"] = list(reversed(top_to_bottom))
        normalize_layer_order(self.project)
        # Rebuild only after the native drag transaction has ended.  This keeps
        # the QListWidget model and project model synchronized and prevents the
        # target row from being removed by Qt's delayed move cleanup.
        self.refresh_layers()
        self.canvas.refresh(draft=False)
        self.canvas.set_selected(self.selected_key)

    def layer_selected(self):
        items = self.layers.selectedItems()
        if items:
            self.select_key(items[0].data(Qt.UserRole))

    def layer_visibility_changed(self, item):
        key = item.data(Qt.UserRole)
        spec = _spec_for_key(self.project, key)
        if spec is None:
            return
        if key in ("circle", "bar"):
            spec["visible"] = True
            return
        spec["visible"] = item.checkState() == Qt.Checked
        self.canvas.refresh(draft=False)

    def select_key(self, key):
        self.selected_key = key or ""
        self.canvas.set_selected(self.selected_key)
        self.layers.blockSignals(True)
        for i in range(self.layers.count()):
            item = self.layers.item(i)
            item.setSelected(item.data(Qt.UserRole) == self.selected_key)
        self.layers.blockSignals(False)

    def _edit_key(self, key):
        self.select_key(key)
        self.edit_selected()

    def _copy_key(self, key):
        self.select_key(key)
        self.copy_selected()

    def _delete_key(self, key):
        self.select_key(key)
        self.delete_selected()

    def _move_key(self, key, action):
        self.select_key(key)
        self.move_layer(action)

    def _align_key(self, key, action):
        self.select_key(key)
        spec = _spec_for_key(self.project, key)
        if not spec or spec.get("locked", False):
            return
        canvas_w, canvas_h = [int(v) for v in self.project["resolution"]]
        if action == "horizontal":
            spec["x"] = max(0, int(round((canvas_w - int(spec.get("w", 1))) / 2.0)))
        elif action == "vertical":
            spec["y"] = max(0, int(round((canvas_h - int(spec.get("h", 1))) / 2.0)))
        elif action == "fullscreen":
            spec["x"], spec["y"], spec["w"], spec["h"] = 0, 0, canvas_w, canvas_h
        self.refresh_all()

    def _show_layer_context_menu(self, key, global_pos):
        if not key:
            return
        self.select_key(key)
        menu = QMenu(self)
        properties = menu.addAction("属性")
        copy_action = menu.addAction("复制")
        copy_action.setEnabled(key not in ("circle", "bar"))
        delete_action = menu.addAction("删除")
        menu.addSeparator()
        horizontal_action = menu.addAction("水平居中")
        vertical_action = menu.addAction("垂直居中")
        fullscreen_action = menu.addAction("一键全屏居中")
        menu.addSeparator()
        top_action = menu.addAction("置顶")
        up_action = menu.addAction("上移一层")
        down_action = menu.addAction("下移一层")
        is_countdown = key in ("circle", "bar")
        top_action.setEnabled(not is_countdown)
        up_action.setEnabled(not is_countdown)
        down_action.setEnabled(not is_countdown)
        chosen = menu.exec_(global_pos)
        if chosen is properties:
            self.edit_selected()
        elif chosen is copy_action:
            self.copy_selected()
        elif chosen is delete_action:
            self.delete_selected()
        elif chosen is horizontal_action:
            self._align_key(key, "horizontal")
        elif chosen is vertical_action:
            self._align_key(key, "vertical")
        elif chosen is fullscreen_action:
            self._align_key(key, "fullscreen")
        elif chosen is top_action:
            self.move_layer("top")
        elif chosen is up_action:
            self.move_layer("up")
        elif chosen is down_action:
            self.move_layer("down")

    def edit_selected(self):
        if not self.selected_key:
            return
        dialog = ObjectPropertiesDialog(self.project, self.selected_key, self)
        if dialog.exec_() == QDialog.Accepted:
            dialog.apply()
            self.refresh_all()

    def _offset_duplicate(self, duplicate):
        canvas_w, canvas_h = [int(v) for v in self.project["resolution"]]
        offset = max(12, int(round(min(canvas_w, canvas_h) * 0.022)))
        width = int(duplicate.get("w", 1)); height = int(duplicate.get("h", 1))
        duplicate["x"] = max(0, min(canvas_w - width, int(duplicate.get("x", 0)) + offset))
        duplicate["y"] = max(0, min(canvas_h - height, int(duplicate.get("y", 0)) + offset))

    def copy_selected(self):
        key = self.selected_key
        spec = _spec_for_key(self.project, key)
        if not spec or key in ("circle", "bar"):
            return
        if key.startswith("text:"):
            duplicate = copy.deepcopy(spec)
            duplicate["id"] = new_object_id("txt")
            duplicate["locked"] = False
            self._offset_duplicate(duplicate)
            self.project["text_boxes"].append(duplicate)
            new_key = "text:" + duplicate["id"]
        elif key.startswith("extra:"):
            duplicate = copy.deepcopy(spec)
            duplicate["id"] = new_object_id("img")
            duplicate["locked"] = False
            self._offset_duplicate(duplicate)
            self.project["extra_images"].append(duplicate)
            new_key = "extra:" + duplicate["id"]
        elif key in ("logo", "company"):
            duplicate = copy.deepcopy(spec)
            duplicate["id"] = new_object_id("img")
            duplicate["locked"] = False
            duplicate["removed"] = False
            if duplicate.get("path"):
                duplicate["source_builtin"] = ""
            else:
                duplicate["source_builtin"] = key
            duplicate["display_name"] = "主 Logo 副本" if key == "logo" else "小logo副本"
            duplicate.setdefault("contain", True)
            self._offset_duplicate(duplicate)
            self.project["extra_images"].append(duplicate)
            new_key = "extra:" + duplicate["id"]
        elif key == "prompt":
            duplicate = {
                "id": new_object_id("txt"),
                "text": spec.get("text", "按Esc键立刻跳过本次所有开机动画"),
                "x": int(spec.get("x", 0)), "y": int(spec.get("y", 0)),
                "w": int(spec.get("w", 1)), "h": int(spec.get("h", 1)),
                "font_path": spec.get("font_path", ""), "font_size": int(spec.get("font_size", 28)),
                "color": spec.get("color", "#FFFFFF"), "align": "center", "visible": True,
                "lock_aspect": False, "locked": False,
            }
            self._offset_duplicate(duplicate)
            self.project["text_boxes"].append(duplicate)
            new_key = "text:" + duplicate["id"]
        else:
            return
        self.project["layer_order"].append(new_key)
        self.selected_key = new_key
        self.refresh_all()

    def delete_selected(self):
        key = self.selected_key
        if not key:
            return
        if key in ("circle", "bar"):
            self.project["countdown_mode"] = "none"
            self.countdown.blockSignals(True)
            self.countdown.setCurrentIndex(max(0, self.countdown.findData("none")))
            self.countdown.blockSignals(False)
        elif key in ("logo", "company", "prompt"):
            spec = self.project.get(key, {})
            spec["removed"] = True
            spec["visible"] = False
            self.project["layer_order"] = [x for x in self.project.get("layer_order", []) if x != key]
        elif key.startswith("text:"):
            self.project["text_boxes"] = [x for x in self.project["text_boxes"] if "text:" + x["id"] != key]
            self.project["layer_order"] = [x for x in self.project.get("layer_order", []) if x != key]
        elif key.startswith("extra:"):
            self.project["extra_images"] = [x for x in self.project["extra_images"] if "extra:" + x["id"] != key]
            self.project["layer_order"] = [x for x in self.project.get("layer_order", []) if x != key]
        order = self.project.get("layer_order", [])
        self.selected_key = order[-1] if order else ""
        self.refresh_all()

    def move_layer(self, action):
        key = self.selected_key
        order = self.project.get("layer_order", [])
        if key in ("circle", "bar") or key not in order:
            return
        index = order.index(key)
        if action == "top":
            new_index = len(order) - 1
        elif action == "up":
            new_index = min(len(order) - 1, index + 1)
        else:
            new_index = max(0, index - 1)
        if new_index != index:
            order.pop(index)
            order.insert(new_index, key)
            self.refresh_all()

    def refresh_all(self):
        self._countdown_preview_generation = getattr(self, "_countdown_preview_generation", 0) + 1
        normalize_project(self.project)
        self.refresh_layers()
        self.canvas.refresh(draft=False)

    def preview_countdown(self):
        self._countdown_preview_generation = getattr(self, "_countdown_preview_generation", 0) + 1
        generation = self._countdown_preview_generation
        if not self.original_files:
            return
        timeout = max(0, int(self.project.get("timeout", 3)))
        if timeout <= 0 or not countdown_key(self.project.get("countdown_mode", "none")):
            self.canvas.refresh()
            return

        # GRUB refreshes the timeout component once per second.  Start from an
        # empty state immediately, then advance by exactly 1/timeout each second
        # until the ring or bar reaches 100 percent at the configured timeout.
        fractions = [index / float(timeout) for index in range(timeout + 1)]
        self._preview_index = 0

        def render_fraction(fraction):
            project = self.project
            originals = self.original_files
            w, h = project["resolution"]
            factor = min(1.0, 1600 / max(1, w), 1000 / max(1, h))
            tw = max(1, int(w * factor)); th = max(1, int(h * factor))
            image = render_preview_scaled(project, originals, tw, th, preview_fraction=fraction, draft=True)
            self.canvas._scene.clear()
            self.canvas._selection_item = None
            self.canvas._handle_items = {}
            self.canvas._lock_label = None
            self.canvas._guide_items = []
            pix = self.canvas._scene.addPixmap(_pil_to_pixmap(image))
            pix.setScale(1 / factor)
            self.canvas._scene.setSceneRect(0, 0, w, h)
            self.canvas._pixmap_item = pix
            self.canvas._empty = False
            self.canvas._draw_guides()
            self.canvas._draw_selection()

        def step():
            if generation != self._countdown_preview_generation:
                return
            if self._preview_index >= len(fractions):
                self.canvas.refresh()
                return
            render_fraction(fractions[self._preview_index])
            self._preview_index += 1
            if self._preview_index < len(fractions):
                QTimer.singleShot(1000, step)
            else:
                QTimer.singleShot(250, finish)

        def finish():
            if generation == self._countdown_preview_generation:
                self.canvas.refresh()

        step()

    def closeEvent(self, event):
        self._countdown_preview_generation = getattr(self, "_countdown_preview_generation", 0) + 1
        super().closeEvent(event)

    def new_project(self):
        if QMessageBox.question(self, "重建画布", "清空当前编辑内容？") != QMessageBox.Yes:
            return
        w,h = self.project["resolution"]; self.project=default_project(w,h); self.selected_key="circle"; self._load_controls(); self.canvas.refresh(keep_view=False)

    def save_project(self):
        path, _ = QFileDialog.getSaveFileName(self, "保存工程", "", "AnimeBoot Neo 工程 (*.anbp)")
        if path:
            if not path.lower().endswith(".anbp"): path += ".anbp"
            try: save_project(self.project, path); self.status_label.setText("工程已保存")
            except Exception as exc: QMessageBox.critical(self, "保存失败", str(exc))

    def export_theme_package(self):
        if self._task_busy:return
        path,_=QFileDialog.getSaveFileName(self,'导出分享包','','可分享包 (*.anbtheme)')
        if not path:return
        if not path.lower().endswith('.anbtheme'):path+='.anbtheme'
        choice=QMessageBox.question(self,'包含动画素材','是否同时打包主动画及轮换素材？\n选择“否”只分享静图主题。请确保您有权分发所包含的图片、字体和动画。',QMessageBox.Yes|QMessageBox.No|QMessageBox.Cancel)
        if choice==QMessageBox.Cancel:return
        project=copy.deepcopy(self.project)
        dialog=ThemePackageTaskDialog('导出分享包',lambda:export_theme(project,path,choice==QMessageBox.Yes),self)
        dialog.exec_()
        ok,data=dialog.result_data
        if ok:self.status_label.setText('分享包已导出：'+path)
        else:QMessageBox.critical(self,'导出失败',data)

    def import_theme_package(self):
        if self._task_busy:return
        path,_=QFileDialog.getOpenFileName(self,'导入分享包','','可分享包 (*.anbtheme)')
        if not path:return
        folder=QFileDialog.getExistingDirectory(self,'选择保存导入素材的父文件夹')
        if not folder:return
        import uuid
        destination=os.path.join(folder,'AnimeBootTheme-'+uuid.uuid4().hex[:8])
        dialog=ThemePackageTaskDialog('导入分享包',lambda:import_theme(path,destination),self)
        dialog.exec_();ok,data=dialog.result_data
        if not ok:QMessageBox.critical(self,'导入失败',data);return
        self.project=data;self.selected_key='';self._load_controls();self.canvas.refresh(keep_view=False)
        self.status_label.setText('分享包已导入，素材保存在：'+destination)

    def configure_rotation(self):
        if self._task_busy:return
        dialog=RotationDialog(self.project,self,estimate_cache=getattr(self,'_rotation_estimate_cache',None))
        accepted=dialog.exec_()==QDialog.Accepted
        self._rotation_estimate_cache=dialog.estimate_cache()
        if accepted:
            self.project['boot_animation']=dialog.result_spec
            self._load_controls()

    @staticmethod
    def _missing_image_message(missing_images):
        lines = []
        for item in missing_images:
            label = str(item.get("label", "图片") or "图片")
            path = str(item.get("path", "") or "")
            lines.append("• %s：%s" % (label, path))
        detail = "\n".join(lines)
        return (
            "工程中有图片文件在原路径下已经找不到。\n"
            "点击确定后仍会继续载入工程，但对应的图片对象会自动从画布中移除。"
            + ("\n\n缺失文件：\n" + detail if detail else "")
        )

    @staticmethod
    def _missing_animation_message(missing_animation):
        data = dict(missing_animation or {})
        path = str(data.get("path", "") or "")
        kind = str(data.get("source_kind", "动画来源") or "动画来源")
        if data.get("was_enabled"):
            action = (
                "工程保存时开机动画处于启用状态。为保证工程仍可正常载入，"
                "本次已自动清空失效的动画来源，并关闭“启用动画”。\n"
                "其余动画参数（编码、JPEG质量、帧率、适配方式等）均保留；"
                "重新选择有效素材后即可再次启用动画。"
            )
        else:
            action = (
                "工程保存时开机动画原本就是关闭状态。工程仍会正常载入，"
                "本次仅自动清空失效的动画来源路径，其余动画参数保持不变。"
            )
        return (
            "工程中记录的%s在原路径下已经找不到：\n%s\n\n%s"
            % (kind, path, action)
        )

    def load_project_path(self, path):
        path = os.path.abspath(str(path or ""))
        if not path:
            return False
        try:
            project, missing_images, missing_animation = load_project(
                path,
                tolerate_missing_images=True,
                return_missing_images=True,
                tolerate_missing_animation=True,
                return_missing_animation=True,
            )
            messages = []
            if missing_images:
                messages.append(self._missing_image_message(missing_images))
            if missing_animation:
                messages.append(self._missing_animation_message(missing_animation))
            if messages:
                QMessageBox.warning(
                    self,
                    "工程外部资源缺失",
                    "\n\n────────────\n\n".join(messages),
                    QMessageBox.Ok,
                )
            self.project = project
            family = countdown_key(self.project.get("countdown_mode", "none"))
            order = self.project.get("layer_order", [])
            self.selected_key = family if family else (order[-1] if order else "")
            self._load_controls()
            self.canvas.refresh(keep_view=False)
            removed = len(missing_images)
            if missing_animation and missing_animation.get("was_enabled"):
                suffix = "；缺失动画来源已清空并自动关闭动画"
            elif missing_animation:
                suffix = "；缺失动画来源路径已清空"
            else:
                suffix = ""
            if removed or missing_animation:
                image_text = ("，已移除 %d 个缺失图片对象" % removed) if removed else ""
                self.status_label.setText("工程已载入%s%s" % (image_text, suffix))
            else:
                self.status_label.setText("工程已载入")
            return True
        except Exception as exc:
            QMessageBox.critical(self, "载入失败", str(exc))
            return False

    def open_project(self):
        path, _ = QFileDialog.getOpenFileName(self, "载入工程", "", "AnimeBoot Neo 工程 (*.anbp)")
        if path:
            self.load_project_path(path)

    def _refresh_busy_progress_animation(self):
        if self._task_busy and self.progress.isVisible():
            # Qt's indeterminate QProgressBar can stop repainting after a
            # minimize/restore cycle until the mouse enters it. A lightweight
            # explicit update tick keeps the style animation alive.
            self.progress.update()
            self.statusBar().update()

    def _set_task_busy(self, busy, text=None):
        busy = bool(busy)
        self._task_busy = busy
        if hasattr(self,'canvas_manager'):self._refresh_canvas_manager()
        if hasattr(self, "backdrop"):
            self.backdrop.set_busy(busy)
        if busy:
            self.progress.setRange(0, 0)
            self.progress.show()
            self._busy_progress_repaint_timer.start()
            if text:
                self.status_label.setText(text)
        else:
            self._busy_progress_repaint_timer.stop()
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            self.progress.hide()

    def _next_output_directory(self):
        base = self._program_directory()
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        candidate = os.path.join(base, stamp)
        suffix = 2
        while os.path.exists(candidate):
            candidate = os.path.join(base, "%s_%d" % (stamp, suffix))
            suffix += 1
        os.makedirs(candidate)
        return candidate

    def _poll_animation_frame_dir(self):
        if self._frame_folder_opened or not self.output_dir:
            return
        path = os.path.join(self.output_dir, "提取的每帧PNG预览图片")
        if not os.path.isdir(path):
            return
        self._frame_folder_opened = True
        self._frame_watch_timer.stop()
        try:
            if os.name == "nt":
                os.startfile(path)
        except Exception:
            # The build itself must not fail merely because Explorer could not be opened.
            pass


    def _elevated_build_state_payload(self):
        self.apply_basic()
        return {
            "version": 1,
            "app_version": APP_VERSION,
            "app_edition": APP_EDITION,
            "project": copy.deepcopy(self.project),
        }

    def _request_admin_and_resume_build(self):
        state_path = ""
        try:
            state_path = _write_elevated_build_state(self._elevated_build_state_payload())
            _launch_self_elevated_for_build(state_path)
        except Exception as exc:
            if state_path:
                try:
                    os.remove(state_path)
                except OSError:
                    pass
            QMessageBox.warning(self, "需要管理员权限", str(exc))
            return False
        app = QApplication.instance()
        if app is not None:
            QTimer.singleShot(0, app.quit)
        return True

    def _restore_elevated_build_state(self, payload):
        project = copy.deepcopy(dict(payload.get("project") or {}))
        normalize_project(project)
        self.project = project
        family = countdown_key(self.project.get("countdown_mode", "none"))
        order = self.project.get("layer_order", [])
        self.selected_key = family if family else (order[-1] if order else "")
        self._load_controls()
        self.canvas.refresh(keep_view=False)
        # UAC relaunch is a continuation of the user's click on “开始制作”,
        # so return to that workflow page instead of the default 静图 page.
        if hasattr(self, "workflow_tabs"):
            self.workflow_tabs.setCurrentIndex(2)

    def _resume_build_after_elevation(self):
        if hasattr(self, "workflow_tabs"):
            self.workflow_tabs.setCurrentIndex(2)
        if not _is_process_elevated():
            QMessageBox.warning(
                self,
                "仍未获得管理员权限",
                "Windows 没有授予管理员权限，因此制作没有开始。\n\n"
                "某些标准用户、单位管理账户或被策略设置为自动拒绝提权的账户不会显示 UAC 对话框；"
                "程序不能绕过 Windows 的账户安全策略自动获得管理员权限。\n\n"
                "请使用具有管理员权限的账户/凭据，或右键程序选择“以管理员身份运行”后再制作。",
            )
            return
        self.start_build()

    def _animation_space_preflight_parameters(self):
        animation = self.project.get("boot_animation", {}) or {}
        if not bool(animation.get("enabled")):
            return None, ""
        source = str(animation.get("source", "") or "")
        if not source or not os.path.exists(source):
            return None, ""
        codec = str(animation.get("codec", "jpeg") or "jpeg").lower()
        parameters = {
            "rotation_sources":animation_sources(self.project)[1:],
            "static_project":copy.deepcopy(self.project),
            "source": source,
            "width": int(self.project["resolution"][0]),
            "height": int(self.project["resolution"][1]),
            "fit_mode": str(animation.get("fit_mode", "stretch") or "stretch"),
            "maximum": int(animation.get("max_frames", 600) or 600),
            "extraction_fps": float(animation.get("extract_frame_rate", animation.get("frame_rate", 60)) or 60),
            "sampling_mode": str(animation.get("sampling_mode", "head") or "head"),
            "jpeg_quality": int(animation.get("jpeg_quality", 90) or 90),
            "sample_limit": 6,
            # Build preflight only needs the codec the user actually selected.
            # The manual estimate button still benchmarks both codecs.
            "estimate_codecs": (codec,),
        }
        return parameters, codec

    def _space_risk_from_estimate(self, result, space, codec):
        if not space or not space.get("ok"):
            return None
        key = "qoi_total_boot_bytes" if codec == "qoi" else "jpeg_total_boot_bytes"
        estimated_total = int((result or {}).get(key, 0) or 0)
        available = int(space.get("replaceable_bytes", space.get("free_bytes", 0)) or 0)
        reserve = 8 * 1024 * 1024
        if estimated_total <= 0 or estimated_total + reserve <= available:
            return None
        animation = self.project.get("boot_animation", {}) or {}
        return {
            "codec": codec,
            "quality": int(animation.get("jpeg_quality", 90) or 90),
            "estimated_total": estimated_total,
            "available": available,
            "reserve": reserve,
        }

    def _space_risk_message(self, risk):
        codec = str((risk or {}).get("codec", "jpeg") or "jpeg")
        needed = AnimationDialog._format_estimated_size(int((risk or {}).get("estimated_total", 0) or 0))
        available = AnimationDialog._format_estimated_size(int((risk or {}).get("available", 0) or 0))
        if codec == "qoi":
            headline = "当前选择 QOI Raw，预计启动组件总占用约 %s，而启动分区可用于本次替换的空间约 %s。" % (needed, available)
            suggestion = "建议改用 JPEG，或减少最大抽帧数量。"
        else:
            headline = "当前 JPEG(Q=%d) 预计启动组件总占用约 %s，而启动分区可用于本次替换的空间约 %s。" % (
                int((risk or {}).get("quality", 90) or 90), needed, available)
            suggestion = "建议降低 JPEG 质量，或减少最大抽帧数量。"
        return (
            headline + "\n\n"
            "按当前代表帧估算，存在较高的空间不足风险，但估算结果并不是最终实际写入大小。\n"
            + suggestion + "\n\n"
            "如果你清楚风险，也可以忽略本次提醒并强行继续制作。"
        )

    def _ask_force_space_warning(self, risk):
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Warning)
        dialog.setWindowTitle("启动分区空间可能不足")
        dialog.setText(self._space_risk_message(risk))
        force = dialog.addButton("仍要强行继续制作", QMessageBox.AcceptRole)
        back = dialog.addButton("返回调整", QMessageBox.RejectRole)
        dialog.setDefaultButton(back)
        dialog.exec_()
        return dialog.clickedButton() is force

    def _start_build_space_preflight(self):
        parameters, codec = self._animation_space_preflight_parameters()
        if parameters is None:
            self._begin_build_after_preflight()
            return
        self.status_label.setText("正在制作：正在并行估算动画体积与检查启动分区空间……")
        self.build_action_btn.setText("正在制作中... 正在进行体积预检")
        self.build_action_btn.setEnabled(False)
        self._build_preflight_codec = codec
        self._build_preflight_thread = QThread(self)
        self._build_preflight_worker = CodecEstimateWorker(
            parameters, float(self.project.get("capacity_mib", 32.0) or 32.0)
        )
        self._build_preflight_worker.moveToThread(self._build_preflight_thread)
        self._build_preflight_thread.started.connect(self._build_preflight_worker.run)
        self._build_preflight_worker.finished.connect(self._build_space_preflight_done)
        self._build_preflight_worker.failed.connect(self._build_space_preflight_failed)
        self._build_preflight_worker.finished.connect(self._build_preflight_thread.quit)
        self._build_preflight_worker.failed.connect(self._build_preflight_thread.quit)
        self._build_preflight_worker.finished.connect(self._build_preflight_worker.deleteLater)
        self._build_preflight_worker.failed.connect(self._build_preflight_worker.deleteLater)
        self._build_preflight_thread.finished.connect(self._build_preflight_thread_finished)
        self._build_preflight_thread.finished.connect(self._build_preflight_thread.deleteLater)
        self._build_preflight_thread.start()

    def _build_space_preflight_done(self, result, space):
        codec = str(getattr(self, "_build_preflight_codec", "jpeg") or "jpeg")
        risk = self._space_risk_from_estimate(result, space, codec)
        if risk and not self._ask_force_space_warning(risk):
            self._forced_space_warning = None
            self._finish_build_ui()
            self.status_label.setText("已取消制作：请调整动画体积或启动分区空间后重试")
            return
        self._forced_space_warning = dict(risk or {}) if risk else None
        self._begin_build_after_preflight()

    def _build_space_preflight_failed(self, message):
        text = str(message or "未知错误")
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Warning)
        if text.startswith("固件启动模式无法识别："):
            dialog.setWindowTitle("无法识别当前启动方式")
            dialog.setText(
                text + "\n\n"
                "你仍可以先完成启动资源构建；但如果制作完成后仍无法确认 UEFI/Legacy，"
                "本工具会安全停止自动安装启动项，不会猜测写入启动链。构建结果会保留，"
                "之后可点击“制作后期遇到问题点我继续”重试安装。\n\n"
                "是否仍要继续制作启动资源？"
            )
            force_text = "仍要继续制作启动资源"
        elif text.startswith("启动分区容量读取失败："):
            dialog.setWindowTitle("无法读取启动分区容量")
            dialog.setText(
                text + "\n\n"
                "当前只是无法提前判断是否放得下，不代表制作本身失败。真正安装时如果空间确实不足，"
                "本工具会按现有安全回滚逻辑保留构建结果。是否仍要在了解空间风险的情况下继续？"
            )
            force_text = "仍要强行继续制作"
        else:
            dialog.setWindowTitle("体积预检未完成")
            dialog.setText(
                "动画体积/启动分区预检没有得到可靠结果：\n%s\n\n"
                "你可以返回检查，也可以在了解风险的情况下强行继续制作。" % text
            )
            force_text = "仍要强行继续制作"
        force = dialog.addButton(force_text, QMessageBox.AcceptRole)
        back = dialog.addButton("返回检查", QMessageBox.RejectRole)
        dialog.setDefaultButton(back)
        dialog.exec_()
        if dialog.clickedButton() is force:
            self._forced_space_warning = {"preflight_failed": True, "message": text}
            self._begin_build_after_preflight()
        else:
            self._forced_space_warning = None
            self._finish_build_ui()
            self.status_label.setText("已取消制作：体积预检未完成")

    def _build_preflight_thread_finished(self):
        thread = self.sender()
        if self._build_preflight_thread is thread:
            self._build_preflight_worker = None
            self._build_preflight_thread = None

    def _validate_animation_boot_space(self):
        """Legacy synchronous checker retained for tests/internal callers.

        The real Start Build path now uses CodecEstimateWorker and never runs
        this CPU-heavy estimate in the GUI thread.
        """
        parameters, codec = self._animation_space_preflight_parameters()
        if parameters is None:
            return
        parameters.pop("static_project",None)
        result = estimate_rotation_sizes(**parameters)
        result["qoi_total_boot_bytes"] = _estimate_total_boot_bytes(result.get("qoi_bytes", 0), self.project.get("capacity_mib", 32.0)) if result.get("qoi_bytes") else 0
        result["jpeg_total_boot_bytes"] = _estimate_total_boot_bytes(result.get("jpeg_bytes", 0), self.project.get("capacity_mib", 32.0)) if result.get("jpeg_bytes") else 0
        space = probe_boot_partition_space()
        risk = self._space_risk_from_estimate(result, space, codec)
        if risk:
            raise BuildError(self._space_risk_message(risk))

    def start_build(self):
        if self._build_worker is not None:
            self.cancel_build()
            return
        if self._build_preflight_worker is not None or self._deploy_context is not None:
            return
        if not _is_process_elevated():
            self._request_admin_and_resume_build()
            return
        self._forced_space_warning = None
        # Give immediate visual feedback before any estimate/probe work starts.
        self._set_task_busy(True, "正在制作：正在准备生成启动文件……")
        self.build_action_btn.setText("正在制作中... 正在准备")
        self.build_action_btn.setEnabled(False)
        try:
            if not self.original_files:
                raise BuildError("内置画布图片无法读取，无法制作。")
            validate_project(self.project)
        except Exception as exc:
            self._finish_build_ui()
            QMessageBox.warning(self, "无法开始制作", str(exc))
            return
        # Generate real files immediately. A slow ESP mount must not gate frame
        # encoding; the elevated installer checks actual free space afterwards.
        self._begin_build_after_preflight()

    def _begin_build_after_preflight(self):
        try:
            output_dir = self._next_output_directory()
        except Exception as exc:
            self._finish_build_ui()
            QMessageBox.warning(self, "无法开始制作", str(exc))
            return
        self._debug_log("开始构建：生成普通启动文件，安装时检查实际启动分区空间。")
        self.output_dir = output_dir
        self.build_result = None
        self.final_btn.setEnabled(False)
        self._frame_folder_opened = False
        self._set_task_busy(True, "正在制作，请稍候…")
        self.build_action_btn.setText("正在制作中... 点击来放弃制作")
        self.build_action_btn.setEnabled(True)
        if bool(self.project.get("boot_animation", {}).get("enabled")):
            self._frame_watch_timer.start()
        self._build_thread = QThread(self)
        self._build_worker = BuildWorker(self.project, output_dir)
        self._build_worker.moveToThread(self._build_thread)
        self._build_thread.started.connect(self._build_worker.run)
        self._build_worker.finished.connect(self._build_done)
        self._build_worker.failed.connect(self._build_failed)
        self._build_worker.cancelled.connect(self._build_cancelled)
        self._build_worker.progress.connect(self._build_progress)
        self._build_worker.finished.connect(self._build_thread.quit)
        self._build_worker.failed.connect(self._build_thread.quit)
        self._build_worker.cancelled.connect(self._build_thread.quit)
        self._build_worker.finished.connect(self._build_worker.deleteLater)
        self._build_worker.failed.connect(self._build_worker.deleteLater)
        self._build_worker.cancelled.connect(self._build_worker.deleteLater)
        self._build_thread.finished.connect(self._build_thread_finished)
        self._build_thread.finished.connect(self._build_thread.deleteLater)
        self._build_thread.start()

    def cancel_build(self):
        worker = self._build_worker
        if worker is None:
            return
        worker.cancel()
        self.build_action_btn.setText("正在放弃制作，请稍候…")
        self.build_action_btn.setEnabled(False)
        self.status_label.setText("正在停止当前制作任务…")

    def _build_progress(self, message):
        self.status_label.setText(str(message))
        self._debug_log(message)

    def _finish_build_ui(self):
        self._frame_watch_timer.stop()
        self._set_task_busy(False)
        self.build_action_btn.setEnabled(True)
        self.build_action_btn.setText("开始制作")

    def _build_thread_finished(self):
        thread = self.sender()
        if self._build_thread is thread:
            self._build_worker = None
            self._build_thread = None

    def _build_cancelled(self):
        self._frame_watch_timer.stop()
        try:
            if self.output_dir and os.path.isdir(self.output_dir):
                shutil.rmtree(self.output_dir, ignore_errors=True)
        except Exception:
            pass
        self._finish_build_ui()
        self.status_label.setText("已放弃制作")

    def _build_done(self, result):
        self._debug_log("构建完成：" + str(result))
        self.build_result = result
        self._poll_animation_frame_dir()
        self._frame_watch_timer.stop()
        manifest = self._base_manifest()
        self._remember_pending_install_manifest(manifest, retry_allowed=False)
        self.final_btn.setEnabled(True)
        # Do not probe BitLocker in the unelevated GUI before a potentially
        # expensive build.  The real install always runs elevated and performs
        # the fail-closed encryption check immediately before touching the
        # startup chain.  A failed install leaves this build available for
        # "继续安装启动项", so the user never has to rebuild the animation.
        self.status_label.setText("构建完成，正在以管理员权限检查设备加密并自动安装启动项…")
        self.build_action_btn.setText("正在完成制作，请稍候…")
        self.build_action_btn.setEnabled(False)
        try:
            self.deploy_install(auto_chain=True)
        except Exception as exc:
            self._keep_pending_after_install_problem(manifest)
            self._finish_build_ui()
            self.status_label.setText("构建成功：自动安装暂未完成，可直接重试安装")
            QMessageBox.critical(self, "自动安装暂未完成", "%s\n\n本次构建结果已保留，无需重新制作。完成前置处理后请点击“制作后期遇到问题点我继续”。" % exc)

    def _build_failed(self, message):
        self._debug_log(message)
        self._finish_build_ui(); self.status_label.setText("制作失败"); QMessageBox.critical(self, "制作失败", message)

    def direct_modify_runtime_fps(self):
        if self._deploy_context is not None or self._build_worker is not None:
            QMessageBox.warning(self, "当前正忙", "请先等待当前制作或部署操作完成。")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("直接修改当前动图最高播放帧率")
        dialog.setWindowFlags(dialog.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        dialog.setMinimumWidth(560)
        layout = QVBoxLayout(dialog)
        notice = QLabel(
            "必须已经制作并安装了开启动图的启动画面方能使用本功能快速修改最高播放帧率。\n\n"
            "如果之后在AnimeBoot Neo Debug中重新执行“重新安装启动项”，或者重新使用本程序制作，"
            "之前用本功能做出的修改会被制作时填写的参数顶替掉!\n\n"
            "本功能仅用于快速修改当前动画最高播放帧率值，免除重新制作的麻烦。"
        )
        notice.setWordWrap(True)
        layout.addWidget(notice)
        row = QFormLayout()
        fps = ThemedSpinBox()
        fps.setRange(1, 1000)
        animation = self.project.get("boot_animation", {}) or {}
        preset = int(animation.get("frame_rate", 120) or 120)
        fps.setValue(max(1, min(1000, preset)))
        fps.setSuffix(" FPS")
        row.addRow("新的最高播放帧率（范围 1～1000 FPS）", fps)
        layout.addLayout(row)
        buttons = _localized_dialog_buttons(dialog)
        buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec_() != QDialog.Accepted:
            return
        try:
            self._start_deploy("set_runtime_fps", {"fps": int(fps.value())}, chained=False)
        except Exception as exc:
            QMessageBox.critical(self, "修改失败", str(exc))

    def _base_manifest(self):
        if self.build_result:
            return {
                "payload_dir": self.build_result["payload"]["payload_dir"],
                "asset_root": self.build_result["payload"]["asset_root"],
                "expected_timeout": int(self.build_result.get("project_snapshot", self.project)["timeout"]),
                "expected_architecture": str((self.build_result.get("payload", {}) or {}).get("architecture", "") or ""),
                "app_version": APP_VERSION,
            }
        if self._pending_manifest_valid(self.pending_install_manifest):
            return dict(self.pending_install_manifest)
        raise DeploymentError("请先完成一次制作构建。")

    def continue_pending_install(self):
        if self._deploy_context is not None or self._build_worker is not None:
            QMessageBox.warning(self, "当前正忙", "请先等待当前任务结束。")
            return
        if not self._pending_manifest_valid(self.pending_install_manifest) and not self.build_result:
            QMessageBox.warning(self, "没有待安装结果", "没有找到可继续安装的已构建结果，请先完成一次制作。")
            return
        if self.pending_install_manifest and not getattr(self, "_pending_retry_allowed", True):
            QMessageBox.warning(self, "暂不能继续安装", "上次自动回滚未完全完成，请先检查启动分区与 Windows 启动链。构建文件仍保留。")
            return
        try:
            self.deploy_install(auto_chain=True)
        except Exception as exc:
            self._keep_pending_after_install_problem()
            self.status_label.setText("安装暂未完成，可稍后直接重试")
            QMessageBox.critical(self, "安装暂未完成", "%s\n\n已构建文件仍然保留，不需要重新制作。" % exc)

    def deploy_install(self, auto_chain=False):
        try:
            # Always run a separate elevated READ-ONLY platform preflight first.
            # Legacy returns immediately after mode confirmation; UEFI additionally
            # performs the frozen fix13 read-only BootOrder/repair-choice preflight.
            self._start_deploy("install_preflight", self._base_manifest(), chained=bool(auto_chain))
        except Exception:
            if auto_chain:
                self._finish_build_ui()
            raise

    def deploy_final(self):
        try:
            self._start_deploy("final", self._base_manifest(), chained=False)
        except Exception as exc:
            QMessageBox.critical(self, "验证失败", str(exc))

    def _start_deploy(self, action, manifest, chained=False):
        self._debug_log("部署阶段：" + action)
        if action == "install_preflight":
            label = "正在完成制作：只读确认当前启动方式与安装环境，尚未修改启动项…"
        elif action == "install_full":
            label = "正在完成制作：检查设备加密并自动安装启动项…"
        elif action == "install" and chained:
            label = "正在完成制作：独立安装启动项…"
        elif action == "install":
            label = "正在独立安装启动项…"
        elif action == "uninstall_boot_item":
            label = "正在按本程序安装记录卸载本机AnimeBoot Neo Debug启动项…"
        elif action == "set_runtime_fps":
            label = "正在寻找已安装DIY动画CFG并修改运行时FPS…"
        else:
            label = "正在验证制作结果…"
        self._set_task_busy(True, label)
        try:
            prepared = prepare_elevated_action(manifest, action)
            launch_elevated_action(action, prepared["payload"], prepared["result_path"])
            self._deploy_context = {"action": action, "chained": bool(chained), "manifest": dict(manifest), **prepared}
            self._deploy_timer.start(300)
        except Exception:
            self._set_task_busy(False)
            raise

    def _production_success_message(self, result):
        dat_info = (self.build_result or {}).get("payload", {})
        project = (self.build_result or {}).get("project_snapshot", self.project)
        animation = dat_info.get("animation", {}) or {}
        lines = [
            "制作成功！",
            "",
            "构建文件夹：%s" % self.output_dir,
            "目标架构：%s" % str(dat_info.get("architecture", "未知")),
            "静图显示时间：%d 秒" % int(project.get("timeout", 3)),
        ]
        if animation.get("enabled"):
            items=animation.get('items',[])
            if animation.get('rotation')=='sequential' and len(items)>1:
                lines.append('启动动画：已启用多动画顺序轮换，共 %d 段（含主动画）；每次播放一段。' % len(items))
                lines.append('各段实际抽帧：'+ '；'.join('%s %d 帧' % ('主动画' if i==0 else '轮换%d'%i,int(item.get('frame_count',0))) for i,item in enumerate(items)))
                lines.append('全部动画实际抽帧合计：%d 帧。' % int(animation.get('frame_count',0)))
            else:
                lines.append("启动动画：已启用单段动画，共 %d 帧" % int(animation.get("frame_count", 0) or 0))
        else:
            lines.append("启动动画：未启用")
        if len(countdown_modes(project))>1:
            lines.append('静图顺序轮换：共 %d 张画布；每次显示后推进一次。'%len(countdown_modes(project)))
        if int(project.get("timeout", 3)) > 0:
            lines.append("显示静图时按 Esc 可直接启动 Windows，跳过本次动图动画。" if animation.get("enabled")
                         else "显示静图时按 Esc 可结束等待，直接启动 Windows。")
        else:
            lines.append("静图已关闭；无法使用 Esc 跳过本次动图动画。" if animation.get("enabled")
                         else "静图已关闭；直接启动 Windows。")
        if (int(project.get("timeout", 3)) > 0 and not animation.get("enabled")
                and project.get("static_windows_transition", "blend") == "blend"):
            lines.append("静图过渡：无感融入；全部画布均不显示倒计时控件，保持背景连续交接。" if all(mode=='none' for mode in countdown_modes(project))
                         else "静图过渡：无感融入；有画布显示倒计时控件，整套工程使用普通倒计时交接，会短暂黑闪后恢复静图背景。")
        mode_value = str(result.get("boot_mode", "unknown") or "unknown")
        mode_text = {"uefi": "UEFI", "bios": "传统 BIOS / Legacy"}.get(mode_value, "未能确定")
        lines.extend([
            "",
            "启动项：已安装；已保存原 Windows 启动文件，供禁用时恢复",
            "检测到的引导方式：%s" % mode_text,
            "BitLocker/设备加密安装前检查：通过（系统卷已完全解密）",
        ])
        if bool(result.get("replaced_existing")):
            lines.append("已有启动项：已自动安全覆盖更新为本次新制作内容")

        # Only surface a UEFI path warning when a bypass path was positively
        # identified and the user explicitly kept the original order.  Raw
        # BootCurrent reads are filtered on some otherwise normal PCs, so an
        # unreadable native-variable diagnostic must not become a scary
        # "制作成功" warning.
        boot_probe = result.get("bootcurrent_probe") or {}
        if mode_value == "uefi" and not bool(result.get("bootcurrent_repair_requested")):
            if boot_probe.get("needs_repair"):
                path_text = " / ".join(boot_probe.get("paths", []) or [])
                source_text = (
                    "UEFI固件默认第一启动项"
                    if str(boot_probe.get("source", "")).startswith("fwbootmgr-bcd")
                    else "本次实际UEFI启动项"
                )
                lines.extend([
                    "",
                    "UEFI启动路径提示：%s指向 %s，并非 EFI\\Microsoft\\Boot\\bootmgfw.efi。" % (source_text, path_text or "其他EFI文件"),
                    "你选择了“保持原流程”，因此开机画面仍可能被该固件启动项绕过。",
                ])

        cleaned_extra_wbm = int(result.get("uefi_unexpected_wbm_cleaned", 0) or 0)
        if cleaned_extra_wbm > 0:
            lines.extend([
                "",
                "UEFI兼容处理：检测到本机Windows/固件在真实安装过程中额外生成了%d个未经请求的启动项，已按本次事务前BootOrder快照精确清理。" % cleaned_extra_wbm,
                "用户原有启动项不按名称删除；仅清理本次操作前不存在、且不是本次明确创建的灾备项/修复WBM的Boot####。",
            ])

        if bool(result.get("bcd_auxiliary_skipped")) and not bool(result.get("bootcurrent_repair_manual_required")):
            lines.extend([
                "",
                "本机未能可靠建立可选的AnimeBoot Neo Debug灾备辅助启动项，已安全跳过；主启动链不受影响。",
            ])
        if bool(result.get("bootcurrent_repair_requested")) and bool(result.get("bootcurrent_repair_ok")):
            lines.extend([
                "",
                "UEFI启动顺序修复：已经完成。",
                "本程序已新建一个Windows Boot Manager，明确指向 EFI\\Microsoft\\Boot\\bootmgfw.efi（本次刚写入的AnimeBoot Neo Debug启动文件），并设为UEFI第一启动项。",
            ])
            if result.get('auxiliary_available'):
                lines.append("若需返回 Windows，可在 BIOS/UEFI 启动菜单选择“Boot Windows created by AnimeBoot Neo Debug”，随后用本程序“我要禁用”恢复原启动入口。")
        return "\n".join(lines)

    @staticmethod
    def _localize_messagebox_details_button(dialog):
        """Force Qt's built-in Details toggle to Chinese even without qt_zh_CN.qm."""
        def relabel():
            for button in dialog.buttons():
                text = str(button.text() or "").strip()
                if text in ("Show Details...", "Show Details…", "显示详细信息..."):
                    button.setText("显示详情...")
                elif text in ("Hide Details...", "Hide Details…", "隐藏详细信息..."):
                    button.setText("隐藏详情...")

        relabel()
        for button in dialog.buttons():
            text = str(button.text() or "")
            if "Details" in text or "详情" in text or "详细信息" in text:
                button.clicked.connect(lambda _checked=False: QTimer.singleShot(0, relabel))

    def _ask_bootcurrent_repair_choice(self, result):
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Warning)
        dialog.setWindowTitle(str(result.get("consent_title", "") or "当前UEFI启动路径可能绕过开机画面"))
        dialog.setText(str(result.get("consent_message", "") or "检测到UEFI固件默认启动路径没有指向EFI\\Microsoft\\Boot\\bootmgfw.efi。"))
        diagnostic = str(result.get("diagnostic", "") or "").strip()
        if diagnostic:
            dialog.setDetailedText("检测诊断：\n" + diagnostic)
        repair = dialog.addButton("需要，帮我修复", QMessageBox.AcceptRole)
        keep = dialog.addButton("不需要，保持原流程", QMessageBox.RejectRole)
        dialog.setDefaultButton(keep)
        self._localize_messagebox_details_button(dialog)
        dialog.exec_()
        return "repair" if dialog.clickedButton() is repair else "keep"

    @staticmethod
    def _diskgenius_manual_uefi_repair_text(error_text="", auxiliary_available=False, boot_root=""):
        lines = [
            "启动文件已安装并验证，但自动调整 UEFI 第一启动项未完成。请手动设置下面的启动项，避免开机画面被原启动路径绕过。",
            "",
            "请使用 DiskGenius 手动完成以下操作：",
            "1. 以管理员身份运行 DiskGenius。",
            "2. 打开顶部“工具”菜单，进入“设置UEFI BIOS启动项”。",
            "3. 检查当前 Windows 使用的 EFI 系统分区（ESP），选择其中的 \\EFI\\Microsoft\\Boot\\bootmgfw.efi。不要选择其他硬盘的同名文件，也不要选择 EFI\\Boot\\bootx64.efi。",
            "4. 若已有指向该分区、该文件的启动项，直接使用它；否则点击“添加”，名称填写“Windows Boot Manager”，选择上述文件。",
            "5. 将这个启动项上移到第一位，保存当前启动项设置。不要按名称批量删除机器原有的 Windows Boot Manager 或其他启动项。",
            "6. 重启验收。也可以在具备“添加启动项/选择EFI文件”功能的固件设置界面完成同样的设置。",
            "",
            "DiskGenius 官方操作说明：https://www.diskgenius.cn/help/set-uefi-bios-options.php",
        ]
        if boot_root:
            lines.insert(2, "本次安装的启动分区位置：%s（临时盘符卸载后可能不再显示）。" % boot_root)
        if auxiliary_available:
            lines.extend(["", "若需返回 Windows，可在固件启动菜单选择“Boot Windows created by AnimeBoot Neo Debug”，随后用本程序“我要禁用”恢复 Windows 原启动入口。"])
        else:
            lines.extend(["", "本次没有建立可选的 Windows 直达启动项。若决定取消此次安装，请先在当前 Windows 中点击本程序“我要禁用”，完整恢复原启动入口。"])
        if error_text:
            lines.extend(["", "自动修复未完成的原因：" + str(error_text)])
        return "\n".join(lines)

    def _show_manual_uefi_repair(self, result):
        # Complete instructions stay scrollable and copyable on small screens.
        dialog = QDialog(self)
        dialog.setWindowTitle("制作成功：请手动设置UEFI启动项")
        layout = QVBoxLayout(dialog)
        summary = QLabel("启动文件已安装，UEFI 第一启动项仍需手动设置。")
        summary.setWordWrap(True)
        layout.addWidget(summary)
        text = self._diskgenius_manual_uefi_repair_text(
            result.get('bootcurrent_repair_error', ''),
            bool(result.get('auxiliary_available')), str(result.get('boot_root', '')))
        instructions = QPlainTextEdit()
        instructions.setObjectName('ManualUefiRepairInstructions')
        instructions.setReadOnly(True)
        instructions.setPlainText(text)
        layout.addWidget(instructions, 1)
        buttons = QHBoxLayout()
        copy_button = QPushButton("复制操作步骤")
        copy_button.clicked.connect(lambda: QApplication.clipboard().setText(text))
        close_button = QPushButton("关闭")
        close_button.clicked.connect(dialog.accept)
        buttons.addWidget(copy_button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)
        screen = QApplication.primaryScreen().availableGeometry()
        dialog.resize(min(720, screen.width()-60), min(600, screen.height()-60))
        dialog.exec_()

    def _ask_secure_boot_install_consent(self, result):
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Warning)
        dialog.setWindowTitle(str(result.get("consent_title", "") or "Secure Boot安装风险提示"))
        dialog.setText(str(result.get("consent_message", "") or "当前Secure Boot信任链无法确认。"))
        diagnostic = str(result.get("diagnostic", "") or "").strip()
        if diagnostic:
            dialog.setDetailedText("检测诊断：\n" + diagnostic)
        continue_button = dialog.addButton("继续安装", QMessageBox.AcceptRole)
        cancel_button = dialog.addButton("取消", QMessageBox.RejectRole)
        dialog.setDefaultButton(cancel_button)
        self._localize_messagebox_details_button(dialog)
        dialog.exec_()
        return dialog.clickedButton() is continue_button

    def _relaunch_deploy_after_consent(self, action, manifest, chained):
        prepared = prepare_elevated_action(manifest, action)
        launch_elevated_action(action, prepared["payload"], prepared["result_path"])
        self._deploy_context = {
            "action": action,
            "chained": bool(chained),
            "manifest": dict(manifest),
            **prepared
        }
        self._deploy_timer.start(300)

    def _poll_deploy(self):
        if not self._deploy_context:
            return
        path = self._deploy_context["result_path"]
        if not os.path.isfile(path):
            return
        self._deploy_timer.stop()
        chained = bool(self._deploy_context.get("chained"))
        restarted_for_consent = False
        try:
            result = read_action_result(path, remove=True)
            self._debug_log(json.dumps(result, ensure_ascii=False, indent=2))

            current_action = str(self._deploy_context.get("action", "") or "")
            if current_action == "install_preflight" and result.get("ok"):
                manifest = dict(self._deploy_context.get("manifest") or {})
                if not manifest:
                    raise DeploymentError("UEFI只读前置检测返回后缺少待安装参数。")
                if bool(result.get("consent_needed")):
                    choice = self._ask_bootcurrent_repair_choice(result)
                    choice_origin = "user"
                else:
                    choice = "keep"
                    choice_origin = "auto-normal"
                manifest["fallback_bootcurrent_repair_choice"] = choice
                manifest["fallback_bootcurrent_repair_choice_origin"] = choice_origin
                manifest["uefi_install_preflight_complete"] = True
                manifest["uefi_install_preflight_existing_repair_guid"] = str(result.get("existing_repair_guid", "") or "")
                manifest["uefi_install_preflight_existing_repair_boot_number"] = result.get("existing_repair_boot_number")
                probe = result.get("bootcurrent_probe") or {}
                manifest["uefi_install_preflight_paths"] = list(probe.get("paths", []) or [])
                if str(result.get("boot_mode", "") or "").lower() == "bios":
                    self.status_label.setText("已确认传统 BIOS/Legacy 启动，正在开始真实安装…")
                elif bool(result.get("consent_needed")):
                    self.status_label.setText(
                        "已明确选择修复UEFI启动顺序，正在开始真实安装…" if choice == "repair"
                        else "已明确选择保持原UEFI启动顺序，正在开始真实安装…"
                    )
                else:
                    self.status_label.setText("已确认UEFI启动环境与默认启动路径，正在开始真实安装…")
                self._relaunch_deploy_after_consent("install_full", manifest, chained)
                restarted_for_consent = True
                return

            if result.get("consent_required"):
                action = str(self._deploy_context.get("action", "") or "")
                manifest = dict(self._deploy_context.get("manifest") or {})
                if action not in ("install", "install_full") or not manifest:
                    raise DeploymentError("管理员安装返回了无法继续处理的风险确认请求。")
                consent_code = str(result.get("consent_code", "") or "")
                if consent_code == "bootcurrent_fallback_repair":
                    choice = self._ask_bootcurrent_repair_choice(result)
                    manifest["fallback_bootcurrent_repair_choice"] = choice
                    self.status_label.setText(
                        "已选择自动修复UEFI启动顺序，正在继续安装…" if choice == "repair"
                        else "已保持原有UEFI启动项，正在按默认流程继续安装…"
                    )
                    self._relaunch_deploy_after_consent(action, manifest, chained)
                    restarted_for_consent = True
                    return

                if self._ask_secure_boot_install_consent(result):
                    manifest["allow_unverified_secure_boot_shim"] = True
                    self.status_label.setText("已确认固件信任提示，正在继续安装 Fedora 签名启动链…")
                    self._relaunch_deploy_after_consent(action, manifest, chained)
                    restarted_for_consent = True
                    return

                self._keep_pending_after_install_problem(manifest)
                if chained and self._pending_manifest_valid(self.pending_install_manifest):
                    self.continue_install_btn.setEnabled(True)
                    self.status_label.setText("构建成功：已取消Secure Boot风险安装，可稍后继续")
                else:
                    self.status_label.setText("已取消Secure Boot风险安装")
                return

            if not result.get("ok"):
                error_text = str(result.get("error", "未知错误") or "未知错误")
                action_name = str(self._deploy_context.get("action", "") or "")
                if action_name in ("install", "install_full") and _is_no_space_error(error_text):
                    if "安装失败后的自动回滚也有异常" in error_text:
                        error_text = (
                            "安装启动项时目标 EFI/启动分区可用空间不足，并且自动回滚未能完全恢复到安装前状态。\n\n"
                            "请不要直接重启，也不要立即点击“制作后期遇到问题点我继续”。请先检查并处理下面的回滚异常；"
                            "确认 Windows 启动链与 EFI 分区状态恢复正常后再继续。\n\n"
                            "原始错误：\n%s" % error_text
                        )
                    else:
                        error_text = (
                            "安装启动项时目标 EFI/启动分区可用空间不足。\n\n"
                            "未完成的启动文件部署已经自动回滚，Windows 启动链已恢复到本次安装前状态。"
                            "请自行扩容对应启动分区，或者减少动画帧数、降低分辨率、减少素材容量后重新制作。"
                            "本次已经完成的构建结果仍会保留；如果只是扩容启动分区，可以直接点击“制作后期遇到问题点我继续”再次安装。"
                        )
                raise DeploymentError(error_text)
            action = self._deploy_context["action"]
            if action == "set_runtime_fps":
                previous = result.get("previous_fps", []) or []
                previous_text = ", ".join("%d FPS" % int(v) for v in previous) if previous else "未设置（读取当前动画帧资源）"
                QMessageBox.information(
                    self, "运行时FPS已修改",
                    "修改完成，不需要重新制作。\n\nCFG：%s\n原运行时覆盖：%s\n当前运行时覆盖：%d FPS\n\n重启后即可直接测试。"
                    % (str(result.get("cfg_path", "")), previous_text, int(result.get("fps", 0)))
                )
                self.status_label.setText("运行时FPS已修改为 %d" % int(result.get("fps", 0)))
            elif action in ("install", "install_full"):
                self.final_btn.setEnabled(True)
                repair_manual = bool(result.get("bootcurrent_repair_manual_required"))
                if repair_manual:
                    # Keep the already-built payload and the continue button so
                    # the user can retry later without rebuilding boot files.
                    self._keep_pending_after_install_problem(self._deploy_context.get("manifest"))
                    self._show_manual_uefi_repair(result)
                    self.status_label.setText("制作成功：请手动设置UEFI启动项")
                else:
                    self._clear_pending_install_manifest()
                    QMessageBox.information(self, "制作成功" if chained else "安装完成", self._production_success_message(result))
                    self.status_label.setText("制作成功：启动项已自动安装" if chained else "安装完成")
            elif action == "uninstall_boot_item":
                self._clear_pending_install_manifest()
                if result.get('already_disabled'):
                    QMessageBox.information(self,'无需禁用','本机尚未安装 AnimeBoot Neo Debug 启动项，Windows 原启动入口保持正常。')
                    self.status_label.setText('本机没有需要禁用的启动项')
                    return
                mode_value = str(result.get("boot_mode", "unknown") or "unknown")
                mode_text = {"uefi": "UEFI", "bios": "传统 BIOS / Legacy"}.get(mode_value, "未知")
                repair_cleanup = result.get("repair_windows_boot_cleanup") or {}
                repair_line = ""
                if mode_value == "uefi":
                    if repair_cleanup.get("boot_number") is not None:
                        repair_line = (
                            "\n本次安装创建的修复 Windows Boot Manager 已按精确 Boot#### 身份移除：Boot%04X。" % int(repair_cleanup['boot_number'])
                        )
                    else:
                        repair_line = "\n本次安装状态中没有记录需要删除的 AnimeBoot Neo Debug 修复 Windows Boot Manager。"
                QMessageBox.information(
                    self, "已禁用",
                    "已按完整回滚‘卸载启动项’逻辑完成处理。\n\n"
                    "管理员权限：已通过UAC管理员子进程执行。\n"
                    "启动方式：%s\n"
                    "AnimeBoot Neo Debug 开机启动项已移除，Windows 启动链已恢复。%s\n"
                    "AnimeBootNeo 与 AnimeBootNeo.Backup 两个专属目录已删除并复核。"
                    % (mode_text, repair_line)
                )
                self.status_label.setText("已卸载AnimeBoot Neo Debug启动项")
            else:
                mode_value = str(result.get("boot_mode", "unknown") or "unknown")
                mode_text = {"uefi": "UEFI", "bios": "传统 BIOS / Legacy"}.get(mode_value, "未能确定")
                boot_root = str(result.get("boot_root", result.get("esp_root", "")) or "")
                lines = ["验证完成，没有发现问题。", "检测到的引导方式：%s" % mode_text]
                if boot_root:
                    lines.append("启动文件位置：%sAnimeBootNeo" % boot_root)
                lines.extend(["", "可以再次开机验收开机画面效果啦！"])
                QMessageBox.information(self, "验证通过", "\n".join(lines))
                self.status_label.setText("验证通过，可以开机验收")
        except Exception as exc:
            error_text = str(exc)
            rollback_incomplete = "自动回滚未能完全恢复" in error_text or "安装失败后的自动回滚也有异常" in error_text
            action_name = str((self._deploy_context or {}).get("action", "") or "")
            if action_name in ("install_preflight", "install", "install_full"):
                self._keep_pending_after_install_problem((self._deploy_context or {}).get("manifest"), retry_allowed=not rollback_incomplete)
            if chained:
                if self._pending_manifest_valid(self.pending_install_manifest) and not rollback_incomplete:
                    self.continue_install_btn.setEnabled(True)
                if rollback_incomplete:
                    QMessageBox.critical(
                        self, "自动安装失败：回滚未完全完成",
                        "%s\n\n构建结果仍保留在输出目录中，但为安全起见当前不会点亮“制作后期遇到问题点我继续”。" % error_text
                    )
                    self.status_label.setText("安装失败：自动回滚未完全完成，请先检查启动分区")
                else:
                    QMessageBox.critical(
                        self, "自动安装暂未完成",
                        "%s\n\n构建结果已经保留。处理完设备加密、启动分区空间或其他前置条件后，直接点击“制作后期遇到问题点我继续”。" % error_text
                    )
                    self.status_label.setText("构建成功：安装暂未完成，可直接重试")
            else:
                if self._deploy_context and self._deploy_context.get("action") == "set_runtime_fps":
                    QMessageBox.critical(self, "修改失败", str(exc))
                    self.status_label.setText("运行时FPS修改失败")
                elif self._deploy_context and self._deploy_context.get("action") == "uninstall_boot_item":
                    QMessageBox.critical(self, "禁用未完成", str(exc) + "\n\n请根据上述原因排除文件占用、权限或启动文件异常后，再次执行“我要禁用”。未完成清理时不会报告禁用成功。")
                    self.status_label.setText("禁用未完成，请查看当前制作/安装信息")
                else:
                    QMessageBox.critical(self, "验证失败", str(exc))
                    self.status_label.setText("验证失败")
        finally:
            if not restarted_for_consent:
                self._deploy_context = None
                if chained:
                    self._finish_build_ui()
                else:
                    self._set_task_busy(False)

    def disable_boot_animations(self):
        if self._deploy_context or self._build_worker:
            QMessageBox.warning(self, "当前正忙", "请先等待当前制作或部署操作完成。")
            return
        answer = QMessageBox.question(
            self,
            "禁用本机开机动画",
            "这个按钮实际执行的是与本程序“禁用”等价的完整回滚功能，而不是只修改动画配置。\n\n"
            "继续后将：\n"
            "• 恢复安装AnimeBoot Neo Debug启动项前的 Windows 启动链；\n"
            "• 移除本程序安装的 GRUB、Linux 播放资源及启动项；\n"
            "• Legacy 模式使用本机保存的启动文件和 MBR 备份恢复 Windows 引导。\n\n"
            "确定继续吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            self._start_deploy("uninstall_boot_item", {}, chained=False)
        except Exception as exc:
            QMessageBox.critical(self, "禁用失败", str(exc))

    def show_build_help(self):
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Information)
        dialog.setWindowTitle("帮助与注意事项")
        dialog.setText(
            "制作完成后会请求管理员权限安装启动项。安装前检查启动分区与设备加密状态；检查失败时保留制作结果供继续安装。\n\n"
            "如果制作或安装失败，请按以下情况检查：\n\n"
            "一、制作阶段提示磁盘空间不足\n"
            "这表示程序所在磁盘或 Windows 临时目录没有足够空间来保存动画帧、编码临时数据、initramfs 与启动文件，"
            "不是 EFI 分区检测。请先释放磁盘空间；动画素材很大时，也可以减少动画帧数、降低分辨率或减少素材容量后重新制作。\n\n"
            "二、制作完成后，安装启动项阶段提示 EFI/启动分区空间不足\n"
            "这时才需要自行扩容对应启动分区，或者减少动画帧数、分辨率与素材容量后重新制作。\n\n"
            "三、启动环境前置检查未通过\n"
            "请确认 BitLocker/设备加密已经完整解密，并确认当前引导方式可被识别。"
            "UEFI 模式应存在 EFI 系统分区；传统 BIOS / Legacy 模式应存在可定位的 Windows 系统启动分区。"
            "静图阶段按 Esc 直接进入 Windows，跳过本次 Linux 与动图；动图阶段的 Esc 暂不执行跳过。"
        )
        dialog.setStandardButtons(QMessageBox.Ok)
        ok_button = dialog.button(QMessageBox.Ok)
        if ok_button is not None:
            ok_button.setText("确定")
        dialog.exec_()

    def show_about(self):
        QMessageBox.information(self, "关于 AnimeBoot Neo Debug",
            "AnimeBoot Neo Debug (%s)\n"
            "Fedora 签名 UEFI 启动链 / 独立 GRUB Legacy 启动链。\n"
            "开源组件许可与来源见工程 licenses 和教程目录。\n\n"
            "作者:原心（仅收发邮件:3979250616@qq.com）\n"
            "开源测试版本！本版不再提供后续维护或更新。如需基于源码进行修改或重新开发，请务必移除程序标题栏及本“关于”页面中的原作者名、邮箱等信息。原作者不对任何二次开发版本的问题负责，也不提供维护或技术支持！\n"
            "如发现倒卖本项目（合作并入新架构前的本测试版本），或分发仍保留上述原作者信息的二次开发版本，原作者将依法追究责任。您可在清除上述原作者信息后进行改进及自用；如继续分发二次开发版本，必须遵守本项目的开源许可并公开相应源码。禁止倒卖！\n\n"
            "许可：本项目自有代码采用 GPL-3.0-or-later。上述反倒卖文字为作者倡议，不构成 GPL 的附加限制；第三方组件遵循各自许可。二次开发请勿冒用原作者身份，并保留源码版权及许可声明。" % APP_VERSION)


    def _theme_changed(self):
        self.set_theme(self.theme_selector.currentData() or THEME_LIQUID)

    def set_theme(self, theme_id):
        self.current_theme = theme_id if theme_id in THEME_STYLES else THEME_LIQUID
        apply_application_theme(self, self.current_theme)
        self._fit_header_controls()
        QTimer.singleShot(0,self._reflow_top_bar)

    def _sync_panel_toggle_states(self):
        if hasattr(self, "left_toggle"):
            self.left_toggle.blockSignals(True)
            self.left_toggle.setChecked(not self.left_panel.isHidden())
            self.left_toggle.blockSignals(False)
            self.left_toggle.update()
        if hasattr(self, "right_toggle"):
            self.right_toggle.blockSignals(True)
            self.right_toggle.setChecked(not self.right_panel.isHidden())
            self.right_toggle.blockSignals(False)
            self.right_toggle.update()

    def _refresh_panel_toggle_visuals(self):
        self._sync_panel_toggle_states()
        for button in (getattr(self, "left_toggle", None), getattr(self, "right_toggle", None)):
            if button is None:
                continue
            try:
                button.style().unpolish(button)
                button.style().polish(button)
                button.update()
            except Exception:
                pass
        if hasattr(self, "backdrop"):
            self.backdrop.update()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange and self._task_busy:
            if not self.isMinimized():
                QTimer.singleShot(0, self._refresh_busy_progress_animation)
                QTimer.singleShot(60, self._refresh_busy_progress_animation)

    def showEvent(self, event):
        super().showEvent(event)
        # Before the top-level window is shown, QWidget.isVisible() is false for
        # every child even when the child itself is not hidden.  Older code used
        # that value to sync the checkable panel buttons, so both buttons could
        # start visually unchecked until the first resize/maximize event.
        self._refresh_panel_toggle_visuals()
        QTimer.singleShot(0, self._refresh_panel_toggle_visuals)
        QTimer.singleShot(80, self._refresh_panel_toggle_visuals)

    def toggle_left(self):
        self.left_panel.setVisible(self.left_panel.isHidden())
        self._sync_panel_toggle_states()
        if hasattr(self, "backdrop"):
            self.backdrop.update()

    def toggle_right(self):
        self.right_panel.setVisible(self.right_panel.isHidden())
        self._sync_panel_toggle_states()
        if hasattr(self, "backdrop"):
            self.backdrop.update()

    def _fit_header_controls(self):
        if not hasattr(self,'theme_selector'):return
        combo=self.theme_selector;combo.ensurePolished()
        text_width=max(QFontMetrics(combo.font()).horizontalAdvance(text) for text,_ in THEME_OPTIONS)
        width=max(154,text_width+54)
        combo.setFixedWidth(width)
        for _ in range(3):
            option=QStyleOptionComboBox();combo.initStyleOption(option)
            text_rect=combo.style().subControlRect(QStyle.CC_ComboBox,option,QStyle.SC_ComboBoxEditField,combo)
            usable=min(text_rect.right(),combo.width()-35)-text_rect.left()+1
            if usable>=text_width+6:break
            combo.setFixedWidth(combo.width()+text_width+6-usable)
        share=self.theme_package_btn;share.ensurePolished()
        share.setMinimumWidth(max(130,QFontMetrics(share.font()).horizontalAdvance(share.text())+84))

    def _reflow_top_bar(self):
        if not hasattr(self,'top_bar_grid'):return
        threshold=self.top_brand.minimumSizeHint().width()+self.top_actions.minimumSizeHint().width()+36
        narrow=self.top_bar.width()<threshold
        if narrow==self._top_bar_wrapped:return
        self.top_bar_grid.removeWidget(self.top_actions)
        if narrow:
            self.top_bar_grid.addWidget(self.top_actions,1,0,1,2)
            self.top_bar_grid.setColumnStretch(0,1);self.top_bar_grid.setColumnStretch(1,0)
        else:
            self.top_bar_grid.addWidget(self.top_actions,0,1)
            self.top_bar_grid.setColumnStretch(0,0);self.top_bar_grid.setColumnStretch(1,1)
        self._top_bar_wrapped=narrow;self.top_actions.show();self.top_bar.updateGeometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self,'top_bar'):QTimer.singleShot(0,self._reflow_top_bar)
        if self.width() < 1080 and not self.right_panel.isHidden():
            self.right_panel.hide()
        self._sync_panel_toggle_states()
        if hasattr(self, "backdrop"):
            self.backdrop.update()



def _startup_project_path(argv):
    """Return the current .anbp path supplied by Windows Open With, if any."""
    for value in list(argv or [])[1:]:
        value = str(value or "").strip().strip('"')
        if not value or value.startswith("--"):
            continue
        if value.lower().endswith(".anbp") and os.path.isfile(value):
            return os.path.abspath(value)
    return ""

def _choose_ui_font(app: QApplication):
    families = set()
    try:
        families = set(QFontDatabase().families())
    except Exception:
        pass
    for name in ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI Variable", "Segoe UI", "Noto Sans CJK SC", "PingFang SC"):
        if name in families:
            app.setFont(QFont(name, 9))
            return


def main():
    startup_project = _startup_project_path(sys.argv)
    if QApplication.instance() is None:
        if hasattr(Qt, "AA_DisableWindowContextHelpButton"):
            QApplication.setAttribute(Qt.AA_DisableWindowContextHelpButton, True)
    app = QApplication.instance() or QApplication(sys.argv)
    icon_path = _application_icon_path()
    if os.path.isfile(icon_path):
        app.setWindowIcon(QIcon(icon_path))
    app._dialog_localization_filter = DialogLocalizationFilter(app)
    app.installEventFilter(app._dialog_localization_filter)
    # Native Qt/QSS scrollbar painting only.  The former global
    # Python Paint-event filter was intentionally retired because
    # continuous thumb dragging could crash some PySide2/Qt5
    # software-raster Windows systems.
    app.setOrganizationName("AnimeBoot Neo Debug")
    app.setApplicationName("AnimeBoot Neo Debug")
    app.setApplicationVersion(APP_VERSION)
    app.setStyle(ThemeProxyStyle("Fusion"))
    _choose_ui_font(app)
    app.setProperty("uiTheme", THEME_LIQUID)
    app.setPalette(build_palette(THEME_LIQUID))
    app.setStyleSheet(THEME_STYLES[THEME_LIQUID])
    elevated_build_state_path = _startup_elevated_build_state_path(sys.argv)
    if elevated_build_state_path:
        try:
            elevated_build_state = _read_elevated_build_state(elevated_build_state_path)
        except Exception as exc:
            QMessageBox.critical(None, "无法继续制作", str(exc))
            return 2
        window = MainWindow()
        window._restore_elevated_build_state(elevated_build_state)
        window.show()
        QTimer.singleShot(0, window._resume_build_after_elevation)
        return app.exec_()
    window = MainWindow()
    window.show()
    if startup_project:
        QTimer.singleShot(0, lambda path=startup_project: window.load_project_path(path))
    return app.exec_()
