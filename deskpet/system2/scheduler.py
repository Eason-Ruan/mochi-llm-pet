"""Durable System 2 wakes, Observation waits, and interruption arbitration."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from deskpet.contracts import DisplayCommand, TaskView
from deskpet.observation_v2.context import RealtimeBridge, RealtimeHead
from deskpet.observation_v2.realtime import sample_realtime_clock
from deskpet.observation_v2.routes import SYSTEM2
from deskpet.settings import SettingsStore

from .contact_history import recent_contacts
from .semantic_policy import semantic_background_enabled


TICK_INTERVAL = timedelta(seconds=60)
REMINDER_PRIORITY_HORIZON = timedelta(seconds=120)
_DECISION_PURPOSES = {"focus_suggestion", "pet_presentation"}
_OBSERVATION_CHANGES = {"activity_state", "activity_context", "lock_state"}
_DEFER_LIMIT = timedelta(hours=1)
_ACTIONABLE_WAKE_LIFETIME = timedelta(minutes=5)
_WAKE_RETRY_DELAY = timedelta(seconds=5)
_SEMANTIC_WAKE_REASONS = {"phase_elapsed", "presentation_context_changed"}
_PRESENTATION_LEASE = timedelta(seconds=45)
_EARLY_REASONS = {"opportunity", "tool"}
_URGENCY_CODES = {"meeting_preparation", "time_sensitive", "deadline_approaching", "expiring"}
_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS system2_schedule (
        user_scope TEXT PRIMARY KEY, tick_at TEXT NOT NULL, last_wall TEXT NOT NULL,
        tick_sequence INTEGER NOT NULL DEFAULT 0,
        phase_key TEXT, focus_running INTEGER NOT NULL DEFAULT 0,
        producer_generation TEXT, publication_cursor INTEGER NOT NULL DEFAULT 0,
        departure_serial INTEGER NOT NULL DEFAULT 0, closed_through_cursor INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS system2_wakes (
        event_id TEXT PRIMARY KEY, user_scope TEXT NOT NULL, kind TEXT NOT NULL, at TEXT NOT NULL,
        reason TEXT NOT NULL, task_id TEXT, occurrence_id TEXT, task_version INTEGER, valid_until TEXT,
        waiter_id TEXT, producer_generation TEXT, publication_cursor INTEGER,
        state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','acked','cancelled'))
    )""",
    """CREATE INDEX IF NOT EXISTS system2_wake_schedule
        ON system2_wakes(user_scope, state, at)""",
    """CREATE TABLE IF NOT EXISTS system2_waiters (
        waiter_id TEXT PRIMARY KEY, user_scope TEXT NOT NULL, purpose TEXT NOT NULL,
        occurrence_key TEXT NOT NULL, task_id TEXT, task_version INTEGER,
        after_cursor INTEGER NOT NULL, producer_generation TEXT, settings_version INTEGER,
        valid_until TEXT NOT NULL, state TEXT NOT NULL CHECK (state IN ('waiting','consumed','cancelled')),
        wake_event_id TEXT, consumed_cursor INTEGER, consumed_generation TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS system2_defer_context (
        id TEXT PRIMARY KEY, evidence TEXT NOT NULL
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS system2_one_waiter
        ON system2_waiters(user_scope, purpose, occurrence_key) WHERE state='waiting'""",
    """CREATE INDEX IF NOT EXISTS system2_consumed_waiters
        ON system2_waiters(user_scope, purpose, occurrence_key, consumed_generation, consumed_cursor)""",
    """CREATE TABLE IF NOT EXISTS system2_phase_wakes (
        event_id TEXT PRIMARY KEY REFERENCES system2_wakes(event_id), details TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS system2_wake_retries (
        event_id TEXT PRIMARY KEY REFERENCES system2_wakes(event_id),
        retry_id TEXT UNIQUE NOT NULL REFERENCES system2_wakes(event_id)
    )""",
    """CREATE TABLE IF NOT EXISTS system2_presentation_context (
        delivery_id TEXT PRIMARY KEY, user_scope TEXT NOT NULL, channel TEXT NOT NULL,
        observation_generation TEXT, publication_cursor INTEGER, context_revision INTEGER,
        policy_generation INTEGER NOT NULL, route_version TEXT NOT NULL, valid_until TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS system2_passive_reminders (
        user_scope TEXT NOT NULL, task_id TEXT NOT NULL,
        occurrence INTEGER NOT NULL, PRIMARY KEY(user_scope, task_id)
    )""",
    """CREATE TABLE IF NOT EXISTS system2_contacts (
        user_scope TEXT NOT NULL, delivery_id TEXT NOT NULL, kind TEXT NOT NULL, at TEXT NOT NULL,
        channel TEXT NOT NULL, subject_ref TEXT, response TEXT,
        occurrence_id TEXT, exposure TEXT, response_at TEXT, sound_requested INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(user_scope, delivery_id)
    )""",
    """CREATE INDEX IF NOT EXISTS system2_contact_time ON system2_contacts(user_scope, at)""",
    """CREATE TABLE IF NOT EXISTS system2_opportunities (
        opportunity_id TEXT PRIMARY KEY, user_scope TEXT NOT NULL, opened_at TEXT NOT NULL,
        closed_at TEXT, claimed_delivery_id TEXT
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS system2_one_opportunity
        ON system2_opportunities(user_scope) WHERE closed_at IS NULL""",
    """CREATE UNIQUE INDEX IF NOT EXISTS system2_one_interruption_claim
        ON system2_opportunities(claimed_delivery_id) WHERE claimed_delivery_id IS NOT NULL""",
    """CREATE TABLE IF NOT EXISTS system2_reminder_authority (
        delivery_id TEXT PRIMARY KEY, user_scope TEXT NOT NULL, opportunity_id TEXT,
        producer_generation TEXT NOT NULL, publication_cursor INTEGER NOT NULL,
        context_revision INTEGER NOT NULL, valid_until TEXT NOT NULL, reason_code TEXT NOT NULL,
        urgency_reason TEXT, low_distraction INTEGER NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('pending','presented','revoked'))
    )""",
    """CREATE INDEX IF NOT EXISTS system2_pending_reminder_authority
        ON system2_reminder_authority(user_scope) WHERE state='pending'""",
)


class SchedulerError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class WakeEvent:
    event_id: str
    kind: str
    at: datetime
    user_scope: str
    subject_refs: tuple[str, ...]
    occurrence_id: str | None
    reason: str
    publication_cursor: int | None = None
    producer_generation: str | None = None
    phase_elapsed: dict | None = None


@dataclass(frozen=True)
class ReminderAuthorization:
    head: RealtimeHead
    task: TaskView
    opportunity_id: str | None
    valid_until: datetime
    low_distraction: bool
    urgency_reason: str | None


def _stamp(at: datetime) -> str:
    if not isinstance(at, datetime) or at.utcoffset() is None:
        raise SchedulerError("INVALID_ARGUMENT", "A timezone-aware timestamp is required")
    return at.astimezone(UTC).isoformat(timespec="microseconds")


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _occurrence(task: TaskView) -> str:
    return task.occurrence_id


