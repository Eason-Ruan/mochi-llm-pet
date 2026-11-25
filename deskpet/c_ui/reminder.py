"""C-owned reminder controls and receipt-aware cards; B remains task authority."""
from __future__ import annotations

from datetime import datetime, timedelta
from functools import partial
from uuid import uuid4

from PyQt6.QtCore import QDateTime, QLocale, QSignalBlocker, Qt, QTimer, QTimeZone, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractSpinBox, QComboBox, QDateEdit, QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPushButton, QSizePolicy, QSpinBox, QTimeEdit, QVBoxLayout, QWidget,
)

from deskpet.contracts import TaskDraft
from deskpet.i18n import language, tr
from deskpet.timezones import resolve_timezone
from .presentation import grouped_delivery_note
from .refine_style import GREEN, MUTED, MenuField, Toggle, _card, _text
from .time_format import ZONE_NAME, format_day_clock, format_time, format_timezone, local_time
from .work_panel import label

ERRORS = {
    "STALE_TASK": ("事项已更新，请查看最新安排后重试。",
                   "This item changed. Check the current schedule and try again."),
    "STALE_SURFACE": ("这张卡片已失效，请使用当前事项。",
                      "This card is out of date. Open the current item."),
    "CANCELLED": ("事项已取消。", "Item cancelled."),
    "NOT_FOUND": ("事项不存在。", "Item not found."),
    "INVALID_ARGUMENT": ("请检查事项和时间输入。", "Check the item and its times."),
    "INVALID_TRANSITION": ("当前状态不支持此操作。", "This action is unavailable in the current state."),
}
RESULT_MESSAGES = {
    "create": ("草稿已创建，请核对时间后确认。", "Draft created. Check the schedule and confirm it."),
    "confirm": ("安排已确认。", "Schedule confirmed."),
    "done": ("事项已完成。", "Item completed."),
    "ack": ("已记录确认收到。", "Reminder acknowledged."),
    "snooze": ("已延后，请查看新的提醒时间。", "Reminder snoozed. Check the new time."),
    "edit": ("安排已更新。", "Schedule updated."),
    "cancel": ("事项已取消。", "Item cancelled."),
    "archive": ("已归档。", "Item archived."),
    "reopen": ("事项已重开，可重新设置提醒。", "Item reopened. You can set a new reminder."),
}


def display_time(value, reference=None, *, zone=None):
    return format_time(value, reference, zone=zone)


def schedule_text(draft, reference=None, *, zone=None):
    return _schedule_display(draft, reference, zone=zone)[0]


def _schedule_display(draft, reference=None, *, zone=None, split_exact_timezone=False):
    show_time = partial(display_time, reference=reference, zone=zone)
    start = draft.get("remind_after") or draft.get("earliest_at")
    deadline = draft.get("remind_by") or draft.get("latest_first_delivery_at")
    parts = []
    preview_zone = ""
    if not draft.get("reminder_enabled", True):
        parts.append(tr("未设置提醒", "No reminder set"))
    elif draft["mode"] == "hard" and deadline:
        if split_exact_timezone:
            time = local_time(deadline, zone=zone)
            reference_day = local_time(reference, zone=zone).date() if reference is not None else None
            parts.append(tr("{time} · 准点提醒", "{time} · At set time").format(
                time=format_day_clock(time, reference_day),
            ))
            preview_zone = format_timezone(time, zone=zone).replace(" UTC", " · UTC")
        else:
            parts.append(tr("准点提醒 {time}", "At set time · {time}").format(time=show_time(deadline)))
    elif draft.get("latest_first_delivery_at") and draft.get("earliest_at") and not draft.get("remind_by"):
        parts.append(tr("柔性提醒窗口 {start} 至 {end}", "Flexible reminder window {start} to {end}").format(
            start=show_time(start), end=show_time(deadline),
        ))
    else:
        parts.append(tr("择机提醒", "Flexible reminder"))
        if timing_context := draft.get("timing_context"):
            parts.append(tr("时机条件：{context}", "Timing condition · {context}").format(context=timing_context))
        for title, value in (
            (tr("最早", "Earliest"), draft.get("remind_after")),
            (tr("目标", "Target"), draft.get("target_at")),
            (tr("最迟", "Latest"), deadline),
        ):
            if value:
                parts.append(title + " " + show_time(value))
        estimate_start = draft.get("window_open_at")
        estimate_end = draft.get("window_close_at")
        if estimate_start and estimate_end:
            opened = max(datetime.fromisoformat(value) for value in (estimate_start, start) if value)
            closed = min(datetime.fromisoformat(value) for value in (estimate_end, deadline, draft.get("expires_at")) if value)
            parts.append(tr("预计在 {start} 至 {end} 找空档提醒", "Likely to remind between {start} and {end}").format(
                start=show_time(opened), end=show_time(closed),
            ))
    for title, key in ((tr("提醒失效", "Reminder expires"), "expires_at"), (tr("事项截止", "Item due"), "due_at")):
        if draft.get(key):
            parts.append(title + " " + show_time(draft[key]))
    return "\n".join(parts), preview_zone


