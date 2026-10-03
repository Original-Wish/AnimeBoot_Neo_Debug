# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 原心
"""Compact scene controls which wrap their actions on narrow previews."""
from PySide2.QtCore import QTimer
from PySide2.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QPushButton


class CanvasManagerBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.outer=QVBoxLayout(self);self.outer.setContentsMargins(0,0,0,0);self.outer.setSpacing(4)
        self.primary=QHBoxLayout();self.primary.setContentsMargins(0,0,0,0);self.primary.setSpacing(5)
        self.outer.addLayout(self.primary)
        self.label=QLabel('当前画布：');self.primary.addWidget(self.label)
        self.selector=QComboBox();self.selector.setMinimumWidth(130)
        self.selector.setToolTip('分辨率和时长共用；每张画布保留各自对象及倒计时属性。多个画布按次顺序轮换。')
        self.primary.addWidget(self.selector,1)
        self.actions=QWidget();row=QHBoxLayout(self.actions);row.setContentsMargins(0,0,0,0);row.setSpacing(5)
        self.add_button=QPushButton('新增');self.copy_button=QPushButton('复制');self.delete_button=QPushButton('删除')
        for button in (self.add_button,self.copy_button,self.delete_button):
            button.setMinimumWidth(62);row.addWidget(button)
        self.primary.addWidget(self.actions)
        self.wrapped=False
        QTimer.singleShot(0,self._reflow)

    def _reflow(self):
        threshold=self.label.sizeHint().width()+130+self.actions.sizeHint().width()+20
        narrow=self.width()<threshold
        if narrow==self.wrapped:return
        if narrow:
            self.primary.removeWidget(self.actions);self.outer.addWidget(self.actions)
        else:
            self.outer.removeWidget(self.actions);self.primary.addWidget(self.actions)
        self.wrapped=narrow;self.actions.show();self.updateGeometry()

    def resizeEvent(self,event):
        super().resizeEvent(event);self._reflow()
