"""SQLite adapter for the M0 semantics, with a transactional delivery outbox.

One instance belongs to its creating domain thread. Separate processes/connections
serialize through BEGIN IMMEDIATE, including the short, local DeliveryPort call.
Never put a model/network call in DeliveryPort.show: the Qt port only queues work.
The intent is committed *before* that call; a crash can replay the same ID.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import uuid

from pydantic import ValidationError

from deskpet.contracts import (
    CommandResult, DecisionEnvelope, DisplayCommand, DisplayReceipt, ErrorCode,
    Observation, ProposedPlan, TaskCommand, TaskView,
)
from deskpet.mocks.task_service import MemoryTaskService, _Attempt


SCHEMA_VERSION = 1


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class _Effects:
    """Collect side effects while the unchanged domain rules run in a transaction."""

    def __init__(self):
        self.items = []

    def show(self, command):
        self.items.append(("show", command.task_id, command.task_version, command.delivery_id))
        return None

    def dismiss(self, task_id):
        self.items.append(("dismiss", task_id, None, None))


class _Domain(MemoryTaskService):
    """Only restart authority differs: old-boot decisions need fresh context."""

    restart_floor = None

    def validate(self, envelope):
        error = super().validate(envelope)
        if error is None and self.restart_floor is not None and envelope.created_at < self.restart_floor:
            return ErrorCode.STALE_CONTEXT
        return error


class SQLiteTaskService:
    def __init__(self, clock, delivery, emit, *, path,
                 receipt_timeout_s=5.0, retry_interval_s=1.0, busy_timeout_s=5.0):
        # Let the reference validate timer settings before opening a database.
        MemoryTaskService(clock, delivery, emit, receipt_timeout_s=receipt_timeout_s,
                          retry_interval_s=retry_interval_s)
        if busy_timeout_s <= 0:
            raise ValueError("busy_timeout_s must be positive")
        self.clock, self.delivery, self.emit = clock, delivery, emit
        self.receipt_timeout_s, self.retry_interval_s = receipt_timeout_s, retry_interval_s
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._session = uuid.uuid4().hex
        self._opened_at = clock.wall_now()
        self._temporary = None
        self._closed = False
        self._db = sqlite3.connect(self.path, timeout=busy_timeout_s, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA synchronous=FULL")
        try:
            with self._transaction():
                version = self._db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, SCHEMA_VERSION):
                    raise ValueError(f"unsupported task database schema {version}")
                # execute one statement at a time: executescript would implicitly COMMIT.
                for statement in (
                    "CREATE TABLE IF NOT EXISTS tasks (task_id TEXT PRIMARY KEY, view TEXT NOT NULL)",
                    "CREATE TABLE IF NOT EXISTS commands (command_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT NOT NULL)",
                    "CREATE TABLE IF NOT EXISTS decisions (request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL)",
                    "CREATE TABLE IF NOT EXISTS rechecks (task_id TEXT PRIMARY KEY, version INTEGER NOT NULL, run_at TEXT NOT NULL, request_id TEXT NOT NULL)",
                    "CREATE TABLE IF NOT EXISTS deliveries (delivery_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, task_version INTEGER NOT NULL, occurrence INTEGER NOT NULL, command TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL, last_attempt_at TEXT NOT NULL, owner TEXT NOT NULL, last_attempt_mono REAL NOT NULL, receipt TEXT, active INTEGER NOT NULL, UNIQUE(task_id,task_version,occurrence))",
                    "CREATE UNIQUE INDEX IF NOT EXISTS one_active_delivery ON deliveries(task_id) WHERE active=1",
                    "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
                    "CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, task_id TEXT NOT NULL, task_version INTEGER NOT NULL, delivery_id TEXT, not_before TEXT NOT NULL)",
                    "CREATE UNIQUE INDEX IF NOT EXISTS one_queued_show ON outbox(delivery_id) WHERE kind='show'",
                    "CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT NOT NULL, kind TEXT NOT NULL, fields TEXT NOT NULL, emitted INTEGER NOT NULL DEFAULT 0)",
                ):
                    self._db.execute(statement)
                self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                self._db.execute("INSERT OR IGNORE INTO metadata VALUES ('database_id',?)", (uuid.uuid4().hex,))
                self.database_id = self._db.execute("SELECT value FROM metadata WHERE key='database_id'").fetchone()[0]
                self._recovering = self._db.execute("SELECT 1 FROM tasks LIMIT 1").fetchone() is not None
        except BaseException:
            self._db.close()
            raise

    @contextmanager
    def _transaction(self):
        if self._closed:
            raise RuntimeError("task service is closed")
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def _load(self):
        effects, events = _Effects(), []
        core = _Domain(self.clock, effects,
                                 lambda kind, **fields: events.append((kind, fields)),
                                 receipt_timeout_s=self.receipt_timeout_s,
                                 retry_interval_s=self.retry_interval_s)
        core.restart_floor = self._opened_at if self._recovering else None
        core._tasks = {r["task_id"]: TaskView.model_validate_json(r["view"])
                       for r in self._db.execute("SELECT * FROM tasks")}
        core._commands = {r["command_id"]: (r["fingerprint"], CommandResult.model_validate_json(r["result"]))
                          for r in self._db.execute("SELECT * FROM commands")}
        core._decisions = {r["request_id"]: r["fingerprint"] for r in self._db.execute("SELECT * FROM decisions")}
        core._rechecks = {r["task_id"]: (r["version"], datetime.fromisoformat(r["run_at"]), r["request_id"])
                          for r in self._db.execute("SELECT * FROM rechecks")}
        metadata = dict(self._db.execute("SELECT key,value FROM metadata"))
        # A's observation pipeline starts a new revision sequence after boot.
        # Keep the prior observation for audit, but require this session's fresh
        # observe() before using it. This also avoids trusting old no-TTL evidence.
        if "observation" in metadata and metadata.get("observation_owner") == self._session:
            core._observation = Observation.model_validate_json(metadata["observation"])
        core._last_deferred = {k: tuple(v) for k, v in json.loads(metadata.get("deferred", "{}")).items()}
        now, mono = self.clock.wall_now(), self.clock.monotonic_now()
        for row in self._db.execute("SELECT * FROM deliveries"):
            # Raw monotonic values are meaningful only in this service session.
            last_mono = row["last_attempt_mono"] if row["owner"] == self._session else (
                mono - max(0.0, (now - datetime.fromisoformat(row["last_attempt_at"])).total_seconds()))
            attempt = _Attempt(command=DisplayCommand.model_validate_json(row["command"]),
                               last_attempt_mono=last_mono, status=row["status"],
                               attempts=row["attempts"],
                               receipt=DisplayReceipt.model_validate_json(row["receipt"]) if row["receipt"] else None)
            core._attempts[row["delivery_id"]] = attempt
            view = core._tasks.get(row["task_id"])
            if (row["active"] and view and view.status == "registered"
                    and view.version == attempt.command.task_version
                    and view.occurrence == attempt.command.occurrence):
                core._active_delivery[view.task_id] = row["delivery_id"]
        return core, effects, events

    def _save(self, core, effects, events):
        db = self._db
        db.executemany("INSERT OR REPLACE INTO tasks VALUES (?,?)",
                       [(k, v.model_dump_json()) for k, v in core._tasks.items()])
        db.executemany("INSERT OR REPLACE INTO commands VALUES (?,?,?)",
                       [(k, fp, r.model_dump_json()) for k, (fp, r) in core._commands.items()])
        db.executemany("INSERT OR REPLACE INTO decisions VALUES (?,?)", list(core._decisions.items()))
        db.execute("DELETE FROM rechecks")
        db.executemany("INSERT INTO rechecks VALUES (?,?,?,?)",
                       [(k, v, at.isoformat(), request) for k, (v, at, request) in core._rechecks.items()])
        if core._observation is not None:
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('observation',?)", (core._observation.model_dump_json(),))
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('observation_owner',?)", (self._session,))
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('deferred',?)", (_json(core._last_deferred),))
        db.execute("UPDATE deliveries SET active=0 WHERE active=1")
        now, mono = self.clock.wall_now(), self.clock.monotonic_now()
        for key, attempt in core._attempts.items():
            command = attempt.command
            last_at = now - timedelta(seconds=max(0.0, mono-attempt.last_attempt_mono))
            db.execute("INSERT OR REPLACE INTO deliveries VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                key, command.task_id, command.task_version, command.occurrence,
                command.model_dump_json(), attempt.status, attempt.attempts,
                last_at.isoformat(), self._session, attempt.last_attempt_mono,
                attempt.receipt.model_dump_json() if attempt.receipt else None,
                int(core._active_delivery.get(command.task_id) == key)))
        for kind, task_id, version, delivery_id in effects.items:
            version = version if version is not None else core._tasks[task_id].version
            db.execute("INSERT OR IGNORE INTO outbox(kind,task_id,task_version,delivery_id,not_before) VALUES (?,?,?,?,?)",
                       (kind, task_id, version, delivery_id, now.isoformat()))
        for kind, fields in events:
            db.execute("INSERT INTO events(recorded_at,kind,fields) VALUES (?,?,?)", (now.isoformat(), kind, _json(fields)))

    def _change(self, method, *args):
        with self._transaction():
            core, effects, events = self._load()
            result = getattr(core, method)(*args)
            self._save(core, effects, events)
        self._flush_events()
        self._drain()
        return result

    def get(self, task_id):
        row = self._db.execute("SELECT view FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return TaskView.model_validate_json(row[0]) if row else None

    def list_tasks(self):
        return [TaskView.model_validate_json(row[0]) for row in self._db.execute("SELECT view FROM tasks ORDER BY task_id")]

    def apply(self, command: TaskCommand):
        return self._change("apply", command)

    def observe(self, observation: Observation):
        return self._change("observe", observation)

    def validate(self, envelope: DecisionEnvelope):
        with self._transaction():
            return self._load()[0].validate(envelope)

    def accept_decision(self, plan: ProposedPlan):
        return self._change("accept_decision", plan)

    def schedule_recheck(self, envelope: DecisionEnvelope, run_at: datetime):
        return self._change("schedule_recheck", envelope, run_at)

    def request_delivery(self, envelope: DecisionEnvelope):
        original = self._change("request_delivery", envelope)
        if original.status == "rejected":
            return original
        # The port may have synchronously returned a real receipt during drain.
        # Query again, without invoking/draining another external effect.
        with self._transaction():
            core, _, _ = self._load()
            error = core.validate(envelope)
            view = core.get(envelope.task_id)
            if error is not None:
                return original.model_copy(update={"status": "rejected", "error_code": error,
                                                    "receipt_id": None})
            attempt = core._attempts.get(core._active_delivery.get(envelope.task_id))
            return core._delivery_result(envelope, view, attempt) if attempt else original

    def record_receipt(self, receipt: DisplayReceipt):
        return self._change("record_receipt", receipt)

    def pump(self):
        return self._change("pump")

    def _flush_events(self):
        # At-least-once telemetry. D can deduplicate a replay after a sink crash
        # using b_event_id. The SQL event history remains available even if emit fails.
        with self._transaction():
            for row in self._db.execute("SELECT * FROM events WHERE emitted=0 ORDER BY id").fetchall():
                fields = json.loads(row["fields"])
                fields["payload"] = dict(fields.get("payload", {}),
                                         b_event_id=f"{self.database_id}:{row['id']}",
                                         b_recorded_at=row["recorded_at"])
                try:
                    self.emit(row["kind"], **fields)
                except Exception:
                    break
                self._db.execute("UPDATE events SET emitted=1 WHERE id=?", (row["id"],))

    def _drain(self):
        """Deliver only committed intents; serialize cancellation against show.

        The brief SQLite write lock covers final authority checking + show. It
        excludes a second process committing a cancellation between those steps.
        Crash after show but before receipt commit => replay SAME delivery ID.
        """
        while True:
            with self._transaction():
                # ISO strings are canonical UTC offsets from our injected clock.
                row = self._db.execute("SELECT * FROM outbox WHERE not_before<=? ORDER BY id LIMIT 1",
                                       (self.clock.wall_now().isoformat(),)).fetchone()
                if row is None:
                    break
                core, effects, events = self._load()
                view = core.get(row["task_id"])
                valid = view is not None and view.version == row["task_version"]
                if row["kind"] == "dismiss":
                    # A delayed dismissal must never erase a newer reminder card.
                    active = core._attempts.get(core._active_delivery.get(row["task_id"]))
                    if valid and not (active and active.status == "displayed"):
                        try:
                            self.delivery.dismiss(row["task_id"])
                        except Exception as exc:
                            core.emit("delivery.dismiss_failed", task_id=view.task_id, task_version=view.version,
                                      payload={"error": type(exc).__name__})
                            self._db.execute("UPDATE outbox SET not_before=? WHERE id=?",
                                             ((self.clock.wall_now()+timedelta(seconds=self.retry_interval_s)).isoformat(), row["id"]))
                            self._save(core, effects, events)
                            continue
                else:
                    attempt = core._attempts.get(row["delivery_id"])
                    valid = (valid and view.status == "registered" and attempt is not None
                             and core._active_delivery.get(view.task_id) == row["delivery_id"]
                             and view.occurrence == attempt.command.occurrence
                             and view.displayed_at is None)
                    if valid:
                        if self.clock.wall_now() >= attempt.command.expires_at:
                            attempt.command = attempt.command.model_copy(update={
                                "expires_at": self.clock.wall_now()+timedelta(seconds=max(30.0,self.receipt_timeout_s*2))})
                            self._save(core, effects, events)
                            continue  # Commit the renewed lease BEFORE calling show.
                        attempt.last_attempt_mono = self.clock.monotonic_now()
                        try:
                            receipt = self.delivery.show(attempt.command)
                        except Exception as exc:
                            attempt.status = "failed"
                            core.emit("delivery.failed", task_id=view.task_id, task_version=view.version,
                                      delivery_id=row["delivery_id"],
                                      payload={"error_code": ErrorCode.FAILED.value, "error": type(exc).__name__})
                        else:
                            if receipt is not None:
                                try:
                                    receipt = DisplayReceipt.model_validate(receipt)
                                except (ValidationError, TypeError, ValueError):
                                    attempt.status = "failed"
                                    core.emit("delivery.failed", task_id=view.task_id, task_version=view.version,
                                              delivery_id=row["delivery_id"],
                                              payload={"error_code": ErrorCode.INVALID_ARGUMENT.value,
                                                       "error": "invalid display receipt"})
                                else:
                                    core.record_receipt(receipt)
                    else:
                        core.emit("delivery.suppressed", task_id=row["task_id"], task_version=row["task_version"],
                                  delivery_id=row["delivery_id"], payload={"reason": "obsolete_intent"})
                self._db.execute("DELETE FROM outbox WHERE id=?", (row["id"],))
                self._save(core, effects, events)
            self._flush_events()
        self._flush_events()

    def close(self):
        if not self._closed:
            self._db.close()
            self._closed = True
            if self._temporary is not None:
                self._temporary.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __del__(self):
        # Existing M0 fixtures have no close hook. Explicit close on the owner
        # thread is preferred; interpreter/Qt teardown is only best effort.
        try:
            self.close()
        except (AttributeError, sqlite3.Error, OSError):
            pass


def factory(*, clock, delivery, emit):
    """Production adapter: require an explicit path, never silently lose tasks."""
    path = os.environ.get("MOCHI_TASK_DB")
    if not path:
        raise ValueError("set MOCHI_TASK_DB to a persistent SQLite path, or use isolated_factory for tests/replay")
    return SQLiteTaskService(clock, delivery, emit, path=path)


def isolated_factory(*, clock, delivery, emit):
    """A real, separate on-disk SQLite DB per environment, only for tests/replay."""
    directory = TemporaryDirectory(prefix="mochi-b-", ignore_cleanup_errors=True)
    try:
        service = SQLiteTaskService(clock, delivery, emit, path=Path(directory.name)/"tasks.sqlite3")
    except BaseException:
        directory.cleanup()
        raise
    service._temporary = directory
    return service
