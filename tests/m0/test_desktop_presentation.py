"""Phase 1 Qt presentation checks using an isolated SQLite store and OS double."""

import sqlite3
from datetime import timedelta
from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QApplication

from deskpet.c_ui.presentation import DesktopPresentation, NativeReminderFollowup
from deskpet.c_ui.refine_delivery import RefineDeliveryMixin
from deskpet.contracts import TaskDraft
from deskpet.mocks.clock import FakeClock
from deskpet.settings import SettingsStore
from deskpet.system2.scheduler import Scheduler
from deskpet.task_service.sqlite import SQLiteTaskService
from tests.m0.test_task_service import DisplayProbe, command, observe, register


class NotificationProbe:
    available = True
    status = "可用"
    can_schedule = True
    can_verify_removal = True

    def __init__(self):
        self.pending = set()
        self.delivered = set()
        self.submissions = []
        self.removals = []
        self.confirm_removal = False

    def refresh(self, *, request=False):
        pass

    def submit(self, identifier, due_at, *, sound, complete):
        self.submissions.append((identifier, due_at, sound))
        if due_at is not None:
            self.pending.add(identifier)
        complete("scheduled" if due_at is not None else "submitted", None)

    def inventory(self, complete):
        complete(frozenset(self.pending), frozenset(self.delivered))

    def remove_pending(self, identifier):
        self.removals.append(identifier)
        self.pending.discard(identifier)
        return self.confirm_removal


@pytest.fixture
def business(tmp_path):
    clock = FakeClock()
    delivery = DisplayProbe(clock)
    delivery.mode = "missing"
    with SQLiteTaskService(clock, delivery, lambda *_args, **_fields: None,
                           path=tmp_path / "business.sqlite3") as tasks:
        yield clock, delivery, tasks


def snapshot(clock, tasks, **settings):
    return {
        "clock": "virtual",
        "now": clock.wall_now().isoformat(),
        "tasks": [task.model_dump(mode="json") for task in tasks.list_tasks()],
        "pending_deliveries": tasks.pending_deliveries(),
        "settings": settings,
    }


@pytest.mark.parametrize("invalidate", ["cancel", "reschedule", "lock", "expire"])
def test_queued_presentation_rechecks_current_item_and_lock(business, invalidate):
    clock, delivery, tasks = business
    locked = False
    register(tasks, clock, mode="hard", due=0)
    tasks.pump()
    queued = delivery.commands[-1]
    presenter = DesktopPresentation(path=tasks.path, lock_probe=lambda: locked, synthetic=True)
    if invalidate == "cancel":
        assert command(tasks, "cancel").status == "applied"
    elif invalidate == "reschedule":
        due = clock.wall_now() + timedelta(minutes=5)
        draft = TaskDraft(content="Moved", mode="hard", remind_after=due, remind_by=due)
        assert command(tasks, "edit", draft=draft.model_dump(mode="json")).status == "applied"
    elif invalidate == "lock":
        locked = True
    else:
        clock.advance((queued.expires_at - clock.wall_now()).total_seconds())
    presenter.observe(snapshot(clock, tasks, reminder_sound=True))
    assert not presenter.permit(queued)
    assert not presenter.ledger.rows()


def test_cancellation_after_permitted_paint_keeps_history_but_skips_queued_sound(business, monkeypatch):
    clock, delivery, tasks = business
    notifications = NotificationProbe()
    register(tasks, clock, mode="hard", due=0)
    tasks.pump()
    queued = delivery.commands[-1]
    presenter = DesktopPresentation(path=tasks.path, notifications=notifications,
                                    lock_probe=lambda: False, synthetic=True)
    presenter.native = True
    beeps = []
    monkeypatch.setattr(QApplication, "beep", lambda: beeps.append(True))
    presenter.observe(snapshot(clock, tasks, reminder_sound=True))
    assert presenter.permit(queued)
    # The worker commits between the Qt backing-store paint and its callback.
    assert command(tasks, "cancel").status == "applied"
    presenter.painted([queued])
    assert not beeps
    assert [row["state"] for row in presenter.ledger.rows()] == ["painted"]
    with sqlite3.connect(tasks.path) as db:
        history = [event for event, in db.execute(
            "SELECT kind FROM events WHERE kind='card.painted' OR kind LIKE 'sound.%' ORDER BY rowid")]
    assert history == ["card.painted", "sound.skipped"]


