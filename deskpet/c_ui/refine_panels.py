"""Reminder, focus and settings summaries for the desktop toolbar."""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from deskpet.i18n import tr

from .refine_icons import icon
from .refine_style import (
    CARD_OUTLINE, GREEN, GREEN_TINT, INK, MUTED, OUTLINE, PANEL_WIDTH, PAPER, SAND,
    _Panel, _card, _font, _scroll_area, _text, _title,
)
from .time_format import format_clock, format_day_clock, local_time
from .work_panel import STATES, duration_text


WORK_STATES = {
    **STATES,
    "running": ("休息中", "On break"),
    "elapsed": ("休息结束", "Break ended"),
    "ready": ("待开始", "Ready"),
}


def active_reminders(snapshot: dict) -> list[dict]:
    return sorted(
        (task for task in snapshot.get("tasks", ()) if task["status"] == "registered"
         and task["draft"].get("reminder_enabled", True)
         and not (task.get("acknowledged_at") or task.get("expired_at") or task.get("archived_at"))),
        key=lambda task: (
            task["draft"].get("remind_after") or task["draft"].get("earliest_at") or "",
            task["draft"]["mode"] != "hard",
            task["task_id"],
        ),
    )


def current_work(snapshot: dict) -> dict | None:
    sessions = snapshot.get("sessions", ())
    return next(
        (view for view in reversed(sessions) if view["state"] != "ended"),
        sessions[-1] if sessions else None,
    )


def _badge(text="", *, positive=True):
    badge = _text(text, size=11, color=GREEN if positive else MUTED, bold=True)
    _style_badge(badge, positive)
    badge.setFixedHeight(19)
    badge.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    return badge


def _style_badge(badge, positive):
    color, background = (GREEN, GREEN_TINT) if positive else (MUTED, SAND)
    badge.setStyleSheet(f"color: {color}; background: {background}; border-radius: 6px; padding: 0 7px")


def _schedule(draft, reference, zone=None):
    start_value = draft.get("remind_after") or draft.get("earliest_at")
    end_value = draft.get("remind_by") or draft.get("latest_first_delivery_at")
    today = local_time(reference, zone=zone).date() if reference is not None else None

    def day_and_clock(when):
        return format_day_clock(when, today)

    if draft["mode"] == "hard" and end_value:
        when = day_and_clock(local_time(end_value, zone=zone))
        return tr("{when} · 到点提醒", "{when} · At set time").format(when=when)
    if not (start_value and end_value):
        for key, label in (
            ("target_at", tr("目标", "Target")),
            ("remind_after", tr("最早", "From")),
            ("remind_by", tr("最迟", "By")),
        ):
            if draft.get(key):
                when = day_and_clock(local_time(draft[key], zone=zone))
                return tr("{label} {when} · 柔性提醒", "{label} {when} · Flexible").format(label=label, when=when)
        return tr("择机提醒", "Flexible reminder")
    start = local_time(start_value, zone=zone)
    end = local_time(end_value, zone=zone)
    finish = format_clock(end) if end.date() == start.date() else day_and_clock(end)
    return tr("{start}–{end} · 柔性提醒", "{start}–{end} · Flexible").format(start=day_and_clock(start), end=finish)


def _size_wrapped_label(label, line_width):
    if label.fontMetrics().horizontalAdvance(label.text()) <= line_width and "\n" not in label.text():
        label.setFixedHeight(label.heightForWidth(line_width))
    else:
        label.setMinimumHeight(label.fontMetrics().height())
        label.setMaximumHeight(16777215)


