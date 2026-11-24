"""Place the one native pet window on the user's display and its active Space."""
from __future__ import annotations

from PyQt6.QtCore import QPoint, QTimer, Qt
from PyQt6.QtGui import QCursor, QGuiApplication
from PyQt6.QtWidgets import QApplication


def relative_anchor(point, source, target, width, height):
    def offset(value, prior, extent):
        return round(min(1, max(0, value / max(1, prior))) * max(0, extent))

    return target.topLeft() + QPoint(
        offset(point.x() - source.left(), source.width() - width, target.width() - width),
        offset(point.y() - source.top(), source.height() - height, target.height() - height),
    )


class MacDesktopFollower:
    def __init__(self, pet, width, height):
        import AppKit
        import objc
        import Quartz

        self.pet = pet
        self.width, self.height = width, height
        self.quartz = Quartz
        self.workspace = AppKit.NSWorkspace.sharedWorkspace()
        window = objc.objc_object(c_void_p=int(pet.winId())).window()
        behavior = window.collectionBehavior()
        incompatible = (AppKit.NSWindowCollectionBehaviorMoveToActiveSpace
                        | AppKit.NSWindowCollectionBehaviorFullScreenPrimary
                        | AppKit.NSWindowCollectionBehaviorFullScreenNone)
        window.setCollectionBehavior_((behavior & ~incompatible)
                                      | AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
                                      | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary)
        self.pointer_screen = self.focus_screen = None
        self.home_area = pet.screen().availableGeometry()
        self.timer = QTimer(pet)
        self.timer.setInterval(750)
        self.timer.timeout.connect(self.update)
        self.timer.start()
        self.update()

    def _focused_screen(self):
        app = self.workspace.frontmostApplication()
        if app is None:
            return None
        q = self.quartz
        options = q.kCGWindowListOptionOnScreenOnly | q.kCGWindowListExcludeDesktopElements
        for window in q.CGWindowListCopyWindowInfo(options, q.kCGNullWindowID) or ():
            if window.get(q.kCGWindowOwnerPID) != app.processIdentifier() or window.get(q.kCGWindowLayer) != 0:
                continue
            rect = window[q.kCGWindowBounds]
            if rect['Width'] < 2 or rect['Height'] < 2:
                continue
            return QGuiApplication.screenAt(QPoint(round(rect['X'] + rect['Width'] / 2),
                                                   round(rect['Y'] + rect['Height'] / 2)))
        return None

    def update(self):
        pet = self.pet
        if (pet._press is not None or QApplication.mouseButtons() != Qt.MouseButton.NoButton
                or QApplication.activeModalWidget() or QApplication.activePopupWidget()):
            return
        screens = QGuiApplication.screens()
        if not screens:
            return
        pointer = QGuiApplication.screenAt(QCursor.pos())
        focused = self._focused_screen() if len(screens) > 1 else None
        target = None
        if focused and focused != self.focus_screen:
            target = focused
        elif pointer and pointer != self.pointer_screen:
            target = pointer
        self.focus_screen, self.pointer_screen = focused, pointer
        here = QGuiApplication.screenAt(pet._home_anchor + QPoint(self.width // 2, self.height // 2))
        if here:
            self.home_area = here.availableGeometry()
        target = target or here or pointer or screens[0]
        if here == target:
            return
        pet._home_anchor = relative_anchor(pet._home_anchor, self.home_area, target.availableGeometry(),
                                           self.width, self.height)
        self.home_area = target.availableGeometry()
        pet.end_temporary_motion()
        pet._layout_pet()
        pet.home_moved.emit(QPoint(pet._home_anchor))
