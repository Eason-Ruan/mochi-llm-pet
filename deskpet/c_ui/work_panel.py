"""Snapshot-driven work-session controls and explicit user-confirmed reviews."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from html import escape
from uuid import uuid4

from PyQt6.QtCore import QDateTime, Qt, QTimeZone, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDateTimeEdit, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton, QSizePolicy, QSpinBox, QTextEdit, QVBoxLayout, QWidget,
)

from deskpet.i18n import language, tr
from deskpet.timezones import resolve_timezone
from .refine_style import MUTED, PANEL_WIDTH, MenuField, _card, _text, stepper


STATES = {
    "focusing": ("专注中", "Focusing"),
    "paused": ("已暂停", "Paused"),
    "break_due": ("本轮时间到", "Time is up"),
    "ended": ("已结束", "Ended"),
}
ERRORS = {
    "STALE_TASK": ("工作段已在其他入口更新，请查看最新状态后重试。",
                   "This session changed elsewhere. Check its current state and try again."),
    "INVALID_TRANSITION": ("当前阶段不支持此操作，请查看最新状态。",
                           "Check the current session state before trying this action again."),
    "INVALID_ARGUMENT": ("输入无效，请检查目标、时长或回顾内容。",
                         "Check the goal, duration, or review and try again."),
    "NOT_FOUND": ("工作段已不存在，请重新选择。",
                  "This session is no longer available. Select another session."),
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
        super().__init__(tr("专注工作段", "Focus session"), parent)
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
        self.goal.setPlaceholderText(tr("例如：阅读论文的方法部分", "e.g. Read the paper’s methods section"))
        self.goal.textChanged.connect(self._refresh_controls)
        self.duration = QSpinBox()
        self.duration.setRange(1, 86400)
        self.duration.setValue(1500)
        self.duration.setSuffix(tr(" 秒", " s"))
        self.task = QComboBox()
        self.task.addItem(tr("不关联事项", "No linked item"), None)
        form.addRow(tr("工作目标", "Goal"), self.goal)
        form.addRow(tr("时长（1500 秒 = 25 分钟）", "Duration (1500 s = 25 min)"), self.duration)
        form.addRow(tr("关联事项（可选）", "Linked item (optional)"), self.task)
        layout.addLayout(form)
        self.start_button = QPushButton(tr("开始工作段", "Start session"))
        self.start_button.setObjectName("c-session-start")
        self.start_button.clicked.connect(lambda: self._command("start"))
        layout.addWidget(self.start_button)

        self.sessions = QListWidget()
        self.sessions.setObjectName("c-session-list")
        self.sessions.setMaximumHeight(95)
        self.sessions.currentItemChanged.connect(self._selection_changed)
        layout.addWidget(self.sessions)
        self.summary = label(tr("尚无工作段，填写目标后即可开始。", "Set a goal to start a focus session."))
        layout.addWidget(self.summary)
        row = QHBoxLayout()
        self.buttons = {"start": self.start_button}
        for kind, title in (
            ("pause", tr("暂停", "Pause")),
            ("resume", tr("继续", "Resume")),
            ("end", tr("结束并回顾", "End and review")),
        ):
            button = QPushButton(title)
            button.setObjectName(f"c-session-{kind}")
            button.clicked.connect(lambda checked=False, k=kind: self._open_review() if k == "end" else self._command(k))
            row.addWidget(button)
            self.buttons[kind] = button
        layout.addLayout(row)

        self.review_editor = QGroupBox(tr("确认本轮成果", "Confirm session outcomes"))
        review_layout = QVBoxLayout(self.review_editor)
        review_layout.addWidget(label(tr(
            "请填写实际完成的内容和下一步，每行一项，可留空。确认结束不会自动完成关联事项。",
            "Enter what you finished and your next steps, one per line. Both fields are optional. Linked items stay open.",
        )))
        self.outcomes = QTextEdit()
        self.outcomes.setAcceptRichText(False)
        self.outcomes.setPlaceholderText(tr("已完成的成果", "Completed work"))
        self.outcomes.setMaximumHeight(65)
        self.next_steps = QTextEdit()
        self.next_steps.setAcceptRichText(False)
        self.next_steps.setPlaceholderText(tr("下一步", "Next steps"))
        self.next_steps.setMaximumHeight(65)
        review_layout.addWidget(self.outcomes)
        review_layout.addWidget(self.next_steps)
        self.confirm_none = QCheckBox(tr("我确认本轮没有完成项（可填写下一步）", "No completed work this session"))
        review_layout.addWidget(self.confirm_none)
        self.review_notice = label("")
        review_layout.addWidget(self.review_notice)
        review_buttons = QHBoxLayout()
        self.confirm_end = QPushButton(tr("确认结束并保存回顾", "End and save review"))
        self.confirm_end.setObjectName("c-review-confirm")
        self.confirm_end.clicked.connect(lambda: self._command("end"))
        self.cancel_end = QPushButton(tr("返回工作段", "Back to session"))
        self.cancel_end.clicked.connect(self._close_review)
        review_buttons.addWidget(self.confirm_end)
        review_buttons.addWidget(self.cancel_end)
        review_layout.addLayout(review_buttons)
        layout.addWidget(self.review_editor)
        self.review_editor.hide()
        self.review = label(tr(
            "工作回顾：结束工作段后展示用户确认的成果与下一步。",
            "Your completed work and next steps appear here after the session.",
        ))
        layout.addWidget(self.review)
        self.result = label(tr("填写目标，设置时长，开始一段专注。", "Set a goal and duration to start focusing."))
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
                    self.result.setText(tr(
                        "已填写成果时不能同时确认没有完成项，请检查后提交。",
                        "Clear the checkbox or remove the completed work before saving.",
                    ))
                    return
                payload.update(confirmed_outcomes=outcomes, next_steps=self._lines(self.next_steps),
                    _review={**self._review_offers[view["session_id"]],
                             "explicit_no_completed_items": self.confirm_none.isChecked()})
        try:
            event_id = self.submit(f"session.{kind}", payload=payload)
        except (RuntimeError, ValueError) as exc:
            self.result.setText(tr("暂时无法提交：{error}。输入已保留。",
                                   "Could not submit ({error}). Your input was kept.").format(error=exc))
            return
        self.pending = {"event_id": event_id, "kind": kind, "session_id": payload["session_id"]}
        self.result.setText(tr("正在提交…", "Submitting…"))
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
                self.result.setText(tr("工作段已创建。", "Session created."))
            elif pending["kind"] == "end":
                self._drafts.pop(pending["session_id"], None)
                self._none_drafts.pop(pending["session_id"], None)
                self._review_offers.pop(pending["session_id"], None)
                self._review_session = None
                self.review_editor.hide()
                self.result.setText(tr(
                    "已结束，回顾已保存。空白回顾不计为确认没有完成项。",
                    "Review saved. Use the checkbox to explicitly record a session with no completed work.",
                ))
            else:
                self.result.setText(tr("已暂停。", "Paused.") if pending["kind"] == "pause" else tr("已继续。", "Resumed."))
        else:
            error = result.get("error_code")
            message = tr(*ERRORS[error]) if error in ERRORS else tr(
                "提交未成功：{error}", "Submission failed ({error}).",
            ).format(error=error or result.get("status"))
            self.result.setText(message + tr(" 输入已保留。", " Your input was kept."))
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
        self.review_notice.setText(tr("确认前工作段保持当前状态；返回后可以继续操作。", "Confirm to end the session, or go back to keep working."))
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
            self.task.addItem(tr("不关联事项", "No linked item"), None)
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
                state = tr(*STATES[view["state"]])
                item = QListWidgetItem(f"{view['goal']} · {state} · {duration_text(view['remaining_s'])}")
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
            self.summary.setText(tr("尚无工作段，填写目标后即可开始。", "Set a goal to start a focus session."))
            self.review.setText(tr("工作回顾：尚未选择工作段。", "Select a session to see its review."))
        else:
            self.summary.setText(tr("{goal} · {state} · 剩余 {time}", "{goal} · {state} · {time} left").format(
                goal=view["goal"], state=tr(*STATES[view["state"]]), time=duration_text(view["remaining_s"]),
            ))
            if view["state"] == "break_due":
                self.summary.setText(self.summary.text() + "\n" + tr(
                    "本轮时间已到，请结束并确认成果；尚未自动开始休息。",
                    "Time is up. End the session and confirm your outcomes before starting a break.",
                ))
            if view["state"] == "ended":
                empty = tr("未填写", "Not entered")
                outcomes = "\n".join(f"• {s}" for s in view["confirmed_outcomes"]) or empty
                next_steps = "\n".join(f"• {s}" for s in view["next_steps"]) or empty
                self.review.setText(tr(
                    "工作回顾 · {goal}\n确认成果：\n{outcomes}\n下一步：\n{next_steps}",
                    "Session review · {goal}\nCompleted work\n{outcomes}\nNext steps\n{next_steps}",
                ).format(goal=view["goal"], outcomes=outcomes, next_steps=next_steps))
                if self._review_session == view["session_id"]:
                    self.review_notice.setText(tr(
                        "此工作段已在其他入口结束。当前草稿仍保留，但不能再次结束。",
                        "This session ended elsewhere. Your draft is still here.",
                    ))
            else:
                self.review.setText(tr("结束时可填写本轮成果与下一步，确认后在此展示。", "Add outcomes and next steps when you end the session."))
        self._refresh_controls()


PHASE_STATES = {
    "ready": ("待开始", "Ready"),
    "running": ("计时中", "Running"),
    "paused": ("已暂停", "Paused"),
    "elapsed": ("时间到", "Time is up"),
    "ended": ("已结束", "Ended"),
}


class MinutesSpinBox(QDoubleSpinBox):
    def textFromValue(self, value):
        return self.locale().toString(value, "f", 0 if value.is_integer() else self.decimals())


def _phase_editor_section(title):
    section = QGroupBox()
    section.setAccessibleName(title)
    layout = QVBoxLayout(section)
    # Reserve space for the heading in the same layout as the fields.
    layout.addWidget(_text(title, size=12))
    return section, layout


class PhasePanel(QGroupBox):
    """The desktop timer, driven by acknowledged actions and the shared phase view."""

    def __init__(self, *, submit, timezone=None, parent=None):
        super().__init__(tr("专注与回顾", "Focus & review"), parent)
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
        layout.setContentsMargins(0, 0, 2, 0)
        layout.setSpacing(8)
        self.summary = _text(tr("当前没有专注", "No active focus session"), size=12, color=MUTED, wrap=True)
        self.summary.setObjectName("phase-summary")
        layout.addWidget(self.summary)

        self.goal_card, goal_layout = _card(spacing=5)
        goal_layout.addWidget(_text(tr("专注目标", "Focus goal"), bold=True))
        goal_layout.addWidget(_text(tr("标题", "Title"), size=11, color=MUTED, bold=True))
        self.goal = QLineEdit()
        self.goal.setObjectName("phase-title")
        self.goal.setAccessibleName(tr("专注标题", "Focus title"))
        self.goal.setMaxLength(500)
        self.goal.setPlaceholderText(tr("专注标题（可留空）", "Focus title (optional)"))
        goal_layout.addWidget(self.goal)
        goal_layout.addWidget(_text(tr("描述", "Description"), size=11, color=MUTED, bold=True))
        self.description = QLineEdit()
        self.description.setAccessibleName(tr("目标描述", "Goal description"))
        self.description.setMaxLength(4000)
        self.description.setPlaceholderText(tr("目标描述（可留空）", "Goal description (optional)"))
        goal_layout.addWidget(self.description)
        layout.addWidget(self.goal_card)

        self.timing_card, setup = _card(spacing=8)
        self.duration = self._minutes(tr("专注时长", "Focus duration"))
        self.duration.setObjectName("phase-duration")
        self.duration.valueChanged.connect(self._edited_duration)
        self.duration_row = QWidget()
        duration = QHBoxLayout(self.duration_row)
        duration.setContentsMargins(0, 0, 0, 0)
        duration.addWidget(_text(tr("专注时长", "Focus duration"), size=12, bold=True), 1)
        duration.addWidget(stepper(self.duration))
        setup.addWidget(self.duration_row)

        self.task = MenuField()
        self.task.addItem(tr("不关联事项", "No linked item"), None)
        self.task.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.task.setAccessibleName(tr("关联事项", "Linked item"))
        self.task.setMinimumContentsLength(5)
        self.task.setFixedWidth(130)
        self.task_row = QWidget()
        linked = QHBoxLayout(self.task_row)
        linked.setContentsMargins(0, 0, 0, 0)
        linked.addWidget(_text(tr("关联事项", "Linked item"), size=12), 1)
        linked.addWidget(self.task)
        setup.addWidget(self.task_row)
        layout.addWidget(self.timing_card)

        self.buttons = {}
        self._button(layout, "start", tr("开始专注", "Start focusing"))
        self.buttons["start"].setProperty("primary", True)
        self._button(layout, "continue_habit", tr("继续习惯", "Continue habit"))
        timing = QHBoxLayout()
        self._button(timing, "pause", tr("暂停", "Pause"))
        self._button(timing, "resume", tr("继续", "Resume"))
        self._button(timing, "end", tr("结束", "End"))
        layout.addLayout(timing)
        self.count_completed = QCheckBox(tr("提前结束计为完成一轮", "Count as a full session"))
        layout.addWidget(self.count_completed)
        details = QPushButton(tr("编辑目标", "Edit goal"))
        details.setObjectName("phase-edit-details")
        details.clicked.connect(self._toggle_details)
        self.edit_details = details
        layout.addWidget(details)
        self._button(layout, "set_details", tr("保存目标", "Save goal"))
        self._button(layout, "set_links", tr("保存关联", "Save links"))
        self._button(layout, "start_break", tr("开始休息", "Start break"))
        self._button(layout, "skip_break", tr("跳过休息", "Skip break"))

        adjust = QHBoxLayout()
        self.adjust_mode = MenuField()
        self.adjust_mode.setAccessibleName(tr("调整方式", "Adjustment mode"))
        self.adjust_mode.addItem(tr("总时长", "Total"), "total_s")
        self.adjust_mode.addItem(tr("额外增加", "Add"), "add_s")
        adjust.addWidget(self.adjust_mode)
        self.adjust_minutes = self._minutes(tr("调整时间", "Adjust time"))
        self.adjust_minutes.setSuffix(tr(" 分", " min"))
        self.adjust_minutes.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.adjust_mode.currentIndexChanged.connect(self._set_adjust_initial)
        adjust.addWidget(self.adjust_minutes)
        self._button(adjust, "adjust", tr("调整", "Adjust"))
        layout.addLayout(adjust)

        self.review_editor, review_layout = _phase_editor_section(tr("本轮回顾", "Session review"))
        self.outcomes = QTextEdit()
        self.outcomes.setAcceptRichText(False)
        self.outcomes.setPlaceholderText(tr("实际完成的内容，每行一项", "Completed work, one per line"))
        self.outcomes.setFixedHeight(55)
        self.next_steps = QTextEdit()
        self.next_steps.setAcceptRichText(False)
        self.next_steps.setPlaceholderText(tr("下一步，每行一项", "Next steps, one per line"))
        self.next_steps.setFixedHeight(55)
        review_layout.addWidget(self.outcomes)
        review_layout.addWidget(self.next_steps)
        self._button(review_layout, "review", tr("保存回顾", "Save review"))
        self.interval_editor, interval_layout = _phase_editor_section(tr("修改工时归属", "Reassign tracked time"))
        self.review_intervals = MenuField()
        self.review_intervals.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.review_intervals.setAccessibleName(tr("工时区间", "Tracked interval"))
        self.review_intervals.currentIndexChanged.connect(self._select_review_interval)
        self.review_issue = MenuField()
        self.review_issue.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.review_issue.setAccessibleName(tr("新的工时归属", "New linked item"))
        interval_layout.addWidget(self.review_intervals)
        interval_layout.addWidget(self.review_issue)
        self._button(interval_layout, "reassign_interval", tr("保存工时归属", "Save assignment"))
        review_layout.addWidget(self.interval_editor)
        layout.addWidget(self.review_editor)

        self.recovery_editor, recovery_layout = _phase_editor_section(tr("补记中断时间", "Log time during interruption"))
        recovery_layout.addWidget(label(tr("请选择你实际专注的起止时刻。", "Select when you started and stopped focusing.")))
        self.credit_start = QDateTimeEdit(QDateTime.currentDateTime())
        self.credit_end = QDateTimeEdit(QDateTime.currentDateTime())
        for text, editor in ((tr("开始", "Start"), self.credit_start), (tr("结束", "End"), self.credit_end)):
            editor.setDisplayFormat("MM-dd HH:mm:ss" + (" ttt" if self.zone is not None else ""))
            if self.zone is not None:
                editor.setTimeZone(QTimeZone(self.zone.key.encode("ascii")))
            editor.setAccessibleName(text)
            row = QHBoxLayout()
            row.addWidget(label(text))
            row.addWidget(editor, 1)
            recovery_layout.addLayout(row)
        self._button(recovery_layout, "credit_recovery", tr("确认补记", "Save logged time"))
        layout.addWidget(self.recovery_editor)

        self.result = label("")
        self.result.setObjectName("phase-result")
        self.result.hide()
        layout.addWidget(self.result)
        self.render_snapshot({})

    @staticmethod
    def _minutes(accessible_name):
        editor = MinutesSpinBox()
        editor.setDecimals(2)
        editor.setRange(0.02, 1440)
        editor.setValue(25)
        editor.setSuffix(tr(" 分钟", " min"))
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
            tr("{title} · {state}\n剩余 {left} / {total}", "{title} · {state}\n{left} left / {total}").format(
                title=active["title"], state=tr(*PHASE_STATES[state]),
                left=duration_text(active["remaining_s"]), total=duration_text(active["planned_duration_s"]),
            )
            if active else tr("当前没有专注", "No active focus session")
        )
        phase_key = (active["id"], active.get("primary_issue_id")) if is_focus else None
        self._render_tasks(snapshot, phase_key)
        if not self._duration_edited:
            seconds = snapshot.get("phase_defaults", {}).get("default_focus_s", 1500)
            blocked = self.duration.blockSignals(True)
            self.duration.setValue(seconds / 60)
            self.duration.blockSignals(blocked)
        link_enabled = snapshot.get("phase_defaults", {}).get("link_issues", False)
        show_task = not active or (is_focus and link_enabled)
        self.task_row.setVisible(show_task)
        self.task.setEnabled(link_enabled)
        self.timing_card.setVisible(show_task)
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
        self.goal_card.setVisible(not active or self._editing_details)
        self.duration_row.setVisible(not active)
        self.edit_details.setVisible(is_focus)
        self.edit_details.setText(tr("收起目标", "Hide goal") if self._editing_details else tr("编辑目标", "Edit goal"))
        self.count_completed.setVisible(bool(is_focus and active.get("habit_id") and state != "elapsed"))

        continuation = snapshot.get("phase_habit_continuation")
        if continuation:
            button = self.buttons["continue_habit"]
            duration = duration_text(continuation["duration_s"])
            title = continuation["title"]
            template = tr("继续习惯 · {title} · {time}", "Continue · {title} · {time}")
            full_label = template.format(title=title, time=duration)
            button.setAccessibleName(full_label)
            button.setToolTip(f"<p>{escape(full_label)}</p>")
            if language() == "en":
                # Deduct the detail panel margins, scrollbar, and button padding.
                text_width = PANEL_WIDTH - 58
                available = text_width - button.fontMetrics().horizontalAdvance(template.format(title="", time=duration))
                title = button.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, available)
            else:
                title = title[:20]
            button.setText(template.format(title=title, time=duration))
        suggested = snapshot.get("phase_suggested_break")
        if suggested:
            action = tr("开始长休息", "Start long break") if suggested["kind"] == "long_break" else tr(
                "开始短休息", "Start short break",
            )
            self.buttons["start_break"].setText(f"{action} · {duration_text(suggested['duration_s'])}")
        else:
            self.buttons["start_break"].setText(tr("开始休息", "Start break"))
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
            self.task.addItem(tr("不关联事项", "No linked item"), None)
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
            self.review_issue.addItem(tr("不归属事项", "No linked item"), None)
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
            self._report(tr("{error}", "Could not save ({error}).").format(error=exc))
            return
        self._report(tr("正在保存…", "Saving…"))
        self.render_snapshot(self.snapshot)

    def _report(self, message):
        self.result.setText(message)
        self.result.show()

    def receive_result(self, result):
        if self.pending is None or (result.get("command_id") or result.get("event_id")) != self.pending:
            return
        self.pending = None
        if result.get("status") == "applied" or result.get("original_status") == "applied":
            self._report(tr("已保存", "Saved"))
        else:
            error = result.get("error_code")
            self._report(tr(*ERRORS[error]) if error in ERRORS else tr("未能保存此操作", "Could not save changes"))
        self.render_snapshot(self.snapshot)