def task_status(view):
    if view.get("archived_at"):
        return tr("已归档", "Archived")
    issue = view["draft"].get("item_type") == "issue"
    issue_label = {
        "created": ("事项待开始", "Item not started"),
        "in_progress": ("事项进行中", "Item in progress"),
        "paused": ("事项已暂停", "Item paused"),
    }
    if view["status"] == "registered":
        reminder = ""
        if view.get("expired_at"):
            reminder = tr("提醒已失效", "Reminder expired")
        elif view.get("acknowledged_at"):
            reminder = tr("已确认收到", "Acknowledged")
        elif view["draft"].get("reminder_enabled", True):
            reminder = tr("已提醒，待处理", "Awaiting response") if view["displayed_at"] else tr("等待提醒", "Reminder pending")
        result = tr(*issue_label.get(view.get("issue_status"), issue_label["created"])) if issue else ""
        return " · ".join(part for part in (result, reminder) if part)
    states = {
        "draft": ("草稿，待确认", "Draft awaiting confirmation"),
        "completed": ("已完成", "Completed"),
        "cancelled": ("已取消", "Cancelled"),
    }
    return tr(*states[view["status"]])


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
        self.date.calendarWidget().setLocale(QLocale(tr("zh_CN", "en_US")))
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
        super().__init__(tr("提醒事项", "Reminders"), parent)
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
        self.content.setPlaceholderText(tr("例如：提交报告", "e.g. Submit the report"))
        self.mode = MenuField() if advanced else QComboBox()
        self.mode.addItem(tr("到点提醒", "At set time"), "hard")
        self.mode.addItem(tr("柔性提醒（窗口内择机）", "Flexible time"), "soft")
        if advanced:
            self.mode.addItem(tr("只记录事项", "Item only"), "none")
        self.delay = QSpinBox()
        self.delay.setRange(0, 604800)
        self.delay.setValue(10)
        self.delay.setSuffix(tr(" 秒后", " s later"))
        self.window = QSpinBox()
        self.window.setRange(1, 86400)
        self.window.setValue(20)
        self.window.setSuffix(tr(" 秒", " s"))
        self.times = {}
        if advanced:
            content_card, content_layout = _card(spacing=6)
            content_layout.addWidget(_text(tr("事项内容", "Item details"), bold=True))
            content_layout.addWidget(_text(tr("标题", "Title"), size=11, color=MUTED, bold=True))
            self.content.setAccessibleName(tr("标题", "Title"))
            content_layout.addWidget(self.content)
            self.description = QLineEdit()
            self.description.setAccessibleName(tr("说明", "Description"))
            self.description.setMaxLength(4000)
            content_layout.addWidget(_text(tr("说明", "Description"), size=11, color=MUTED, bold=True))
            content_layout.addWidget(self.description)
            self.item_type = MenuField()
            self.item_type.addItem(tr("纯提醒", "Reminder"), "reminder")
            self.item_type.addItem(tr("工作事项", "Work item"), "issue")
            self.item_type.setAccessibleName(tr("类型", "Type"))
            self.item_type.setFixedWidth(130)
            item_type = QHBoxLayout()
            item_type.addWidget(_text(tr("类型", "Type"), size=12), 1)
            item_type.addWidget(self.item_type)
            content_layout.addLayout(item_type)
            self.item_type.currentIndexChanged.connect(self._refresh_controls)
            layout.addWidget(content_card)

            time_card, time_layout = _card(spacing=7)
            mode = QHBoxLayout()
            mode.addWidget(_text(tr("提醒方式", "Timing"), bold=True), 1)
            self.mode.setAccessibleName(tr("提醒方式", "Reminder type"))
            self.mode.setFixedWidth(148)
            mode.addWidget(self.mode)
            time_layout.addLayout(mode)
            sound_row = QHBoxLayout()
            sound_row.addWidget(_text(tr("提醒声音", "Sound"), size=12), 1)
            self.sound = MenuField()
            self.sound.setAccessibleName(tr("提醒声音", "Reminder sound"))
            self.sound.addItem(tr("跟随默认设置", "Use default"), None)
            self.sound.addItem(tr("仅无声提示", "Silent"), False)
            self.sound.addItem(tr("允许提示音", "Play sound"), True)
            self.sound.setFixedWidth(148)
            sound_row.addWidget(self.sound)
            time_layout.addLayout(sound_row)
            for key, titles, toggle_labels in (
                ("exact", ("准点时间", "Exact time"), ()),
                ("remind_after", ("最早提醒", "Earliest reminder"), ("设置最早提醒时间", "Set earliest reminder")),
                ("target_at", ("目标时间", "Target time"), ("设置目标时间", "Set target time")),
                ("remind_by", ("最迟提醒", "Latest reminder"), ("设置最迟提醒时间", "Set latest reminder")),
                ("expires_at", ("提醒失效", "Reminder expires"), ("设置提醒失效时间", "Set reminder expiry")),
                ("due_at", ("事项截止", "Item due date"), ("设置事项截止时间", "Set item due date")),
            ):
                editor = OptionalTime(required=key == "exact", timezone=self.timezone)
                title = tr(*titles)
                editor.date.setAccessibleName(tr("{title}日期", "{title} · Date").format(title=title))
                editor.time.setAccessibleName(tr("{title}时刻", "{title} · Time").format(title=title))
                if toggle_labels:
                    editor.enabled.setText(tr(*toggle_labels))
                else:
                    editor.layout().insertWidget(0, _text(title, size=11, color=MUTED, bold=True))
                self.times[key] = editor
                time_layout.addWidget(editor)
                editor.changed.connect(self._refresh_controls)
            layout.addWidget(time_card)
        else:
            form.addRow(tr("标题", "Title"), self.content)
            form.addRow(tr("提醒方式", "Reminder type"), self.mode)
            form.addRow(tr("最早提醒", "Earliest reminder"), self.delay)
            form.addRow(tr("柔性窗口宽度", "Flexible window"), self.window)
            layout.addLayout(form)
        waiting = tr("等待服务时间…", "Waiting for service time…")
        self.preview = _text(waiting, size=12, color=GREEN, bold=True, wrap=True) if advanced else label(waiting)
        self.preview.setTextFormat(Qt.TextFormat.PlainText)
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
        self.create_button = QPushButton(tr("创建草稿", "Create draft"))
        self.create_button.setProperty("primary", True)
        self.save_edit = QPushButton(tr("保存修改", "Save edits"))
        self.cancel_edit = QPushButton(tr("取消修改", "Cancel"))
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
            management.addWidget(_text(tr("已添加的事项", "Added items"), bold=True))
            layout.addWidget(self.management_card)
        self.tasks = QListWidget()
        self.tasks.setMaximumHeight(100)
        self.tasks.currentItemChanged.connect(self._render_details)
        self.group_note = label("")
        self.group_exit = QPushButton(tr("显示全部事项", "Show all items"))
        self.group_exit.clicked.connect(lambda: self.open_delivery_group(None))
        self.group_note.hide()
        self.group_exit.hide()
        management.addWidget(self.group_note)
        management.addWidget(self.group_exit)
        management.addWidget(self.tasks)
        self.detail_title = GroupTitle()
        management.addWidget(self.detail_title)
        self.detail_title.hide()
        self.details = label(tr("尚无事项。创建草稿后，请核对时间并确认。", "Create a draft, then check and confirm the schedule."))
        self.details.setTextFormat(Qt.TextFormat.PlainText)
        management.addWidget(self.details)
        row = QGridLayout() if advanced else QHBoxLayout()
        self.buttons = {}
        buttons = [
            ("confirm", tr("确认", "Confirm")),
            ("done", tr("标记完成", "Mark done")),
            ("edit", tr("修改安排", "Edit schedule")),
            ("cancel", tr("取消", "Cancel")),
        ]
        if advanced:
            buttons.insert(1, ("ack", tr("确认收到", "Acknowledge")))
            buttons.extend((("archive", tr("归档", "Archive")), ("reopen", tr("重开事项", "Reopen item"))))
        columns = 2 if language() == "en" else 3
        for index, (kind, title) in enumerate(buttons):
            button = QPushButton(title)
            button.setObjectName(f"c-reminder-{kind}")
            button.clicked.connect(lambda checked=False, k=kind: self._selected_action(k))
            self.buttons[kind] = button
            if advanced:
                row.addWidget(button, index // columns, index % columns)
            else:
                row.addWidget(button)
        management.addLayout(row)
        self.snooze_seconds = QSpinBox()
        self.snooze_seconds.setRange(1, 604800)
        self.snooze_seconds.setValue(300 if advanced else 600)
        self.snooze_seconds.setSuffix(tr(" 秒后", " s later"))
        self.buttons["snooze"] = QPushButton(tr("延后提醒", "Snooze"))
        self.buttons["snooze"].clicked.connect(lambda: self._selected_action("snooze"))
        if advanced:
            self.snooze_seconds.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self.snooze_mode = MenuField()
            self.snooze_mode.addItem(tr("左右", "Around"), "flexible_target")
            self.snooze_mode.addItem(tr("以后", "No earlier"), "no_earlier")
            self.snooze_mode.addItem(tr("准点", "Exactly"), "exact")
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
        self._draft_note = tr("草稿确认前不会调度提醒。", "Confirm the draft to schedule the reminder.")
        self.result = label(self._draft_note)
        layout.addWidget(self.result)
        self.content.textChanged.connect(self._refresh_controls)
        self.mode.currentIndexChanged.connect(self._refresh_controls)
        if advanced:
            self.sound.currentIndexChanged.connect(self._refresh_controls)
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
            editable_fields = {"due_at", "sound"} if self.advanced else set()
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
                 "reminder_enabled": mode != "none", "sound": self.sound.currentData() if mode != "none" else None,
                 "due_at": values["due_at"] if item_type == "issue" else None}
        if mode != "none" and (timing_context := (self._edit_original or {}).get("timing_context")):
            draft["timing_context"] = timing_context
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
            self.result.setText(tr(
                "事项已更新或操作尚未完成，请查看最新安排。",
                "Check the current schedule. The item changed or an action is still pending.",
            ))
            return None
        try:
            action = "set_status" if kind == "reopen" else kind
            event_id = self.submit(f"reminder.{action}", task_id=task_id, version=version,
                                   payload={"status": "created"} if kind == "reopen" else payload or {})
        except (RuntimeError, ValueError) as exc:
            self.result.setText(tr("暂时无法提交：{error}。输入已保留。",
                                   "Could not submit ({error}). Your input was kept.").format(error=exc))
            return None
        self.pending[event_id] = {"kind": kind, "task_id": task_id, "version": version}
        self.result.setText(tr("正在提交…", "Submitting…"))
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
            self.sound.setCurrentIndex(self.sound.findData(draft.get("sound")))
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
        self.edit_note.setText(tr(
            "正在修改：{title}。保存前原安排保持有效。",
            "Editing {title}. The current schedule stays in effect until you save.",
        ).format(title=view["draft"]["content"]))
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
        if self.advanced:
            self.sound.setCurrentIndex(0)
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
            self.result.setText(tr(*RESULT_MESSAGES[pending["kind"]]))
            if pending["kind"] == "edit":
                self._cancel_edit()
        else:
            error = result.get("error_code")
            message = tr(*ERRORS[error]) if error in ERRORS else tr("操作未成功。", "The action could not be completed.")
            self.result.setText(message + tr(
                " 输入已保留；修改冲突时请重新点击「修改安排」载入最新版本。",
                " Your input was kept. If the schedule changed, click Edit schedule to load the latest version.",
            ))
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
                kind = tr("工作事项", "Work item") if view["draft"].get("item_type") == "issue" else tr("纯提醒", "Reminder")
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
                if self._delivery_note(view) == grouped_delivery_note():
                    self.detail_title.update()
            else:
                self.detail_title.subject = None
        detail = tr("尚无事项。创建草稿后，请核对时间并确认。", "Create a draft, then check and confirm the schedule.")
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
            self.group_note.setText(tr("同时到期的 {count} 条提醒", "{count} reminders due together").format(
                count=len(task_ids),
            ))
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
        schedule, zone = _schedule_display(payload, self.snapshot.get("now"),
                                           zone=self._display_zone(payload), split_exact_timezone=True)
        self.preview.setText(schedule)
        self.preview_timezone.setText(zone)
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
            self.result.setVisible(self.result.text() != self._draft_note)
            mode = self.mode.currentData()
            self.sound.setEnabled(mode != "none")
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
                    self.preview.setText(tr(
                        "请检查最早时间、目标、最迟时间和失效时间的先后顺序。",
                        "Check the order of the earliest, target, latest, and expiry times.",
                    ))
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
            self.snooze_preview.setText(tr("延后时间 {time}", "Snooze time · {time}").format(
                time=self._snooze_time(self.snooze_seconds.value(), view),
            ))


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
        layout.addWidget(label(tr("该提醒到时间了：{title}", "Reminder due · {title}").format(title=command.content)))
        self.arrangement = label("")
        layout.addWidget(self.arrangement)
        row = QHBoxLayout()
        self.done = QPushButton(tr("已完成", "Done"))
        self.done.clicked.connect(lambda: self._action("done"))
        self.cancel = QPushButton(tr("取消提醒", "Cancel reminder"))
        self.cancel.clicked.connect(lambda: self._action("cancel"))
        self.edit = QPushButton(tr("修改安排", "Edit schedule"))
        self.edit.clicked.connect(lambda: controller.open_edit(command.task_id) if self._valid() else None)
        for button in (self.done, self.edit, self.cancel):
            row.addWidget(button)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.snooze_seconds = QSpinBox()
        self.snooze_seconds.setRange(1, 604800)
        self.snooze_seconds.setValue(600)
        self.snooze_seconds.setSuffix(tr(" 秒后", " s later"))
        self.snooze = QPushButton(tr("延后提醒", "Snooze"))
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
            self.snooze_preview.setText(tr("延后后的最早时间：{time}", "Earliest time after snoozing · {time}").format(
                time=self.controller._snooze_time(self.snooze_seconds.value(), view),
            ))
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
