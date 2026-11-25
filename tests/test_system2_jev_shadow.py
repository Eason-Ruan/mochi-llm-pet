"""Exercise opt-in Jev alongside real scheduler and admission transactions."""

from datetime import timedelta
import json
from threading import Event, current_thread
from time import monotonic, sleep

import pytest

from deskpet.contracts import TaskCommand
from deskpet.system2 import proposals
from deskpet.system2.host import System2Host
from deskpet.system2.jev_api import JevResult, JevSettings
from tests.test_system2_host import FixtureEngine
from tests.test_system2_tools import LocalBusiness


_PROVIDER = JevSettings(enabled=True, api_key="fixture-jev-key")


class FakeJev:
    def __init__(self, decide=None):
        self.decide = decide or (lambda _: JevResult("ok", 9, "defer", 0.97, 81, 8))
        self.calls = []
        self.entered = Event()

    def adjudicate(self, settings, state, *, criteria, send_permitted):
        self.calls.append((settings, state, criteria, current_thread().name))
        self.entered.set()
        return self.decide(send_permitted)


@pytest.fixture
def runtime(tmp_path):
    business = LocalBusiness(tmp_path)
    hosts = []

    def host(client, *, provider=None, decide=None, pet_card=False):
        engine = FixtureEngine(decide or (lambda *_: '{"action":"quiet","reason_code":"busy"}'))
        instance = System2Host(
            tmp_path, scheduler=business.scheduler, gateway=business.tools, engine=engine,
            settings_store=business.settings, clock=business.clock, manage_scheduler=False,
            context_reader=business.scheduler.observation, display_capabilities={"pet_card": pet_card},
            jev_settings_provider=provider or (lambda: _PROVIDER), jev_client=client,
        )
        hosts.append(instance)
        return instance

    yield business, host
    for instance in hosts:
        instance.close()
    business.close()


def _records(host, count=1):
    until = monotonic() + 4
    while monotonic() < until:
        results = host.jev_shadow_records()
        if len(results) >= count:
            return results
        sleep(0.01)
    pytest.fail("Jev shadow record did not complete")


def _tick(business, *, advance=60, revision=None, state="working"):
    business.clock.advance(advance)
    if revision is not None:
        with business.observations.connection:
            business.observations._record_live_sample(business.clock.wall_now(), business.clock.monotonic_now())
        business.publish(revision, state)
    business.scheduler.poll()


def _background_turns(host):
    return [json.loads(turn.input_json)["event"] for turn in host.engine.turns if turn.source == "background"]


def test_shadow_completion_and_usage_are_independent_of_real_background_review(runtime):
    business, create = runtime
    client = FakeJev(lambda allowed: JevResult(
        "ok", 9, "defer", 0.97, 81, 8, probabilities={"defer": 0.97, "wake_system2": 0.03},
        send_started_at=business.clock.wall_now(),
    ) if allowed() else JevResult("cancelled", 0))
    host = create(client)
    _tick(business)
    assert host.pump()
    first, = _records(host)
    assert first["status"] == "ok"
    assert first["last_real_review_at"] is None
    assert first["sent_at"] == first["started_at"] == first["completed_at"]
    assert first["input_tokens"] == 81 and first["output_tokens"] == 8
    assert first["model_choice"] == first["host_choice"] == "defer"
    assert first["probabilities"] == {"defer": 0.97, "wake_system2": 0.03}
    assert first["host_reason"] == "no_candidate"
    assert len(_background_turns(host)) == len(host.decision_records()) == 1
    assert host.decision_records()[0]["action"] == "quiet"
    assert not host.ui_events()
    assert business.scheduler.pending_wakes() == ()

    _tick(business)
    assert host.pump()
    second, _ = _records(host, 2)
    assert second["last_real_review_at"] == first["completed_at"]
    assert second["host_choice"] == "defer"
    assert client.calls[0][3] == client.calls[1][3] == "mochi-jev-shadow"
    assert client.calls[1][1]["last_system2_review_at"] == first["completed_at"]
    assert len(_background_turns(host)) == 2
    assert "fixture-jev-key" not in json.dumps((first, second))