@pytest.mark.parametrize("style", ["silent_intent", "disabled_after_queue"])
def test_card_can_paint_silently_with_an_older_sound_enabled_ui_snapshot(business, monkeypatch, style):
    clock, delivery, tasks = business
    preferences = SettingsStore(tasks.path)
    if style == "silent_intent":
        register(tasks, clock, mode="soft", due=0)
        observe(tasks, clock, "available", valid_until=clock.wall_now() + timedelta(seconds=10))
    else:
        register(tasks, clock, mode="hard", due=0)
    tasks.pump()
    queued = delivery.commands[-1]
    assert queued.sound == (None if style == "silent_intent" else "reminder")
    presenter = DesktopPresentation(path=tasks.path, lock_probe=lambda: False, synthetic=True)
    presenter.native = True
    beeps = []
    monkeypatch.setattr(QApplication, "beep", lambda: beeps.append(True))
    monkeypatch.setattr(presenter, "soft_valid", lambda _command: True)
    presenter.observe(snapshot(clock, tasks, reminder_sound=True))
    assert presenter.permit(queued)
    if style == "disabled_after_queue":
        preferences.update(expected_version=0, reminder_sound=False)
    assert presenter.painted([queued]) == [queued]
    assert not beeps


@pytest.mark.parametrize("confirmed", [False, True])
def test_scheduled_system_owner_releases_only_after_verified_retraction(business, confirmed):
    clock, delivery, tasks = business
    notifications = NotificationProbe()
    notifications.confirm_removal = confirmed
    task = register(tasks, clock, mode="hard", due=20)
    presenter = DesktopPresentation(path=tasks.path, notifications=notifications,
                                    lock_probe=lambda: False, synthetic=True)
    presenter.observe(snapshot(clock, tasks, allow_system_notifications=True, reminder_sound=True))
    assert len(notifications.submissions) == 1
    identifier, due_at, sound = notifications.submissions[0]
    assert due_at == task.draft.hard_deadline and not sound
    assert presenter.ledger.rows()[0]["state"] == "scheduled"
    assert tasks.get(task.task_id).acknowledged_at is None

    clock.advance(2)
    presenter.observe(snapshot(clock, tasks, allow_system_notifications=False, reminder_sound=True))
    assert notifications.removals == [identifier]
    clock.advance(2)
    presenter.observe(snapshot(clock, tasks, allow_system_notifications=False, reminder_sound=True))
    assert presenter.ledger.rows()[0]["state"] == ("cancelled" if confirmed else "unknown")

    clock.advance(16)
    for _ in range(2):
        presenter = DesktopPresentation(path=tasks.path, notifications=notifications,
                                        lock_probe=lambda: False, synthetic=True)
        tasks.pump()
        notes = presenter.observe(snapshot(clock, tasks, allow_system_notifications=False, reminder_sound=True))
        assert tasks.get(task.task_id).acknowledged_at is None
        assert len(notifications.submissions) == 1
        if not confirmed:
            assert not delivery.commands
            assert "投递结果未知" in notes[task.task_id]
    if confirmed:
        assert len(delivery.commands) == 1
        assert presenter.permit(delivery.commands[0])


