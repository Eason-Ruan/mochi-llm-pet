"""The desktop can act on a reminder previously presented by the OS."""
from __future__ import annotations

from contextlib import closing
from datetime import timedelta
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from deskpet.c_ui.native_notifications import notification_id
from deskpet.contracts import TaskCommand
from deskpet.mocks.clock import RealClock
from deskpet.mocks.delivery import FakeDeliveryPort
from deskpet.task_service.presentation import PresentationLedger
from deskpet.task_service.sqlite import SQLiteTaskService


ROOT = Path(__file__).resolve().parents[2]
REMINDERS = (
    ("cancel-before-paint", "取消前已排队", "delivered"),
    ("popup-ack", "喝水", "delivered"),
    ("popup-snooze", "整理桌面", "delivered"),
    ("menu-snooze", "收衣服", "delivered"),
    ("menu-ack", "检查门窗", "unknown"),
)


def test_old_native_reminders_offer_quiet_actions_on_the_cat_and_in_the_bell_menu(tmp_path):
    db_path = tmp_path / "business.sqlite3"
    clock = RealClock()
    ledger = PresentationLedger(db_path, source="native")
    now = clock.wall_now()
    deadline = now - timedelta(minutes=5)
    with SQLiteTaskService(clock, FakeDeliveryPort(clock, outcome="missing"),
                           lambda *_args, **_fields: None, path=db_path) as store:
        for index, (task_id, title, state) in enumerate(REMINDERS):
            due = deadline + timedelta(seconds=5 if state == "unknown" else 0)
            registered = store.apply(TaskCommand(
                command_id=f"create:{task_id}", kind="create_registered", task_id=task_id,
                payload={"draft": {"content": title, "mode": "hard", "target_at": due,
                                   "remind_after": due, "remind_by": due}},
            ))
            assert registered.status == "applied"
            task = registered.view
            assert task.version == 1 and task.displayed_at is None and task.acknowledged_at is None
            delivery = f"delivery:{task_id}:{task.occurrence}:v{task.version}"
            assert ledger.prepare_os(task, delivery, notification_id(delivery), due - timedelta(minutes=1))
            assert ledger.update(delivery, {"submitting"}, state, due + timedelta(seconds=index))
    with closing(sqlite3.connect(db_path)) as db, db:
        assert db.execute("SELECT count(*) FROM deliveries").fetchone() == (0,)
        db.execute("DROP TABLE IF EXISTS native_followup_paints")

    for run in ("first", "restart"):
        result = subprocess.run(
            [sys.executable, "-m", "tests.m0.test_native_reminder_followup", str(tmp_path), run],
            cwd=ROOT, env=dict(os.environ, QT_QPA_PLATFORM="offscreen", MOCHI_SYSTEM2_SEMANTIC_BACKGROUND="1"),
            capture_output=True, text=True, timeout=25,
        )
        assert result.returncode == 0, f"{run}\n{result.stdout}{result.stderr}"


