"""Read-only reminders and focus summaries, with an editable input preference."""
from __future__ import annotations

import math
from functools import cache

from PyQt6.QtCore import QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QPainter, QPen
from PyQt6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .refine_icons import icon
from .time_format import local_time
from .work_panel import STATES, duration_text

PANEL_WIDTH = 292
CREAM = "#fff7e9"
PAPER = "#fffcf6"
OUTLINE = "#49382a"
CARD_OUTLINE = "#dfceb6"
INK = "#312921"
MUTED = "#67594c"
GREEN = "#35533c"
GREEN_TINT = "#e0ebd8"
SAND = "#f6ead6"
SHADOW = "#c9b79f"


def active_reminders(snapshot: dict) -> list[dict]:
    return sorted(
        (task for task in snapshot.get("tasks", ()) if task["status"] == "registered"
         and task["draft"].get("reminder_enabled", True) and not task.get("acknowledged_at")),
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


@cache
def _font(size: int, *, bold: bool = False) -> QFont:
    font = QFont()
    installed = QFontDatabase.families()
    for family in ("Noto Sans SC", "PingFang SC", "Microsoft YaHei"):
        if family in installed:
            font.setFamily(family)
            break
    font.setPixelSize(size)
    font.setBold(bold)
    return font


def _text(text="", *, size=13, color=INK, bold=False, wrap=False, height=None):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setFont(_font(size, bold=bold))
    label.setStyleSheet(f"color: {color}")
    label.setWordWrap(wrap)
    if height is not None:
        label.setFixedHeight(height)
    return label


def _badge(text="", *, positive=True):
    badge = _text(text, size=11, color=GREEN if positive else MUTED, bold=True)
    _style_badge(badge, positive)
    badge.setFixedHeight(19)
    badge.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    return badge


def _style_badge(badge, positive):
    color, background = (GREEN, GREEN_TINT) if positive else (MUTED, SAND)
    badge.setStyleSheet(f"color: {color}; background: {background}; border-radius: 6px; padding: 0 7px")


def _title(text, badge=None):
    row = QHBoxLayout()
    row.setSpacing(8)
    row.addWidget(_text(text, size=15, bold=True, height=18), 1)
    if badge is not None:
        row.addWidget(badge, alignment=Qt.AlignmentFlag.AlignVCenter)
    return row


class _Panel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(PANEL_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(13, 13, 13, 16)
        self.body.setSpacing(8)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(SHADOW))
        painter.drawRoundedRect(QRectF(0, 3, self.width(), self.height() - 3), 13, 13)
        painter.setPen(QPen(QColor(OUTLINE), 2))
        painter.setBrush(QColor(CREAM))
        painter.drawRoundedRect(QRectF(1, 1, self.width() - 2, self.height() - 5), 12, 12)


def _card(*, spacing: int):
    frame = QFrame()
    frame.setObjectName("refine-card")
    frame.setStyleSheet(f"QFrame#refine-card {{background: {PAPER}; border: 1px solid {CARD_OUTLINE}; border-radius: 7px}}")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(8, 8, 8, 8)
    layout.setSpacing(spacing)
    return frame, layout


def _schedule(draft, reference):
    start = local_time(draft["earliest_at"])
    today = reference.date() if reference is not None else None

    def day_and_clock(when):
        delta = (when.date() - today).days if today is not None else None
        day = {-1: "昨天", 0: "今天", 1: "明天"}.get(
            delta, f"{when.year}年{when.month}月{when.day}日",
        )
        return f"{day} {clock(when)}"

    def clock(when):
        return when.strftime("%H:%M:%S" if when.second else "%H:%M")

    if draft["mode"] == "hard":
        return f"{day_and_clock(start)} · 到点提醒"
    end = local_time(draft["latest_first_delivery_at"])
    finish = clock(end) if end.date() == start.date() else day_and_clock(end)
    return f"{day_and_clock(start)}–{finish} · 柔性提醒"


def _size_wrapped_label(label, line_height, line_width):
    if label.fontMetrics().horizontalAdvance(label.text()) <= line_width and "\n" not in label.text():
        label.setFixedHeight(line_height)
    else:
        label.setMinimumHeight(line_height)
        label.setMaximumHeight(16777215)


def _scroll_area(widget: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setWidgetResizable(True)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setStyleSheet(
        "QScrollArea { background: transparent; border: 0 }"
        "QScrollBar:vertical { width: 6px; padding-left: 2px; background: transparent }"
        f"QScrollBar::handle:vertical {{ background: {SHADOW}; border-radius: 2px; min-height: 18px }}"
        "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0 }"
        "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent }"
    )
    area.setWidget(widget)
    widget.setAutoFillBackground(False)
    area.viewport().setAutoFillBackground(False)
    return area


class RefineReminders(_Panel):
    """List every registered reminder, including delivered reminders awaiting action."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("refine-reminders")
        self.count = _badge("0 项")
        self.count.setObjectName("refine-reminder-count")
        self.body.addLayout(_title("提醒事项", self.count))
        self._empty = _text("暂无活跃提醒", color=MUTED)
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
        self._rows = ()

    def render_snapshot(self, snapshot: dict):
        reference = local_time(snapshot["now"]) if snapshot.get("now") else None
        rows = tuple(
            (
                task["task_id"],
                task["draft"]["content"],
                _schedule(task["draft"], reference),
                bool(task.get("displayed_at")),
            )
            for task in active_reminders(snapshot)
        )
        if rows == self._rows:
            return
        self._rows = rows
        self.count.setText(f"{len(rows)} 项")
        self._empty.setVisible(not rows)
        self._scroll.setVisible(bool(rows))
        scroll_position = self._scroll.verticalScrollBar().value()
        while self._cards.count():
            widget = self._cards.takeAt(0).widget()
            widget.hide()
            widget.deleteLater()
        for _, name, schedule, pending in rows:
            frame, stack = _card(spacing=3)
            frame.setMinimumHeight(53)
            row = QHBoxLayout()
            row.setSpacing(7)
            name_label = _text(name, bold=True, wrap=True)
            _size_wrapped_label(name_label, 19, 175)
            row.addWidget(name_label, 1)
            row.addWidget(_badge("待处理" if pending else "已登记", positive=pending),
                          alignment=Qt.AlignmentFlag.AlignTop)
            stack.addLayout(row)
            schedule_label = _text(schedule, size=11, color=MUTED, wrap=True)
            _size_wrapped_label(schedule_label, 13, 225)
            stack.addWidget(schedule_label)
            self._cards.addWidget(frame)
        if rows:
            width = PANEL_WIDTH - 26
            minimum_height = 53 * len(rows) + self._cards.spacing() * (len(rows) - 1)
            full_height = max(minimum_height, self._cards.heightForWidth(width))
            self._scroll.setFixedHeight(min(175, full_height))
            self._cards.activate()
            self._scroll.verticalScrollBar().setValue(scroll_position)
        self.updateGeometry()


def _work_duration(seconds: float) -> str:
    seconds = math.ceil(seconds)
    minutes, rest = divmod(seconds, 60)
    if not rest:
        return f"{minutes} 分钟"
    if not minutes:
        return f"{rest} 秒"
    return f"{minutes} 分 {rest} 秒"


def _review(values) -> str:
    return "\n".join(f"• {item}" for item in values) or "未填写"


class RefineWork(_Panel):
    """Show the newest active work segment, then its read-only review."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("refine-work")
        self.status = _badge("未开始", positive=False)
        self.body.addLayout(_title("专注与回顾", self.status))
        self.empty = _text("当前没有专注工作段", color=MUTED, wrap=True)
        self.body.addWidget(self.empty)

        self.summary = QWidget()
        self.body.addWidget(self.summary)
        stack = QVBoxLayout(self.summary)
        stack.setContentsMargins(0, 0, 0, 0)
        stack.setSpacing(8)
        stack.addWidget(_text("当前目标", size=11, color=MUTED, height=13))
        self.goal = _text(size=16, bold=True, wrap=True)
        stack.addWidget(self.goal)
        timing = QHBoxLayout()
        timing.setSpacing(8)
        self.countdown = _text(size=23, color=GREEN, bold=True, height=28)
        self.countdown.setObjectName("refine-work-countdown")
        timing.addWidget(self.countdown)
        self.duration = _text(size=11, color=MUTED, height=13)
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
        review.addWidget(_text("本轮回顾", bold=True))
        review.addWidget(_text("已完成", size=11, color=MUTED))
        self.outcomes = _text(size=12, wrap=True)
        review.addWidget(self.outcomes)
        review.addWidget(_text("下一步", size=11, color=MUTED))
        self.next_steps = _text(size=12, wrap=True)
        review.addWidget(self.next_steps)
        self._review_scroll = _scroll_area(self.review)
        stack.addWidget(self._review_scroll)
        self._last_detail = None
        self.summary.hide()
        self._review_scroll.hide()

    def render_snapshot(self, snapshot: dict):
        view = current_work(snapshot)
        self.summary.setVisible(view is not None)
        self.empty.setVisible(view is None)
        if view is None:
            if self._last_detail is not None:
                self.status.setText("未开始")
                _style_badge(self.status, False)
            self._last_detail = None
            return

        self.countdown.setText(duration_text(view["remaining_s"]))
        task_id = view.get("task_id")
        task = next(
            (task["draft"]["content"] for task in snapshot.get("tasks", ()) if task["task_id"] == task_id),
            "无" if task_id is None else "事项不可用",
        )
        detail = (
            view["state"], view["goal"], view.get("duration_s"), task,
            tuple(view.get("confirmed_outcomes", ())), tuple(view.get("next_steps", ())),
        )
        if detail == self._last_detail:
            return
        self._last_detail = detail
        state, goal, duration, task, outcomes, next_steps = detail
        self.status.setText(STATES[state])
        _style_badge(self.status, state == "focusing")
        self.goal.setText(goal)
        _size_wrapped_label(self.goal, 19, PANEL_WIDTH - 26)
        self.duration.setText(f"剩余 / 共 {_work_duration(duration)}" if duration is not None else "剩余")
        self.task.setText(f"关联事项 · {task}")
        _size_wrapped_label(self.task, 15, PANEL_WIDTH - 47)
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

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("refine-settings")
        self.body.addLayout(_title("设置"))

        pomodoro, pomodoro_stack = _card(spacing=0)
        policy = QHBoxLayout()
        policy.addWidget(_text("自动番茄钟", bold=True), 1)
        policy_badge = _badge("开始前询问")
        policy_badge.setFixedHeight(23)
        policy.addWidget(policy_badge)
        pomodoro_stack.addLayout(policy)
        self.body.addWidget(pomodoro)

        preference, preference_stack = _card(spacing=6)
        preference_stack.addWidget(_text("默认输入方式", bold=True, height=16))
        choices = QHBoxLayout()
        choices.setSpacing(9)
        self._modes = {}
        self._group = QButtonGroup(self)
        for mode, text, glyph in (("voice", "语音", "mic"), ("typing", "打字", "keyboard")):
            button = QPushButton(text)
            button.setObjectName(f"refine-settings-{mode}")
            button.setAccessibleName(f"默认输入方式：{text}")
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
        preference_stack.addWidget(_text("输入中长按方式图标，只切换本次输入。", size=11, color=MUTED, height=13))
        self.body.addWidget(preference)
        self.set_input_mode("voice")

    def set_input_mode(self, mode: str):
        button = self._modes.get(mode, self._modes["voice"])
        blocked = button.blockSignals(True)
        button.setChecked(True)
        button.blockSignals(blocked)
