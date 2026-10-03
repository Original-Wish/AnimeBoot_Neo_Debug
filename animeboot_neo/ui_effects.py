# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
# -*- coding: utf-8 -*-
import ctypes
import math
import os
import time
import weakref

from PySide2.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer
from PySide2.QtGui import (
    QColor,
    QCursor,
    QFontMetrics,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QRadialGradient,
)
from PySide2.QtWidgets import (
    QAbstractButton,
    QApplication,
    QWidget,
    QProxyStyle,
    QStyle,
    QStyleOptionButton,
    QStyleOptionToolButton,
    QToolButton,
)


THEME_AERO = "aero"
THEME_LIQUID = "liquid"
THEME_DARK = "dark"


# Windows taskbar attention helper.  FlashWindowEx is available on the Windows
# versions supported by this project (Windows 7 through Windows 11).  It never
# steals foreground focus: it only asks the shell/taskbar to draw attention to
# the existing application window, and TIMERNOFG stops automatically when the
# user activates the program again.
_FLASHW_STOP = 0x00000000
_FLASHW_TRAY = 0x00000002
_FLASHW_TIMERNOFG = 0x0000000C
_GA_ROOT = 2


class _FLASHWINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("hwnd", ctypes.c_void_p),
        ("dwFlags", ctypes.c_uint),
        ("uCount", ctypes.c_uint),
        ("dwTimeout", ctypes.c_uint),
    ]


def _native_window_handle(widget):
    if widget is None:
        return 0
    try:
        window = widget.window()
    except Exception:
        window = widget
    try:
        return int(window.winId())
    except Exception:
        return 0


def flash_taskbar_notification(widget, count=7):
    """Flash the app's taskbar button only when another app is foreground.

    This intentionally does not call SetForegroundWindow/raise_/activateWindow,
    so reminders never steal focus from what the user is doing.
    """
    if os.name != "nt":
        return False
    hwnd = _native_window_handle(widget)
    if not hwnd:
        return False
    try:
        user32 = ctypes.windll.user32
        # Set pointer-sized signatures explicitly; ctypes defaults to c_int,
        # which would truncate HWND values in a 64-bit process.
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.GetAncestor.restype = ctypes.c_void_p
        user32.FlashWindowEx.argtypes = [ctypes.POINTER(_FLASHWINFO)]
        user32.FlashWindowEx.restype = ctypes.c_bool

        foreground = int(user32.GetForegroundWindow() or 0)
        root_hwnd = int(user32.GetAncestor(ctypes.c_void_p(hwnd), _GA_ROOT) or hwnd)
        foreground_root = int(
            user32.GetAncestor(ctypes.c_void_p(foreground), _GA_ROOT) or foreground
        ) if foreground else 0
        if foreground_root and foreground_root == root_hwnd:
            return False

        info = _FLASHWINFO()
        info.cbSize = ctypes.sizeof(_FLASHWINFO)
        info.hwnd = ctypes.c_void_p(root_hwnd)
        info.dwFlags = _FLASHW_TRAY | _FLASHW_TIMERNOFG
        info.uCount = max(1, int(count))
        info.dwTimeout = 0
        return bool(user32.FlashWindowEx(ctypes.byref(info)))
    except Exception:
        return False


def install_messagebox_taskbar_notifications(message_box_class):
    """Return a QMessageBox subclass that adds inactive-taskbar attention.

    Using a Python subclass instead of monkey-patching the Shiboken/PySide2
    wrapper type keeps this compatible with the Qt builds used on Windows 7
    through Windows 11.
    """
    base = message_box_class

    class TaskbarNotificationMessageBox(base):
        @staticmethod
        def information(parent, *args, **kwargs):
            flash_taskbar_notification(parent)
            return base.information(parent, *args, **kwargs)

        @staticmethod
        def warning(parent, *args, **kwargs):
            flash_taskbar_notification(parent)
            return base.warning(parent, *args, **kwargs)

        @staticmethod
        def critical(parent, *args, **kwargs):
            flash_taskbar_notification(parent)
            return base.critical(parent, *args, **kwargs)

        @staticmethod
        def question(parent, *args, **kwargs):
            flash_taskbar_notification(parent)
            return base.question(parent, *args, **kwargs)

        @staticmethod
        def about(parent, *args, **kwargs):
            flash_taskbar_notification(parent)
            return base.about(parent, *args, **kwargs)

        def exec_(self, *args, **kwargs):
            try:
                parent = self.parentWidget()
            except Exception:
                parent = None
            flash_taskbar_notification(parent or self)
            return base.exec_(self, *args, **kwargs)

    TaskbarNotificationMessageBox.__name__ = "TaskbarNotificationMessageBox"
    return TaskbarNotificationMessageBox


def configure_opengl_format():
    """Compatibility no-op: stable builds intentionally create no OpenGL context."""
    return None


def _theme_for_widget(widget):
    app = QApplication.instance()
    theme = app.property("uiTheme") if app else None
    if theme not in (THEME_AERO, THEME_LIQUID, THEME_DARK) and widget is not None:
        try:
            theme = widget.window().property("uiTheme")
        except Exception:
            theme = None
    return theme if theme in (THEME_AERO, THEME_LIQUID, THEME_DARK) else THEME_AERO


