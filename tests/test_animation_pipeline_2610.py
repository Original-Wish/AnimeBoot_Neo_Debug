# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
# -*- coding: utf-8 -*-
import os
from pathlib import Path
import struct
import tempfile
import threading
import time
import types
import unittest
from unittest import mock
from PIL import Image
from animeboot_neo import animation as a, archive
from animeboot_neo.project import default_project


class PipelineTests(unittest.TestCase):
    def project(self, root, codec='jpeg'):
        folder=Path(root)/'input';folder.mkdir()
        for i in range(7):
            image=Image.new('RGBA',(80,48),(i*30,130,255-i*30,100+i*20))
            image.save(folder/('%03d.png'%i))
        project=default_project(64,48)
        project['boot_animation'].update(enabled=True,source=str(folder),codec=codec,
            max_frames=7,frame_rate=30,extract_frame_rate=30,fit_mode='stretch',jpeg_quality=90)
        return project

    def test_jpeg_pipeline_preserves_serial_bytes_and_uses_native_threads(self):
        with tempfile.TemporaryDirectory() as tmp:
            project=self.project(tmp);threads=set();original=a._fit_frame
            def fit(*args):
                threads.add(threading.get_ident());time.sleep(0.01)
                return original(*args)
            with mock.patch.object(a,'_png_preview_worker_count',return_value=3),mock.patch.object(a,'_fit_frame',side_effect=fit),mock.patch.object(a,'_encode_jpeg_files_parallel',side_effect=AssertionError('must not reread PNG')):
                path,report=a.render_linux_animation_payload_file(project,frame_export_dir=str(Path(tmp)/'preview'),temp_dir=tmp)
            data=Path(path).read_bytes();pos=44
            for source in sorted((Path(tmp)/'input').glob('*.png')):
                with Image.open(source) as im:
                    fitted=original(im,64,48,'stretch');expected=a.encode_jpeg_rgb(fitted,90);fitted.close()
                size=struct.unpack_from('<I',data,pos)[0];pos+=4
                self.assertEqual(data[pos:pos+size],expected);pos+=size
            self.assertEqual(pos,len(data));self.assertEqual(report['frame_count'],7)
            self.assertGreater(len(threads),1)
            self.assertEqual(len(list((Path(tmp)/'preview').glob('*.png'))),7)

    def test_no_png_intermediate_when_jpeg_preview_not_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            project=self.project(tmp)
            seen=[];original=Image.Image.save
            def save(image, *args, **kwargs):
                seen.append(kwargs.get('format'))
                self.assertNotEqual(kwargs.get('format'),'PNG')
                return original(image,*args,**kwargs)
            with mock.patch.object(Image.Image,'save',new=save):
                path,report=a.render_linux_animation_payload_file(project,temp_dir=tmp)
            self.assertEqual(seen,['JPEG']*7);self.assertEqual(report['frame_count'],7)
            self.assertTrue(Path(path).is_file())

    def test_cancel_cleans_partially_processed_frames(self):
        with tempfile.TemporaryDirectory() as tmp:
            project=self.project(tmp);calls=[0]
            def cancel():calls[0]+=1;return calls[0]>=5
            with self.assertRaises(a.AnimationError):
                a.render_linux_animation_payload_file(project,temp_dir=tmp,frame_export_dir=str(Path(tmp)/'preview'),cancel_check=cancel)
            self.assertFalse((Path(tmp)/'preview').exists())
            self.assertFalse(list(Path(tmp).glob('*.anbf')))

    def test_chunked_parallel_output_restores_exact_stream(self):
        with tempfile.TemporaryDirectory() as tmp:
            path,report=a.render_linux_animation_payload_file(self.project(tmp),temp_dir=tmp)
            target=str(Path(tmp)/'animation.jcx');messages=[]
            result=archive._build_jpeg_chunked_xz_container(archive.FileSlice(path),target,chunk_frames=4,progress=messages.append)
            archive._verify_jpeg_chunked_xz_matches_source(target,report['payload_size'],report['payload_sha256'])
            self.assertEqual(result['chunk_count'],3);self.assertEqual(len(messages),3)
            with self.assertRaises(archive.InitramfsPatchError):
                archive._build_jpeg_chunked_xz_container(archive.FileSlice(path),target,chunk_frames=4,cancel_check=lambda:True)

    def test_log_scrolls_after_user_scrolled_up(self):
        os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
        try:
            from PySide2.QtWidgets import QApplication
            from animeboot_neo.qt_gui import BuildLogEdit,MainWindow
        except ImportError:
            self.skipTest('PySide2 required')
        app=QApplication.instance() or QApplication([])
        widget=BuildLogEdit();widget.resize(250,100);widget.show()
        target=types.SimpleNamespace(debug_output=widget)
        for i in range(80):MainWindow._debug_log(target,'line %d'%i)
        app.processEvents();bar=widget.verticalScrollBar();bar.setValue(0)
        MainWindow._debug_log(target,'latest');app.processEvents()
        self.assertGreater(bar.maximum(),0);self.assertEqual(bar.value(),bar.maximum())
        widget.close()

    def test_log_keeps_held_scrollbar_position_and_resumes_after_release(self):
        os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
        try:
            from PySide2.QtCore import QPoint,Qt
            from PySide2.QtTest import QTest
            from PySide2.QtWidgets import QApplication,QStyle,QStyleOptionSlider
            from animeboot_neo.qt_gui import BuildLogEdit,MainWindow
        except ImportError:
            self.skipTest('PySide2 required')
        app=QApplication.instance() or QApplication([])
        widget=BuildLogEdit();widget.resize(250,150);widget.show()
        target=types.SimpleNamespace(debug_output=widget)
        for i in range(150):MainWindow._debug_log(target,'line %d'%i)
        app.processEvents();bar=widget.verticalScrollBar()
        option=QStyleOptionSlider();bar.initStyleOption(option)
        thumb=bar.style().subControlRect(QStyle.CC_ScrollBar,option,QStyle.SC_ScrollBarSlider,bar)
        QTest.mousePress(bar,Qt.LeftButton,Qt.NoModifier,thumb.center())
        self.assertTrue(bar.isSliderDown())
        # Keep appending while the user is holding the thumb at its original
        # bottom position, then at a manually dragged position farther up.
        position=bar.value()
        MainWindow._debug_log(target,'during hold at bottom');app.processEvents()
        self.assertEqual(bar.value(),position)
        QTest.mouseMove(bar,QPoint(thumb.center().x(),bar.height()//3))
        bar.setSliderPosition(bar.maximum()//3)
        position=bar.value()
        MainWindow._debug_log(target,'during drag');app.processEvents()
        self.assertEqual(bar.value(),position)
        self.assertIn('during drag',widget.toPlainText())
        QTest.mouseRelease(bar,Qt.LeftButton,Qt.NoModifier,QPoint(thumb.center().x(),bar.height()//3))
        MainWindow._debug_log(target,'after release');app.processEvents()
        self.assertEqual(bar.value(),bar.maximum())
        widget.close()


if __name__=='__main__':unittest.main()
