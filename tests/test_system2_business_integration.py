"""Exercise admission, local timing, and Qt presentation against one business database."""

from datetime import datetime, time, timedelta, timezone
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from deskpet.c_ui.desktop_system2 import DesktopSystem2
from deskpet.c_ui.presentation import DesktopPresentation
from deskpet.c_ui.refine_conversation_store import ConversationStore
from deskpet.system2.host import System2Host
from deskpet.system2.intent import describe_grants
from deskpet.system2.protocol import EngineReply
from deskpet.system2.tools import System2ToolExecutor
from deskpet.work_sessions import PersistentWorkSessionService
from tests.m0.test_desktop_presentation import NotificationProbe, snapshot
from tests.test_system2_tools import LocalBusiness


class ScriptedTurn:
    def __init__(self, callback):
        self.callback = callback

    def run(self, request, invoke):
        self.callback(json.loads(request.input_json), invoke)
        return EngineReply('{"action":"quiet","reason_code":"test"}')

    def close(self):
        pass


@pytest.fixture
def business(tmp_path):
    runtime = LocalBusiness(tmp_path)
    runtime.tasks.delivery.mode = "missing"
    try:
        yield runtime
    finally:
        runtime.close()


def host_for(business, callback):
    gateway = System2ToolExecutor(business.path, items=business.items, phases=business.phases,
                                 settings=business.settings, scheduler=business.scheduler)
    return System2Host(business.path.parent, scheduler=business.scheduler, gateway=gateway,
                       engine=ScriptedTurn(callback), settings_store=business.settings, clock=business.clock,
                       context_reader=business.scheduler.observation, manage_scheduler=False)


def create_reminder(business, title, **rule):
    result = business.tools.execute_local("item.create", {
        "type": "reminder", "title": title, "reminder_rule": rule,
    }, event_id=f"create {title}")
    assert result.status == "applied"
    return result.view


def present_args(item, reason="available"):
    return {"occurrence_ids": [item["occurrence"]["id"]], "reason_code": reason,
            "presentation": {"sound": True}}


def presenter_for(business, *, notifications=None):
    presenter = DesktopPresentation(path=business.path, notifications=notifications,
                                    lock_probe=lambda: False, synthetic=True)
    presenter.ledger.authorize_early = business.scheduler.authorize_qt
    refresh_presenter(presenter, business)
    return presenter


def refresh_presenter(presenter, business):
    presenter.observe(snapshot(business.clock, business.tasks, **business.settings.read().to_dict()))


@pytest.mark.parametrize("invalidated_at", ["tool", "paint", "sound"])
def test_fresh_observation_is_rechecked_at_each_visible_effect(business, monkeypatch, invalidated_at):
    from PyQt6.QtWidgets import QApplication

    item = create_reminder(business, "喝水")
    business.publish(71)
    business.scheduler.poll()
    results = []

    def turn(data, invoke):
        assert data["context"]["current"]["context_revision"] == 71
        if invalidated_at == "tool":
            business.clock.advance(1)
            business.publish(72, "working")
        results.append(invoke("reminder_present", "show", present_args(item)))

    host = host_for(business, turn)
    try:
        assert host.pump()
        business.tasks.pump()
        if invalidated_at == "tool":
            assert results[0]["error_code"] == "STALE_CONTEXT"
            assert not business.tasks.delivery.commands
            return
        assert results[0]["status"] == "pending"
        early, = business.tasks.delivery.commands
        presenter = presenter_for(business)
        presenter.native = True  # Capture audio requests without a device.
        beeps = []
        monkeypatch.setattr(QApplication, "beep", lambda: beeps.append(True))
        if invalidated_at == "sound":
            assert presenter.permit(early)
        business.clock.advance(1)
        business.publish(72, "working")
        refresh_presenter(presenter, business)
        if invalidated_at == "paint":
            assert not presenter.permit(early)
            assert not presenter.ledger.rows()
        else:
            # A prior paint is a fact, but the subsequent sound is a new effect.
            assert presenter.painted([early]) == [early]
            assert [row["state"] for row in presenter.ledger.rows()] == ["painted"]
        assert not beeps

        due = business.clock.wall_now()
        create_reminder(business, "准点开会", remind_after=due.isoformat(), remind_by=due.isoformat())
        business.tasks.pump()
        hard = business.tasks.delivery.commands[-1]
        assert hard.reason in {"hard_due", "deadline_fallback"}
        refresh_presenter(presenter, business)
        assert presenter.permit(hard)
    finally:
        host.close()


