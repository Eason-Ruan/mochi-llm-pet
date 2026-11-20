"""C-owned reminder controls and receipt-aware cards; B remains task authority."""
from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox, QFormLayout, QFrame, QGroupBox, QHBoxLayout, QLineEdit,
    QListWidget, QListWidgetItem, QPushButton, QSpinBox, QVBoxLayout,
)

from .work_panel import label
from .time_format import format_time

ERRORS = {
    "STALE_TASK": "事项已更新，请查看最新安排后重试。",
    "STALE_SURFACE": "这张卡片已失效，请使用当前事项。",
    "CANCELLED": "事项已取消。", "NOT_FOUND": "事项不存在。",
    "INVALID_ARGUMENT": "请检查事项和时间输入。",
    "INVALID_TRANSITION": "当前状态不支持此操作。",
}


def display_time(value, reference=None):
    return format_time(value, reference)


def schedule_text(draft, reference=None):
    if draft["mode"] == "hard":
        return "到点提醒：" + display_time(draft["earliest_at"], reference)
    return "柔性提醒窗口：" + display_time(draft["earliest_at"], reference) + " 至 " + display_time(draft["latest_first_delivery_at"], reference)


def task_status(view):
    if view["status"] == "registered":
        return "已提醒，待处理" if view["displayed_at"] else "已确认，等待提醒"
    return {"draft": "草稿，待确认", "completed": "已完成", "cancelled": "已取消"}[view["status"]]


