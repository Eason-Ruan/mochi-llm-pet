"""Native Mochi window and its shared Figma toolbar."""

from __future__ import annotations

from collections.abc import Callable
import sys

from PyQt6.QtCore import (
    QEasingCurve, QEvent, QPoint, QPropertyAnimation, QRect, QRectF, QSize, Qt, QTimer,
    QVariantAnimation, pyqtSignal,
)
from PyQt6.QtGui import QColor, QGuiApplication, QPainter, QPen, QRegion
from PyQt6.QtWidgets import (
    QFrame, QGraphicsEffect, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QMenu,
    QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from .desktop import DesktopPet
from .refine_icons import icon as icon


CREAM = "#fff7e9"
INK = "#49382a"
TEXT = "#312921"
PAPER = "#fffcf6"
GREEN = "#35533c"
SELECTED = "#e0ebd8"
PEACH = "#ffd9b3"

WIDTH = 292
CAT_HEIGHT = 64
GAP = 7
MARGIN = 2
DURATION_MS = 270
HOLD_MS = 550

_BUTTON_STYLE = f"""
    QPushButton {{
        color: {TEXT}; background: {PAPER}; border: 2px solid {INK};
        border-radius: 8px; font: 700 13px;
    }}
    QPushButton[selected="true"] {{ background: {SELECTED}; color: {GREEN}; }}
    QPushButton[affirmative="true"] {{ background: {PEACH}; }}
    QPushButton:focus {{ border-color: #795f42; }}
    QPushButton:pressed {{ background: #f6ead6; }}
"""


def _surface(widget: QWidget, radius: int) -> None:
    painter = QPainter(widget)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#39704f32"))
    painter.drawRoundedRect(QRectF(0, 2, widget.width(), widget.height() - 2), radius, radius)
    painter.setPen(QPen(QColor(INK), 2))
    painter.setBrush(QColor(CREAM))
    painter.drawRoundedRect(QRectF(1, 1, widget.width() - 2, widget.height() - 4), radius, radius)


def _button(parent: QWidget, name: str, label: str, width: int, height: int, *, hold=False):
    button = _HoldButton(parent) if hold else QPushButton(parent)
    button.setObjectName(name)
    button.setAccessibleName(label)
    button.setToolTip(label)
    button.setFixedSize(width, height)
    button.setIconSize(QSize(16, 16))
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setStyleSheet(_BUTTON_STYLE)
    return button


class _HoldButton(QPushButton):
    held = pyqtSignal()

    def __init__(self, parent):
        super().__init__(parent)
        self.hold_enabled = True
        self._held = False
        self._hold_timer = QTimer(self)
        self._hold_timer.setSingleShot(True)
        self._hold_timer.setInterval(HOLD_MS)
        self._hold_timer.timeout.connect(self._commit_hold)

    def _commit_hold(self):
        if self.isDown() and self.hold_enabled:
            self._held = True
            self.held.emit()

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self._held = False
            if self.hold_enabled and self.isDown():
                self._hold_timer.start()

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        if not self.isDown():
            self._hold_timer.stop()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._hold_timer.stop()
            if self._held:
                self._held = False
                self.setDown(False)
                event.accept()
                return
        super().mouseReleaseEvent(event)


class _PixelFade(QGraphicsEffect):
    """Quantize a diagonal sweep to square cells and three opacity levels."""

    def __init__(self, widget, opening, complete):
        super().__init__(widget)
        self.opening = opening
        self._progress = 0.0
        self.animation = QVariantAnimation(self)
        self.animation.setDuration(DURATION_MS)
        self.animation.setStartValue(0.0)
        self.animation.setEndValue(1.0)
        self.animation.valueChanged.connect(self._advance)
        self.animation.finished.connect(complete)

    def _advance(self, progress):
        self._progress = progress
        self.update()

    def draw(self, painter):
        if self._progress >= 1:
            if self.opening:
                self.drawSource(painter)
            return
        if self._progress <= 0:
            if not self.opening:
                self.drawSource(painter)
            return

        snapshot, offset = self.sourcePixmap(
            Qt.CoordinateSystem.LogicalCoordinates,
            QGraphicsEffect.PixmapPadMode.NoPad,
        )
        bounds = snapshot.deviceIndependentSize().toSize()
        width, height = bounds.width(), bounds.height()
        cell = 8
        span = max(1, width + height - 2 * cell)
        feather = 0.17
        edge = self._progress * (1 + feather)
        regions = [QRegion() for _ in range(4)]
        for y in range(0, height, cell):
            for x in range(0, width, cell):
                reveal = (edge - (x + y) / span) / feather
                level = max(0, min(3, int(reveal * 4)))
                if not self.opening:
                    level = 3 - level
                if level:
                    regions[level] += QRegion(
                        QRect(offset.x() + x, offset.y() + y, min(cell, width - x), min(cell, height - y))
                    )
        for opacity, region in zip((0.30, 0.65, 1.0), regions[1:]):
            if not region.isEmpty():
                painter.save()
                painter.setClipRegion(region, Qt.ClipOperation.IntersectClip)
                painter.setOpacity(opacity)
                painter.drawPixmap(offset, snapshot)
                painter.restore()


class _Toolbar(QWidget):
    def paintEvent(self, event):
        _surface(self, 13)


class _Bubble(QFrame):
    def __init__(
        self, pet, text: str, on_answer: Callable[[bool], None] | None,
        on_painted: Callable[[], bool | None] | None, on_dismiss: Callable[[], None] | None,
    ):
        super().__init__(pet)
        self.setObjectName("refine-bubble")
        self._pet = pet
        self._on_painted = on_painted
        self._on_answer = on_answer
        self._on_dismiss = on_dismiss
        self._paint_timer = QTimer(self)
        self._paint_timer.setSingleShot(True)
        self._paint_timer.timeout.connect(self._report)
        self._retry_timer = QTimer(self)
        self._retry_timer.setInterval(45)
        self._retry_timer.timeout.connect(self.update)

        self.setFixedWidth(WIDTH)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(13, 11, 13, 13)
        layout.setSpacing(7)
        label = QLabel(text)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setStyleSheet(f"color: {TEXT}; font: 700 14px; background: transparent;")
        label.setAccessibleName(text)
        if on_answer is None:
            label.setObjectName("refine-bubble-dismiss")
            label.setCursor(Qt.CursorShape.PointingHandCursor)
            label.setToolTip("点击收起消息")
            label.installEventFilter(self)

        if label.heightForWidth(WIDTH - 26) > 105:
            scroller = QScrollArea(self)
            scroller.setObjectName("refine-bubble-scroll")
            scroller.setStyleSheet(
                "QScrollArea#refine-bubble-scroll { border: 0; background: transparent; }"
                "QScrollArea#refine-bubble-scroll > QWidget { background: transparent; }"
            )
            scroller.setFixedHeight(105)
            scroller.setWidgetResizable(True)
            scroller.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            label.setMinimumHeight(label.heightForWidth(WIDTH - 41))
            scroller.setWidget(label)
            layout.addWidget(scroller)
        else:
            layout.addWidget(label)

        if on_answer is not None:
            row = QHBoxLayout()
            row.setSpacing(7)
            for action, label_text, value in (("yes", "是", True), ("no", "否", False)):
                button = _button(self, f"refine-bubble-{action}", label_text, 52, 29)
                button.setText(label_text)
                button.setProperty("affirmative", value)
                button.clicked.connect(lambda _checked=False, answer=value: self._answer(answer))
                row.addWidget(button)
            row.addStretch()
            layout.addLayout(row)
        else:
            self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def _answer(self, value: bool):
        if self._pet.bubble is not self:
            return
        handler = self._on_answer
        self._pet.clear_bubble()
        if handler is not None:
            handler(value)

    def _dismiss(self):
        if self._pet.bubble is self and self._on_answer is None:
            handler = self._on_dismiss
            self._pet.clear_bubble()
            if handler is not None:
                handler()

    def eventFilter(self, watched, event):
        if (event.type() == QEvent.Type.MouseButtonRelease
                and event.button() == Qt.MouseButton.LeftButton):
            self._dismiss()
            return True
        return super().eventFilter(watched, event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dismiss()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Space, Qt.Key.Key_Return) and self._on_answer is None:
            self._dismiss()
            event.accept()
        else:
            super().keyPressEvent(event)

    def paintEvent(self, event):
        _surface(self, 13)
        if (self._on_painted is not None and self._pet._backing_store_paint
                and not self._paint_timer.isActive()):
            self._paint_timer.start(0)

    def showEvent(self, event):
        super().showEvent(event)
        if self._on_painted is not None and QGuiApplication.platformName() not in ("offscreen", "minimal"):
            self._retry_timer.start()

    def _report(self):
        if not self._pet._bubble_exposed(self) or self._on_painted is None:
            return
        if self._on_painted() is not False:
            self._on_painted = None
            self._retry_timer.stop()


class RefinePet(DesktopPet):
    """Anchored cat, native child controls, and an optional read-only panel."""

    section_requested = pyqtSignal(str)
    input_held = pyqtSignal()
    diagnostics_requested = pyqtSignal()
    collapsed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Mochi")
        if sys.platform == "darwin":
            self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
        self.setToolTip("")
        self._expanded = True
        self._closing_toolbar = False
        self._desired_panel: QWidget | None = None
        self._panel: QWidget | None = None
        self._closing_panel: QWidget | None = None
        self._section: str | None = None
        self._chat_toolbar: str | None = None
        self._input_mode = "voice"
        self._work_state: str | None = None
        self._cat_area = QRect(0, 0, WIDTH, CAT_HEIGHT)
        self._backing_store_paint = False
        self.bubble: _Bubble | None = None
        self._in_layout = False
        self._last_geometry = None

        self.toolbar = _Toolbar(self)
        self.toolbar.setObjectName("refine-toolbar")
        self.toolbar.setFixedSize(280, 58)
        row = QHBoxLayout(self.toolbar)
        row.setContentsMargins(9, 8, 9, 10)
        row.setSpacing(7)
        self.buttons = {}
        for key, label, width in (
            ("reminders", "提醒事项", 68),
            ("work", "专注与回顾", 44),
            ("input", "语音输入", 44),
            ("settings", "设置", 44),
            ("collapse", "收起菜单", 34),
        ):
            button = _button(self.toolbar, f"refine-menu-{key}", label, width, 40, hold=key == "input")
            self.buttons[key] = button
            row.addWidget(button)
            if key == "collapse":
                button.clicked.connect(self._collapse)
            else:
                button.clicked.connect(lambda _checked=False, section=key: self.section_requested.emit(section))

        self.buttons["input"].held.connect(self.input_held)
        # The bell and badge are decorative children of one clickable control.
        badge_layout = QHBoxLayout(self.buttons["reminders"])
        badge_layout.setContentsMargins(7, 0, 7, 0)
        badge_layout.setSpacing(6)
        badge_layout.addStretch()
        self._bell = QLabel()
        self._bell.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        badge_layout.addWidget(self._bell)
        self._badge = QLabel("0")
        self._badge.setObjectName("refine-reminder-count")
        self._badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._badge.setMinimumWidth(21)
        self._badge.setFixedHeight(22)
        self._badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._badge.setStyleSheet(f"""
            color: {TEXT}; background: {PEACH}; border-radius: 5px; font: 700 13px;
            padding-left: 2px; padding-right: 2px;
        """)
        badge_layout.addWidget(self._badge)
        badge_layout.addStretch()

        self._expand = _button(self, "refine-menu-expand", "展开菜单", 32, 26)
        self._expand.setIcon(icon("down", INK, 14))
        self._expand.clicked.connect(self._open_toolbar)
        self._expand.hide()
        self._update_buttons()
        self._layout_pet()
        if self.screen() is not None:
            area = self.screen().availableGeometry()
            # Leave enough space below the cat for the taller Figma panels.
            self.move(max(area.left(), area.right() - self.width() - 24),
                      max(area.top(), area.bottom() - self.height() - 245))

    def set_status(self, count: int, work_state: str | None, input_mode: str) -> None:
        status_changed = (self.pending_count, self._work_state, self._input_mode) != (count, work_state, input_mode)
        if not status_changed:
            return
        self.pending_count = count
        self._work_state = work_state
        self._input_mode = input_mode
        self.player.set_state("sit" if work_state == "focusing" else "idle")
        self._badge.setText(str(count))
        self._update_buttons()
        self.update(self._cat_area)

    def set_chat_toolbar(self, mode: str | None) -> None:
        if mode != self._chat_toolbar:
            self._chat_toolbar = mode
            self._update_buttons()

    def _update_buttons(self):
        for key, button in self.buttons.items():
            selected = key == self._section
            if button.property("selected") != selected:
                button.setProperty("selected", selected)
                button.style().unpolish(button)
                button.style().polish(button)
                button.update()
            color = GREEN if selected else INK
            if key == "reminders":
                self._bell.setPixmap(icon("bell", color, 16).pixmap(QSize(16, 16), self.devicePixelRatioF()))
                label = f"提醒事项 · {self.pending_count} 项"
            elif key == "input":
                mode = self._chat_toolbar if selected else self._input_mode
                icon_name, label = {
                    "voice": ("mic", "语音输入"),
                    "typing": ("keyboard", "打字输入"),
                    None: ("back", "返回"),
                }[mode]
                button.hold_enabled = mode is not None
                button.setIcon(icon(icon_name, color, 16))
            elif key == "work":
                variants = {
                    "focusing": ("work-focusing", "专注中"),
                    "paused": ("work-paused", "已暂停"),
                    "break_due": ("work-review", "待回顾"),
                    "ended": ("work-review", "已结束"),
                }
                glyph, state = variants.get(self._work_state, ("work", ""))
                label = f"专注与回顾 · {state}" if state else "专注与回顾"
                button.setIcon(icon(glyph, color, 16))
            else:
                label = "设置" if key == "settings" else "收起菜单"
                button.setIcon(icon("up" if key == "collapse" else key, color, 16))
            button.setAccessibleName(label)
            button.setToolTip(label)

    def set_section(self, key: str | None, widget: QWidget | None) -> None:
        self._section = key
        self._desired_panel = widget
        self._update_buttons()
        if widget is not None and not self._expanded:
            self._open_toolbar()
        self._sync_panel()

    def _start_fade(self, widget, opening, complete):
        def finished():
            def clean():
                if widget.graphicsEffect() is effect:
                    widget.setGraphicsEffect(None)
                    complete()
            # Removing an effect also deletes the animation that emitted the signal.
            QTimer.singleShot(0, clean)

        effect = _PixelFade(widget, opening, finished)
        widget.setGraphicsEffect(effect)
        effect.animation.start()

    def _sync_panel(self):
        requested = self._desired_panel if self._expanded else None
        if self._closing_panel is not None:
            return
        if self._panel is requested:
            self._layout_pet()
            return
        if self._panel is not None:
            panel = self._panel
            self._closing_panel = panel
            panel.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

            def after_close():
                panel.hide()
                panel.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
                self._panel = self._closing_panel = None
                self._sync_panel()

            self._start_fade(panel, False, after_close)
            return

        self._panel = requested
        if requested is not None:
            requested.setParent(self)
            self._layout_pet()
            requested.show()
            self._start_fade(requested, True, lambda: None)
        else:
            self._layout_pet()

    def _collapse(self):
        if not self._expanded:
            return
        self._expanded = False
        self._closing_toolbar = True
        self._section = None
        self._desired_panel = None
        self._update_buttons()
        self.toolbar.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.collapsed.emit()
        self._sync_panel()

        def after_close():
            self._closing_toolbar = False
            self.toolbar.hide()
            self.toolbar.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
            self._expand.show()
            self._layout_pet()

        self._start_fade(self.toolbar, False, after_close)

    def _open_toolbar(self):
        if self._expanded:
            return
        self._expanded = True
        self._closing_toolbar = False
        self.toolbar.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self._expand.hide()
        self.toolbar.show()
        self._layout_pet()
        self._start_fade(self.toolbar, True, lambda: None)

    def show_bubble(
        self, text: str, on_answer: Callable[[bool], None] | None = None,
        on_painted: Callable[[], bool | None] | None = None, *,
        on_dismiss: Callable[[], None] | None = None, animate: bool = False,
    ) -> None:
        self.clear_bubble()
        self.bubble = _Bubble(self, text, on_answer, on_painted, on_dismiss)
        self._layout_pet()
        if animate:
            self._animate_bubble(self.bubble)
        self.bubble.show()
        self.update(self.bubble.geometry())

    def _animate_bubble(self, bubble):
        effect = QGraphicsOpacityEffect(bubble)
        effect.setOpacity(0)
        bubble.setGraphicsEffect(effect)
        animation = QPropertyAnimation(effect, b"opacity", effect)
        animation.setDuration(280)
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)

        def clean():
            if self.bubble is bubble and bubble.graphicsEffect() is effect:
                bubble.setGraphicsEffect(None)

        animation.finished.connect(lambda: QTimer.singleShot(0, clean))
        animation.start()

    def clear_bubble(self) -> None:
        if self.bubble is not None:
            bubble, self.bubble = self.bubble, None
            bubble.hide()
            bubble.deleteLater()
            self._layout_pet()

    def _bubble_exposed(self, bubble: _Bubble) -> bool:
        handle = self.windowHandle()
        return (
            bubble is self.bubble and QGuiApplication.platformName() not in ("offscreen", "minimal")
            and bool(handle and handle.isExposed()) and bubble.isVisible()
            and not self.isMinimized() and not bubble.visibleRegion().isEmpty()
        )

    def event(self, event):
        if event.type() == QEvent.Type.UpdateRequest:
            # QWidget.grab()/render() can issue paint events outside the native
            # backing-store update, so those cannot acknowledge a reminder.
            self._backing_store_paint = True
            try:
                return super().event(event)
            finally:
                self._backing_store_paint = False
        result = super().event(event)
        if event.type() == QEvent.Type.LayoutRequest and hasattr(self, "toolbar"):
            self._layout_pet()
        return result

    def _layout_pet(self):
        if self._in_layout:
            return
        self._in_layout = True
        try:
            self._update_geometry()
        finally:
            self._in_layout = False

    def _update_geometry(self):
        anchor = self.y() + self._cat_area.top()
        screen = self.screen()
        available = screen.availableGeometry() if screen is not None else None
        y = MARGIN
        regions = QRegion()

        if self.bubble is not None:
            bubble_height = max(self.bubble.sizeHint().height(), self.bubble.heightForWidth(WIDTH))
            self.bubble.setGeometry(MARGIN, y, WIDTH, bubble_height)
            regions += QRegion(self.bubble.geometry())
            y += bubble_height + GAP

        self._cat_area = QRect(MARGIN, y, WIDTH, CAT_HEIGHT)
        regions += QRegion(QRect((WIDTH + 2 * MARGIN - 88) // 2, y, 88, CAT_HEIGHT))
        y += CAT_HEIGHT + GAP
        if self._expanded or self._closing_toolbar:
            self.toolbar.move((WIDTH + 2 * MARGIN - self.toolbar.width()) // 2, y)
            regions += QRegion(self.toolbar.geometry())
            y += self.toolbar.height()
        else:
            self._expand.move((WIDTH + 2 * MARGIN - self._expand.width()) // 2, y)
            regions += QRegion(self._expand.geometry())
            y += self._expand.height()

        if self._panel is not None:
            y += GAP
            panel_height = max(self._panel.minimumHeight(), self._panel.sizeHint().height(), 1)
            if self._panel.hasHeightForWidth():
                panel_height = max(panel_height, self._panel.heightForWidth(WIDTH))
            self._panel.setGeometry(MARGIN, y, WIDTH, panel_height)
            regions += QRegion(self._panel.geometry())
            y += panel_height

        size = QSize(WIDTH + 2 * MARGIN, y + MARGIN)
        x = self.x()
        y = anchor - self._cat_area.top()
        if available is not None:
            x = max(available.left(), min(x, available.right() - size.width() + 1))
            y = max(available.top(), min(y, available.bottom() - size.height() + 1))
        geometry = (x, y, size.width(), size.height())
        if size != self.size():
            self.setFixedSize(size)
        if self.pos() != QPoint(x, y):
            self.move(x, y)
        if (geometry, regions) != self._last_geometry:
            if QGuiApplication.platformName() not in ("offscreen", "minimal"):
                self.setMask(regions)
            self._last_geometry = (geometry, regions)
        self.update()

    def _animate(self):
        self.player.update(.1)
        self.update(self._cat_area)

    def paintEvent(self, event):
        frame = self.player.current_frame()
        if frame is None or frame.isNull():
            return
        painter = QPainter(self)
        painter.setClipRect(self._cat_area)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        # Susan's idle clip is 32×32 with transparent rows above the 18px cat.
        # A 4× integer scale puts the same 72px cat at the Figma baseline.
        target = QRect(self.width() // 2 - 68, self._cat_area.bottom() + 1 - 128, 128, 128)
        painter.drawPixmap(target, frame)

    def mousePressEvent(self, event):
        if self._cat_area.contains(event.position().toPoint()):
            super().mousePressEvent(event)
        else:
            event.ignore()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._press is not None:
            self._press = None
            event.accept()
        else:
            event.ignore()

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.addAction("打开原联调界面", self.diagnostics_requested.emit)
        menu.addSeparator()
        menu.addAction("退出 Mochi", self.quit_requested.emit)
        menu.exec(event.globalPos())

    def activate_for_input(self) -> None:
        self.raise_()
        self.activateWindow()
        if sys.platform == "darwin" and QGuiApplication.platformName() not in ("offscreen", "minimal"):
            import AppKit
            AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
