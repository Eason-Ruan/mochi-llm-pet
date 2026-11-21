"""Behavioral checks for C's independently injectable session adapter."""
from datetime import timedelta

import pytest

from deskpet.contracts import ErrorCode, SessionCommand
from deskpet.mocks.clock import FakeClock
from deskpet.work_sessions.service import CWorkSessionService, factory


def command(key, kind, version=0, payload=None, session_id="work"):
    return SessionCommand(command_id=key, kind=kind, session_id=session_id,
                          expected_version=version, payload=payload or {})


@pytest.fixture
def env():
    clock = FakeClock()
    events = []
    service = factory(clock=clock, emit=lambda kind, **fields: events.append((kind, fields)))
    return clock, service, events


def test_pause_resume_review_and_read_versions(env):
    clock, service, events = env
    assert isinstance(service, CWorkSessionService)
    result = service.apply(command("start", "start", payload={"goal": "  Read  ", "duration_s": 60, "task_id": "paper"}))
    assert result.view.goal == "Read" and result.view.task_id == "paper"
    clock.advance(20)
    assert service.get("work").remaining_s == 40 and service.get("work").version == 1
    assert service.apply(command("pause", "pause", 1)).view.state == "paused"
    clock.advance(500)
    assert service.list_sessions()[0].remaining_s == 40
    assert service.get("work").phase_due_at is None
    result = service.apply(command("resume", "resume", 2))
    assert result.view.phase_due_at == clock.wall_now() + timedelta(seconds=40)
    clock.advance(10)
    result = service.apply(command("end", "end", 3, {"confirmed_outcomes": ["Read introduction"], "next_steps": ["Read methods"]}))
    assert result.view.state == "ended" and result.view.version == 4
    assert result.view.remaining_s == 30 and result.view.phase_due_at is None
    assert result.view.confirmed_outcomes == ("Read introduction",)
    assert result.view.next_steps == ("Read methods",)
    clock.advance(1000)
    assert service.get("work") == result.view
    assert [kind for kind, _ in events] == ["session.applied"] * 4


def test_exact_expiry_rejects_old_pause_and_emits_once(env):
    clock, service, events = env
    service.apply(command("start", "start", payload={"goal": "Read", "duration_s": 5}))
    clock.advance(5)
    rejected = service.apply(command("pause", "pause", 1))
    assert rejected.error_code == ErrorCode.STALE_TASK
    assert rejected.view.state == "break_due" and rejected.view.version == 2
    assert service.apply(command("pause-new", "pause", 2)).error_code == ErrorCode.INVALID_TRANSITION
    service.pump()
    service.list_sessions()
    assert [kind for kind, _ in events].count("session.break_due") == 1
    ended = service.apply(command("end", "end", 2))
    assert ended.view.version == 3 and ended.view.remaining_s == 0
    assert ended.view.confirmed_outcomes == ended.view.next_steps == ()
    assert service.apply(command("end-again", "end", 3)).error_code == ErrorCode.INVALID_TRANSITION


def test_command_retries_collisions_and_stale_writes(env):
    clock, service, _ = env
    start = command("start", "start", payload={"goal": "Read", "duration_s": 60})
    service.apply(start)
    reordered = command("start", "start", payload={"duration_s": 60, "goal": "Read"})
    assert service.apply(reordered).status == "duplicate"
    assert service.apply(command("start", "start", payload={"goal": "Other"})).error_code == ErrorCode.INVALID_ARGUMENT
    assert service.apply(command("stale", "pause", 0)).error_code == ErrorCode.STALE_TASK
    assert service.apply(command("resume", "resume", 1)).error_code == ErrorCode.INVALID_TRANSITION
    pause = command("pause", "pause", 1)
    service.apply(pause)
    clock.advance(100)
    assert service.apply(pause).status == "duplicate"
    assert service.get("work").state == "paused" and service.get("work").version == 2


@pytest.mark.parametrize("payload", [
    {"goal": "  "}, {"goal": "x" * 501}, {"goal": "x", "duration_s": 0},
    {"goal": "x", "duration_s": -1}, {"goal": "x", "duration_s": float("nan")},
    {"goal": "x", "duration_s": float("inf")}, {"goal": "x", "duration_s": 1e300},
    {"goal": "x", "unexpected": True},
])
def test_invalid_start_has_no_task_or_schedule_side_effect(env, payload):
    clock, service, events = env
    result = service.apply(command("invalid", "start", payload=payload))
    assert result.error_code == ErrorCode.INVALID_ARGUMENT
    clock.advance(100)
    assert service.list_sessions() == []
    assert not any(kind == "session.break_due" for kind, _ in events)


def test_invalid_review_and_payloads_leave_current_record(env):
    _, service, _ = env
    service.apply(command("start", "start", payload={"goal": "Read"}))
    before = service.get("work")
    for key, kind, payload in (("bad-review", "end", {"confirmed_outcomes": "AI claims success"}),
                               ("bad-item", "end", {"next_steps": [123]}),
                               ("bad-pause", "pause", {"extra": True})):
        assert service.apply(command(key, kind, 1, payload)).error_code == ErrorCode.INVALID_ARGUMENT
        assert service.get("work") == before
    assert service.apply(command("missing", "end", session_id="missing")).error_code == ErrorCode.NOT_FOUND


def test_monotonic_time_survives_wall_clock_jump():
    class AdjustableClock(FakeClock):
        offset = timedelta()
        def wall_now(self):
            return super().wall_now() + self.offset
    clock = AdjustableClock()
    service = factory(clock=clock, emit=lambda *a, **kw: None)
    service.apply(command("start", "start", payload={"goal": "Read", "duration_s": 60}))
    clock.offset = timedelta(hours=2)
    clock.advance(15)
    assert service.get("work").remaining_s == 45
    service.apply(command("pause", "pause", 1))
    clock.offset = timedelta(hours=-2)
    service.apply(command("resume", "resume", 2))
    assert service.get("work").phase_due_at == clock.wall_now() + timedelta(seconds=45)
    clock.advance(45)
    assert service.get("work").state == "break_due"


def test_adapter_replaces_service_without_loading_qt():
    from deskpet.m0.composition import build_environment
    env = build_environment(adapters="examples/m0/c-product.adapters.json")
    assert isinstance(env.sessions, CWorkSessionService)
