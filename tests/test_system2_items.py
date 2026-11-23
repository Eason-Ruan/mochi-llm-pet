from contextlib import closing
from datetime import timedelta
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest

from deskpet.contracts import TaskCommand, TaskDraft
from deskpet.mocks.clock import FakeClock
from deskpet.system2.items import ItemService
from deskpet.task_service.presentation import PresentationLedger
from deskpet.task_service.sqlite import SQLiteTaskService
from deskpet.work_sessions import PersistentWorkSessionService, PhaseCommand
from tests.m0.test_task_service import DisplayProbe, observe


def _items(path, clock, display):
    return SQLiteTaskService(clock, display, lambda *args, **kwargs: None, path=path,
                             enforce_opportunities=True, auto_opportunity=False)


def _write(items, function, key, version=0, **args):
    return items.execute(function, args, command_id=key, expected_version=version)


def test_manual_snooze_commits_admission_fence_for_original_occurrence_once(tmp_path):
    from deskpet.c_ui.desktop_system2 import DesktopSystem2

    clock = FakeClock()
    path = tmp_path / "business.sqlite3"
    with _items(path, clock, DisplayProbe(clock)) as tasks:
        item = _write(ItemService(tasks), "item.create", "create", type="reminder", title="喝水",
                      reminder_rule={"target_at": clock.wall_now() + timedelta(minutes=5)})["view"]
        command = TaskCommand(command_id="manual-snooze", kind="snooze", task_id=item["id"],
                              expected_version=item["version"], payload={"seconds": 60, "mode": "exact"})

        def fail_after_write(db, function, args, event_id):
            assert db.in_transaction and function == "reminder.snooze"
            assert args == {"occurrence_id": item["occurrence"]["id"]}
            assert event_id == command.command_id
            assert tasks.get(item["id"], db=db).occurrence_id != args["occurrence_id"]
            raise RuntimeError("abort before commit")

        desktop = SimpleNamespace(host=SimpleNamespace(fence_local=fail_after_write))
        def fence(db, applied, old_view):
            DesktopSystem2.fence_task(desktop, db, applied, old_view)

        with pytest.raises(RuntimeError, match="abort before commit"):
            tasks.apply(command, on_applied=fence)
        assert tasks.get(item["id"]).occurrence_id == item["occurrence"]["id"]
        assert tasks.command_result(command.command_id) is None
        calls = []
        desktop.host.fence_local = lambda db, fn, args, event_id: calls.append((fn, args, event_id))
        assert tasks.apply(command, on_applied=fence).status == "applied"
        assert tasks.apply(command, on_applied=fence).status == "duplicate"
        assert calls == [("reminder.snooze", {"occurrence_id": item["occurrence"]["id"]}, "manual-snooze")]


def test_item_edit_reuses_the_semantic_window_and_keeps_hard_deadlines_independent(tmp_path):
    clock = FakeClock()
    display = DisplayProbe(clock)
    path = tmp_path / "business.sqlite3"
    target = clock.wall_now() + timedelta(minutes=30)
    opens = target - timedelta(minutes=12)
    closes = target + timedelta(minutes=3)
    with _items(path, clock, display) as tasks:
        items = ItemService(tasks)
        first = _write(items, "item.create", "create", type="reminder", title="喝水", reminder_rule={
            "timezone": "Asia/Shanghai", "target_at": target, "window_open_at": opens, "window_close_at": closes,
        })["view"]
        clock.advance(45)
        # The same versioned edit the Refine form sends after leaving the editor open.
        task = tasks.get(first["id"])
        amended = TaskDraft.model_validate(task.draft.model_dump() | {"content": "喝温水", "description": "厨房"})
        tasks.apply(TaskCommand(command_id="ui-edit", kind="edit", task_id=task.task_id,
                                expected_version=task.version, payload={"draft": amended.model_dump(mode="json")}))
        edited = items.get(first["id"])
        assert edited["title"] == "喝温水"
        assert edited["rule"] == first["rule"]
        assert edited["occurrence"]["id"] == first["occurrence"]["id"]
        assert tasks.get(first["id"]).draft.opens_at == opens
        assert tasks.get(first["id"]).draft.hard_deadline is None
        clock.advance((closes - clock.wall_now()).total_seconds())
        tasks.pump()
        assert not items.eligible() and not display.commands
        assert items.get(first["id"])["occurrence"]["status"] == "pending"
        exact = clock.wall_now() + timedelta(minutes=1)
        updated = _write(items, "reminder.configure", "exact", edited["version"], item_id=edited["id"],
                         rule={"remind_after": exact, "remind_by": exact})["view"]
        assert updated["occurrence"]["id"] != first["occurrence"]["id"]
        assert items.get_occurrence(first["occurrence"]["id"])["status"] == "superseded"
    clock.advance(60)
    with _items(path, clock, display) as tasks:
        tasks.pump()
        assert [item.reason for item in display.commands] == ["hard_due"]


