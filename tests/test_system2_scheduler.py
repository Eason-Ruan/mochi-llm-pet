from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace

import pytest

from deskpet.contracts import TaskCommand
from deskpet.mocks.clock import FakeClock
from deskpet.mocks.delivery import FakeDeliveryPort
from deskpet.observation_v2.activity import ActivityRecorder, ActivitySample
from deskpet.observation_v2.context import ObservationContextReader
from deskpet.observation_v2.pipeline import ObservationStore
from deskpet.observation_v2.state import Observation
from deskpet.observation_v2.vision import ScreenObservation
from deskpet.settings import SettingsStore
from deskpet.system2.scheduler import Scheduler
from deskpet.task_service.presentation import PresentationLedger
from deskpet.task_service.sqlite import SQLiteTaskService
from deskpet.work_sessions.models import PhaseCommand
from deskpet.work_sessions.persistent import PersistentWorkSessionService


class Desktop:
    def __init__(self, path, observation):
        self.path = path / 'business.sqlite'
        self.clock = FakeClock()
        self.delivery = FakeDeliveryPort(self.clock, outcome='missing')
        self.tasks = SQLiteTaskService(self.clock, self.delivery, lambda *a, **k: None, path=self.path,
                                       enforce_opportunities=True, auto_opportunity=False)
        self.settings = SettingsStore(self.path)
        self.observation = observation
        self.reader = ObservationContextReader(observation.connection.execute('PRAGMA database_list').fetchone()[2],
                                               boot_id='test-boot')
        self.scheduler = self.reopen_scheduler()
        self.tasks.on_occurrence_invalidated = self.scheduler.invalidate_occurrence

    @property
    def now(self):
        return self.clock.wall_now()

    def reopen_scheduler(self):
        return Scheduler(self.path, clock=self.clock, observation=self.reader)

    def publish(self, state='available', revision=71, ttl=15, *, seconds=0):
        self.clock.advance(seconds)
        mono = self.clock.monotonic_now()
        with self.observation.connection:
            self.observation._record_live_sample(self.now, mono)
        self.observation.save_observation(
            Observation('work', state, None, (), self.now, self.now + timedelta(seconds=ttl), revision),
            published_at=self.now, monotonic_s=mono,
        )

    def reminder(self, name, **values):
        result = self.tasks.apply(TaskCommand(command_id=f'create:{name}', kind='create_registered', task_id=name,
                                               payload={'draft': {'content': name, 'mode': 'soft', **values}}))
        assert result.status == 'applied', result
        return result.view

    def present(self, occurrence, *, reason='available', authority=None, scheduler=None, presentation=None):
        scheduler = scheduler or self.scheduler
        authority = authority or self.authority(scheduler)
        return scheduler.present(occurrence_ids=[occurrence], presentation=presentation or {'sound': True},
                                 reason_code=reason, authority=authority, now=self.now)

    def authority(self, scheduler=None):
        snapshot = (scheduler or self.scheduler).snapshot(self.now)
        obs = snapshot['current']
        return SimpleNamespace(
            context_revision=obs.context_revision if obs else None,
            observation_valid_until=obs.valid_until if obs else None,
            observation_cursor=snapshot['publication_cursor'],
            observation_generation=snapshot['producer_generation'],
            opportunity_id=snapshot['opportunity_id'],
        )

    def enable_focus_waits(self):
        self.settings.update(expected_version=self.settings.read().version, auto_adjust=True)


@pytest.fixture
def desktop(tmp_path):
    with ObservationStore(tmp_path / 'observation.sqlite', live=True, boot_id='test-boot') as observation:
        app = Desktop(tmp_path, observation)
        observation.begin_live_run(app.now, monotonic_s=0)
        try:
            yield app
        finally:
            app.tasks.close()


