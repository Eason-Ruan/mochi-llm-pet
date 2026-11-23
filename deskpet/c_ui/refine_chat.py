"""Mochi's input panel and its visible conversation history."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from math import ceil, sin
import sqlite3

from PyQt6.QtCore import QElapsedTimer, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFontDatabase, QPainter
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from .refine_dialogue import DemoConversation, DemoTurn, LocalConversation, MILK_CONFIRM, MILK_REQUEST
from .refine_icons import icon

INK = "#433426"
GREEN_INK = "#34513A"
PANEL_WIDTH = 292


STYLE = """
QFrame#refine-chat-panel {
    background: #FFF7E8; border: 2px solid #433426; border-radius: 13px;
}
QFrame#refine-chat-shadow { background: #C8B7A1; border-radius: 13px; }
QFrame#refine-chat-message, QFrame#refine-chat-composer {
    background: #FDFAF4; border: 1px solid #DEC8AA; border-radius: 8px;
}
QFrame#refine-chat-message[speaker="mochi"] {
    background: #DDEAD7; border-color: #8EAD85;
}
QLabel { background: transparent; color: #302821; border: none; font-size: 12px; }
QLabel[role="sender"] { color: #645546; font-size: 11px; font-weight: 700; }
QLabel[role="sender"][speaker="mochi"], QLabel[role="sender"][speaker="you"] { font-size: 10px; }
QLabel[role="sender"][speaker="mochi"] { color: #34513A; }
QLabel#refine-chat-elapsed { color: #645546; font-size: 11px; }
QLineEdit#refine-chat-editor {
    background: transparent; border: none; color: #302821; font-size: 12px;
    selection-background-color: #DDEAD7;
}
QFrame#refine-chat-editbox {
    background: #FFFFFF; border: 1px solid #DEC8AA; border-radius: 6px;
}
QPushButton {
    background: #FDFAF4; border: 2px solid #433426; border-radius: 7px;
    padding: 0;
}
QPushButton[accent="true"] { background: #DDEAD7; }
QPushButton:pressed, QPushButton:focus { border-color: #34513A; }
QPushButton:disabled { background: #F1E9DA; border-color: #B8A48A; }
"""


@dataclass(frozen=True)
class Message:
    speaker: str
    text: str


class _GestureButton(QPushButton):
    held = pyqtSignal()
    tapped = pyqtSignal()

    def __init__(self, name: str, title: str, *, accent=False, hold=False, width=32, height=28):
        super().__init__()
        self.setAccessibleName(title)
        self.setToolTip(title)
        self.setFixedSize(width, height)
        self.setProperty("accent", accent)
        self.setIcon(icon(name, GREEN_INK if accent else INK, 16))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._held = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(550)
        self._timer.timeout.connect(self._on_hold)
        if hold:
            self.pressed.connect(self._start)
            self.released.connect(self._timer.stop)
        self.clicked.connect(self._on_click)

    def _start(self):
        self._held = False
        self._timer.start()

    def _on_hold(self):
        if self.isDown():
            self._held = True
            self.held.emit()

    def _on_click(self):
        if not self._held:
            self.tapped.emit()


class _Waveform(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedSize(76, 22)
        self.phase = 0

    def paintEvent(self, event):
        painter = QPainter(self)
        for index in range(12):
            level = round(7 + 11 * abs(sin(self.phase / 260 + index * 1.31)))
            painter.fillRect(index * 6 + 4, (self.height() - level) // 2, 3, level,
                             QColor(GREEN_INK if index % 3 == 0 else "#789C74"))


class RefineChat(QWidget):
    """Displays two messages at most, counting an active composer as one."""

    toolbar_mode_changed = pyqtSignal(object)
    typing_requested = pyqtSignal()
    storage_error = pyqtSignal(str)

    def __init__(self, submit: Callable[[dict], str], snapshot: Callable[[], dict], parent=None, *,
                 store=None, conversation=None):
        super().__init__(parent)
        self.setObjectName("refine-chat")
        font = self.font()
        families = QFontDatabase.families()
        for family in ("Noto Sans SC", "PingFang SC", "Microsoft YaHei"):
            if family in families:
                font.setFamily(family)
                break
        self.setFont(font)
        self.setStyleSheet(STYLE)
        self._submit = submit
        self._snapshot = snapshot
        self._store = store
        self._voice_available = store is None
        self._script = conversation or (DemoConversation() if store is None else LocalConversation())
        messages, draft, pending = store.load() if store is not None else ([], "", None)
        self._messages = [Message(*item) for item in messages]
        self._pending = (pending[0], DemoTurn(**pending[1])) if pending else None
        self.recovered_event = self._pending[0] if pending else None
        self._preferred = "voice" if self._voice_available else "typing"
        self._composition: str | None = None
        self._history_end: int | None = None
        self._draft = draft
        self._follow_up = False
        self._voice_started = QElapsedTimer()
        self._voice_timer = QTimer(self)
        self._voice_timer.setInterval(80)
        self._voice_timer.timeout.connect(self._animate_voice)
        self._editor: QLineEdit | None = None
        self._elapsed: QLabel | None = None
        self._waveform: _Waveform | None = None

        shadow = QFrame()
        shadow.setObjectName("refine-chat-shadow")
        panel = QFrame()
        panel.setObjectName("refine-chat-panel")
        outside = QVBoxLayout(self)
        outside.setContentsMargins(0, 0, 0, 0)
        outside.addWidget(shadow)
        depth = QVBoxLayout(shadow)
        depth.setContentsMargins(0, 0, 0, 3)
        depth.addWidget(panel)
        self._cards = QVBoxLayout(panel)
        self._cards.setContentsMargins(11, 11, 11, 11)
        self._cards.setSpacing(8)

    def sizeHint(self) -> QSize:
        # Measuring wrapped labels at the panel width avoids Qt's default
        # narrower text size hint adding empty lines to short messages.
        layout = self.layout()
        height = layout.totalHeightForWidth(PANEL_WIDTH) if layout.hasHeightForWidth() else layout.sizeHint().height()
        return QSize(PANEL_WIDTH, height)

    def set_preference(self, mode: str):
        self._preferred = mode if self._voice_available else "typing"

    def open(self, preferred_mode: str, *, alternate: bool = False):
        self.set_preference(preferred_mode)
        if self._pending:
            self._render()
            return
        self._compose(self._other(self._preferred) if alternate and self._voice_available else self._preferred)

    def reset_input(self):
        self._voice_timer.stop()
        self._composition = None
        self._history_end = None
        if self._store is None:
            self._draft = ""
        self._render()

    @staticmethod
    def _other(mode: str) -> str:
        return "voice" if mode == "typing" else "typing"

    def toggle_input(self):
        if self._composition is not None and self._voice_available:
            self._compose(self._other(self._composition))

    def _compose(self, mode: str):
        self._composition = mode
        self._history_end = None
        if mode == "voice":
            self._voice_started.start()
            self._voice_timer.start()
        else:
            self._voice_timer.stop()
        self._render()
        if mode == "typing":
            self._request_typing_focus()

    def showEvent(self, event):
        super().showEvent(event)
        self._request_typing_focus()

    def _request_typing_focus(self):
        if self._editor is not None and self.isVisible():
            QTimer.singleShot(0, self._focus_editor)

    def _focus_editor(self):
        if self._editor is not None and self._editor.isVisible():
            self.typing_requested.emit()
            self._editor.setFocus(Qt.FocusReason.OtherFocusReason)

    def _animate_voice(self):
        if self._elapsed is None or self._waveform is None:
            return
        elapsed = self._voice_started.elapsed()
        minutes, seconds = divmod(elapsed // 1000, 60)
        self._elapsed.setText(f"{minutes:02d}:{seconds:02d}")
        self._waveform.phase = elapsed
        self._waveform.update()

    def _stop_voice(self):
        sample = MILK_CONFIRM if self._script.confirmation else MILK_REQUEST
        self._send(sample, simulated=True)

    def _send(self, raw: str, *, simulated: bool = False):
        value = raw.strip()
        if not value or self._pending is not None:
            return
        speaker = "你 · 模拟语音" if simulated else "你"
        turn = self._script.reply(value, self._snapshot())
        if self._store is not None:
            try:
                self._store.begin_turn(speaker, value, turn)
            except sqlite3.Error:
                self.storage_error.emit("暂时无法保存对话。")
                return
        self._messages.append(Message(speaker, value))
        self._composition = self._history_end = None
        self._voice_timer.stop()
        self._draft = ""
        self._follow_up = False
        if turn.action is not None:
            try:
                self._pending = (self._submit(turn.action), turn)
            except (ValueError, RuntimeError):
                self._pending = (turn.action["event_id"], turn)
                self._finish_turn("暂时无法提交。请稍后重试。", True)
        else:
            self._reply(turn.text, turn.follow_up)
        self._render()

    def _reply(self, text: str, follow_up: bool):
        self._messages.append(Message("Mochi", text))
        self._follow_up = follow_up

    def append_message(self, text: str):
        if self._store is not None:
            self._store.append_message("Mochi", text)
        self._reply(text, True)
        self._render()

    def _finish_turn(self, text: str, follow_up: bool):
        if self._store is not None:
            try:
                self._store.finish_turn(self._pending[0], text)
            except sqlite3.Error:
                self.storage_error.emit("暂时无法保存对话结果。")
                return
        self._pending = None
        self.recovered_event = None
        self._reply(text, follow_up)

    def receive_result(self, result: dict):
        if self._pending is None or (result.get("command_id") or result.get("event_id")) != self._pending[0]:
            return
        _, turn = self._pending
        if result.get("status") == "applied" or (result.get("status") == "duplicate"
                                                  and result.get("original_status") == "applied"):
            text = turn.text
            if self._store is None and turn.action["action_id"] == "session.pause" and result.get("view"):
                minutes, seconds = divmod(ceil(result["view"]["remaining_s"]), 60)
                text = f"{text[:-1]}，还剩 {minutes} 分 {seconds:02d} 秒。"
            self._finish_turn(text, turn.follow_up)
        elif result.get("status") == "result_unavailable":
            self._finish_turn("上次操作没有已保存的结果。请查看当前计时后继续。", True)
        else:
            self._finish_turn("未能更新。请查看最新状态后重试。", True)
        self._render()

    def _history_count(self) -> int:
        return len(self._messages) + (self._composition is not None or self._pending is not None)

    def _older(self):
        end = self._history_end if self._history_end is not None else self._history_count()
        self._history_end = max(2, end - 1)
        self._render()

    def _newer(self, latest=False):
        if latest or self._history_end + 1 >= self._history_count():
            self._history_end = None
        else:
            self._history_end += 1
        self._render()
        if self._history_end is None:
            self._request_typing_focus()

    @staticmethod
    def _label(text: str, *, sender=False, speaker=None) -> QLabel:
        label = QLabel(text)
        label.setTextFormat(Qt.TextFormat.PlainText)
        if sender:
            label.setProperty("role", "sender")
            if speaker:
                label.setProperty("speaker", speaker)
        else:
            label.setWordWrap(True)
            label.setObjectName("refine-chat-message-text")
        return label

    def _message_card(self, message: Message) -> QFrame:
        mochi = message.speaker == "Mochi"
        card = QFrame()
        card.setObjectName("refine-chat-message")
        if mochi:
            card.setProperty("speaker", "mochi")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(8, 7, 8, 7)
        layout.setSpacing(2)
        layout.addWidget(self._label(message.speaker, sender=True, speaker="mochi" if mochi else "you"))
        layout.addWidget(self._label(message.text))
        return card

    def _composer(self) -> QFrame:
        voice = self._composition == "voice"
        card = QFrame()
        card.setObjectName("refine-chat-composer")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(9, 9, 9, 9)
        layout.setSpacing(4)
        header = QHBoxLayout()
        header.addWidget(self._label("语音输入中" if voice else "打字输入中", sender=True), 1)
        if self._voice_available:
            mode = _GestureButton("mic" if voice else "keyboard", "长按临时切换输入方式",
                                  accent=True, hold=True, width=30, height=26)
            mode.setObjectName("refine-chat-mode")
            mode.held.connect(self.toggle_input)
            header.addWidget(mode)
        layout.addLayout(header)
        if voice:
            row = QHBoxLayout()
            self._elapsed = QLabel("00:00")
            self._elapsed.setObjectName("refine-chat-elapsed")
            self._waveform = _Waveform()
            row.addWidget(self._elapsed, 1)
            row.addWidget(self._waveform)
            row.addStretch(1)
            stop = _GestureButton("stop", "停止模拟语音并发送", accent=True)
            stop.setObjectName("refine-chat-stop")
            stop.tapped.connect(self._stop_voice)
            row.addWidget(stop)
            layout.addLayout(row)
            self._animate_voice()
        else:
            field = QFrame()
            field.setObjectName("refine-chat-editbox")
            row = QHBoxLayout(field)
            row.setContentsMargins(5, 5, 5, 5)
            self._editor = QLineEdit(self._draft)
            self._editor.setObjectName("refine-chat-editor")
            self._editor.setAccessibleName("打字输入")
            hint = "例如：开始 25 分钟"
            if self._voice_available:
                hint = "确认，明晚六点提醒我。" if self._script.confirmation else "把买牛奶延后到明晚六点"
            self._editor.setPlaceholderText(hint)
            self._editor.setMaxLength(240)
            send = _GestureButton("send", "发送", accent=True)
            send.setObjectName("refine-chat-send")
            send.setEnabled(bool(self._draft.strip()))
            self._editor.textChanged.connect(lambda text: send.setEnabled(bool(text.strip())))
            self._editor.textChanged.connect(self._save_draft)
            self._editor.returnPressed.connect(lambda: self._send(self._draft))
            send.tapped.connect(lambda: self._send(self._draft))
            row.addWidget(self._editor, 1)
            row.addWidget(send)
            layout.addWidget(field)
        return card

    def _save_draft(self, text: str):
        self._draft = text
        if self._store is not None:
            try:
                self._store.save_draft(text)
            except sqlite3.Error:
                self.storage_error.emit("暂时无法保存草稿。")

    def _render(self):
        self._editor = self._elapsed = self._waveform = None
        while self._cards.count():
            item = self._cards.takeAt(0).widget()
            item.hide()
            item.setParent(None)
            item.deleteLater()

        browsing = self._history_end is not None
        virtual = self._composition is not None or self._pending is not None
        end = self._history_end if browsing else len(self._messages)
        start = max(0, end - (1 if virtual and not browsing else 2))
        if start:
            up = _GestureButton("up", "查看更早消息", width=29, height=22)
            up.setObjectName("refine-chat-history-up")
            up.tapped.connect(self._older)
            self._cards.addWidget(up, 0, Qt.AlignmentFlag.AlignHCenter)
        for msg in self._messages[start:end]:
            self._cards.addWidget(self._message_card(msg))
        self.toolbar_mode_changed.emit(self._composition if not self._messages else None)
        if not browsing and self._composition is not None:
            self._cards.addWidget(self._composer())
        elif not browsing and self._pending is not None:
            self._cards.addWidget(self._message_card(Message("Mochi", "正在更新…")))
        elif browsing:
            down = _GestureButton("down", "查看较新消息，长按回到最新", hold=True, width=34)
            down.setObjectName("refine-chat-history-down")
            down.tapped.connect(self._newer)
            down.held.connect(lambda: self._newer(latest=True))
            self._cards.addWidget(down, 0, Qt.AlignmentFlag.AlignHCenter)
        elif self._follow_up:
            mode = "keyboard" if self._preferred == "typing" else "mic"
            button = _GestureButton(mode, "继续输入", accent=True, hold=self._voice_available, width=34)
            button.setObjectName("refine-chat-continue")
            button.tapped.connect(lambda: self._compose(self._preferred))
            button.held.connect(lambda: self._compose(self._other(self._preferred)))
            self._cards.addWidget(button, 0, Qt.AlignmentFlag.AlignHCenter)
        self.updateGeometry()