def test_snooze_modes_rotate_the_occurrence_and_keep_issue_due_at(tmp_path):
    clock = FakeClock()
    display = DisplayProbe(clock)
    deadline = clock.wall_now() + timedelta(seconds=120)
    due = deadline + timedelta(hours=3)
    with _items(tmp_path / "business.sqlite3", clock, display) as tasks:
        items = ItemService(tasks)
        issue = _write(items, "item.create", "issue", type="issue", title="交报告", due_at=due,
                       reminder_rule={"remind_by": deadline})["view"]
        snooze = {"occurrence_id": issue["occurrence"]["id"], "delay_s": 300, "mode": "flexible_target",
                  "window_before_s": 20, "window_after_s": 10}
        moved = _write(items, "reminder.snooze", "flexible", issue["version"], **snooze)["view"]
        assert _write(items, "reminder.snooze", "flexible", issue["version"], **snooze)["status"] == "duplicate"
        assert _write(items, "reminder.ack", "old-card", issue["version"],
                      occurrence_id=issue["occurrence"]["id"])["error_code"] == "STALE_TASK"
        assert moved["due_at"] == issue["due_at"]
        timing = tasks.get(issue["id"]).draft
        assert timing.target_at == clock.wall_now() + timedelta(minutes=5)
        assert timing.opens_at == timing.target_at - timedelta(seconds=20)
        assert timing.hard_deadline is None
        assert not items.eligible()
        clock.advance(120)
        tasks.pump()
        assert not display.commands
        next_check = _write(items, "reminder.snooze", "no-earlier", moved["version"],
                            occurrence_id=moved["occurrence"]["id"], delay_s=300, mode="no_earlier")["view"]
        timing = tasks.get(issue["id"]).draft
        assert timing.remind_after == clock.wall_now() + timedelta(minutes=5)
        assert timing.target_at is None and timing.hard_deadline is None
        exact = _write(items, "reminder.snooze", "exact", next_check["version"],
                       occurrence_id=next_check["occurrence"]["id"], delay_s=300, mode="exact")["view"]
        timing = tasks.get(issue["id"]).draft
        assert timing.remind_after == timing.hard_deadline == clock.wall_now() + timedelta(minutes=5)
        assert exact["due_at"] == issue["due_at"]
        clock.advance(300)
        tasks.pump()
        assert [command.occurrence for command in display.commands] == [exact["occurrence"]["ordinal"]]
        received = _write(items, "reminder.ack", "okay", exact["version"],
                          occurrence_id=exact["occurrence"]["id"])["view"]
        assert received["status"] == "created"
        assert received["occurrence"]["status"] == "acknowledged"


def test_focus_intervals_move_issue_state_and_preserve_the_work_total(tmp_path):
    clock = FakeClock()
    path = tmp_path / "business.sqlite3"
    display = DisplayProbe(clock)
    with _items(path, clock, display) as tasks, closing(PersistentWorkSessionService(path=path, clock=clock)) as phases:
        items = ItemService(tasks)
        tasks.on_terminal_issue = phases.unlink_completed_issue
        a, b, c = [(_write(items, "item.create", name, type="issue", title=name)["view"]["id"])
                    for name in ("A", "B", "C")]
        assert phases.apply_phase(PhaseCommand(command_id="start", kind="start", phase_id="phase", payload={
            "planned_duration_s": 2400, "issue_links": [a, b], "primary_issue_id": a,
        })).status == "applied"
        assert items.get(a)["status"] == "in_progress"
        clock.advance(1500)
        phase = phases.apply_phase(PhaseCommand(command_id="switch", kind="set_links", phase_id="phase",
            expected_version=1, payload={"issue_links": [a, b], "primary_issue_id": b})).view
        assert (items.get(a)["status"], items.get(a)["actual_focus_s"]) == ("paused", 1500)
        assert items.get(b)["status"] == "in_progress"
        clock.advance(900)
        elapsed = phases.current_phase()
        assert elapsed.actual_focus_s == 2400
        assert phase.begin_at == elapsed.begin_at
        assert (items.get(b)["status"], items.get(b)["actual_focus_s"]) == ("paused", 900)
        intervals = phases.list_focus_intervals()
        former_b = intervals[1]
        repaired = PhaseCommand(command_id="reassign", kind="reassign_interval", phase_id="phase",
            expected_version=elapsed.version, payload={"interval_id": former_b.id,
                "expected_interval_version": former_b.version, "target_issue_id": c})
        assert phases.apply_phase(repaired).status == "applied"
        assert phases.apply_phase(repaired).status == "duplicate"
        assert [items.get(issue)["actual_focus_s"] for issue in (a, b, c)] == [1500, 0, 900]
        assert items.get(c)["status"] == "created"
        assert len(phases.list_focus_intervals()) == 2