def build_palette(theme_id):
    palette = QPalette()
    if theme_id == THEME_DARK:
        window = QColor("#202020")
        base = QColor("#171717")
        alternate = QColor("#242424")
        text = QColor("#F2F2F2")
        disabled = QColor("#7A7A7A")
        button = QColor("#2D2D2D")
        highlight = QColor("#0078D4")
        highlighted = QColor("#FFFFFF")
        tooltip = QColor("#2B2B2B")
    elif theme_id == THEME_LIQUID:
        window = QColor("#DDE8F7")
        base = QColor(248, 251, 255, 225)
        alternate = QColor(231, 239, 251, 220)
        text = QColor("#17243A")
        disabled = QColor("#8793A6")
        button = QColor(239, 246, 255, 210)
        highlight = QColor("#647EF3")
        highlighted = QColor("#FFFFFF")
        tooltip = QColor(248, 251, 255, 248)
    else:
        window = QColor("#D8E8F4")
        base = QColor("#FFFFFF")
        alternate = QColor("#EAF4FB")
        text = QColor("#20384D")
        disabled = QColor("#8394A3")
        button = QColor("#D9ECF8")
        highlight = QColor("#2388C5")
        highlighted = QColor("#FFFFFF")
        tooltip = QColor("#EEF8FF")

    palette.setColor(QPalette.Window, window)
    palette.setColor(QPalette.WindowText, text)
    palette.setColor(QPalette.Base, base)
    palette.setColor(QPalette.AlternateBase, alternate)
    palette.setColor(QPalette.Text, text)
    palette.setColor(QPalette.Button, button)
    palette.setColor(QPalette.ButtonText, text)
    palette.setColor(QPalette.Highlight, highlight)
    palette.setColor(QPalette.HighlightedText, highlighted)
    palette.setColor(QPalette.ToolTipBase, tooltip)
    palette.setColor(QPalette.ToolTipText, text)
    palette.setColor(QPalette.Link, highlight)
    palette.setColor(QPalette.BrightText, highlighted)
    palette.setColor(QPalette.Disabled, QPalette.WindowText, disabled)
    palette.setColor(QPalette.Disabled, QPalette.Text, disabled)
    palette.setColor(QPalette.Disabled, QPalette.ButtonText, disabled)
    palette.setColor(QPalette.Disabled, QPalette.Highlight, QColor("#535A63"))
    palette.setColor(QPalette.Disabled, QPalette.HighlightedText, QColor("#AEB4BC"))
    return palette


def apply_native_titlebar(window, theme_id):
    if os.name != "nt":
        return
    try:
        hwnd = int(window.winId())
        value = ctypes.c_int(1 if theme_id == THEME_DARK else 0)
        # 20 is supported by newer Windows 10 builds; 19 covers older builds.
        for attribute in (20, 19):
            try:
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    ctypes.c_void_p(hwnd),
                    ctypes.c_uint(attribute),
                    ctypes.byref(value),
                    ctypes.sizeof(value),
                )
                break
            except Exception:
                continue
    except Exception:
        pass


