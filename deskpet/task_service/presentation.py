"""Short transactional presentation permits and a durable OS delivery ledger."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import sqlite3
from uuid import uuid4

from deskpet.contracts import DisplayCommand, TaskView
from deskpet.system2.semantic_policy import semantic_background_enabled


PRESENTATION_TABLE = """
CREATE TABLE IF NOT EXISTS presentations (
    delivery_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, task_version INTEGER NOT NULL,
    occurrence INTEGER NOT NULL, channel TEXT NOT NULL, state TEXT NOT NULL,
    owner_session TEXT NOT NULL, os_id TEXT, deadline TEXT, reason TEXT, updated_at TEXT NOT NULL
)
"""

RELEASED_STATES = {"cancelled", "failed", "released", "unavailable"}
EARLY_OS_TRANSFER = "semantic_early_transfer"
_TRANSFER_MARGIN = timedelta(seconds=5)


def running_focus(db):
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='phase_views'").fetchone() is None:
        return False
    row = db.execute("SELECT view FROM phase_views WHERE active=1").fetchone()
    view = json.loads(row[0]) if row else {}
    return view.get("kind") == "focus" and view.get("state") == "running"


def _stored_preferences(db):
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pet_settings'").fetchone() is None:
        return {}
    row = db.execute("SELECT settings FROM pet_settings WHERE user_scope='default'").fetchone()
    return json.loads(row[0]) if row else {}


class PresentationLedger:
    def __init__(self, path, *, source="reference", authorize_early=None):
        self.path = path
        self.authorize_early = authorize_early
        self.session = uuid4().hex
        self.source = source

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=0.025, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
        finally:
            db.close()

    @staticmethod
    def _current(db, task_id, version, occurrence, now):
        row = db.execute("SELECT view FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            return None
        task = TaskView.model_validate_json(row[0])
        if ((task.version, task.occurrence) != (version, occurrence) or task.status != "registered"
                or task.displayed_at is not None or task.acknowledged_at is not None
                or not task.draft.reminder_enabled or (task.draft.expires_at and now >= task.draft.expires_at)):
            return None
        return task

    def _write_owner(self, db, command_id, task, channel, state, now, identifier=None, reason=None):
        deadline = task.draft.hard_deadline
        db.execute("INSERT OR REPLACE INTO presentations VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
            command_id, task.task_id, task.version, task.occurrence, channel, state, self.session, identifier,
            deadline.isoformat() if deadline is not None else None, reason, now.isoformat(),
        ))
        self._event(db, command_id, task.task_id, task.version, channel, state, now, reason)

    def _event(self, db, delivery_id, task_id, version, channel, state, now, reason):
        event = {"submitting": "presentation.claimed", "permitted": "presentation.claimed",
                 "painted": "card.painted", "badge_shown": "badge.painted",
                 "submitted": "os.accepted", "unavailable": "os.failed"}.get(state)
        if event is None:
            event = f"os.{state}" if channel == "os_notification" else f"presentation.{state}"
        fields = {"task_id": task_id, "task_version": version, "delivery_id": delivery_id,
                  "source": self.source, "simulated": self.source != "native",
                  "payload": {"channel": channel, "state": state, "reason": reason}}
        db.execute("INSERT INTO events(recorded_at,kind,fields) VALUES (?,?,?)",
                   (now.isoformat(), event, json.dumps(fields, ensure_ascii=False)))

    def sound_event(self, command, now, played, reason):
        try:
            with self._transaction() as db:
                if played:
                    current = self._current(db, command.task_id, command.task_version, command.occurrence, now)
                    if current is None or not command.requested_at <= now < command.expires_at:
                        return False
                    if command.reason in {"opportunity", "tool"} and self.authorize_early is not None:
                        if not self.authorize_early(db, command, now):
                            return False
                    preferences = _stored_preferences(db)
                    key = "phase_end_sound" if command.sound == "phase_end" else "reminder_sound"
                    if not preferences.get(key, True):
                        return False
                    row = db.execute("SELECT state,owner_session FROM presentations WHERE delivery_id=?",
                                     (command.delivery_id,)).fetchone()
                    if row is None or row["state"] != "painted" or row["owner_session"] != self.session:
                        return False
                fields = {"task_id": command.task_id, "task_version": command.task_version,
                          "delivery_id": command.delivery_id, "source": self.source, "simulated": self.source != "native",
                          "payload": {"sound": command.sound, "reason": reason}}
                db.execute("INSERT INTO events(recorded_at,kind,fields) VALUES (?,?,?)",
                           (now.isoformat(), "sound.requested" if played else "sound.skipped", json.dumps(fields)))
                return True
        except sqlite3.OperationalError:
            return False

    @staticmethod
    def _other_owner(db, task, command_id):
        return db.execute("""SELECT * FROM presentations WHERE task_id=? AND delivery_id<>?
                             AND state NOT IN ('cancelled','failed','released','unavailable','grouped')
                             AND (occurrence=? OR (channel='os_notification' AND state IN
                                  ('submitting','scheduled','cancel_intent','cancelling'))) LIMIT 1""",
                          (task.task_id, command_id, task.occurrence)).fetchone()

    def permit_qt(self, command, now):
        """Read current authority and reserve one active channel immediately before a visible paint."""
        if command.channel != "pet_card":
            return False
        return self._permit_pet(command, now, "pet_card")

    def permit_badge(self, command, now):
        if command.channel != "pet_badge":
            return False
        return self._permit_pet(command, now, "pet_badge")

    def _permit_pet(self, command, now, channel):
        try:
            with self._transaction() as db:
                task = self._eligible_qt(db, command, now)
                if task is None:
                    return False
                if command.reason in {"opportunity", "tool"}:
                    if self.authorize_early is not None:
                        if not self.authorize_early(db, command, now):
                            return False
                    else:
                        close = task.draft.window_close_at
                        if (close is not None and now >= close) or running_focus(db):
                            return False
                if self._other_owner(db, task, command.delivery_id):
                    return False
                row = db.execute("SELECT * FROM presentations WHERE delivery_id=?", (command.delivery_id,)).fetchone()
                if row and row["state"] not in RELEASED_STATES | {"grouped"}:
                    return row["channel"] == channel and row["state"] == "permitted" and row["owner_session"] == self.session
                self._write_owner(db, command.delivery_id, task, channel, "permitted", now)
                return True
        except sqlite3.OperationalError:
            return False

    def _eligible_qt(self, db, command, now):
        task = self._current(db, command.task_id, command.task_version, command.occurrence, now)
        if task is None or not command.requested_at <= now < command.expires_at:
            return None
        if semantic_background_enabled() and task.draft.explicit_start_at and now < task.draft.explicit_start_at:
            return None
        row = db.execute("SELECT command FROM deliveries WHERE delivery_id=? AND active=1",
                         (command.delivery_id,)).fetchone()
        return task if row is not None and DisplayCommand.model_validate_json(row[0]) == command else None

    def can_group(self, command, now):
        try:
            with self._transaction() as db:
                task = self._eligible_qt(db, command, now)
                if task is None or self._other_owner(db, task, command.delivery_id):
                    return False
                row = db.execute("SELECT state FROM presentations WHERE delivery_id=?", (command.delivery_id,)).fetchone()
                return row is None or row["state"] in RELEASED_STATES
        except sqlite3.OperationalError:
            return False

    def record_grouped(self, commands, now):
        try:
            with self._transaction() as db:
                for command in commands:
                    task = self._eligible_qt(db, command, now)
                    if task is None or self._other_owner(db, task, command.delivery_id):
                        continue
                    row = db.execute("SELECT state FROM presentations WHERE delivery_id=? AND state NOT IN "
                                     "('cancelled','failed','released','unavailable')", (command.delivery_id,)).fetchone()
                    if row is None:
                        self._write_owner(db, command.delivery_id, task, "pet_badge", "grouped", now)
        except sqlite3.OperationalError:
            pass

    def prepare_os(self, task, delivery_id, identifier, now, *, command=None):
        """Commit the OS submission intent first. A lost result is ambiguous after restart."""
        try:
            with self._transaction() as db:
                if self._current(db, task.task_id, task.version, task.occurrence, now) is None:
                    return False
                if not _stored_preferences(db).get("allow_system_notifications", True):
                    return False
                if command is not None:
                    if (command.channel != "os_notification" or command.delivery_id != delivery_id
                            or self._eligible_qt(db, command, now) is None or self.authorize_early is None
                            or not self.authorize_early(db, command, now)):
                        return False
                existing = db.execute("SELECT state, reason, updated_at FROM presentations WHERE delivery_id=?",
                                      (delivery_id,)).fetchone()
                if self._other_owner(db, task, delivery_id):
                    return False
                transferred = existing and existing["state"] == "cancelled" and existing["reason"] == EARLY_OS_TRANSFER
                if existing and existing["state"] != "unavailable" and not transferred:
                    if existing["state"] != "failed" or now < datetime.fromisoformat(existing["updated_at"]) + timedelta(seconds=5):
                        return False
                    tries = db.execute("""SELECT count(*) FROM events WHERE kind='presentation.claimed'
                        AND json_extract(fields, '$.delivery_id')=?
                        AND json_extract(fields, '$.payload.channel')='os_notification'""", (delivery_id,)).fetchone()[0]
                    if tries >= 3:
                        return False
                self._write_owner(db, delivery_id, task, "os_notification", "submitting", now, identifier)
                return True
        except sqlite3.OperationalError:
            return False

    def _can_transfer_early(self, db, command, now):
        task = self._eligible_qt(db, command, now)
        return (task is not None and task.draft.mode == "soft" and task.draft.hard_deadline is not None
                and now + _TRANSFER_MARGIN < task.draft.hard_deadline
                and command.reason == "tool" and self.authorize_early is not None
                and self.authorize_early(db, command, now))

    def begin_early_transfer(self, command, now):
        """Keep the existing OS owner until cancellation can be verified at the platform."""
        try:
            with self._transaction() as db:
                row = db.execute("""SELECT * FROM presentations WHERE delivery_id=? AND channel='os_notification'
                    AND task_version=? AND occurrence=? AND state='scheduled'""",
                    (command.delivery_id, command.task_version, command.occurrence)).fetchone()
                if row is None or not self._can_transfer_early(db, command, now):
                    return None
                db.execute("UPDATE presentations SET state='cancel_intent', reason=?, updated_at=? WHERE delivery_id=?",
                           (EARLY_OS_TRANSFER, now.isoformat(), command.delivery_id))
                self._event(db, command.delivery_id, row["task_id"], row["task_version"], row["channel"],
                            "cancel_intent", now, EARLY_OS_TRANSFER)
                return dict(row)
        except sqlite3.OperationalError:
            return None

    def can_finish_early_transfer(self, command, now):
        try:
            with self._transaction() as db:
                return self._can_transfer_early(db, command, now)
        except sqlite3.OperationalError:
            # A busy writer can be committing the selected effect.
            return True

    def os_sound_enabled(self):
        try:
            with self._transaction() as db:
                return _stored_preferences(db).get("reminder_sound", True)
        except sqlite3.OperationalError:
            return False

    def update(self, delivery_id, old_states, state, now, reason=None, *, current_session=False):
        try:
            with self._transaction() as db:
                row = db.execute("SELECT * FROM presentations WHERE delivery_id=?", (delivery_id,)).fetchone()
                if (row is None or row["state"] not in old_states
                        or (current_session and row["owner_session"] != self.session)):
                    return False
                db.execute("UPDATE presentations SET state=?, reason=?, updated_at=? WHERE delivery_id=?",
                           (state, reason, now.isoformat(), delivery_id))
                self._event(db, delivery_id, row["task_id"], row["task_version"], row["channel"], state, now, reason)
                return True
        except sqlite3.OperationalError:
            return False

    def mark_painted(self, command, now):
        state = "badge_shown" if command.channel == "pet_badge" else "painted"
        return self.update(command.delivery_id, {"permitted"}, state, now, current_session=True)

    def record_unavailable(self, task, delivery_id, now, reason):
        try:
            with self._transaction() as db:
                if self._current(db, task.task_id, task.version, task.occurrence, now) is None:
                    return
                if not db.execute("SELECT 1 FROM presentations WHERE delivery_id=?", (delivery_id,)).fetchone():
                    self._write_owner(db, delivery_id, task, "os_notification", "unavailable", now, reason=reason)
        except sqlite3.OperationalError:
            pass

    def rows(self):
        try:
            db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=0.01)
            db.row_factory = sqlite3.Row
            try:
                return [dict(row) for row in db.execute("SELECT * FROM presentations ORDER BY task_version")]
            finally:
                db.close()
        except sqlite3.OperationalError:
            return []
