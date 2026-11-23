"""Production reminder form plus per-item and renewed-lease Qt receipts."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


def test_production_editor_keeps_absolute_times_and_reports_each_visible_item(tmp_path):
    code = r'''
from datetime import datetime, timedelta, timezone
import sys
from time import monotonic
from types import SimpleNamespace
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication
from deskpet.c_ui.refine import RefineSession
from deskpet.contracts import TaskCommand
from deskpet.mocks.clock import RealClock
from deskpet.mocks.delivery import FakeDeliveryPort
from deskpet.task_service.sqlite import SQLiteTaskService

app = QApplication([])
app.setQuitOnLastWindowClosed(False)
session = RefineSession(app, SimpleNamespace(profile="realtime", scenario="delivery-regression",
    data_dir=sys.argv[1], output=sys.argv[1], synthetic_delivery=True, adapters=None,
    user_timezone="America/New_York"))
panel, window = session.reminders, session.window

def wait(predicate):
    deadline = monotonic() + 7
    while monotonic() < deadline and not predicate():
        QTest.qWait(20)
    assert predicate(), (window.errors, window.results, session.prompt)

try:
    wait(lambda: session.context and window.ready_for_actions and "settings" in session.snapshot)
    session.pet._open_toolbar()
    session.open_section("reminders")
    from PyQt6.QtWidgets import QPushButton
    wait(session.overviews["reminders"].isVisible)
    session.overviews["reminders"].findChild(QPushButton, "refine-manage").click()
    wait(session.detail_panels["reminders"].isVisible)
    base = datetime.now(timezone.utc)
    due = (base + timedelta(hours=4)).isoformat()
    target = (base + timedelta(minutes=30)).isoformat()
    panel.item_type.setCurrentIndex(panel.item_type.findData("issue"))
    panel.mode.setCurrentIndex(panel.mode.findData("soft"))
    panel.times["target_at"].load(target, base)
    panel.times["due_at"].load(due, base)
    panel.content.setText("小组工作")
    assert panel.create_button.isEnabled()
    panel.create_button.click()
    wait(lambda: len(window.views) == 1 and not panel.pending)
    item_id, before = next(iter(window.views.items()))
    assert before["draft"]["timezone"] == window.worker.system2.host.timezone == "America/New_York"
    assert before["draft"]["remind_by"] is None
    assert before["draft"]["target_at"] and before["draft"]["due_at"]
    panel.open_edit(item_id)
    panel.content.setText("改过标题")
    QTest.qWait(250)
    panel.save_edit.click()
    wait(lambda: window.views[item_id]["version"] == 2)
    assert window.views[item_id]["draft"] == before["draft"] | {"content": "改过标题"}

    when = (datetime.now(timezone.utc) + timedelta(seconds=1.5)).isoformat()
    ids = ("first", "second", "third", "fourth")
    for task_id in ids:
        session._action("reminder.create", task_id=task_id, version=0,
                        payload={"draft": {"content": task_id, "mode": "hard", "target_at": when,
                                           "remind_after": when, "remind_by": when}})
    wait(lambda: all(task_id in window.views for task_id in ids))
    assert all(window.views[task_id]["draft"]["timezone"] == "America/New_York" for task_id in ids)
    for task_id in ids:
        session._action("reminder.confirm", task_id=task_id, version=1)
    wait(lambda: session.prompt and session.prompt.painted
         and all(window.views[item.task_id]["displayed_at"] for item in session.prompt.visible_commands()))
    prompt = session.prompt
    assert len(prompt.commands()) == 4 and len(prompt.visible_commands()) == 2
    assert "还有 2 条" in prompt.text
    visible = {item.task_id for item in prompt.visible_commands()}
    hidden = set(ids) - visible
    assert {receipt.task_id for receipt in window.receipts.values()} == visible
    assert all(window.views[task_id]["displayed_at"] is None for task_id in hidden)
    session.answer_prompt(True)
    wait(lambda: panel.tasks.count() == 4)
    selected = sorted(hidden)[0]
    from PyQt6.QtCore import Qt
    for index in range(panel.tasks.count()):
        if panel.tasks.item(index).data(Qt.ItemDataRole.UserRole) == selected:
            panel.tasks.setCurrentRow(index)
    QTest.qWait(80)
    session.panels["reminders"].ensureWidgetVisible(panel.detail_title, 10, 10)
    wait(lambda: window.views[selected]["displayed_at"] is not None)
    assert session.prompt is None, session.prompt
    assert {receipt.task_id for receipt in window.receipts.values()} == visible | {selected}
    assert all(receipt.source == "offscreen" for receipt in window.receipts.values())

    # Commit a cancellation from a separate writer after Qt built the group,
    # before the backing-store paint and before the service snapshot catches up.
    session.answer_prompt(False)
    show_bubble = session.pet.show_bubble
    cancelled_after_queue = []

    def cancel_one_before_paint(*args, **kwargs):
        ids = {part.task_id for part in session.prompt.commands()}
        if ids == {"stale", "remaining"} and not cancelled_after_queue:
            clock = RealClock()
            with SQLiteTaskService(clock, FakeDeliveryPort(clock, outcome="missing"),
                                   lambda *_args, **_fields: None, path=session.business_db) as writer:
                result = writer.apply(TaskCommand(command_id="cancel-after-qt-queued",
                    kind="cancel", task_id="stale", expected_version=2))
            assert result.status == "applied"
            cancelled_after_queue.append(True)
        return show_bubble(*args, **kwargs)

    session.pet.show_bubble = cancel_one_before_paint
    when = (datetime.now(timezone.utc) + timedelta(seconds=1.5)).isoformat()
    for task_id in ("stale", "remaining"):
        session._action("reminder.create", task_id=task_id, version=0,
                        payload={"draft": {"content": task_id, "mode": "hard", "target_at": when,
                                           "remind_after": when, "remind_by": when}})
    wait(lambda: all(task_id in window.views for task_id in ("stale", "remaining")))
    for task_id in ("stale", "remaining"):
        session._action("reminder.confirm", task_id=task_id, version=1)
    wait(lambda: window.views["remaining"]["displayed_at"])
    assert cancelled_after_queue
    assert window.views["stale"]["status"] == "cancelled" and not window.views["stale"]["displayed_at"]
    assert [part.task_id for part in session.prompt.commands()] == ["remaining"]
    assert {receipt.task_id for receipt in window.receipts.values()} == visible | {selected, "remaining"}
    assert all(row["task_id"] != "stale" for row in session.presentation.ledger.rows())
    assert not window.errors
finally:
    session.quit()
    wait(lambda: not window.thread.isRunning())
'''
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], cwd=ROOT,
                            env=dict(os.environ, QT_QPA_PLATFORM="offscreen"),
                            capture_output=True, text=True, timeout=25)
    assert result.returncode == 0, result.stdout + result.stderr


def test_hidden_inbox_renews_same_delivery_without_a_hidden_display_receipt():
    code = r'''
from datetime import datetime, timedelta, timezone
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication
from deskpet.c_ui.surface import CWorkspace
from deskpet.contracts import DisplayCommand
from deskpet.m0.ui_surface import SurfaceContext

app = QApplication([])
receipts = []
context = SurfaceContext(submit=lambda action: None, can_report=lambda: True,
    report=lambda delivery_id, outcome, error: receipts.append(delivery_id) or True)
workspace = CWorkspace(context=context)
workspace.state_panel.render_snapshot = lambda snapshot: None
start = datetime(2026, 1, 1, tzinfo=timezone.utc)

def snapshot(now):
    drafts = [{"task_id": task_id, "version": 2, "status": "registered", "displayed_at": None,
               "draft": {"content": task_id, "mode": "hard", "earliest_at": start.isoformat(),
                         "latest_first_delivery_at": start.isoformat()}} for task_id in ("first", "second")]
    return {"now": now.isoformat(), "tasks": drafts, "sessions": []}

def command(task_id):
    return DisplayCommand(delivery_id=task_id, task_id=task_id, task_version=2, occurrence=1,
                          content=task_id, requested_at=start, expires_at=start + timedelta(seconds=30), reason="hard_due")

workspace.render_snapshot(snapshot(start))
workspace.show()
workspace.show_reminder(command("first"))
workspace.show_reminder(command("second"))
QTest.qWait(120)
assert receipts == ["first"], receipts
new_time = start + timedelta(seconds=31)
workspace.render_snapshot(snapshot(new_time))
workspace.show_reminder(command("second").model_copy(update={"expires_at": new_time + timedelta(seconds=30)}))
assert receipts == ["first"], receipts
workspace.inbox.selector.setCurrentIndex(workspace.inbox.selector.findData("second"))
QTest.qWait(120)
assert receipts == ["first", "second"], receipts
'''
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                            env=dict(os.environ, QT_QPA_PLATFORM="offscreen"),
                            capture_output=True, text=True, timeout=12)
    assert result.returncode == 0, result.stdout + result.stderr
