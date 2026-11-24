from types import SimpleNamespace

from PyQt6.QtCore import QPoint, QRect, Qt

from deskpet.c_ui import macos_desktops as desktops


def test_follow_display_changes_and_disconnect(monkeypatch):
    class Screen:
        def __init__(self, bounds):
            self.bounds = bounds

        def availableGeometry(self):
            return self.bounds

    left = Screen(QRect(-1000, -300, 1000, 800))
    main = Screen(QRect(0, 0, 1920, 1080))
    screens = [left, main]
    active = [main]
    pointer = [QPoint(1700, 30)]
    monkeypatch.setattr(desktops, "QGuiApplication", SimpleNamespace(
        screens=lambda: screens, screenAt=lambda p: next((s for s in screens if s.bounds.contains(p)), None)))
    monkeypatch.setattr(desktops, "QCursor", SimpleNamespace(pos=lambda: pointer[0]))
    monkeypatch.setattr(desktops, "QApplication", SimpleNamespace(
        mouseButtons=lambda: Qt.MouseButton.NoButton, activeModalWidget=lambda: None, activePopupWidget=lambda: None))
    positions = []
    pet = SimpleNamespace(_press=None, _home_anchor=QPoint(900, 300), end_temporary_motion=lambda: None,
                          _layout_pet=lambda: None, home_moved=SimpleNamespace(emit=positions.append))
    follower = desktops.MacDesktopFollower.__new__(desktops.MacDesktopFollower)
    follower.pet, follower.width, follower.height = pet, 292, 64
    follower.pointer_screen = follower.focus_screen = main
    follower.home_area = main.bounds
    follower._focused_screen = lambda: active[0]
    pointer[0] = QPoint(-500, 80)
    follower.update()
    assert positions and left.bounds.contains(positions[-1])
    active[0] = left
    follower.update()
    active[0] = main   # keyboard switches the focused window while the pointer stays put
    follower.update()
    assert main.bounds.contains(positions[-1])
    pointer[0] = QPoint(30, 50)
    follower.update()
    pointer[0] = QPoint(-400, 90)
    follower.update()
    assert left.bounds.contains(positions[-1])
    screens.remove(left)
    follower.update()
    assert main.bounds.contains(positions[-1])