@pytest.mark.parametrize("kind,reason", [
    ("resume", "resume"), ("recheck", "phase_elapsed"), ("recheck", "focus_released"),
    ("recheck", "pet_presentation"), ("recheck", "window_close"), ("reminder_ready", "window_open"),
    ("fresh_observation", "observation_waiter"),
])
def test_mixed_tick_and_actionable_wake_only_run_real_system2(runtime, kind, reason):
    business, create = runtime
    client = FakeJev()
    host = create(client)
    business.clock.advance(60)
    now = business.clock.wall_now()
    with business.scheduler._transaction() as db:
        business.scheduler._enqueue(db, event_id="actionable", kind=kind, at=now, reason=reason)
    business.scheduler.poll()
    assert host.pump()
    event, = _background_turns(host)
    assert set(event["trigger_reasons"]) == {"tick", reason}
    assert client.calls == [] and host.jev_shadow_records() == ()
    assert business.scheduler.pending_wakes() == ()


@pytest.mark.parametrize("boundary", ["hard_reminder", "timed_recheck", "phase"])
def test_priority_edge_before_next_tick_bypasses_shadow_read_only(runtime, boundary):
    business, create = runtime
    client = FakeJev()
    host = create(client)
    deadline = business.clock.wall_now() + timedelta(seconds=119)
    if boundary == "hard_reminder":
        result = business.tasks.apply(TaskCommand(
            command_id="register-hard", kind="create_registered", task_id="redacted-task",
            payload={"draft": {"mode": "hard", "content": "Private reminder", "earliest_at": deadline,
                               "latest_first_delivery_at": deadline}},
        ))
        assert result.status == "applied"
    elif boundary == "timed_recheck":
        with business.scheduler._transaction() as db:
            business.scheduler._enqueue(db, event_id="later", kind="recheck", at=deadline, reason="window_close")
    else:
        started = business.command("phase.start", {"planned_duration_s": 119}, "phase")
        assert started.status == "applied"

    _tick(business)
    assert host.pump()
    first, = _records(host)
    assert first["status"] == "bypassed"
    assert first["host_reason"] == ("priority_wake" if boundary == "timed_recheck" else "deadline_near")
    assert first["host_choice"] == "wake_system2" and client.calls == []
    event, = _background_turns(host)
    assert event["trigger_reasons"] == ["tick"]
    if boundary == "timed_recheck":
        business.clock.advance(59)
        wake, = business.scheduler.pending_wakes()
        assert wake.event_id == "later"


def test_slow_shadow_does_not_delay_chat_business_writes_or_later_tick(runtime):
    business, create = runtime
    sent, release = Event(), Event()

    def decide(allowed):
        assert allowed()
        sent.set()
        assert release.wait(20)
        return JevResult("ok", 110, "defer", 0.96)

    client = FakeJev(decide)
    host = create(client)
    try:
        _tick(business)
        assert host.pump() and sent.wait(4)
        assert len(_background_turns(host)) == 1
        host.submit_user("Any update?", event_id="chat")
        assert host.pump()
        reply, = host.ui_events()
        assert reply.reply_to_message_id == "chat"
        business.auto_adjust()
        _tick(business)
        assert host.pump()
        assert len(client.calls) == 1 and len(_background_turns(host)) == 2
        assert host.jev_shadow_records() == ()
    finally:
        release.set()
    record, = _records(host)
    assert record["status"] == "discarded"
    assert record["model_choice"] == "defer" and record["host_choice"] == "wake_system2"


@pytest.mark.parametrize("change", ["routine_cursor", "phase", "permission", "sleep", "scene"])
def test_permission_and_material_evidence_are_checked_at_send_and_completion(runtime, change):
    business, create = runtime
    release_send, sent, release_result = Event(), Event(), Event()
    permission = [_PROVIDER]
    transmitted = []

    def decide(allowed):
        assert release_send.wait(4)
        if not allowed():
            return JevResult("cancelled", 0)
        transmitted.append(True)
        sent.set()
        assert release_result.wait(4)
        return JevResult("ok", 12, "defer", 0.98)

    client = FakeJev(decide)
    host = create(client, provider=lambda: permission[0])
    try:
        _tick(business, revision=71)
        assert host.pump() and client.entered.wait(4)
        if change == "phase":
            assert business.command("phase.start", {"planned_duration_s": 1200}, "phase").status == "applied"
        elif change == "scene":
            business.clock.advance(0.1)
            business.publish(72, "available")
        elif change == "routine_cursor":
            business.clock.advance(0.1)
            business.publish(72, "working")
        release_send.set()
        if change not in {"phase", "scene"}:
            assert sent.wait(4)
        if change == "permission":
            permission[0] = JevSettings(enabled=False)
        elif change == "sleep":
            host.suspend_background()
            host.resume_background()
    finally:
        release_send.set()
        release_result.set()
    record, = _records(host)
    if change == "routine_cursor":
        assert transmitted and record["status"] == "ok" and record["host_choice"] == "defer"
    else:
        assert bool(transmitted) == (change in {"permission", "sleep"})
        assert record["status"] == "discarded" and record["host_choice"] == "wake_system2"