def test_terminal_issue_closes_the_live_link_waiter_and_os_intent_atomically(tmp_path):
    clock = FakeClock()
    path = tmp_path / "business.sqlite3"
    display = DisplayProbe(clock)
    with _items(path, clock, display) as tasks, closing(PersistentWorkSessionService(path=path, clock=clock)) as phases:
        items = ItemService(tasks)
        tasks.on_terminal_issue = phases.unlink_completed_issue
        seen = []
        tasks.on_occurrence_invalidated = lambda item_id, ordinal, *, db: seen.append(
            (item_id, ordinal, db.in_transaction))
        due = clock.wall_now() + timedelta(seconds=60)
        rule = {"remind_after": due, "remind_by": due}
        issue = _write(items, "item.create", "issue", type="issue", title="工作", reminder_rule=rule)["view"]
        independent = _write(items, "item.create", "independent", type="reminder", title="喝水", reminder_rule=rule)["view"]
        issue_id = issue["id"]
        phase = phases.apply_phase(PhaseCommand(command_id="start", kind="start", phase_id="phase", payload={
            "issue_links": [issue_id], "primary_issue_id": issue_id,
        })).view
        task = tasks.get(issue_id)
        ledger = PresentationLedger(path)
        assert ledger.prepare_os(task, "os-delivery", "platform-id", clock.wall_now())
        assert ledger.update("os-delivery", {"submitting"}, "scheduled", clock.wall_now())
        clock.advance(15)
        db = sqlite3.connect(path, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO rechecks VALUES (?,?,?,?)", (issue_id, task.version, due.isoformat(), "waiter"))
        completed = items.execute("item.set_status", {"id": issue_id, "status": "completed"},
                                  command_id="complete", expected_version=task.version, db=db)["view"]
        assert completed["status"] == "completed" and completed["actual_focus_s"] == 15
        assert db.execute("SELECT state FROM presentations WHERE delivery_id='os-delivery'").fetchone()[0] == "cancel_intent"
        assert db.execute("SELECT 1 FROM rechecks").fetchone() is None
        assert seen == [(issue_id, task.occurrence, True)]
        db.commit()
        db.close()
        items.flush()
        assert phases.get_phase(phase.id).primary_issue_id is None
        clock.advance(45)
        tasks.pump()
        assert [command.task_id for command in display.commands] == [independent["id"]]
        assert items.get(issue_id)["actual_focus_s"] == 15
        reopened = _write(items, "item.set_status", "reopen", completed["version"],
                          id=issue_id, status="created")["view"]
        assert reopened["rule"] is None and reopened["actual_focus_s"] == 15
        assert items.get_occurrence(issue["occurrence"]["id"])["status"] == "cancelled"


def test_refine_editor_commits_content_patch_and_explicit_exact_change(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    from deskpet.c_ui.reminder import ReminderPanel
    from deskpet.m0.gui import QtDeliveryBridge, ServiceWorker

    app = QApplication.instance() or QApplication([])
    assert app is not None
    path = tmp_path / "business.sqlite3"
    worker = ServiceWorker(SimpleNamespace(profile="scenario", scenario="item-editor", output=None,
                                           business_database=path), QtDeliveryBridge())
    actions = []
    errors = []
    worker.error.connect(errors.append)

    def submit(action_id, *, task_id, version, payload):
        event_id = uuid4().hex
        actions.append({"kind": "ui", "action": {
            "event_id": event_id, "action_id": action_id, "task_id": task_id,
            "expected_task_version": version, "surface_revision": version, "payload": payload,
        }})
        return event_id

    panel = ReminderPanel(submit=submit, advanced=True)
    worker.snapshot_ready.connect(panel.render_snapshot)
    worker.result_ready.connect(panel.receive_result)
    worker.start()
    try:
        assert not errors
        items = ItemService(worker.env.tasks)
        target = worker.env.clock.wall_now() + timedelta(minutes=30)
        model = _write(items, "item.create", "chat-create", type="issue", title="带耳机", reminder_rule={
            "target_at": target, "window_open_at": target - timedelta(minutes=10),
            "window_close_at": target + timedelta(minutes=3),
        })["view"]
        worker.snapshot()
        assert "找空档提醒" in panel.details.text()
        panel.open_edit(model["id"])
        worker.env.clock.advance(45)
        panel.content.setText("带会议耳机")
        panel.save_edit.click()
        worker.act(actions.pop())
        edited = items.get(model["id"])
        assert edited["title"] == "带会议耳机"
        assert edited["rule"] == model["rule"]
        assert edited["occurrence"]["id"] == model["occurrence"]["id"]
        panel.open_edit(model["id"])
        panel.mode.setCurrentIndex(panel.mode.findData("hard"))
        panel.save_edit.click()
        worker.act(actions.pop())
        exact = items.get(model["id"])
        timing = worker.env.tasks.get(model["id"]).draft
        assert timing.remind_after == timing.hard_deadline == target
        assert exact["occurrence"]["id"] != model["occurrence"]["id"]
        panel.buttons["ack"].click()
        worker.act(actions.pop())
        acknowledged = items.get(model["id"])
        assert acknowledged["status"] == "created"
        assert acknowledged["occurrence"]["status"] == "acknowledged"
        assert not errors
    finally:
        worker.timer.stop()
        worker.env.sessions.close()
        worker.env.tasks.close()
        panel.close()
