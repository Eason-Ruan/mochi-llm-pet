"""Snapshot-driven work-session controls and explicit user-confirmed reviews."""
from __future__ import annotations

import math
from uuid import uuid4

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPushButton, QSpinBox, QTextEdit, QVBoxLayout,
)


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
                self.result.setText("已结束，回顾已保存。")
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
                evidence = self.snapshot.get("session_reviews", {}).get(view["session_id"], {})
                confirmed_none = (evidence.get("version") == view["version"]
                                  and evidence.get("status") == "confirmed_none")
                outcomes = "\n".join(f"• {s}" for s in view["confirmed_outcomes"])
                if not outcomes:
                    outcomes = "已确认：本轮没有完成项" if confirmed_none else "未填写（未确认没有完成项）"
                next_steps = "\n".join(f"• {s}" for s in view["next_steps"]) or "未填写"
                self.review.setText(f"工作回顾 · {view['goal']}\n确认成果：\n{outcomes}\n下一步：\n{next_steps}")
                if self._review_session == view["session_id"]:
                    self.review_notice.setText("此工作段已在其他入口结束。当前草稿仍保留，但不能再次结束。")
            else:
                self.review.setText("结束时可填写本轮成果与下一步，确认后在此展示。")
        self._refresh_controls()
