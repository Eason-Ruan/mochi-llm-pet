"""SQLite-backed focus timer shared by manual, chat, proposal, and habit actions."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

import psutil
from pydantic import ValidationError

from deskpet.contracts import ErrorCode, SessionCommand, SessionResult, WorkSession

from .models import (
    AdjustInput, CreditRecoveryInput, CreditSleepInput, DetailsInput, FocusInterval, HabitRun,
    LinksInput, PhaseCommand, PhaseResult, PhaseView, ReassignInput, RecoveryInterval,
    ReviewInput, StartInput,
)

PHASE_TITLES = {"focus": "专注", "short_break": "短休息", "long_break": "长休息"}
CHECKPOINT_SECONDS = 5


@dataclass(frozen=True)
class Tick:
    wall: datetime
    mono: float


@dataclass(frozen=True)
class Checkpoint:
    id: str
    phase_id: str
    boot_id: str
    owner_session_id: str
    owner_pid: int
    owner_birth: float
    wall_at: str
    monotonic_s: float
    actual_focus_s: float
    remaining_s: float

    @property
    def wall(self) -> datetime:
        return datetime.fromisoformat(self.wall_at)


class Rejected(Exception):
    def __init__(self, code: ErrorCode):
        self.code = code


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        try:
            return str(psutil.boot_time())
        except OSError:
            # Some desktop sandboxes hide the boot timestamp. PID and creation time
            # still distinguish the process that owns a checkpoint from its restart.
            return "unavailable"


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS phase_schema (version INTEGER NOT NULL)",
    """CREATE TABLE IF NOT EXISTS phase_views (
        id TEXT PRIMARY KEY, active INTEGER NOT NULL CHECK (active IN (0, 1)), view TEXT NOT NULL
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS phase_only_one_active ON phase_views(active) WHERE active = 1",
    """CREATE TABLE IF NOT EXISTS phase_commands (
        id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS phase_checkpoints (
        phase_id TEXT PRIMARY KEY REFERENCES phase_views(id), id TEXT NOT NULL UNIQUE,
        boot_id TEXT NOT NULL, owner_session_id TEXT NOT NULL,
        owner_pid INTEGER NOT NULL, owner_birth REAL NOT NULL,
        wall_at TEXT NOT NULL, monotonic_s REAL NOT NULL,
        actual_focus_s REAL NOT NULL, remaining_s REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS phase_intervals (
        id TEXT PRIMARY KEY, phase_id TEXT NOT NULL REFERENCES phase_views(id),
        is_open INTEGER NOT NULL CHECK (is_open IN (0, 1)), interval TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS phase_intervals_by_phase ON phase_intervals(phase_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS phase_one_open_interval ON phase_intervals(is_open) WHERE is_open = 1",
    """CREATE TABLE IF NOT EXISTS phase_recovery (
        id TEXT PRIMARY KEY, phase_id TEXT NOT NULL REFERENCES phase_views(id), interval TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS phase_defaults (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        focus_s REAL NOT NULL, break_s REAL NOT NULL, link_issues INTEGER NOT NULL
    )""",
    "CREATE TABLE IF NOT EXISTS phase_habit_runs (id TEXT PRIMARY KEY, view TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS phase_habit_completed (phase_id TEXT PRIMARY KEY REFERENCES phase_views(id))",
)


class PersistentWorkSessionService:
    def __init__(self, *, path, clock, emit=None, default_focus_s=1500, default_break_s=300,
                 settings_store=None):
        self.clock = clock
        self.emit = emit or (lambda *args, **kwargs: None)
        self.settings_store = settings_store
        self.owner_session_id = uuid4().hex
        self.owner_pid = os.getpid()
        self.owner_birth = psutil.Process().create_time()
        self.boot_id = _boot_id()
        self._lock = threading.RLock()
        self._closed = False
        self._sleeping = False
        self._pending_sleep_recovery: str | None = None
        defaults = self._validate_defaults(default_focus_s, default_break_s)
        if path != ":memory:":
            path = Path(path).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, isolation_level=None, timeout=5, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        try:
            with self._write() as db:
                for sql in _SCHEMA:
                    db.execute(sql)
                version = db.execute("SELECT version FROM phase_schema").fetchone()
                if version is None:
                    db.execute("INSERT INTO phase_schema VALUES (1)")
                elif version["version"] != 1:
                    raise RuntimeError(f"unsupported focus schema version {version['version']}")
                db.execute("INSERT OR IGNORE INTO phase_defaults VALUES (1, ?, ?, 1)", defaults)
                self._refresh(db, self._tick(), allow_automatic=False)
        except BaseException:
            self._db.close()
            raise

    @contextmanager
    def _write(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def _tick(self):
        wall = self.clock.wall_now()
        if wall.utcoffset() is None:
            raise ValueError("focus clock requires an aware wall timestamp")
        return Tick(wall.astimezone(timezone.utc), self.clock.monotonic_now())

    @staticmethod
    def _validate_defaults(focus_s, break_s):
        if focus_s is None or break_s is None:
            raise ValueError("focus and break defaults are required")
        return tuple(
            StartInput(planned_duration_s=value).planned_duration_s for value in (focus_s, break_s)
        )

    def attach_settings(self, settings_store):
        self.settings_store = settings_store

    def configure_defaults(self, *, default_focus_s, default_break_s, link_issues=True):
        focus_s, break_s = self._validate_defaults(default_focus_s, default_break_s)
        with self._write() as db:
            self._update_defaults(db, focus_s, break_s, link_issues)

    def apply_settings_update(self, db, current, updated):
        """Apply a settings change under the settings store's commit boundary."""
        if not db.in_transaction:
            raise ValueError("a write transaction is required to apply focus settings")
        if (current.default_focus_s, current.default_break_s, current.link_issues) != (
            updated.default_focus_s, updated.default_break_s, updated.link_issues
        ):
            self._update_defaults(db, updated.default_focus_s, updated.default_break_s, updated.link_issues)

    def _update_defaults(self, db, focus_s, break_s, link_issues):
        db.execute("UPDATE phase_defaults SET focus_s=?, break_s=?, link_issues=?",
                   (focus_s, break_s, bool(link_issues)))
        phase = self._active(db)
        if not link_issues and phase and phase.kind == "focus" and phase.issue_links:
            tick = self._tick()
            current = self._refresh(db, tick, force=True)
            if current and current.id == phase.id:
                self._set_links(db, current, LinksInput(), tick)

    def _defaults(self, db):
        focus_s, break_s, link_issues = db.execute(
            "SELECT focus_s, break_s, link_issues FROM phase_defaults WHERE singleton=1"
        ).fetchone()
        if self.settings_store is not None:
            settings = self.settings_store.read(db=db)
            focus_s, break_s, link_issues = (
                settings.default_focus_s, settings.default_break_s, settings.link_issues
            )
        return {"default_focus_s": focus_s, "default_break_s": break_s, "link_issues": bool(link_issues)}

    @staticmethod
    def _get(db, phase_id):
        row = db.execute("SELECT view FROM phase_views WHERE id=?", (phase_id,)).fetchone()
        return PhaseView.model_validate_json(row[0]) if row else None

    @staticmethod
    def _active(db):
        row = db.execute("SELECT view FROM phase_views WHERE active=1").fetchone()
        return PhaseView.model_validate_json(row[0]) if row else None

    @staticmethod
    def _latest_phase_id(db):
        row = db.execute("SELECT id FROM phase_views ORDER BY rowid DESC LIMIT 1").fetchone()
        return row[0] if row else None

    @staticmethod
    def _save(db, phase, *, active):
        db.execute("""INSERT INTO phase_views VALUES (?, ?, ?)
                      ON CONFLICT(id) DO UPDATE SET active=excluded.active, view=excluded.view""",
                   (phase.id, active, phase.model_dump_json()))
        return phase

    @staticmethod
    def _checkpoint(db, phase_id):
        row = db.execute("""SELECT id, phase_id, boot_id, owner_session_id, owner_pid, owner_birth,
                                   wall_at, monotonic_s, actual_focus_s, remaining_s
                            FROM phase_checkpoints WHERE phase_id=?""", (phase_id,)).fetchone()
        return Checkpoint(*row) if row else None

    def _save_checkpoint(self, db, phase, tick, previous=None):
        db.execute(
            "INSERT OR REPLACE INTO phase_checkpoints VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (phase.id, uuid4().hex, self.boot_id,
             previous.owner_session_id if previous else self.owner_session_id,
             previous.owner_pid if previous else self.owner_pid,
             previous.owner_birth if previous else self.owner_birth,
             tick.wall.isoformat(), tick.mono, phase.actual_focus_s, phase.remaining_s),
        )

    def _owner_alive(self, checkpoint):
        if checkpoint.boot_id != self.boot_id:
            return False
        if checkpoint.owner_pid == self.owner_pid:
            return checkpoint.owner_birth == self.owner_birth
        try:
            owner = psutil.Process(checkpoint.owner_pid)
            return owner.create_time() == checkpoint.owner_birth and owner.status() != psutil.STATUS_ZOMBIE
        except psutil.AccessDenied:
            return True
        except psutil.NoSuchProcess:
            return False

    @staticmethod
    def _open_interval(db, phase_id):
        row = db.execute(
            "SELECT interval FROM phase_intervals WHERE phase_id=? AND is_open=1", (phase_id,)
        ).fetchone()
        return FocusInterval.model_validate_json(row[0]) if row else None

    @staticmethod
    def _store_interval(db, interval):
        db.execute(
            """INSERT INTO phase_intervals VALUES (?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET is_open=excluded.is_open, interval=excluded.interval""",
            (interval.id, interval.phase_id, interval.ended_at is None, interval.model_dump_json()),
        )

    def _begin_interval(self, db, phase, tick):
        self._store_interval(db, FocusInterval(
            id=uuid4().hex, phase_id=phase.id, started_at=tick.wall,
            primary_issue_id=phase.primary_issue_id, origin=phase.source,
        ))
        self._save_checkpoint(db, phase, tick)

    def _update_interval(self, db, phase_id, delta=0, *, close=False):
        interval = self._open_interval(db, phase_id)
        if interval:
            seconds = interval.seconds + delta
            interval = interval.model_copy(update={
                "seconds": seconds,
                "ended_at": interval.started_at + timedelta(seconds=seconds) if close else None,
            })
            self._store_interval(db, interval)
        return interval

    def _refresh(self, db, tick, *, force=False, allow_automatic=True):
        allow_automatic = allow_automatic and not self._sleeping
        phase = self._active(db)
        if phase is None or phase.state != "running":
            return phase
        if phase.kind != "focus":
            remaining = min(phase.remaining_s, max(0.0, (phase.phase_due_at - tick.wall).total_seconds()))
            if remaining > 0:
                return phase.model_copy(update={"remaining_s": remaining})
            elapsed = phase.model_copy(update={"state": "elapsed", "remaining_s": 0,
                "ended_at": phase.phase_due_at, "phase_due_at": None, "version": phase.version + 1})
            self._save(db, elapsed, active=False)
            successor = self._automatic_follow_on(db, elapsed, tick) if allow_automatic else None
            if successor is None:
                self._change_run_state(db, elapsed, "paused")
            return successor
        checkpoint = self._checkpoint(db, phase.id)
        if not self._owner_alive(checkpoint) or tick.mono < checkpoint.monotonic_s:
            return self._interrupt(db, phase, checkpoint, tick, origin="recovery")
        delta = min(phase.remaining_s, tick.mono - checkpoint.monotonic_s)
        remaining = phase.remaining_s - delta
        projected = phase.model_copy(update={
            "remaining_s": remaining,
            "actual_focus_s": phase.actual_focus_s + delta,
            "phase_due_at": tick.wall + timedelta(seconds=remaining),
        })
        if force or remaining == 0 or tick.mono - checkpoint.monotonic_s >= CHECKPOINT_SECONDS:
            interval = self._update_interval(db, phase.id, delta, close=remaining == 0)
            self._credit_habit(db, phase, delta)
            if remaining == 0:
                self._count_habit_completion(db, phase)
                elapsed_at = interval.ended_at if interval else tick.wall
                projected = projected.model_copy(update={"state": "elapsed", "phase_due_at": None,
                    "ended_at": elapsed_at, "version": phase.version + 1})
                db.execute("DELETE FROM phase_checkpoints WHERE phase_id=?", (phase.id,))
            else:
                self._save_checkpoint(db, projected, tick, checkpoint)
            self._save(db, projected, active=True)
            if remaining == 0 and allow_automatic:
                return self._automatic_follow_on(db, projected, tick) or projected
        return projected

    def _interrupt(self, db, phase, checkpoint, tick, *, origin):
        closed = self._update_interval(db, phase.id, close=True)
        db.execute("DELETE FROM phase_checkpoints WHERE phase_id=?", (phase.id,))
        paused = phase.model_copy(update={
            "state": "paused", "phase_due_at": None, "version": phase.version + 1,
        })
        self._save(db, paused, active=True)
        self._change_run_state(db, phase, "paused")
        if origin == "exit":
            return paused
        started = max(checkpoint.wall, closed.ended_at) if closed else checkpoint.wall
        interval = RecoveryInterval(
            id=uuid4().hex, phase_id=phase.id, origin=origin, started_at=started,
            observed_until_at=tick.wall if origin == "recovery" and tick.wall > started else None,
        )
        self._store_recovery(db, interval)
        if origin == "sleep":
            self._pending_sleep_recovery = interval.id
        return paused

    @staticmethod
    def _store_recovery(db, recovery):
        db.execute("""INSERT INTO phase_recovery VALUES (?, ?, ?)
                      ON CONFLICT(id) DO UPDATE SET interval=excluded.interval""",
                   (recovery.id, recovery.phase_id, recovery.model_dump_json()))

    def record_sleep(self):
        """Commit the observed departure boundary while the device is still awake."""
        self._sleeping = True
        with self._write() as db:
            tick = self._tick()
            phase = self._refresh(db, tick, force=True, allow_automatic=False)
            if phase and phase.kind == "focus" and phase.state == "running":
                self._interrupt(db, phase, self._checkpoint(db, phase.id), tick, origin="sleep")

    def record_wake(self):
        with self._write() as db:
            tick = self._tick()
            self._refresh(db, tick, allow_automatic=False)
            for interval in self._list_recovery(db):
                if interval.origin == "sleep" and interval.observed_until_at is None:
                    observed = max(tick.wall, interval.started_at)
                    self._store_recovery(db, interval.model_copy(update={
                        "observed_until_at": observed,
                        "trusted_end_at": observed if interval.id == self._pending_sleep_recovery else None,
                        "version": interval.version + 1,
                    }))
        self._pending_sleep_recovery = None
        self._sleeping = False

    def close(self):
        """Checkpoint and pause a focus owned by this runtime on an orderly exit."""
        with self._lock:
            if self._closed:
                return
            with self._write() as db:
                phase = self._active(db)
                if phase and phase.kind == "focus" and phase.state == "running":
                    checkpoint = self._checkpoint(db, phase.id)
                    if checkpoint.owner_session_id == self.owner_session_id:
                        tick = self._tick()
                        phase = self._refresh(db, tick, force=True, allow_automatic=False)
                        if phase.state == "running":
                            self._interrupt(db, phase, self._checkpoint(db, phase.id), tick, origin="exit")
            self._db.close()
            self._closed = True

    def pump(self):
        with self._write() as db:
            self._refresh(db, self._tick())

    def current_phase(self):
        with self._write() as db:
            return self.current_phase_in_transaction(db)

    def current_phase_in_transaction(self, db):
        """Resolve the timer under a caller's business write transaction."""
        if not db.in_transaction:
            raise ValueError("a write transaction is required to resolve the active phase")
        return self._refresh(db, self._tick())

    def get_phase(self, phase_id):
        with self._write() as db:
            active = self._refresh(db, self._tick())
            return active if active and active.id == phase_id else self._get(db, phase_id)

    def list_phases(self):
        with self._write() as db:
            active = self._refresh(db, self._tick())
            return self._list_phases(db, active)

    @staticmethod
    def _list_phases(db, active):
        phases = [PhaseView.model_validate_json(row[0])
                  for row in db.execute("SELECT view FROM phase_views ORDER BY rowid")]
        return [active if active and phase.id == active.id else phase for phase in phases]

    @staticmethod
    def _list_intervals(db, phase_id=None):
        if phase_id is None:
            rows = db.execute("SELECT interval FROM phase_intervals ORDER BY rowid")
        else:
            rows = db.execute("SELECT interval FROM phase_intervals WHERE phase_id=? ORDER BY rowid", (phase_id,))
        return [FocusInterval.model_validate_json(row[0]) for row in rows]

    def list_focus_intervals(self, phase_id=None):
        with self._write() as db:
            self._refresh(db, self._tick(), force=True)
            return self._list_intervals(db, phase_id)

    def issue_focus_summary(self, issue_id):
        with self._write() as db:
            active = self._refresh(db, self._tick(), force=True)
            phases = self._list_phases(db, active)
            intervals = [interval for interval in self._list_intervals(db)
                         if interval.primary_issue_id == issue_id and interval.seconds > 0]
            session_links = {interval.phase_id for interval in intervals}
            return {
                "first_focus_at": min((interval.started_at for interval in intervals), default=None),
                "actual_focus_s": sum(interval.seconds for interval in intervals),
                "session_links": [phase.id for phase in phases if phase.id in session_links or issue_id in phase.issue_links],
            }

    @staticmethod
    def _list_recovery(db):
        return [RecoveryInterval.model_validate_json(row[0])
                for row in db.execute("SELECT interval FROM phase_recovery ORDER BY rowid")]

    def snapshot_fields(self):
        with self._write() as db:
            active = self._refresh(db, self._tick())
            phases = self._list_phases(db, active)
            latest = active or (phases[-1] if phases else None)
            habit_run = self._get_run(db, latest.habit_run_id) if latest else None
            review_phase = next((phase for phase in reversed(phases)
                                 if phase.kind == "focus" and phase.state in ("elapsed", "ended")), None)
            return {
                "active_phase": active.model_dump(mode="json") if active else None,
                "phases": [phase.model_dump(mode="json") for phase in phases],
                "phase_defaults": self._defaults(db),
                "phase_recovery": [interval.model_dump(mode="json") for interval in self._list_recovery(db)],
                "phase_habit_run": habit_run.model_dump(mode="json") if habit_run else None,
                "phase_suggested_break": self._suggest_break(db, latest, habit_run),
                "phase_habit_continuation": self._suggest_habit_focus(db, latest, habit_run),
                "phase_review_intervals": [interval.model_dump(mode="json")
                    for interval in self._list_intervals(db, review_phase.id) if interval.seconds > 0]
                    if review_phase else [],
            }

    def command_result(self, command_id):
        with self._lock:
            row = self._db.execute("SELECT result FROM phase_commands WHERE id=?", (command_id,)).fetchone()
        return PhaseResult.model_validate_json(row[0]) if row else None

    def apply_phase(self, command: PhaseCommand) -> PhaseResult:
        with self._write() as db:
            tick = self._tick()
            result = self._apply(db, command, tick)
        self.emit("session." + result.status, session_id=command.phase_id, payload={
            "command_id": command.command_id, "kind": command.kind,
            "version": result.view.version if result.view else None,
            "state": result.view.state if result.view else None,
            "error_code": result.error_code.value if result.error_code else None,
        })
        return result

    def _apply(self, db, command, tick, *, automatic=False):
        fingerprint = json.dumps(command.model_dump(mode="json"), sort_keys=True,
                                 separators=(",", ":"), ensure_ascii=False)
        row = db.execute("SELECT fingerprint, result FROM phase_commands WHERE id=?",
                         (command.command_id,)).fetchone()
        if row:
            if row["fingerprint"] != fingerprint:
                return PhaseResult(command_id=command.command_id, status="rejected",
                                   error_code=ErrorCode.INVALID_ARGUMENT)
            previous = PhaseResult.model_validate_json(row["result"])
            return previous.model_copy(update={"status": "duplicate", "original_status": previous.status})
        active = self._active(db) if automatic else self._refresh(db, tick, force=True)
        current = active if active and active.id == command.phase_id else self._get(db, command.phase_id)
        db.execute("SAVEPOINT phase_mutation")
        try:
            if command.source == "habit" and not automatic:
                raise Rejected(ErrorCode.INVALID_TRANSITION)
            view = self._execute(db, command, tick, active, current)
            result = PhaseResult(command_id=command.command_id, status="applied", view=view)
        except (Rejected, ValidationError, ValueError, OverflowError) as exc:
            db.execute("ROLLBACK TO phase_mutation")
            result = PhaseResult(command_id=command.command_id, status="rejected", view=current,
                                 error_code=exc.code if isinstance(exc, Rejected) else ErrorCode.INVALID_ARGUMENT)
        db.execute("RELEASE phase_mutation")
        db.execute("INSERT INTO phase_commands VALUES (?, ?, ?)",
                   (command.command_id, fingerprint, result.model_dump_json()))
        return result

    def _execute(self, db, command, tick, active, current):
        if command.kind == "start":
            if current is not None:
                raise Rejected(ErrorCode.INVALID_TRANSITION)
            if command.expected_version != 0:
                raise Rejected(ErrorCode.STALE_TASK)
            return self._start(db, command, StartInput.model_validate(command.payload), tick, active)
        if current is None:
            raise Rejected(ErrorCode.NOT_FOUND)
        if current.version != command.expected_version:
            raise Rejected(ErrorCode.STALE_TASK)
        if command.kind == "reassign_interval":
            self._reassign_interval(db, current, ReassignInput.model_validate(command.payload))
            return current
        if command.kind in ("credit_sleep", "credit_recovery"):
            return self._credit_recovery(db, current, command, tick)
        if command.kind == "review":
            if current.kind != "focus" or current.state not in ("ended", "elapsed"):
                raise Rejected(ErrorCode.INVALID_TRANSITION)
            return self._review(db, current, ReviewInput.model_validate(command.payload),
                                active=active is not None and current.id == active.id)
        if active is None or active.id != current.id:
            raise Rejected(ErrorCode.INVALID_TRANSITION)
        if command.kind == "adjust":
            return self._adjust(db, current, AdjustInput.model_validate(command.payload), tick)
        if command.kind == "set_details":
            return self._set_details(db, current, DetailsInput.model_validate(command.payload))
        if command.kind == "set_links":
            links = LinksInput.model_validate(command.payload)
            if current.kind != "focus" or (links.issue_links and not self._defaults(db)["link_issues"]):
                raise Rejected(ErrorCode.INVALID_TRANSITION)
            return self._set_links(db, current, links, tick)
        if command.kind == "end":
            return self._end(db, current, ReviewInput.model_validate(command.payload), tick)
        if command.payload:
            raise Rejected(ErrorCode.INVALID_ARGUMENT)
        if command.kind == "skip_break":
            if current.kind == "focus":
                raise Rejected(ErrorCode.INVALID_TRANSITION)
            return self._end(db, current, ReviewInput(), tick, skipped_break=True)
        return self._pause_resume(db, current, command.kind, tick)

    def _start(self, db, command, args, tick, active):
        previous = self._get(db, args.previous_phase_id) if args.previous_phase_id else None
        if args.previous_phase_id and (previous is None or previous.version != args.expected_previous_version):
            raise Rejected(ErrorCode.STALE_TASK)
        if active and (previous is None or previous.id != active.id or active.state != "elapsed"
                       or active.kind != "focus" or args.kind == "focus"):
            raise Rejected(ErrorCode.INVALID_TRANSITION)
        if previous and (previous.state not in ("elapsed", "ended") or
                         (previous.kind == "focus") == (args.kind == "focus")):
            raise Rejected(ErrorCode.INVALID_TRANSITION)
        if previous and self._latest_phase_id(db) != previous.id:
            raise Rejected(ErrorCode.STALE_TASK)

        defaults = self._defaults(db)
        if args.issue_links and not defaults["link_issues"]:
            raise Rejected(ErrorCode.INVALID_TRANSITION)
        habit, run = self._habit_for_start(db, args, previous, command.source)
        duration = args.planned_duration_s
        if habit:
            if args.kind == "focus":
                duration = duration or habit.behavior.focus_s
            elif args.kind == "long_break":
                if not habit.behavior.long_break_enabled:
                    raise Rejected(ErrorCode.INVALID_TRANSITION)
                duration = duration or habit.behavior.long_break_s
            else:
                duration = duration or habit.behavior.short_break_s
        duration = duration or defaults["default_focus_s" if args.kind == "focus" else "default_break_s"]
        phase = PhaseView(
            id=command.phase_id, version=1, kind=args.kind,
            title=(args.title or "").strip() or PHASE_TITLES[args.kind],
            description=(args.description or "").strip() or None,
            state="running", planned_duration_s=duration, remaining_s=duration, actual_focus_s=0,
            created_at=tick.wall, begin_at=tick.wall, phase_due_at=tick.wall + timedelta(seconds=duration),
            source=command.source, issue_links=args.issue_links, primary_issue_id=args.primary_issue_id,
            habit_id=habit.id if habit else None, applied_habit_version=habit.version if habit else None,
            habit_run_id=run.id if run else None,
        )
        latest = active or self._get(db, self._latest_phase_id(db))
        if active:
            self._save(db, active, active=False)
        if latest and latest.habit_run_id and latest.habit_run_id != phase.habit_run_id:
            self._change_run_state(db, latest, "ended")
        self._save(db, phase, active=True)
        if run:
            updates = {"state": "active", "applied_version": habit.version}
            if phase.kind == "long_break":
                updates.update(focus_s_since_long_break=0, completed_phases_since_long_break=0,
                               next_break_kind="short_break")
            self._update_run(db, run, **updates)
        if phase.kind == "focus":
            self._begin_interval(db, phase, tick)
        return phase

    def _pause_resume(self, db, phase, action, tick):
        if action == "pause" and phase.state == "running":
            view = phase.model_copy(update={"state": "paused", "phase_due_at": None,
                                            "version": phase.version + 1})
            self._update_interval(db, phase.id, close=True)
            db.execute("DELETE FROM phase_checkpoints WHERE phase_id=?", (phase.id,))
            self._change_run_state(db, phase, "paused")
        elif action == "resume" and phase.state == "paused" and phase.remaining_s > 0:
            view = phase.model_copy(update={"state": "running", "phase_due_at":
                tick.wall + timedelta(seconds=phase.remaining_s), "version": phase.version + 1})
            if phase.kind == "focus":
                self._begin_interval(db, view, tick)
            self._change_run_state(db, phase, "active")
        else:
            raise Rejected(ErrorCode.INVALID_TRANSITION)
        return self._save(db, view, active=True)

    def _adjust(self, db, phase, args, tick):
        if phase.state not in ("running", "paused", "elapsed"):
            raise Rejected(ErrorCode.INVALID_TRANSITION)
        spent = phase.planned_duration_s - phase.remaining_s
        total = args.total_s if args.total_s is not None else phase.planned_duration_s + args.add_s
        if total <= spent or total > 86400:
            raise Rejected(ErrorCode.INVALID_ARGUMENT)
        remaining = total - spent
        state = "paused" if phase.state == "paused" else "running"
        view = phase.model_copy(update={
            "planned_duration_s": total, "remaining_s": remaining, "state": state, "ended_at": None,
            "phase_due_at": tick.wall + timedelta(seconds=remaining) if state == "running" else None,
            "version": phase.version + 1,
        })
        if phase.kind == "focus" and state == "running":
            if phase.state == "elapsed":
                self._begin_interval(db, view, tick)
            else:
                self._save_checkpoint(db, view, tick, self._checkpoint(db, phase.id))
        return self._save(db, view, active=True)

    @staticmethod
    def _review_updates(review):
        return {"confirmed_outcomes": review.confirmed_outcomes, "next_steps": review.next_steps}

    def _review(self, db, phase, review, *, active):
        return self._save(db, phase.model_copy(update={
            **self._review_updates(review), "version": phase.version + 1,
        }), active=active)

    def _end(self, db, phase, review, tick, *, skipped_break=False):
        if phase.kind != "focus" and (review.confirmed_outcomes or review.next_steps or review.count_as_completed):
            raise Rejected(ErrorCode.INVALID_ARGUMENT)
        self._update_interval(db, phase.id, close=True)
        db.execute("DELETE FROM phase_checkpoints WHERE phase_id=?", (phase.id,))
        if review.count_as_completed:
            self._count_habit_completion(db, phase)
        self._change_run_state(db, phase, "paused" if skipped_break or review.count_as_completed else "ended")
        return self._save(db, phase.model_copy(update={
            **self._review_updates(review), "state": "ended", "phase_due_at": None,
            "ended_at": phase.ended_at or tick.wall, "version": phase.version + 1,
        }), active=False)

    def _set_details(self, db, phase, details):
        if phase.kind != "focus" or not details.model_fields_set:
            raise Rejected(ErrorCode.INVALID_TRANSITION)
        updates = {"version": phase.version + 1}
        if "title" in details.model_fields_set:
            updates["title"] = (details.title or "").strip() or PHASE_TITLES["focus"]
        if "description" in details.model_fields_set:
            updates["description"] = (details.description or "").strip() or None
        return self._save(db, phase.model_copy(update=updates), active=True)

    def _set_links(self, db, phase, links, tick):
        view = phase.model_copy(update={
            "issue_links": links.issue_links, "primary_issue_id": links.primary_issue_id,
            "version": phase.version + 1,
        })
        if phase.state == "running" and view.primary_issue_id != phase.primary_issue_id:
            self._update_interval(db, phase.id, close=True)
            self._begin_interval(db, view, tick)
        return self._save(db, view, active=True)

    def unlink_completed_issue(self, issue_id, *, db=None):
        """Close the current issue interval in the caller's item transaction."""
        if db is None:
            with self._write() as owned_db:
                return self.unlink_completed_issue(issue_id, db=owned_db)
        if not db.in_transaction:
            raise ValueError("a write transaction is required to unlink an issue")
        phase = self._active(db)
        if phase is None or issue_id not in phase.issue_links:
            return phase
        tick = self._tick()
        current = self._refresh(db, tick, force=True)
        if current is None or current.id != phase.id:
            return current
        return self._set_links(db, current, LinksInput(
            issue_links=tuple(link for link in current.issue_links if link != issue_id),
            primary_issue_id=None if issue_id == current.primary_issue_id else current.primary_issue_id,
        ), tick)

    def _credit_recovery(self, db, phase, command, tick):
        if command.kind == "credit_recovery":
            args = CreditRecoveryInput.model_validate(command.payload)
            recovery_id = args.recovery_interval_id
        else:
            sleep = CreditSleepInput.model_validate(command.payload)
            recovery_id = sleep.interval_id
        row = db.execute("SELECT interval FROM phase_recovery WHERE id=? AND phase_id=?",
                         (recovery_id, phase.id)).fetchone()
        if row is None:
            raise Rejected(ErrorCode.NOT_FOUND)
        recovery = RecoveryInterval.model_validate_json(row[0])
        if recovery.credited_seconds is not None or phase.kind != "focus" or phase.state == "running":
            raise Rejected(ErrorCode.INVALID_TRANSITION)
        if command.kind == "credit_sleep":
            if recovery.origin != "sleep" or recovery.trusted_end_at is None:
                raise Rejected(ErrorCode.INVALID_TRANSITION)
            args = CreditRecoveryInput(
                recovery_interval_id=recovery.id, expected_recovery_version=recovery.version,
                started_at=recovery.started_at, ended_at=recovery.started_at + timedelta(seconds=sleep.seconds),
            )
        if args.expected_recovery_version != recovery.version:
            raise Rejected(ErrorCode.STALE_TASK)
        seconds = (args.ended_at - args.started_at).total_seconds()
        bound = recovery.trusted_end_at or tick.wall
        if seconds <= 0 or seconds > phase.remaining_s or args.started_at < recovery.started_at or args.ended_at > bound:
            raise Rejected(ErrorCode.INVALID_ARGUMENT)
        for row in db.execute("SELECT interval FROM phase_intervals"):
            booked = FocusInterval.model_validate_json(row[0])
            until = booked.ended_at or booked.started_at + timedelta(seconds=booked.seconds)
            if booked.seconds > 0 and booked.started_at < args.ended_at and args.started_at < until:
                raise Rejected(ErrorCode.INVALID_ARGUMENT)
        self._store_interval(db, FocusInterval(
            id=uuid4().hex, phase_id=phase.id, started_at=args.started_at, ended_at=args.ended_at,
            seconds=seconds, origin=recovery.origin,
        ))
        self._store_recovery(db, recovery.model_copy(update={
            "credited_seconds": seconds, "version": recovery.version + 1,
        }))
        remaining = phase.remaining_s - seconds
        updates = {"remaining_s": remaining, "actual_focus_s": phase.actual_focus_s + seconds,
                   "version": phase.version + 1}
        if phase.state == "paused" and remaining == 0:
            updates.update(state="elapsed", ended_at=tick.wall)
            self._count_habit_completion(db, phase)
        self._credit_habit(db, phase, seconds)
        active = self._active(db)
        return self._save(db, phase.model_copy(update=updates), active=active is not None and active.id == phase.id)

    def _reassign_interval(self, db, phase, args):
        row = db.execute("SELECT interval FROM phase_intervals WHERE id=? AND phase_id=?",
                         (args.interval_id, phase.id)).fetchone()
        if row is None:
            raise Rejected(ErrorCode.NOT_FOUND)
        interval = FocusInterval.model_validate_json(row[0])
        if interval.version != args.expected_interval_version:
            raise Rejected(ErrorCode.STALE_TASK)
        if interval.ended_at is None:
            raise Rejected(ErrorCode.INVALID_TRANSITION)
        if args.target_issue_id is not None and not args.target_issue_id.strip():
            raise Rejected(ErrorCode.INVALID_ARGUMENT)
        self._store_interval(db, interval.model_copy(update={
            "primary_issue_id": args.target_issue_id, "version": interval.version + 1,
        }))

    @staticmethod
    def _get_run(db, run_id):
        if not run_id:
            return None
        row = db.execute("SELECT view FROM phase_habit_runs WHERE id=?", (run_id,)).fetchone()
        return HabitRun.model_validate_json(row[0]) if row else None

    def get_habit_run(self, run_id):
        with self._write() as db:
            self._refresh(db, self._tick())
            return self._get_run(db, run_id)

    @staticmethod
    def _update_run(db, run, **changes):
        if all(getattr(run, attr) == value for attr, value in changes.items()):
            return run
        view = run.model_copy(update={**changes, "version": run.version + 1})
        db.execute("UPDATE phase_habit_runs SET view=? WHERE id=?", (view.model_dump_json(), view.id))
        return view

    def _get_habit(self, db, habit_id):
        return self.settings_store.get_habit(habit_id, db=db) if self.settings_store and habit_id else None

    def _habit_for_start(self, db, args, previous, source):
        previous_run_id = previous.habit_run_id if previous else None
        habit_id = args.habit_id or (previous.habit_id if previous else None)
        habit = self._get_habit(db, habit_id)
        if args.habit_id:
            if habit is None:
                raise Rejected(ErrorCode.NOT_FOUND)
            if habit.version != args.expected_habit_version:
                raise Rejected(ErrorCode.STALE_TASK)
            if habit.status != "enabled":
                raise Rejected(ErrorCode.INVALID_TRANSITION)
        if not habit or habit.status != "enabled":
            if source == "habit" or args.habit_run_id:
                raise Rejected(ErrorCode.INVALID_TRANSITION)
            return None, None
        run = self._get_run(db, previous_run_id)
        if run and (run.habit_id != habit.id or run.state == "ended"):
            run = None
        if args.habit_run_id and (run is None or run.id != args.habit_run_id
                                 or run.version != args.expected_habit_run_version):
            raise Rejected(ErrorCode.STALE_TASK)
        if source == "habit":
            if not args.habit_id or not args.habit_run_id or run is None or previous is None or previous.state != "elapsed":
                raise Rejected(ErrorCode.INVALID_TRANSITION)
            permission = habit.behavior.auto_start_focus if args.kind == "focus" else habit.behavior.auto_start_break
            if not permission or not self.settings_store.read(db=db).habits_enabled:
                raise Rejected(ErrorCode.INVALID_TRANSITION)
            if previous.kind == args.kind or (previous.kind != "focus" and args.kind != "focus"):
                raise Rejected(ErrorCode.INVALID_TRANSITION)
        if run is None:
            run = HabitRun(id=uuid4().hex, habit_id=habit.id, applied_version=habit.version)
            db.execute("INSERT INTO phase_habit_runs VALUES (?, ?)", (run.id, run.model_dump_json()))
        return habit, run

    def _change_run_state(self, db, phase, state):
        run = self._get_run(db, phase.habit_run_id)
        if run:
            self._update_run(db, run, state=state)

    @staticmethod
    def _next_break(habit, focus_s, completed):
        if habit and habit.behavior.long_break_enabled:
            progress = focus_s if habit.behavior.long_break_basis == "focus_seconds" else completed
            if progress >= habit.behavior.threshold:
                return "long_break"
        return "short_break"

    def _credit_habit(self, db, phase, seconds):
        run = self._get_run(db, phase.habit_run_id)
        if run and seconds:
            habits = self._get_habit(db, run.habit_id)
            total = run.focus_s_since_long_break + seconds
            self._update_run(db, run, focus_s_since_long_break=total, next_break_kind=
                self._next_break(habits, total, run.completed_phases_since_long_break))

    def _count_habit_completion(self, db, phase):
        run = self._get_run(db, phase.habit_run_id)
        if not run:
            return
        counted = db.execute("INSERT OR IGNORE INTO phase_habit_completed VALUES (?)", (phase.id,))
        if counted.rowcount:
            completed = run.completed_phases_since_long_break + 1
            habit = self._get_habit(db, run.habit_id)
            self._update_run(db, run, completed_phases_since_long_break=completed, next_break_kind=
                self._next_break(habit, run.focus_s_since_long_break, completed))

    def _suggest_break(self, db, phase, run):
        if not phase or phase.kind != "focus":
            return None
        pending_habit = run and run.state != "ended"
        if phase.state != "elapsed" and not (phase.state == "ended" and pending_habit):
            return None
        habit = self._get_habit(db, phase.habit_id) if pending_habit else None
        if habit and habit.status == "enabled":
            kind = self._next_break(habit, run.focus_s_since_long_break, run.completed_phases_since_long_break)
            duration = habit.behavior.long_break_s if kind == "long_break" else habit.behavior.short_break_s
        else:
            kind, duration = "short_break", self._defaults(db)["default_break_s"]
        return {"kind": kind, "duration_s": duration, "previous_phase_id": phase.id,
                "expected_previous_version": phase.version}

    def _suggest_habit_focus(self, db, phase, run):
        if not (phase and phase.kind != "focus" and phase.state in ("elapsed", "ended")
                and run and run.state == "paused"):
            return None
        habit = self._get_habit(db, phase.habit_id)
        if not (habit and habit.status == "enabled"):
            return None
        return {"title": habit.title, "duration_s": habit.behavior.focus_s, "habit_id": habit.id,
                "expected_habit_version": habit.version, "habit_run_id": run.id,
                "expected_habit_run_version": run.version, "previous_phase_id": phase.id,
                "expected_previous_version": phase.version}

    def _automatic_follow_on(self, db, elapsed, tick):
        run = self._get_run(db, elapsed.habit_run_id)
        habit = self._get_habit(db, elapsed.habit_id)
        if not (run and habit and habit.status == "enabled" and self.settings_store.read(db=db).habits_enabled):
            return None
        if elapsed.kind == "focus" and habit.behavior.auto_start_break:
            kind = self._next_break(habit, run.focus_s_since_long_break, run.completed_phases_since_long_break)
        elif elapsed.kind != "focus" and habit.behavior.auto_start_focus:
            kind = "focus"
        else:
            return None
        transition_id = uuid5(NAMESPACE_URL, f"mochi:habit:{elapsed.id}:{elapsed.version}").hex
        result = self._apply(db, PhaseCommand(
            command_id=f"habit-transition:{transition_id}",
            phase_id=f"habit-phase:{transition_id}", source="habit", kind="start",
            payload={"kind": kind, "previous_phase_id": elapsed.id,
                     "expected_previous_version": elapsed.version, "habit_run_id": run.id,
                     "expected_habit_run_version": run.version, "habit_id": habit.id,
                     "expected_habit_version": habit.version},
        ), tick, automatic=True)
        return result.view if result.status == "applied" else None

    @staticmethod
    def _legacy_view(phase):
        if phase is None:
            return None
        return WorkSession(
            session_id=phase.id, version=phase.version, task_id=phase.primary_issue_id, goal=phase.title,
            state={"running": "focusing", "paused": "paused", "elapsed": "break_due",
                   "ended": "ended", "ready": "paused"}[phase.state],
            remaining_s=phase.remaining_s, phase_due_at=phase.phase_due_at,
            confirmed_outcomes=phase.confirmed_outcomes, next_steps=phase.next_steps,
        )

    def get(self, session_id):
        phase = self.get_phase(session_id)
        return self._legacy_view(phase) if phase and phase.kind == "focus" else None

    def list_sessions(self):
        return [self._legacy_view(phase) for phase in self.list_phases() if phase.kind == "focus"]

    def duration_for(self, session_id):
        phase = self.get_phase(session_id)
        return phase.planned_duration_s if phase else None

    def apply(self, command: SessionCommand) -> SessionResult:
        result = self.apply_phase(PhaseCommand(
            command_id=command.command_id, phase_id=command.session_id, kind=command.kind,
            expected_version=command.expected_version,
            payload=self.phase_payload(command.kind, command.payload),
        ))
        return SessionResult(
            command_id=result.command_id, status=result.status, view=self._legacy_view(result.view),
            error_code=ErrorCode.DUPLICATE if result.status == "duplicate" else result.error_code,
        )

    @staticmethod
    def phase_payload(kind, payload):
        args = dict(payload)
        if kind == "start":
            if "goal" in args:
                args.setdefault("title", args.pop("goal"))
            if "duration_s" in args:
                args.setdefault("planned_duration_s", args.pop("duration_s"))
            if "task_id" in args:
                task_id = args.pop("task_id")
                if task_id is not None:
                    args.setdefault("issue_links", [task_id])
                    args.setdefault("primary_issue_id", task_id)
        return args