def test_minute_tick_and_upgrade_from_a_persisted_two_minute_schedule(desktop):
    from deskpet.system2.scheduler import _stamp

    original = desktop.now
    with desktop.scheduler._transaction() as db:
        db.execute("UPDATE system2_schedule SET tick_at=? WHERE user_scope='default'",
                   (_stamp(original + timedelta(seconds=120)),))
    scheduler = Scheduler(desktop.tasks, clock=desktop.clock)
    for cycle in range(2):
        desktop.clock.advance(59)
        scheduler.poll()
        assert scheduler.pending_wakes() == ()
        desktop.clock.advance(1)
        scheduler.poll()
        event, = scheduler.pending_wakes()
        assert (event.kind, event.at) == ('tick', original + timedelta(minutes=cycle + 1))
        scheduler.ack_wakes([event.event_id])


@pytest.fixture
def legacy_background(monkeypatch):
    monkeypatch.setenv("MOCHI_SYSTEM2_SEMANTIC_BACKGROUND", "0")


def test_window_open_recheck_and_idle_tick_merge(desktop):
    start = desktop.now
    opens = start + timedelta(seconds=120)
    reminder = desktop.reminder('meeting', target_at=start + timedelta(seconds=180), window_open_at=opens,
                                window_close_at=start + timedelta(seconds=240))
    desktop.scheduler.schedule_recheck(occurrence_id=reminder.occurrence_id, run_at=opens)
    desktop.scheduler.poll()
    assert desktop.scheduler.pending_wakes() == ()
    desktop.clock.advance(120)
    desktop.scheduler.poll()
    events = desktop.scheduler.pending_wakes()
    assert {event.reason for event in events} == {'tick', 'window_open', 'scheduled_recheck'}
    assert all(event.at == opens for event in events)
    desktop.scheduler.ack_wakes(event.event_id for event in events)
    desktop.scheduler.poll()
    assert desktop.scheduler.pending_wakes() == ()
    desktop.clock.advance(120)
    desktop.scheduler.poll()
    assert {event.reason for event in desktop.scheduler.pending_wakes()} == {'tick', 'window_close'}
    desktop.tasks.pump()
    assert desktop.delivery.commands == []


def test_delayed_settings_read_does_not_poison_a_fresh_observation(desktop, monkeypatch):
    desktop.publish()
    requested_at = desktop.now
    read_settings = desktop.scheduler._settings

    def delayed_settings(db):
        if desktop.now == requested_at:
            desktop.clock.advance(0.2)
        return read_settings(db)

    monkeypatch.setattr(desktop.scheduler, "_settings", delayed_settings)
    first = desktop.scheduler.snapshot(requested_at)
    assert first["current_status"] == "fresh"
    assert first["current"].context_revision == 71
    assert desktop.scheduler.snapshot(desktop.now)["current_status"] == "fresh"


def test_snooze_invalidates_wait_and_old_deadline_while_exact_deadline_runs_offline(desktop):
    first_deadline = desktop.now + timedelta(seconds=20)
    reminder = desktop.reminder('hydration', remind_by=first_deadline)
    desktop.scheduler.schedule_recheck(occurrence_id=reminder.occurrence_id, run_at=first_deadline - timedelta(seconds=1))
    desktop.scheduler.register_wait(purpose='reminder_recheck', after_cursor=0, occurrence_id=reminder.occurrence_id,
                                    valid_until=first_deadline)
    snooze = desktop.tasks.apply(TaskCommand(command_id='snooze', kind='snooze', task_id='hydration', expected_version=1,
                                             payload={'seconds': 30, 'mode': 'exact'}))
    assert snooze.status == 'applied'
    desktop.clock.advance(20)
    desktop.publish()
    desktop.scheduler.poll()
    desktop.tasks.pump()
    assert desktop.scheduler.pending_wakes() == ()
    assert desktop.delivery.commands == []
    desktop.clock.advance(10)
    desktop.tasks.pump()
    assert [(command.reason, command.occurrence) for command in desktop.delivery.commands] == [('hard_due', 2)]


