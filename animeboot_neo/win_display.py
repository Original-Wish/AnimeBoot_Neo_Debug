# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
# -*- coding: utf-8 -*-
import os

from .constants import COMMON_RESOLUTIONS


def _devmode_type():
    import ctypes
    from ctypes import wintypes

    class DEVMODEW(ctypes.Structure):
        _fields_ = [
            ("dmDeviceName", wintypes.WCHAR * 32),
            ("dmSpecVersion", wintypes.WORD),
            ("dmDriverVersion", wintypes.WORD),
            ("dmSize", wintypes.WORD),
            ("dmDriverExtra", wintypes.WORD),
            ("dmFields", wintypes.DWORD),
            ("dmPositionX", wintypes.LONG),
            ("dmPositionY", wintypes.LONG),
            ("dmDisplayOrientation", wintypes.DWORD),
            ("dmDisplayFixedOutput", wintypes.DWORD),
            ("dmColor", wintypes.SHORT),
            ("dmDuplex", wintypes.SHORT),
            ("dmYResolution", wintypes.SHORT),
            ("dmTTOption", wintypes.SHORT),
            ("dmCollate", wintypes.SHORT),
            ("dmFormName", wintypes.WCHAR * 32),
            ("dmLogPixels", wintypes.WORD),
            ("dmBitsPerPel", wintypes.DWORD),
            ("dmPelsWidth", wintypes.DWORD),
            ("dmPelsHeight", wintypes.DWORD),
            ("dmDisplayFlags", wintypes.DWORD),
            ("dmDisplayFrequency", wintypes.DWORD),
            ("dmICMMethod", wintypes.DWORD),
            ("dmICMIntent", wintypes.DWORD),
            ("dmMediaType", wintypes.DWORD),
            ("dmDitherType", wintypes.DWORD),
            ("dmReserved1", wintypes.DWORD),
            ("dmReserved2", wintypes.DWORD),
            ("dmPanningWidth", wintypes.DWORD),
            ("dmPanningHeight", wintypes.DWORD),
        ]
    return ctypes, DEVMODEW


def get_current_display_resolution(default=(1920, 1080)):
    if os.name == "nt":
        try:
            ctypes, DEVMODEW = _devmode_type()
            mode = DEVMODEW()
            mode.dmSize = ctypes.sizeof(DEVMODEW)
            if ctypes.windll.user32.EnumDisplaySettingsW(None, -1, ctypes.byref(mode)):
                if mode.dmPelsWidth and mode.dmPelsHeight:
                    return int(mode.dmPelsWidth), int(mode.dmPelsHeight)
        except Exception:
            pass
    return tuple(default)


def get_current_display_refresh_rate(default=None):
    """Return the current Windows primary-display refresh rate in Hz.

    EnumDisplaySettingsW(None, ENUM_CURRENT_SETTINGS, ...) addresses the
    current primary display.  Some drivers report 1/0 for an unknown rate, so
    reject implausible values instead of presenting them as a recommendation.
    """
    if os.name == "nt":
        try:
            ctypes, DEVMODEW = _devmode_type()
            mode = DEVMODEW()
            mode.dmSize = ctypes.sizeof(DEVMODEW)
            if ctypes.windll.user32.EnumDisplaySettingsW(None, -1, ctypes.byref(mode)):
                hz = float(mode.dmDisplayFrequency or 0)
                if 20.0 <= hz <= 1000.0:
                    return hz
        except Exception:
            pass
    if default is None:
        return None
    try:
        return float(default)
    except Exception:
        return None


def get_display_resolutions():
    values = set(COMMON_RESOLUTIONS)
    values.add(get_current_display_resolution())
    if os.name == "nt":
        try:
            ctypes, DEVMODEW = _devmode_type()
            enum_display_settings = ctypes.windll.user32.EnumDisplaySettingsW
            index = 0
            while True:
                mode = DEVMODEW()
                mode.dmSize = ctypes.sizeof(DEVMODEW)
                if not enum_display_settings(None, index, ctypes.byref(mode)):
                    break
                if mode.dmPelsWidth and mode.dmPelsHeight:
                    values.add((int(mode.dmPelsWidth), int(mode.dmPelsHeight)))
                index += 1
        except Exception:
            pass
    return sorted(values, key=lambda item: (item[0] * item[1], item[0], item[1]))
