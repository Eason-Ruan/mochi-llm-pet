"""Exercise semantic phase and power wakes through the production desktop composition."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
from threading import Event
from types import SimpleNamespace

import pytest

from deskpet.c_ui.desktop_system2 import DesktopSystem2
from deskpet.contracts import TaskCommand
from deskpet.mocks.clock import FakeClock
from deskpet.mocks.delivery import FakeDeliveryPort
from deskpet.observation_v2.activity import Activity, ActivitySample
from deskpet.observation_v2.context import ObservationContextReader
from deskpet.observation_v2.pipeline import ObservationStore
from deskpet.observation_v2.state import Observation
from deskpet.settings import HabitBehavior, SettingsStore
from deskpet.system2.host import System2Host
from deskpet.system2.protocol import EngineReply
from deskpet.system2.scheduler import Scheduler, SchedulerError
from deskpet.task_service.sqlite import SQLiteTaskService
from deskpet.work_sessions import PersistentWorkSessionService, PhaseCommand


class RecordingEngine:
    def __init__(self):
        self.turns = []
        self.action = None

    def run(self, request, invoke):
        turn = json.loads(request.input_json)
        self.turns.append(turn)
        if self.action is not None:
            self.action(turn, invoke)
        return EngineReply("")

    def close(self):
        pass


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    monkeypatch.setenv("MOCHI_SYSTEM2_SEMANTIC_BACKGROUND", "1")
    monkeypatch.setattr(System2Host, "start", lambda self: None)
    path = tmp_path / "business.sqlite3"
    clock = FakeClock()
    delivery = FakeDeliveryPort(clock, outcome="missing")
    tasks = SQLiteTaskService(clock, delivery, lambda *_args, **_kwargs: None, path=path)
    settings = SettingsStore(path)
    settings.update(expected_version=settings.read().version, auto_adjust=True)
    phases = PersistentWorkSessionService(path=path, clock=clock, settings_store=settings)
    engine = RecordingEngine()
    with ObservationStore(tmp_path / "observation.sqlite3", live=True, boot_id="phase-test") as store:
        store.begin_live_run(clock.wall_now(), monotonic_s=clock.monotonic_now())
        observation = ObservationContextReader(tmp_path / "observation.sqlite3", boot_id="phase-test")
        env = SimpleNamespace(clock=clock, sessions=phases, tasks=tasks, observation_context_reader=observation)
        args = SimpleNamespace(
            business_database=path, data_dir=tmp_path, desktop_lock_probe=lambda: False,
            desktop_display_capabilities={"pet_badge": True}, system2_engine=engine,
        )
        runtime = DesktopSystem2(args, env, settings)
        try:
            yield runtime, env, engine, store
        finally:
            runtime.close()
            phases.close()
            tasks.close()


def start(env, phase_id, **payload):
    result = env.sessions.apply_phase(PhaseCommand(
        command_id=f"start:{phase_id}", phase_id=phase_id, kind="start", payload=payload,
    ))
    assert result.status == "applied"
    return result.view


def test_natural_focus_and_break_include_confirmed_habit_successors_once(desktop):
    runtime, env, engine, _ = desktop
    habit = runtime.settings.save_habit(title="论文", expected_version=0, behavior=HabitBehavior(
        focus_s=20, short_break_s=10, auto_start_break=True, auto_start_focus=True,
    ))
    focus = start(env, "focus", habit_id=habit.id, expected_habit_version=habit.version)
    offers = []

    def offer_same_successor(turn, invoke):
        successor = turn["event"]["phase_elapsed"][0]["successor"]
        offers.append(invoke("proposal.offer", "duplicate-successor", {
            "function": "phase.start", "args": {"kind": successor["kind"]},
            "display_text": "开启下一段？",
        }))

    engine.action = offer_same_successor
    env.clock.advance(20)
    env.sessions.pump()
    rest = env.sessions.current_phase()
    assert rest.kind == "short_break"
    assert runtime.host.pump()
    turn, = engine.turns
    elapsed, = turn["event"]["phase_elapsed"]
    assert elapsed == {
        "phase_id": focus.id, "phase_version": focus.version + 1, "phase_kind": focus.kind,
        "transition_reason": "natural_elapsed", "elapsed_at": env.clock.wall_now().isoformat(timespec="microseconds"),
        "successor": {"id": rest.id, "version": rest.version, "kind": rest.kind},
    }
    assert turn["context"]["current_phase"]["id"] == rest.id

    env.clock.advance(10)
    env.sessions.pump()
    next_focus = env.sessions.current_phase()
    assert (next_focus.kind, next_focus.source) == ("focus", "habit")
    assert runtime.host.pump()
    assert len(engine.turns) == 2
    elapsed, = engine.turns[-1]["event"]["phase_elapsed"]
    assert elapsed["phase_id"] == rest.id
    assert elapsed["successor"]["id"] == next_focus.id
    assert [offer["error_code"] for offer in offers] == ["INVALID_TRANSITION", "INVALID_TRANSITION"]
    assert not runtime.host.pending_proposals()
    env.sessions.pump()
    runtime.scheduler.poll()
    assert not runtime.host.pump()


@pytest.mark.parametrize("named", [False, True])
def test_proposal_can_continue_only_the_authoritative_elapsed_phase(desktop, named):
    runtime, env, engine, _ = desktop
    if named:
        habit = runtime.settings.save_habit(title="论文", expected_version=0, behavior=HabitBehavior(
            focus_s=20, short_break_s=10, auto_start_break=True, auto_start_focus=False,
        ))
        start(env, "focus", habit_id=habit.id, expected_habit_version=habit.version)
        env.clock.advance(20)
        previous = env.sessions.current_phase()
        assert previous.kind == "short_break"
        # Consume the earlier focus wake with the successful automatic break.
        assert runtime.host.pump()
        next_kind = "focus"
    else:
        previous = start(env, "focus", planned_duration_s=20)
        next_kind = "short_break"
    env.clock.advance(previous.planned_duration_s)
    env.sessions.pump()
    outcomes = []

    def offer(turn, invoke):
        current = turn["context"]["phase_predecessor"]
        assert current == {"id": previous.id, "version": previous.version + 1, "kind": previous.kind}
        args = {"kind": next_kind, "previous_phase_id": current["id"],
                "expected_previous_version": current["version"], "expected_version": 0}
        outcomes.append(invoke("proposal.offer", "unknown", {
            "function": "phase.start", "args": {**args, "previous_phase_id": "foreign"},
            "display_text": "开启下一段？",
        }))
        outcomes.append(invoke("proposal.offer", "continue", {
            "function": "phase.start", "args": args, "display_text": "开启下一段？",
        }))

    engine.action = offer
    assert runtime.host.pump()
    assert outcomes[0]["error_code"] == "NOT_AUTHORIZED"
    assert outcomes[1]["status"] == "pending"
    proposal, = runtime.host.pending_proposals()
    assert proposal["expected_versions"] == {f"phase:{previous.id}": previous.version + 1}
    runtime.host.resolve_proposal(proposal["id"], "accept", version=proposal["version"], event_id="accepted")
    assert runtime.host.pump()
    active = env.sessions.current_phase()
    assert active.kind == next_kind and active.source == "proposal"
    assert active.habit_run_id == previous.habit_run_id
    assert not runtime.host.pending_proposals()


def test_resume_merges_elapsed_break_and_soft_recheck_after_restoring_timers(desktop):
    runtime, env, engine, _ = desktop
    rest = start(env, "break", kind="short_break", planned_duration_s=20)
    due = env.clock.wall_now() + timedelta(seconds=20)
    result = env.tasks.apply(TaskCommand(
        command_id="create:stretch", task_id="stretch", kind="create_registered",
        payload={"draft": {"content": "伸展", "mode": "soft", "earliest_at": due}},
    ))
    assert result.status == "applied"
    runtime.scheduler.schedule_recheck(occurrence_id=result.view.occurrence_id, run_at=due)

    runtime.record_sleep()
    env.clock.advance(20)
    assert not runtime.host.pump()
    runtime.record_wake()
    assert runtime.host.pump()
    turn, = engine.turns
    assert {"resume", "phase_elapsed", "scheduled_recheck", "window_open"} <= set(turn["event"]["trigger_reasons"])
    elapsed, = turn["event"]["phase_elapsed"]
    assert elapsed["phase_id"] == rest.id
    assert elapsed["transition_reason"] == "elapsed_during_sleep"
    assert turn["context"]["current_phase"] is None
    assert not runtime.host.pump()


def test_sleep_fences_inflight_background_write_and_resume_recovers(desktop):
    runtime, env, engine, _ = desktop
    focus = start(env, "focus", planned_duration_s=600)
    env.clock.advance(120)
    runtime.scheduler.poll()
    entered, release = Event(), Event()
    outcomes = []

    def schedule_after_resume(turn, invoke):
        entered.set()
        assert release.wait(3)
        outcomes.append(invoke("decision.recheck", "after-suspend", {
            "purpose": "pet_presentation",
            "recheck_at": (env.clock.wall_now() + timedelta(seconds=10)).isoformat(),
        }))

    engine.action = schedule_after_resume
    with ThreadPoolExecutor(max_workers=1) as executor:
        pump = executor.submit(runtime.host.pump)
        try:
            assert entered.wait(3)
            runtime.record_sleep()
            env.clock.advance(200)
            runtime.record_wake()
        finally:
            release.set()
        assert pump.result(3)
    assert outcomes[0]["error_code"] == "SUPERSEDED"
    assert env.sessions.get_phase(focus.id).actual_focus_s == 120
    engine.action = None
    assert runtime.host.pump()
    assert "resume" in engine.turns[-1]["event"]["trigger_reasons"]
    assert "phase_elapsed" not in engine.turns[-1]["event"]["trigger_reasons"]


def test_resume_without_a_departure_uses_only_checkpointed_focus(desktop):
    runtime, env, engine, _ = desktop
    focus = start(env, "focus", planned_duration_s=30)
    env.clock.advance(10)
    env.sessions.pump()
    env.clock.advance(45)
    runtime.record_wake()
    active = env.sessions.current_phase()
    assert (active.id, active.state, active.actual_focus_s) == (focus.id, "paused", 10)
    assert runtime.host.pump()
    assert engine.turns[0]["event"]["trigger_reasons"] == ["resume"]


def publish(env, store, state, revision, *, app=None, domain=None):
    now = env.clock.wall_now()
    monotonic_s = env.clock.monotonic_now()
    with store.connection:
        sample = ActivitySample(now, monotonic_s, app_name=app, screen_locked=False)
        activity = store._insert(Activity(0, now, now, 0, app_name=app, domain=domain)) if app else None
        store._record_live_sample(now, monotonic_s, sample=sample, activity=activity)
    store.save_observation(Observation(
        "work", state, None, (), now, now + timedelta(seconds=15), revision,
    ), published_at=now, monotonic_s=monotonic_s)


def test_pet_observation_followup_replaces_timer_and_requires_meaningful_change(desktop):
    runtime, env, engine, store = desktop
    scheduler = runtime.scheduler
    publish(env, store, "working", 1)
    now = env.clock.wall_now()
    scheduler.schedule_recheck(purpose="pet_presentation", run_at=now + timedelta(seconds=2))
    scheduler.register_wait(purpose="pet_presentation", valid_until=now + timedelta(seconds=10),
                            changes=["activity_state"])

    env.clock.advance(2)
    publish(env, store, "working", 2)
    scheduler.poll()
    assert not runtime.host.pump()

    env.clock.advance(1)
    publish(env, store, "available", 3)
    scheduler.poll()
    assert runtime.host.pump()
    turn, = engine.turns
    assert turn["event"]["kinds"] == ["observation_ready"]
    assert turn["event"]["trigger_reasons"] == ["pet_presentation"]
    assert not runtime.host.pump()


def test_observation_change_before_wait_registration_is_not_lost(desktop):
    runtime, env, engine, store = desktop
    scheduler = runtime.scheduler
    publish(env, store, "working", 1)
    snapshot = scheduler.snapshot(env.clock.wall_now())
    env.clock.advance(1)
    publish(env, store, "available", 2)

    receipt = scheduler.register_wait(
        purpose="pet_presentation",
        after_cursor={"publication_cursor": snapshot["publication_cursor"],
                      "producer_generation": snapshot["producer_generation"]},
        valid_until=env.clock.wall_now() + timedelta(seconds=10),
        changes=["activity_state"],
    )
    assert receipt["state"] == "consumed"
    assert runtime.host.pump()
    assert engine.turns[0]["event"]["kinds"] == ["observation_ready"]
    assert not runtime.host.pump()


@pytest.mark.parametrize(("following_app", "following_domain", "wakes"), [
    ("Browser", "docs.example", False),
    ("Browser", "meet.example", True),
    ("Meeting Client", None, True),
])
def test_foreground_change_before_wait_registration_uses_the_decision_publication(
    desktop, following_app, following_domain, wakes,
):
    runtime, env, engine, store = desktop
    scheduler = runtime.scheduler
    publish(env, store, "working", 1, app="Browser", domain="docs.example")
    snapshot = scheduler.snapshot(env.clock.wall_now())
    env.clock.advance(1)
    publish(env, store, "working", 2, app=following_app, domain=following_domain)

    receipt = scheduler.register_wait(
        purpose="pet_presentation",
        after_cursor={"publication_cursor": snapshot["publication_cursor"],
                      "producer_generation": snapshot["producer_generation"]},
        valid_until=env.clock.wall_now() + timedelta(seconds=10),
        changes=["activity_context"],
    )
    assert receipt["state"] == ("consumed" if wakes else "waiting")
    assert runtime.host.pump() is wakes
    if wakes:
        turn, = engine.turns
        assert turn["event"]["kinds"] == ["observation_ready"]
        assert "foreground_key" not in json.dumps(turn)


def test_break_elapsed_at_restart_delivers_one_wake_after_composition(tmp_path, monkeypatch):
    monkeypatch.setenv("MOCHI_SYSTEM2_SEMANTIC_BACKGROUND", "1")
    clock = FakeClock()
    path = tmp_path / "business.sqlite3"
    sessions = PersistentWorkSessionService(path=path, clock=clock)
    phase = start(SimpleNamespace(sessions=sessions), "break", kind="short_break", planned_duration_s=10)
    sessions.close()
    clock.advance(20)

    recovered = PersistentWorkSessionService(path=path, clock=clock)
    scheduler = Scheduler(path, clock=clock)
    try:
        for _ in range(2):
            recovered.attach_system2(on_phase_elapsed=scheduler.on_phase_elapsed,
                                     on_phase_started=scheduler.invalidate_focus_waits)
        event, = scheduler.pending_wakes()
        assert event.phase_elapsed["phase_id"] == phase.id
        assert event.phase_elapsed["transition_reason"] == "elapsed_on_restart"
        assert event.at == phase.phase_due_at
        scheduler.ack_wakes([event.event_id])
        recovered.pump()
        assert scheduler.pending_wakes() == ()
    finally:
        recovered.close()


def test_phase_start_invalidates_deferred_focus_and_timed_rechecks_are_bounded(desktop):
    runtime, env, engine, _ = desktop
    scheduler = runtime.scheduler
    now = env.clock.wall_now()
    scheduler.schedule_recheck(purpose="focus_suggestion", run_at=now + timedelta(seconds=2))
    start(env, "focus", planned_duration_s=30)
    env.clock.advance(2)
    scheduler.poll()
    assert not runtime.host.pump()

    env.clock.advance(118)
    scheduler.poll()
    outcomes = []

    def schedule_recheck(turn, invoke):
        outcomes.append(invoke("decision.recheck", "defer", {
            "purpose": "pet_presentation", "recheck_at": (env.clock.wall_now() + timedelta(seconds=3)).isoformat(),
        }))

    engine.action = schedule_recheck
    assert runtime.host.pump()
    assert outcomes[0]["outcome"] == "deferred"
    env.clock.advance(3)
    scheduler.poll()
    assert runtime.host.pump()
    assert outcomes[1]["error_code"] == "RECHECK_LIMIT"
    assert not runtime.host.pump()
    with pytest.raises(SchedulerError, match="next hour"):
        scheduler.schedule_recheck(purpose="pet_presentation", run_at=env.clock.wall_now())


def test_rollback_discards_pending_and_future_semantic_wakes(desktop, monkeypatch):
    runtime, env, _, store = desktop
    start(env, "focus", planned_duration_s=10)
    env.clock.advance(10)
    env.sessions.pump()
    publish(env, store, "working", 1)
    now = env.clock.wall_now()
    runtime.scheduler.schedule_recheck(purpose="pet_presentation", run_at=now + timedelta(seconds=10))
    runtime.scheduler.register_wait(purpose="focus_suggestion", valid_until=now + timedelta(seconds=25),
                                    changes=["activity_state"])
    assert [wake.reason for wake in runtime.scheduler.pending_wakes()] == ["phase_elapsed"]

    monkeypatch.setenv("MOCHI_SYSTEM2_SEMANTIC_BACKGROUND", "0")
    assert runtime.scheduler.pending_wakes() == ()
    monkeypatch.setenv("MOCHI_SYSTEM2_SEMANTIC_BACKGROUND", "1")
    env.clock.advance(10)
    publish(env, store, "available", 2)
    runtime.scheduler.poll()
    assert runtime.scheduler.pending_wakes() == ()


def test_claim_merges_a_reason_committed_after_initial_poll(desktop, monkeypatch):
    runtime, env, engine, _ = desktop
    start(env, "focus", planned_duration_s=10)
    env.clock.advance(10)
    env.sessions.pump()
    pending_wakes = runtime.scheduler.pending_wakes

    def poll_before_resume():
        pending = pending_wakes()
        assert [wake.reason for wake in pending] == ["phase_elapsed"]
        runtime.scheduler.resume()
        return pending

    monkeypatch.setattr(runtime.scheduler, "pending_wakes", poll_before_resume)
    assert runtime.host.pump()
    assert engine.turns[0]["event"]["trigger_reasons"] == ["phase_elapsed", "resume"]
    assert pending_wakes() == ()


@pytest.mark.parametrize("wake_kind", ["phase_elapsed", "timer", "observation"])
def test_transient_background_failure_retries_one_shot_wake_once(desktop, wake_kind):
    runtime, env, engine, store = desktop
    now = env.clock.wall_now()
    if wake_kind == "phase_elapsed":
        start(env, "focus", planned_duration_s=10)
        env.clock.advance(10)
        env.sessions.pump()
    elif wake_kind == "timer":
        runtime.scheduler.schedule_recheck(purpose="pet_presentation", run_at=now + timedelta(seconds=1))
        env.clock.advance(1)
    else:
        publish(env, store, "working", 1)
        runtime.scheduler.register_wait(purpose="pet_presentation", valid_until=now + timedelta(seconds=25),
                                        changes=["activity_state"])
        env.clock.advance(1)
        publish(env, store, "available", 2)
        runtime.scheduler.poll()

    def offline(*_):
        raise TimeoutError

    engine.action = offline
    assert runtime.host.pump() and not runtime.host.pump()
    env.clock.advance(5)
    assert runtime.host.pump() and not runtime.host.pump()
    first, second = [turn["event"] for turn in engine.turns]
    assert first["trigger_ids"] != second["trigger_ids"]
    assert second["trigger_reasons"] == first["trigger_reasons"]
    if wake_kind == "phase_elapsed":
        assert second["phase_elapsed"] == first["phase_elapsed"]
    env.clock.advance(5)
    assert not runtime.host.pump()


@pytest.mark.parametrize("committed", [False, True])
def test_interrupted_claim_is_retried_without_repeating_a_durable_effect(desktop, monkeypatch, committed):
    runtime, env, engine, _ = desktop
    start(env, "focus", planned_duration_s=10)
    env.clock.advance(10)
    env.sessions.pump()
    old_host = runtime.host
    next_at = env.clock.wall_now() + timedelta(seconds=20)

    class ProcessLost(BaseException):
        pass

    def interrupt(*_):
        raise ProcessLost

    if committed:
        # The tool transaction committed, but the host never saved its copy.
        monkeypatch.setattr(old_host._store, "save_call_result", interrupt)

        def invoke_before_crash(_, invoke):
            invoke("decision.recheck", "commit", {"purpose": "pet_presentation", "recheck_at": next_at.isoformat()})

        engine.action = invoke_before_crash
    else:
        engine.action = interrupt
    with pytest.raises(ProcessLost):
        old_host.pump()
    assert runtime.scheduler.pending_wakes() == ()

    replacement = RecordingEngine()
    runtime.host = System2Host(old_host.data_dir, scheduler=runtime.scheduler, gateway=runtime.gateway,
                               settings_store=runtime.settings, clock=env.clock, engine=replacement,
                               context_reader=old_host.context_reader)
    try:
        assert runtime.host.pump() is not committed
        assert not runtime.host.pump()
        if committed:
            assert replacement.turns == []
            env.clock.advance(20)
            assert runtime.host.pump()
            assert replacement.turns[0]["event"]["trigger_reasons"] == ["pet_presentation"]
        else:
            retried, = replacement.turns
            assert retried["event"]["phase_elapsed"][0]["phase_id"] == "focus"
    finally:
        old_host.close()
