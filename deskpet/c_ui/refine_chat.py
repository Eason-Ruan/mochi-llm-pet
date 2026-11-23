"""Mochi's input panel and its visible conversation history."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from math import ceil, sin
import sqlite3
from uuid import uuid4

from PyQt6.QtCore import QElapsedTimer, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QAbstractTextDocumentLayout, QColor, QFontDatabase, QPainter, QPalette, QTextDocument, QTextOption,
)
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from .refine_dialogue import DemoConversation, DemoTurn, LocalConversation, MILK_CONFIRM, MILK_REQUEST
from .refine_icons import icon
from .refine_style import _font, _scroll_area

INK = "#433426"
GREEN_INK = "#34513A"
PANEL_WIDTH = 292
DEFAULT_MAX_HEIGHT = 420
MIN_HEIGHT = 134


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
    color: #433426;
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
    id: str | None = None
    pending: bool = False
    reply_to: str | None = None
    reply_source: str | None = None


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


class _MessageDocument(QTextDocument):
    def loadResource(self, resource_type, url):
        # A response may mention an image URL or file. Chat never opens it.
        return None


class _MessageBody(QLabel):
    def __init__(self, value, *, markdown):
        super().__init__(value)
        self.setObjectName("refine-chat-message-text")
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(True)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.document = _MessageDocument(self)
        self.document.setDocumentMargin(0)
        self.document.setDefaultFont(_font(12))
        options = self.document.defaultTextOption()
        options.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.document.setDefaultTextOption(options)
        if markdown:
            self.document.setMarkdown(value, QTextDocument.MarkdownFeature.MarkdownDialectGitHub
                                      | QTextDocument.MarkdownFeature.MarkdownNoHTML)
        else:
            self.document.setPlainText(value)

    def heightForWidth(self, width):
        self.document.setTextWidth(max(1, width))
        return ceil(self.document.size().height())

    def paintEvent(self, event):
        self.document.setTextWidth(max(1, self.width()))
        context = QAbstractTextDocumentLayout.PaintContext()
        context.palette.setColor(QPalette.ColorRole.Text, QColor("#302821"))
        painter = QPainter(self)
        self.document.documentLayout().draw(painter, context)


class _MessageCard(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("refine-chat-message")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.body = None

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.body is not None:
            margins = self.layout().contentsMargins()
            width = self.contentsRect().width() - margins.left() - margins.right()
            height = self.body.heightForWidth(width)
            if self.body.minimumHeight() != height:
                self.body.setFixedHeight(height)


class _HeightGrip(QWidget):
    dragged = pyqtSignal(float)
    finished = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setObjectName("refine-chat-height-grip")
        self.setAccessibleName("调整聊天面板高度")
        self.setToolTip("拖动调整聊天面板高度")
        self.setFixedHeight(14)
        self.setCursor(Qt.CursorShape.SizeVerCursor)
        self.origin = None

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#9a8063") if self.underMouse() or self.origin is not None else QColor("#c8b7a1"))
        painter.drawRoundedRect(self.width() // 2 - 17, 5, 34, 3, 1.5, 1.5)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.origin = event.globalPosition().y()
            self.update()
            event.accept()

    def mouseMoveEvent(self, event):
        if self.origin is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.dragged.emit(event.globalPosition().y() - self.origin)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.origin is not None:
            self.origin = None
            self.update()
            self.finished.emit()
            event.accept()


class RefineChat(QWidget):
    """Scrollable conversation with a fixed composer and a resizable panel."""

    toolbar_mode_changed = pyqtSignal(object)
    typing_requested = pyqtSignal()
    storage_error = pyqtSignal(str)
    geometry_requested = pyqtSignal()
    height_committed = pyqtSignal(int)

    def __init__(self, submit: Callable[[dict], str], snapshot: Callable[[], dict], parent=None, *,
                 store=None, conversation=None, system2=None, configure_system2=None, proposal_card=None,
                 preferred_height=0):
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
        self._system2 = system2
        self._configure_system2 = configure_system2
        self._proposal_card = proposal_card
        self._supersedes_message_id = None
        self._proposal_context = None
        self._projection_version = None
        self._route_status = None
        self._voice_available = store is None
        self._script = conversation or (DemoConversation() if store is None else LocalConversation())
        messages, draft, pending = store.load() if store is not None else ([], "", None)
        self._messages = [Message(*item) for item in messages]
        self._pending = (pending[0], DemoTurn(**pending[1])) if pending else None
        self.recovered_event = self._pending[0] if pending else None
        self._preferred = "voice" if self._voice_available else "typing"
        self._composition: str | None = None
        self._preferred_height = preferred_height
        self._resize_start = None
        self._panel_above = False
        self._resize_limit = 0
        self._rendered_messages: list[Message] = []
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
        self._panel_layout = QVBoxLayout(panel)
        self._panel_layout.setContentsMargins(11, 7, 11, 7)
        self._panel_layout.setSpacing(5)

        history = QWidget()
        self._history_layout = QVBoxLayout(history)
        self._history_layout.setContentsMargins(0, 0, 3, 0)
        self._history_layout.setSpacing(6)
        self._history_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._history_layout.setSizeConstraint(QVBoxLayout.SizeConstraint.SetMinimumSize)
        self._message_cards = QVBoxLayout()
        self._message_cards.setSpacing(6)
        self._history_layout.addLayout(self._message_cards)
        self._extras = QVBoxLayout()
        self._history_layout.addLayout(self._extras)
        self._history = _scroll_area(history)
        self._history.setStyleSheet(self._history.styleSheet() + """
            QScrollBar:vertical { width: 9px }
            QScrollBar::handle:vertical { margin: 0 1px }
            QScrollBar::handle:vertical:hover { background: #9a8063 }
        """)
        self._history.setObjectName("refine-chat-history")
        self._history.setAccessibleName("聊天记录")
        self._history.setMinimumHeight(0)
        self._history.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        scrollbar = self._history.verticalScrollBar()
        self._history_maximum = scrollbar.maximum()
        scrollbar.rangeChanged.connect(self._follow_history_range)
        self._history.hide()
        self._panel_layout.addWidget(self._history, 1)

        self._cards = QVBoxLayout()
        self._cards.setSpacing(5)
        self._panel_layout.addLayout(self._cards)
        self._grip = _HeightGrip()
        self._grip.dragged.connect(self._resize_from_grip)
        self._grip.finished.connect(self._commit_height)
        self._panel_layout.addWidget(self._grip)
        if self._system2 is not None:
            self.refresh_projection(self._store.projection_version())

    def sizeHint(self) -> QSize:
        history = self._history_layout
        chat_width = PANEL_WIDTH - 31
        natural_height = max(history.sizeHint().height(), history.totalHeightForWidth(chat_width))
        chrome = self.layout().sizeHint().height()
        height = self._preferred_height or min(DEFAULT_MAX_HEIGHT, chrome + max(0, natural_height))
        return QSize(PANEL_WIDTH, max(height, self._minimum_panel_height()))

    def _minimum_panel_height(self):
        return max(self.minimumSizeHint().height() + 32, MIN_HEIGHT)

    def panel_geometry(self, *, below, above, limit):
        height = self.sizeHint().height()
        if self._grip.origin is None:
            use_above = height > below and above > below
            if use_above != self._panel_above:
                self._panel_above = use_above
                self._panel_layout.removeWidget(self._grip)
                index = 0 if use_above else self._panel_layout.count()
                self._panel_layout.insertWidget(index, self._grip)
        self._resize_limit = min(limit, max(1, above if self._panel_above else below))
        return min(height, self._resize_limit), self._panel_above

    def _resize_from_grip(self, delta):
        if self._resize_start is None:
            self._resize_start = self.height()
        offset = -delta if self._panel_above else delta
        self._preferred_height = min(self._resize_limit,
                                     max(self._minimum_panel_height(), round(self._resize_start + offset)))
        self.geometry_requested.emit()

    def _commit_height(self):
        if self._resize_start is not None:
            self._resize_start = None
            self.height_committed.emit(self._preferred_height)

    def ensureWidgetVisible(self, widget):
        if self._history.widget().isAncestorOf(widget):
            self._history.ensureWidgetVisible(widget)

    def _to_latest(self):
        scrollbar = self._history.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _follow_history_range(self, _minimum, maximum):
        scrollbar = self._history.verticalScrollBar()
        previous_maximum = self._history_maximum
        self._history_maximum = maximum
        if scrollbar.value() >= previous_maximum:
            scrollbar.setValue(maximum)

    def set_preference(self, mode: str):
        self._preferred = mode if self._voice_available else "typing"

    def open(self, preferred_mode: str, *, alternate: bool = False):
        self.set_preference(preferred_mode)
        if self._pending:
            self._render()
            return
        self._compose(self._other(self._preferred) if alternate and self._voice_available else self._preferred)
        QTimer.singleShot(0, self._to_latest)

    def reset_input(self):
        self._voice_timer.stop()
        self._composition = None
        self._supersedes_message_id = self._proposal_context = None
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
        if self._system2 is not None:
            snapshot = self._snapshot()
            offline = snapshot.get("system2_route_status") != "verified"
            if not (offline and not self._supersedes_message_id and not self._proposal_context
                    and (turn := self._script.reply(value, snapshot)) is not None):
                self._send_system2(value)
                return
        else:
            turn = self._script.reply(value, self._snapshot())
        speaker = "你 · 模拟语音" if simulated else "你"
        if self._store is not None:
            try:
                self._store.begin_turn(speaker, value, turn)
            except sqlite3.Error:
                self.storage_error.emit("暂时无法保存对话。")
                return
        self._messages.append(Message(speaker, value))
        self._composition = None
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

    def _send_system2(self, value):
        message_id = uuid4().hex
        try:
            self._system2(message_id, value, proposal=self._proposal_context,
                          supersedes_message_id=self._supersedes_message_id)
        except (sqlite3.Error, RuntimeError):
            self.storage_error.emit("暂时无法保存并提交消息。")
            return
        self._draft = ""
        self._supersedes_message_id = self._proposal_context = None
        self.refresh_projection(self._store.projection_version())
        QTimer.singleShot(0, self._to_latest)
        self._request_typing_focus()

    def refresh_projection(self, version):
        if self._system2 is None or version == self._projection_version:
            return
        had_focus = bool(self._editor and self._editor.hasFocus())
        self._messages = [Message(*row) for row in self._store.projected_messages()]
        self._projection_version = version
        if self._messages:
            self._follow_up = True
        self._render()
        if had_focus:
            self._request_typing_focus()

    def refresh_route(self, status):
        if status != self._route_status:
            self._route_status = status
            self._render()

    def _edit_message(self, message_id):
        self._supersedes_message_id = message_id
        self._proposal_context = None
        self._compose("typing")

    def reply_to_proposal(self, proposal):
        self._proposal_context = (proposal["id"], proposal["version"])
        self._supersedes_message_id = None
        self._compose("typing")

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
        card = _MessageCard()
        if mochi:
            card.setProperty("speaker", "mochi")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(8, 7, 8, 7)
        layout.setSpacing(2)
        layout.addWidget(self._label(message.speaker, sender=True, speaker="mochi" if mochi else "you"))
        if message.reply_to and message.reply_source:
            quotation = message.reply_source[:44] + ("…" if len(message.reply_source) > 44 else "")
            reply = self._label(f"回复「{quotation}」", sender=True)
            reply.setProperty("replyToMessageId", message.reply_to)
            layout.addWidget(reply)
        card.body = _MessageBody(message.text, markdown=mochi)
        layout.addWidget(card.body)
        if message.pending:
            status = QHBoxLayout()
            status.addWidget(self._label("等待回复", sender=True), 1)
            edit = QPushButton("改口")
            edit.setObjectName("refine-chat-supersede")
            edit.clicked.connect(lambda: self._edit_message(message.id))
            status.addWidget(edit)
            layout.addLayout(status)
        return card

    def _composer(self) -> QFrame:
        voice = self._composition == "voice"
        card = QFrame()
        card.setObjectName("refine-chat-composer")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(9, 9, 9, 9)
        layout.setSpacing(4)
        header = QHBoxLayout()
        heading = "语音输入中" if voice else "打字输入中"
        if self._proposal_context:
            heading = "回复提议"
        if self._supersedes_message_id:
            heading = "更正这条消息"
        header.addWidget(self._label(heading, sender=True), 1)
        if self._proposal_context or self._supersedes_message_id:
            cancel = QPushButton("取消")
            cancel.clicked.connect(self._cancel_context)
            header.addWidget(cancel)
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

    def _cancel_context(self):
        self._proposal_context = self._supersedes_message_id = None
        self._compose("typing")

    def _save_draft(self, text: str):
        self._draft = text
        if self._store is not None:
            try:
                self._store.save_draft(text)
            except sqlite3.Error:
                self.storage_error.emit("暂时无法保存草稿。")

    def _clear_widgets(self, layout):
        while layout.count():
            widget = layout.takeAt(0).widget()
            widget.hide()
            if widget is not self._proposal_card:
                widget.setParent(None)
                widget.deleteLater()

    def _render_messages(self):
        for index, message in enumerate(self._messages):
            if index < len(self._rendered_messages):
                if self._rendered_messages[index] == message:
                    continue
                previous = self._message_cards.takeAt(index).widget()
                previous.hide()
                previous.setParent(None)
                previous.deleteLater()
            self._message_cards.insertWidget(index, self._message_card(message))
        while self._message_cards.count() > len(self._messages):
            widget = self._message_cards.takeAt(self._message_cards.count() - 1).widget()
            widget.hide()
            widget.setParent(None)
            widget.deleteLater()
        self._rendered_messages = self._messages.copy()

    def _render(self):
        self._editor = self._elapsed = self._waveform = None
        self._clear_widgets(self._extras)
        self._clear_widgets(self._cards)
        self._render_messages()
        if self._proposal_card is not None and self._proposal_card.has_proposals:
            self._extras.addWidget(self._proposal_card)
            self._proposal_card.show()
        if self._configure_system2 and self._route_status in {"not_configured", "unverified"}:
            configure = QPushButton("配置对话模型" if self._route_status == "not_configured" else "验证对话模型")
            configure.setObjectName("refine-chat-configure-system2")
            configure.clicked.connect(self._configure_system2)
            self._extras.addWidget(configure)
        if self._pending is not None:
            self._extras.addWidget(self._message_card(Message("Mochi", "正在更新…")))
        self._history.setVisible(bool(self._messages or self._extras.count()))

        self.toolbar_mode_changed.emit(self._composition if not self._messages else None)
        if self._composition is not None:
            self._cards.addWidget(self._composer())
        elif self._pending is None and self._follow_up:
            mode = "keyboard" if self._preferred == "typing" else "mic"
            button = _GestureButton(mode, "继续输入", accent=True, hold=self._voice_available, width=34)
            button.setObjectName("refine-chat-continue")
            button.tapped.connect(lambda: self._compose(self._preferred))
            button.held.connect(lambda: self._compose(self._other(self._preferred)))
            self._cards.addWidget(button, 0, Qt.AlignmentFlag.AlignHCenter)
        self.updateGeometry()

    def refresh_proposals(self):
        self._render()