def test_working_urgency_and_focus_override_are_rechecked_at_qt_permit(desktop, legacy_background):
    phases = PersistentWorkSessionService(path=desktop.path, clock=desktop.clock)
    try:
        ordinary = desktop.reminder('drink')
        urgent = desktop.reminder('headphones', urgency_until=desktop.now + timedelta(minutes=2),
                                  urgency_reason='The call begins in two minutes')
        phase = phases.apply_phase(PhaseCommand(command_id='focus', phase_id='focus', kind='start',
                                                payload={'planned_duration_s': 1200}))
        assert phase.status == 'applied'
        desktop.publish('available')
        desktop.scheduler.poll()
        assert desktop.present(ordinary.occurrence_id)['error_code'] == 'STALE_CONTEXT'
        desktop.publish('working', revision=72, seconds=1)
        assert desktop.present(ordinary.occurrence_id, reason='meeting_preparation')['error_code'] == 'STALE_CONTEXT'
        result = desktop.present(urgent.occurrence_id, reason='meeting_preparation')
        assert result['status'] == 'pending'
        desktop.tasks.pump()
        assert len(desktop.delivery.commands) == 1
        command = desktop.delivery.commands[0]
        assert command.position_hint == 'screen_edge' and command.sound is None
        ledger = PresentationLedger(desktop.path, authorize_early=desktop.scheduler.authorize_qt)
        assert ledger.permit_qt(command, desktop.now)
        assert ledger.mark_painted(command, desktop.now)
        assert not ledger.permit_qt(command, desktop.now)
    finally:
        phases.close()


def test_concurrent_opportunity_claim_and_replay_of_intervening_departure(desktop, legacy_background):
    first = desktop.reminder('first')
    second = desktop.reminder('second')
    desktop.publish()
    authority = desktop.authority()
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(desktop.present, item.occurrence_id, authority=authority,
                                  scheduler=desktop.reopen_scheduler()) for item in (first, second)]
        results = [future.result()['status'] for future in futures]
    assert sorted(results) == ['pending', 'rejected']
    winner = (first, second)[results.index('pending')]
    loser = (first, second)[results.index('rejected')]
    desktop.tasks.pump()
    ledger = PresentationLedger(desktop.path, authorize_early=desktop.scheduler.authorize_qt)
    assert ledger.permit_qt(desktop.delivery.commands[0], desktop.now)
    assert ledger.mark_painted(desktop.delivery.commands[0], desktop.now)
    restarted = desktop.reopen_scheduler()
    desktop.clock.advance(16)
    restarted.poll()
    desktop.publish('unknown')
    assert restarted.snapshot(desktop.now)['opportunity_id'] == authority.opportunity_id
    desktop.publish(revision=72)
    assert restarted.snapshot(desktop.now)['opportunity_id'] == authority.opportunity_id
    assert desktop.present(loser.occurrence_id, scheduler=restarted)['status'] == 'rejected'
    desktop.publish('working', revision=73)
    desktop.publish('available', revision=74, seconds=1)
    assert restarted.snapshot(desktop.now)['opportunity_id'] != authority.opportunity_id
    assert desktop.present(loser.occurrence_id, scheduler=restarted)['status'] == 'pending'
    assert desktop.present(winner.occurrence_id, scheduler=restarted)['status'] == 'pending'


def test_replay_wait_both_publication_orders_and_settings_change(desktop, legacy_background):
    desktop.enable_focus_waits()
    deadline = desktop.now + timedelta(minutes=3)
    first = desktop.scheduler.register_wait(purpose='focus_suggestion', after_cursor=0, valid_until=deadline)
    assert first['state'] == 'waiting'
    assert desktop.scheduler.register_wait(purpose='focus_suggestion', after_cursor=0, valid_until=deadline) == first
    desktop.publish()
    recovered = desktop.reopen_scheduler()
    recovered.poll()
    wake, = recovered.pending_wakes()
    assert wake.reason == 'focus_suggestion' and wake.publication_cursor == 1 and not wake.subject_refs
    recovered.ack_wakes([wake.event_id])
    recovered.poll()
    assert recovered.pending_wakes() == ()
    desktop.publish(revision=71, seconds=1)
    second = recovered.register_wait(purpose='focus_suggestion', after_cursor=1, valid_until=deadline)
    assert second['state'] == 'consumed'
    assert recovered.register_wait(purpose='focus_suggestion', after_cursor=1, valid_until=deadline) == second
    wake, = recovered.pending_wakes()
    assert wake.publication_cursor == 2
    desktop.settings.update(expected_version=desktop.settings.read().version, auto_adjust=False)
    assert recovered.pending_wakes() == ()


