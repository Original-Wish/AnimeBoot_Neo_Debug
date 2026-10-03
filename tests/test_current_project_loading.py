# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
# -*- coding: utf-8 -*-
import os
import tempfile
import unittest

from animeboot_neo.project import default_project, load_project, save_project


class CurrentProjectLoadingTests(unittest.TestCase):
    def test_missing_external_images_are_pruned_but_project_loads(self):
        project = default_project()
        project["logo"]["path"] = os.path.join(tempfile.gettempdir(), "missing-logo-for-animebootneo.png")
        project["extra_images"].append({
            "id": "img_missing", "path": os.path.join(tempfile.gettempdir(), "missing-extra-for-animebootneo.png"),
            "x": 1, "y": 2, "w": 30, "h": 40, "visible": True, "source_builtin": "",
        })
        project["layer_order"].extend(["extra:img_missing"])
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "demo.anbp")
            save_project(project, path)
            loaded, missing = load_project(path, tolerate_missing_images=True, return_missing_images=True)
        self.assertEqual(len(missing), 2)
        self.assertTrue(loaded["logo"]["removed"])
        self.assertFalse(loaded["logo"]["visible"])
        self.assertEqual(loaded["extra_images"], [])
        self.assertNotIn("extra:img_missing", loaded["layer_order"])

    def test_missing_enabled_animation_is_cleared_and_disabled_but_project_loads(self):
        project = default_project()
        missing_video = os.path.join(tempfile.gettempdir(), "missing-animation-for-animebootneo.mp4")
        project["boot_animation"].update({
            "enabled": True,
            "source": missing_video,
            "codec": "jpeg",
            "jpeg_quality": 91,
            "frame_rate": 120,
        })
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "missing-animation.anbp")
            save_project(project, path)
            loaded, images, animation = load_project(
                path,
                tolerate_missing_images=True,
                return_missing_images=True,
                tolerate_missing_animation=True,
                return_missing_animation=True,
            )
        self.assertEqual(images, [])
        self.assertTrue(animation["was_enabled"])
        self.assertEqual(animation["path"], missing_video)
        self.assertFalse(loaded["boot_animation"]["enabled"])
        self.assertEqual(loaded["boot_animation"]["source"], "")
        self.assertEqual(loaded["boot_animation"]["jpeg_quality"], 91)
        self.assertEqual(loaded["boot_animation"]["frame_rate"], 120)

    def test_missing_disabled_animation_source_is_cleared_without_enabling_or_rejecting(self):
        project = default_project()
        missing_folder = os.path.join(tempfile.gettempdir(), "missing-png-frames-for-animebootneo")
        project["boot_animation"].update({
            "enabled": False,
            "source": missing_folder,
            "codec": "qoi",
            "frame_rate": 30,
        })
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "missing-disabled-animation.anbp")
            save_project(project, path)
            loaded, animation = load_project(
                path, tolerate_missing_animation=True, return_missing_animation=True
            )
        self.assertFalse(animation["was_enabled"])
        self.assertFalse(loaded["boot_animation"]["enabled"])
        self.assertEqual(loaded["boot_animation"]["source"], "")
        self.assertEqual(loaded["boot_animation"]["codec"], "qoi")
        self.assertEqual(loaded["boot_animation"]["frame_rate"], 30)

    def test_default_strict_loading_still_rejects_missing_image(self):
        project = default_project()
        project["company"]["path"] = os.path.join(tempfile.gettempdir(), "missing-company-for-animebootneo.png")
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "strict.anbp")
            save_project(project, path)
            with self.assertRaises(Exception):
                load_project(path)


if __name__ == "__main__":
    unittest.main()