class ThemeProxyStyle(QProxyStyle):
    """Draw complete themed buttons without relying on stylesheet label paint.

    Qt wraps the application style in QStyleSheetStyle whenever a global QSS is
    installed.  Delegating button labels through that wrapper is fragile on Qt
    5/PySide2, so this style paints the surface, icon and text from the actual
    QAbstractButton instance.  This also guarantees that standard icons remain
    visible in every theme.
    """

    def __init__(self, base_style="Fusion"):
        super().__init__(base_style)
        self._motion = weakref.WeakKeyDictionary()
        self._motion_timer = QTimer(self)
        self._motion_timer.setInterval(16)
        self._motion_timer.timeout.connect(self._advance_motion)

    def polish(self, target):
        result = super().polish(target)
        if isinstance(target, QAbstractButton):
            target.setAttribute(Qt.WA_Hover, True)
            target.setMouseTracking(True)
            target.installEventFilter(self)
            self._ensure_motion(target)
        return result

    def unpolish(self, target):
        if isinstance(target, QAbstractButton):
            try:
                target.removeEventFilter(self)
            except Exception:
                pass
            self._motion.pop(target, None)
        return super().unpolish(target)

    def _ensure_motion(self, widget):
        state = self._motion.get(widget)
        if state is None:
            state = {"position": 0.0, "velocity": 0.0, "target": 0.0}
            self._motion[widget] = state
        return state

    def _set_motion_target(self, widget, value):
        state = self._ensure_motion(widget)
        state["target"] = float(value)
        if not self._motion_timer.isActive():
            self._motion_timer.start()
        widget.update()

    def eventFilter(self, watched, event):
        if isinstance(watched, QAbstractButton):
            event_type = event.type()
            if event_type in (QEvent.Enter, QEvent.HoverEnter):
                self._set_motion_target(watched, 0.48)
            elif event_type in (QEvent.Leave, QEvent.HoverLeave):
                self._set_motion_target(watched, 0.0)
            elif event_type == QEvent.MouseButtonPress:
                self._set_motion_target(watched, -0.22)
            elif event_type == QEvent.MouseButtonRelease:
                self._set_motion_target(watched, 0.48 if watched.underMouse() else 0.0)
            elif event_type in (QEvent.HoverMove, QEvent.MouseMove, QEvent.FocusIn, QEvent.FocusOut):
                watched.update()
        return super().eventFilter(watched, event)

    def _advance_motion(self):
        active = False
        for widget, state in list(self._motion.items()):
            if widget is None:
                continue
            position = float(state["position"])
            velocity = float(state["velocity"])
            target = float(state["target"])
            velocity += (target - position) * 0.17
            velocity *= 0.61
            position += velocity
            if abs(target - position) < 0.003 and abs(velocity) < 0.003:
                position = target
                velocity = 0.0
            else:
                active = True
            state["position"] = position
            state["velocity"] = velocity
            try:
                widget.update()
            except Exception:
                pass
        if not active:
            self._motion_timer.stop()

    def _motion_value(self, widget):
        if widget is None:
            return 0.0
        return float(self._ensure_motion(widget)["position"])

    def sizeFromContents(self, content_type, option, size, widget=None):
        result = super().sizeFromContents(content_type, option, size, widget)
        if content_type == QStyle.CT_PushButton:
            result.setHeight(max(result.height(), 34))
            minimum_width = 48
            if isinstance(widget, QAbstractButton):
                metrics = QFontMetrics(widget.font())
                text_width = metrics.horizontalAdvance(self._display_text(widget.text()))
                icon_width = 25 if not widget.icon().isNull() else 0
                minimum_width = max(minimum_width, text_width + icon_width + 24)
            result.setWidth(max(result.width(), minimum_width))
        elif content_type == QStyle.CT_ToolButton:
            result.setHeight(max(result.height(), 31))
            result.setWidth(max(result.width(), 31))
        return result

    def pixelMetric(self, metric, option=None, widget=None):
        if metric == QStyle.PM_ButtonMargin:
            return 8
        if metric == QStyle.PM_ButtonDefaultIndicator:
            return 1
        if metric in (QStyle.PM_IndicatorWidth, QStyle.PM_IndicatorHeight):
            return 16
        return super().pixelMetric(metric, option, widget)

    def drawPrimitive(self, element, option, painter, widget=None):
        if element in (QStyle.PE_PanelButtonCommand, QStyle.PE_PanelButtonTool):
            self._draw_button_surface(option.rect, option.state, painter, widget)
            return
        if element in (QStyle.PE_IndicatorCheckBox, QStyle.PE_IndicatorItemViewItemCheck):
            self._draw_check_indicator(option.rect, option.state, painter, widget)
            return
        super().drawPrimitive(element, option, painter, widget)

    def _draw_check_indicator(self, rect, state, painter, widget=None):
        theme = _theme_for_widget(widget)
        enabled = bool(state & QStyle.State_Enabled)
        checked = bool(state & QStyle.State_On)
        mixed = bool(state & QStyle.State_NoChange)
        hover = bool(state & QStyle.State_MouseOver)
        pressed = bool(state & QStyle.State_Sunken)

        # Keep one physical pixel inside the option rectangle.  Qt can place
        # dialog checkbox indicator rects exactly at x=0; antialiasing on the
        # old centered box then clipped the whole left border/rounded corner.
        safe_rect = QRectF(rect).adjusted(1.5, 0.5, -0.5, -0.5)
        side = float(max(12, min(safe_rect.width(), safe_rect.height(), 16)))
        box = QRectF(
            safe_rect.left(),
            safe_rect.center().y() - side * 0.5,
            side,
            side,
        ).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = 3.2 if theme != THEME_DARK else 2.4

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        if theme == THEME_DARK:
            if checked or mixed:
                top = QColor('#1594E8' if hover else '#0078D4')
                bottom = QColor('#0069B8' if not pressed else '#005A9E')
                border = QColor('#4DB5F4' if hover else '#168BD2')
            else:
                top = QColor('#353535' if hover else '#2A2A2A')
                bottom = QColor('#242424')
                border = QColor('#858585' if hover else '#656565')
            mark = QColor('#FFFFFF')
        elif theme == THEME_LIQUID:
            if checked or mixed:
                top = QColor(114, 220, 255, 245)
                bottom = QColor(139, 111, 245, 238 if hover else 222)
                border = QColor(103, 126, 225, 235)
            else:
                top = QColor(255, 255, 255, 215)
                bottom = QColor(216, 232, 255, 190)
                border = QColor(116, 143, 205, 205 if hover else 165)
            mark = QColor('#FFFFFF')
        else:
            if checked or mixed:
                top = QColor(117, 213, 245, 245)
                bottom = QColor(25, 132, 192, 238 if hover else 224)
                border = QColor(30, 105, 151, 235)
            else:
                top = QColor(255, 255, 255, 242)
                bottom = QColor(207, 229, 243, 230)
                border = QColor(92, 135, 165, 205 if hover else 180)
            mark = QColor('#FFFFFF')

        if not enabled:
            top.setAlpha(120); bottom.setAlpha(105); border.setAlpha(105); mark.setAlpha(150)

        gradient = QLinearGradient(box.topLeft(), box.bottomLeft())
        gradient.setColorAt(0.0, top)
        gradient.setColorAt(1.0, bottom)
        painter.setPen(QPen(border, 1.0))
        painter.setBrush(gradient)
        painter.drawRoundedRect(box, radius, radius)

        if checked:
            pen = QPen(mark, max(1.7, side * 0.14), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            painter.setPen(pen)
            path = QPainterPath()
            path.moveTo(box.left() + side * 0.23, box.top() + side * 0.53)
            path.lineTo(box.left() + side * 0.43, box.top() + side * 0.72)
            path.lineTo(box.left() + side * 0.78, box.top() + side * 0.30)
            painter.drawPath(path)
        elif mixed:
            painter.setPen(QPen(mark, max(1.8, side * 0.15), Qt.SolidLine, Qt.RoundCap))
            painter.drawLine(
                QPointF(box.left() + side * 0.25, box.center().y()),
                QPointF(box.right() - side * 0.25, box.center().y()),
            )
        painter.restore()

    def drawControl(self, element, option, painter, widget=None):
        if element in (QStyle.CE_PushButtonLabel, QStyle.CE_ToolButtonLabel) and isinstance(widget, QAbstractButton):
            self._draw_button_content(option.rect, option.state, painter, widget)
            return
        super().drawControl(element, option, painter, widget)

    def drawComplexControl(self, control, option, painter, widget=None):
        super().drawComplexControl(control, option, painter, widget)
        if widget is not None and bool(widget.property("esrCustomIndicator")):
            return
        if control == QStyle.CC_ComboBox:
            self._draw_combo_indicator(option, painter, widget)
        elif control == QStyle.CC_SpinBox:
            self._draw_spin_indicators(option, painter, widget)

    def _indicator_colors(self, theme, enabled, active=False, pressed=False):
        if theme == THEME_DARK:
            if not enabled:
                return QColor("#252525"), QColor("#454545"), QColor("#777777")
            if pressed:
                return QColor("#134E72"), QColor("#168BD2"), QColor("#FFFFFF")
            if active:
                return QColor("#343434"), QColor("#168BD2"), QColor("#FFFFFF")
            return QColor("#292929"), QColor("#505050"), QColor("#D8D8D8")
        if theme == THEME_LIQUID:
            if not enabled:
                return QColor(232, 238, 248, 150), QColor(184, 198, 221, 120), QColor("#8390A4")
            if pressed:
                return QColor(181, 218, 255, 220), QColor(117, 144, 235, 215), QColor("#27446A")
            if active:
                return QColor(230, 247, 255, 235), QColor(139, 184, 255, 220), QColor("#284C78")
            return QColor(246, 251, 255, 210), QColor(173, 202, 245, 170), QColor("#355575")
        if not enabled:
            return QColor(220, 232, 240, 190), QColor(150, 172, 189, 135), QColor("#7890A1")
        if pressed:
            return QColor(156, 211, 239, 235), QColor(67, 147, 195, 230), QColor("#143E5A")
        if active:
            return QColor(223, 247, 255, 240), QColor(90, 183, 226, 230), QColor("#174B6C")
        return QColor(239, 249, 255, 230), QColor(142, 193, 221, 205), QColor("#31566F")

    def _draw_control_segment(self, painter, rect, theme, enabled, active, pressed):
        top, bottom, arrow = self._indicator_colors(theme, enabled, active, pressed)
        gradient = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        gradient.setColorAt(0.0, top)
        gradient.setColorAt(1.0, bottom)
        painter.fillRect(rect, gradient)
        separator = QColor("#474747") if theme == THEME_DARK else QColor(75, 116, 148, 115)
        painter.setPen(QPen(separator, 1.0))
        painter.drawLine(QPointF(rect.left(), rect.top() + 1.0), QPointF(rect.left(), rect.bottom() - 1.0))
        return arrow

    @staticmethod
    def _control_outline_color(theme, enabled):
        if theme == THEME_DARK:
            return QColor("#3C3C3C" if not enabled else "#565656")
        if theme == THEME_LIQUID:
            return QColor(137, 155, 190, 185) if not enabled else QColor(105, 137, 205, 220)
        return QColor(113, 148, 171, 150) if not enabled else QColor("#789AB4")

    @staticmethod
    def _draw_chevron(painter, center, direction, color):
        painter.setPen(QPen(color, 1.55, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        if direction == "down":
            painter.drawLine(QPointF(center.x() - 3.5, center.y() - 1.5), QPointF(center.x(), center.y() + 2.0))
            painter.drawLine(QPointF(center.x(), center.y() + 2.0), QPointF(center.x() + 3.5, center.y() - 1.5))
        else:
            painter.drawLine(QPointF(center.x() - 3.5, center.y() + 1.5), QPointF(center.x(), center.y() - 2.0))
            painter.drawLine(QPointF(center.x(), center.y() - 2.0), QPointF(center.x() + 3.5, center.y() + 1.5))

    def _draw_combo_indicator(self, option, painter, widget):
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(option.rect)
        width = min(27.0, max(22.0, rect.height() - 2.0))
        button = QRectF(rect.right() - width + 1.0, rect.top() + 1.0, width - 2.0, rect.height() - 2.0)
        theme = _theme_for_widget(widget)
        enabled = bool(option.state & QStyle.State_Enabled)
        active = bool(option.state & QStyle.State_MouseOver)
        pressed = bool(option.state & QStyle.State_Sunken)
        outer = QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5)
        clip_path = QPainterPath()
        clip_path.addRoundedRect(outer, 5.0, 5.0)
        painter.setClipPath(clip_path)
        arrow = self._draw_control_segment(painter, button, theme, enabled, active, pressed)
        painter.setClipping(False)
        self._draw_chevron(painter, button.center(), "down", arrow)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(self._control_outline_color(theme, enabled), 1.0))
        painter.drawRoundedRect(outer, 5.0, 5.0)
        painter.restore()

    def _draw_spin_indicators(self, option, painter, widget):
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(option.rect)
        width = min(27.0, max(22.0, rect.height() - 2.0))
        right = rect.right() - 1.0
        left = right - width + 1.0
        half = (rect.height() - 2.0) / 2.0
        up_rect = QRectF(left, rect.top() + 1.0, width, half)
        down_rect = QRectF(left, rect.top() + 1.0 + half, width, half)
        theme = _theme_for_widget(widget)
        enabled = bool(option.state & QStyle.State_Enabled)
        up_active = bool(option.activeSubControls & QStyle.SC_SpinBoxUp)
        down_active = bool(option.activeSubControls & QStyle.SC_SpinBoxDown)
        sunken = bool(option.state & QStyle.State_Sunken)
        outer = QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5)
        clip_path = QPainterPath()
        clip_path.addRoundedRect(outer, 5.0, 5.0)
        painter.setClipPath(clip_path)
        up_arrow = self._draw_control_segment(painter, up_rect, theme, enabled, up_active, sunken and up_active)
        down_arrow = self._draw_control_segment(painter, down_rect, theme, enabled, down_active, sunken and down_active)
        painter.setClipping(False)
        separator = QColor("#474747") if theme == THEME_DARK else QColor(75, 116, 148, 115)
        painter.setPen(QPen(separator, 1.0))
        painter.drawLine(QPointF(down_rect.left(), down_rect.top()), QPointF(down_rect.right() - 1.0, down_rect.top()))
        self._draw_chevron(painter, up_rect.center(), "up", up_arrow)
        self._draw_chevron(painter, down_rect.center(), "down", down_arrow)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(self._control_outline_color(theme, enabled), 1.0))
        painter.drawRoundedRect(outer, 5.0, 5.0)
        painter.restore()

    @staticmethod
    def _rounded_path(rect, radius):
        path = QPainterPath()
        path.addRoundedRect(QRectF(rect), radius, radius)
        return path

    @staticmethod
    def _display_text(text):
        marker = "\u0000"
        return str(text or "").replace("&&", marker).replace("&", "").replace(marker, "&")

    def _content_color(self, theme, enabled, primary):
        if not enabled:
            return QColor("#777777") if theme == THEME_DARK else QColor("#718092")
        if theme == THEME_DARK:
            return QColor("#FFFFFF") if primary else QColor("#F3F3F3")
        if theme == THEME_LIQUID:
            return QColor("#FFFFFF") if primary else QColor("#14263D")
        return QColor("#FFFFFF") if primary else QColor("#123B56")

    def _draw_button_content(self, rect, state, painter, widget):
        enabled = bool(state & QStyle.State_Enabled)
        hover = bool(state & QStyle.State_MouseOver)
        pressed = bool(state & QStyle.State_Sunken)
        primary = widget.objectName() == "PrimaryButton" or (widget.objectName() == "PanelToggle" and widget.isChecked())
        theme = _theme_for_widget(widget)
        motion = self._motion_value(widget)

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.setFont(widget.font())
        painter.setPen(self._content_color(theme, enabled, primary))

        # The surface may breathe subtly, but text and icon stay pixel-stable.
        y_offset = 0

        icon = widget.icon()
        has_icon = not icon.isNull()
        text = self._display_text(widget.text())
        has_text = bool(text)
        icon_size = widget.iconSize()
        if not icon_size.isValid() or icon_size.width() <= 0:
            icon_size = QSize(18, 18)
        icon_size = QSize(min(icon_size.width(), 20), min(icon_size.height(), 20))
        mode = QIcon.Disabled if not enabled else QIcon.Normal
        icon_state = QIcon.On if widget.isChecked() else QIcon.Off
        content = QRect(rect).adjusted(7, 3 + y_offset, -7, -3 + y_offset)
        metrics = QFontMetrics(widget.font())

        if isinstance(widget, QToolButton) and widget.toolButtonStyle() == Qt.ToolButtonIconOnly:
            if has_icon:
                pixmap = icon.pixmap(icon_size, mode, icon_state)
                x = content.center().x() - pixmap.width() // 2
                y = content.center().y() - pixmap.height() // 2
                painter.drawPixmap(x, y, pixmap)
            painter.restore()
            return

        if isinstance(widget, QToolButton) and widget.toolButtonStyle() == Qt.ToolButtonTextUnderIcon:
            if has_icon:
                pixmap = icon.pixmap(icon_size, mode, icon_state)
                x = content.center().x() - pixmap.width() // 2
                y = content.top() + 2
                painter.drawPixmap(x, y, pixmap)
                text_top = y + pixmap.height() + 2
            else:
                text_top = content.top()
            if has_text:
                painter.drawText(QRect(content.left(), text_top, content.width(), content.bottom() - text_top + 1), Qt.AlignHCenter | Qt.AlignVCenter, text)
            painter.restore()
            return

        text_width = metrics.horizontalAdvance(text) if has_text else 0
        gap = 7 if has_icon and has_text else 0
        total_width = (icon_size.width() if has_icon else 0) + gap + text_width
        x = content.center().x() - total_width // 2
        if has_icon:
            pixmap = icon.pixmap(icon_size, mode, icon_state)
            py = content.center().y() - pixmap.height() // 2
            painter.drawPixmap(x, py, pixmap)
            x += icon_size.width() + gap
        if has_text:
            painter.drawText(QRect(x, content.top(), max(1, content.right() - x + 1), content.height()), Qt.AlignLeft | Qt.AlignVCenter, text)
        painter.restore()

    def _draw_button_surface(self, rect, state, painter, widget):
        enabled = bool(state & QStyle.State_Enabled)
        hover = bool(state & QStyle.State_MouseOver)
        pressed = bool(state & QStyle.State_Sunken)
        focused = bool(state & QStyle.State_HasFocus)
        primary = bool(widget and (widget.objectName() == "PrimaryButton" or (widget.objectName() == "PanelToggle" and widget.isChecked())))
        theme = _theme_for_widget(widget)
        motion = self._motion_value(widget)

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        if theme == THEME_DARK:
            self._paint_dark(rect, painter, enabled, hover, pressed, focused, primary)
        elif theme == THEME_LIQUID:
            self._paint_liquid(rect, painter, widget, enabled, hover, pressed, focused, primary, motion)
        else:
            self._paint_aero(rect, painter, enabled, hover, pressed, focused, primary)
        painter.restore()

    def _paint_aero(self, rect, painter, enabled, hover, pressed, focused, primary):
        # Leave an internal margin so a broad Aero glow can be painted without
        # being clipped by the widget boundary.
        r = QRectF(rect).adjusted(5.0, 3.0, -5.0, -4.0)
        radius = 5.2
        path = self._rounded_path(r, radius)

        shadow = self._rounded_path(r.translated(0.0, 2.2), radius + 0.5)
        painter.fillPath(shadow, QColor(18, 55, 78, 56 if enabled else 26))

        if not enabled:
            stops = ((0.0, QColor(239, 247, 252, 180)), (0.55, QColor(199, 221, 234, 165)), (1.0, QColor(161, 192, 210, 170)))
            outline = QColor(100, 135, 158, 125)
        elif pressed:
            if primary:
                stops = ((0.0, QColor(164, 220, 244, 235)), (0.50, QColor(84, 174, 215, 232)), (1.0, QColor(52, 139, 187, 235)))
            else:
                stops = ((0.0, QColor(210, 239, 251, 226)), (0.50, QColor(139, 205, 232, 222)), (1.0, QColor(99, 174, 211, 224)))
            outline = QColor(38, 111, 151, 220)
        elif hover:
            if primary:
                stops = ((0.0, QColor(226, 250, 255, 242)), (0.42, QColor(126, 219, 246, 238)), (1.0, QColor(48, 154, 207, 238)))
            else:
                stops = ((0.0, QColor(250, 254, 255, 238)), (0.45, QColor(196, 235, 249, 230)), (1.0, QColor(113, 194, 228, 230)))
            outline = QColor(27, 132, 183, 225)
        else:
            if primary:
                stops = ((0.0, QColor(219, 246, 255, 232)), (0.46, QColor(134, 211, 239, 228)), (1.0, QColor(65, 157, 202, 230)))
            else:
                stops = ((0.0, QColor(248, 253, 255, 226)), (0.48, QColor(210, 235, 247, 215)), (1.0, QColor(139, 194, 221, 218)))
            outline = QColor(65, 119, 151, 200)

        # Large soft halo inspired by Aero taskbar/caption controls.
        if enabled and (hover or focused or primary):
            halo_alpha = 58 if hover else 34
            for width, alpha in ((9.0, halo_alpha // 3), (6.0, halo_alpha // 2), (3.2, halo_alpha)):
                painter.setPen(QPen(QColor(55, 194, 242, alpha), width))
                painter.drawPath(self._rounded_path(r.adjusted(-1.0, -1.0, 1.0, 1.0), radius + 1.4))

        body = QLinearGradient(r.topLeft(), r.bottomLeft())
        for offset, color in stops:
            body.setColorAt(offset, color)
        painter.fillPath(path, body)

        # Blue environmental light from the lower edge, matching the top bar.
        pool = QRadialGradient(r.center().x(), r.bottom() + r.height() * 0.22, max(r.width() * 0.72, r.height() * 2.2))
        pool.setColorAt(0.0, QColor(88, 213, 247, 120 if hover else 72 if enabled else 25))
        pool.setColorAt(0.48, QColor(56, 160, 210, 50 if enabled else 18))
        pool.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.fillPath(path, pool)

        painter.setPen(QPen(outline, 1.0))
        painter.drawPath(path)
        painter.setPen(QPen(QColor(255, 255, 255, 72 if enabled else 35), 1.0))
        painter.drawPath(self._rounded_path(r.adjusted(1.2, 1.2, -1.2, -1.2), radius - 1.1))

        # Very soft upper reflection; no white band and no pressed half-ellipse.
        painter.save()
        painter.setClipPath(path)
        reflection = QLinearGradient(r.left(), r.top(), r.left(), r.top() + r.height() * 0.34)
        reflection.setColorAt(0.0, QColor(255, 255, 255, 74 if enabled else 35))
        reflection.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.fillRect(QRectF(r.left() + 2.5, r.top() + 1.5, r.width() - 5.0, r.height() * 0.28), reflection)
        painter.restore()

        if pressed and enabled:
            shade = QLinearGradient(r.topLeft(), r.bottomLeft())
            shade.setColorAt(0.0, QColor(21, 72, 101, 42))
            shade.setColorAt(1.0, QColor(255, 255, 255, 8))
            painter.fillPath(path, shade)

    def _paint_liquid(self, rect, painter, widget, enabled, hover, pressed, focused, primary, motion):
        base = QRectF(rect).adjusted(4.0, 3.0, -4.0, -4.0)
        # Only a tiny surface breath remains. Text/icon geometry is never scaled.
        delta = max(0.0, motion) * 0.38 - max(0.0, -motion) * 0.24
        r = base.adjusted(-delta, -delta * 0.45, delta, delta * 0.45)
        radius = max(9.0, min(15.0, r.height() * 0.40))
        path = self._rounded_path(r, radius)

        shadow = self._rounded_path(r.translated(0.0, 2.4), radius + 0.7)
        painter.fillPath(shadow, QColor(65, 73, 112, 34 if enabled else 18))

        if not enabled:
            start, middle, end = QColor(247, 250, 255, 120), QColor(225, 234, 249, 95), QColor(211, 222, 243, 82)
        elif primary:
            if pressed:
                start, middle, end = QColor(110, 218, 255, 232), QColor(117, 169, 255, 230), QColor(178, 118, 248, 224)
            elif hover:
                start, middle, end = QColor(174, 244, 255, 238), QColor(135, 198, 255, 234), QColor(211, 144, 255, 230)
            else:
                start, middle, end = QColor(151, 232, 255, 229), QColor(142, 191, 255, 224), QColor(197, 145, 255, 218)
        else:
            if pressed:
                start, middle, end = QColor(235, 252, 255, 210), QColor(188, 228, 255, 178), QColor(226, 192, 255, 166)
            elif hover:
                start, middle, end = QColor(255, 255, 255, 235), QColor(205, 241, 255, 205), QColor(238, 209, 255, 194)
            else:
                start, middle, end = QColor(255, 255, 255, 208), QColor(213, 240, 255, 178), QColor(235, 213, 255, 164)

        body = QLinearGradient(r.topLeft(), r.bottomRight())
        body.setColorAt(0.0, start)
        body.setColorAt(0.52, middle)
        body.setColorAt(1.0, end)
        painter.fillPath(path, body)

        if enabled and widget is not None:
            # Keep the control-local highlight truly local.  The previous
            # raster port clamped an arbitrarily distant cursor onto the nearest
            # button edge, so every control acted as if the pointer were sitting
            # directly on it.  Use the real cursor position and a short
            # distance falloff instead; beyond roughly three button-heights the
            # control receives no pointer whitening at all.
            local = widget.mapFromGlobal(QCursor.pos())
            px = float(local.x())
            py = float(local.y())
            nearest_x = max(r.left(), min(r.right(), px))
            nearest_y = max(r.top(), min(r.bottom(), py))
            distance = math.hypot(px - nearest_x, py - nearest_y)
            proximity_limit = max(72.0, min(120.0, r.height() * 3.0))
            if distance < proximity_limit:
                proximity = 1.0 - distance / proximity_limit
                proximity *= proximity
                light = QRadialGradient(px, py, max(r.width(), r.height()) * 0.92)
                light.setColorAt(0.0, QColor(255, 255, 255, int((128 if hover else 76) * proximity)))
                light.setColorAt(0.38, QColor(210, 245, 255, int((48 if hover else 26) * proximity)))
                light.setColorAt(1.0, QColor(255, 255, 255, 0))
                painter.fillPath(path, light)

        # Diffuse glow only: no blue/purple oval line.
        if enabled and (hover or focused or primary):
            for width, color in (
                (8.0, QColor(118, 205, 255, 18 if not hover else 30)),
                (5.0, QColor(216, 143, 255, 20 if not hover else 34)),
                (2.8, QColor(255, 255, 255, 34 if not hover else 60)),
            ):
                painter.setPen(QPen(color, width))
                painter.drawPath(self._rounded_path(r.adjusted(-0.6, -0.6, 0.6, 0.6), radius + 1.0))

        painter.setPen(QPen(QColor(255, 255, 255, 120 if hover else 82 if enabled else 44), 1.0))
        painter.drawPath(path)
        painter.setPen(QPen(QColor(255, 255, 255, 38 if enabled else 20), 1.0))
        painter.drawPath(self._rounded_path(r.adjusted(1.5, 1.5, -1.5, -1.5), radius - 1.4))

        highlight = QRadialGradient(r.left() + r.width() * 0.28, r.top() + r.height() * 0.18, max(r.width() * 0.58, 26.0))
        highlight.setColorAt(0.0, QColor(255, 255, 255, 105 if hover else 62))
        highlight.setColorAt(0.45, QColor(255, 255, 255, 24 if hover else 14))
        highlight.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.fillPath(path, highlight)

        if pressed and enabled:
            press_shade = QLinearGradient(r.topLeft(), r.bottomLeft())
            press_shade.setColorAt(0.0, QColor(89, 110, 180, 28))
            press_shade.setColorAt(1.0, QColor(255, 255, 255, 5))
            painter.fillPath(path, press_shade)

    def _paint_dark(self, rect, painter, enabled, hover, pressed, focused, primary):
        r = QRectF(rect).adjusted(1.0, 1.0, -1.0, -1.0)
        radius = 3.0
        path = self._rounded_path(r, radius)
        if not enabled:
            top, bottom, outline = QColor("#292929"), QColor("#242424"), QColor("#3A3A3A")
        elif primary:
            if pressed:
                top, bottom, outline = QColor("#005A9E"), QColor("#0067B8"), QColor("#1A85D1")
            elif hover:
                top, bottom, outline = QColor("#168BD2"), QColor("#0078D4"), QColor("#3AA0E8")
            else:
                top, bottom, outline = QColor("#0D82CE"), QColor("#006CBD"), QColor("#1B78B7")
        else:
            if pressed:
                top, bottom, outline = QColor("#262626"), QColor("#202020"), QColor("#0078D4")
            elif hover:
                top, bottom, outline = QColor("#3A3A3A"), QColor("#313131"), QColor("#168BD2")
            else:
                top, bottom, outline = QColor("#343434"), QColor("#2C2C2C"), QColor("#505050")
        gradient = QLinearGradient(r.topLeft(), r.bottomLeft())
        gradient.setColorAt(0.0, top)
        gradient.setColorAt(1.0, bottom)
        painter.fillPath(path, gradient)
        painter.setPen(QPen(outline, 1.0))
        painter.drawPath(path)
        if focused and enabled:
            painter.setPen(QPen(QColor("#45A7E7"), 1.0, Qt.DotLine))
            painter.drawRoundedRect(r.adjusted(3.0, 3.0, -3.0, -3.0), 1.5, 1.5)


class ThemeBackdrop(QWidget):
    """60 FPS software-rendered backdrop used by all stable builds.

    Earlier releases used a native OpenGL widget with several FBO/shader passes.  A
    small set of laptop display drivers can bugcheck Windows from that native
    OpenGL path, so the stable branch deliberately stays on Qt's raster
    QPainter backend.  The animated whitening/sheens are preserved without
    creating an OpenGL context.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setAutoFillBackground(False)
        self._theme = THEME_AERO
        self._registered = []
        self._started = time.perf_counter()
        self._animation = QTimer(self)
        self._animation.setTimerType(Qt.PreciseTimer)
        self._animation.setInterval(16)
        self._animation.timeout.connect(self.update)
        self._busy = False

    def register_panel(self, widget):
        if widget is not None:
            self._registered.append(weakref.ref(widget))

    def set_theme(self, theme_id):
        self._theme = theme_id if theme_id in (THEME_AERO, THEME_LIQUID, THEME_DARK) else THEME_AERO
        self._sync_animation_timer()
        self.update()

    def set_busy(self, busy):
        # Keep the original policy: expensive decorative animation pauses while
        # building/deploying so CPU time goes to the actual job.
        self._busy = bool(busy)
        self._sync_animation_timer()
        self.update()

    def _sync_animation_timer(self):
        if self._theme == THEME_LIQUID and not self._busy:
            if not self._animation.isActive():
                self._animation.start()
        else:
            self._animation.stop()

    def _draw_liquid(self, painter):
        w = max(1, self.width())
        h = max(1, self.height())
        elapsed = float(time.perf_counter() - self._started)

        # Stable colour field: the same blue/lilac/mint family as the previous
        # shader, but rendered entirely by QPainter/raster.
        base = QLinearGradient(0, 0, w, h)
        base.setColorAt(0.0, QColor('#A9D3FF'))
        base.setColorAt(0.34, QColor('#D7C9FF'))
        base.setColorAt(0.68, QColor('#B8EEE5'))
        base.setColorAt(1.0, QColor('#E8EDF7'))
        painter.fillRect(self.rect(), base)

        # Two slowly moving colour ribbons preserve the breathing/liquid look.
        phase_a = math.sin(elapsed * 0.38) * 0.5 + 0.5
        phase_b = math.sin(elapsed * 0.31 + 2.1) * 0.5 + 0.5
        ribbon = QLinearGradient(0, int(h * (0.10 + 0.20 * phase_a)), w, int(h * (0.82 - 0.12 * phase_b)))
        ribbon.setColorAt(0.0, QColor(80, 150, 255, 18))
        ribbon.setColorAt(0.35, QColor(187, 126, 255, 34))
        ribbon.setColorAt(0.70, QColor(100, 235, 210, 24))
        ribbon.setColorAt(1.0, QColor(255, 255, 255, 8))
        painter.fillRect(self.rect(), ribbon)

        # Busy/build mode must disable the decorative whitening pass itself,
        # not merely stop the 16 ms timer.  Otherwise set_busy(True) triggers
        # one final repaint and freezes the sheen, mouse glow and panel traces
        # over the controls for the entire build.  Keep only the static colour
        # field/ribbons while busy; normal 66% sweep / 32% mouse glow / 60 FPS effects resume after.
        if self._busy:
            return

        # The requested 60 FPS realtime whitening effect.  A diagonal white
        # band sweeps across the window continuously; it is intentionally
        # generated as a gradient rather than a blurred bitmap/FBO.
        cycle = (elapsed % 6.4) / 6.4
        center = -0.35 * w + cycle * 1.70 * w
        # The raster version deliberately uses a much wider, softer band than
        # the first CPU fallback.  This recovers the large-area whitening of the
        # old shader/FBO effect without creating any native OpenGL context.
        span = max(320.0, w * 0.66)
        sheen = QLinearGradient(center - span, h, center + span, 0)
        sheen.setColorAt(0.00, QColor(255, 255, 255, 0))
        sheen.setColorAt(0.18, QColor(255, 255, 255, 18))
        sheen.setColorAt(0.34, QColor(255, 255, 255, 72))
        sheen.setColorAt(0.50, QColor(255, 255, 255, 188))
        sheen.setColorAt(0.66, QColor(236, 249, 255, 108))
        sheen.setColorAt(0.82, QColor(255, 255, 255, 30))
        sheen.setColorAt(1.00, QColor(255, 255, 255, 0))
        painter.fillRect(self.rect(), sheen)

        # A soft mouse-following highlight keeps the UI responsive to pointer
        # movement without ever touching a GPU API.
        local = self.mapFromGlobal(QCursor.pos())
        radius = min(float(w), float(h)) * 0.32
        glow = QRadialGradient(QPointF(float(local.x()), float(local.y())), radius)
        glow.setColorAt(0.0, QColor(255, 255, 255, 78))
        glow.setColorAt(0.30, QColor(232, 247, 255, 50))
        glow.setColorAt(0.62, QColor(220, 242, 255, 22))
        glow.setColorAt(0.86, QColor(255, 255, 255, 6))
        glow.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.fillRect(self.rect(), glow)

        # Lightly trace registered glass panels so the moving background still
        # reads as translucent glass even without the old refraction shader.
        painter.save()
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor(255, 255, 255, 44), 1.0))
        root_rect = self.rect()
        for reference in list(self._registered):
            widget = reference()
            if widget is None:
                try:
                    self._registered.remove(reference)
                except ValueError:
                    pass
                continue
            if not widget.isVisible() or widget.width() <= 2 or widget.height() <= 2:
                continue
            top_left = self.mapFromGlobal(widget.mapToGlobal(QPoint(0, 0)))
            rect = QRect(top_left, widget.size()).intersected(root_rect)
            if rect.width() > 2 and rect.height() > 2:
                painter.drawRoundedRect(QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5), 11.0, 11.0)
        painter.restore()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        if self._theme == THEME_DARK:
            painter.fillRect(self.rect(), QColor('#171717'))
        elif self._theme == THEME_LIQUID:
            self._draw_liquid(painter)
        else:
            gradient = QLinearGradient(self.rect().topLeft(), self.rect().bottomLeft())
            gradient.setColorAt(0.0, QColor('#8FC8EA'))
            gradient.setColorAt(0.20, QColor('#C8E6F7'))
            gradient.setColorAt(0.58, QColor('#EAF4FA'))
            gradient.setColorAt(1.0, QColor('#C4DBEA'))
            painter.fillRect(self.rect(), gradient)
            radial = QRadialGradient(QPoint(int(self.width() * 0.28), 0), self.width() * 0.62)
            radial.setColorAt(0.0, QColor(255, 255, 255, 105))
            radial.setColorAt(1.0, QColor(255, 255, 255, 0))
            painter.fillRect(self.rect(), radial)
        painter.end()