def test_wait_rollback_transaction_and_expired_publication_do_not_authorize(desktop):
    desktop.enable_focus_waits()
    valid_until = desktop.now + timedelta(minutes=2)
    desktop.scheduler.register_wait(purpose='focus_suggestion', after_cursor=0, valid_until=valid_until)
    desktop.publish(ttl=3)
    with pytest.raises(RuntimeError, match='interrupt'):
        with desktop.scheduler._transaction() as db:
            head = desktop.scheduler._sync(db, desktop.now)
            desktop.scheduler._consume_waits(db, desktop.now, head)
            raise RuntimeError('interrupt')
    assert desktop.scheduler.pending_wakes() == ()
    desktop.clock.advance(3)
    recovered = desktop.reopen_scheduler()
    recovered.poll()
    assert recovered.pending_wakes() == ()
    desktop.publish()
    recovered.poll()
    assert [event.publication_cursor for event in recovered.pending_wakes()] == [2]


def test_rollback_never_allows_previous_observation_or_queued_effect(desktop, legacy_background):
    task = desktop.reminder('drink')
    desktop.publish(revision=71)
    admitted = desktop.authority()
    assert desktop.present(task.occurrence_id, authority=admitted)['status'] == 'pending'
    desktop.tasks.pump()
    queued, = desktop.delivery.commands
    desktop.publish('working', revision=72, seconds=8)
    desktop.clock._wall -= timedelta(seconds=6)
    assert desktop.reader.read_observation_context(desktop.now).current.context_revision == 71
    restarted = desktop.reopen_scheduler()
    ledger = PresentationLedger(desktop.path, authorize_early=restarted.authorize_qt)
    assert not ledger.permit_qt(queued, desktop.now)
    assert desktop.present(task.occurrence_id, authority=admitted, scheduler=restarted)['error_code'] == 'STALE_CONTEXT'
    desktop.publish('available', revision=73, seconds=1)
    assert restarted.snapshot(desktop.now)['current'].context_revision == 73


def test_time_sensitive_reminder_priority_and_shared_interruption_quota(desktop, legacy_background):
    desktop.enable_focus_waits()
    normal = desktop.reminder('drink')
    urgent = desktop.reminder('minutes', expires_at=desktop.now + timedelta(seconds=60))
    desktop.publish()
    snapshot = desktop.scheduler.snapshot(desktop.now)
    opportunity = snapshot['opportunity_id']
    assert not desktop.scheduler.authorize_proposal(context_revision=71, now=desktop.now)
    assert desktop.present(normal.occurrence_id)['error_code'] == 'STALE_CONTEXT'
    accepted = desktop.present(urgent.occurrence_id)
    assert accepted['status'] == 'pending'
    assert not desktop.scheduler.claim_opportunity(opportunity_id=opportunity, delivery_id='other', now=desktop.now)
    desktop.clock.advance(15)
    desktop.scheduler.poll()
    desktop.publish(revision=72)
    assert not desktop.scheduler.authorize_proposal(context_revision=72, now=desktop.now)
    assert desktop.present(urgent.occurrence_id)['delivery_id'] == accepted['delivery_id']


def test_claim_wakes_rechecks_cancellation_and_can_rollback_atomically(desktop):
    task = desktop.reminder('drink')
    desktop.scheduler.poll()
    pending, = desktop.scheduler.pending_wakes()
    with pytest.raises(RuntimeError):
        with desktop.scheduler._transaction() as db:
            assert desktop.scheduler.claim_wakes([pending.event_id], now=desktop.now, db=db) == (pending,)
            raise RuntimeError
    assert desktop.scheduler.pending_wakes() == (pending,)
    desktop.tasks.apply(TaskCommand(command_id='cancel:drink', kind='cancel', task_id='drink', expected_version=task.version))
    assert desktop.scheduler.claim_wakes([pending.event_id], now=desktop.now) == ()


