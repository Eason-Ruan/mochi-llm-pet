"""Persistent reminder timing and opportunity ownership regressions."""
from datetime import timedelta

from deskpet.contracts import TaskDraft
from deskpet.mocks.clock import FakeClock
from deskpet.task_service.sqlite import SQLiteTaskService
from tests.m0.test_task_service import DisplayProbe, command, observe


def _register(tasks, task_id, **fields):
    draft = TaskDraft(content=task_id, **fields)
    assert command(tasks, "create", task_id=task_id, draft=draft.model_dump(mode="json")).status == "applied"
    return command(tasks, "confirm", task_id=task_id).view


def test_target_obeys_optional_semantic_window_and_does_not_invent_a_deadline(tmp_path):
    clock = FakeClock()
    display = DisplayProbe(clock)
    with SQLiteTaskService(clock, display, lambda *args, **fields: None, path=tmp_path / "tasks.sqlite3",
                           enforce_opportunities=True) as tasks:
        target = clock.wall_now() + timedelta(minutes=8)
        issue_due = target + timedelta(hours=6)
        item = _register(tasks, "target", item_type="issue", target_at=target, due_at=issue_due)
        assert item.draft.opens_at == target
        window = TaskDraft(content="use an early window", target_at=target,
                           window_open_at=target - timedelta(seconds=45), window_close_at=target + timedelta(seconds=30),
                           window_computed_at=clock.wall_now())
        assert window.opens_at == target - timedelta(seconds=45)
        assert item.draft.hard_deadline is None
        observe(tasks, clock, "working", valid_until=target + timedelta(hours=1))
        clock.advance(9 * 60)
        tasks.pump()
        assert not display.commands
        observe(tasks, clock, "available", revision=2, valid_until=clock.wall_now() + timedelta(seconds=30))
        tasks.pump()
        assert [item.reason for item in display.commands] == ["opportunity"]

        updated = command(tasks, "snooze", task_id=item.task_id, seconds=300, mode="flexible_target").view
        assert updated.draft.target_at == clock.wall_now() + timedelta(minutes=5)
        assert updated.draft.hard_deadline is None
        assert updated.draft.due_at == issue_due
        assert updated.occurrence == item.occurrence + 1


def test_one_fresh_available_window_across_restart_and_expired_deadline(tmp_path):
    clock = FakeClock()
    display = DisplayProbe(clock)
    path = tmp_path / "tasks.sqlite3"
    until = clock.wall_now() + timedelta(seconds=30)
    with SQLiteTaskService(clock, display, lambda *args, **fields: None, path=path,
                           enforce_opportunities=True) as tasks:
        for task_id in ("first", "second", "third"):
            _register(tasks, task_id, mode="soft")
        due = clock.wall_now() + timedelta(seconds=1)
        _register(tasks, "expired", mode="hard", remind_after=due, remind_by=due, expires_at=due + timedelta(seconds=1))
        observe(tasks, clock, "available", valid_until=until)
        tasks.pump()
        assert len(display.commands) == 1
    clock.advance(3)
    with SQLiteTaskService(clock, display, lambda *args, **fields: None, path=path,
                           enforce_opportunities=True) as tasks:
        observe(tasks, clock, "available", revision=2, valid_until=until)
        tasks.pump()
        assert len(display.commands) == 1
        observe(tasks, clock, "working", revision=3, valid_until=until)
        observe(tasks, clock, "available", revision=4, valid_until=until)
        tasks.pump()
        assert len(display.commands) == 2
        assert all(item.task_id != "expired" for item in display.commands)
