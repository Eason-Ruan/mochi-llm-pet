"""The cream and sage treatment shared by Mochi's desktop panels."""
from functools import cache

from PyQt6.QtCore import QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QPainter, QPen
from PyQt6.QtWidgets import (
    QAbstractSpinBox, QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)


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

    def add_manage_action(self, label, callback):
        button = QPushButton(label)
        button.setObjectName("refine-manage")
        button.setFont(_font(11, bold=True))
        button.setFixedHeight(22)
        button.setStyleSheet(
            f"QPushButton {{color: {GREEN}; background: {PAPER}; border: 1px solid {CARD_OUTLINE};"
            "border-radius: 5px; padding: 0 6px}"
            f"QPushButton:hover, QPushButton:focus {{background: {GREEN_TINT}}}"
        )
        button.clicked.connect(callback)
        row = self.body.itemAt(0).layout()
        row.insertWidget(1, button)
        return button

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


_CONTROL_STYLE = f"""
QLabel, QCheckBox {{ color: {INK}; }}
QPushButton, QComboBox, QLineEdit, QAbstractSpinBox, QListWidget, QTextEdit {{
    color: {INK}; background: {PAPER}; border: 1px solid {CARD_OUTLINE};
    border-radius: 6px; padding: 4px 8px; min-height: 20px;
}}
QPushButton:hover {{ background: {SAND}; }}
QPushButton[primary="true"] {{ background: {GREEN_TINT}; border-color: {GREEN}; color: {GREEN}; font-weight: bold; }}
QPushButton:disabled, QComboBox:disabled, QAbstractSpinBox:disabled, QPushButton[primary="true"]:disabled {{
    color: {MUTED}; background: {SAND}; border-color: {CARD_OUTLINE};
}}
QPushButton:focus, QComboBox:focus, QLineEdit:focus, QAbstractSpinBox:focus {{
    border-color: {GREEN};
}}
"""


DETAIL_CONTROL_STYLE = _CONTROL_STYLE + f"""
QPushButton, QComboBox, QLineEdit, QAbstractSpinBox, QCheckBox {{
    font-size: 12px;
}}
QPushButton {{font-weight: bold}}
QGroupBox {{background: transparent; color: {INK}; border: 0; margin: 0}}
QComboBox::drop-down, QDateEdit::drop-down {{
    border: 0; background: transparent; width: 22px; subcontrol-origin: padding;
}}
QComboBox QAbstractItemView {{background: {PAPER}; color: {INK}; selection-background-color: {GREEN_TINT}; selection-color: {INK}}}
QAbstractSpinBox QLineEdit {{background: transparent; border: 0; padding: 0}}
QLineEdit {{placeholder-text-color: #877563}}
QPushButton[stepper="true"] {{background: transparent; border: 0; min-width: 0; padding: 0; min-height: 0}}
QPushButton[stepper="true"]:hover, QPushButton[stepper="true"]:focus {{background: {GREEN_TINT}; border: 1px solid {GREEN}}}
"""


class DetailPanel(_Panel):
    def __init__(self, title, content, on_back):
        super().__init__()
        self.setStyleSheet(DETAIL_CONTROL_STYLE)
        row = _title(title)
        back = QPushButton("返回摘要")
        back.setObjectName("refine-back-to-summary")
        back.clicked.connect(on_back)
        row.addWidget(back)
        self.body.addLayout(row)
        content.setTitle("")
        self._content = content
        self._scroll = _scroll_area(content)
        self.body.addWidget(self._scroll)
        self._reveal_target = None

    def reveal_on_show(self, widget):
        self._reveal_target = widget
        if self.isVisible():
            self._reveal_widget()

    def showEvent(self, event):
        super().showEvent(event)
        if self._reveal_target is not None:
            QTimer.singleShot(0, self._reveal_widget)

    def _reveal_widget(self):
        if self._reveal_target is not None and self.isVisible():
            self.ensureWidgetVisible(self._reveal_target)
            self._reveal_target = None

    def set_height_limit(self, max_height):
        margins = self.body.contentsMargins()
        header = self.body.itemAt(0).layout().sizeHint().height()
        chrome = margins.top() + margins.bottom() + header + self.body.spacing()
        target = min(530, self._content.sizeHint().height())
        height = min(target, max(1, max_height - chrome))
        if self._scroll.height() != height or self._scroll.minimumHeight() != height:
            self._scroll.setFixedHeight(height)

    def ensureWidgetVisible(self, widget, x_margin=10, y_margin=10):
        self._scroll.ensureWidgetVisible(widget, x_margin, y_margin)


class MenuField(QComboBox):
    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setFont(_font(12, bold=True))
        painter.setPen(QColor(MUTED))
        caret = self.rect().adjusted(self.width() - 22, -2, 0, -2)
        painter.drawText(caret, Qt.AlignmentFlag.AlignCenter, "⌄")


class Toggle(QCheckBox):
    """A keyboard-accessible switch with the label on the left."""

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setAttribute(Qt.WidgetAttribute.WA_LayoutUsesWidgetRect)

    def hitButton(self, position):
        return self.rect().contains(position)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setOpacity(1 if self.isEnabled() else 0.55)
        track = QRectF(self.width() - 30.5, (self.height() - 16) / 2, 30, 16)
        painter.setPen(QPen(QColor(GREEN if self.isChecked() else SHADOW)))
        painter.setBrush(QColor(GREEN if self.isChecked() else SAND))
        painter.drawRoundedRect(track, 8, 8)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(PAPER))
        knob = track.right() - 13 if self.isChecked() else track.left() + 3
        painter.drawEllipse(QRectF(knob, track.top() + 3, 10, 10))

        painter.setPen(QColor(INK))
        painter.setFont(_font(12))
        text_area = self.rect().adjusted(0, 0, -36, 0)
        text = painter.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, text_area.width())
        painter.drawText(text_area, Qt.AlignmentFlag.AlignVCenter, text)
        if self.hasFocus():
            painter.setPen(QPen(QColor(GREEN), 1, Qt.PenStyle.DotLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)


def stepper(editor, *, on_step=None, width=110):
    """Keep the native numeric editor and use the design's minus and plus buttons."""
    shell = QFrame()
    shell.setObjectName("refine-stepper")
    shell.setFixedWidth(width)
    shell.setStyleSheet(f"""
        QFrame#refine-stepper {{background: {PAPER}; border: 1px solid {CARD_OUTLINE}; border-radius: 6px}}
        QFrame#refine-stepper QAbstractSpinBox {{background: transparent; border: 0; padding-right: 0}}
    """)
    row = QHBoxLayout(shell)
    row.setContentsMargins(1, 1, 4, 1)
    row.setSpacing(2)
    editor.setMinimumWidth(0)
    editor.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
    editor.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
    row.addWidget(editor, 1)

    def step(direction):
        editor.interpretText()
        editor.stepBy(direction)
        if on_step is not None:
            on_step()

    for direction, glyph, action in ((-1, "−", "减少"), (1, "+", "增加")):
        button = QPushButton(glyph)
        button.setProperty("stepper", True)
        button.setFixedSize(16, 22)
        button.setAccessibleName(action + editor.accessibleName())
        button.clicked.connect(lambda _checked=False, amount=direction: step(amount))
        row.addWidget(button)
    return shell