def test_pause_registers_recheck_even_before_first_phase_poll(desktop):
    phases = PersistentWorkSessionService(path=desktop.path, clock=desktop.clock)
    phases.on_focus_released = desktop.scheduler.on_focus_released
    try:
        desktop.reminder('stretched')
        desktop.scheduler.poll()
        desktop.scheduler.ack_wakes(event.event_id for event in desktop.scheduler.pending_wakes())
        started = phases.apply_phase(PhaseCommand(command_id='start', phase_id='focus', kind='start')).view
        paused = phases.apply_phase(PhaseCommand(command_id='pause', phase_id='focus', kind='pause',
                                                 expected_version=started.version))
        assert paused.view.state == 'paused'
        wakes = desktop.reopen_scheduler().pending_wakes()
        assert [event.reason for event in wakes] == ['focus_released']
        desktop.scheduler.poll()
        assert desktop.scheduler.pending_wakes() == wakes
    finally:
        phases.close()


def test_semantic_reminders_use_explicit_bounds_and_model_selected_style(desktop):
    start = desktop.now
    ordinary = desktop.reminder("ordinary", earliest_at=start + timedelta(seconds=2),
                                window_open_at=start + timedelta(seconds=10),
                                window_close_at=start + timedelta(seconds=12))
    urgent = desktop.reminder("urgent", remind_by=start + timedelta(seconds=20))
    desktop.enable_focus_waits()
    phases = PersistentWorkSessionService(path=desktop.path, clock=desktop.clock)
    try:
        phases.apply_phase(PhaseCommand(command_id="start", kind="start", phase_id="focus"))
        desktop.publish("working")
        assert desktop.present(ordinary.occurrence_id)["error_code"] == "OUTSIDE_WINDOW"
        desktop.clock.advance(2)
        snapshot = desktop.scheduler.snapshot(desktop.now)
        assert {candidate["task_id"] for candidate in snapshot["eligible_items"]} == {"ordinary", "urgent"}
        assert desktop.scheduler.authorize_proposal(context_revision=71, now=desktop.now)

        result = desktop.present(ordinary.occurrence_id)
        assert result["status"] == "pending"
        duplicate = desktop.present(ordinary.occurrence_id)
        assert duplicate["delivery_id"] == result["delivery_id"]
        desktop.tasks.pump()
        command, = desktop.delivery.commands
        assert command.sound == "reminder" and command.position_hint != "screen_edge"

        desktop.clock.advance(18)
        desktop.tasks.pump()
        assert [(c.task_id, c.reason) for c in desktop.delivery.commands if c.task_id == urgent.task_id] == [
            ("urgent", "deadline_fallback"),
        ]
    finally:
        phases.close()


def test_presentation_survives_new_sampling_and_expired_estimates(desktop):
    start = desktop.now
    task = desktop.reminder("review", window_open_at=start, window_close_at=start + timedelta(seconds=4))
    desktop.publish("working", ttl=3)
    authority = desktop.authority()
    desktop.publish("working", revision=72, seconds=1, ttl=3)
    assert desktop.present(task.occurrence_id, authority=authority)["status"] == "pending"
    desktop.tasks.pump()
    command, = desktop.delivery.commands
    desktop.publish("working", revision=73, seconds=5, ttl=3)
    desktop.clock.advance(4)
    restarted = desktop.reopen_scheduler()
    restarted.poll()
    assert restarted.snapshot(desktop.now)["recent_contacts"] == []
    ledger = PresentationLedger(desktop.path, authorize_early=restarted.authorize_qt)
    assert ledger.permit_qt(command, desktop.now)
    assert ledger.mark_painted(command, desktop.now)
    contact, = restarted.snapshot(desktop.now)["recent_contacts"]
    assert contact["kind"] == "reminder" and contact["delivery_id"] == command.delivery_id
    assert not ledger.permit_qt(command, desktop.now)