class ReminderPanel(QGroupBox):
    actions_changed = pyqtSignal()
    edit_opened = pyqtSignal()

    def __init__(self, *, submit, parent=None):
        super().__init__("提醒事项", parent)
        self.submit = submit
        self.snapshot = {}
        self.pending = {}
        self._editing = None
        self._select_next = None
        self._rows = None
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.content = QLineEdit()
        self.content.setMaxLength(500)
        self.content.setPlaceholderText("例如：提交报告")
        self.mode = QComboBox()
        self.mode.addItem("到点提醒", "hard")
        self.mode.addItem("柔性提醒（窗口内择机）", "soft")
        self.delay = QSpinBox()
        self.delay.setRange(0, 604800)
        self.delay.setValue(10)
        self.delay.setSuffix(" 秒后")
        self.window = QSpinBox()
        self.window.setRange(1, 86400)
        self.window.setValue(20)
        self.window.setSuffix(" 秒")
        form.addRow("事项", self.content)
        form.addRow("提醒方式", self.mode)
        form.addRow("最早提醒", self.delay)
        form.addRow("柔性窗口宽度", self.window)
        layout.addLayout(form)
        self.preview = label("等待服务时间…")
        layout.addWidget(self.preview)
        self.edit_note = label("")
        layout.addWidget(self.edit_note)
        row = QHBoxLayout()
        self.create_button = QPushButton("创建草稿")
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
        self.tasks = QListWidget()
        self.tasks.setMaximumHeight(100)
        self.tasks.currentItemChanged.connect(self._render_details)
        layout.addWidget(self.tasks)
        self.details = label("尚无事项。创建草稿后，请核对时间并确认。")
        layout.addWidget(self.details)
        row = QHBoxLayout()
        self.buttons = {}
        for kind, title in (("confirm", "确认提醒"), ("done", "已完成"), ("edit", "修改安排"), ("cancel", "取消提醒")):
            button = QPushButton(title)
            button.setObjectName(f"c-reminder-{kind}")
            button.clicked.connect(lambda checked=False, k=kind: self._selected_action(k))
            self.buttons[kind] = button
            row.addWidget(button)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.snooze_seconds = QSpinBox()
        self.snooze_seconds.setRange(1, 604800)
        self.snooze_seconds.setValue(600)
        self.snooze_seconds.setSuffix(" 秒后")
        self.buttons["snooze"] = QPushButton("延后提醒")
        self.buttons["snooze"].clicked.connect(lambda: self._selected_action("snooze"))
        row.addWidget(self.snooze_seconds)
        row.addWidget(self.buttons["snooze"])
        layout.addLayout(row)
        self.snooze_preview = label("")
        layout.addWidget(self.snooze_preview)
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

    def busy(self, task_id):
        return any(p["task_id"] == task_id for p in self.pending.values())

    def _payload(self):
        return {"content": self.content.text().strip(), "mode": self.mode.currentData(),
                "delay_s": self.delay.value(), "window_s": self.window.value()}

    def _create(self):
        if self.create_button.isEnabled():
            self.request("create", f"c-task-{uuid4().hex}", 0, self._payload())

    def request(self, kind, task_id, version, payload=None):
        view = self.view(task_id)
        if self.busy(task_id) or (kind != "create" and (not view or view["version"] != version)):
            self.result.setText("事项已更新或操作尚未完成，请查看最新安排。")
            return None
        try:
            event_id = self.submit(f"reminder.{kind}", task_id=task_id, version=version, payload=payload or {})
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
                         {"seconds": self.snooze_seconds.value()} if kind == "snooze" else {})

    def open_edit(self, task_id):
        view = self.view(task_id)
        if not view or view["status"] not in ("draft", "registered") or self.busy(task_id):
            return
        self._editing = (task_id, view["version"])
        self.content.setText(view["draft"]["content"])
        self.mode.setCurrentIndex(self.mode.findData(view["draft"]["mode"]))
        now = datetime.fromisoformat(self.snapshot["now"])
        earliest = datetime.fromisoformat(view["draft"]["earliest_at"])
        latest = datetime.fromisoformat(view["draft"]["latest_first_delivery_at"])
        self.delay.setValue(max(0, round((earliest - now).total_seconds())))
        self.window.setValue(max(1, round((latest - earliest).total_seconds())))
        self.edit_note.setText("正在修改：" + view["draft"]["content"] + "。保存前原安排保持有效。")
        self._refresh_controls()
        self.edit_opened.emit()

    def _save_edit(self):
        if self.save_edit.isEnabled():
            task_id, version = self._editing
            self.request("edit", task_id, version, self._payload())

    def _cancel_edit(self):
        self._editing = None
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
            titles = {"create": "草稿已创建，请核对时间后确认。", "confirm": "提醒已确认。", "done": "事项已完成。",
                      "snooze": "已延后，请查看新的提醒时间。", "edit": "安排已更新。", "cancel": "提醒已取消。"}
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
        rows = [(v["task_id"], v["version"], v["displayed_at"]) for v in views]
        if rows != self._rows:
            old = self.selected()
            selected = self._select_next or (old["task_id"] if old else None)
            self.tasks.blockSignals(True)
            self.tasks.clear()
            for view in views:
                item = QListWidgetItem(view["draft"]["content"] + " · " + task_status(view))
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
        self.details.setText((view["draft"]["content"] + "\n" + task_status(view) + "\n" + schedule_text(view["draft"], self.snapshot.get('now')))
                             if view else "尚无事项。创建草稿后，请核对时间并确认。")
        self._refresh_controls()

    def _refresh_controls(self):
        ready = bool(self.snapshot)
        view = self.selected()
        available = ready and view is not None and not self.busy(view["task_id"])
        status = view["status"] if view else None
        valid = ready and bool(self.content.text().strip())
        self.window.setEnabled(self.mode.currentData() == "soft")
        self.create_button.setEnabled(valid and self._editing is None and not any(p["kind"] == "create" for p in self.pending.values()))
        self.save_edit.setVisible(self._editing is not None)
        self.cancel_edit.setVisible(self._editing is not None)
        editing_view = self.view(self._editing[0]) if self._editing else None
        self.save_edit.setEnabled(valid and editing_view is not None and editing_view["status"] in ("draft", "registered") and not self.busy(editing_view["task_id"]))
        self.cancel_edit.setEnabled(not self._editing or not self.busy(self._editing[0]))
        for kind, button in self.buttons.items():
            allowed = status == "draft" if kind == "confirm" else status in ("draft", "registered") if kind in ("edit", "cancel") else status == "registered"
            button.setEnabled(available and allowed)
        if ready:
            now = datetime.fromisoformat(self.snapshot["now"])
            start = now + timedelta(seconds=self.delay.value())
            end = start + timedelta(seconds=self.window.value() if self.mode.currentData() == "soft" else 0)
            self.preview.setText(schedule_text({"mode": self.mode.currentData(), "earliest_at": start.isoformat(), "latest_first_delivery_at": end.isoformat()}, now))
            self.snooze_preview.setText("延后后的最早时间：" + display_time(now + timedelta(seconds=self.snooze_seconds.value()), now))


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
        self.snooze.clicked.connect(lambda: self._action("snooze", {"seconds": self.snooze_seconds.value()}))
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
            self.arrangement.setText(schedule_text(view["draft"], self.controller.snapshot.get('now')))
        if self.controller.snapshot:
            now = datetime.fromisoformat(self.controller.snapshot["now"])
            self.snooze_preview.setText("延后后的最早时间：" + display_time(now + timedelta(seconds=self.snooze_seconds.value()), now))
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
