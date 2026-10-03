# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
# -*- coding: utf-8 -*-
import json
from pathlib import Path
import struct
import tempfile
import unittest
import subprocess
from unittest import mock
from PIL import Image
from animeboot_neo import animation as a
from animeboot_neo.project import default_project


class FrameProcessTests(unittest.TestCase):
    def test_small_video_keeps_fused_path_without_source_png_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); video=root/'small.mp4'
            ffmpeg=a._find_ffmpeg()
            if not ffmpeg:self.skipTest('FFmpeg required')
            subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-f','lavfi','-i',
                'testsrc2=size=160x96:rate=30','-frames:v','4','-c:v','libx264','-preset','ultrafast',
                '-y',str(video)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            project=default_project(128,96)
            project['boot_animation'].update(enabled=True,source=str(video),codec='jpeg',
                max_frames=4,extract_frame_rate=30,frame_rate=30,fit_mode='stretch')
            with mock.patch.object(a,'_process_frame_files',side_effect=AssertionError('extra source PNG I/O')):
                path,report=a.render_linux_animation_payload_file(project,temp_dir=tmp)
            self.assertEqual(report['frame_count'],4)
            self.assertEqual(report['frame_worker_kind'],'native_threads')
            self.assertEqual(report['video_extraction']['extraction_backend'],'ffmpeg_raw_rgba_pipe')
            self.assertFalse(list(root.glob('animebootneo_source_frames_*')))
            self.assertTrue(Path(path).is_file())

    def test_4k_pool_uses_independent_memory_budget(self):
        with mock.patch.object(a.os, 'cpu_count', return_value=12), \
             mock.patch.object(a.os, 'name', 'posix'), \
             mock.patch.object(a, '_available_worker_memory_limit', return_value=16):
            workers, budget = a._frame_process_worker_count(60, 3840*2160, 3840*2160)
        self.assertGreater(workers, 2)
        self.assertLessEqual(workers, 11)
        self.assertGreater(budget, 128)

    def test_real_child_pipeline_preserves_jpeg_qoi_and_preview(self):
        for codec in ('jpeg', 'qoi'):
            with self.subTest(codec=codec), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); source = root/'source'; source.mkdir()
                for index in range(3):
                    image = Image.new('RGBA', (1920,1080), (index*80,99,211,80+index*70))
                    image.save(source/('%d.png' % index)); image.close()
                project = default_project(64,48)
                project['boot_animation'].update(enabled=True, source=str(source), codec=codec,
                    max_frames=3, frame_rate=30, extract_frame_rate=30, fit_mode='contain', jpeg_quality=90)
                with mock.patch.object(a, '_frame_process_worker_count', return_value=(2,128)), \
                     mock.patch.object(a, '_load_frames', side_effect=AssertionError('must use file jobs')):
                    path, report = a.render_linux_animation_payload_file(project,
                        frame_export_dir=str(root/'preview'), temp_dir=tmp)
                self.assertEqual(report['frame_worker_kind'],'independent_processes')
                self.assertEqual(report['frame_pipeline_queue_limit'],0)
                self.assertEqual(report['build_'+codec+'_workers'],2)
                data = Path(path).read_bytes(); pos = 44
                for index, source_path in enumerate(sorted(source.glob('*.png'))):
                    with Image.open(source_path) as image:
                        fitted = a._fit_frame(image,64,48,'contain')
                    expected = a.encode_jpeg_rgb(fitted,90) if codec=='jpeg' else a.encode_qoi_rgba(fitted)
                    size = struct.unpack_from('<I',data,pos)[0]; pos+=4
                    self.assertEqual(data[pos:pos+size],expected); pos+=size
                    with Image.open(root/'preview'/('frame_%05d.png' % (index+1))) as preview:
                        self.assertEqual(preview.tobytes(),fitted.tobytes())
                    fitted.close()
                self.assertEqual(pos,len(data))
                self.assertFalse(list(root.glob('animebootneo_*_frames_*')))

    def test_failed_child_is_terminated_and_manifest_cleaned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); image=root/'source.png'; Image.new('RGBA',(16,12)).save(image)
            encoded=root/'encoded'; encoded.mkdir(); preview=root/'preview'; preview.mkdir()
            command=[a.sys.executable,'-c','import sys; sys.exit(2)']
            processes=[]; original=a.subprocess.Popen
            def start(*args,**kwargs):
                child=original(*args,**kwargs); processes.append(child); return child
            with mock.patch.object(a,'_frame_worker_command',return_value=command), \
                 mock.patch.object(a.subprocess,'Popen',side_effect=start):
                with self.assertRaises(a.AnimationError):
                    a._process_frame_files([str(image)],16,12,'stretch','jpeg',90,
                        str(preview),str(encoded))
            self.assertTrue(all(child.poll() is not None for child in processes))
            self.assertFalse(list(encoded.glob('pipeline_*.json')))

    def test_cancellation_stops_active_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); image=root/'source.png'; Image.new('RGBA',(16,12)).save(image)
            encoded=root/'encoded'; encoded.mkdir()
            processes=[]; original=a.subprocess.Popen
            def start(*args,**kwargs):
                child=original(*args,**kwargs); processes.append(child); return child
            command=[a.sys.executable,'-c','import time; time.sleep(60)']
            with mock.patch.object(a,'_frame_worker_command',return_value=command), \
                 mock.patch.object(a.subprocess,'Popen',side_effect=start):
                with self.assertRaises(a.AnimationError):
                    a._process_frame_files([str(image)],16,12,'stretch','jpeg',90,'',
                        str(encoded),cancel_check=lambda:bool(processes))
            self.assertTrue(all(child.poll() is not None for child in processes))
            self.assertFalse(list(encoded.glob('pipeline_*.json')))


if __name__=='__main__': unittest.main()