def test_material_change_rechecks_once_and_lock_unknown_blocks_detail(desktop):
    task = desktop.reminder("handoff")
    desktop.publish("working")
    assert desktop.present(task.occurrence_id)["status"] == "pending"
    desktop.tasks.pump()
    command, = desktop.delivery.commands
    desktop.publish("away", revision=72, seconds=1)
    ledger = PresentationLedger(desktop.path, authorize_early=desktop.scheduler.authorize_qt)
    assert not ledger.permit_qt(command, desktop.now)
    assert not ledger.permit_qt(command, desktop.now)
    events = [event for event in desktop.scheduler.pending_wakes() if event.reason == "presentation_context_changed"]
    wake, = events
    assert wake.occurrence_id == task.occurrence_id

    another = desktop.reminder("water")
    desktop.scheduler.lock_probe = lambda: None
    assert desktop.present(another.occurrence_id)["error_code"] == "STALE_CONTEXT"
    assert desktop.present(another.occurrence_id, presentation={"channel": "os_notification"})[
        "error_code"] == "DISABLED"
    desktop.settings.update(expected_version=desktop.settings.read().version, allow_system_notifications=True)
    assert desktop.present(another.occurrence_id, presentation={"channel": "os_notification"})[
        "status"] == "pending"


def test_no_evidence_can_authorize_and_expired_soft_reminder_does_not_display(desktop):
    now = desktop.now
    task = desktop.reminder("stretch", target_at=now + timedelta(seconds=30),
                            expires_at=now + timedelta(seconds=35))
    desktop.enable_focus_waits()
    assert desktop.scheduler.snapshot(now)["current"] is None
    assert desktop.scheduler.authorize_proposal(context_revision=None, now=now)
    assert desktop.present(task.occurrence_id)["status"] == "pending"
    desktop.tasks.pump()
    command, = desktop.delivery.commands
    ledger = PresentationLedger(desktop.path, authorize_early=desktop.scheduler.authorize_qt)
    desktop.clock.advance(35)
    desktop.scheduler.poll()
    desktop.tasks.pump()
    assert not ledger.permit_qt(command, desktop.now)
    assert len(desktop.delivery.commands) == 1



def test_starting_a_meeting_rechecks_a_reminder_without_a_state_change(desktop):
    task = desktop.reminder("handoff")
    desktop.publish("working")
    assert desktop.present(task.occurrence_id)["status"] == "pending"
    desktop.tasks.pump()
    command, = desktop.delivery.commands
    desktop.publish("working", revision=72, seconds=1)

    recorder = ActivityRecorder(desktop.observation)
    sample = ActivitySample(desktop.now, desktop.clock.monotonic_now(), app_name="Meeting",
                            window_id=1, screen_locked=False, idle_s=1)
    recorder.observe(sample)
    activity = recorder.current_activity
    with desktop.observation.connection:
        desktop.observation._record_live_sample(desktop.now, desktop.clock.monotonic_now(),
                                                sample=sample, activity=activity)
    desktop.observation.save_screen(ScreenObservation(
        "meeting", activity.activity_id, desktop.now, "ready", "fixture", "meeting", None, None,
    ), processed_at=desktop.now)

    ledger = PresentationLedger(desktop.path, authorize_early=desktop.scheduler.authorize_qt)
    assert not ledger.permit_qt(command, desktop.now)
    events = [event for event in desktop.scheduler.pending_wakes() if event.reason == "presentation_context_changed"]
    assert [event.occurrence_id for event in events] == [task.occurrence_id]



def test_scheduled_native_fallback_does_not_block_an_early_candidate(desktop):
    deadline = desktop.now + timedelta(minutes=5)
    task = desktop.reminder("hand in application", remind_by=deadline)
    desktop.settings.update(expected_version=desktop.settings.read().version, allow_system_notifications=True)
    ledger = PresentationLedger(desktop.path, authorize_early=desktop.scheduler.authorize_qt)
    delivery_id = f"delivery:{task.task_id}:{task.occurrence}:v{task.version}"
    # The ledger may be populated before the app's deadline so native
    # delivery can run while the app is unavailable.
    with ledger._transaction() as db:
        ledger._write_owner(db, delivery_id, task, "os_notification", "scheduled", desktop.now, "fallback")
    candidate, = desktop.scheduler.snapshot(desktop.now)["eligible_items"]
    assert candidate["task_id"] == task.task_id
    pending = desktop.present(task.occurrence_id)
    assert pending["view"]["channel"] == "pet_card" and pending["delivery_id"] == delivery_id
    desktop.scheduler.poll()
    desktop.tasks.pump()
    command, = desktop.delivery.commands
    assert command.channel == "pet_card"
    row, = ledger.rows()
    assert row["state"] == "scheduled" and row["channel"] == "os_notification"
