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
from .refine_chat import RefineChat


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
        before_paint: Callable[[], bool] | None = None,
        answer_labels: tuple[str, str] | None = None,
    ):
        super().__init__(pet)
        self.setObjectName("refine-bubble")
        self._pet = pet
        self._on_painted = on_painted
        self._on_answer = on_answer
        self._on_dismiss = on_dismiss
        self._before_paint = before_paint
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
            yes, no = answer_labels or ("是", "否")
            for action, label_text, value in (("yes", yes, True), ("no", no, False)):
                button = _button(self, f"refine-bubble-{action}", label_text, 52, 29)
                button.setText(label_text)
                button.ensurePolished()
                button.setFixedWidth(max(52, button.fontMetrics().horizontalAdvance(label_text) + 16))
                button.setProperty("affirmative", value)
                button.clicked.connect(lambda _checked=False, answer=value: self._answer(answer))
                row.addWidget(button)
            row.addStretch()
            if on_dismiss is not None:
                close = _button(self, "refine-bubble-close", "×", 29, 29)
                close.setAccessibleName("关闭提示")
                close.setToolTip("关闭提示")
                close.clicked.connect(self._dismiss)
                row.addWidget(close)
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
        if self._pet.bubble is self:
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
        if event.button() == Qt.MouseButton.LeftButton and self._on_answer is None:
            self._dismiss()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if (event.key() == Qt.Key.Key_Escape and self._on_dismiss is not None
                or (event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return) and self._on_answer is None)):
            self._dismiss()
            event.accept()
        else:
            super().keyPressEvent(event)

    def paintEvent(self, event):
        if (self._pet._backing_store_paint and self._before_paint is not None
                and not self._before_paint()):
            self.hide()
            return
        _surface(self, 13)
        if (self._on_painted is not None and self._pet._backing_store_paint
                and not self._paint_timer.isActive()):
            self._paint_timer.start(0)

    def showEvent(self, event):
        super().showEvent(event)
        if self._on_painted is not None and (QGuiApplication.platformName() not in ("offscreen", "minimal")
                                             or getattr(self._pet, "_synthetic_receipts", False)):
            self._retry_timer.start()

    def _report(self):
        if not self._pet._bubble_exposed(self) or self._on_painted is None:
            return
        if self._on_painted() is not False:
            self._on_painted = None
            self._retry_timer.stop()


class _DeliveryBadge(QLabel):
    def __init__(self, pet, *args):
        super().__init__(*args)
        self._pet = pet
        self._deliveries = {}
        self._attention = set()

    def set_deliveries(self, deliveries):
        changed = self._deliveries.keys() != deliveries.keys()
        self._deliveries = deliveries
        if changed or deliveries:
            self.update()

    def clear_attention(self):
        if self._attention:
            self._attention.clear()
            self.update()

    def paintEvent(self, event):
        reports = []
        if self._pet._backing_store_paint and self._pet._widget_exposed(self):
            for key, (permit, report) in self._deliveries.items():
                if permit():
                    reports.append((key, report))
        super().paintEvent(event)
        visible = self._attention | {key for key, _ in reports}
        if visible:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#9f3b29"))
            painter.drawRoundedRect(self.rect(), 5, 5)
            painter.setPen(QColor(PAPER))
            count = len(visible)
            cue = "!" if count == 1 else f"{count}!" if count < 10 else "9+"
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, cue)
        if reports:
            def painted():
                if self._pet._widget_exposed(self):
                    for key, report in reports:
                        if report():
                            self._attention.add(key)
                            self._deliveries.pop(key, None)
            QTimer.singleShot(0, painted)


