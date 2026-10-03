# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
"""Current-version UI, layout and failed-install persistence boundaries."""
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

if os.name == "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "windows")
else:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from animeboot_neo import qt_gui as gui
from animeboot_neo.constants import APP_VERSION


class RegistryDouble:
    HKEY_CURRENT_USER = "HKCU"
    REG_SZ = 1
    KEY_QUERY_VALUE = 1
    KEY_SET_VALUE = 2
    KEY_READ = 3

    def __init__(self):
        self.keys = {}
        self.writes = []
        self.reads = []

    class Handle:
        def __init__(self, path): self.path = path
        def __enter__(self): return self.path
        def __exit__(self, *args): return False

    def OpenKey(self, root, path, *args):
        self.reads.append(path)
        if path not in self.keys: raise FileNotFoundError(path)
        return self.Handle(path)

    def CreateKeyEx(self, root, path, *args):
        parts = path.split("\\")
        for i in range(1, len(parts)+1): self.keys.setdefault("\\".join(parts[:i]), {})
        return self.Handle(path)

    def SetValueEx(self, path, name, reserved, kind, value):
        self.keys[path][name] = (value, kind)
        self.writes.append((path, name, value))

    def QueryValueEx(self, path, name):
        if name not in self.keys[path]: raise FileNotFoundError(name)
        return self.keys[path][name]

    def DeleteValue(self, path, name):
        if name not in self.keys[path]: raise FileNotFoundError(name)
        del self.keys[path][name]

    def QueryInfoKey(self, path):
        children = {key[len(path)+1:].split("\\", 1)[0] for key in self.keys if key.startswith(path+"\\")}
        return len(children), len(self.keys[path]), 0

    def DeleteKey(self, root, path):
        if path not in self.keys: raise FileNotFoundError(path)
        if any(key.startswith(path+"\\") for key in self.keys): raise OSError("not empty")
        del self.keys[path]


def make_harness(folder):
    payload = Path(folder)/"InstallPayload"
    payload.mkdir()
    (payload/"manifest.json").write_text(json.dumps({"format": "AnimeBootNeo-payload-3-canvases", "app_version": APP_VERSION}), encoding="utf-8")
    result = {"payload": {"payload_dir": str(payload), "asset_root": str(folder), "architecture": "x64 UEFI / Legacy BIOS"}}
    window = types.SimpleNamespace(output_dir=str(folder), project={"timeout": 3}, build_result=result,
        pending_install_manifest=None, _deploy_context=None, _build_worker=None,
        continue_install_btn=mock.Mock(), final_btn=mock.Mock(), status_label=mock.Mock(),
        build_action_btn=mock.Mock(), _frame_watch_timer=mock.Mock(), _deploy_timer=mock.Mock(),
        _debug_log=mock.Mock(), _finish_build_ui=mock.Mock(), _set_task_busy=mock.Mock(),
        _poll_animation_frame_dir=mock.Mock(), _production_success_message=mock.Mock(return_value="done"),
        _diskgenius_manual_uefi_repair_text=mock.Mock(return_value="manual"),
        deploy_install=mock.Mock(), _ask_secure_boot_install_consent=mock.Mock(return_value=False))
    methods = ("_pending_manifest_file", "_remember_pending_install_manifest", "_persist_pending_install_manifest",
        "_keep_pending_after_install_problem", "_pending_manifest_valid", "_restore_pending_install_manifest",
        "_clear_pending_install_manifest", "_base_manifest", "_build_done", "_poll_deploy", "continue_pending_install")
    for name in methods: setattr(window, name, getattr(gui.MainWindow, name).__get__(window))
    return window, result


class RegistryLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.registry = RegistryDouble()
        self.registry_patch = mock.patch.dict(sys.modules, {"winreg": self.registry})
        self.registry_patch.start()
        self.platform_patch = mock.patch.object(gui.os, "name", "nt")
        self.platform_patch.start()
        self.messages = mock.patch.object(gui, "QMessageBox", mock.Mock())
        self.messages.start()
        self.addCleanup(self.messages.stop)
        self.addCleanup(self.platform_patch.stop)
        self.addCleanup(self.registry_patch.stop)

    def _poll(self, window, folder, action, response, chained=True):
        path = Path(folder)/"action-result.json"
        path.write_text("{}", encoding="utf-8")
        window._deploy_context = {"result_path": str(path), "action": action, "chained": chained,
                                  "manifest": window._base_manifest()}
        with mock.patch.object(gui, "read_action_result", return_value=response): window._poll_deploy()

    def test_successful_build_enters_install_without_registry_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            window, result = make_harness(folder)
            window._build_done(result)
            self.assertEqual(self.registry.writes, [])
            self.assertFalse(Path(window._pending_manifest_file()).exists())
            window.deploy_install.assert_called_once_with(auto_chain=True)

    def test_uac_cancel_keeps_retry_only_after_install_problem(self):
        with tempfile.TemporaryDirectory() as folder:
            window, result = make_harness(folder)
            window.deploy_install.side_effect = OSError("user cancelled UAC")
            window._build_done(result)
            self.assertEqual(len(self.registry.writes), 1)
            self.assertTrue(window._pending_manifest_valid(window.pending_install_manifest))
            record = json.loads(Path(window._pending_manifest_file()).read_text(encoding="utf-8"))
            self.assertEqual((record["version"], record["app_version"], record["app_edition"]), (2, "2610.3", "Debug"))
            self.assertTrue(record["retry_allowed"])

    def test_failed_preflight_and_install_preserve_retry_across_restart(self):
        for action in ("install_preflight", "install", "install_full"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as folder:
                window, result = make_harness(folder)
                self._poll(window, folder, action, {"ok": False, "error": "partition full"})
                self.assertEqual(gui._read_pending_registry_path(), window._pending_manifest_file())
                window.pending_install_manifest = None
                self.assertTrue(window._restore_pending_install_manifest())
                self.assertTrue(window._pending_retry_allowed)
                gui._clear_pending_registry_path()

    def test_risk_consent_cancellation_preserves_completed_build(self):
        with tempfile.TemporaryDirectory() as folder:
            window, _ = make_harness(folder)
            self._poll(window, folder, "install_full", {"consent_required": True, "consent_code": "shim-not-trusted"})
            self.assertTrue(window._pending_retry_allowed)
            self.assertTrue(Path(window._pending_manifest_file()).is_file())

    def test_rollback_failure_preserves_files_but_does_not_enable_retry_after_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            window, _ = make_harness(folder)
            self._poll(window, folder, "install_full", {"ok": False, "error": "自动回滚未能完全恢复"})
            self.assertFalse(window._pending_retry_allowed)
            self.assertTrue(window._restore_pending_install_manifest())
            self.assertFalse(window._pending_retry_allowed)
            window.continue_pending_install()
            window.deploy_install.assert_not_called()

    def test_manual_firmware_repair_keeps_retry_but_success_all_install_paths_clears_it(self):
        with tempfile.TemporaryDirectory() as folder:
            window, _ = make_harness(folder)
            self._poll(window, folder, "install_full", {"ok": True, "bootcurrent_repair_manual_required": True})
            self.assertTrue(gui._read_pending_registry_path())
        for action in ("install", "install_full"):
            for chained in (False, True):
                with self.subTest(action=action, chained=chained), tempfile.TemporaryDirectory() as folder:
                    window, _ = make_harness(folder)
                    window._keep_pending_after_install_problem()
                    record = Path(window._pending_manifest_file())
                    self._poll(window, folder, action, {"ok": True}, chained)
                    self.assertEqual(gui._read_pending_registry_path(), "")
                    self.assertNotIn(gui._pending_registry_key(), self.registry.keys)
                    self.assertFalse(record.exists())
                    self.assertIsNone(window.pending_install_manifest)

    def test_old_registry_key_is_never_read_or_removed_and_current_wrong_schema_is_rejected(self):
        old = r"Software\AnimeBoot Neo Debug\AnimeBoot Neo Debug"
        self.registry.keys[old] = {"theme_final": ("dark", 1), "pending_install_manifest": ("old.json", 1)}
        with tempfile.TemporaryDirectory() as folder:
            window, _ = make_harness(folder)
            window._keep_pending_after_install_problem()
            path = Path(window._pending_manifest_file())
            record = json.loads(path.read_text(encoding="utf-8"))
            record["app_version"] = "2610.1"
            path.write_text(json.dumps(record), encoding="utf-8")
            self.assertFalse(window._restore_pending_install_manifest())
            self.assertNotIn(old, self.registry.reads)
            self.assertEqual(self.registry.keys[old]["theme_final"], ("dark", 1))
            self.assertNotIn(gui._pending_registry_key(), self.registry.keys)

    def test_old_payload_cannot_be_used_for_current_continuation(self):
        with tempfile.TemporaryDirectory() as folder:
            window, _ = make_harness(folder)
            manifest = window._base_manifest()
            report = Path(manifest["payload_dir"])/"manifest.json"
            report.write_text(json.dumps({"format": "AnimeBootNeo-1", "app_version": "2610.1"}), encoding="utf-8")
            self.assertFalse(window._pending_manifest_valid(manifest))
            self.assertFalse(window._keep_pending_after_install_problem(manifest))
            self.assertEqual(self.registry.writes, [])

    def test_denied_registry_write_keeps_retry_available_in_current_session(self):
        with tempfile.TemporaryDirectory() as folder:
            window, _ = make_harness(folder)
            with mock.patch.object(gui, "_write_pending_registry_path", side_effect=PermissionError("denied")):
                self.assertFalse(window._keep_pending_after_install_problem())
            self.assertTrue(window._pending_retry_allowed)
            window.continue_install_btn.setEnabled.assert_called_with(True)
            window._debug_log.assert_called_once()
            self.assertEqual(self.registry.writes, [])


class CurrentUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide2.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_title_debug_scope_theme_and_countdown_are_current_and_unclipped(self):
        from PySide2.QtCore import Qt
        from PySide2.QtGui import QFont, QFontMetrics
        from PySide2.QtWidgets import QLabel
        original_font = QFont(self.app.font())
        window = None
        # The normal and larger fonts exercise the logical geometry used with
        # DPI scaling. This check never starts the background font scanner.
        try:
            for font_points in (10, 13):
                font = QFont(original_font); font.setPointSize(font_points)
                self.app.setFont(font)
                with mock.patch.object(gui, "_read_pending_registry_path", return_value=""), \
                     mock.patch.object(gui, "_write_pending_registry_path") as write, \
                     mock.patch.object(gui, "_font_catalog_loader", return_value=mock.Mock()):
                    window = gui.MainWindow()
                    window.setAttribute(Qt.WA_DontShowOnScreen, True)
                    window.resize(1040, 720)
                    window.show(); self.app.processEvents()
                    self.assertIn("(2610.3) 作者:原心", window.windowTitle())
                    self.assertGreaterEqual(window.left_panel.minimumWidth(), 355)
                    self.assertGreaterEqual(window.left_panel.maximumWidth(), 460)
                    labels = window.findChildren(QLabel)
                    countdown = next(label for label in labels if label.text() == "倒计时(0-180秒)")
                    self.assertGreaterEqual(countdown.width(), QFontMetrics(countdown.font()).horizontalAdvance(countdown.text()))
                    self.assertTrue(any(label.text() == "Qt 可视化启动界面制作工具 (2610.3)" for label in labels))
                    subtitle = next(label for label in labels if label.text() == "Qt 可视化启动界面制作工具 (2610.3)")
                    self.assertTrue(subtitle.wordWrap())
                    self.assertGreaterEqual(subtitle.height(), subtitle.heightForWidth(subtitle.width()))
                    self.assertTrue(any("播放结束后显示两页诊断信息，每页 8 秒" in label.text() for label in labels))
                    self.assertIn("仅在启用动图时生效", window.show_debug_info.toolTip())
                    parameter_box = window.show_debug_info.parentWidget()
                    self.assertTrue(parameter_box.title().startswith("①"))
                    diagnostic_hint = next(label for label in labels if label.objectName() == "DebugPlaybackHint")
                    self.assertIs(diagnostic_hint.parentWidget(), parameter_box)
                    parameters = parameter_box.layout()
                    self.assertLess(parameters.indexOf(window.grub_compat_new.parentWidget()), parameters.indexOf(window.show_debug_info))
                    self.assertLess(parameters.indexOf(diagnostic_hint), parameters.indexOf(window.static_windows_transition_row))
                    self.assertEqual(window.debug_output.maximumHeight(), 120)
                    build_box = window.build_action_btn.parentWidget()
                    self.assertTrue(build_box.title().startswith("②"))
                    information_title = next(label for label in labels if label.objectName() == "BuildInfoTitle")
                    self.assertIs(information_title.parentWidget(), build_box)
                    self.assertIs(window.debug_output.parentWidget(), build_box)
                    build_layout = build_box.layout()
                    self.assertLess(build_layout.indexOf(window.build_action_btn), build_layout.indexOf(information_title))
                    self.assertLess(build_layout.indexOf(information_title), build_layout.indexOf(window.debug_output))
                    self.assertLess(build_layout.indexOf(window.debug_output), build_layout.indexOf(window.continue_install_btn))
                    window.set_theme(gui.THEME_DARK)
                    self.assertEqual(window.current_theme, gui.THEME_DARK)
                    write.assert_not_called()
                    window.close(); window.deleteLater(); self.app.processEvents(); window = None
        finally:
            if window is not None: window.close()
            self.app.setFont(original_font)

    def test_static_transition_choices_are_exclusive_and_round_trip_current_project(self):
        from PySide2.QtCore import Qt
        with tempfile.TemporaryDirectory() as folder, \
             mock.patch.object(gui, "_read_pending_registry_path", return_value=""), \
             mock.patch.object(gui, "_font_catalog_loader", return_value=mock.Mock()):
            window = gui.MainWindow()
            window.setAttribute(Qt.WA_DontShowOnScreen, True)
            window.show(); self.app.processEvents()
            try:
                self.assertEqual(window.project["static_windows_transition"], "blend")
                self.assertTrue(window.static_windows_blend.isChecked())
                self.assertTrue(window.static_windows_blend.isEnabled())
                self.assertFalse(window.static_windows_black.isChecked())
                for value, button in (("black", window.static_windows_black), ("blend", window.static_windows_blend)):
                    button.click()
                    self.assertEqual(window.project["static_windows_transition"], value)
                    self.assertEqual(sum(int(b.isChecked()) for b in window.static_windows_transition_group.buttons()), 1)
                    window.apply_basic()
                    self.assertEqual(window.project["static_windows_transition"], value)
                    path = str(Path(folder)/(value+".anbp"))
                    with mock.patch.object(gui.QFileDialog, "getSaveFileName", return_value=(path, "")):
                        window.save_project()
                    opposite = window.static_windows_blend if value == "black" else window.static_windows_black
                    opposite.click()
                    self.assertNotEqual(window.project["static_windows_transition"], value)
                    self.assertTrue(window.load_project_path(path))
                    self.assertEqual(window.project["static_windows_transition"], value)
                    self.assertTrue(button.isChecked())
                # Saving an animation-enabled project must retain the static
                # preference even though the user cannot change it right now.
                from PIL import Image
                source = str(Path(folder)/"animation.gif")
                Image.new("RGB", (8, 8), "red").save(source)
                window.static_windows_black.click()
                window.project["boot_animation"].update(enabled=True, source=source)
                window._load_controls()
                path = str(Path(folder)/"animated.anbp")
                with mock.patch.object(gui.QFileDialog, "getSaveFileName", return_value=(path, "")):
                    window.save_project()
                window.project["boot_animation"]["enabled"] = False
                window._load_controls(); window.static_windows_blend.click()
                self.assertTrue(window.load_project_path(path))
                self.assertTrue(window.project["boot_animation"]["enabled"])
                self.assertEqual(window.project["static_windows_transition"], "black")
                self.assertTrue(window.static_windows_black.isChecked())
                self.assertFalse(window.static_windows_blend.isEnabled())
                window.project["boot_animation"]["enabled"] = False
                window._load_controls()
                self.assertTrue(window.static_windows_black.isEnabled())
                self.assertTrue(window.static_windows_black.isChecked())
            finally:
                window.close(); window.deleteLater(); self.app.processEvents()

    def test_animation_dialog_and_basic_sync_disable_static_choices_without_losing_preference(self):
        from PySide2.QtCore import Qt
        with mock.patch.object(gui, "_read_pending_registry_path", return_value=""), \
             mock.patch.object(gui, "_font_catalog_loader", return_value=mock.Mock()):
            window = gui.MainWindow()
            window.setAttribute(Qt.WA_DontShowOnScreen, True)
            window.show(); self.app.processEvents()
            try:
                window.static_windows_black.click()
                for enabled in (True, False):
                    dialog = mock.Mock()
                    dialog.exec_.return_value = gui.QDialog.Accepted
                    def apply(project, enabled=enabled):
                        project["boot_animation"]["enabled"] = enabled
                    dialog.apply.side_effect = apply
                    with mock.patch.object(gui, "AnimationDialog", return_value=dialog):
                        window.configure_animation()
                    self.assertEqual(window.project["static_windows_transition"], "black")
                    self.assertTrue(window.static_windows_black.isChecked())
                    self.assertEqual(window.static_windows_blend.isEnabled(), not enabled)
                    self.assertEqual(window.static_windows_black.isEnabled(), not enabled)
                    self.assertEqual(window.static_windows_transition_hint.isEnabled(), not enabled)
                    if enabled:
                        window.static_windows_blend.click()
                        self.assertEqual(window.project["static_windows_transition"], "black")
                window.project["boot_animation"]["enabled"] = True
                window.apply_basic()
                self.assertFalse(window.static_windows_transition_row.isEnabled())
                window.project["boot_animation"]["enabled"] = False
                window.apply_basic()
                self.assertTrue(window.static_windows_transition_row.isEnabled())
                self.assertTrue(window.static_windows_black.isChecked())
            finally:
                window.close(); window.deleteLater(); self.app.processEvents()

    def test_open_with_accepts_current_project_extension_only(self):
        with tempfile.TemporaryDirectory() as folder:
            old, current = Path(folder)/"old.esrp", Path(folder)/"current.anbp"
            old.write_text("old"); current.write_text("current")
            self.assertEqual(gui._startup_project_path(["app.py", str(old)]), "")
            self.assertEqual(gui._startup_project_path(["app.py", str(current)]), str(current))


if __name__ == "__main__": unittest.main()