def test_hidden_deadline_reports_permission_failure_and_unknown_after_acceptance(business):
    clock, delivery, tasks = business
    notifications = NotificationProbe()
    notifications.available = False
    notifications.can_schedule = False
    notifications.status = "系统通知权限已拒绝"
    task = register(tasks, clock, mode="hard", due=0)
    tasks.pump()
    queued = delivery.commands[-1]
    settings = {"allow_system_notifications": True, "reminder_sound": True}
    presenter = DesktopPresentation(path=tasks.path, notifications=notifications,
                                    lock_probe=lambda: True, synthetic=True)
    presenter.observe(snapshot(clock, tasks, **settings))
    presenter.route_hidden(queued)
    notes = presenter.observe(snapshot(clock, tasks, **settings))
    assert not notifications.submissions
    assert notifications.status in notes[task.task_id]
    assert presenter.native_followups() == []
    assert not presenter.permit(queued)

    notifications.available = True
    presenter.route_hidden(queued)
    assert presenter.ledger.rows()[0]["state"] == "submitted"
    assert len(notifications.submissions) == 1
    assert notifications.submissions[0][1] is None
    assert not tasks.get(task.task_id).displayed_at and not tasks.get(task.task_id).acknowledged_at

    # The platform can no longer prove the accepted card is in notification
    # history after wake. Repeated starts leave one silent pending occurrence.
    clock.advance(10)
    for _ in range(2):
        presenter = DesktopPresentation(path=tasks.path, notifications=notifications,
                                        lock_probe=lambda: False, synthetic=True)
        notes = presenter.observe(snapshot(clock, tasks, **settings))
        presenter.route_hidden(queued)
        tasks.pump()
        assert len(notifications.submissions) == 1
        assert "投递结果未知" in notes[task.task_id]
        assert not presenter.permit(queued)
        assert not tasks.get(task.task_id).displayed_at and not tasks.get(task.task_id).acknowledged_at


@pytest.mark.parametrize("delivered", [False, True])
def test_native_followup_recovers_existing_os_only_occurrence_once_at_or_after_due(business, delivered):
    clock, delivery, tasks = business
    notifications = NotificationProbe()
    task = register(tasks, clock, mode="hard", due=20)
    presenter = DesktopPresentation(path=tasks.path, notifications=notifications, synthetic=True)
    settings = {"allow_system_notifications": True, "reminder_sound": True}
    presenter.observe(snapshot(clock, tasks, **settings))
    assert presenter.native_followups() == []
    identifier, due, sound = notifications.submissions[0]
    assert due == task.draft.hard_deadline and sound is False

    # The old application left only the native ledger row. Startup both
    # migrates the database and recovers the occurrence after its deadline.
    with sqlite3.connect(tasks.path) as db:
        db.execute("DROP TABLE native_followup_paints")
    notifications.pending.clear()
    if delivered:
        notifications.delivered.add(identifier)
    clock.advance(30)
    locked = True
    settings["allow_system_notifications"] = False
    with SQLiteTaskService(clock, delivery, lambda *_args, **_fields: None, path=tasks.path) as restarted:
        restarted.pump()
        assert not delivery.commands
        presenter = DesktopPresentation(path=tasks.path, notifications=notifications, lock_probe=lambda: locked,
                                        synthetic=True)
        notes = presenter.observe(snapshot(clock, restarted, **settings))
        followup, = presenter.native_followups(include_painted=False)
        assert (followup.task_id, followup.task_version, followup.occurrence, followup.content, followup.delivered) == (
            task.task_id, task.version, task.occurrence, task.draft.content, delivered)
        assert ("已送达" in notes[task.task_id]) == delivered
        assert not presenter.native_followup_current(followup, mark_painted=True)
        locked = False
        assert presenter.native_followup_current(followup)
        assert presenter.native_followup_current(followup, mark_painted=True)
        assert not presenter.native_followup_current(followup, mark_painted=True)
        assert presenter.native_followup_current(followup, include_painted=True)

        presenter = DesktopPresentation(path=tasks.path, notifications=notifications, synthetic=True)
        presenter.observe(snapshot(clock, restarted, **settings))
        assert presenter.native_followups(include_painted=False) == []
        assert presenter.native_followups() == [followup]
        assert presenter.ledger.rows()[0]["state"] == ("delivered" if delivered else "unknown")
        assert notifications.submissions == [(identifier, due, False)]
        assert restarted.get(task.task_id).displayed_at is None
        assert restarted.get(task.task_id).acknowledged_at is None
        with sqlite3.connect(tasks.path) as db:
            assert db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 0
            events = db.execute("SELECT kind FROM events WHERE kind LIKE 'sound.%' OR "
                                "kind='presentation.native_followup_painted'").fetchall()
        assert events == [("presentation.native_followup_painted",)]

        assert command(restarted, "ack").status == "applied"
        assert not presenter.native_followup_current(followup, include_painted=True)
        presenter.observe(snapshot(clock, restarted, **settings))
        assert presenter.native_followups() == []