class RefineReminders(_Panel):
    """List every registered reminder, including delivered reminders awaiting action."""

    def __init__(self, parent=None, *, timezone=None, respond=None, customize=None, busy=None):
        super().__init__(parent)
        self.timezone = timezone
        self._respond = respond
        self._customize = customize
        self._busy = busy
        self.setObjectName("refine-reminders")
        self.count = _badge(tr("0 项", "0 items"))
        self.count.setObjectName("refine-reminder-count")
        self.body.addLayout(_title(tr("提醒事项", "Reminders"), self.count))
        self._empty = _text(tr("暂无活跃提醒", "No active reminders"), color=MUTED)
        self.body.addWidget(self._empty)
        cards = QWidget()
        self._cards = QVBoxLayout(cards)
        self._cards.setSizeConstraint(QVBoxLayout.SizeConstraint.SetMinimumSize)
        self._cards.setContentsMargins(0, 0, 0, 0)
        self._cards.setSpacing(8)
        self._cards.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._scroll = _scroll_area(cards)
        self._scroll.setObjectName("refine-reminder-list")
        self.body.addWidget(self._scroll)
        self._scroll.hide()
        self._feedback = _text(size=11, color=MUTED, wrap=True)
        self._feedback.setObjectName("refine-reminder-feedback")
        self.body.addWidget(self._feedback)
        self._feedback.hide()
        self._rows = ()
        self._action_buttons = {}

    def render_snapshot(self, snapshot: dict, *, actionable=()):
        reference = snapshot.get("now")
        tasks = active_reminders(snapshot)
        if self._respond:
            tasks.sort(key=lambda task: (task["task_id"], task.get("occurrence", 1)) not in actionable)
        rows = []
        for task in tasks:
            subject = (task["task_id"], task.get("occurrence", 1))
            draft = task["draft"]
            can_respond = bool(self._respond and subject in actionable)
            rows.append((subject, draft["content"], _schedule(draft, reference, draft.get("timezone") or self.timezone),
                         can_respond or bool(task.get("displayed_at")), can_respond, draft.get("expires_at")))
        rows = tuple(rows)
        if rows == self._rows:
            return
        self._rows = rows
        units = tr("{count} 项", "{count} item" if len(rows) == 1 else "{count} items")
        self.count.setText(units.format(count=len(rows)))
        self._empty.setVisible(not rows)
        self._scroll.setVisible(bool(rows))
        scroll_position = self._scroll.verticalScrollBar().value()
        self._action_buttons.clear()
        while self._cards.count():
            widget = self._cards.takeAt(0).widget()
            widget.hide()
            widget.deleteLater()
        minimum_height = self._cards.spacing() * (len(rows) - 1)
        for subject, name, schedule, pending, can_respond, expires_at in rows:
            frame, stack = _card(spacing=3)
            frame.setProperty("task_id", subject[0])
            frame.setProperty("occurrence", subject[1])
            height = 80 if can_respond else 53
            frame.setMinimumHeight(height)
            minimum_height += height
            row = QHBoxLayout()
            row.setSpacing(7)
            status = tr("待处理", "Pending") if pending else tr("已登记", "Scheduled")
            badge = _badge(status, positive=pending)
            name_label = _text(name, bold=True, wrap=True)
            # Account for the card gutters and the scrollbar when reserving room for the badge.
            name_width = PANEL_WIDTH - 50 - row.spacing() - badge.sizeHint().width()
            _size_wrapped_label(name_label, name_width)
            row.addWidget(name_label, 1)
            row.addWidget(badge, alignment=Qt.AlignmentFlag.AlignTop)
            stack.addLayout(row)
            schedule_label = _text(schedule, size=11, color=MUTED, wrap=True)
            _size_wrapped_label(schedule_label, 225)
            stack.addWidget(schedule_label)
            if can_respond:
                stack.addLayout(self._action_row(subject, name, expires_at))
            self._cards.addWidget(frame)
        self.refresh_actions()
        if rows:
            width = PANEL_WIDTH - 26
            full_height = max(minimum_height, self._cards.heightForWidth(width))
            self._scroll.setFixedHeight(min(175, full_height))
            self._cards.activate()
            self._scroll.verticalScrollBar().setValue(scroll_position)
        self.updateGeometry()

    def _action_row(self, subject, name, expires_at):
        row = QHBoxLayout()
        row.setSpacing(6)
        acknowledge = QPushButton(tr("确认收到", "Acknowledge"))
        acknowledge.setObjectName("refine-reminder-ack")
        acknowledge.clicked.connect(lambda checked=False: self._respond("ack", *subject))
        snooze = QPushButton(tr("稍后提醒", "Snooze"))
        snooze.setObjectName("refine-reminder-snooze")
        menu = QMenu(snooze)
        presets = {}
        for minutes in (5, 10, 30):
            seconds = minutes * 60
            action = menu.addAction(tr("{minutes} 分钟后", "In {minutes} min").format(minutes=minutes))
            action.setObjectName(f"refine-reminder-snooze-{seconds}")
            action.triggered.connect(lambda checked=False, delay=seconds: self._respond("snooze", *subject, seconds=delay))
            presets[seconds] = action
        if expires_at:
            expiry = datetime.fromisoformat(expires_at)

            def update_presets():
                now = datetime.now(expiry.tzinfo)
                for seconds, action in presets.items():
                    action.setEnabled(now + timedelta(seconds=seconds) < expiry)

            menu.aboutToShow.connect(update_presets)
        menu.addSeparator()
        menu.addAction(tr("自定时长…", "Custom delay…"), lambda: self._customize(*subject))
        snooze.setMenu(menu)
        for button in (acknowledge, snooze):
            button.setAccessibleName(f"{button.text()} · {name}")
            button.setFont(_font(11, bold=True))
            button.setFixedHeight(23)
            button.setStyleSheet(
                f"QPushButton {{color: {GREEN}; background: {PAPER}; border: 1px solid {CARD_OUTLINE};"
                "border-radius: 5px; padding: 0 9px}"
                f"QPushButton:hover, QPushButton:focus {{background: {GREEN_TINT}}}"
                f"QPushButton:disabled {{color: {MUTED}; background: {SAND}}}"
                "QPushButton::menu-indicator {subcontrol-origin: padding; subcontrol-position: center right; right: 4px}"
            )
            row.addWidget(button)
        row.addStretch()
        self._action_buttons[subject[0]] = (acknowledge, snooze)
        return row

    def refresh_actions(self):
        for task_id, buttons in self._action_buttons.items():
            enabled = not self._busy(task_id)
            for button in buttons:
                button.setEnabled(enabled)

    def set_feedback(self, text):
        self._feedback.setText(text)
        self._feedback.setVisible(bool(text))

    def showEvent(self, event):
        super().showEvent(event)
        if self._respond:
            self._scroll.verticalScrollBar().setValue(0)