def _table(db: sqlite3.Connection, name: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _phase(db: sqlite3.Connection) -> dict | None:
    if not _table(db, "phase_views"):
        return None
    row = db.execute("SELECT view FROM phase_views WHERE active=1").fetchone()
    return json.loads(row[0]) if row else None


def _running_focus(phase: dict | None) -> bool:
    return bool(phase and phase.get("kind") == "focus" and phase.get("state") == "running")


def _active(task: TaskView | None, now: datetime) -> bool:
    return bool(task and task.active_reminder and task.displayed_at is None
                and (task.draft.expires_at is None or now < task.draft.expires_at))


def _soft_lifetime(task: TaskView | None, now: datetime) -> bool:
    return bool(_active(task, now) and task.draft.mode == "soft"
                and (task.draft.hard_deadline is None or now < task.draft.hard_deadline))


def _early(task: TaskView | None, now: datetime, *, check_close: bool = True) -> bool:
    if not _soft_lifetime(task, now):
        return False
    draft = task.draft
    return ((draft.opens_at is None or draft.opens_at <= now)
            and (not check_close or draft.window_close_at is None or now < draft.window_close_at))


def _within_user_bounds(task: TaskView | None, now: datetime) -> bool:
    if not _soft_lifetime(task, now):
        return False
    earliest = task.draft.explicit_start_at
    return earliest is None or now >= earliest


def _eligible(task: TaskView | None, now: datetime) -> bool:
    return _within_user_bounds(task, now) if semantic_background_enabled() else _early(task, now)


class Scheduler:
    """Business-DB state used by the local pump, model bridge, and Qt permit gate."""

    def __init__(self, tasks_or_path, items=None, *, observation=None, clock,
                 user_scope: str = "default", lock_probe=None, observation_policy: bool | None = None):
        self.path = Path(getattr(tasks_or_path, "path", tasks_or_path)).resolve()
        self.items = items
        self.clock = clock
        self.user_scope = user_scope
        self.lock_probe = lock_probe
        self.observation = getattr(observation, "reader", observation)
        self.observation_policy = (getattr(observation, "settings_path", None) is not None
                                   if observation_policy is None else observation_policy)
        now = self.clock.wall_now()
        with self._transaction() as db:
            for statement in _SCHEMA:
                db.execute(statement)
            columns = {row[1] for row in db.execute("PRAGMA table_info(system2_contacts)")}
            for name, sql_type in (("occurrence_id", "TEXT"), ("exposure", "TEXT"), ("response_at", "TEXT"),
                                   ("sound_requested", "INTEGER NOT NULL DEFAULT 0")):
                if name not in columns:
                    db.execute(f"ALTER TABLE system2_contacts ADD COLUMN {name} {sql_type}")
            db.execute("CREATE INDEX IF NOT EXISTS system2_contact_response_time ON system2_contacts(user_scope, response_at)")
            created = db.execute("INSERT OR IGNORE INTO system2_schedule(user_scope,tick_at,last_wall) VALUES (?,?,?)",
                                 (self.user_scope, _stamp(now + TICK_INTERVAL), _stamp(now))).rowcount
            if not created:
                # An existing installation may still have a two-minute tick scheduled.
                db.execute("UPDATE system2_schedule SET tick_at=? WHERE user_scope=? AND tick_at>?",
                           (_stamp(now + TICK_INTERVAL), self.user_scope, _stamp(now + TICK_INTERVAL)))
            if created and self.user_scope == "default" and _table(db, "metadata"):
                previous = db.execute("SELECT value FROM metadata WHERE key='opportunity'").fetchone()
                opportunity = json.loads(previous[0]) if previous else {}
                if opportunity.get("id"):
                    db.execute("INSERT INTO system2_opportunities VALUES (?,?,?,NULL,?)",
                               (opportunity["id"], self.user_scope, _stamp(now), opportunity.get("claim")))

    @contextmanager
    def _transaction(self, db=None, *, write=True):
        if db is not None:
            if write and not db.in_transaction:
                raise ValueError("A business write transaction is required")
            yield db
            return
        with closing(sqlite3.connect(self.path, timeout=5, isolation_level=None)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def _settings(self, db):
        return SettingsStore.read_at(self.path, user_scope=self.user_scope, db=db)

    def _schedule(self, db):
        return db.execute("SELECT * FROM system2_schedule WHERE user_scope=?", (self.user_scope,)).fetchone()

    @staticmethod
    def _tasks(db) -> Iterable[TaskView]:
        if not _table(db, "tasks"):
            return
        for row in db.execute("SELECT view FROM tasks ORDER BY task_id"):
            yield TaskView.model_validate_json(row[0])

    @staticmethod
    def _task(db, task_id: str | None) -> TaskView | None:
        if not _table(db, "tasks"):
            return None
        row = db.execute("SELECT view FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return TaskView.model_validate_json(row[0]) if row else None

    def _resolve(self, db, occurrence_id: str) -> TaskView | None:
        if not isinstance(occurrence_id, str):
            return None
        task_id, separator, _ = occurrence_id.rpartition(":")
        task = self._task(db, task_id) if separator else None
        return task if task and _occurrence(task) == occurrence_id else None

    def _bridge_read(self, db, now: datetime) -> RealtimeBridge:
        settings = self._settings(db)
        if self.observation is None:
            return RealtimeBridge(RealtimeHead(None, "no_data"))
        if settings.observation_paused:
            return RealtimeBridge(RealtimeHead(None, "context_changed"))
        clock = sample_realtime_clock(self.clock.wall_now, self.clock.monotonic_now)
        if clock is None:
            return RealtimeBridge(RealtimeHead(None, "no_data"))
        sampled_at, sampled_monotonic = clock
        arguments = {"monotonic_s": sampled_monotonic,
                     "policy_generation": settings.observation_generation if self.observation_policy else None}
        try:
            reader = getattr(self.observation, "read_realtime_bridge", None)
            bridge = (reader(sampled_at, **arguments) if reader is not None else
                      RealtimeBridge(self.observation.read_realtime_head(sampled_at, **arguments)))
        except (sqlite3.Error, OSError, ValueError):
            return RealtimeBridge(RealtimeHead(None, "no_data"))
        head = bridge.head
        if head.current is not None and not head.current.observed_at <= now < head.current.valid_until:
            return replace(bridge, head=replace(head, current=None, current_status="context_changed"))
        return bridge

    def _lock_state(self) -> bool | None:
        return False if self.lock_probe is None else self.lock_probe()

    def _current_opportunity(self, db):
        return db.execute("SELECT * FROM system2_opportunities WHERE user_scope=? AND closed_at IS NULL",
                          (self.user_scope,)).fetchone()

    def get_opportunity(self, *, db=None) -> str | None:
        with self._transaction(db, write=False) as tx:
            row = self._current_opportunity(tx)
            return row["opportunity_id"] if row else None

    def _close(self, db, now: datetime, through_cursor: int) -> None:
        db.execute("UPDATE system2_opportunities SET closed_at=? WHERE user_scope=? AND closed_at IS NULL",
                   (_stamp(now), self.user_scope))
        db.execute("""UPDATE system2_schedule SET closed_through_cursor=max(closed_through_cursor,?)
                      WHERE user_scope=?""", (through_cursor, self.user_scope))

    def close_opportunity(self, *, reason: str, now: datetime | None = None, db=None) -> None:
        if reason not in {"locked", "sleep", "away", "working"}:
            raise ValueError("An observed departure is required")
        at = now if now is not None else self.clock.wall_now()
        with self._transaction(db) as tx:
            head = self._bridge_read(tx, at).head
            through = max(head.publication_cursor or 0, self._schedule(tx)["publication_cursor"])
            self._close(tx, at, through)
            if semantic_background_enabled() and reason in {"locked", "sleep"}:
                tx.execute("""UPDATE system2_presentation_context SET valid_until=?
                    WHERE user_scope=? AND valid_until>?""", (_stamp(at), self.user_scope, _stamp(at)))

    def _enqueue(self, db, *, event_id: str, kind: str, at: datetime, reason: str,
                 task: TaskView | None = None, valid_until: datetime | None = None,
                 waiter_id: str | None = None, head: RealtimeHead | None = None) -> None:
        db.execute("""INSERT OR IGNORE INTO system2_wakes
            (event_id,user_scope,kind,at,reason,task_id,occurrence_id,task_version,valid_until,
             waiter_id,producer_generation,publication_cursor) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (
            event_id, self.user_scope, kind, _stamp(at), reason, task.task_id if task else None,
            _occurrence(task) if task else None, task.version if task else None,
            _stamp(valid_until) if valid_until else None, waiter_id,
            head.producer_generation if head else None, head.publication_cursor if head else None,
        ))

    def resume(self, now: datetime | None = None, *, db=None) -> str:
        at = now if now is not None else self.clock.wall_now()
        event_id = self._event_id("resume", _stamp(at))
        with self._transaction(db) as tx:
            if db is not None and semantic_background_enabled():
                self._poll_timers(tx, at)
            self._enqueue(tx, event_id=event_id, kind="resume", at=at, reason="resume")
        return event_id

    def _event_id(self, *key) -> str:
        # JSON tuple encoding makes IDs unambiguous even when task IDs contain punctuation.
        return json.dumps((self.user_scope, *key), ensure_ascii=False, separators=(",", ":"))

    def _sync(self, db, now: datetime) -> RealtimeHead:
        bridge = self._bridge_read(db, now)
        head = bridge.head
        schedule = self._schedule(db)
        cursor = head.publication_cursor or 0
        if bridge.departure_serial is not None and bridge.departure_serial > schedule["departure_serial"]:
            through = bridge.departure_through_cursor or 0
            if bridge.departure_reason in {"working", "away"}:
                through = max(0, through - 1)
            self._close(db, now, through)
            db.execute("UPDATE system2_schedule SET departure_serial=? WHERE user_scope=?",
                       (bridge.departure_serial, self.user_scope))
            schedule = self._schedule(db)
        if cursor and cursor < schedule["publication_cursor"]:
            return RealtimeHead(None, "context_changed", head.publication_cursor, head.producer_generation)
        if head.producer_generation and (cursor != schedule["publication_cursor"]
                                         or head.producer_generation != schedule["producer_generation"]):
            db.execute("""UPDATE system2_schedule SET producer_generation=?, publication_cursor=?
                          WHERE user_scope=?""", (head.producer_generation, cursor, self.user_scope))
        locked = self._lock_state()
        if locked is True:
            self._close(db, now, max(cursor, schedule["publication_cursor"]))
        if locked is not False:
            return RealtimeHead(None, "context_changed", head.publication_cursor, head.producer_generation)
        if head.current_status != "fresh" or head.current is None:
            return head
        if cursor <= schedule["closed_through_cursor"]:
            return RealtimeHead(None, "context_changed", head.publication_cursor, head.producer_generation)
        if head.current.state in {"working", "away"}:
            # A valid working observation remains usable for the silent, urgent exception.
            self._close(db, now, max(0, cursor - 1))
        elif head.current.state == "available" and self._current_opportunity(db) is None:
            db.execute("INSERT INTO system2_opportunities VALUES (?,?,?,NULL,NULL)",
                       (uuid4().hex, self.user_scope, _stamp(now)))
        return head

    def _wait_evidence(self, head):
        current = head.current if head.current_status == "fresh" else None
        return {
            "activity_state": current.state if current else None,
            "activity_context": [current.activity_kind, getattr(head, "foreground_key", None)] if current else None,
            "lock_state": self._lock_state(),
        }

    @staticmethod
    def _has_wait_change(previous, current):
        for key, previous_value in previous.items():
            actual = current[key]
            if actual is None:
                continue
            if key == "activity_context" and previous_value is not None:
                if actual[0] != previous_value[0] or (actual[1] and previous_value[1] and actual[1] != previous_value[1]):
                    return True
            elif actual != previous_value:
                return True
        return False

    def _cancel_deferred_waits(self, db, purpose, occurrence_id):
        db.execute("""UPDATE system2_waiters SET state='cancelled' WHERE user_scope=? AND purpose=?
                    AND occurrence_key=? AND state='waiting'""", (self.user_scope, purpose, occurrence_id or ""))
        db.execute("""UPDATE system2_wakes SET state='cancelled' WHERE user_scope=? AND reason=?
                    AND occurrence_id IS ? AND kind='observation_ready' AND state='pending'""",
                   (self.user_scope, purpose, occurrence_id))

    def _cancel_deferred_timers(self, db, purpose, occurrence_id, *, except_event=None):
        reason = "scheduled_recheck" if occurrence_id else purpose
        db.execute("""UPDATE system2_wakes SET state='cancelled' WHERE user_scope=? AND reason=?
                    AND occurrence_id IS ? AND kind='recheck' AND state='pending' AND event_id IS NOT ?""",
                   (self.user_scope, reason, occurrence_id, except_event))

    def _wait_valid(self, db, waiter, now: datetime, settings) -> tuple[bool, TaskView | None]:
        if _time(waiter["valid_until"]) <= now or settings.observation_paused:
            return False, None
        semantic = semantic_background_enabled()
        if waiter["purpose"] == "pet_presentation" and not semantic:
            return False, None
        if (waiter["purpose"] == "focus_suggestion" and
                (not settings.auto_adjust or (not semantic and waiter["settings_version"] != settings.version))):
            return False, None
        if not waiter["occurrence_key"]:
            return True, None
        task = self._task(db, waiter["task_id"])
        return (bool(_soft_lifetime(task, now) and task.version == waiter["task_version"]
                     and _occurrence(task) == waiter["occurrence_key"]), task)

    def _consume_waits(self, db, now: datetime, head: RealtimeHead) -> None:
        settings = self._settings(db)
        rows = db.execute("""SELECT w.*, c.evidence FROM system2_waiters w
                          LEFT JOIN system2_defer_context c ON w.waiter_id=c.id
                          WHERE w.user_scope=? AND w.state='waiting' ORDER BY w.rowid""",
                          (self.user_scope,)).fetchall()
        current_evidence = None
        for waiter in rows:
            valid, task = self._wait_valid(db, waiter, now, settings)
            if not valid:
                db.execute("UPDATE system2_waiters SET state='cancelled' WHERE waiter_id=?", (waiter["waiter_id"],))
                continue
            cursor = head.publication_cursor
            if cursor is None or cursor <= waiter["after_cursor"]:
                continue
            deadline = _time(waiter["valid_until"])
            if waiter["evidence"] is not None:
                if not semantic_background_enabled():
                    db.execute("UPDATE system2_waiters SET state='cancelled' WHERE waiter_id=?", (waiter["waiter_id"],))
                    continue
                if current_evidence is None:
                    current_evidence = self._wait_evidence(head)
                baseline = json.loads(waiter["evidence"])
                if not self._has_wait_change(baseline, current_evidence):
                    previous_context = baseline.get("activity_context")
                    observed_context = current_evidence["activity_context"]
                    if (previous_context and previous_context[1] is None
                            and observed_context and observed_context[1] is not None):
                        baseline["activity_context"] = observed_context
                        db.execute("UPDATE system2_defer_context SET evidence=? WHERE id=?",
                                   (json.dumps(baseline), waiter["waiter_id"]))
                    continue
                until = min(head.current.valid_until, deadline) if head.current else deadline
            else:
                if head.current_status != "fresh" or head.current is None:
                    continue
                until = min(head.current.valid_until, deadline)
            event_id = self._event_id("observation_ready", waiter["waiter_id"])
            self._enqueue(db, event_id=event_id, kind="observation_ready", at=now, reason=waiter["purpose"],
                          task=task, valid_until=until, waiter_id=waiter["waiter_id"], head=head)
            db.execute("""UPDATE system2_waiters SET state='consumed', wake_event_id=?, consumed_cursor=?,
                          consumed_generation=? WHERE waiter_id=?""",
                       (event_id, cursor, head.producer_generation, waiter["waiter_id"]))

    def register_wait(self, *, purpose: str, after_cursor=None, valid_until: datetime,
                      occurrence_id: str | None = None, settings_version: int | None = None,
                      now: datetime | None = None, db=None, changes=None) -> dict:
        at = now if now is not None else self.clock.wall_now()
        semantic = semantic_background_enabled()
        _stamp(valid_until)
        if valid_until <= at:
            raise SchedulerError("EXPIRED_REQUEST", "The wait has already expired")
        purposes = _DECISION_PURPOSES if semantic else {"focus_suggestion"}
        if ((purpose == "reminder_recheck") != bool(occurrence_id)
                or purpose not in purposes | {"reminder_recheck"}):
            raise SchedulerError("INVALID_ARGUMENT", "Specify a valid wait purpose and reminder occurrence")
        if changes is not None and (not semantic or not changes or set(changes) - _OBSERVATION_CHANGES):
            raise SchedulerError("INVALID_ARGUMENT", "Specify a valid Observation change")
        if semantic:
            valid_until = min(valid_until, at + _DEFER_LIMIT)
        generation = None
        if isinstance(after_cursor, Mapping):
            generation = after_cursor.get("producer_generation")
            after_cursor = after_cursor.get("publication_cursor")
        if after_cursor is not None and (type(after_cursor) is not int or after_cursor < 0):
            raise SchedulerError("INVALID_ARGUMENT", "The publication cursor is invalid")
        with self._transaction(db) as tx:
            task = self._resolve(tx, occurrence_id) if occurrence_id else None
            if occurrence_id and not _soft_lifetime(task, at):
                raise SchedulerError("STALE_TASK", "The reminder occurrence is no longer eligible")
            if task:
                valid_until = min(value for value in (valid_until, task.draft.hard_deadline, task.draft.expires_at)
                                  if value is not None)
            settings = self._settings(tx)
            if settings.observation_paused or (purpose == "focus_suggestion" and not settings.auto_adjust):
                raise SchedulerError("DISABLED", "Observation waiting is disabled for this purpose")
            if settings_version is not None and settings_version != settings.version:
                raise SchedulerError("STALE_CONTEXT", "The wait settings have changed")
            head = self._sync(tx, at)
            self._consume_waits(tx, at, head)
            key = (self.user_scope, purpose, occurrence_id or "")
            existing = tx.execute("""SELECT * FROM system2_waiters WHERE user_scope=? AND purpose=?
                                      AND occurrence_key=? AND state='waiting'""", key).fetchone()
            # If a prior wait consumed the head during this registration, return
            # its wake. Registering against the same explicit cursor cannot fork it.
            if not semantic and existing is None and after_cursor is not None:
                existing = tx.execute("""SELECT * FROM system2_waiters WHERE user_scope=? AND purpose=?
                    AND occurrence_key=? AND state='consumed' AND after_cursor=? AND consumed_cursor=?
                    AND consumed_generation IS ? AND task_version IS ? AND settings_version IS ?
                    ORDER BY rowid DESC LIMIT 1""",
                    (*key, after_cursor, head.publication_cursor, head.producer_generation,
                     task.version if task else None, settings.version if purpose == "focus_suggestion" else None)).fetchone()
            if existing is None:
                schedule = self._schedule(tx)
                consumed = tx.execute("""SELECT max(consumed_cursor) FROM system2_waiters
                    WHERE user_scope=? AND purpose=? AND occurrence_key=?""", key).fetchone()[0]
                start_cursor = schedule["publication_cursor"] if after_cursor is None else after_cursor
                start_cursor = max(start_cursor, consumed or 0)
                known_evidence = None
                if semantic and after_cursor is not None and after_cursor < schedule["publication_cursor"]:
                    read_original = getattr(self.observation, "prior_wait_evidence", None)
                    if read_original is not None:
                        known_evidence = read_original(after_cursor, generation)
                    if known_evidence is None:
                        # Earlier publications cannot be treated as a change
                        # when the decision's baseline is unavailable.
                        start_cursor = schedule["publication_cursor"]
                if start_cursor > schedule["publication_cursor"]:
                    raise SchedulerError("INVALID_ARGUMENT", "The publication cursor is ahead of the live source")
                waiter_id = uuid4().hex
                tx.execute("""INSERT INTO system2_waiters
                    (waiter_id,user_scope,purpose,occurrence_key,task_id,task_version,after_cursor,
                     producer_generation,settings_version,valid_until,state) VALUES (?,?,?,?,?,?,?,?,?,?,'waiting')""", (
                    waiter_id, *key, task.task_id if task else None, task.version if task else None, start_cursor,
                    generation or schedule["producer_generation"], settings.version if purpose == "focus_suggestion" else None,
                    _stamp(valid_until),
                ))
                if semantic:
                    evidence = self._wait_evidence(head)
                    if known_evidence is not None:
                        evidence.update({key: value for key, value in known_evidence.items() if value is not None})
                    tx.execute("INSERT INTO system2_defer_context VALUES (?, ?)",
                               (waiter_id, json.dumps({key: evidence[key] for key in sorted(changes or _OBSERVATION_CHANGES)})))
                    self._cancel_deferred_timers(tx, purpose, occurrence_id)
                    # An unclaimed prior observation wake is the same decision.
                    tx.execute("""UPDATE system2_wakes SET state='cancelled' WHERE user_scope=? AND reason=?
                                  AND occurrence_id IS ? AND kind='observation_ready' AND state='pending'""",
                               (self.user_scope, purpose, occurrence_id))
                self._consume_waits(tx, at, head)
                existing = tx.execute("SELECT * FROM system2_waiters WHERE waiter_id=?", (waiter_id,)).fetchone()
            return {field: existing[field] for field in ("waiter_id", "state", "wake_event_id", "after_cursor", "valid_until")}

    def invalidate_occurrence(self, task_id: str, ordinal: int, *, db=None) -> None:
        occurrence_id = f"{task_id}:{ordinal}"
        with self._transaction(db) as tx:
            tx.execute("UPDATE system2_waiters SET state='cancelled' WHERE user_scope=? AND occurrence_key=? AND state='waiting'",
                       (self.user_scope, occurrence_id))
            tx.execute("UPDATE system2_wakes SET state='cancelled' WHERE user_scope=? AND occurrence_id=? AND state='pending'",
                       (self.user_scope, occurrence_id))

    def invalidate_focus_waits(self, *, db=None) -> None:
        with self._transaction(db) as tx:
            self._cancel_deferred_waits(tx, "focus_suggestion", None)
            self._cancel_deferred_timers(tx, "focus_suggestion", None)

    def on_phase_elapsed(self, phase_id: str, phase_version: int, *, db, elapsed_at=None,
                         phase_kind=None, transition_reason=None, successor=None) -> None:
        if not semantic_background_enabled():
            return
        elapsed_at = elapsed_at if elapsed_at is not None else self.clock.wall_now()
        event_id = self._event_id("phase_elapsed", phase_id, phase_version)
        details = {"phase_id": phase_id, "phase_version": phase_version, "phase_kind": phase_kind,
                   "elapsed_at": _stamp(elapsed_at), "transition_reason": transition_reason}
        if successor:
            details["successor"] = {key: successor[key] for key in ("id", "version", "kind")}
        with self._transaction(db) as tx:
            valid_until = max(elapsed_at, self.clock.wall_now()) + _ACTIONABLE_WAKE_LIFETIME
            self._enqueue(tx, event_id=event_id, kind="recheck", at=elapsed_at, reason="phase_elapsed",
                          valid_until=valid_until)
            tx.execute("INSERT OR IGNORE INTO system2_phase_wakes VALUES (?,?)", (event_id, json.dumps(details)))

    def on_focus_released(self, phase_id: str, phase_version: int, *, db) -> None:
        """Wake eligible reminders in the same transaction that releases focus."""
        now = self.clock.wall_now()
        with self._transaction(db) as tx:
            tx.execute("UPDATE system2_schedule SET focus_running=0 WHERE user_scope=?", (self.user_scope,))
            if any(_eligible(task, now) for task in self._tasks(tx)):
                self._enqueue(tx, event_id=self._event_id("focus_released", phase_id, phase_version),
                              kind="recheck", at=now, reason="focus_released")

    def schedule_recheck(self, *, run_at: datetime, occurrence_id: str | None = None,
                         purpose: str | None = None, now: datetime | None = None, db=None) -> str:
        at = now if now is not None else self.clock.wall_now()
        stamp = _stamp(run_at)
        semantic = semantic_background_enabled()
        if occurrence_id:
            if purpose not in {None, "reminder_recheck"}:
                raise SchedulerError("INVALID_ARGUMENT", "A reminder recheck requires its occurrence")
            purpose = "reminder_recheck"
        elif not semantic or purpose not in _DECISION_PURPOSES:
            raise SchedulerError("INVALID_ARGUMENT", "Specify a valid recheck purpose")
        with self._transaction(db) as tx:
            task = self._resolve(tx, occurrence_id) if occurrence_id else None
            if occurrence_id and not _soft_lifetime(task, at):
                raise SchedulerError("STALE_TASK", "The reminder occurrence is no longer active")
            if task and (run_at < at or (task.draft.hard_deadline and run_at >= task.draft.hard_deadline)
                         or (task.draft.expires_at and run_at >= task.draft.expires_at)):
                raise SchedulerError("OUTSIDE_WINDOW", "The recheck is outside the reminder lifetime")
            reason = "scheduled_recheck" if task else purpose
            event_id = (self._event_id("recheck", occurrence_id, task.version, stamp) if task
                        else self._event_id("decision_recheck", purpose, stamp))
            valid_until = None
            if semantic:
                if not at < run_at <= at + _DEFER_LIMIT:
                    raise SchedulerError("OUTSIDE_WINDOW", "Recheck within the next hour")
                if purpose == "focus_suggestion" and not self._settings(tx).auto_adjust:
                    raise SchedulerError("DISABLED", "Automatic focus suggestions are disabled")
                evidence = self._wait_evidence(self._sync(tx, at))
                phase = _phase(tx)
                if purpose == "focus_suggestion" and phase:
                    evidence["phase"] = [phase["id"], phase["state"], phase["kind"]]
                encoded = json.dumps(evidence, sort_keys=True)
                repeated = tx.execute("""SELECT 1 FROM system2_wakes w JOIN system2_defer_context c
                    ON c.id=w.event_id WHERE w.user_scope=? AND w.reason=? AND w.occurrence_id IS ?
                    AND w.task_version IS ? AND w.state='acked' AND c.evidence=? AND w.at>?
                    LIMIT 1""", (self.user_scope, reason, occurrence_id, task.version if task else None, encoded,
                                 _stamp(at - _DEFER_LIMIT))).fetchone()
                if repeated:
                    raise SchedulerError("RECHECK_LIMIT", "A recheck already evaluated these facts")
                self._cancel_deferred_waits(tx, purpose, occurrence_id)
                self._cancel_deferred_timers(tx, purpose, occurrence_id, except_event=event_id)
                tx.execute("INSERT OR REPLACE INTO system2_defer_context VALUES (?, ?)", (event_id, encoded))
                valid_until = run_at + _DEFER_LIMIT
            else:
                tx.execute("""UPDATE system2_wakes SET state='cancelled' WHERE user_scope=? AND occurrence_id=?
                    AND task_version=? AND reason='scheduled_recheck' AND at>? AND event_id<>? AND state='pending'""",
                    (self.user_scope, occurrence_id, task.version, _stamp(at), event_id))
            self._enqueue(tx, event_id=event_id, kind="recheck", at=run_at, reason=reason, task=task, valid_until=valid_until)
            tx.execute("UPDATE system2_wakes SET state='pending' WHERE event_id=? AND state='cancelled'", (event_id,))
            return event_id

    def _poll_timers(self, db, now: datetime) -> None:
        schedule = self._schedule(db)
        tick_at = _time(schedule["tick_at"])
        tick_sequence = schedule["tick_sequence"]
        if now < _time(schedule["last_wall"]):
            tick_at = now + TICK_INTERVAL
        if tick_at <= now:
            tick_at += ((now - tick_at) // TICK_INTERVAL) * TICK_INTERVAL
            # A blocked/offline model needs one catch-up tick, not a backlog of idle prompts.
            pending = db.execute("SELECT 1 FROM system2_wakes WHERE user_scope=? AND kind='tick' AND state='pending'",
                                 (self.user_scope,)).fetchone()
            if not pending:
                tick_sequence += 1
                self._enqueue(db, event_id=self._event_id("tick", tick_sequence), kind="tick", at=tick_at, reason="tick")
            tick_at += TICK_INTERVAL
        phase = _phase(db)
        running = _running_focus(phase)
        phase_key = f"{phase.get('id')}:{phase.get('state')}" if phase else None
        stage_changed = schedule["focus_running"] and not running
        if (stage_changed and schedule["phase_key"] != phase_key
                and any(_eligible(task, now) for task in self._tasks(db))):
            self._enqueue(db, event_id=self._event_id("focus_released", schedule["phase_key"], phase_key, _stamp(now)),
                          kind="recheck", at=now, reason="focus_released")
        db.execute("""UPDATE system2_schedule SET tick_at=?,last_wall=?,tick_sequence=?,phase_key=?,focus_running=?
                      WHERE user_scope=?""", (_stamp(tick_at), _stamp(now), tick_sequence, phase_key, running, self.user_scope))
        for task in self._tasks(db):
            draft = task.draft
            if (_soft_lifetime(task, now) and draft.target_at is not None
                    and draft.window_open_at is None and draft.window_close_at is None):
                self._enqueue(db, event_id=self._event_id("window_estimate", _occurrence(task), task.version),
                              kind="recheck", at=now, reason="window_estimate", task=task)
            if not _early(task, now, check_close=False):
                continue
            semantic = semantic_background_enabled()
            if semantic and not _within_user_bounds(task, now):
                continue
            opened_at = task.draft.opens_at or task.confirmed_at or now
            if semantic and draft.explicit_start_at is not None:
                opened_at = max(opened_at, draft.explicit_start_at)
            if _early(task, now):
                self._enqueue(db, event_id=self._event_id("reminder_ready", _occurrence(task), task.version),
                              kind="reminder_ready", at=opened_at, reason="window_open", task=task,
                              valid_until=None if semantic else task.draft.window_close_at)
            close_at = task.draft.window_close_at
            if close_at and close_at <= now:
                self._enqueue(db, event_id=self._event_id("window_close", _occurrence(task), task.version),
                              kind="recheck", at=close_at, reason="window_close", task=task)

    def poll(self) -> None:
        """Advance local wakes without a model or IPC notification."""
        now = self.clock.wall_now()
        with self._transaction() as db:
            self._discard_disabled_wakes(db)
            head = self._sync(db, now)
            self._consume_waits(db, now, head)
            self._poll_timers(db, now)
            self._revoke_stale_deliveries(db, now, head)

    def _discard_disabled_wakes(self, db) -> None:
        if semantic_background_enabled():
            return
        db.execute("""UPDATE system2_wakes AS w SET state='cancelled' WHERE user_scope=? AND state='pending'
            AND (reason IN ('phase_elapsed','presentation_context_changed')
                 OR (kind='recheck' AND reason IN ('focus_suggestion','pet_presentation'))
                 OR EXISTS (SELECT 1 FROM system2_defer_context AS c WHERE c.id=w.event_id OR c.id=w.waiter_id)
                 OR EXISTS (SELECT 1 FROM system2_wake_retries AS r WHERE r.retry_id=w.event_id))""",
                   (self.user_scope,))
        db.execute("""UPDATE system2_waiters AS w SET state='cancelled' WHERE user_scope=? AND state='waiting'
            AND EXISTS (SELECT 1 FROM system2_defer_context AS c WHERE c.id=w.waiter_id)""", (self.user_scope,))

    def _wake_live(self, db, row, now: datetime, settings) -> bool:
        if row["valid_until"] and _time(row["valid_until"]) <= now:
            return False
        if (row["reason"] in _SEMANTIC_WAKE_REASONS and not row["valid_until"]
                and _time(row["at"]) + _ACTIONABLE_WAKE_LIFETIME <= now):
            return False
        if row["reason"] in _DECISION_PURPOSES and row["kind"] == "recheck":
            return (semantic_background_enabled()
                    and (row["reason"] != "focus_suggestion" or settings.auto_adjust))
        if row["waiter_id"]:
            waiter = db.execute("SELECT * FROM system2_waiters WHERE waiter_id=?", (row["waiter_id"],)).fetchone()
            if waiter is None or not self._wait_valid(db, waiter, now, settings)[0]:
                return False
        if row["task_id"]:
            task = self._task(db, row["task_id"])
            eligible = _eligible(task, now) if row["reason"] == "window_open" else _soft_lifetime(task, now)
            return bool(eligible and task.version == row["task_version"] and _occurrence(task) == row["occurrence_id"])
        return True

    def _pending_wakes(self, db, now: datetime, ids: set[str] | None = None) -> tuple[WakeEvent, ...]:
        self._discard_disabled_wakes(db)
        settings = self._settings(db)
        rows = db.execute("""SELECT * FROM system2_wakes WHERE user_scope=? AND state='pending' AND at<=?
            ORDER BY at, rowid""", (self.user_scope, _stamp(now))).fetchall()
        events = []
        for row in rows:
            if ids is not None and row["event_id"] not in ids:
                continue
            if not self._wake_live(db, row, now, settings):
                db.execute("UPDATE system2_wakes SET state='cancelled' WHERE event_id=?", (row["event_id"],))
                continue
            phase_elapsed = None
            if row["reason"] == "phase_elapsed":
                phase = db.execute("SELECT details FROM system2_phase_wakes WHERE event_id=?",
                                   (row["event_id"],)).fetchone()
                phase_elapsed = json.loads(phase[0]) if phase else None
            events.append(WakeEvent(row["event_id"], row["kind"], _time(row["at"]), self.user_scope,
                                    (row["task_id"],) if row["task_id"] else (), row["occurrence_id"],
                                    row["reason"], row["publication_cursor"], row["producer_generation"],
                                    phase_elapsed))
        return tuple(events)

    def pending_wakes(self) -> tuple[WakeEvent, ...]:
        with self._transaction() as db:
            return self._pending_wakes(db, self.clock.wall_now())

    def claim_wakes(self, ids: Iterable[str], *, now: datetime, db=None, coalesce=False) -> tuple[WakeEvent, ...]:
        """Recheck and acknowledge candidates inside the admission write transaction."""
        with self._transaction(db) as tx:
            wakes = self._pending_wakes(tx, now, None if coalesce else set(ids))
            self.ack_wakes((wake.event_id for wake in wakes), db=tx)
            return wakes

    def retry_wakes(self, ids: Iterable[str], *, now: datetime, immediate=False, db=None) -> None:
        """Recover one-shot wakes once, carrying over their original evidence and limits."""
        if not semantic_background_enabled():
            return
        at = now if immediate else now + _WAKE_RETRY_DELAY
        with self._transaction(db) as tx:
            settings = self._settings(tx)
            for event_id in ids:
                row = tx.execute("""
                    SELECT * FROM system2_wakes WHERE event_id=? AND user_scope=? AND state='acked'
                    AND kind IN ('reminder_ready', 'recheck', 'observation_ready')
                    AND NOT EXISTS (SELECT 1 FROM system2_wake_retries WHERE event_id=? OR retry_id=?)
                """, (event_id, self.user_scope, event_id, event_id)).fetchone()
                if row is None or not self._wake_live(tx, row, at, settings):
                    continue
                valid_until = _time(row["valid_until"]) if row["valid_until"] else _time(row["at"]) + _ACTIONABLE_WAKE_LIFETIME
                if at >= valid_until:
                    continue
                retry_id = self._event_id("retry", event_id)
                tx.execute("""INSERT INTO system2_wakes
                    (event_id,user_scope,kind,at,reason,task_id,occurrence_id,task_version,valid_until,
                     waiter_id,producer_generation,publication_cursor)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (retry_id, self.user_scope, row["kind"], _stamp(at), row["reason"], row["task_id"],
                            row["occurrence_id"], row["task_version"], _stamp(valid_until), row["waiter_id"],
                            row["producer_generation"], row["publication_cursor"]))
                tx.execute("INSERT INTO system2_wake_retries VALUES (?,?)", (event_id, retry_id))
                tx.execute("INSERT INTO system2_phase_wakes SELECT ?,details FROM system2_phase_wakes WHERE event_id=?",
                           (retry_id, event_id))
                tx.execute("INSERT INTO system2_defer_context SELECT ?,evidence FROM system2_defer_context WHERE id=?",
                           (retry_id, event_id))

    def recheck_items(self, occurrence_ids: Iterable[str], as_of: datetime) -> list[dict]:
        """Resolve live soft occurrences that caused a wake, even outside their estimate."""
        with self._transaction(write=False) as db:
            items = []
            for occurrence_id in dict.fromkeys(occurrence_ids):
                task = self._resolve(db, occurrence_id)
                if _soft_lifetime(task, as_of):
                    items.append(self._candidate_view(db, task))
            return items

    def ack_wakes(self, ids: Iterable[str], *, db=None) -> None:
        with self._transaction(db) as tx:
            tx.executemany("UPDATE system2_wakes SET state='acked' WHERE user_scope=? AND event_id=? AND state='pending'",
                           ((self.user_scope, event_id) for event_id in ids))

    def snapshot(self, as_of: datetime) -> dict:
        with self._transaction() as db:
            head = self._sync(db, as_of)
            self._consume_waits(db, as_of, head)
            candidates = [task for task in self._tasks(db) if _eligible(task, as_of)]
            if semantic_background_enabled():
                candidates = [task for task in candidates if not self._deadline_owner(db, task, allow_fallback_transfer=True)]
            else:
                candidates.sort(key=lambda task: self._priority(task, as_of))
            items = [self._candidate_view(db, task) for task in candidates]
            return {"eligible_items": items, "opportunity_id": self.get_opportunity(db=db),
                    "producer_generation": head.producer_generation, "publication_cursor": head.publication_cursor,
                    "current_phase": _phase(db), "current_status": head.current_status, "current": head.current,
                    "recent_contacts": self._recent_contacts(db, as_of) if semantic_background_enabled() else []}

    def _candidate_view(self, db, task):
        view = self.items.get(task.task_id, db=db) if self.items else task.model_dump(mode="json")
        if semantic_background_enabled() and db.execute("""SELECT 1 FROM system2_passive_reminders
            WHERE user_scope=? AND task_id=? AND occurrence=?""",
            (self.user_scope, task.task_id, task.occurrence)).fetchone():
            view["passive_available"] = True
        return view

    def passive_reminders(self, now):
        if not semantic_background_enabled():
            return []
        with self._transaction(write=False) as db:
            rows = db.execute("""SELECT m.occurrence, t.view FROM system2_passive_reminders m
                JOIN tasks t ON t.task_id=m.task_id WHERE m.user_scope=?""", (self.user_scope,))
            result = []
            for row in rows:
                task = TaskView.model_validate_json(row["view"])
                if row["occurrence"] == task.occurrence and _within_user_bounds(task, now):
                    result.append({"task_id": task.task_id, "occurrence": task.occurrence})
            return result

    def record_contact(self, *, delivery_id, kind, at, channel, subject_ref=None, occurrence_id=None,
                       exposure=None, response=None, sound_requested=False, db=None):
        timestamp = _stamp(at)
        with self._transaction(db) as tx:
            tx.execute("""INSERT INTO system2_contacts
                (user_scope,delivery_id,kind,at,channel,subject_ref,response,occurrence_id,exposure,response_at,sound_requested)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(user_scope, delivery_id) DO UPDATE SET
                occurrence_id=coalesce(system2_contacts.occurrence_id, excluded.occurrence_id),
                exposure=CASE WHEN excluded.exposure='painted' THEN excluded.exposure
                    ELSE coalesce(system2_contacts.exposure, excluded.exposure) END,
                response=coalesce(excluded.response, system2_contacts.response),
                response_at=CASE WHEN excluded.response=system2_contacts.response
                    THEN coalesce(system2_contacts.response_at, excluded.response_at)
                    ELSE coalesce(excluded.response_at, system2_contacts.response_at) END,
                sound_requested=max(excluded.sound_requested, system2_contacts.sound_requested)""",
                (self.user_scope, delivery_id, kind, timestamp, channel, subject_ref, response,
                 occurrence_id, exposure, timestamp if response else None, int(sound_requested)))

    def _recent_contacts(self, db, as_of):
        return recent_contacts(db, self.user_scope, as_of)

    @staticmethod
    def _urgency(task: TaskView, now: datetime) -> str | None:
        return task.draft.urgency_reason if task.draft.urgency_until and now < task.draft.urgency_until else None

    def _priority(self, task: TaskView, now: datetime) -> tuple:
        far_future = datetime.max.replace(tzinfo=UTC)
        deadline = min((value for value in (task.draft.hard_deadline, task.draft.expires_at,
                                             task.draft.urgency_until) if value and now < value), default=far_future)
        return (deadline, not bool(self._urgency(task, now)), task.confirmed_at or task.created_at or now, task.task_id)

    @staticmethod
    def _presentation_owner(db, task: TaskView, *, allow_fallback_transfer=False):
        deadline = task.draft.hard_deadline
        transferable = allow_fallback_transfer and task.draft.mode == "soft" and deadline is not None
        return db.execute("""SELECT delivery_id, channel FROM presentations WHERE task_id=?
            AND (occurrence=? OR (channel='os_notification' AND state IN
            ('submitting','scheduled','cancel_intent','cancelling')))
            AND state NOT IN ('cancelled','failed','released','unavailable','grouped')
            AND NOT (? AND occurrence=? AND deadline IS ? AND channel='os_notification' AND state='scheduled')
            ORDER BY occurrence=? DESC LIMIT 1""",
            (task.task_id, task.occurrence, transferable, task.occurrence, deadline.isoformat() if deadline else None,
             task.occurrence)).fetchone()

    def _deadline_owner(self, db, task, *, allow_fallback_transfer=False):
        return self._presentation_owner(db, task, allow_fallback_transfer=allow_fallback_transfer) is not None or db.execute("""
            SELECT 1 FROM deliveries
            WHERE task_id=? AND task_version=? AND occurrence=? AND active=1""",
            (task.task_id, task.version, task.occurrence)).fetchone() is not None

    def _time_sensitive(self, task: TaskView, now: datetime) -> bool:
        if self._urgency(task, now):
            return True
        # Keep deadline prioritization independent of the idle-thinking cadence.
        return any(edge and now < edge <= now + REMINDER_PRIORITY_HORIZON
                   for edge in (task.draft.hard_deadline, task.draft.expires_at))

    def has_time_sensitive_candidate(self, *, now: datetime, db=None) -> bool:
        with self._transaction(db, write=False) as tx:
            return any(_early(task, now) and self._time_sensitive(task, now) and not self._deadline_owner(tx, task)
                       for task in self._tasks(tx))

    def claim_opportunity(self, *, opportunity_id: str, delivery_id: str, now: datetime, db=None) -> bool:
        if not opportunity_id or not delivery_id:
            return False
        with self._transaction(db) as tx:
            head = self._sync(tx, now)
            if head.current is None or head.current.state != "available":
                return False
            existing = tx.execute("SELECT opportunity_id FROM system2_opportunities WHERE claimed_delivery_id=?",
                                  (delivery_id,)).fetchone()
            if existing is not None and existing[0] != opportunity_id:
                return False
            return tx.execute("""UPDATE system2_opportunities SET claimed_delivery_id=? WHERE user_scope=?
                AND opportunity_id=? AND closed_at IS NULL AND (claimed_delivery_id IS NULL OR claimed_delivery_id=?)""",
                (delivery_id, self.user_scope, opportunity_id, delivery_id)).rowcount == 1

    def release_opportunity(self, *, delivery_id: str, db=None) -> None:
        """Release only after a caller has proved the intent was revoked before effect."""
        with self._transaction(db) as tx:
            tx.execute("UPDATE system2_opportunities SET claimed_delivery_id=NULL WHERE user_scope=? AND claimed_delivery_id=?",
                       (self.user_scope, delivery_id))

    @staticmethod
    def _matches_context(head, now, revision, valid_until=None, cursor=None, generation=None) -> bool:
        return bool(head.current and head.producer_generation is not None and head.publication_cursor is not None
                    and head.current.context_revision == revision
                    and (valid_until is None or now < valid_until)
                    and (cursor is None or head.publication_cursor >= cursor)
                    and (generation is None or head.producer_generation == generation))

    def _route_version(self, db) -> str:
        row = None
        if _table(db, "observation_cloud_routes"):
            row = db.execute("""SELECT route_generation, consent_generation, permission, verified
                FROM observation_cloud_routes WHERE user_scope=? AND role=?""",
                (self.user_scope, SYSTEM2)).fetchone()
        return json.dumps(tuple(row) if row is not None else None)

    def _material_change(self, head, now, revision, cursor, generation) -> bool:
        if (head.current is None or not generation or head.producer_generation != generation
                or cursor is None or head.publication_cursor is None or cursor >= head.publication_cursor):
            return False
        check = getattr(self.observation, "presentation_context_changed", None)
        if check is None:
            return False
        try:
            return check(cursor, head.publication_cursor, revision=revision, generation=generation, now=now)
        except (sqlite3.Error, OSError, ValueError):
            return False

    def _context_recheck(self, db, *, presentation_kind, task, cursor, generation, now):
        # Repeated renders and action retries from the same publication can
        # request one new judgment. A reminder remains bound to its occurrence.
        subject = (_occurrence(task), task.version) if task else ()
        event_id = self._event_id("presentation_changed", presentation_kind, *subject, generation, cursor)
        self._enqueue(db, event_id=event_id, kind="recheck", at=now, reason="presentation_context_changed",
                      task=task, valid_until=now + _ACTIONABLE_WAKE_LIFETIME)

    def _presentation_error(self, db, *, now, presentation_kind, channel, context_revision,
                            observation_cursor, observation_generation, context_valid_until,
                            recheck_key, capture, task=None, head=None):
        if context_valid_until is not None and now >= context_valid_until:
            return "EXPIRED_REQUEST"
        settings = self._settings(db)
        if task is not None and not _within_user_bounds(task, now):
            return "OUTSIDE_WINDOW"
        previous = None if capture or recheck_key is None else db.execute("""
            SELECT * FROM system2_presentation_context WHERE delivery_id=? AND user_scope=?
            """, (recheck_key, self.user_scope)).fetchone()
        if channel == "os_notification":
            if not settings.allow_system_notifications:
                return "DISABLED"
        else:
            locked = self._lock_state()
            if locked is not False:
                if locked is True and previous is not None:
                    self._context_recheck(db, presentation_kind=presentation_kind, task=task,
                                          cursor=previous["publication_cursor"], generation="locked", now=now)
                return "STALE_CONTEXT"
        route_version = self._route_version(db)
        if previous is not None:
            if now >= _time(previous["valid_until"]):
                return "EXPIRED_REQUEST"
            if (previous["channel"] != channel or previous["route_version"] != route_version
                    or previous["policy_generation"] != settings.observation_generation):
                return "STALE_CONTEXT"
            context_revision = previous["context_revision"]
            observation_cursor = previous["publication_cursor"]
            observation_generation = previous["observation_generation"]
        if head is None:
            head = self._sync(db, now)
        if self._material_change(head, now, context_revision, observation_cursor, observation_generation):
            self._context_recheck(db, presentation_kind=presentation_kind, task=task,
                                  cursor=head.publication_cursor, generation=head.producer_generation, now=now)
            return "STALE_CONTEXT"
        if capture and recheck_key:
            valid_until = context_valid_until or now + _PRESENTATION_LEASE
            db.execute("""INSERT OR REPLACE INTO system2_presentation_context
                VALUES (?,?,?,?,?,?,?,?,?)""", (recheck_key, self.user_scope, channel, observation_generation,
                                               observation_cursor, context_revision, settings.observation_generation,
                                               route_version, _stamp(valid_until)))
        return None

    def presentation_permit(self, *, now, presentation_kind, channel="qt", db=None, context_revision=None,
                            observation_cursor=None, observation_generation=None, context_valid_until=None,
                            recheck_key=None, capture=False) -> bool:
        with self._transaction(db) as tx:
            return self._presentation_error(
                tx, now=now, presentation_kind=presentation_kind, channel=channel, context_revision=context_revision,
                observation_cursor=observation_cursor, observation_generation=observation_generation,
                context_valid_until=context_valid_until, recheck_key=recheck_key, capture=capture,
            ) is None

    def authorize_proposal(self, *, context_revision: int | None, now: datetime, db=None,
                           context_valid_until=None, observation_cursor=None, observation_generation=None,
                           recheck_key=None) -> bool:
        with self._transaction(db) as tx:
            if semantic_background_enabled():
                return self._settings(tx).auto_adjust and self.presentation_permit(
                    now=now, presentation_kind="proposal", context_revision=context_revision,
                    context_valid_until=context_valid_until, observation_cursor=observation_cursor,
                    observation_generation=observation_generation, recheck_key=recheck_key, capture=True, db=tx,
                )
            head = self._sync(tx, now)
            if (head.current_status != "fresh" or not head.current or head.current.state not in {"working", "available"}
                    or (context_valid_until is not None and now >= context_valid_until)
                    or not self._settings(tx).auto_adjust or self.has_time_sensitive_candidate(now=now, db=tx)):
                return False
            if self._matches_context(head, now, context_revision, context_valid_until,
                                     observation_cursor, observation_generation):
                return True
            if not observation_generation or observation_generation != head.producer_generation:
                return False
            return self.observation.same_published_state(
                observation_cursor, head.publication_cursor, revision=context_revision,
                state=head.current.state, generation=observation_generation,
            )

    def authorize_pet(self, *, context_revision: int | None, now: datetime, db=None,
                      context_valid_until=None, observation_cursor=None, observation_generation=None) -> str | None:
        with self._transaction(db) as tx:
            head = self._sync(tx, now)
            self._revoke_stale_deliveries(tx, now, head)
            if (not self._matches_context(head, now, context_revision, context_valid_until,
                                          observation_cursor, observation_generation)
                    or head.current.state != "available" or _running_focus(_phase(tx))
                    or self.has_time_sensitive_candidate(now=now, db=tx)):
                return None
            return self.get_opportunity(db=tx)

    def _visible_permit(self, db, now, opportunity, delivery, revision, valid_until, cursor, generation, *, proposal):
        head = self._sync(db, now)
        row = self._current_opportunity(db)
        return bool(self._matches_context(head, now, revision, valid_until, cursor, generation)
                    and head.current.state == "available" and row and row["opportunity_id"] == opportunity
                    and row["claimed_delivery_id"] == delivery
                    and (self._settings(db).auto_adjust if proposal else not _running_focus(_phase(db))))

    def proposal_permit(self, *, opportunity_id: str, delivery_id: str, context_revision: int,
                        context_valid_until: datetime, now: datetime, db=None,
                        observation_cursor=None, observation_generation=None) -> bool:
        with self._transaction(db) as tx:
            if semantic_background_enabled():
                return self._settings(tx).auto_adjust and self.presentation_permit(
                    now=now, presentation_kind="proposal", context_revision=context_revision,
                    context_valid_until=context_valid_until, observation_cursor=observation_cursor,
                    observation_generation=observation_generation, recheck_key=delivery_id, db=tx,
                )
            if not opportunity_id:
                return self.authorize_proposal(
                    context_revision=context_revision, context_valid_until=context_valid_until, now=now, db=tx,
                    observation_cursor=observation_cursor, observation_generation=observation_generation,
                )
            # Presentations queued by older builds retain their original slot binding.
            return self._visible_permit(tx, now, opportunity_id, delivery_id, context_revision,
                                        context_valid_until, observation_cursor, observation_generation, proposal=True)

    def pet_permit(self, *, opportunity_id: str, delivery_id: str, context_revision: int,
                   context_valid_until: datetime, now: datetime, db=None,
                   observation_cursor=None, observation_generation=None) -> bool:
        with self._transaction(db) as tx:
            return self._visible_permit(tx, now, opportunity_id, delivery_id, context_revision,
                                        context_valid_until, observation_cursor, observation_generation, proposal=False)

    def _authorize_reminder(self, db, *, occurrence_id, context_revision, reason_code, now, head):
        task = self._resolve(db, occurrence_id)
        if not _early(task, now):
            raise SchedulerError("STALE_TASK", "This occurrence is outside the active reminder window")
        current = head.current
        if not self._matches_context(head, now, context_revision) or current.state not in {"working", "available"}:
            raise SchedulerError("STALE_CONTEXT", "A matching, fresh observation is required")
        low = current.state == "working" or _running_focus(_phase(db))
        urgency = self._urgency(task, now)
        semantic_reason = reason_code in _URGENCY_CODES or reason_code == urgency
        if low and not (urgency and semantic_reason):
            raise SchedulerError("STALE_CONTEXT", "A time-sensitive reason is required to interrupt work or focus")
        opportunity = self.get_opportunity(db=db) if current.state == "available" else None
        if current.state == "available" and not opportunity:
            raise SchedulerError("STALE_CONTEXT", "The interruption opportunity has closed")
        draft = task.draft
        until = min(value for value in (current.valid_until, draft.opportunity_close_at,
                                        draft.urgency_until if low else None) if value is not None)
        return ReminderAuthorization(head, task, opportunity, until, low, urgency)

    def authorize_reminder(self, *, occurrence_id: str, context_revision: int | None, reason_code: str,
                           now: datetime, db=None) -> ReminderAuthorization:
        with self._transaction(db) as tx:
            head = self._sync(tx, now)
            if semantic_background_enabled():
                return self._semantic_reminder(tx, occurrence_id, now, head)
            return self._authorize_reminder(tx, occurrence_id=occurrence_id, context_revision=context_revision,
                                            reason_code=reason_code, now=now, head=head)

    def _semantic_reminder(self, db, occurrence_id, now, head):
        task = self._resolve(db, occurrence_id)
        if not _soft_lifetime(task, now):
            raise SchedulerError("STALE_TASK", "The soft reminder occurrence is no longer active")
        earliest = task.draft.explicit_start_at
        if earliest is not None and now < earliest:
            raise SchedulerError("OUTSIDE_WINDOW", "The reminder is earlier than the time the user allowed")
        until = min(value for value in (now + _PRESENTATION_LEASE, task.draft.hard_deadline, task.draft.expires_at)
                    if value is not None)
        return ReminderAuthorization(head, task, None, until, False, self._urgency(task, now))

    @staticmethod
    def _deny(code: str) -> dict:
        return {"status": "rejected", "error_code": code}

    def present(self, *, occurrence_ids, presentation, reason_code, authority, now: datetime, db=None) -> dict:
        if len(occurrence_ids) != 1 or not isinstance(reason_code, str) or not reason_code.strip():
            return self._deny("INVALID_ARGUMENT")
        options = presentation if isinstance(presentation, Mapping) else {}
        semantic = semantic_background_enabled()
        level = options.get("level")
        if level not in (None, "quiet", "passive", "actionable", "enhanced"):
            return self._deny("INVALID_ARGUMENT")
        if level == "quiet":
            return {"status": "applied", "outcome": "quiet", "interruption_level": level}
        if level == "passive" and (not semantic or options.get("channel") not in (None, "pet_badge")):
            return self._deny("UNSUPPORTED_CHANNEL")
        supported_channels = {"pet_card", "pet_badge", "os_notification"} if semantic else {"pet_card"}
        channel = options.get("channel") or (presentation if presentation in ("os_notification", "pet_badge") else "pet_card")
        if (not isinstance(channel, str) or channel not in supported_channels
                or (level in {"actionable", "enhanced"} and channel == "pet_badge")):
            return self._deny("UNSUPPORTED_CHANNEL")
        with self._transaction(db) as tx:
            context_revision = getattr(authority, "context_revision", None)
            cursor = getattr(authority, "observation_cursor", None)
            generation = getattr(authority, "observation_generation", None)
            try:
                grant = self.authorize_reminder(occurrence_id=occurrence_ids[0], context_revision=context_revision,
                                                reason_code=reason_code, now=now, db=tx)
            except SchedulerError as error:
                return self._deny(error.code)
            authority_limit = getattr(authority, "expires_at" if semantic else "observation_valid_until", None)
            if not semantic and not self._matches_context(grant.head, now, context_revision, authority_limit,
                                                          cursor, generation):
                return self._deny("STALE_CONTEXT")
            valid_until = min(grant.valid_until, authority_limit) if authority_limit else grant.valid_until
            task = grant.task
            if level == "passive":
                # Updating the menu must not claim or acknowledge the occurrence's primary delivery.
                tx.execute("""INSERT INTO system2_passive_reminders (user_scope, task_id, occurrence) VALUES (?,?,?)
                    ON CONFLICT(user_scope, task_id) DO UPDATE SET occurrence=excluded.occurrence
                    WHERE occurrence<>excluded.occurrence""",
                    (self.user_scope, task.task_id, task.occurrence))
                return {"status": "applied", "outcome": "committed", "subject_ref": task.task_id,
                        "interruption_level": level, "view": {"channel": "reminder_menu"},
                        "presentation_receipt": {"channel": "reminder_menu", "state": "available"}}
            self._revoke_stale_deliveries(tx, now, grant.head)
            delivery_id = f"delivery:{task.task_id}:{task.occurrence}:v{task.version}"
            owner = self._presentation_owner(tx, task, allow_fallback_transfer=semantic)
            if owner is not None:
                return {"status": "pending", "delivery_id": owner["delivery_id"], "subject_ref": task.task_id,
                        "view": {"channel": owner["channel"]}}
            existing = tx.execute("SELECT command FROM deliveries WHERE delivery_id=? AND active=1",
                                  (delivery_id,)).fetchone()
            if existing is not None:
                queued = DisplayCommand.model_validate_json(existing["command"])
                return {"status": "pending", "delivery_id": delivery_id, "subject_ref": task.task_id,
                        "view": {"channel": queued.channel}}
            if semantic:
                error = self._presentation_error(
                    tx, now=now, presentation_kind="reminder", channel=channel, context_revision=context_revision,
                    observation_cursor=cursor, observation_generation=generation, context_valid_until=valid_until,
                    recheck_key=delivery_id, capture=True, task=task, head=grant.head,
                )
                if error:
                    result = self._deny(error)
                    if error == "STALE_CONTEXT":
                        result.update(outcome="deferred", retryable=True)
                    return result
            if grant.opportunity_id:
                if (grant.opportunity_id != getattr(authority, "opportunity_id", None)
                        or (not self._time_sensitive(task, now) and self.has_time_sensitive_candidate(now=now, db=tx))
                        or not self.claim_opportunity(opportunity_id=grant.opportunity_id, delivery_id=delivery_id, now=now, db=tx)):
                    return self._deny("STALE_CONTEXT")
            low = grant.low_distraction or options.get("interruption") == "low" or presentation in ("low", "side_card")
            position = "screen_edge" if low else options.get("position_hint")
            if position not in ("stay", "home", "screen_edge", None):
                position = None
            mover = options.get("animation")
            animation = mover if not low and mover in ("idle", "look_up", "wave", "approach") else None
            settings = self._settings(tx)
            request_sound = options.get("sound") in (True, "reminder")
            if level == "actionable":
                request_sound = False
            elif level == "enhanced":
                request_sound = options.get("sound") is not False
            sound = ("reminder" if not low and channel != "pet_badge" and settings.reminder_sound
                     and task.draft.sound is not False
                     and request_sound else None)
            if semantic:
                if settings.reduce_motion or not settings.allow_proactive_movement or channel != "pet_card":
                    position = "stay"
                if settings.reduce_motion or channel != "pet_card":
                    animation = None
            command = DisplayCommand(delivery_id=delivery_id, task_id=task.task_id, task_version=task.version,
                                     occurrence=task.occurrence, content=task.draft.content, requested_at=now,
                                     expires_at=valid_until, reason="tool", context_revision=context_revision,
                                     context_valid_until=valid_until, position_hint=position, sound=sound,
                                     animation=animation, channel=channel)
            tx.execute("""INSERT OR REPLACE INTO deliveries
                (delivery_id,task_id,task_version,occurrence,command,status,attempts,last_attempt_at,owner,
                last_attempt_mono,receipt,active) VALUES (?,?,?,?,?,'pending',1,?,?,?,NULL,1)""", (
                delivery_id, task.task_id, task.version, task.occurrence, command.model_dump_json(),
                now.isoformat(), "system2", self.clock.monotonic_now(),
            ))
            tx.execute("""INSERT OR REPLACE INTO system2_reminder_authority VALUES (?,?,?,?,?,?,?,?,?,?,'pending')""", (
                delivery_id, self.user_scope, grant.opportunity_id, grant.head.producer_generation or "",
                grant.head.publication_cursor or 0, context_revision or 0, _stamp(valid_until), reason_code,
                grant.urgency_reason, low,
            ))
            tx.execute("""INSERT INTO outbox(kind,task_id,task_version,delivery_id,not_before)
                          VALUES ('show',?,?,?,?)""", (task.task_id, task.version, delivery_id, now.isoformat()))
            tx.execute("INSERT INTO events(recorded_at,kind,fields) VALUES (?, 'delivery.requested', ?)", (
                now.isoformat(), json.dumps({"task_id": task.task_id, "task_version": task.version, "delivery_id": delivery_id,
                    "payload": {"reason": reason_code, "window_open_at": str(task.draft.opens_at),
                                "window_close_at": str(task.draft.window_close_at),
                                "observation_state": grant.head.current.state if grant.head.current else "unknown",
                                "channel": channel, "style": "low" if low else "ordinary", "occurrence": task.occurrence,
                                **({"interruption_level": level} if level else {})}}),
            ))
            return {"status": "pending", "delivery_id": delivery_id, "subject_ref": task.task_id,
                    "view": {"channel": channel}, **({"interruption_level": level} if level else {})}

    def _still_authorized(self, db, authority, task, command, now, head) -> bool:
        if authority["state"] == "revoked" or _time(authority["valid_until"]) <= now:
            return False
        semantic = db.execute("SELECT 1 FROM system2_presentation_context WHERE delivery_id=? AND user_scope=?",
                              (authority["delivery_id"], self.user_scope)).fetchone()
        if semantic:
            return semantic_background_enabled() and self._presentation_error(
                db, now=now, presentation_kind="reminder", channel=command.channel,
                context_revision=None, observation_cursor=None, observation_generation=None,
                context_valid_until=_time(authority["valid_until"]), recheck_key=authority["delivery_id"],
                capture=False, task=task, head=head,
            ) is None
        if (head.producer_generation != authority["producer_generation"]
                or head.publication_cursor is None or head.publication_cursor < authority["publication_cursor"]):
            return False
        try:
            grant = self._authorize_reminder(db, occurrence_id=_occurrence(task), now=now, head=head,
                                             context_revision=authority["context_revision"], reason_code=authority["reason_code"])
        except SchedulerError:
            return False
        if (grant.opportunity_id != authority["opportunity_id"] or grant.urgency_reason != authority["urgency_reason"]
                or (grant.low_distraction and not authority["low_distraction"])):
            return False
        opportunity = self._current_opportunity(db) if grant.opportunity_id else None
        return not grant.opportunity_id or (opportunity and opportunity["claimed_delivery_id"] == authority["delivery_id"])

    def authorize_qt(self, db, command: DisplayCommand, now: datetime) -> bool:
        """Reuse admission-time policy in the transaction that claims the presentation channel."""
        row = db.execute("SELECT * FROM system2_reminder_authority WHERE delivery_id=? AND user_scope=?",
                         (command.delivery_id, self.user_scope)).fetchone()
        task = self._task(db, command.task_id)
        if row is None or task is None or task.version != command.task_version or task.occurrence != command.occurrence:
            return False
        head = self._sync(db, now)
        return bool(self._still_authorized(db, row, task, command, now, head))

    def _revoke_stale_deliveries(self, db, now, head):
        if not _table(db, "deliveries"):
            return
        rows = db.execute("""SELECT a.*,d.task_id,d.task_version,d.occurrence,d.command,d.active,d.status AS delivery_status,
            p.channel AS owner_channel,p.state AS owner_state
            FROM system2_reminder_authority a JOIN deliveries d ON a.delivery_id=d.delivery_id
            LEFT JOIN presentations p ON p.delivery_id=a.delivery_id
            WHERE a.user_scope=? AND a.state='pending'""", (self.user_scope,)).fetchall()
        for row in rows:
            delivery_id = row["delivery_id"]
            os_owned = row["owner_channel"] == "os_notification"
            command = DisplayCommand.model_validate_json(row["command"])
            early = command.reason in _EARLY_REASONS
            if row["delivery_status"] == "displayed" or row["owner_state"] in {"painted", "badge_shown", "delivered"}:
                db.execute("UPDATE system2_reminder_authority SET state='presented' WHERE delivery_id=?", (delivery_id,))
                continue
            task = self._task(db, row["task_id"])
            os_state = row["owner_state"] if os_owned else None
            fallback_transfer = (task is not None and task.draft.hard_deadline is not None
                                 and now < task.draft.hard_deadline
                                 and (os_state in {"scheduled", "cancel_intent", "cancelling"}
                                      or (os_state == "submitting" and command.channel != "os_notification")))
            if (os_owned and command.channel == "os_notification" and not fallback_transfer
                    and os_state in {"submitting", "scheduled", "submitted", "unknown",
                                     "cancel_intent", "cancelling"}):
                continue
            if (early and (not os_owned or command.channel == "os_notification" or fallback_transfer)
                    and row["active"]
                    and task and task.version == row["task_version"]
                    and task.occurrence == row["occurrence"]
                    and self._still_authorized(db, row, task, command, now, head)):
                continue
            if not os_owned and self._effect_started(db, delivery_id):
                continue
            db.execute("UPDATE system2_reminder_authority SET state='revoked' WHERE delivery_id=?", (delivery_id,))
            if early:
                db.execute("DELETE FROM outbox WHERE delivery_id=? AND kind='show'", (delivery_id,))
                db.execute("UPDATE deliveries SET active=0 WHERE delivery_id=?", (delivery_id,))
            self.release_opportunity(delivery_id=delivery_id, db=db)

    @staticmethod
    def _effect_started(db, delivery_id: str) -> bool:
        row = db.execute("SELECT state FROM presentations WHERE delivery_id=?", (delivery_id,)).fetchone()
        return bool(row and row["state"] not in {"released", "failed", "cancelled", "unavailable", "grouped"})