@pytest.mark.parametrize(("seconds_remaining", "elapsed", "secondary_label", "secondary_action"), [
    (299, 0, "查看详情", "details"),
    (300, 0, "查看详情", "details"),
    (301, 0, "5 分钟后", "snooze"),
    (301, 1, "5 分钟后", "details"),
])
def test_native_card_uses_details_when_five_minute_snooze_would_reach_expiry(
        seconds_remaining, elapsed, secondary_label, secondary_action):
    clock = FakeClock()
    followup = NativeReminderFollowup("native-id", "task", 1, 1, "Test reminder", True,
                                      clock.wall_now() + timedelta(seconds=seconds_remaining))
    actions = []
    session = RefineDeliveryMixin()
    session.production = True
    session.presentation = SimpleNamespace(
        now=clock.wall_now,
        native_followups=lambda **kwargs: [followup],
        native_followup_current=lambda *_args, **_kwargs: True,
    )
    session._can_show_reminder = lambda **kwargs: True
    session._valid_command = lambda _followup: True
    session._respond_to_reminder = lambda action, *subject, seconds=None: actions.append((action, subject, seconds))
    session._customize_reminder_snooze = lambda *subject: actions.append(("details", subject, None))

    prompt = session._native_followup_prompt()
    assert prompt.answer_labels == ("确认收到", secondary_label)
    clock.advance(elapsed)
    prompt.on_answer(False)
    prompt.on_answer(True)
    assert actions == [(secondary_action, ("task", 1), 300 if secondary_action == "snooze" else None),
                       ("ack", ("task", 1), None)]


def test_hidden_deadline_retries_known_failure_with_same_platform_id(business):
    clock, delivery, tasks = business

    class RejectedOnce(NotificationProbe):
        def submit(self, identifier, due_at, *, sound, complete):
            if not self.submissions:
                self.submissions.append((identifier, due_at, sound))
                complete("failed", "synthetic platform rejection")
            else:
                super().submit(identifier, due_at, sound=sound, complete=complete)

    notifications = RejectedOnce()
    notifications.can_schedule = False
    register(tasks, clock, mode="hard", due=0)
    tasks.pump()
    presenter = DesktopPresentation(path=tasks.path, notifications=notifications,
                                    lock_probe=lambda: True, synthetic=True)
    presenter.observe(snapshot(clock, tasks, allow_system_notifications=True, reminder_sound=True))
    presenter.route_hidden(delivery.commands[-1])
    assert presenter.ledger.rows()[0]["state"] == "failed"
    presenter.route_hidden(delivery.commands[-1])
    assert len(notifications.submissions) == 1
    clock.advance(5)
    tasks.pump()
    presenter.observe(snapshot(clock, tasks, allow_system_notifications=True, reminder_sound=True))
    presenter.route_hidden(delivery.commands[-1])
    assert presenter.ledger.rows()[0]["state"] == "submitted"
    assert len(notifications.submissions) == 2
    assert notifications.submissions[0][0] == notifications.submissions[1][0]


