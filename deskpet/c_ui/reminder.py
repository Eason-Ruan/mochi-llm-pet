"""C-owned reminder controls and receipt-aware cards; B remains task authority."""
from __future__ import annotations

from datetime import datetime, timedelta
from functools import partial
from uuid import uuid4

from PyQt6.QtCore import QDateTime, QSignalBlocker, Qt, QTimer, QTimeZone, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractSpinBox, QComboBox, QDateEdit, QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPushButton, QSizePolicy, QSpinBox, QTimeEdit, QVBoxLayout, QWidget,
)

from deskpet.contracts import TaskDraft
from deskpet.timezones import resolve_timezone
from .work_panel import label
from .refine_style import GREEN, MUTED, MenuField, Toggle, _card, _text
from .time_format import ZONE_NAME, format_time, local_time

ERRORS = {
    "STALE_TASK": "事项已更新，请查看最新安排后重试。",
    "STALE_SURFACE": "这张卡片已失效，请使用当前事项。",
    "CANCELLED": "事项已取消。", "NOT_FOUND": "事项不存在。",
    "INVALID_ARGUMENT": "请检查事项和时间输入。",
    "INVALID_TRANSITION": "当前状态不支持此操作。",
}


def display_time(value, reference=None, *, zone=None):
    return format_time(value, reference, zone=zone)


def schedule_text(draft, reference=None, *, zone=None):
    show_time = partial(display_time, reference=reference, zone=zone)
    start = draft.get("remind_after") or draft.get("earliest_at")
    deadline = draft.get("remind_by") or draft.get("latest_first_delivery_at")
    parts = []
    if not draft.get("reminder_enabled", True):
        parts.append("未设置提醒")
    elif draft["mode"] == "hard" and deadline:
        parts.append("准点提醒 " + show_time(deadline))
    elif draft.get("latest_first_delivery_at") and draft.get("earliest_at") and not draft.get("remind_by"):
        parts.append("柔性提醒窗口 " + show_time(start) + " 至 " + show_time(deadline))
    else:
        parts.append("择机提醒")
        for title, value in (("最早", draft.get("remind_after")), ("目标", draft.get("target_at")), ("最迟", deadline)):
            if value:
                parts.append(title + " " + show_time(value))
        estimate_start = draft.get("window_open_at")
        estimate_end = draft.get("window_close_at")
        if estimate_start and estimate_end:
            opened = max(datetime.fromisoformat(value) for value in (estimate_start, start) if value)
            closed = min(datetime.fromisoformat(value) for value in (estimate_end, deadline, draft.get("expires_at")) if value)
            parts.append("预计在 " + show_time(opened) + " 至 " + show_time(closed) + " 找空档提醒")
    for title, key in (("提醒失效", "expires_at"), ("事项截止", "due_at")):
        if draft.get(key):
            parts.append(title + " " + show_time(draft[key]))
    return "\n".join(parts)


def task_status(view):
    if view.get("archived_at"):
        return "已归档"
    issue = view["draft"].get("item_type") == "issue"
    issue_label = {"created": "事项待开始", "in_progress": "事项进行中", "paused": "事项已暂停"}
    if view["status"] == "registered":
        reminder = ""
        if view.get("expired_at"):
            reminder = "提醒已失效"
        elif view.get("acknowledged_at"):
            reminder = "已确认收到"
        elif view["draft"].get("reminder_enabled", True):
            reminder = "已提醒，待处理" if view["displayed_at"] else "等待提醒"
        result = issue_label.get(view.get("issue_status"), "事项待开始") if issue else ""
        return " · ".join(part for part in (result, reminder) if part)
    return {"draft": "草稿，待确认", "completed": "已完成", "cancelled": "已取消"}[view["status"]]


