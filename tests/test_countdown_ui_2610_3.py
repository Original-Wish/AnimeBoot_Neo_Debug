# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
"""Native countdown styles retain editable canvas objects and truthful previews."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide2.QtCore import QEvent, QPointF, Qt
from PySide2.QtGui import QMouseEvent
from PySide2.QtWidgets import QApplication, QLabel
from animeboot_neo import qt_gui as gui
from animeboot_neo.project import COUNTDOWN_MODES, countdown_key, countdown_display_name, default_project, save_project


VISIBLE = tuple(mode for mode in COUNTDOWN_MODES if countdown_key(mode))


class CountdownVariantUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.stack = __import__('contextlib').ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(gui, '_read_pending_registry_path', return_value=''))
        self.stack.enter_context(mock.patch.object(gui, '_font_catalog_loader', return_value=mock.Mock()))
        self.window = gui.MainWindow()
        self.window.project = default_project(640, 480)
        self.window._load_controls()
        self.window.show()
        self.app.processEvents()
        self.addCleanup(self.window.close)

    def set_mode_silently(self, mode):
        self.window.project['countdown_mode'] = mode
        self.window._load_controls()
        self.window.refresh_all()
        self.app.processEvents()

    def test_style_selector_lists_all_styles_and_default_remains_hidden(self):
        combo = self.window.countdown
        self.assertEqual(tuple(combo.itemData(i) for i in range(combo.count())), COUNTDOWN_MODES)
        self.assertEqual(combo.currentData(), 'none')
        self.assertEqual(self.window.project['countdown_mode'], 'none')
        self.assertIn('固定 16px', combo.toolTip())
        for index, mode in enumerate(COUNTDOWN_MODES):
            self.assertEqual(combo.itemText(index), countdown_display_name(mode))

    def test_each_visible_style_requires_confirmation_cancel_restores_exact_previous_choice(self):
        shown = []
        def execute(message):
            shown.append(message.text())
            return 0
        with mock.patch.object(gui.QMessageBox, 'exec_', execute):
            for mode in VISIBLE:
                with self.subTest(mode=mode):
                    previous = self.window.project['countdown_mode']
                    with mock.patch.object(gui.QMessageBox, 'clickedButton',
                            lambda message: next(button for button in message.buttons() if button.text() == '取消')):
                        self.window.countdown.setCurrentIndex(self.window.countdown.findData(mode))
                    self.assertEqual(self.window.project['countdown_mode'], previous)
                    self.assertEqual(self.window.countdown.currentData(), previous)
                    with mock.patch.object(gui.QMessageBox, 'clickedButton',
                            lambda message: next(button for button in message.buttons() if button.text() == '仍要继续')):
                        self.window.countdown.setCurrentIndex(self.window.countdown.findData(mode))
                    self.assertEqual(self.window.project['countdown_mode'], mode)
                    self.assertEqual(self.window.selected_key, countdown_key(mode))
        self.assertEqual(len(shown), len(VISIBLE) * 2)
        self.assertTrue(all('任何可见倒计时样式' in text and '未启用动图' in text and '黑屏闪烁' in text for text in shown))

    def test_project_load_preserves_each_new_style_without_confirmation(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(gui.QMessageBox, 'exec_') as confirm:
            for mode in VISIBLE:
                with self.subTest(mode=mode):
                    project = default_project(640, 480)
                    project['countdown_mode'] = mode
                    path = Path(folder) / (mode + '.anbp')
                    save_project(project, path)
                    self.assertTrue(self.window.load_project_path(path))
                    self.assertEqual(self.window.project['countdown_mode'], mode)
                    self.assertEqual(self.window.countdown.currentData(), mode)
                    self.assertEqual(self.window.selected_key, countdown_key(mode))
            confirm.assert_not_called()

    def test_layer_listing_and_hit_testing_use_existing_family_objects(self):
        for mode in VISIBLE:
            with self.subTest(mode=mode):
                self.set_mode_silently(mode)
                key = countdown_key(mode)
                row = self.window.layers.item(0)
                self.assertEqual(row.data(Qt.UserRole), key)
                self.assertIn(countdown_display_name(mode), row.text())
                self.assertFalse(row.flags() & Qt.ItemIsDragEnabled)
                spec = self.window.project[key]
                center = QPointF(spec['x'] + spec['w']/2.0, spec['y'] + spec['h']/2.0)
                self.assertEqual(self.window.canvas._hit_test(center), key)
                self.window.select_key(key)
                self.assertEqual(self.window.canvas.selected_key, key)

    def test_every_variant_can_be_dragged_without_replacing_mode_or_object(self):
        for mode in VISIBLE:
            with self.subTest(mode=mode):
                self.set_mode_silently(mode)
                key = countdown_key(mode)
                self.window.select_key(key)
                canvas = self.window.canvas
                rect = canvas._selection_rect(key)
                start = QPointF(canvas.mapFromScene(rect.center()))
                old = dict(self.window.project[key])
                press = QMouseEvent(QEvent.MouseButtonPress, start, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
                canvas.mousePressEvent(press)
                self.assertIsNotNone(canvas._drag)
                # Move left/up so even a bottom-aligned progress bar has room.
                end = start + QPointF(-8, -8)
                move = QMouseEvent(QEvent.MouseMove, end, Qt.NoButton, Qt.LeftButton, Qt.NoModifier)
                canvas.mouseMoveEvent(move)
                release = QMouseEvent(QEvent.MouseButtonRelease, end, Qt.LeftButton, Qt.NoButton, Qt.NoModifier)
                canvas.mouseReleaseEvent(release)
                self.assertIsNone(canvas._drag)
                self.assertNotEqual((self.window.project[key]['x'], self.window.project[key]['y']), (old['x'], old['y']))
                self.assertEqual(self.window.project['countdown_mode'], mode)
                self.assertEqual(self.window.selected_key, key)

    def test_properties_edit_family_geometry_and_number_has_no_custom_font_size(self):
        for mode in VISIBLE:
            with self.subTest(mode=mode):
                self.set_mode_silently(mode)
                key = countdown_key(mode)
                dialog = gui.ObjectPropertiesDialog(self.window.project, key, self.window)
                try:
                    dialog.x.setValue(15)
                    dialog.y.setValue(21)
                    if mode == 'number':
                        self.assertIsNone(dialog.font_size)
                        self.assertIsNone(dialog.bar_bg)
                        self.assertIsNone(dialog.bar_border)
                        self.assertEqual(dialog.w.minimum(), 24)
                        self.assertEqual(dialog.h.minimum(), 16)
                        self.assertTrue(any('固定 16px' in label.text() for label in dialog.findChildren(QLabel)))
                        dialog.bar_fg.setText('#25B3FF')
                    elif key == 'circle':
                        dialog.tick_count.setValue(30)
                        dialog.tick_size.setValue(7)
                    else:
                        if mode == 'bar_texture':
                            self.assertEqual(dialog.w.minimum(), 4)
                            self.assertEqual(dialog.h.minimum(), 4)
                        dialog.bar_fg.setText('#25B3FF')
                    dialog.apply()
                finally:
                    dialog.close()
                self.assertEqual((self.window.project[key]['x'], self.window.project[key]['y']), (15, 21))
                self.assertEqual(self.window.project['countdown_mode'], mode)
                if key == 'circle':
                    self.assertEqual(self.window.project[key]['num_ticks'], 30)
                    self.assertEqual(self.window.project[key]['tick_size'], 7)
                else:
                    self.assertEqual(self.window.project[key]['fg'], '#25B3FF')

    def test_preview_timer_updates_each_style_at_the_real_remaining_second_cadence(self):
        real_render = gui.render_preview_scaled
        for mode in VISIBLE:
            with self.subTest(mode=mode):
                self.set_mode_silently(mode)
                self.window.project['timeout'] = 3
                scheduled, frames = [], []
                def render(project, originals, width, height, **kwargs):
                    image = real_render(project, originals, width, height, **kwargs)
                    frames.append((kwargs['preview_fraction'], image.tobytes()))
                    return image
                with mock.patch.object(gui, 'render_preview_scaled', side_effect=render), \
                     mock.patch.object(gui.QTimer, 'singleShot', side_effect=lambda delay, callback: scheduled.append((delay, callback))):
                    self.window.preview_countdown()
                    while scheduled:
                        delay, callback = scheduled.pop(0)
                        self.assertIn(delay, (1000, 250))
                        callback()
                self.assertEqual([fraction for fraction, _ in frames[:4]], [0.0, 1/3.0, 2/3.0, 1.0])
                self.assertTrue(all(frames[index][1] != frames[index+1][1] for index in range(3)))

    def test_changing_style_or_restarting_preview_cancels_old_timer_callbacks(self):
        scheduled = []
        self.set_mode_silently('circle')
        with mock.patch.object(gui.QTimer, 'singleShot', side_effect=lambda delay, callback: scheduled.append((delay, callback))), \
             mock.patch.object(gui, 'render_preview_scaled', wraps=gui.render_preview_scaled) as render:
            self.window.preview_countdown()
            old = scheduled.pop(0)[1]
            self.window.preview_countdown()
            count = render.call_count
            old()
            self.assertEqual(render.call_count, count)
            current = scheduled.pop(0)[1]
            self.set_mode_silently('number')
            count = render.call_count
            current()
            self.assertEqual(render.call_count, count)


if __name__ == '__main__':
    unittest.main()
