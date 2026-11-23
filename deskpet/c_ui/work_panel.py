"""Snapshot-driven work-session controls and explicit user-confirmed reviews."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from PyQt6.QtCore import QDateTime, Qt, QTimeZone, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDateTimeEdit, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton, QSpinBox, QTextEdit, QVBoxLayout,
)

from deskpet.timezones import resolve_timezone


STATES = {"focusing": "专注中", "paused": "已暂停", "break_due": "本轮时间到", "ended": "已结束"}
ERRORS = {
    "STALE_TASK": "工作段已在其他入口更新，请查看最新状态后重试。",
    "INVALID_TRANSITION": "当前阶段不支持此操作，请查看最新状态。",
    "INVALID_ARGUMENT": "输入无效，请检查目标、时长或回顾内容。",
    "NOT_FOUND": "工作段已不存在，请重新选择。",
}


def label(text):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    return widget


def duration_text(seconds):
    minutes, seconds = divmod(math.ceil(seconds), 60)
    return f"{minutes:02d}:{seconds:02d}"


class WorkPanel(QGroupBox):
    review_opened = pyqtSignal()

    def __init__(self, *, submit, parent=None):
        super().__init__("专注工作段", parent)
        self.submit = submit
        self.snapshot = {}
        self.pending = None
        self._select_when_available = None
        self._review_session = None
        self._drafts = {}
        self._none_drafts = {}
        self._review_offers = {}
        self._session_rows = None
        self._task_rows = None

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.goal = QLineEdit()
        self.goal.setMaxLength(500)
        self.goal.setPlaceholderText("例如：阅读论文的方法部分")
        self.goal.textChanged.connect(self._refresh_controls)
        self.duration = QSpinBox()
        self.duration.setRange(1, 86400)
        self.duration.setValue(1500)
        self.duration.setSuffix(" 秒")
        self.task = QComboBox()
        self.task.addItem("不关联事项", None)
        form.addRow("工作目标", self.goal)
        form.addRow("时长（1500 秒 = 25 分钟）", self.duration)
        form.addRow("关联事项（可选）", self.task)
        layout.addLayout(form)
        self.start_button = QPushButton("开始工作段")
        self.start_button.setObjectName("c-session-start")
        self.start_button.clicked.connect(lambda: self._command("start"))
        layout.addWidget(self.start_button)

        self.sessions = QListWidget()
        self.sessions.setObjectName("c-session-list")
        self.sessions.setMaximumHeight(95)
        self.sessions.currentItemChanged.connect(self._selection_changed)
        layout.addWidget(self.sessions)
        self.summary = label("尚无工作段，填写目标后即可开始。")
        layout.addWidget(self.summary)
        row = QHBoxLayout()
        self.buttons = {"start": self.start_button}
        for kind, title in (("pause", "暂停"), ("resume", "继续"), ("end", "结束并回顾")):
            button = QPushButton(title)
            button.setObjectName(f"c-session-{kind}")
            button.clicked.connect(lambda checked=False, k=kind: self._open_review() if k == "end" else self._command(k))
            row.addWidget(button)
            self.buttons[kind] = button
        layout.addLayout(row)

        self.review_editor = QGroupBox("确认本轮成果")
        review_layout = QVBoxLayout(self.review_editor)
        review_layout.addWidget(label("请填写实际完成的内容和下一步，每行一项，可留空。确认结束不会自动完成关联事项。"))
        self.outcomes = QTextEdit()
        self.outcomes.setAcceptRichText(False)
        self.outcomes.setPlaceholderText("已完成的成果")
        self.outcomes.setMaximumHeight(65)
        self.next_steps = QTextEdit()
        self.next_steps.setAcceptRichText(False)
        self.next_steps.setPlaceholderText("下一步")
        self.next_steps.setMaximumHeight(65)
        review_layout.addWidget(self.outcomes)
        review_layout.addWidget(self.next_steps)
        self.confirm_none = QCheckBox("我确认本轮没有完成项（可填写下一步）")
        review_layout.addWidget(self.confirm_none)
        self.review_notice = label("")
        review_layout.addWidget(self.review_notice)
        review_buttons = QHBoxLayout()
        self.confirm_end = QPushButton("确认结束并保存回顾")
        self.confirm_end.setObjectName("c-review-confirm")
        self.confirm_end.clicked.connect(lambda: self._command("end"))
        self.cancel_end = QPushButton("返回工作段")
        self.cancel_end.clicked.connect(self._close_review)
        review_buttons.addWidget(self.confirm_end)
        review_buttons.addWidget(self.cancel_end)
        review_layout.addLayout(review_buttons)
        layout.addWidget(self.review_editor)
        self.review_editor.hide()
        self.review = label("工作回顾：结束工作段后展示用户确认的成果与下一步。")
        layout.addWidget(self.review)
        self.result = label("填写目标，设置时长，开始一段专注。")
        layout.addWidget(self.result)
        self._refresh_controls()

    def current_session(self):
        item = self.sessions.currentItem()
        session_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        return next((v for v in self.snapshot.get("sessions", []) if v["session_id"] == session_id), None)

    def _refresh_controls(self):
        # Called only after construction, or after text changes once widgets exist.
        if not hasattr(self, "buttons"):
            return
        ready = bool(self.snapshot) and self.pending is None
        view = self.current_session()
        state = view["state"] if view else None
        editing = self._review_session is not None
        self.start_button.setEnabled(ready and not editing and bool(self.goal.text().strip()))
        self.buttons["pause"].setEnabled(ready and not editing and state == "focusing")
        self.buttons["resume"].setEnabled(ready and not editing and state == "paused")
        self.buttons["end"].setEnabled(ready and not editing and state in ("focusing", "paused", "break_due"))
        self.confirm_end.setEnabled(ready and editing and view is not None
            and view["session_id"] == self._review_session and state != "ended")
        self.cancel_end.setEnabled(self.pending is None)
        self.sessions.setEnabled(self.pending is None)
        self.outcomes.setReadOnly(self.pending is not None)
        self.next_steps.setReadOnly(self.pending is not None)
        self.confirm_none.setEnabled(self.pending is None)

    def _command(self, kind):
        button = self.confirm_end if kind == "end" else self.buttons[kind]
        if not button.isEnabled():
            return
        if kind == "start":
            payload = {"session_id": f"c-session-{uuid4().hex}", "expected_session_version": 0,
                       "goal": self.goal.text().strip(), "duration_s": self.duration.value(),
                       "task_id": self.task.currentData()}
        else:
            view = self.current_session()
            payload = {"session_id": view["session_id"], "expected_session_version": view["version"]}
            if kind == "end":
                self._save_draft()
                outcomes = self._lines(self.outcomes)
                if outcomes and self.confirm_none.isChecked():
                    self.result.setText("已填写成果时不能同时确认没有完成项，请检查后提交。")
                    return
                payload.update(confirmed_outcomes=outcomes, next_steps=self._lines(self.next_steps),
                    _review={**self._review_offers[view["session_id"]],
                             "explicit_no_completed_items": self.confirm_none.isChecked()})
        try:
            event_id = self.submit(f"session.{kind}", payload=payload)
        except (RuntimeError, ValueError) as exc:
            self.result.setText(f"暂时无法提交：{exc}。输入已保留。")
            return
        self.pending = {"event_id": event_id, "kind": kind, "session_id": payload["session_id"]}
        self.result.setText("正在提交…")
        self._refresh_controls()

    @staticmethod
    def _lines(editor):
        return [line.strip() for line in editor.toPlainText().splitlines() if line.strip()]

    def receive_result(self, result):
        if self.pending is None or result.get("command_id", result.get("event_id")) != self.pending["event_id"]:
            return
        pending, self.pending = self.pending, None
        if result.get("status") in ("applied", "duplicate"):
            if pending["kind"] == "start":
                self._select_when_available = pending["session_id"]
                self.result.setText("工作段已创建。")
            elif pending["kind"] == "end":
                self._drafts.pop(pending["session_id"], None)
                self._none_drafts.pop(pending["session_id"], None)
                self._review_offers.pop(pending["session_id"], None)
                self._review_session = None
                self.review_editor.hide()
                self.result.setText("已结束，回顾已保存。空白回顾不计为确认没有完成项。")
            else:
                self.result.setText("已暂停。" if pending["kind"] == "pause" else "已继续。")
        else:
            error = result.get("error_code")
            self.result.setText(ERRORS.get(error, f"提交未成功：{error or result.get('status')}") + " 输入已保留。")
        # Results are acknowledgements; views are updated only by the shared snapshot.
        self._refresh_controls()

    def _save_draft(self):
        if self._review_session is not None:
            self._drafts[self._review_session] = (self.outcomes.toPlainText(), self.next_steps.toPlainText())
            self._none_drafts[self._review_session] = self.confirm_none.isChecked()

    def _open_review(self):
        if not self.buttons["end"].isEnabled():
            return
        self._review_session = self.current_session()["session_id"]
        self._review_offers.setdefault(self._review_session, {
            "review_id": f"c-review-{uuid4().hex}", "session_id": self._review_session,
            "offered_at": self.snapshot.get("now"),
        })
        outcomes, next_steps = self._drafts.get(self._review_session, ("", ""))
        self.outcomes.setPlainText(outcomes)
        self.next_steps.setPlainText(next_steps)
        self.confirm_none.setChecked(self._none_drafts.get(self._review_session, False))
        self.review_notice.setText("确认前工作段保持当前状态；返回后可以继续操作。")
        self.review_editor.show()
        self._refresh_controls()
        self.review_opened.emit()

    def _close_review(self):
        self._save_draft()
        self._review_session = None
        self.review_editor.hide()
        self._refresh_controls()

    def _selection_changed(self):
        if self._review_session is not None:
            self._close_review()
        self._render_details()

    def render_snapshot(self, snapshot):
        self.snapshot = snapshot
        tasks = [(v["task_id"], v["draft"]["content"]) for v in snapshot.get("tasks", [])]
        if tasks != self._task_rows:
            selected = self.task.currentData()
            self.task.clear()
            self.task.addItem("不关联事项", None)
            for task_id, content in tasks:
                self.task.addItem(content, task_id)
            self.task.setCurrentIndex(max(0, self.task.findData(selected)))
            self._task_rows = tasks
        views = snapshot.get("sessions", [])
        rows = [(v["session_id"], v["version"], v["goal"], duration_text(v["remaining_s"])) for v in views]
        if rows != self._session_rows:
            current = self.sessions.currentItem()
            selected = self._select_when_available or (current.data(Qt.ItemDataRole.UserRole) if current else None)
            self.sessions.blockSignals(True)
            self.sessions.clear()
            for view in views:
                item = QListWidgetItem(f"{view['goal']} · {STATES[view['state']]} · {duration_text(view['remaining_s'])}")
                item.setData(Qt.ItemDataRole.UserRole, view["session_id"])
                self.sessions.addItem(item)
                if view["session_id"] == selected:
                    self.sessions.setCurrentItem(item)
            if self.sessions.currentRow() < 0 and self.sessions.count():
                self.sessions.setCurrentRow(self.sessions.count() - 1)
            if self._select_when_available in {v["session_id"] for v in views}:
                self._select_when_available = None
            self.sessions.blockSignals(False)
            self._session_rows = rows
        self._render_details()

    def _render_details(self):
        view = self.current_session()
        if view is None:
            self.summary.setText("尚无工作段，填写目标后即可开始。")
            self.review.setText("工作回顾：尚未选择工作段。")
        else:
            self.summary.setText(f"{view['goal']} · {STATES[view['state']]} · 剩余 {duration_text(view['remaining_s'])}")
            if view["state"] == "break_due":
                self.summary.setText(self.summary.text() + "\n本轮时间已到，请结束并确认成果；尚未自动开始休息。")
            if view["state"] == "ended":
                outcomes = "\n".join(f"• {s}" for s in view["confirmed_outcomes"]) or "未填写"
                next_steps = "\n".join(f"• {s}" for s in view["next_steps"]) or "未填写"
                self.review.setText(f"工作回顾 · {view['goal']}\n确认成果：\n{outcomes}\n下一步：\n{next_steps}")
                if self._review_session == view["session_id"]:
                    self.review_notice.setText("此工作段已在其他入口结束。当前草稿仍保留，但不能再次结束。")
            else:
                self.review.setText("结束时可填写本轮成果与下一步，确认后在此展示。")
        self._refresh_controls()


PHASE_STATES = {
    "ready": "待开始", "running": "计时中", "paused": "已暂停",
    "elapsed": "时间到", "ended": "已结束",
}


class PhasePanel(QGroupBox):
    """The desktop timer, driven by acknowledged actions and the shared phase view."""

    def __init__(self, *, submit, timezone=None, parent=None):
        super().__init__("专注与回顾", parent)
        self.setObjectName("phase-panel")
        self.submit = submit
        self.zone = resolve_timezone(timezone) if timezone is not None else None
        self.snapshot = {}
        self.pending = None
        self._duration_edited = False
        self._task_rows = None
        self._review_issue_rows = None
        self._review_interval_rows = None
        self._linked_phase = None
        self._review_target = None
        self._recovery_target = None
        self._adjust_phase_id = None
        self._editing_details = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(9, 14, 9, 9)
        layout.setSpacing(6)
        self.summary = label("当前没有专注")
        self.summary.setObjectName("phase-summary")
        layout.addWidget(self.summary)

        self.goal = QLineEdit()
        self.goal.setObjectName("phase-title")
        self.goal.setMaxLength(500)
        self.goal.setPlaceholderText("专注标题（可留空）")
        layout.addWidget(self.goal)
        self.description = QLineEdit()
        self.description.setMaxLength(4000)
        self.description.setPlaceholderText("目标描述（可留空）")
        layout.addWidget(self.description)
        self.duration = self._minutes("专注时长")
        self.duration.setObjectName("phase-duration")
        self.duration.valueChanged.connect(self._edited_duration)
        layout.addWidget(self.duration)
        self.task = QComboBox()
        self.task.addItem("不关联事项", None)
        self.task.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.task.setAccessibleName("关联事项")
        layout.addWidget(self.task)

        self.buttons = {}
        self._button(layout, "start", "开始专注")
        self._button(layout, "continue_habit", "继续习惯")
        timing = QHBoxLayout()
        self._button(timing, "pause", "暂停")
        self._button(timing, "resume", "继续")
        self._button(timing, "end", "结束")
        layout.addLayout(timing)
        self.count_completed = QCheckBox("提前结束计为完成一轮")
        layout.addWidget(self.count_completed)
        details = QPushButton("编辑目标")
        details.setObjectName("phase-edit-details")
        details.clicked.connect(self._toggle_details)
        self.edit_details = details
        layout.addWidget(details)
        self._button(layout, "set_details", "保存目标")
        self._button(layout, "set_links", "保存关联")
        self._button(layout, "start_break", "开始休息")
        self._button(layout, "skip_break", "跳过休息")

        adjust = QHBoxLayout()
        self.adjust_mode = QComboBox()
        self.adjust_mode.addItem("总时长", "total_s")
        self.adjust_mode.addItem("额外增加", "add_s")
        adjust.addWidget(self.adjust_mode)
        self.adjust_minutes = self._minutes("调整时间")
        self.adjust_minutes.setSuffix(" 分")
        self.adjust_mode.currentIndexChanged.connect(self._set_adjust_initial)
        adjust.addWidget(self.adjust_minutes)
        self._button(adjust, "adjust", "调整")
        layout.addLayout(adjust)

        self.review_editor = QGroupBox("本轮回顾")
        review_layout = QVBoxLayout(self.review_editor)
        self.outcomes = QTextEdit()
        self.outcomes.setAcceptRichText(False)
        self.outcomes.setPlaceholderText("实际完成的内容，每行一项")
        self.outcomes.setFixedHeight(55)
        self.next_steps = QTextEdit()
        self.next_steps.setAcceptRichText(False)
        self.next_steps.setPlaceholderText("下一步，每行一项")
        self.next_steps.setFixedHeight(55)
        review_layout.addWidget(self.outcomes)
        review_layout.addWidget(self.next_steps)
        self._button(review_layout, "review", "保存回顾")
        self.interval_editor = QGroupBox("修改工时归属")
        interval_layout = QVBoxLayout(self.interval_editor)
        self.review_intervals = QComboBox()
        self.review_intervals.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.review_intervals.setAccessibleName("工时区间")
        self.review_intervals.currentIndexChanged.connect(self._select_review_interval)
        self.review_issue = QComboBox()
        self.review_issue.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.review_issue.setAccessibleName("新的工时归属")
        interval_layout.addWidget(self.review_intervals)
        interval_layout.addWidget(self.review_issue)
        self._button(interval_layout, "reassign_interval", "保存工时归属")
        review_layout.addWidget(self.interval_editor)
        layout.addWidget(self.review_editor)

        self.recovery_editor = QGroupBox("补记中断时间")
        recovery_layout = QVBoxLayout(self.recovery_editor)
        recovery_layout.addWidget(label("请选择你实际专注的起止时刻。"))
        self.credit_start = QDateTimeEdit(QDateTime.currentDateTime())
        self.credit_end = QDateTimeEdit(QDateTime.currentDateTime())
        for text, editor in (("开始", self.credit_start), ("结束", self.credit_end)):
            editor.setDisplayFormat("MM-dd HH:mm:ss" + (" ttt" if self.zone is not None else ""))
            if self.zone is not None:
                editor.setTimeZone(QTimeZone(self.zone.key.encode("ascii")))
            editor.setAccessibleName(text)
            row = QHBoxLayout()
            row.addWidget(label(text))
            row.addWidget(editor, 1)
            recovery_layout.addLayout(row)
        self._button(recovery_layout, "credit_recovery", "确认补记")
        layout.addWidget(self.recovery_editor)

        self.result = label("")
        self.result.setObjectName("phase-result")
        layout.addWidget(self.result)
        self.render_snapshot({})

    @staticmethod
    def _minutes(accessible_name):
        editor = QDoubleSpinBox()
        editor.setDecimals(2)
        editor.setRange(0.02, 1440)
        editor.setValue(25)
        editor.setSuffix(" 分钟")
        editor.setAccessibleName(accessible_name)
        return editor

    def _button(self, layout, action, text):
        button = QPushButton(text)
        button.setObjectName(f"phase-{action}")
        button.clicked.connect(lambda checked=False: self._command(action))
        layout.addWidget(button)
        self.buttons[action] = button

    def _edited_duration(self):
        self._duration_edited = True

    def _set_adjust_initial(self):
        phase = self.snapshot.get("active_phase")
        if phase:
            minutes = phase["planned_duration_s"] / 60 if self.adjust_mode.currentData() == "total_s" else 5
            self.adjust_minutes.setValue(minutes)

    def _toggle_details(self):
        self._editing_details = not self._editing_details
        self.render_snapshot(self.snapshot)

    def render_snapshot(self, snapshot):
        self.snapshot = snapshot
        active = snapshot.get("active_phase")
        is_focus = bool(active and active["kind"] == "focus")
        state = active["state"] if active else None
        self.summary.setText(
            f"{active['title']} · {PHASE_STATES[state]}\n"
            f"剩余 {duration_text(active['remaining_s'])} / {duration_text(active['planned_duration_s'])}"
            if active else "当前没有专注"
        )
        phase_key = (active["id"], active.get("primary_issue_id")) if is_focus else None
        self._render_tasks(snapshot, phase_key)
        if not self._duration_edited:
            seconds = snapshot.get("phase_defaults", {}).get("default_focus_s", 1500)
            blocked = self.duration.blockSignals(True)
            self.duration.setValue(seconds / 60)
            self.duration.blockSignals(blocked)
        link_enabled = snapshot.get("phase_defaults", {}).get("link_issues", False)
        self.task.setVisible(link_enabled and (is_focus or not active))
        self.adjust_mode.setVisible(bool(active))
        self.adjust_minutes.setVisible(bool(active))
        active_id = active["id"] if active else None
        if active_id != self._adjust_phase_id:
            self._adjust_phase_id = active_id
            self._editing_details = False
            self.goal.setText(active["title"] if is_focus else "")
            self.description.setText((active.get("description") or "") if is_focus else "")
            self.count_completed.setChecked(False)
            self._set_adjust_initial()
        self.goal.setVisible(not active or self._editing_details)
        self.description.setVisible(not active or self._editing_details)
        self.duration.setVisible(not active)
        self.edit_details.setVisible(is_focus)
        self.edit_details.setText("收起目标" if self._editing_details else "编辑目标")
        self.count_completed.setVisible(bool(is_focus and active.get("habit_id") and state != "elapsed"))

        continuation = snapshot.get("phase_habit_continuation")
        if continuation:
            self.buttons["continue_habit"].setText(
                f"继续习惯 · {continuation['title'][:20]} · {duration_text(continuation['duration_s'])}")
        suggested = snapshot.get("phase_suggested_break")
        if suggested:
            title = "长休息" if suggested["kind"] == "long_break" else "短休息"
            self.buttons["start_break"].setText(f"开始{title} · {duration_text(suggested['duration_s'])}")
        else:
            self.buttons["start_break"].setText("开始休息")
        visible = {
            "start": active is None, "continue_habit": bool(continuation and active is None),
            "pause": state == "running", "resume": state == "paused",
            "end": is_focus, "start_break": bool(suggested or not active),
            "skip_break": bool(active and not is_focus), "adjust": bool(active),
            "set_links": is_focus and link_enabled,
            "set_details": is_focus and self._editing_details,
        }
        for action, shown in visible.items():
            self.buttons[action].setVisible(shown)
            self.buttons[action].setEnabled(shown and self._ready())
        self._render_review(active)
        self._render_recovery(active)

    def _ready(self):
        return "phase_defaults" in self.snapshot and self.pending is None

    @staticmethod
    def _issues(snapshot, *, include_terminal=False):
        if "items" in snapshot:
            return [(item["id"], item["title"]) for item in snapshot["items"]
                    if item["type"] == "issue" and (include_terminal or item["status"] not in ("completed", "cancelled"))]
        return [(task["task_id"], task["draft"]["content"]) for task in snapshot.get("tasks", [])
                if task["draft"].get("item_type") == "issue"
                and (include_terminal or task["status"] not in ("completed", "cancelled"))]

    def _render_tasks(self, snapshot, phase_key):
        rows = self._issues(snapshot)
        selected = phase_key[1] if phase_key and phase_key != self._linked_phase else self.task.currentData()
        if rows != self._task_rows:
            self.task.clear()
            self.task.addItem("不关联事项", None)
            for task_id, title in rows:
                self.task.addItem(title, task_id)
            self._task_rows = rows
        if phase_key != self._linked_phase or selected != self.task.currentData():
            self.task.setCurrentIndex(max(0, self.task.findData(selected)))
        self._linked_phase = phase_key

    def _review_phase(self, active):
        if active and active["kind"] == "focus":
            return active if active["state"] == "elapsed" else None
        return next((phase for phase in reversed(self.snapshot.get("phases", []))
                     if phase["kind"] == "focus" and phase["state"] in ("elapsed", "ended")), None)

    def _render_review(self, active):
        phase = self._review_phase(active)
        self.review_editor.setVisible(phase is not None)
        self.buttons["review"].setEnabled(phase is not None and self._ready())
        target = (phase["id"], phase["version"]) if phase else None
        if target != self._review_target and phase:
            self.outcomes.setPlainText("\n".join(phase["confirmed_outcomes"]))
            self.next_steps.setPlainText("\n".join(phase["next_steps"]))
        self._review_target = target
        self._render_review_intervals(phase)

    def _render_review_intervals(self, phase):
        intervals = [interval for interval in self.snapshot.get("phase_review_intervals", [])
                     if phase and interval["phase_id"] == phase["id"]]
        self.interval_editor.setVisible(bool(intervals))
        self.buttons["reassign_interval"].setEnabled(bool(intervals) and self._ready())
        issues = self._issues(self.snapshot, include_terminal=True)
        changed = False
        if issues != self._review_issue_rows:
            self.review_issue.clear()
            self.review_issue.addItem("不归属事项", None)
            for issue_id, title in issues:
                self.review_issue.addItem(title, issue_id)
            self._review_issue_rows = issues
            changed = True
        if intervals != self._review_interval_rows:
            selected = self.review_intervals.currentData()
            blocked = self.review_intervals.blockSignals(True)
            self.review_intervals.clear()
            for interval in intervals:
                started = datetime.fromisoformat(interval["started_at"]).astimezone(self.zone)
                self.review_intervals.addItem(
                    f"{started:%m-%d %H:%M:%S} · {duration_text(interval['seconds'])}", interval)
                if selected and selected["id"] == interval["id"]:
                    self.review_intervals.setCurrentIndex(self.review_intervals.count() - 1)
            self.review_intervals.blockSignals(blocked)
            self._review_interval_rows = intervals
            changed = True
        if changed:
            self._select_review_interval()

    def _select_review_interval(self):
        interval = self.review_intervals.currentData() or {}
        self.review_issue.setCurrentIndex(max(0, self.review_issue.findData(interval.get("primary_issue_id"))))

    def _recovery(self, active):
        phase = active if active and active["kind"] == "focus" else self._review_phase(active)
        if phase is None or phase["state"] not in ("paused", "ended") or phase["remaining_s"] <= 0:
            return phase, None
        return phase, next((item for item in reversed(self.snapshot.get("phase_recovery", []))
                            if item["phase_id"] == phase["id"] and item["credited_seconds"] is None
                            and item["observed_until_at"]
                            and datetime.fromisoformat(item["observed_until_at"]) > datetime.fromisoformat(item["started_at"])), None)

    def _render_recovery(self, active):
        phase, item = self._recovery(active)
        self.recovery_editor.setVisible(item is not None)
        self.buttons["credit_recovery"].setEnabled(item is not None and self._ready())
        key = (item["id"], item["version"]) if item else None
        if key != self._recovery_target and item:
            start = datetime.fromisoformat(item["started_at"])
            end = min(datetime.fromisoformat(item["observed_until_at"]),
                      start + timedelta(seconds=phase["remaining_s"]))
            self.credit_start.setDateTime(QDateTime.fromMSecsSinceEpoch(
                math.ceil(start.timestamp() * 1000), self.credit_start.timeZone(),
            ))
            self.credit_end.setDateTime(QDateTime.fromMSecsSinceEpoch(
                math.floor(end.timestamp() * 1000), self.credit_end.timeZone(),
            ))
        self._recovery_target = key

    def _command(self, action):
        if not self._ready() or not self.buttons[action].isEnabled():
            return
        active = self.snapshot.get("active_phase")
        if action in ("start", "start_break", "continue_habit"):
            payload = {"session_id": f"phase-{uuid4().hex}", "expected_session_version": 0}
            if action == "start":
                payload["title"] = self.goal.text().strip()
                payload["description"] = self.description.text().strip()
                if self._duration_edited:
                    payload["duration_s"] = round(self.duration.value() * 60)
                if not self.task.isHidden():
                    payload["task_id"] = self.task.currentData()
            elif action == "start_break":
                suggestion = self.snapshot.get("phase_suggested_break")
                if suggestion:
                    payload.update({k: v for k, v in suggestion.items() if k != "duration_s"})
            else:
                continuation = self.snapshot["phase_habit_continuation"]
                payload.update({k: v for k, v in continuation.items() if k not in ("duration_s", "title")})
                action = "start"
        else:
            phase = self._review_phase(active) if action in ("review", "reassign_interval") else active
            recovery = None
            if action == "credit_recovery":
                phase, recovery = self._recovery(active)
            payload = {"session_id": phase["id"], "expected_session_version": phase["version"]}
            if action == "adjust":
                payload[self.adjust_mode.currentData()] = round(self.adjust_minutes.value() * 60)
            if action == "end":
                payload["count_as_completed"] = self.count_completed.isChecked()
            if action == "set_details":
                payload.update(title=self.goal.text().strip(), description=self.description.text().strip())
            if action == "set_links":
                primary = self.task.currentData()
                payload.update(issue_links=[primary] if primary else [], primary_issue_id=primary)
            if action == "review":
                payload.update(confirmed_outcomes=WorkPanel._lines(self.outcomes),
                               next_steps=WorkPanel._lines(self.next_steps))
            if action == "reassign_interval":
                interval = self.review_intervals.currentData()
                payload.update(interval_id=interval["id"], expected_interval_version=interval["version"],
                               target_issue_id=self.review_issue.currentData())
            if recovery:
                payload.update(recovery_interval_id=recovery["id"], expected_recovery_version=recovery["version"],
                    started_at=datetime.fromtimestamp(self.credit_start.dateTime().toMSecsSinceEpoch() / 1000,
                                                      tz=timezone.utc).isoformat(),
                    ended_at=datetime.fromtimestamp(self.credit_end.dateTime().toMSecsSinceEpoch() / 1000,
                                                    tz=timezone.utc).isoformat())
        try:
            self.pending = self.submit(f"session.{action}", payload=payload)
        except (RuntimeError, ValueError) as exc:
            self.result.setText(str(exc))
            return
        self.result.setText("正在保存…")
        self.render_snapshot(self.snapshot)

    def receive_result(self, result):
        if self.pending is None or (result.get("command_id") or result.get("event_id")) != self.pending:
            return
        self.pending = None
        if result.get("status") == "applied" or result.get("original_status") == "applied":
            self.result.setText("已保存")
        else:
            self.result.setText(ERRORS.get(result.get("error_code"), "未能保存此操作"))
        self.render_snapshot(self.snapshot)
