# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
# -*- coding: utf-8 -*-
import copy
import unittest
from pathlib import Path
from PIL import Image

from animeboot_neo.project import default_project, normalize_project


class CanvasKeyboardRotationTests(unittest.TestCase):
    def test_rotation_defaults_and_migration(self):
        project = default_project(640, 360)
        self.assertNotIn("rotation", project["circle"])
        for key in ("logo", "company", "prompt"):
            self.assertEqual(float(project[key].get("rotation", 0)), 0.0)
        project["extra_images"].append({"id":"img_a", "x":1,"y":1,"w":10,"h":20,"path":""})
        project["text_boxes"].append({"id":"txt_a", "x":1,"y":1,"w":20,"h":10,"text":"x"})
        normalize_project(project)
        self.assertEqual(project["extra_images"][0]["rotation"], 0.0)
        self.assertEqual(project["text_boxes"][0]["rotation"], 0.0)

    def test_gui_has_keyboard_nudge_delete_and_rotation_property(self):
        gui = (Path(__file__).parents[1] / "animeboot_neo" / "qt_gui.py").read_text(encoding="utf-8")
        self.assertIn("def keyPressEvent(self, event):", gui)
        self.assertIn("Qt.Key_Delete", gui)
        self.assertIn("Qt.Key_Left", gui)
        self.assertIn("self._nudge_selected", gui)
        self.assertIn("旋转角度（正值顺时针）", gui)
        self.assertIn("def _selection_polygon(self, key=None):", gui)
        self.assertIn("self._scene.addPolygon(polygon, pen)", gui)
        self.assertIn("polygon.containsPoint(point, Qt.OddEvenFill)", gui)
        self.assertIn("实际帧率受动画来源与硬件性能影响", gui)
        self.assertNotIn("不再改变从源文件抽取多少帧", gui)

    def test_theme_uses_rotation_for_static_objects(self):
        theme = (Path(__file__).parents[1] / "animeboot_neo" / "theme.py").read_text(encoding="utf-8")
        self.assertIn("_composite_rotated_layer", theme)
        self.assertIn("layer.rotate(-angle, expand=True", theme)

    def test_arbitrary_rotation_renders_without_lanczos_error(self):
        from animeboot_neo.theme import _composite_rect
        canvas = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        source = Image.new("RGBA", (20, 10), (255, 0, 0, 255))
        _composite_rect(canvas, source, {"x": 40, "y": 45, "w": 20, "h": 10, "rotation": 33.0}, False)
        bbox = canvas.getchannel("A").getbbox()
        self.assertIsNotNone(bbox)
        self.assertGreater(bbox[2] - bbox[0], 20)
        self.assertGreater(bbox[3] - bbox[1], 10)


if __name__ == "__main__":
    unittest.main()
