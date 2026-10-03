# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
"""Editable playlist with asynchronous, selected-codec capacity approval."""

# 此窗口编辑私有项目副本，只有“确定保存”才向主窗口提交 boot_animation；取消不会改变工程。
# 结构检查与资源检查分开：失效资源不能播放，但不能阻止用户关闭轮换、删除条目或保存关闭状态。
# 容量估算放在 QThread；信号回到 GUI 线程更新界面。工作线程运行时不能销毁窗口或修改素材列表。
# approved_signature 只证明对应输入曾通过抽样容量估算，不替代部署前的实际文件大小和空闲空间检查。

import copy, os
from PySide2.QtCore import Qt, QThread, QTimer
from PySide2.QtGui import QColor, QFontMetrics
from PySide2.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QCheckBox, QLabel,
    QListWidget, QListWidgetItem, QPushButton, QFileDialog, QDialogButtonBox,
    QScrollArea, QSizePolicy, QFrame, QMessageBox)
from .rotation import validate_rotation, estimate_parameters, estimate_signature


class RotationDialog(QDialog):
    def __init__(self, project, parent=None, estimate_cache=None):
        super().__init__(parent)
        self.setWindowTitle('多动画顺序轮换')
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self.resize(720, 580)
        self.project = copy.deepcopy(project)
        self.result_spec = None
        self.thread = None; self.worker = None
        self.approved_signature = None; self.running_signature = None
        self.enable_requested = False; self.last_result = None
        layout = QVBoxLayout(self)
        self.enabled = QCheckBox('启用顺序轮换')
        self.enabled.setChecked(bool(project['boot_animation'].get('rotation_enabled')))
        layout.addWidget(self.enabled)
        hint = QLabel('主动画始终为第一段；最多8段（含主动画）。所有动画共用当前编码、分辨率、抽帧和播放参数。\n'
                      '按次顺序轮换；Esc 跳过及播放后的重启不推进动图序号。重新制作后从主动画开始。')
        hint.setWordWrap(True);layout.addWidget(hint)
        self.primary_label = QLabel();self.primary_label.setWordWrap(True);layout.addWidget(self.primary_label)
        self.listing = QListWidget();layout.addWidget(self.listing, 1)
        for item in project['boot_animation'].get('playlist', []):self._append(item['source'])
        row = QHBoxLayout();layout.addLayout(row)
        self.edit_buttons = []
        actions = [('添加文件', lambda:self.add_source(False)),
                   ('添加PNG帧文件夹', lambda:self.add_source(True)),
                   ('删除', self.delete_source), ('上移', lambda:self.move_source(-1)),
                   ('下移', lambda:self.move_source(1))]
        for text, callback in actions:
            button = QPushButton(text)
            button.setMinimumWidth(QFontMetrics(button.font()).horizontalAdvance(text)+30)
            button.clicked.connect(callback);row.addWidget(button);self.edit_buttons.append(button)
        row.addStretch()
        self.estimate_button = QPushButton('估算全部动画占用（仅当前编码）')
        self.estimate_button.clicked.connect(self.start_estimate);layout.addWidget(self.estimate_button)
        self.information = QLabel();self.information.setWordWrap(True)
        layout.addWidget(self.information)
        self.capacity = QLabel('尚未估算。勾选“启用顺序轮换”时会自动估算主动画及列表中所有动画，并读取启动分区空间。')
        self.capacity.setWordWrap(True);self.capacity.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.capacity.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
        capacity_scroll=QScrollArea();capacity_scroll.setWidgetResizable(True)
        capacity_scroll.setFrameShape(QFrame.NoFrame);capacity_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        capacity_scroll.setMinimumHeight(156);capacity_scroll.setMaximumHeight(252)
        capacity_scroll.setWidget(self.capacity);layout.addWidget(capacity_scroll)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText('确定保存')
        self.buttons.button(QDialogButtonBox.Cancel).setText('取消')
        self.buttons.accepted.connect(self.apply);self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.enabled.toggled.connect(self._enable_toggled)
        self._refresh_resources()
        # 估算是素材与编码/画布设置的结果，不是“勾选”这个动作的结果。
        # 缓存只保存在本次程序进程中，不写注册表、工程或分享包；源文件
        # 大小/修改时间参与签名，删改素材后不会沿用容量批准结果。
        if estimate_cache and estimate_cache.get('signature') == estimate_signature(self.candidate()):
            self.running_signature=estimate_cache['signature']
            self._estimated(*copy.deepcopy(estimate_cache['result']))
        if self.enabled.isChecked():QTimer.singleShot(0, self._check_existing)

    def _append(self, source):
        item=QListWidgetItem();item.setData(Qt.UserRole, source);self.listing.addItem(item)

    def candidate(self):
        result=copy.deepcopy(self.project)
        result['boot_animation']['playlist']=[{'source':self.listing.item(i).data(Qt.UserRole)}
                                            for i in range(self.listing.count())]
        return result

    def _refresh_resources(self):
        primary=self.project['boot_animation'].get('source','')
        valid=bool(primary and os.path.exists(primary))
        self.primary_label.setText('主动画：'+(primary or '尚未选择，请先在动画设置中添加')+
            (' 【已失效，请在动画设置中重新选择】' if primary and not valid else
             '（尚未启用主动画）' if valid and not self.project['boot_animation'].get('enabled') else ''))
        self.primary_label.setStyleSheet('color:#b45b30;' if primary and not valid else '')
        missing=[]
        for i in range(self.listing.count()):
            item=self.listing.item(i);source=item.data(Qt.UserRole)
            present=bool(source and os.path.exists(source))
            item.setText(('【已失效】 ' if not present else '')+source)
            item.setToolTip(source)
            item.setForeground(QColor('#b45b30') if not present else self.palette().text().color())
            if not present:missing.append(source)
        self.information.setText('存在失效素材。可以删除、重新选择，或关闭轮换后保存；这不会关闭主动画。' if missing or (primary and not valid)
                                 else '尚未选择主动画。可以先整理轮换列表，选择主动画后再估算启用。' if not primary
                                 else '估算包含主动画及全部轮换动画；素材或制作参数不变时可复用结果。')

    def _set_checked(self, checked):
        self.enabled.blockSignals(True);self.enabled.setChecked(checked);self.enabled.blockSignals(False)

    def _edited(self):
        self.approved_signature=None;self.enable_requested=False;self._set_checked(False)
        self.capacity.setText('列表已修改，请重新估算后启用顺序轮换。')
        self._refresh_resources()

    def add_source(self, folder):
        if self.listing.count()>=7:
            self.information.setText('最多8段动画（含主动画），请先删除一段。');return
        if folder:path=QFileDialog.getExistingDirectory(self,'添加PNG帧文件夹')
        else:path,_=QFileDialog.getOpenFileName(self,'添加动画文件','','动画 (*.gif *.webp *.apng *.mp4 *.mkv *.webm *.avi *.mov *.m4v *.mpg *.mpeg)')
        if path:self._append(path);self._edited()

    def delete_source(self):
        index=self.listing.currentRow()
        if index>=0:self.listing.takeItem(index);self._edited()

    def move_source(self, delta):
        index=self.listing.currentRow();target=index+delta
        if 0<=target<self.listing.count() and index>=0:
            item=self.listing.takeItem(index);self.listing.insertItem(target,item)
            self.listing.setCurrentRow(target);self._edited()

    def _check_existing(self):
        if self.enabled.isChecked():self._enable_toggled(True)

    def _enable_toggled(self, checked):
        if not checked:
            self.enable_requested=False
            self.information.setText('轮换已关闭，可直接保存。失效素材不会阻止关闭轮换。')
            return
        if not self.listing.count():
            self._set_checked(False)
            self.information.setText('请先添加至少一段轮换动画，连同主动画组成顺序列表。')
            return
        candidate=self.candidate()
        if self.approved_signature and self.approved_signature==estimate_signature(candidate):
            self.information.setText('已复用通过的估算结果，确定保存后生效。');return
        self.enable_requested=True;self.start_estimate()

    def start_estimate(self):
        if self.thread is not None:return
        candidate=self.candidate()
        candidate['boot_animation'].update(enabled=True,rotation_enabled=bool(candidate['boot_animation']['playlist']))
        try:
            validate_rotation(candidate)
            signature=estimate_signature(candidate)
        except ValueError as error:
            self._set_checked(False);self.enable_requested=False
            self.capacity.setText('尚未启用轮换：'+str(error));self._refresh_resources();return
        from .qt_gui import CodecEstimateWorker
        parameters=estimate_parameters(candidate,True)
        parameters['static_project']=candidate
        self.running_signature=signature
        self.thread=QThread(self);self.worker=CodecEstimateWorker(parameters,32)
        self.worker.moveToThread(self.thread);self.thread.started.connect(self.worker.run)
        self.worker.finished.connect(self._estimated);self.worker.failed.connect(self._failed)
        self.worker.progress.connect(self.capacity.setText)
        self.worker.finished.connect(self.thread.quit);self.worker.failed.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater);self.worker.failed.connect(self.worker.deleteLater)
        self.thread.finished.connect(self._finished);self.thread.finished.connect(self.thread.deleteLater)
        for button in self.edit_buttons+[self.estimate_button]:button.setEnabled(False)
        self.buttons.setEnabled(False)
        self.capacity.setText('正在读取启动分区空间并后台估算全部动画，仅处理当前所选编码…\n轮换在估算通过并确定保存后生效；可随时取消勾选。')
        self.thread.start()

    @staticmethod
    def _size(value):return '%.2f MiB' % (int(value)/float(1024*1024))

    def _estimated(self, result, space):
        candidate=self.candidate();signature=estimate_signature(candidate)
        if signature!=self.running_signature:
            self._failed('估算过程中素材发生变化，请重新估算。');return
        codec=candidate['boot_animation'].get('codec','jpeg')
        available=int(space.get('replaceable_bytes',space.get('free_bytes',0)) or 0)
        needed=int(result.get(codec+'_total_boot_bytes',0))
        reserve=8*1024*1024
        lines=[]
        for i,item in enumerate(result.get('items',[])):
            lines.append('%s：%s，%d 帧，预计 %s' % ('主动画' if i==0 else '轮换 %d'%i,
                os.path.basename(item['source'].rstrip('/\\')),item.get('planned_frame_count',0),self._size(item.get(codec+'_bytes',0))))
        lines += ['编码：%s；全部动画合计 %s；含启动组件/全部静图后预计总占用 %s。' %
                  (codec.upper(),self._size(result.get(codec+'_bytes',0)),self._size(needed)),
                  '启动分区：空闲 %s + 净可回收 %s = 可替换空间 %s；预留余量 %s。' %
                  (self._size(space.get('free_bytes',0)),self._size(space.get('reclaimable_bytes',0)),
                   self._size(available),self._size(reserve)),
                  '抽样估算会有偏差；制作后仍会按实际文件大小检查空间。']
        sufficient=bool(space.get('ok') and needed>0 and needed+reserve<=available)
        if sufficient:
            self.approved_signature=signature
            lines.append('估算通过。'+('可启用顺序轮换。' if self.listing.count() else '请再添加至少一段动画以启用轮换。'))
            if self.enable_requested and self.listing.count():self._set_checked(True)
        else:
            self.approved_signature=None;self._set_checked(False)
            lines.append('空间不足或容量未确认，轮换未启用；请调整动画参数或素材后重新估算。')
        self.capacity.setText('\n'.join(lines));self.last_result=(result,space)

    def _failed(self, message):
        self.approved_signature=None;self._set_checked(False)
        self.capacity.setText('估算未完成，轮换未启用：'+str(message))

    def _finished(self):
        self.thread=None;self.worker=None
        for button in self.edit_buttons+[self.estimate_button]:button.setEnabled(True)
        self.buttons.setEnabled(True)

    def estimate_cache(self):
        """仅返回成功且仍对应当前素材的估算；安装阶段另查实际分区容量。"""
        if self.approved_signature and self.last_result and self.approved_signature==estimate_signature(self.candidate()):
            return {'signature':self.approved_signature,'result':copy.deepcopy(self.last_result)}
        return None

    def _confirm_disabled(self):
        message=QMessageBox(self)
        message.setIcon(QMessageBox.Information);message.setWindowTitle('顺序轮换尚未启用')
        message.setText('您已添加轮换动画，但尚未勾选“启用顺序轮换”。\n\n'
                        '选择“现在启用”会复用有效估算或自动估算；选择“保持关闭并保存”只保存列表，不启用轮换。')
        enable=message.addButton('现在启用',QMessageBox.AcceptRole)
        disabled=message.addButton('保持关闭并保存',QMessageBox.ActionRole)
        message.addButton('返回设置',QMessageBox.RejectRole)
        message.setDefaultButton(enable);message.exec_()
        if message.clickedButton() is enable:return 'enable'
        return 'disabled' if message.clickedButton() is disabled else 'back'

    def apply(self):
        if self.thread is not None:return
        candidate=self.candidate();spec=candidate['boot_animation']
        spec['rotation_enabled']=self.enabled.isChecked()
        if not spec['playlist']:spec['rotation_enabled']=False
        try:
            # Structural-only validation permits disabling/removing stale files.
            validate_rotation(candidate,check_resources=spec['rotation_enabled'])
            if spec['rotation_enabled'] and self.approved_signature!=estimate_signature(candidate):
                self.enable_requested=True;self.start_estimate();return
        except ValueError as error:
            self._set_checked(False);self.capacity.setText(str(error));self._refresh_resources();return
        if spec['playlist'] and not spec['rotation_enabled']:
            choice=self._confirm_disabled()
            if choice=='enable':
                self.enabled.setChecked(True)
                # 异步估算完成后仍由用户确认保存，失败时保留可编辑窗口。
                if self.thread is None and self.enabled.isChecked():self.apply()
                return
            if choice!='disabled':return
        self.result_spec=copy.deepcopy(spec);self.accept()

    def reject(self):
        if self.thread is None:super().reject()

    def closeEvent(self,event):
        if self.thread is not None:event.ignore()
        else:super().closeEvent(event)