@pytest.mark.parametrize("result", ["paint", "restart", "unverified", "near_deadline"])
def test_soft_latest_system_fallback_transfers_only_after_verified_removal(business, result):
    clock, delivery, tasks = business
    now = clock.wall_now()
    draft = TaskDraft(content="Soft reminder with a hard latest", mode="soft",
                      remind_after=now, remind_by=now + timedelta(seconds=30))
    assert command(tasks, "create", draft=draft.model_dump(mode="json")).status == "applied"
    task = command(tasks, "confirm").view
    preferences = SettingsStore(tasks.path)
    preferences.update(expected_version=preferences.read().version, allow_system_notifications=True)
    scheduler = Scheduler(tasks, clock=clock, lock_probe=lambda: False)
    notifications = NotificationProbe()
    notifications.confirm_removal = result != "unverified"
    settings = {"allow_system_notifications": True, "reminder_sound": True}

    def presentation():
        display = DesktopPresentation(path=tasks.path, notifications=notifications,
                                      lock_probe=lambda: False, synthetic=True)
        display.ledger.authorize_early = scheduler.authorize_qt
        display.observe(snapshot(clock, tasks, **settings))
        return display

    presenter = presentation()
    identifier, due, _ = notifications.submissions[0]
    assert due == task.draft.hard_deadline
    authority = SimpleNamespace(expires_at=now + timedelta(seconds=30))
    receipt = scheduler.present(occurrence_ids=[task.occurrence_id], presentation={"sound": False},
                                reason_code="quiet_break", authority=authority, now=now)
    assert receipt["status"] == "pending"
    tasks.flush()
    queued = delivery.commands[-1]
    assert not presenter.permit(queued)
    if result == "near_deadline":
        clock.advance(26)
        presenter.observe(snapshot(clock, tasks, **settings))
        presenter.transfer_early(queued)
        assert not notifications.removals
        assert presenter.ledger.rows()[0]["state"] == "scheduled"
        assert not presenter.permit(queued)
        return
    presenter.transfer_early(queued)
    assert notifications.removals == [identifier]
    assert not presenter.permit(queued)
    clock.advance(2)
    presenter.observe(snapshot(clock, tasks, **settings))
    row, = presenter.ledger.rows()
    if result == "unverified":
        assert row["state"] == "unknown"
        assert not presenter.permit(queued)
        assert len(notifications.submissions) == 1
    else:
        assert row["state"] == "cancelled"
        if result == "restart":
            presenter = presentation()
            assert notifications.submissions == [(identifier, due, False)] * 2
            assert not presenter.permit(queued)
        else:
            assert presenter.permit(queued)
            assert presenter.painted([queued]) == [queued]
            assert presenter.ledger.rows()[0]["state"] == "painted"
            assert len(notifications.submissions) == 1


def test_semantic_system_channel_respects_newer_sound_preference(business):
    clock, delivery, tasks = business
    now = clock.wall_now()
    draft = TaskDraft(content="Private reminder", mode="soft", remind_after=now)
    assert command(tasks, "create", draft=draft.model_dump(mode="json")).status == "applied"
    task = command(tasks, "confirm").view
    preferences = SettingsStore(tasks.path)
    preferences.update(expected_version=preferences.read().version, allow_system_notifications=True, reminder_sound=True)
    scheduler = Scheduler(tasks, clock=clock, lock_probe=lambda: None)
    authority = SimpleNamespace(expires_at=now + timedelta(seconds=30))
    result = scheduler.present(occurrence_ids=[task.occurrence_id],
                               presentation={"channel": "os_notification", "sound": True},
                               reason_code="quiet_system_channel", authority=authority, now=now)
    assert result["status"] == "pending"
    tasks.flush()
    queued = delivery.commands[-1]
    assert queued.sound == "reminder"
    notifications = NotificationProbe()
    presenter = DesktopPresentation(path=tasks.path, notifications=notifications,
                                    lock_probe=lambda: None, synthetic=True)
    presenter.ledger.authorize_early = scheduler.authorize_qt
    presenter.observe(snapshot(clock, tasks, allow_system_notifications=True, reminder_sound=True))
    preferences.update(expected_version=preferences.read().version, reminder_sound=False)
    presenter.route_hidden(queued)
    presenter.route_hidden(queued)
    assert len(notifications.submissions) == 1
    assert notifications.submissions[0][1:] == (None, False)
    assert presenter.ledger.rows()[0]["state"] == "submitted"
    assert not tasks.get(task.task_id).displayed_at


def test_estimated_target_does_not_create_a_hard_fallback(business):
    clock, delivery, tasks = business
    target = clock.wall_now() + timedelta(minutes=5)
    draft = TaskDraft(content="Flexible target", target_at=target, earliest_at=clock.wall_now())
    assert command(tasks, "create", draft=draft.model_dump(mode="json")).status == "applied"
    assert command(tasks, "confirm").status == "applied"
    observe(tasks, clock, "working", valid_until=target + timedelta(minutes=15))
    for advance in (0, 300, 300):
        clock.advance(advance)
        tasks.pump()
        assert not delivery.commands
    changed = TaskDraft(content=draft.content, mode="hard", target_at=clock.wall_now(),
                        remind_after=clock.wall_now(), remind_by=clock.wall_now())
    assert command(tasks, "edit", draft=changed.model_dump(mode="json")).status == "applied"
    tasks.pump()
    assert len(delivery.commands) == 1
    assert delivery.commands[0].reason == "hard_due"