class OptionalTime(QWidget):
    changed = pyqtSignal()

    def __init__(self, *, required=False, timezone=None):
        super().__init__()
        self._loaded_time = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        self.enabled = Toggle()
        self.enabled.setChecked(required)
        self.enabled.setVisible(not required)
        layout.addWidget(self.enabled)
        fields = QHBoxLayout()
        fields.setSpacing(7)
        self.date = QDateEdit(calendarPopup=True)
        self.date.setDisplayFormat("yyyy-MM-dd")
        self.time = QTimeEdit()
        self.time.setDisplayFormat("HH:mm:ss")
        self.time.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        for editor in (self.date, self.time):
            editor.setMinimumWidth(0)
            editor.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            editor.setEnabled(required)
            fields.addWidget(editor, 1)
            self.enabled.toggled.connect(editor.setEnabled)
        layout.addLayout(fields)
        self.set_timezone(timezone)
        self.enabled.toggled.connect(self.changed)
        self.date.dateChanged.connect(self.changed)
        self.time.timeChanged.connect(self.changed)

    def set_timezone(self, timezone):
        self.zone = resolve_timezone(timezone or ZONE_NAME)
        self._qt_zone = QTimeZone(self.zone.key.encode("ascii"))

    def load(self, value, default, *, required=False):
        local = local_time(value or default, zone=self.zone.key)
        self._loaded_time = QDateTime.fromMSecsSinceEpoch(round(local.timestamp() * 1000), self._qt_zone)
        with QSignalBlocker(self.date), QSignalBlocker(self.time), QSignalBlocker(self.enabled):
            self.date.setDate(self._loaded_time.date())
            self.time.setTime(self._loaded_time.time())
            self.enabled.setChecked(required or value is not None)
            self.date.setEnabled(self.enabled.isChecked())
            self.time.setEnabled(self.enabled.isChecked())

    def value(self):
        if not self.enabled.isChecked():
            return None
        day, time = self.date.date(), self.time.time()
        selected = self._loaded_time
        if selected is None or selected.date() != day or selected.time() != time:
            selected = QDateTime(day, time, self._qt_zone)
        return datetime.fromtimestamp(selected.toMSecsSinceEpoch() / 1000, self.zone).isoformat()


class GroupTitle(QLabel):
    def __init__(self):
        super().__init__()
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(True)
        self.subject = None
        self.real_paint = None
        self.report = None

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.subject and self.real_paint and self.real_paint() and self.visibleRegion().contains(self.contentsRect()):
            subject = self.subject
            QTimer.singleShot(0, lambda: self._report_visible(subject))

    def _report_visible(self, subject):
        if self.subject == subject and self.isVisible() and self.visibleRegion().contains(self.contentsRect()):
            self.report(*subject)


