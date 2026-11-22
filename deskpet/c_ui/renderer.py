"""Render the v1 flat UiSpec subset. Descriptions never execute code."""
from __future__ import annotations

import json
from typing import get_args
from uuid import uuid4

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QComboBox, QGroupBox, QLineEdit, QListWidget, QPushButton, QVBoxLayout, QWidget

from deskpet.contracts import UiAction, UiSpec
from .work_panel import label

ACTIONS = frozenset(get_args(UiAction.model_fields['action_id'].annotation))


def text(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


class UiSpecRenderer(QWidget):
    def __init__(self, *, submit, allowed_actions=None, parent=None):
        super().__init__(parent)
        self.submit = submit
        self.allowed_actions = ACTIONS if allowed_actions is None else ACTIONS & set(allowed_actions)
        self.spec = None
        self.widgets = {}
        self._slots = {}
        self._enabled = {}
        self._valid = False
        self.pending = None
        self.last_error = None
        layout = QVBoxLayout(self)
        self.status = label('等待界面数据…')
        layout.addWidget(self.status)
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        layout.addWidget(self.body)

    def render_spec(self, value):
        try:
            spec = UiSpec.model_validate(value.model_dump(mode='python') if isinstance(value, UiSpec) else value)
            ids, keys = set(), set()
            for c in spec.components:
                if not c.component_id or c.component_id in ids:
                    raise ValueError('component_id must be nonempty and unique')
                ids.add(c.component_id)
                if c.type == 'button' and c.action_id not in self.allowed_actions:
                    raise ValueError('unregistered action')
                if c.type != 'button' and c.action_id is not None:
                    raise ValueError('only buttons can bind actions')
                if c.type in ('text_input', 'select'):
                    if not c.data_key or c.data_key in keys:
                        raise ValueError('input data_key must be nonempty and unique')
                    keys.add(c.data_key)
                if c.type == 'select' and (not c.options or len(set(c.options)) != len(c.options)):
                    raise ValueError('select requires distinct options')
                if c.type == 'button' and c.data_key and not isinstance(spec.data.get(c.data_key), dict):
                    raise ValueError('button data_key must bind a payload object')
                if c.type == 'list' and not isinstance(spec.data.get(c.data_key, []), (list, tuple)):
                    raise ValueError('list requires an array')
            if self.spec and spec.surface_id == self.spec.surface_id and spec.surface_revision < self.spec.surface_revision:
                raise ValueError('stale surface revision')
            if self.spec and spec.surface_id == self.spec.surface_id and spec.surface_revision == self.spec.surface_revision and spec.components != self.spec.components:
                raise ValueError('structural changes require a new revision')
        except (ValueError, TypeError) as exc:
            self._valid = False
            self.last_error = str(exc)
            self.status.setText('此界面暂时不可用，请重新加载。')
            self._refresh_buttons()
            return False

        previous = self.spec
        saved = {}
        if previous and previous.surface_id == spec.surface_id:
            for c in previous.components:
                w = self.widgets[c.component_id]
                if c.type in ('text_input', 'select'):
                    saved[(c.component_id, c.type, c.data_key)] = w.text() if c.type == 'text_input' else w.currentText()
        rebuild = previous is None or previous.components != spec.components or previous.surface_id != spec.surface_id
        if rebuild:
            for w in self.widgets.values():
                w.setEnabled(False)
            self.layout().removeWidget(self.body)
            self.body.hide()
            self.body.deleteLater()
            self.body = QWidget()
            self.body_layout = QVBoxLayout(self.body)
            self.layout().addWidget(self.body)
            self.widgets, self._slots = {}, {}
            for c in spec.components:
                if c.type == 'text':
                    w = label('')
                elif c.type == 'card':
                    w = QGroupBox(c.label)
                    box = QVBoxLayout(w)
                    w.content_label = label('')
                    box.addWidget(w.content_label)
                elif c.type == 'text_input':
                    w = QLineEdit()
                    w.setMaxLength(500)
                    w.setPlaceholderText(c.label)
                elif c.type == 'select':
                    w = QComboBox()
                    w.addItems(c.options)
                    w.setAccessibleName(c.label)
                elif c.type == 'button':
                    w = QPushButton(c.label)
                else:
                    w = QListWidget()
                    w.setMaximumHeight(100)
                w.setObjectName('c-dynamic-' + c.component_id)
                self.widgets[c.component_id] = w
                self.body_layout.addWidget(w)
        self.spec, self._valid, self.last_error = spec, True, None
        for c in spec.components:
            w = self.widgets[c.component_id]
            value = spec.data.get(c.data_key, '') if c.data_key else c.label
            if c.type == 'text':
                w.setText(text(value))
            elif c.type == 'card':
                w.content_label.setText(text(value))
            elif c.type == 'list':
                entries = [text(v) for v in spec.data.get(c.data_key, [])]
                if [w.item(i).text() for i in range(w.count())] != entries:
                    w.clear()
                    w.addItems(entries)
            elif c.type in ('text_input', 'select'):
                desired = saved.get((c.component_id, c.type, c.data_key), str(value))
                if c.type == 'text_input' and w.text() != desired:
                    w.setText(desired)
                elif c.type == 'select':
                    w.setCurrentIndex(max(0, w.findText(desired)))
            elif c.type == 'button':
                if c.component_id in self._slots:
                    w.clicked.disconnect(self._slots[c.component_id])
                token = (spec.surface_id, spec.surface_revision)
                slot = lambda checked=False, key=c.component_id, t=token: self._dispatch(t, key)
                w.clicked.connect(slot)
                self._slots[c.component_id] = slot
        self._refresh_buttons()
        if previous is None or self.status.text() == '此界面暂时不可用，请重新加载。':
            self.status.setText('可查看状态，或明确设置临时纠正。')
        return True

    def enable_action(self, action_id, enabled):
        self._enabled[action_id] = enabled
        self._refresh_buttons()

    def _refresh_buttons(self):
        if not self.spec:
            return
        for c in self.spec.components:
            if c.type == 'button':
                self.widgets[c.component_id].setEnabled(self._valid and self.pending is None and self._enabled.get(c.action_id, True))

    def _dispatch(self, token, component_id):
        if not self.spec or not self._valid or self.pending is not None or token != (self.spec.surface_id, self.spec.surface_revision):
            return
        component = next((c for c in self.spec.components if c.component_id == component_id), None)
        if component is None or component.type != 'button' or not self._enabled.get(component.action_id, True):
            return
        payload = dict(self.spec.data[component.data_key]) if component.data_key else {}
        # Clear actions use their empty payload; form values belong to other actions.
        if component.action_id != 'state.clear_override':
            for c in self.spec.components:
                if c.type in ('text_input', 'select'):
                    w = self.widgets[c.component_id]
                    payload[c.data_key] = w.text() if c.type == 'text_input' else w.currentText()
        action = UiAction(event_id=f'c-dynamic-{uuid4().hex}', action_id=component.action_id,
            surface_revision=self.spec.surface_revision, task_id=self.spec.task_id,
            expected_task_version=self.spec.task_version, payload=payload)
        try:
            self.submit(action)
        except (ValueError, RuntimeError) as exc:
            self.status.setText(str(exc))
            return
        self.pending = action.event_id
        self.status.setText('正在提交…')
        self._refresh_buttons()

    def receive_result(self, result):
        if self.pending is None or result.get('command_id', result.get('event_id')) != self.pending:
            return
        self.pending = None
        status = result.get('status')
        messages = {'correction_applied': '临时状态已设置。', 'correction_cleared': '已清除纠正，恢复当前估计。', 'applied': '操作已完成。'}
        self.status.setText(messages.get(status, '操作未成功，输入已保留，请查看最新状态后重试。'))
        self._refresh_buttons()