def test_ready_urgent_reminder_takes_the_shared_slot_before_proposal(business):
    business.auto_adjust()
    ordinary = create_reminder(business, "喝水")
    due = (business.clock.wall_now() + timedelta(minutes=1)).isoformat()
    urgent = create_reminder(business, "会前接耳机", remind_by=due)
    business.publish(71)
    business.scheduler.poll()
    results = {}

    def turn(data, invoke):
        assert [item["title"] for item in data["context"]["eligible_items"]] == ["会前接耳机", "喝水"]
        results["ordinary"] = invoke("reminder_present", "ordinary", present_args(ordinary))
        results["proposal"] = invoke("proposal_offer", "proposal", {
            "function": "phase.start", "args": {"planned_duration_s": 1500}, "display_text": "现在开始专注？",
        })
        results["urgent"] = invoke("reminder_present", "urgent", present_args(urgent))

    host = host_for(business, turn)
    try:
        assert host.pump()
        assert results["ordinary"]["status"] == "rejected"
        assert results["proposal"]["status"] == "pending" and not results["proposal"].get("delivery_id")
        assert results["urgent"]["status"] == "pending" and results["urgent"]["delivery_id"]
        business.tasks.pump()
        queued, = business.tasks.delivery.commands
        assert queued.delivery_id == results["urgent"]["delivery_id"]
        presenter = presenter_for(business)
        assert presenter.permit(queued)
        assert presenter.painted([queued]) == [queued]
    finally:
        host.close()

    # Reopen the authority and re-publish while the user remains available.
    business.clock.advance(1)
    latest = business.publish(72)
    gateway = business.gateway()
    retried = gateway.execute("reminder.present", present_args(ordinary) | {
        "expected_version": ordinary["version"],
    }, business.authority("after restart", origin="agent", snapshot=latest))
    assert retried.status == "rejected"
    assert len(business.tasks.delivery.commands) == 1
    assert len(gateway.pending_proposals()) == 1


@pytest.mark.parametrize("permit_taken", [False, True])
def test_restarted_presenter_preserves_a_slot_when_effect_is_unknown(business, permit_taken):
    first = create_reminder(business, "喝水")
    second = create_reminder(business, "起身走动")
    current = business.publish(71)
    result = business.command("reminder.present", present_args(first) | {
        "expected_version": first["version"],
    }, "first display", origin="agent", snapshot=current)
    assert result.status == "pending"
    business.tasks.pump()
    queued, = business.tasks.delivery.commands
    if permit_taken:
        assert presenter_for(business).permit(queued)

    business.clock.advance(1)
    latest = business.publish(72)
    assert latest["opportunity_id"] == current["opportunity_id"]
    restarted = presenter_for(business)
    assert [row["state"] for row in restarted.ledger.rows()] == (["unknown"] if permit_taken else [])
    assert not restarted.permit(queued)
    business.scheduler.poll()
    next_result = business.command("reminder.present", present_args(second) | {
        "expected_version": second["version"],
    }, "next display", origin="agent", snapshot=latest)
    assert next_result.status == ("rejected" if permit_taken else "pending")