class ReminderPanel(QGroupBox):
    actions_changed = pyqtSignal()
    edit_opened = pyqtSignal()

    def __init__(self, *, submit, advanced=False, timezone=None, parent=None):
        super().__init__("提醒事项", parent)
        self.submit = submit
        self.advanced = advanced
        self.timezone = timezone
        self.snapshot = {}
        self.pending = {}
        self._editing = None
        self._edit_original = None
        self._edit_timing = None
        self._select_next = None
        self._rows = None
        self._delivery_group = None
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        if advanced:
            layout.setContentsMargins(0, 0, 2, 0)
            layout.setSpacing(8)
        self.content = QLineEdit()
        self.content.setMaxLength(500)
        self.content.setPlaceholderText("例如：提交报告")
        self.mode = MenuField() if advanced else QComboBox()
        self.mode.addItem("到点提醒", "hard")
        self.mode.addItem("柔性提醒（窗口内择机）", "soft")
        if advanced:
            self.mode.addItem("只记录事项", "none")
        self.delay = QSpinBox()
        self.delay.setRange(0, 604800)
        self.delay.setValue(10)
        self.delay.setSuffix(" 秒后")
        self.window = QSpinBox()
        self.window.setRange(1, 86400)
        self.window.setValue(20)
        self.window.setSuffix(" 秒")
        self.times = {}
        if advanced:
            content_card, content_layout = _card(spacing=6)
            content_layout.addWidget(_text("事项内容", bold=True))
            content_layout.addWidget(_text("标题", size=11, color=MUTED, bold=True))
            self.content.setAccessibleName("标题")
            content_layout.addWidget(self.content)
            self.description = QLineEdit()
            self.description.setAccessibleName("说明")
            self.description.setMaxLength(4000)
            content_layout.addWidget(_text("说明", size=11, color=MUTED, bold=True))
            content_layout.addWidget(self.description)
            self.item_type = MenuField()
            self.item_type.addItem("纯提醒", "reminder")
            self.item_type.addItem("工作事项", "issue")
            self.item_type.setAccessibleName("类型")
            self.item_type.setFixedWidth(130)
            item_type = QHBoxLayout()
            item_type.addWidget(_text("类型", size=12), 1)
            item_type.addWidget(self.item_type)
            content_layout.addLayout(item_type)
            self.item_type.currentIndexChanged.connect(self._refresh_controls)
            layout.addWidget(content_card)

            time_card, time_layout = _card(spacing=7)
            mode = QHBoxLayout()
            mode.addWidget(_text("提醒方式", bold=True), 1)
            self.mode.setAccessibleName("提醒方式")
            self.mode.setFixedWidth(148)
            mode.addWidget(self.mode)
            time_layout.addLayout(mode)
            for key, title in (("exact", "准点时间"), ("remind_after", "最早提醒"), ("target_at", "目标时间"),
                               ("remind_by", "最迟提醒"), ("expires_at", "提醒失效"), ("due_at", "事项截止")):
                editor = OptionalTime(required=key == "exact", timezone=self.timezone)
                editor.date.setAccessibleName(f"{title}日期")
                editor.time.setAccessibleName(f"{title}时刻")
                if key == "exact":
                    editor.layout().insertWidget(0, _text(title, size=11, color=MUTED, bold=True))
                else:
                    editor.enabled.setText(f"设置{title}时间")
                self.times[key] = editor
                time_layout.addWidget(editor)
                editor.changed.connect(self._refresh_controls)
            layout.addWidget(time_card)
        else:
            form.addRow("标题", self.content)
            form.addRow("提醒方式", self.mode)
            form.addRow("最早提醒", self.delay)
            form.addRow("柔性窗口宽度", self.window)
            layout.addLayout(form)
        self.preview = _text("等待服务时间…", size=12, color=GREEN, bold=True, wrap=True) if advanced else label("等待服务时间…")
        if advanced:
            preview = QVBoxLayout()
            preview.setSpacing(2)
            preview.addWidget(self.preview)
            self.preview_timezone = _text(size=10, color=MUTED, wrap=True)
            self.preview_timezone.hide()
            preview.addWidget(self.preview_timezone)
            layout.addLayout(preview)
        else:
            layout.addWidget(self.preview)
        self.edit_note = label("")
        layout.addWidget(self.edit_note)
        row = QHBoxLayout()
        self.create_button = QPushButton("创建草稿")
        self.create_button.setProperty("primary", True)
        self.save_edit = QPushButton("保存修改")
        self.cancel_edit = QPushButton("取消修改")
        for b in (self.create_button, self.save_edit, self.cancel_edit):
            row.addWidget(b)
        layout.addLayout(row)
        self.create_button.clicked.connect(self._create)
        self.save_edit.clicked.connect(self._save_edit)
        self.cancel_edit.clicked.connect(self._cancel_edit)
        self.save_edit.hide()
        self.cancel_edit.hide()
        management = layout
        if advanced:
            self.management_card, management = _card(spacing=6)
            management.addWidget(_text("已添加的事项", bold=True))
            layout.addWidget(self.management_card)
        self.tasks = QListWidget()
        self.tasks.setMaximumHeight(100)
        self.tasks.currentItemChanged.connect(self._render_details)
        self.group_note = label("")
        self.group_exit = QPushButton("显示全部事项")
        self.group_exit.clicked.connect(lambda: self.open_delivery_group(None))
        self.group_note.hide()
        self.group_exit.hide()
        management.addWidget(self.group_note)
        management.addWidget(self.group_exit)
        management.addWidget(self.tasks)
        self.detail_title = GroupTitle()
        management.addWidget(self.detail_title)
        self.detail_title.hide()
        self.details = label("尚无事项。创建草稿后，请核对时间并确认。")
        management.addWidget(self.details)
        row = QGridLayout() if advanced else QHBoxLayout()
        self.buttons = {}
        buttons = [("confirm", "确认"), ("done", "标记完成"), ("edit", "修改安排"), ("cancel", "取消")]
        if advanced:
            buttons.insert(1, ("ack", "确认收到"))
            buttons.extend((("archive", "归档"), ("reopen", "重开事项")))
        for index, (kind, title) in enumerate(buttons):
            button = QPushButton(title)
            button.setObjectName(f"c-reminder-{kind}")
            button.clicked.connect(lambda checked=False, k=kind: self._selected_action(k))
            self.buttons[kind] = button
            if advanced:
                row.addWidget(button, index // 3, index % 3)
            else:
                row.addWidget(button)
        management.addLayout(row)
        self.snooze_seconds = QSpinBox()
        self.snooze_seconds.setRange(1, 604800)
        self.snooze_seconds.setValue(300 if advanced else 600)
        self.snooze_seconds.setSuffix(" 秒后")
        self.buttons["snooze"] = QPushButton("延后提醒")
        self.buttons["snooze"].clicked.connect(lambda: self._selected_action("snooze"))
        if advanced:
            self.snooze_mode = MenuField()
            self.snooze_mode.addItem("左右", "flexible_target")
            self.snooze_mode.addItem("以后", "no_earlier")
            self.snooze_mode.addItem("准点", "exact")
            self.snooze_controls = QWidget()
            controls = QGridLayout(self.snooze_controls)
            controls.setContentsMargins(0, 0, 0, 0)
            controls.addWidget(self.snooze_seconds, 0, 0)
            controls.addWidget(self.snooze_mode, 0, 1)
            controls.addWidget(self.buttons["snooze"], 1, 0, 1, 2)
            management.addWidget(self.snooze_controls)
        else:
            row = QHBoxLayout()
            row.addWidget(self.snooze_seconds)
            row.addWidget(self.buttons["snooze"])
            management.addLayout(row)
        self.snooze_preview = label("")
        management.addWidget(self.snooze_preview)
        self.result = label("草稿确认前不会调度提醒。")
        layout.addWidget(self.result)
        self.content.textChanged.connect(self._refresh_controls)
        self.mode.currentIndexChanged.connect(self._refresh_controls)
        self.delay.valueChanged.connect(self._refresh_controls)
        self.window.valueChanged.connect(self._refresh_controls)
        self.snooze_seconds.valueChanged.connect(self._refresh_controls)
        self._refresh_controls()

    def view(self, task_id):
        return next((v for v in self.snapshot.get("tasks", []) if v["task_id"] == task_id), None)

    def selected(self):
        item = self.tasks.currentItem()
        return self.view(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def select_task(self, task_id):
        for index in range(self.tasks.count()):
            item = self.tasks.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == task_id:
                self.tasks.setCurrentItem(item)
                break

    def busy(self, task_id):
        return any(p["task_id"] == task_id for p in self.pending.values())

    def _payload(self):
        content = self.content.text().strip()
        timing = self._timing_input()
        if self._edit_original is not None and self._edit_timing is not None:
            editable_fields = {"due_at"} if self.advanced else set()
            if all(timing[key] == self._edit_timing.get(key) for key in timing.keys() - editable_fields):
                updates = {key: timing[key] for key in editable_fields if timing[key] != self._edit_timing.get(key)}
                if self.advanced:
                    updates["description"] = self.description.text().strip() or None
                return {"draft": self._edit_original | updates | {"content": content}}
        if self.advanced:
            return {"draft": {"content": content, "description": self.description.text().strip() or None, **timing}}
        return {"content": content, **timing}

    def _timing_input(self):
        mode = self.mode.currentData()
        if not self.advanced:
            return {"mode": mode, "delay_s": self.delay.value(), "window_s": self.window.value()}
        values = {key: editor.value() for key, editor in self.times.items()}
        item_type = self.item_type.currentData()
        timezone = (self._edit_original or {}).get("timezone", self.timezone or ZONE_NAME)
        draft = {"mode": "hard" if mode == "hard" else "soft", "item_type": item_type, "timezone": timezone,
                 "reminder_enabled": mode != "none",
                 "due_at": values["due_at"] if item_type == "issue" else None}
        if mode == "hard":
            draft.update(remind_after=values["exact"], target_at=values["exact"], remind_by=values["exact"],
                         expires_at=values["expires_at"])
        elif mode == "soft":
            draft.update({key: values[key] for key in ("remind_after", "target_at", "remind_by", "expires_at")})
        return draft

    def _snooze_payload(self, seconds):
        payload = {"seconds": seconds}
        if self.advanced:
            payload["mode"] = self.snooze_mode.currentData()
        return payload

    def _create(self):
        if self.create_button.isEnabled():
            self.request("create", f"c-task-{uuid4().hex}", 0, self._payload())

    def request(self, kind, task_id, version, payload=None):
        view = self.view(task_id)
        if self.busy(task_id) or (kind != "create" and (not view or view["version"] != version)):
            self.result.setText("事项已更新或操作尚未完成，请查看最新安排。")
            return None
        try:
            action = "set_status" if kind == "reopen" else kind
            event_id = self.submit(f"reminder.{action}", task_id=task_id, version=version,
                                   payload={"status": "created"} if kind == "reopen" else payload or {})
        except (RuntimeError, ValueError) as exc:
            self.result.setText(f"暂时无法提交：{exc}。输入已保留。")
            return None
        self.pending[event_id] = {"kind": kind, "task_id": task_id, "version": version}
        self.result.setText("正在提交…")
        self._refresh_controls()
        self.actions_changed.emit()
        return event_id

    def _selected_action(self, kind):
        if not self.buttons[kind].isEnabled():
            return
        view = self.selected()
        if kind == "edit":
            self.open_edit(view["task_id"])
        else:
            self.request(kind, view["task_id"], view["version"],
                         self._snooze_payload(self.snooze_seconds.value()) if kind == "snooze" else {})

    def open_edit(self, task_id):
        view = self.view(task_id)
        if not view or view["status"] not in ("draft", "registered") or self.busy(task_id):
            return
        self._editing = (task_id, view["version"])
        draft = view["draft"]
        self._edit_original = draft.copy()
        self._edit_timing = None
        self.content.setText(draft["content"])
        if self.advanced:
            self.description.setText(draft.get("description") or "")
        mode = draft["mode"] if draft.get("reminder_enabled", True) else "none"
        self.mode.setCurrentIndex(max(0, self.mode.findData(mode)))
        now = datetime.fromisoformat(self.snapshot["now"])
        start = draft.get("remind_after") or draft.get("earliest_at")
        deadline = draft.get("remind_by") or draft.get("latest_first_delivery_at")
        if self.advanced:
            self.item_type.setCurrentIndex(self.item_type.findData(draft.get("item_type", "reminder")))
            legacy_start = start if draft.get("latest_first_delivery_at") else None
            values = draft | {
                "exact": draft.get("target_at") or deadline,
                "remind_after": draft.get("remind_after") or legacy_start,
                "remind_by": deadline,
            }
            for key, editor in self.times.items():
                editor.set_timezone(self._display_zone(draft))
                editor.load(values.get(key), now + timedelta(minutes=5), required=key == "exact")
        else:
            earliest = datetime.fromisoformat(start) if start else now
            latest = datetime.fromisoformat(deadline) if deadline else earliest
            self.delay.setValue(max(0, round((earliest - now).total_seconds())))
            self.window.setValue(max(1, round((latest - earliest).total_seconds())))
        self._edit_timing = self._timing_input()
        self.edit_note.setText("正在修改：" + view["draft"]["content"] + "。保存前原安排保持有效。")
        self._refresh_controls()
        self.edit_opened.emit()

    def _save_edit(self):
        if self.save_edit.isEnabled():
            task_id, version = self._editing
            self.request("edit", task_id, version, self._payload())

    def _cancel_edit(self):
        self._editing = None
        self._edit_original = None
        self._edit_timing = None
        for editor in self.times.values():
            editor.set_timezone(self.timezone)
        self.edit_note.setText("")
        self._refresh_controls()

    def receive_result(self, result):
        pending = self.pending.pop(result.get("command_id", result.get("event_id")), None)
        if pending is None:
            return
        success = result.get("status") == "applied" or (
            result.get("status") == "duplicate" and result.get("message") == "replayed original applied result")
        if success:
            self._select_next = pending["task_id"]
            titles = {"create": "草稿已创建，请核对时间后确认。", "confirm": "安排已确认。", "done": "事项已完成。", "ack": "已记录确认收到。",
                      "snooze": "已延后，请查看新的提醒时间。", "edit": "安排已更新。", "cancel": "事项已取消。",
                      "archive": "已归档。", "reopen": "事项已重开，可重新设置提醒。"}
            self.result.setText(titles[pending["kind"]])
            if pending["kind"] == "edit":
                self._cancel_edit()
        else:
            self.result.setText(ERRORS.get(result.get("error_code"), "操作未成功。") + " 输入已保留；修改冲突时请重新点击「修改安排」载入最新版本。")
        self._refresh_controls()
        self.actions_changed.emit()

    def render_snapshot(self, snapshot):
        self.snapshot = snapshot
        views = snapshot.get("tasks", [])
        if self.advanced:
            views = [view for view in views if not view.get("archived_at")]
        if self._delivery_group is not None:
            views = [view for view in views if view["task_id"] in self._delivery_group]
        rows = [(v["task_id"], v["version"], v["displayed_at"], self._delivery_note(v)) for v in views]
        if self.advanced and self._rows is None and self._editing is None:
            for key, editor in self.times.items():
                editor.load(None, datetime.fromisoformat(snapshot["now"]) + timedelta(minutes=5), required=key == "exact")
        if rows != self._rows:
            old = self.selected()
            selected = self._select_next or (old["task_id"] if old else None)
            self.tasks.blockSignals(True)
            self.tasks.clear()
            for view in views:
                kind = "工作事项" if view["draft"].get("item_type") == "issue" else "纯提醒"
                item = QListWidgetItem(view["draft"]["content"] + " · " + kind + " · " + (self._delivery_note(view) or task_status(view)))
                item.setData(Qt.ItemDataRole.UserRole, view["task_id"])
                self.tasks.addItem(item)
                if view["task_id"] == selected:
                    self.tasks.setCurrentItem(item)
            if self.tasks.currentRow() < 0 and self.tasks.count():
                self.tasks.setCurrentRow(self.tasks.count() - 1)
            if self._select_next in {v["task_id"] for v in views}:
                self._select_next = None
            self.tasks.blockSignals(False)
            self._rows = rows
        self._render_details()
        self.actions_changed.emit()

    def _render_details(self):
        view = self.selected()
        if self.advanced:
            self.detail_title.setVisible(view is not None)
            if view:
                self.detail_title.setText(view["draft"]["content"])
                self.detail_title.subject = (view["task_id"], view["version"])
                if self._delivery_note(view).startswith("同批到期"):
                    self.detail_title.update()
            else:
                self.detail_title.subject = None
        detail = "尚无事项。创建草稿后，请核对时间并确认。"
        if view:
            lines = [self._delivery_note(view) or task_status(view), self._schedule(view["draft"])]
            if view["draft"].get("description"):
                lines.insert(1, view["draft"]["description"])
            if not self.advanced:
                lines.insert(0, view["draft"]["content"])
            detail = "\n".join(lines)
        self.details.setText(detail)
        self._refresh_controls()

    def bind_group_paints(self, report, real_paint):
        self.detail_title.report = report
        self.detail_title.real_paint = real_paint

    def open_delivery_group(self, task_ids):
        self._delivery_group = task_ids
        self.group_note.setVisible(task_ids is not None)
        self.group_exit.setVisible(task_ids is not None)
        if task_ids is not None:
            self.group_note.setText(f"同时到期的 {len(task_ids)} 条提醒")
        self.render_snapshot(self.snapshot)

    def _delivery_note(self, view):
        return self.snapshot.get("presentation_pending", {}).get(view["task_id"], "")

    def _display_zone(self, draft):
        return draft.get("timezone", self.timezone) if self.timezone is not None else None

    def _schedule(self, draft):
        return schedule_text(draft, self.snapshot.get("now"), zone=self._display_zone(draft))

    def _snooze_time(self, seconds, view):
        now = datetime.fromisoformat(self.snapshot["now"])
        zone = self._display_zone(view["draft"]) if view else self.timezone
        return display_time(now + timedelta(seconds=seconds), now, zone=zone)

    def _render_schedule_preview(self, draft):
        payload = draft.model_dump(mode="json")
        schedule = self._schedule(payload)
        zone = ""
        prefix = "准点提醒 "
        if schedule.startswith(prefix):
            first_line, separator, extra = schedule.partition("\n")
            label, _, zone = first_line.removeprefix(prefix).partition("（")
            schedule = label + " · 准点提醒" + separator + extra
        self.preview.setText(schedule)
        self.preview_timezone.setText(zone.removesuffix("）").replace(" UTC", " · UTC"))
        self.preview_timezone.setVisible(bool(zone))

    def _refresh_controls(self):
        ready = bool(self.snapshot)
        view = self.selected()
        available = ready and view is not None and not self.busy(view["task_id"])
        status = view["status"] if view else None
        valid = ready and bool(self.content.text().strip())
        self.window.setEnabled(self.mode.currentData() == "soft")
        if self.advanced:
            self.management_card.setVisible(self.tasks.count() > 0 or self._delivery_group is not None)
            self.edit_note.setVisible(bool(self.edit_note.text()))
            self.result.setVisible(self.result.text() != "草稿确认前不会调度提醒。")
            mode = self.mode.currentData()
            issue = self.item_type.currentData() == "issue"
            self.item_type.setEnabled(self._editing is None)
            for key, editor in self.times.items():
                visible = issue if key == "due_at" else mode != "none" if key == "expires_at" else (mode == "hard") == (key == "exact") and mode != "none"
                editor.setVisible(visible)
            valid = valid and (mode != "none" or issue)
            if ready:
                payload = self._payload()
                try:
                    draft = TaskDraft.model_validate(payload["draft"] | {"content": "预览"})
                except ValueError:
                    self.preview_timezone.hide()
                    valid = False
                    self.preview.setText("请检查最早时间、目标、最迟时间和失效时间的先后顺序。")
                else:
                    self._render_schedule_preview(draft)
        self.create_button.setEnabled(valid and self._editing is None and not any(p["kind"] == "create" for p in self.pending.values()))
        self.save_edit.setVisible(self._editing is not None)
        self.cancel_edit.setVisible(self._editing is not None)
        editing_view = self.view(self._editing[0]) if self._editing else None
        self.save_edit.setEnabled(valid and editing_view is not None and editing_view["version"] == self._editing[1] and editing_view["status"] in ("draft", "registered") and not self.busy(editing_view["task_id"]))
        self.cancel_edit.setEnabled(not self._editing or not self.busy(self._editing[0]))
        for kind, button in self.buttons.items():
            allowed = status == "draft" if kind == "confirm" else status in ("draft", "registered") if kind in ("edit", "cancel") else status == "registered"
            if kind in ("ack", "snooze") and view:
                allowed = allowed and view["draft"].get("reminder_enabled", True) and not (
                    view.get("acknowledged_at") or view.get("expired_at"))
            if kind == "reopen" and view:
                allowed = status in ("completed", "cancelled") and view["draft"].get("item_type") == "issue"
            if kind == "archive" and view:
                allowed = not view.get("archived_at")
            button.setEnabled(available and allowed)
        if ready:
            now = datetime.fromisoformat(self.snapshot["now"])
            if not self.advanced:
                if self._edit_original is not None and self._timing_input() == self._edit_timing:
                    draft = self._edit_original
                else:
                    start = now + timedelta(seconds=self.delay.value())
                    end = start + timedelta(seconds=self.window.value() if self.mode.currentData() == "soft" else 0)
                    draft = {"mode": self.mode.currentData(), "earliest_at": start.isoformat(), "latest_first_delivery_at": end.isoformat()}
                self.preview.setText(self._schedule(draft))
            self.snooze_preview.setText("延后时间 " + self._snooze_time(self.snooze_seconds.value(), view))


class ReminderCard(QFrame):
    """A delivery-bound card. Real visible paints are the only success trigger."""
    def __init__(self, command, *, context, controller, parent=None):
        super().__init__(parent)
        self.command = command
        self.context = context
        self.controller = controller
        self._reported = False
        self._retired = False
        self.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.addWidget(label("该提醒到时间了：" + command.content))
        self.arrangement = label("")
        layout.addWidget(self.arrangement)
        row = QHBoxLayout()
        self.done = QPushButton("已完成")
        self.done.clicked.connect(lambda: self._action("done"))
        self.cancel = QPushButton("取消提醒")
        self.cancel.clicked.connect(lambda: self._action("cancel"))
        self.edit = QPushButton("修改安排")
        self.edit.clicked.connect(lambda: controller.open_edit(command.task_id) if self._valid() else None)
        for button in (self.done, self.edit, self.cancel):
            row.addWidget(button)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.snooze_seconds = QSpinBox()
        self.snooze_seconds.setRange(1, 604800)
        self.snooze_seconds.setValue(600)
        self.snooze_seconds.setSuffix(" 秒后")
        self.snooze = QPushButton("延后提醒")
        self.snooze.clicked.connect(lambda: self._action("snooze", self.controller._snooze_payload(self.snooze_seconds.value())))
        self.snooze_preview = label("")
        self.snooze_seconds.valueChanged.connect(self.refresh)
        row.addWidget(self.snooze_seconds)
        row.addWidget(self.snooze)
        layout.addLayout(row)
        layout.addWidget(self.snooze_preview)
        self._paint_timer = QTimer(self)
        self._paint_timer.setSingleShot(True)
        self._paint_timer.timeout.connect(self._acknowledge)
        self.refresh()

    def _valid(self):
        view = self.controller.view(self.command.task_id)
        return bool(not self._retired and view and view["status"] == "registered"
                    and view["version"] == self.command.task_version)

    def _action(self, kind, payload=None):
        if self._valid() and not self.controller.busy(self.command.task_id):
            self.controller.request(kind, self.command.task_id, self.command.task_version, payload)

    def refresh(self):
        enabled = self._valid() and not self.controller.busy(self.command.task_id)
        for button in (self.done, self.cancel, self.edit, self.snooze):
            button.setEnabled(enabled)
        view = self.controller.view(self.command.task_id)
        if view:
            self.arrangement.setText(self.controller._schedule(view["draft"]))
        if self.controller.snapshot:
            self.snooze_preview.setText("延后后的最早时间：" + self.controller._snooze_time(self.snooze_seconds.value(), view))
        if not self._reported and not self._retired:
            self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if (not self._reported and not self._retired and self.context.can_report_display
                and not self.visibleRegion().isEmpty() and not self._paint_timer.isActive()):
            self._paint_timer.start(0)

    def _acknowledge(self):
        # A delivery can reach the GUI before the matching clock snapshot.
        # Wait for a fresh snapshot instead of reporting an earlier render time.
        now = datetime.fromisoformat(self.controller.snapshot['now']) if self.controller.snapshot else None
        if (self._valid() and self.isVisible() and not self.visibleRegion().isEmpty()
                and self.context.can_report_display and now is not None
                and self.command.requested_at <= now < self.command.expires_at):
            self._reported = self.context.report_display(self.command.delivery_id)

    def retire(self):
        self._retired = True
        self._paint_timer.stop()
        self.setEnabled(False)
        self.hide()
