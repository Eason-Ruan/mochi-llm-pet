"""Domain command, proposal and presentation recovery against the shared SQLite services."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
import sqlite3

import pytest

from deskpet.mocks.clock import FakeClock
from deskpet.observation_v2.context import ObservationContextReader
from deskpet.observation_v2.pipeline import ObservationStore
from deskpet.observation_v2.state import Observation
from deskpet.settings import SettingsStore
from deskpet.system2.items import ItemService
from deskpet.system2.protocol import ToolAuthority
from deskpet.system2.scheduler import Scheduler
from deskpet.system2.tools import System2ToolExecutor
from deskpet.task_service.sqlite import SQLiteTaskService
from deskpet.work_sessions import PersistentWorkSessionService
from tests.m0.test_task_service import DisplayProbe


class LocalBusiness:
    def __init__(self, path):
        self.path = path / "business.sqlite3"
        self.clock = FakeClock()
        self.tasks = SQLiteTaskService(
            self.clock, DisplayProbe(self.clock), lambda *_, **__: None,
            path=self.path, auto_opportunity=False,
        )
        self.settings = SettingsStore(self.path)
        self.phases = PersistentWorkSessionService(path=self.path, clock=self.clock, settings_store=self.settings)
        self.tasks.on_terminal_issue = self.phases.unlink_completed_issue
        self.items = ItemService(self.tasks)
        observation = path / "observation.sqlite3"
        self.observations = ObservationStore(observation, live=True, boot_id="fixture-boot")
        self.observations.begin_live_run(self.clock.wall_now(), monotonic_s=0, poll_interval_s=30)
        self.scheduler = Scheduler(self.tasks, self.items, clock=self.clock,
                                   observation=ObservationContextReader(observation, boot_id="fixture-boot"))
        self.tasks.on_occurrence_invalidated = self.scheduler.invalidate_occurrence
        self.tools = self.gateway()

    def gateway(self):
        return System2ToolExecutor(
            self.path, items=self.items, phases=self.phases, settings=self.settings, scheduler=self.scheduler,
            authorization=lambda db, function, args, authority, *, now: (
                None if db.in_transaction and authority.intent_generation == 1 else "SUPERSEDED"
            ),
        )

    def auto_adjust(self):
        self.settings.update(expected_version=self.settings.read().version, auto_adjust=True)

    def publish(self, revision, state="available"):
        now = self.clock.wall_now()
        self.observations.save_observation(
            Observation("work", state, None, (), now, now + timedelta(seconds=15), revision),
            published_at=now, monotonic_s=self.clock.monotonic_now(),
        )
        return self.scheduler.snapshot(now)

    def authority(self, call_id, *, origin="user", versions=None, snapshot=None):
        now = self.clock.wall_now()
        observed = (snapshot or {}).get("current")
        return ToolAuthority(
            f"admission-{call_id}", f"step-{call_id}", call_id, 1, "default", origin, f"event-{call_id}",
            now, now + timedelta(minutes=1), expected_versions=versions or {},
            context_revision=observed.context_revision if observed else None,
            observation_valid_until=observed.valid_until if observed else None,
            opportunity_id=(snapshot or {}).get("opportunity_id"),
            observation_cursor=(snapshot or {}).get("publication_cursor"),
            observation_generation=(snapshot or {}).get("producer_generation"),
        )

    def command(self, function, args, call_id, **auth):
        return self.tools.execute(function, args, self.authority(call_id, **auth))

    def close(self):
        self.phases.close()
        self.observations.close()
        self.tasks.close()


@pytest.fixture
def business(tmp_path):
    runtime = LocalBusiness(tmp_path)
    yield runtime
    runtime.close()


def test_command_receipts_bind_authority_payload_and_version(business):
    gateway = business.tools
    start = business.authority("direct-start")
    args = {"title": " ", "planned_duration_s": 1500}
    assert gateway.execute("phase_start", args, replace(start, intent_generation=2)).error_code == "SUPERSEDED"
    assert gateway.execute("phase.start", args, replace(start, origin="agent")).error_code == "NEEDS_CONFIRMATION"
    first = gateway.execute("phase.start", args, start)
    assert first.status == "applied" and first.view["title"] == "专注"
    business.clock.advance(60)
    restored = business.gateway()
    replay = restored.execute("phase.start", args, start)
    assert (replay.status, replay.original_status, replay.view) == ("duplicate", "applied", first.view)
    assert restored.execute("phase.start", args | {"title": "changed"}, start).error_code == "INVALID_ARGUMENT"
    assert restored.get_result("direct-start") == first
    detail = business.command("command.get_result", {"command_id": start.call_id}, "read")
    assert detail.view == first.to_dict()

    phase_id = first.view["id"]
    phase_version = first.actual_version
    adjusted = business.command("settings.update", {
        "scope": "current", "id": phase_id, "patch": {"total_s": 2400}, "expected_version": phase_version,
    }, "adjust")
    assert adjusted.view["remaining_s"] == 2340
    assert business.settings.read().default_focus_s == 1500
    stale = business.command("phase.pause", {"id": phase_id, "expected_version": adjusted.actual_version}, "stale",
                             versions={f"phase:{phase_id}": phase_version})
    assert stale.error_code == "STALE_TASK"


def _offer(duration=1500):
    return {"function": "phase.start", "args": {"kind": "focus", "planned_duration_s": duration},
            "subject_refs": [], "display_text": "现在开始专注？"}


def test_concurrent_proposals_amendments_and_lost_result_replay(business, monkeypatch):
    business.auto_adjust()
    snapshot = business.publish(71)
    with ThreadPoolExecutor(max_workers=2) as workers:
        offers = [workers.submit(business.command, "proposal.offer", _offer(), key,
                                 origin="agent", snapshot=snapshot) for key in ("model-a", "model-b")]
        offers = [offer.result() for offer in offers]
    assert {offer.status for offer in offers} == {"pending"}
    assert len({offer.subject_ref for offer in offers}) == len({offer.delivery_id for offer in offers}) == 1
    assert offers[0].delivery_id
    card, = business.tools.pending_proposals()
    delivery_id = offers[0].delivery_id
    restored = business.gateway()
    assert restored.permit_presentation(delivery_id)["id"] == card["id"]
    assert restored.validate_presentation(delivery_id)
    assert restored.ack_presentation(delivery_id, displayed=True)
    assert restored.permit_presentation(delivery_id) is None

    answer = {"id": card["id"], "answer": "accept", "expected_version": card["version"],
              "amendments": {"planned_duration_s": 2400}}
    write_receipt = restored._remember

    def crash_after_phase(*args):
        raise sqlite3.OperationalError("interrupted transaction")

    monkeypatch.setattr(restored, "_remember", crash_after_phase)
    with pytest.raises(sqlite3.OperationalError):
        restored.execute("proposal.resolve", answer, business.authority("accept"))
    assert business.phases.current_phase() is None
    assert restored.pending_proposals() == [card]
    monkeypatch.setattr(restored, "_remember", write_receipt)

    result = restored.execute("proposal.resolve", answer, business.authority("accept"))
    assert result.status == "applied"
    assert result.view["source"] == "proposal" and result.view["planned_duration_s"] == 2400
    restored = business.gateway()
    replay = restored.execute("proposal.resolve", answer, business.authority("accept"))
    assert replay.status == "duplicate" and replay.view == result.view
    late = restored.execute("proposal.resolve", {**answer, "amendments": None}, business.authority("old-button"))
    assert late.status == "duplicate" and late.view == result.view
    assert not restored.pending_proposals()
    assert len(business.phases.list_phases()) == 1


def test_refusal_persists_in_same_opportunity_until_work_then_available(business):
    business.auto_adjust()
    snapshot = business.publish(71)
    first = business.command("proposal.offer", _offer(), "offer", origin="agent", snapshot=snapshot)
    revised = business.command("proposal.offer", _offer(2400), "offer-revised", origin="agent", snapshot=snapshot)
    assert revised.subject_ref == first.subject_ref and revised.actual_version == first.actual_version + 1
    card = revised.view
    obsolete = {"id": card["id"], "expected_version": first.actual_version, "answer": "accept"}
    assert business.command("proposal.resolve", obsolete, "old").error_code == "STALE_TASK"
    declined = {**obsolete, "expected_version": card["version"], "answer": "reject"}
    assert business.command("proposal.resolve", declined, "no").status == "applied"
    business.tools = business.gateway()
    business.clock.advance(1)
    same_slot = business.publish(72)
    refused = business.command("proposal.offer", _offer(), "retry", origin="agent", snapshot=same_slot)
    assert refused.error_code == "ALREADY_RESOLVED"
    business.clock.advance(1)
    business.publish(73, "working")
    business.clock.advance(1)
    next_slot = business.publish(74)
    next_offer = business.command("proposal.offer", _offer(), "new-opportunity", origin="agent", snapshot=next_slot)
    assert next_offer.status == "pending" and next_offer.subject_ref != first.subject_ref


def test_fresh_wait_cancellation_and_proposal_priority_share_same_business_state(business):
    business.auto_adjust()
    wait_args = {"purpose": "focus_suggestion", "after_cursor": 0,
                 "valid_until": (business.clock.wall_now() + timedelta(minutes=2)).isoformat()}
    one = business.command("observation.wait_fresh", wait_args, "wait-a", origin="agent")
    two = business.command("observation.wait_fresh", wait_args, "wait-b", origin="agent")
    assert one.view["waiter_id"] == two.view["waiter_id"]
    due = business.clock.wall_now() + timedelta(minutes=2)
    reminder = business.command("item.create", {"type": "reminder", "title": "会前接耳机", "reminder_rule": {
        "urgency_until": due.isoformat(), "urgency_reason": "meeting_preparation",
    }}, "headphones")
    assert reminder.status == "applied"
    snapshot = business.publish(81)
    proposal = business.command("proposal.offer", _offer(), "opportunistic-proposal", origin="agent", snapshot=snapshot)
    assert proposal.status == "pending" and proposal.delivery_id is None
    assert not business.tools.pending_presentations()
    occurrence = reminder.view["occurrence"]
    display_args = {
        "occurrence_ids": [occurrence["id"]], "reason_code": "meeting_preparation",
        "presentation": {"sound": False}, "expected_version": occurrence["version"],
    }
    shown = business.command("reminder.present", display_args, "deliver", origin="agent", snapshot=snapshot)
    assert shown.status == "pending" and shown.delivery_id
    stale_revision = replace(business.authority("stale-deliver", origin="agent", snapshot=snapshot), context_revision=80)
    assert business.tools.execute("reminder.present", display_args, stale_revision).error_code == "STALE_CONTEXT"
    updated = business.command("settings.update", {"scope": "default", "patch": {"auto_adjust": False},
                                                   "expected_version": business.settings.read().version}, "disable")
    assert updated.status == "applied"
    assert not business.tools.pending_proposals()
    disabled = business.command("observation.wait_fresh", wait_args, "wait-disabled", origin="agent")
    assert disabled.error_code == "DISABLED"


def test_local_widget_reuses_original_ids_and_fences_only_applied_writes(business):
    from deskpet.work_sessions import PhaseCommand

    events = []
    business.tools.after_local_commit = lambda db, name, args, event_id: events.append(
        (db.in_transaction, name, event_id),
    )
    button = PhaseCommand(command_id="focus-button", phase_id="qt-phase", kind="start", source="chat",
                          payload={"planned_duration_s": 300})
    first = business.tools.execute_local_phase(button)
    assert first.status == "applied"
    assert (first.view.id, first.view.source) == ("qt-phase", "chat")
    assert business.tools.execute_local_phase(button).status == "duplicate"
    assert business.tools.execute_local_phase(button.model_copy(update={"phase_id": "changed"})).status == "rejected"
    conflict = PhaseCommand(command_id="pause-stale", phase_id="qt-phase", kind="pause")
    assert business.tools.execute_local_phase(conflict).error_code == "STALE_TASK"
    assert events == [(True, "phase.start", "focus-button")]


def test_snooze_supersedes_retry_and_keeps_the_issue_deadline(business):
    now = business.clock.wall_now()
    created = business.command("item.create", {"type": "issue", "title": "准备作业",
                                                "due_at": (now + timedelta(hours=4)).isoformat(),
                                                "reminder_rule": {"remind_by": (now + timedelta(minutes=1)).isoformat()}},
                               "issue")
    original = created.view["occurrence"]
    recheck = {"occurrence_id": original["id"], "run_at": (now + timedelta(seconds=15)).isoformat(),
               "expected_version": original["version"]}
    assert business.command("reminder.recheck", recheck, "original-recheck", origin="agent").status == "pending"
    snooze = {"occurrence_id": original["id"], "delay_s": 120, "mode": "flexible_target",
              "expected_version": original["version"]}
    result = business.command("reminder.snooze", snooze, "snooze")
    assert result.view["occurrence"]["id"] != original["id"]
    assert result.view["due_at"] == created.view["due_at"]
    assert result.view["rule"]["remind_by"] is None
    assert business.command("reminder.recheck", recheck, "obsolete-recheck", origin="agent").error_code == "STALE_TASK"
    assert business.tools.get_result("snooze").view == result.view


def test_terminal_issue_closes_interval_and_historical_reassignment_is_replay_safe(business):
    a, b = [
        business.command("item.create", {"type": "issue", "title": title}, title).view
        for title in ("读取论文", "修订报告")
    ]
    focus = business.command("phase.start", {"planned_duration_s": 100, "issue_links": [a["id"]],
                                            "primary_issue_id": a["id"]}, "focus").view
    business.clock.advance(25)
    current = business.tools.execute_local("item.get", {"id": a["id"]}, event_id="get-a").view
    completed = business.command("item.set_status", {"id": a["id"], "status": "completed",
                                                     "expected_version": current["version"]}, "finish-a")
    assert completed.status == "applied"
    business.clock.advance(15)
    active = business.phases.current_phase()
    assert active.primary_issue_id is None
    assert business.command("phase.end", {"id": focus["id"], "expected_version": active.version}, "end").status == "applied"
    interval, unassigned = business.phases.list_focus_intervals(focus["id"])
    assert (interval.seconds, interval.primary_issue_id, unassigned.seconds) == (25, a["id"], 15)

    args = {"interval_id": interval.id, "expected_interval_version": interval.version, "target_issue_id": b["id"]}
    changed = business.command("phase.reassign_interval", args, "repair")
    assert changed.status == "applied"
    assert changed.view["primary_issue_id"] == b["id"]
    assert business.command("phase.reassign_interval", args, "repair").status == "duplicate"
    assert business.command("phase.reassign_interval", args, "stale-repair").error_code == "STALE_TASK"
    revised, _ = business.phases.list_focus_intervals(focus["id"])
    assert revised.version == interval.version + 1
    assert business.phases.issue_focus_summary(a["id"])["actual_focus_s"] == 0
    assert business.phases.issue_focus_summary(b["id"])["actual_focus_s"] == 25
    assert business.items.get(a["id"])["status"] == "completed"


def test_retry_keeps_delivery_identity_and_rejects_an_old_lease_ack(business):
    business.auto_adjust()
    snapshot = business.publish(81)
    offered = business.command("proposal.offer", _offer(), "offer-a", origin="agent", snapshot=snapshot)
    queued, = business.tools.pending_presentations()
    generation = queued["presentation_generation"]
    assert business.tools.permit_presentation(offered.delivery_id, generation=generation)
    assert business.tools.ack_presentation(offered.delivery_id, displayed=False, generation=generation)
    business.clock.advance(1)
    snapshot = business.publish(82)
    repeated = business.command("proposal.offer", _offer(), "offer-b", origin="agent", snapshot=snapshot)
    assert repeated.delivery_id == offered.delivery_id
    queued, = business.tools.pending_presentations()
    next_generation = queued["presentation_generation"]
    assert next_generation == generation + 1
    assert business.tools.permit_presentation(offered.delivery_id, generation=next_generation)
    assert not business.tools.ack_presentation(offered.delivery_id, displayed=False, generation=generation)
    assert business.tools.ack_presentation(offered.delivery_id, displayed=True, generation=next_generation)