def test_rechecks_estimated_window_without_changing_absolute_target(business):
    start = business.clock.wall_now()

    def at(seconds):
        return (start + timedelta(seconds=seconds)).isoformat()

    item = create_reminder(business, "联系组员确认会议", target_at=at(120), remind_after=at(30),
                           remind_by=at(180), expires_at=at(300), timezone="Asia/Shanghai")
    occurrence = item["occurrence"]["id"]
    assert item["occurrence"]["window_open_at"] is None

    results = []

    def turn(data, invoke):
        assert not data["context"]["eligible_items"]
        recheck, = data["context"]["recheck_items"]
        assert recheck["occurrence"]["id"] == occurrence
        assert "reminder.present" not in data["grants"]
        if "window_estimate" in data["event"]["trigger_reasons"]:
            bounds = {"window_open_at": at(15), "window_close_at": at(60)}
        else:
            assert "window_close" in data["event"]["trigger_reasons"]
            bounds = {"window_open_at": at(130), "window_close_at": at(300)}
        results.append(invoke("reminder_recheck", "estimate", {"occurrence_id": occurrence, **bounds}))

    host = host_for(business, turn)
    try:
        business.scheduler.poll()
        assert host.pump()
        estimated = results[0]["view"]
        assert results[0]["status"] == "applied"
        assert estimated["version"] == item["version"] + 1
        assert estimated["occurrence"]["id"] == occurrence
        assert estimated["rule"]["version"] == item["rule"]["version"]
        assert estimated["occurrence"]["window_open_at"] == at(30)
        assert estimated["occurrence"]["window_close_at"] == at(60)

        business.clock.advance(45)
        current = business.publish(71)
        assert current["eligible_items"][0]["id"] == item["id"]
        queued_result = business.command("reminder.present", present_args(estimated) | {
            "expected_version": estimated["version"],
        }, "early show", origin="agent", snapshot=current)
        assert queued_result.status == "pending"
        business.tasks.pump()
        old, = business.tasks.delivery.commands
        edited = business.tools.execute_local("item.update", {
            "id": item["id"], "expected_version": estimated["version"], "patch": {"title": "约组员开会"},
        }, event_id="edit reminder title").view
        assert edited["rule"] == estimated["rule"]
        assert not presenter_for(business).permit(old)

        business.clock.advance(15)
        business.scheduler.poll()
        business.tasks.pump()
        assert host.pump()
        assert results[1]["status"] == "applied"
        assert len(business.tasks.delivery.commands) == 1
        revised = results[1]["view"]
        assert revised["occurrence"]["window_open_at"] == at(130)
        assert revised["occurrence"]["window_close_at"] == at(180)
        assert {key: revised["rule"][key] for key in ("target_at", "remind_after", "remind_by", "expires_at", "timezone")} == {
            key: item["rule"][key] for key in ("target_at", "remind_after", "remind_by", "expires_at", "timezone")
        }
        assert revised["occurrence"]["id"] == occurrence

        exact = business.tools.execute_local("reminder.configure", {
            "item_id": item["id"], "expected_version": revised["version"],
            "rule": {"target_at": at(120), "remind_after": at(120), "remind_by": at(120)},
        }, event_id="explicit exact change")
        assert exact.status == "applied" and exact.view["occurrence"]["id"] != occurrence
        business.clock.advance(60)
        business.tasks.pump()
        old, hard = business.tasks.delivery.commands
        assert hard.reason == "hard_due" and hard.requested_at.isoformat() == at(120)
        presenter = presenter_for(business)
        assert presenter.permit(hard) and not presenter.permit(old)
    finally:
        host.close()


@pytest.mark.parametrize(("message", "fields"), [
    ("15点左右提醒我喝水", ("target_at",)),
    ("15 点以后提醒我喝水", ("remind_after",)),
    ("最迟 15 点提醒我喝水", ("remind_by",)),
    ("15:00整点提醒我喝水", ("remind_after", "remind_by")),
    ("只在今天有空时提醒我喝水", ("expires_at",)),
])
def test_chat_binds_the_users_literal_reminder_times(business, message, fields):
    zone = ZoneInfo("Asia/Shanghai")
    today = business.clock.wall_now().astimezone(zone).date()
    due = datetime.combine(today, time(15), tzinfo=zone)
    if fields == ("expires_at",):
        due = datetime.combine(today + timedelta(days=1), time.min, tzinfo=zone)
    expected = due.astimezone(timezone.utc).isoformat()
    results = []

    def turn(data, invoke):
        assert "item.create" in data["grants"]
        reminder = {"type": "reminder", "title": "喝水"}
        shifted = (due + timedelta(minutes=1)).isoformat()
        results.append(invoke("item_create", "omitted", reminder))
        results.append(invoke("item_create", "shifted", reminder | {
            "reminder_rule": dict.fromkeys(fields, shifted),
        }))
        results.append(invoke("item_create", "explicit", reminder | {
            "reminder_rule": dict.fromkeys(fields, expected),
        }))

    host = host_for(business, turn)
    try:
        host.submit_user(message)
        assert host.pump()
        assert [result["error_code"] for result in results[:2]] == ["NOT_AUTHORIZED", "NOT_AUTHORIZED"]
        assert results[2]["status"] == "applied"
        rule = results[2]["view"]["rule"]
        assert rule["timezone"] == "Asia/Shanghai"
        for name in ("remind_after", "target_at", "remind_by", "expires_at"):
            stored = datetime.fromisoformat(rule[name]) if rule[name] is not None else None
            assert stored == (due if name in fields else None)
        assert len(business.tasks.list_tasks()) == 1
    finally:
        host.close()