def exercise_desktop(data_dir, run):
    from datetime import datetime
    from time import monotonic
    from types import SimpleNamespace

    from PyQt6.QtCore import Qt, QTimer
    from PyQt6.QtGui import QAction
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication, QFrame, QLabel, QPushButton

    from deskpet.c_ui.refine import RefineSession

    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    session = RefineSession(app, SimpleNamespace(
        profile="realtime", scenario="native-followup", data_dir=data_dir,
        output=data_dir, synthetic_delivery=True, adapters=None,
    ))
    locked = run == "first"
    session.presentation.lock_probe = lambda: locked

    def wait(predicate, description):
        until = monotonic() + 6
        while not (value := predicate()) and monotonic() < until:
            QTest.qWait(20)
        assert value, (description, session.prompt, session.window.views, session.window.errors)
        return value

    def click(widget):
        assert widget.isVisible() and widget.isEnabled() and not widget.visibleRegion().isEmpty(), widget
        QTest.mouseClick(widget, Qt.MouseButton.LeftButton)

    def query(sql):
        with closing(sqlite3.connect(session.business_db)) as db:
            return db.execute(sql).fetchall()

    def paints():
        return query("SELECT task_id, occurrence FROM native_followup_paints ORDER BY task_id")

    def verify_snooze(task_id, minutes):
        snoozed = wait(lambda: session.window.views[task_id]["occurrence"] == 2
                       and session.window.views[task_id], f"{task_id} created a new occurrence")
        target = datetime.fromisoformat(snoozed["draft"]["target_at"])
        assert target - datetime.fromisoformat(snoozed["last_snoozed_at"]) == timedelta(minutes=minutes)
        assert snoozed["version"] == 2 and snoozed["acknowledged_at"] is None

    def find_bubble(title):
        bubble = session.pet.bubble
        return bubble if session.prompt and session.prompt.painted and bubble and bubble.isVisible() and any(
            label.text() == title and label.isVisible() for label in bubble.findChildren(QLabel)
        ) else None

    def menu_button(task_id, name):
        overview = session.overviews["reminders"]
        row = next((row for row in overview.findChildren(QFrame, "refine-card")
                    if row.property("task_id") == task_id and row.isVisible()), None)
        if row is None:
            return None
        button = row.findChild(QPushButton, f"refine-reminder-{name}")
        return button if button and button.isVisible() and button.isEnabled() else None

    show_bubble = session.pet.show_bubble
    cancelled_after_queue = False

    def cancel_queued_bubble(text, *args, **kwargs):
        nonlocal cancelled_after_queue
        if text == "提醒：取消前已排队" and not cancelled_after_queue:
            clock = RealClock()
            with SQLiteTaskService(clock, FakeDeliveryPort(clock, outcome="missing"),
                                   lambda *_args, **_fields: None, path=session.business_db) as writer:
                result = writer.apply(TaskCommand(command_id="cancel:queued", kind="cancel",
                                                  task_id="cancel-before-paint", expected_version=1))
            assert result.status == "applied"
            cancelled_after_queue = True
        return show_bubble(text, *args, **kwargs)

    try:
        wait(lambda: session.window.ready_for_actions and len(session.window.views) == len(REMINDERS), "desktop ready")
        click(session.pet.buttons["collapse"])
        wait(lambda: session.pet.findChild(QPushButton, "refine-menu-expand").isVisible(), "collapsed cat")
        if run == "first":
            session.pet.show_bubble = cancel_queued_bubble
            QTest.qWait(400)
            assert session.pet.bubble is None and not paints()
            locked = None
            QTest.qWait(400)
            assert session.pet.bubble is None and not paints()
            locked = False
            bubble = wait(lambda: find_bubble("提醒：喝水"), "delivered native reminder on the collapsed cat")
            assert cancelled_after_queue
            assert session.window.views["cancel-before-paint"]["status"] == "cancelled"
            assert not session.pet._expanded
            ack = bubble.findChild(QPushButton, "refine-bubble-yes")
            assert ack.text() == "确认收到"
            click(ack)
            wait(lambda: session.window.views["popup-ack"]["acknowledged_at"], "cat confirmation applied")

            bubble = wait(lambda: find_bubble("提醒：整理桌面"), "another delivered reminder on the cat")
            snooze = bubble.findChild(QPushButton, "refine-bubble-no")
            assert snooze.text() == "5 分钟后"
            click(snooze)
            verify_snooze("popup-snooze", 5)

            bubble = wait(lambda: find_bubble("提醒：收衣服"), "second delivered reminder")
            locked = True
            click(bubble.findChild(QPushButton, "refine-bubble-yes"))
            wait(lambda: session.pet.bubble is None, "action retired when the desktop locks")
            assert session.window.views["menu-snooze"]["version"] == 1
            assert session.window.views["menu-snooze"]["acknowledged_at"] is None

            locked = False
            wait(lambda: find_bubble("待确认提醒：检查门窗"), "ambiguous OS reminder without a delivery claim")
            assert session.window.views["menu-snooze"]["version"] == 1
        else:
            assert session.pet.bubble is None
            click(session.pet.findChild(QPushButton, "refine-menu-expand"))
            click(session.pet.buttons["reminders"])
            snooze = wait(lambda: menu_button("menu-snooze", "snooze"), "snooze directly in the bell menu")
            ack = wait(lambda: menu_button("menu-ack", "ack"), "confirmation directly in the bell menu")
            assert snooze.text() == "稍后提醒" and ack.text() == "确认收到"
            assert session.pet.bubble is None and not session.detail_panels["reminders"].isVisible()

            menu = snooze.menu()
            delay = menu.findChild(QAction, "refine-reminder-snooze-600")
            assert delay.text() == "10 分钟后"
            QTimer.singleShot(80, lambda: QTest.mouseClick(
                menu, Qt.MouseButton.LeftButton, pos=menu.actionGeometry(delay).center(),
            ))
            click(snooze)
            verify_snooze("menu-snooze", 10)
            assert session._respond_to_reminder("ack", "menu-snooze", 1) is None

            ack = wait(lambda: menu_button("menu-ack", "ack"), "second reminder still actionable")
            click(ack)
            wait(lambda: session.window.views["menu-ack"]["acknowledged_at"], "bell confirmation applied")
            assert session.window.views["menu-ack"]["version"] == 2
            assert session.window.views["menu-snooze"]["version"] == 2
            assert session.window.views["menu-snooze"]["acknowledged_at"] is None
            assert not menu_button("menu-ack", "ack")
            assert session.pet.bubble is None

        assert paints() == sorted((task_id, 1) for task_id, _, _ in REMINDERS if task_id != "cancel-before-paint")
        assert all(view["displayed_at"] is None for view in session.window.views.values())
        assert query("SELECT count(*) FROM deliveries") == [(0,)]
        assert query("SELECT count(*) FROM events WHERE kind = 'sound.requested'") == [(0,)]
        assert not session.window.errors
    finally:
        session.quit()
        wait(lambda: not session.window.thread.isRunning(), "desktop stopped")


if __name__ == "__main__":
    exercise_desktop(*sys.argv[1:])
