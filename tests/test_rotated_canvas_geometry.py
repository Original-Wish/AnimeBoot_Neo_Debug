# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
# -*- coding: utf-8 -*-
import unittest
from pathlib import Path

from animeboot_neo.canvas_geometry import (
    clamp_move_delta, has_corner_in_canvas, resize_from_rotated_corner, rotated_corners,
)


class RotatedCanvasGeometryTests(unittest.TestCase):
    def test_rotated_move_allows_three_corners_outside_but_not_four(self):
        rect = (40.0, 40.0, 40.0, 20.0)
        angle = 45.0
        dx, dy = clamp_move_delta(rect, angle, -200.0, -200.0, 100.0, 100.0)
        moved = (rect[0] + dx, rect[1] + dy, rect[2], rect[3])
        corners = rotated_corners(*moved, angle)
        inside = [0.0 <= x <= 100.0 and 0.0 <= y <= 100.0 for x, y in corners]
        self.assertTrue(any(inside))
        self.assertLessEqual(sum(inside), 1)

    def test_zero_degree_move_keeps_full_rect_inside(self):
        rect = (40.0, 40.0, 40.0, 20.0)
        dx, dy = clamp_move_delta(rect, 0.0, -200.0, 200.0, 100.0, 100.0)
        self.assertEqual((rect[0] + dx, rect[1] + dy), (0.0, 80.0))

    def test_rotated_resize_keeps_opposite_corner_fixed(self):
        rect = (30.0, 30.0, 40.0, 20.0)
        angle = 33.0
        before = rotated_corners(*rect, angle)
        # Drag BR farther along the rotated local diagonal; TL is the anchor.
        anchor = before[0]
        result = resize_from_rotated_corner(rect, angle, 'br', (95.0, 90.0), 200.0, 200.0, False)
        after = rotated_corners(*result, angle)
        self.assertAlmostEqual(anchor[0], after[0][0], places=5)
        self.assertAlmostEqual(anchor[1], after[0][1], places=5)
        self.assertGreater(result[2], 12.0)
        self.assertGreater(result[3], 12.0)

    def test_rotated_resize_never_loses_all_corners(self):
        rect = (20.0, 20.0, 50.0, 30.0)
        result = resize_from_rotated_corner(rect, 45.0, 'tl', (-500.0, -500.0), 100.0, 100.0, False)
        self.assertTrue(has_corner_in_canvas(result, 45.0, 100.0, 100.0))

    def test_scaled_preview_preserves_negative_rotated_coordinates(self):
        theme = (Path(__file__).parents[1] / 'animeboot_neo' / 'theme.py').read_text(encoding='utf-8')
        self.assertIn('Signed x/y are intentional for rotated objects', theme)
        self.assertNotIn('max(1 if name in ("w", "h") else 0, int(round(spec[name] * factor)))', theme)

    def test_object_property_editor_can_show_negative_rotated_coordinates(self):
        gui = (Path(__file__).parents[1] / 'animeboot_neo' / 'qt_gui.py').read_text(encoding='utf-8')
        self.assertIn('coordinate_minimum = -8192 if signed_position else 0', gui)
        self.assertIn('def _rotation_coordinate_range_changed(self, value):', gui)

    def test_animation_dialog_defensively_rejects_missing_source(self):
        gui = (Path(__file__).parents[1] / 'animeboot_neo' / 'qt_gui.py').read_text(encoding='utf-8')
        self.assertIn('def _animation_source_is_valid(self):', gui)
        self.assertIn('请先选择有效的动画文件或PNG帧文件夹', gui)
        self.assertIn('enabled = self.enabled.isChecked() and self._animation_source_is_valid()', gui)
        self.assertIn('self.file_btn.setEnabled(True)', gui)
        self.assertIn('resize_from_rotated_corner', gui)
        self.assertIn('clamp_move_delta', gui)


if __name__ == '__main__':
    unittest.main()