def test_chat_creates_relative_reminder_at_the_user_event_time_without_a_model(business):
    def unexpected_model(*_):
        pytest.fail("literal reminder and capability question should run locally")

    host = host_for(business, unexpected_model)
    try:
        start = business.clock.wall_now()
        host.submit_user("一分钟后提醒我喝水", event_id="water")
        business.clock.advance(3)
        assert host.pump()
        task, = business.tasks.list_tasks()
        due = start + timedelta(minutes=1)
        assert task.draft.content == "喝水"
        assert task.draft.mode == "hard"
        assert task.draft.opens_at == task.draft.hard_deadline == due

        host.submit_user("你现在有权限吗", event_id="capability-question")
        assert host.pump()
        assert len(business.tasks.list_tasks()) == 1
        created, capability = host.ui_events()
        assert created.payload["text"] == "已设置提醒「喝水」。"
        assert "可以直接叫我设提醒" in capability.payload["text"]

        host.submit_user("1秒后提醒我休息", event_id="elapsed-reminder")
        business.clock.advance(2)
        assert host.pump()
        assert len(business.tasks.list_tasks()) == 1
        assert host.ui_events()[-1].payload["status"] == "expired_request"

        business.clock.advance((due - business.clock.wall_now()).total_seconds())
        business.tasks.pump()
        delivery, = business.tasks.delivery.commands
        assert delivery.reason == "hard_due" and delivery.requested_at == due
    finally:
        host.close()


@pytest.mark.parametrize(("status", "state", "opportunity", "allowed"), [
    ("no_data", "unknown", None, False),
    ("expired", "available", "break", False),
    ("fresh", "working", "break", False),
    ("fresh", "available", None, False),
    ("fresh", "available", "break", True),
])
def test_background_pet_presentation_requires_a_current_opportunity(business, status, state, opportunity, allowed):
    grants = describe_grants(None, {
        "eligible_items": [], "enabled_settings": {}, "current_status": status,
        "current": {"state": state}, "opportunity_id": opportunity,
    }, requested_at=business.clock.wall_now())
    assert ("pet.present" in grants) == allowed


@pytest.mark.parametrize(("message", "mode"), [
    ("推迟五分钟", "flexible_target"),
    ("五分钟内别提醒", "no_earlier"),
    ("五分钟后准时", "exact"),
])
def test_chat_snooze_binds_the_mode_and_durable_user_event_time(business, monkeypatch, message, mode):
    original_due = business.clock.wall_now() + timedelta(minutes=1)
    item = create_reminder(business, "喝水", remind_after=original_due.isoformat(), remind_by=original_due.isoformat())
    occurrence = item["occurrence"]["id"]
    results = []

    def turn(data, invoke):
        assert "reminder.snooze" in data["grants"]
        assert datetime.fromisoformat(data["event"]["at"]) == submitted_at
        business.clock.advance(12)
        snooze = {"occurrence_id": occurrence, "delay_s": 300}
        mismatched = "exact" if mode == "flexible_target" else "flexible_target"
        results.append(invoke("reminder_snooze", "wrong mode", snooze | {"mode": mismatched}))
        correct = snooze | {"mode": mode}
        results.append(invoke("reminder_snooze", "model clock", correct | {"snoozed_at": original_due.isoformat()}))
        results.append(invoke("reminder_snooze", "correct mode", correct))
        assert invoke("reminder_snooze", "correct mode", correct) == results[-1]

    host = host_for(business, turn)
    try:
        chat = ConversationStore(business.path)
        desktop = SimpleNamespace(chat=chat, env=business, host=host)
        monkeypatch.setattr("deskpet.c_ui.refine_conversation_store.datetime",
                            SimpleNamespace(now=lambda _: business.clock.wall_now()))
        submitted_at = business.clock.wall_now()
        chat.queue_turn("snooze message", message, {"selected_ref": {"kind": "item", "id": item["id"]}})
        business.clock.advance(3)
        DesktopSystem2.pump(desktop)
        assert host.pump()
        assert [result["error_code"] for result in results[:2]] == ["NOT_AUTHORIZED", "INVALID_ARGUMENT"]
        assert results[2]["status"] == "applied"
        updated = results[2]["view"]
        assert updated["occurrence"]["id"] != occurrence
        assert updated["version"] == item["version"] + 1
        assert datetime.fromisoformat(updated["occurrence"]["last_snoozed_at"]) == submitted_at
        receipt = business.gateway().get_result(results[2]["call_id"])
        assert receipt.status == "applied" and receipt.actual_version == updated["version"]
        assert receipt.view["rule"] == updated["rule"] and receipt.view["occurrence"] == updated["occurrence"]
        rule = updated["rule"]
        when = submitted_at + timedelta(minutes=5)
        actual = {name: datetime.fromisoformat(rule[name]) if rule[name] is not None else None
                  for name in ("remind_after", "target_at", "remind_by")}
        assert actual == {
            "remind_after": None if mode == "flexible_target" else when,
            "target_at": None if mode == "no_earlier" else when,
            "remind_by": when if mode == "exact" else None,
        }
        business.clock.advance((original_due - business.clock.wall_now()).total_seconds())
        business.tasks.pump()
        assert not business.tasks.delivery.commands
        business.clock.advance((when - original_due).total_seconds())
        business.tasks.pump()
        assert [command.reason for command in business.tasks.delivery.commands] == (["hard_due"] if mode == "exact" else [])
    finally:
        host.close()