class RefinePet(DesktopPet):
    """Anchored cat with native child controls and one selected panel."""

    section_requested = pyqtSignal(str)
    input_held = pyqtSignal()
    diagnostics_requested = pyqtSignal()
    collapsed = pyqtSignal()
    home_moved = pyqtSignal(QPoint)

    def __init__(self, *, diagnostics=True, voice_available=True, home=None):
        super().__init__()
        self.setWindowTitle("Mochi")
        if sys.platform == "darwin":
            self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
        self.setToolTip("")
        self._diagnostics = diagnostics
        self._voice_available = voice_available
        self._reduce_motion = False
        self._allow_proactive_movement = True
        self._drag_origin = None
        self._home_anchor = QPoint(home) if isinstance(home, QPoint) else None
        self._temporary_anchor = None
        self._motion_pose = QTimer(self)
        self._motion_pose.setSingleShot(True)
        self._motion_pose.timeout.connect(self._set_base_pose)
        self._expanded = True
        self._closing_toolbar = False
        self._desired_panel: QWidget | None = None
        self._panel: QWidget | None = None
        self._closing_panel: QWidget | None = None
        self._section: str | None = None
        self._chat_toolbar: str | None = None
        self._proposal_count = 0
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
        self._badge = _DeliveryBadge(self, "0")
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

        self._proposal_badge = _DeliveryBadge(self, self.buttons["input"])
        self._proposal_badge.setObjectName("refine-proposal-count")
        self._proposal_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._proposal_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._proposal_badge.setGeometry(24, 3, 17, 15)
        self._proposal_badge.setStyleSheet(f"color: {TEXT}; background: {PEACH}; border-radius: 5px; font: 700 10px;")
        self._proposal_badge.hide()

        self._expand = _button(self, "refine-menu-expand", "展开菜单", 32, 26)
        self._expand.setIcon(icon("down", INK, 14))
        self._expand.clicked.connect(self._open_toolbar)
        self._expand.hide()
        self._update_buttons()
        screen = self.screen()
        if self._home_anchor is None or QGuiApplication.screenAt(self._home_anchor) is None:
            area = screen.availableGeometry() if screen is not None else self.geometry()
            bottom_margin = 245
            self._home_anchor = QPoint(
                max(area.left() + MARGIN, area.right() - WIDTH - MARGIN - 24),
                max(area.top() + MARGIN, area.bottom() - CAT_HEIGHT - GAP - self.toolbar.height() - bottom_margin),
            )
        self._layout_pet()

    def set_reduce_motion(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._reduce_motion:
            return
        self._reduce_motion = enabled
        if enabled:
            self.end_temporary_motion()
            self.timer.stop()
        else:
            self.timer.start()
        self.update()

    def set_proactive_movement(self, enabled: bool) -> None:
        if self._allow_proactive_movement != enabled:
            self._allow_proactive_movement = enabled
            if not enabled:
                self.end_temporary_motion()

    def apply_motion_intent(self, position_hint, animation):
        if self._press is not None or self._reduce_motion:
            return False
        moved = False
        if position_hint == "screen_edge" and self._allow_proactive_movement:
            screen = QGuiApplication.screenAt(self._home_anchor) or self.screen()
            if screen is not None:
                area = screen.availableGeometry()
                at_left = self._home_anchor.x() + WIDTH // 2 <= area.center().x()
                x = area.left() + MARGIN if at_left else area.right() - WIDTH - MARGIN + 1
                anchor = QPoint(x, self._home_anchor.y())
                moved = self._temporary_anchor != anchor and self._home_anchor != anchor
                self._temporary_anchor = anchor
                self._layout_pet()
        elif position_hint == "home":
            moved = self._temporary_anchor is not None
            self.end_temporary_motion()

        pose = {"look_up": "stretch", "wave": "happy", "approach": "walk"}.get(animation)
        if pose and pose in self.player.clips:
            self.player.set_state(pose, force=True)
            self._motion_pose.start(850)
            self.update(self._cat_area)
            return True
        return moved

    def end_temporary_motion(self):
        self._motion_pose.stop()
        self._set_base_pose()
        if self._temporary_anchor is not None:
            self._temporary_anchor = None
            self._layout_pet()

    def _set_base_pose(self):
        poses = {"focusing": "sit", "resting": "lie"}
        self.player.set_state(poses.get(self._work_state, "idle"))
        self.update(self._cat_area)

    def set_status(self, count: int, work_state: str | None, input_mode: str) -> None:
        status_changed = (self.pending_count, self._work_state, self._input_mode) != (count, work_state, input_mode)
        if not status_changed:
            return
        self.pending_count = count
        self._work_state = work_state
        self._input_mode = input_mode
        if not self._motion_pose.isActive():
            self._set_base_pose()
        self._badge.setText(str(count))
        self._update_buttons()
        self.update(self._cat_area)

    def set_chat_toolbar(self, mode: str | None) -> None:
        if mode != self._chat_toolbar:
            self._chat_toolbar = mode
            self._update_buttons()

    def set_proposal_count(self, count):
        if count != self._proposal_count:
            self._proposal_count = count
            self._proposal_badge.setText(str(count) if count < 10 else "9+")
            self._proposal_badge.setVisible(bool(count))
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
                if self._proposal_count:
                    label += f" · {self._proposal_count} 条待确认提议"
                button.hold_enabled = self._voice_available and mode is not None
                button.setIcon(icon(icon_name, color, 16))
            elif key == "work":
                variants = {
                    "focusing": ("work-focusing", "专注中"),
                    "resting": ("work-paused", "休息中"),
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
        if self._reduce_motion:
            widget.setGraphicsEffect(None)
            complete()
            return

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
        before_paint: Callable[[], bool] | None = None,
        answer_labels: tuple[str, str] | None = None,
    ) -> None:
        self.clear_bubble()
        self.bubble = _Bubble(self, text, on_answer, on_painted, on_dismiss, before_paint, answer_labels)
        self._layout_pet()
        if animate and not self._reduce_motion:
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
        return bubble is self.bubble and self._widget_exposed(bubble)

    def _widget_exposed(self, widget) -> bool:
        handle = self.windowHandle()
        return (
            (QGuiApplication.platformName() not in ("offscreen", "minimal")
                                       or getattr(self, "_synthetic_receipts", False))
            and bool(handle and handle.isExposed()) and widget.isVisible()
            and not self.isMinimized() and not widget.visibleRegion().isEmpty()
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
        anchor = self._temporary_anchor if self._temporary_anchor is not None else self._home_anchor
        screen = QGuiApplication.screenAt(anchor + QPoint(WIDTH // 2, CAT_HEIGHT // 2)) or self.screen()
        available = screen.availableGeometry() if screen is not None else None
        y = MARGIN
        regions = QRegion()

        bubble_height = (
            max(self.bubble.sizeHint().height(), self.bubble.heightForWidth(WIDTH)) + GAP
            if self.bubble is not None else 0
        )
        toolbar_height = self.toolbar.height() if self._expanded or self._closing_toolbar else self._expand.height()
        panel_height = 0
        panel_above = False
        if self._panel is not None:
            fixed_height = 2 * MARGIN + bubble_height + CAT_HEIGHT + GAP + toolbar_height + GAP
            max_panel_height = max(1, available.height() - fixed_height) if available is not None else None
            if isinstance(self._panel, RefineChat) and available is not None:
                below = available.bottom() + 1 - anchor.y() - CAT_HEIGHT - toolbar_height - 2 * GAP - MARGIN
                above = anchor.y() - available.top() - MARGIN - GAP - bubble_height
                panel_height, panel_above = self._panel.panel_geometry(
                    below=below, above=above, limit=max_panel_height,
                )
            else:
                limiter = getattr(self._panel, "set_height_limit", None)
                if limiter is not None and max_panel_height is not None:
                    limiter(max_panel_height)
                panel_height = max(self._panel.minimumHeight(), self._panel.sizeHint().height(), 1)
                if self._panel.hasHeightForWidth():
                    panel_height = max(panel_height, self._panel.heightForWidth(WIDTH))
                below = CAT_HEIGHT + GAP + toolbar_height + GAP + panel_height + MARGIN
                above = MARGIN + panel_height + GAP + bubble_height
                panel_above = bool(available is not None and anchor.y() + below > available.bottom() + 1
                                   and anchor.y() - above >= available.top())
            if panel_above:
                self._panel.setGeometry(MARGIN, y, WIDTH, panel_height)
                regions += QRegion(self._panel.geometry())
                y += panel_height + GAP

        if self.bubble is not None:
            self.bubble.setGeometry(MARGIN, y, WIDTH, bubble_height - GAP)
            regions += QRegion(self.bubble.geometry())
            y += bubble_height

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

        if self._panel is not None and not panel_above:
            y += GAP
            self._panel.setGeometry(MARGIN, y, WIDTH, panel_height)
            regions += QRegion(self._panel.geometry())
            y += panel_height

        size = QSize(WIDTH + 2 * MARGIN, y + MARGIN)
        x = anchor.x() - MARGIN
        y = anchor.y() - self._cat_area.top()
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
        if self._backing_store_paint and (report := getattr(self, "_report_motion_paint", None)):
            self._report_motion_paint = None
            QTimer.singleShot(0, report)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._cat_area.contains(event.position().toPoint()):
            self._drag_origin = event.globalPosition().toPoint()
            self._press = self._drag_origin - (self.pos() + self._cat_area.topLeft())
            self._moved = False
            event.accept()
        else:
            event.ignore()

    def mouseMoveEvent(self, event):
        if self._press is None or not event.buttons() & Qt.MouseButton.LeftButton:
            event.ignore()
            return
        pointer = event.globalPosition().toPoint()
        if not self._moved and (pointer - self._drag_origin).manhattanLength() < 4:
            return
        self._moved = True
        self.end_temporary_motion()
        target = pointer - self._press
        screen = QGuiApplication.screenAt(pointer) or self.screen()
        if screen is not None:
            area = screen.availableGeometry()
            compact_height = CAT_HEIGHT + GAP + self.toolbar.height() + MARGIN
            target.setX(max(area.left() + MARGIN, min(target.x(), area.right() - WIDTH - MARGIN + 1)))
            target.setY(max(area.top() + MARGIN, min(target.y(), area.bottom() - compact_height + 1)))
        self._home_anchor = target
        self._layout_pet()
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._press is not None:
            self._press = self._drag_origin = None
            if self._moved:
                self.home_moved.emit(QPoint(self._home_anchor))
            elif not self._expanded:
                self._open_toolbar()
            event.accept()
        else:
            event.ignore()

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        if self._diagnostics:
            menu.addAction("打开原联调界面", self.diagnostics_requested.emit)
        else:
            for key, label in (("work", "专注与回顾"), ("reminders", "提醒事项"),
                               ("input", "聊天"), ("settings", "设置")):
                menu.addAction(label, lambda checked=False, section=key: self.section_requested.emit(section))
        menu.addSeparator()
        menu.addAction("退出 Mochi", self.quit_requested.emit)
        menu.exec(event.globalPos())

    def activate_for_input(self) -> None:
        self.raise_()
        self.activateWindow()
        if sys.platform == "darwin" and QGuiApplication.platformName() not in ("offscreen", "minimal"):
            import AppKit
            AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