def test_shadow_dispatch_failure_does_not_strand_the_claimed_system2_turn(runtime, monkeypatch):
    business, create = runtime
    client = FakeJev()
    host = create(client)

    def unavailable(*_):
        raise OSError("fixture worker start failure")

    monkeypatch.setattr(host._jev, "start", unavailable)
    _tick(business)
    assert host.pump()
    assert len(_background_turns(host)) == len(host.decision_records()) == 1
    assert business.scheduler.pending_wakes() == ()
    assert client.calls == []


def test_timeout_falls_back_without_counting_failed_engine_as_completed_review(runtime):
    business, create = runtime
    client = FakeJev(lambda _: JevResult("timeout", 1500))
    outcomes = iter((False, True, True))

    def decide(*_):
        if not next(outcomes):
            raise OSError("fixture DSH error")
        return '{"action":"quiet","reason_code":"busy"}'

    host = create(client, decide=decide)
    for cycle in range(3):
        _tick(business)
        assert host.pump()
        current, *_ = _records(host, cycle + 1)
        assert current["status"] == current["host_reason"] == "timeout"
        assert current["host_choice"] == "wake_system2" and current["model_choice"] is None
        if cycle < 2:
            assert current["last_real_review_at"] is None
        else:
            assert current["last_real_review_at"] == previous["completed_at"]
        previous = current
    assert len(_background_turns(host)) == 3


def test_one_hypothetical_defer_cap_survives_unrelated_chat(runtime):
    business, create = runtime
    business.auto_adjust()
    host = create(FakeJev(), pet_card=True)
    _tick(business, revision=71)
    assert host.pump()
    first, = _records(host)
    assert first["host_choice"] == "defer"

    host.submit_user("What time is it?", event_id="chat")
    assert host.pump()
    _tick(business, revision=72)
    assert host.pump()
    second, _ = _records(host, 2)
    assert second["model_choice"] == "defer"
    assert second["host_choice"] == "wake_system2" and second["host_reason"] == "defer_cap"
    assert len(_background_turns(host)) == 2


@pytest.mark.parametrize("painted", [True, False])
def test_pending_exemption_requires_exact_origin_and_visible_card(runtime, painted):
    business, create = runtime
    business.auto_adjust()
    client = FakeJev()
    host = create(client, pet_card=True)
    _tick(business, revision=71)
    assert host.pump()
    _records(host)
    event, = _background_turns(host)
    now = business.clock.wall_now()
    proposal = proposals.Proposal(
        id="private-card-id", version=1, subject_refs=(), expected_versions={},
        function="phase.start", args={"kind": "focus"}, origin_trigger=event["trigger_ids"][0],
        created_at=now, valid_until=now + timedelta(minutes=5), display_text="Private card title",
    )
    with business.scheduler._transaction() as db:
        proposals.save(db, "default", proposal, key="business-key")
        proposals.enqueue(db, "default", proposal, delivery_id="card-delivery", opportunity_id="",
                          context_revision=71, valid_until=now + timedelta(seconds=30),
                          observation_cursor=None, observation_generation=None)
        if painted:
            proposals.set_delivery(db, "default", "card-delivery", "displayed")
            business.scheduler.record_contact(delivery_id="card-delivery", kind="proposal", at=now,
                                              channel="pet_card", subject_ref=proposal.id,
                                              exposure="painted", db=db)

    _tick(business, revision=72)
    assert host.pump()
    current, _ = _records(host, 2)
    state = client.calls[1][1]
    assert state["pending_offer"] is True and state["same_opportunity"] is True
    assert (current["host_reason"] == "pending_offer") is painted
    assert "private-card-id" not in json.dumps(state)
    assert "Private card title" not in json.dumps(state)