@pytest.mark.parametrize("scheduled_before_queue", [False, True])
def test_keeps_os_owner_at_deadline_across_qt_restart_and_content_edit(business, scheduled_before_queue):
    business.settings.update(expected_version=0, allow_system_notifications=True)
    due = business.clock.wall_now() + timedelta(minutes=2)
    item = create_reminder(business, "准时服药", remind_by=due.isoformat())
    current = business.publish(71)
    notifications = NotificationProbe()
    if scheduled_before_queue:
        presenter_for(business, notifications=notifications)

    request = present_args(item) | {"expected_version": item["version"]}
    result = business.command("reminder.present", request, "early attempt", origin="agent", snapshot=current)
    assert result.status == "pending"
    business.tasks.pump()
    presenter = presenter_for(business, notifications=notifications)
    row, = presenter.ledger.rows()
    assert row["state"] == "scheduled" and row["delivery_id"] == result.delivery_id
    assert len(notifications.submissions) == 1 and not notifications.removals
    queued = business.tasks.delivery.commands
    assert bool(queued) is not scheduled_before_queue
    assert all(not presenter.permit(command) for command in queued)

    business.clock.advance(2)
    changed = business.tools.execute_local("item.update", {
        "id": item["id"], "expected_version": item["version"], "patch": {"title": "晚上服药"},
    }, event_id="edit OS-owned title")
    assert changed.status == "applied" and changed.view["occurrence"]["id"] == item["occurrence"]["id"]
    refresh_presenter(presenter, business)
    assert [record["state"] for record in presenter.ledger.rows()] == ["scheduled"]
    assert not notifications.removals and len(notifications.submissions) == 1

    business.clock.advance(118)
    notifications.pending.remove(row["os_id"])
    notifications.delivered.add(row["os_id"])
    for _ in range(2):
        business.scheduler.poll()
        business.tasks.pump()
        restarted = presenter_for(business, notifications=notifications)
        assert [row["state"] for row in restarted.ledger.rows()] == ["delivered"]
        assert not notifications.removals
        assert len(notifications.submissions) == 1
        assert all(not restarted.permit(command) for command in business.tasks.delivery.commands)
        task = business.tasks.get(item["id"])
        assert task.displayed_at is None and task.acknowledged_at is None


@pytest.mark.parametrize("archive_first", [False, True])
def test_habit_archive_and_transition_at_same_time_use_commit_order(business, archive_first):
    habit = business.tools.execute_local("habit.create", {
        "title": "论文", "behavior": {"focus_s": 20, "short_break_s": 10,
                                      "auto_start_break": True, "auto_start_focus": True},
    }, event_id="create habit")
    assert habit.status == "applied"
    began = business.tools.execute_local("phase.start", {
        "habit_id": habit.view["id"], "expected_habit_version": habit.actual_version,
    }, event_id="begin habit")
    assert began.status == "applied"
    business.clock.advance(20)
    rest = business.phases.current_phase()
    assert rest.kind == "short_break"

    other = PersistentWorkSessionService(path=business.path, clock=business.clock, settings_store=business.settings)
    try:
        business.clock.advance(10)
        if not archive_first:
            other.pump()
        archived = business.tools.execute_local("habit.archive", {
            "id": habit.view["id"], "expected_version": habit.actual_version,
        }, event_id="archive habit")
        assert archived.status == "applied"
        other.pump()

        restarted = PersistentWorkSessionService(path=business.path, clock=business.clock,
                                                 settings_store=business.settings)
        try:
            phases = restarted.list_phases()
            assert len(phases) == (2 if archive_first else 3)
            assert [phase.state for phase in phases[:2]] == ["elapsed", "elapsed"]
            if not archive_first:
                assert phases[2].state == "running" and phases[2].source == "habit"
                business.clock.advance(20)
                assert restarted.current_phase().state == "elapsed"
                assert len(restarted.list_phases()) == 3
        finally:
            restarted.close()
    finally:
        other.close()