def _work_duration(seconds: float) -> str:
    seconds = math.ceil(seconds)
    minutes, rest = divmod(seconds, 60)
    if not rest:
        return tr("{minutes} 分钟", "{minutes} min").format(minutes=minutes)
    if not minutes:
        return tr("{seconds} 秒", "{seconds} s").format(seconds=rest)
    return tr("{minutes} 分 {seconds} 秒", "{minutes} min {seconds} s").format(minutes=minutes, seconds=rest)


def _review(values) -> str:
    return "\n".join(f"• {item}" for item in values) or tr("未填写", "Not entered")


class RefineWork(_Panel):
    """Show the newest active work segment, then its read-only review."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("refine-work")
        self.status = _badge(tr("未开始", "Not started"), positive=False)
        self.body.addLayout(_title(tr("专注与回顾", "Focus & review"), self.status))
        self.empty = _text(tr("当前没有专注工作段", "No active focus session"), color=MUTED, wrap=True)
        self.body.addWidget(self.empty)

        self.summary = QWidget()
        self.body.addWidget(self.summary)
        stack = QVBoxLayout(self.summary)
        stack.setContentsMargins(0, 0, 0, 0)
        stack.setSpacing(8)
        stack.addWidget(_text(tr("当前目标", "Current goal"), size=11, color=MUTED))
        self.goal = _text(size=16, bold=True, wrap=True)
        stack.addWidget(self.goal)
        timing = QHBoxLayout()
        timing.setSpacing(8)
        self.countdown = _text(size=23, color=GREEN, bold=True)
        self.countdown.setObjectName("refine-work-countdown")
        timing.addWidget(self.countdown)
        self.duration = _text(size=11, color=MUTED, wrap=True)
        timing.addWidget(self.duration, 1)
        stack.addLayout(timing)
        linked = QHBoxLayout()
        linked.setSpacing(6)
        symbol = QLabel()
        symbol.setPixmap(icon("link", GREEN, 15).pixmap(15, 15))
        linked.addWidget(symbol, alignment=Qt.AlignmentFlag.AlignTop)
        self.task = _text(size=12, wrap=True)
        linked.addWidget(self.task, 1)
        stack.addLayout(linked)

        self.review = QWidget()
        review = QVBoxLayout(self.review)
        review.setContentsMargins(0, 0, 0, 0)
        review.setSizeConstraint(QVBoxLayout.SizeConstraint.SetMinimumSize)
        review.setSpacing(5)
        review.addWidget(_text(tr("本轮回顾", "Session review"), bold=True))
        review.addWidget(_text(tr("已完成", "Completed work"), size=11, color=MUTED))
        self.outcomes = _text(size=12, wrap=True)
        review.addWidget(self.outcomes)
        review.addWidget(_text(tr("下一步", "Next steps"), size=11, color=MUTED))
        self.next_steps = _text(size=12, wrap=True)
        review.addWidget(self.next_steps)
        self._review_scroll = _scroll_area(self.review)
        stack.addWidget(self._review_scroll)
        self._last_detail = None
        self.summary.hide()
        self._review_scroll.hide()

    def render_snapshot(self, snapshot: dict):
        view = current_work(snapshot)
        phase = snapshot.get("active_phase")
        if phase and phase["kind"] != "focus":
            view = dict(state=phase["state"], goal=phase["title"], remaining_s=phase["remaining_s"],
                        duration_s=phase["planned_duration_s"], task_id=phase.get("primary_issue_id"))
        self.summary.setVisible(view is not None)
        self.empty.setVisible(view is None)
        if view is None:
            if self._last_detail is not None:
                self.status.setText(tr("未开始", "Not started"))
                _style_badge(self.status, False)
            self._last_detail = None
            return

        self.countdown.setText(duration_text(view["remaining_s"]))
        task_id = view.get("task_id")
        task = next(
            (task["draft"]["content"] for task in snapshot.get("tasks", ()) if task["task_id"] == task_id),
            tr("无", "None") if task_id is None else tr("事项不可用", "Item unavailable"),
        )
        detail = (
            view["state"], view["goal"], view.get("duration_s"), task,
            tuple(view.get("confirmed_outcomes", ())), tuple(view.get("next_steps", ())),
        )
        if detail == self._last_detail:
            return
        self._last_detail = detail
        state, goal, duration, task, outcomes, next_steps = detail
        self.status.setText(tr(*WORK_STATES[state]))
        _style_badge(self.status, state == "focusing")
        self.goal.setText(goal)
        _size_wrapped_label(self.goal, PANEL_WIDTH - 26)
        self.duration.setText(
            tr("剩余 / 共 {total}", "Remaining / {total} total").format(total=_work_duration(duration))
            if duration is not None else tr("剩余", "Remaining")
        )
        self.task.setText(tr("关联事项 · {item}", "Linked item · {item}").format(item=task))
        _size_wrapped_label(self.task, PANEL_WIDTH - 47)
        self._review_scroll.setVisible(state == "ended")
        if state == "ended":
            self.outcomes.setText(_review(outcomes))
            self.next_steps.setText(_review(next_steps))
            review = self.review.layout()
            height = max(review.sizeHint().height(), review.heightForWidth(PANEL_WIDTH - 26))
            self._review_scroll.setFixedHeight(min(152, height))


class RefineSettings(_Panel):
    """Present the invitation policy and let the user select the default input."""

    input_mode_changed = pyqtSignal(str)

    def __init__(self, parent=None, *, voice_available=True):
        super().__init__(parent)
        self.setObjectName("refine-settings")
        self.body.addLayout(_title(tr("设置", "Settings")))

        pomodoro, pomodoro_stack = _card(spacing=0)
        policy = QHBoxLayout()
        policy.addWidget(_text(tr("自动番茄钟", "Auto Pomodoro"), bold=True), 1)
        self.policy_badge = _badge(tr("开始前询问", "Ask before starting"))
        self.policy_badge.setFixedHeight(23)
        policy.addWidget(self.policy_badge)
        pomodoro_stack.addLayout(policy)
        self.body.addWidget(pomodoro)

        preference, preference_stack = _card(spacing=6)
        preference_stack.addWidget(_text(tr("默认输入方式", "Default input"), bold=True))
        choices = QHBoxLayout()
        choices.setSpacing(9)
        self._modes = {}
        self._group = QButtonGroup(self)
        for mode, text, glyph in (
            ("voice", tr("语音", "Voice"), "mic"),
            ("typing", tr("打字", "Typing"), "keyboard"),
        ):
            button = QPushButton(text)
            button.setObjectName(f"refine-settings-{mode}")
            button.setAccessibleName(tr("默认输入方式：{mode}", "Default input · {mode}").format(mode=text))
            button.setCheckable(True)
            button.setFixedHeight(33)
            button.setFixedWidth(116)
            button.setFont(_font(12, bold=True))
            button.setIcon(icon(glyph, OUTLINE, 15))
            button.setIconSize(QSize(15, 15))
            button.setStyleSheet(
                f"QPushButton {{ color: {INK}; background: {PAPER}; border: 1px solid {OUTLINE}; border-radius: 7px }}"
                f"QPushButton:checked {{ background: {GREEN_TINT} }}"
                f"QPushButton:focus {{ border: 2px solid {GREEN} }}"
            )
            self._group.addButton(button)
            button.toggled.connect(lambda checked, key=mode: self.input_mode_changed.emit(key) if checked else None)
            choices.addWidget(button)
            self._modes[mode] = button
        choices.addStretch()
        preference_stack.addLayout(choices)
        hint = tr(
            "输入中长按方式图标，只切换本次输入。",
            "Hold the input icon to switch modes for this message.",
        ) if voice_available else tr("当前版本支持文字输入。", "Typing is available in this version.")
        preference_stack.addWidget(_text(hint, size=11, color=MUTED, wrap=True))
        self.body.addWidget(preference)
        if not voice_available:
            self._modes["voice"].setEnabled(False)
        self.set_input_mode("voice" if voice_available else "typing")

    def set_input_mode(self, mode: str):
        button = self._modes.get(mode, self._modes["voice"])
        blocked = button.blockSignals(True)
        button.setChecked(True)
        button.blockSignals(blocked)
