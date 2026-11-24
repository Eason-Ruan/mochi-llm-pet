"""Short transactional presentation permits and a durable OS delivery ledger."""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
import sqlite3
from uuid import uuid4

from deskpet.contracts import DisplayCommand, TaskView
from deskpet.system2.semantic_policy import semantic_background_enabled


PRESENTATION_TABLE = """
CREATE TABLE IF NOT EXISTS presentations (
    delivery_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, task_version INTEGER NOT NULL,
    occurrence INTEGER NOT NULL, channel TEXT NOT NULL, state TEXT NOT NULL,
    owner_session TEXT NOT NULL, os_id TEXT, deadline TEXT, reason TEXT, updated_at TEXT NOT NULL,
    native_actions INTEGER NOT NULL DEFAULT 0, auto_followup_suppressed INTEGER NOT NULL DEFAULT 0,
    os_requested_for TEXT
)
"""

NATIVE_FOLLOWUP_TABLE = """
CREATE TABLE IF NOT EXISTS native_followup_paints (
    task_id TEXT NOT NULL, occurrence INTEGER NOT NULL, PRIMARY KEY(task_id, occurrence)
)
"""

ATTENTION_SOUND_TABLE = """
CREATE TABLE IF NOT EXISTS attention_sound_requests (
    user_scope TEXT NOT NULL, delivery_id TEXT NOT NULL, contact_kind TEXT NOT NULL,
    requested_at TEXT NOT NULL, batch_at TEXT NOT NULL, PRIMARY KEY(user_scope, delivery_id)
)
"""

ATTENTION_SOUND_INDEX = """
CREATE INDEX IF NOT EXISTS attention_sound_batch
ON attention_sound_requests(user_scope, batch_at, requested_at)
"""

RELEASED_STATES = {"cancelled", "failed", "released", "unavailable"}
NATIVE_OWNER_STATES = {"submitting", "scheduled", "submitted", "delivered", "unknown", "cancel_intent", "cancelling"}
EARLY_OS_TRANSFER = "semantic_early_transfer"
_TRANSFER_MARGIN = timedelta(seconds=5)
_SOUND_BATCH_WINDOW = timedelta(seconds=1)


def notification_id(delivery_id: str, *, scope: str = "") -> str:
    """Use opaque platform identifiers, including a profile namespace on macOS."""
    return sha256((f"{scope}\0{delivery_id}" if scope else delivery_id).encode()).hexdigest()


def native_followup_due(task, row, now, *, platform_response=False):
    """Return the time of an actionable native reminder for ordering in the UI."""
    if (row["channel"] != "os_notification" or row["occurrence"] != task.occurrence
            or (task.draft.explicit_start_at and now < task.draft.explicit_start_at)):
        return None
    deadline = datetime.fromisoformat(row["deadline"]) if row["deadline"] else None
    if deadline != task.draft.hard_deadline:
        return None
    recorded_at = datetime.fromisoformat(row["updated_at"])
    if row["state"] == "delivered" or (platform_response and row["state"] in NATIVE_OWNER_STATES):
        return recorded_at
    if row["state"] in NATIVE_OWNER_STATES:
        due = deadline or recorded_at
        if now >= due:
            return due
    return None


def running_focus(db):
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='phase_views'").fetchone() is None:
        return False
    row = db.execute("SELECT view FROM phase_views WHERE active=1").fetchone()
    view = json.loads(row[0]) if row else {}
    return view.get("kind") == "focus" and view.get("state") == "running"


def _stored_preferences(db, user_scope="default"):
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pet_settings'").fetchone() is None:
        return {}
    row = db.execute("SELECT settings FROM pet_settings WHERE user_scope=?", (user_scope,)).fetchone()
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
        if ((task.version, task.occurrence) != (version, occurrence) or not task.active_reminder
                or task.displayed_at is not None or (task.draft.expires_at and now >= task.draft.expires_at)):
            return None
        return task

    def _write_owner(self, db, command_id, task, channel, state, now, identifier=None, reason=None,
                     native_actions=False, os_requested_for=None):
        deadline = task.draft.hard_deadline
        db.execute("""INSERT OR REPLACE INTO presentations
            (delivery_id, task_id, task_version, occurrence, channel, state, owner_session, os_id,
             deadline, reason, updated_at, native_actions, os_requested_for) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            command_id, task.task_id, task.version, task.occurrence, channel, state, self.session, identifier,
            deadline.isoformat() if deadline is not None else None, reason, now.isoformat(), native_actions,
            os_requested_for.isoformat() if os_requested_for is not None else None,
        ))
        self._event(db, command_id, task.task_id, task.version, channel, state, now, reason)

    def _event(self, db, delivery_id, task_id, version, channel, state, now, reason):
        event = {"submitting": "presentation.claimed", "permitted": "presentation.claimed",
                 "painted": "card.painted", "badge_shown": "badge.painted",
                 "dismissed": "presentation.dismissed",
                 "submitted": "os.accepted", "unavailable": "os.failed"}.get(state)
        if state == "painted" and reason == "menu_painted":
            event = "presentation.menu_painted"
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
                    if (current is None or current.draft.sound is False
                            or not command.requested_at <= now < command.expires_at):
                        return False
                    if command.reason in {"opportunity", "tool"} and self.authorize_early is not None:
                        if not self.authorize_early(db, command, now):
                            return False
                    row = db.execute("SELECT state,owner_session FROM presentations WHERE delivery_id=?",
                                     (command.delivery_id,)).fetchone()
                    if row is None or row["state"] != "painted" or row["owner_session"] != self.session:
                        return False
                    scope = self._reminder_scope(db, command.delivery_id)
                    batch_at = current.draft.hard_deadline if command.reason in {"hard_due", "deadline_fallback"} else now
                    if not self._reserve_sound(db, command.delivery_id, "reminder", now, batch_at,
                                               command.sound, scope):
                        return False
                self._record_sound(db, command.delivery_id, command.sound, now, played, reason,
                                   task_id=command.task_id, task_version=command.task_version)
                return True
        except sqlite3.OperationalError:
            return False

    @staticmethod
    def _reminder_scope(db, delivery_id):
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='system2_reminder_authority'").fetchone():
            row = db.execute("SELECT user_scope FROM system2_reminder_authority WHERE delivery_id=?",
                             (delivery_id,)).fetchone()
            if row:
                return row[0]
        return "default"

    @staticmethod
    def _reserve_sound(db, delivery_id, contact_kind, now, batch_at, kind, user_scope):
        key = {"phase_end": "phase_end_sound", "reminder": "reminder_sound"}.get(kind)
        if key is None or not _stored_preferences(db, user_scope).get(key, True):
            return False
        now = now.astimezone(UTC)
        batch_at = (batch_at or now).astimezone(UTC)
        at = now.isoformat(timespec="microseconds")
        batch = batch_at.isoformat(timespec="microseconds")
        recent = (now - _SOUND_BATCH_WINDOW).isoformat(timespec="microseconds")
        if db.execute("""SELECT 1 FROM attention_sound_requests WHERE user_scope=?
            AND (delivery_id=? OR batch_at=? OR requested_at BETWEEN ? AND ?) LIMIT 1""",
            (user_scope, delivery_id, batch, recent, at)).fetchone():
            return False
        db.execute("INSERT INTO attention_sound_requests VALUES (?,?,?,?,?)",
                   (user_scope, delivery_id, contact_kind, at, batch))
        return True

    def _record_sound(self, db, delivery_id, kind, now, requested, reason, **subject):
        fields = {**subject, "delivery_id": delivery_id, "source": self.source,
                  "simulated": self.source != "native", "payload": {"sound": kind, "reason": reason}}
        db.execute("INSERT INTO events(recorded_at,kind,fields) VALUES (?,?,?)",
                   (now.isoformat(), "sound.requested" if requested else "sound.skipped", json.dumps(fields)))

    def request_attention_sound(self, delivery_id, contact_kind, kind, now, *, batch_at=None, origin=None):
        """Claim the single sound request shared by native, Qt, and phase presentations."""
        try:
            with self._transaction() as db:
                scope = "default"
                if contact_kind == "reminder":
                    row = db.execute("SELECT * FROM presentations WHERE delivery_id=?", (delivery_id,)).fetchone()
                    if row is None:
                        return False
                    grouped = row["state"] == "passive" and row["reason"] == "foreground_batch"
                    if grouped:
                        if (origin != "qt" or not row["deadline"] or row["owner_session"] != self.session
                                or batch_at != datetime.fromisoformat(row["deadline"]) or batch_at > now):
                            return False
                    elif row["state"] not in {"painted", "submitting"}:
                        return False
                    task = self._current(db, row["task_id"], row["task_version"], row["occurrence"], now)
                    if task is None or task.draft.sound is False or (grouped and task.draft.sound is not True):
                        return False
                    scope = self._reminder_scope(db, delivery_id)
                elif contact_kind in {"proposal", "pet"}:
                    table, state = (("system2_proposal_presentations", "displayed") if contact_kind == "proposal"
                                    else ("system2_pet_intents", "claimed"))
                    row = db.execute(f"SELECT user_scope FROM {table} WHERE delivery_id=? AND state=?",
                                     (delivery_id, state)).fetchone()
                    if row is None:
                        return False
                    scope = row["user_scope"]
                if not self._reserve_sound(db, delivery_id, contact_kind, now, batch_at, kind, scope):
                    return False
                self._record_sound(db, delivery_id, kind, now, True, origin)
                return True
        except sqlite3.OperationalError:
            return False

    @staticmethod
    def _other_owner(db, task, command_id=None):
        return db.execute("""SELECT * FROM presentations WHERE task_id=? AND delivery_id IS NOT ?
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

    def prepare_os(self, task, delivery_id, identifier, now, *, command=None, native_actions=False,
                   requested_for=None):
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
                self._write_owner(db, delivery_id, task, "os_notification", "submitting", now, identifier,
                                  reason=EARLY_OS_TRANSFER if transferred else None, native_actions=native_actions,
                                  os_requested_for=requested_for)
                return True
        except sqlite3.OperationalError:
            return False

    def _early_transfer_task(self, db, command, now):
        task = self._eligible_qt(db, command, now)
        if (task is None or task.draft.mode != "soft" or task.draft.hard_deadline is None
                or now + _TRANSFER_MARGIN >= task.draft.hard_deadline
                or command.reason != "tool" or self.authorize_early is None
                or not self.authorize_early(db, command, now)):
            return None
        return task

    def begin_early_transfer(self, command, now):
        """Keep the existing OS owner until cancellation can be verified at the platform."""
        try:
            with self._transaction() as db:
                task = self._early_transfer_task(db, command, now)
                if task is None:
                    return None
                # A metadata edit can leave the fallback under the previous delivery ID.
                row = self._other_owner(db, task)
                if (row is None or row["channel"] != "os_notification" or row["state"] != "scheduled"
                        or not row["os_id"] or row["occurrence"] != task.occurrence
                        or row["deadline"] != task.draft.hard_deadline.isoformat()
                        or self._other_owner(db, task, row["delivery_id"])):
                    return None
                owner_id = row["delivery_id"]
                db.execute("UPDATE presentations SET state='cancel_intent', reason=?, updated_at=? WHERE delivery_id=?",
                           (EARLY_OS_TRANSFER, now.isoformat(), owner_id))
                self._event(db, owner_id, row["task_id"], row["task_version"], row["channel"],
                            "cancel_intent", now, EARLY_OS_TRANSFER)
                return dict(row)
        except sqlite3.OperationalError:
            return None

    def can_finish_early_transfer(self, owner_id, command, now):
        try:
            with self._transaction() as db:
                row = db.execute("""SELECT * FROM presentations WHERE delivery_id=? AND channel='os_notification'
                    AND reason=? AND state IN ('cancel_intent','cancelling','cancelled')""",
                    (owner_id, EARLY_OS_TRANSFER)).fetchone()
                if row is None or row["task_id"] != command.task_id or row["occurrence"] != command.occurrence:
                    return False
                task = self._early_transfer_task(db, command, now)
                return task is not None and row["deadline"] == task.draft.hard_deadline.isoformat()
        except sqlite3.OperationalError:
            # A busy writer can be committing the selected effect.
            return True

    def legacy_os_deadline_intent(self, delivery_id, deadline):
        """Recover the original request time for rows written before it was stored directly."""
        try:
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=0.01)) as db:
                event = db.execute("""SELECT kind,recorded_at FROM events
                    WHERE kind IN ('os.scheduled','presentation.claimed')
                    AND json_extract(fields, '$.delivery_id')=?
                    AND json_extract(fields, '$.payload.channel')='os_notification'
                    ORDER BY rowid DESC LIMIT 1""", (delivery_id,)).fetchone()
            return bool(event and (event[0] == "os.scheduled" or datetime.fromisoformat(event[1]) >= deadline))
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

    def mark_painted(self, command, now, *, passive=False):
        state = "badge_shown" if command.channel == "pet_badge" else "painted"
        return self.update(command.delivery_id, {"permitted"}, state, now,
                           "menu_painted" if passive else None, current_session=True)

    def record_reminder_dismissal(self, delivery_id, now):
        try:
            with self._transaction() as db:
                row = db.execute("SELECT * FROM presentations WHERE delivery_id=?", (delivery_id,)).fetchone()
                if row is None or not (row["state"] == "painted" or db.execute(
                    "SELECT 1 FROM native_followup_paints WHERE task_id=? AND occurrence=?",
                    (row["task_id"], row["occurrence"]),
                ).fetchone()):
                    return False
                if db.execute("""SELECT 1 FROM events WHERE kind='presentation.dismissed'
                    AND json_extract(fields, '$.delivery_id')=?""", (delivery_id,)).fetchone():
                    return False
                self._event(db, delivery_id, row["task_id"], row["task_version"], row["channel"],
                            "dismissed", now, "user_closed")
                return True
        except sqlite3.OperationalError:
            return False

    def native_followup_current(self, followup, now, *, include_painted=False, mark_painted=False):
        """Check the occurrence at paint/action time without changing its native owner."""
        try:
            with self._transaction() as db:
                task = self._current(db, followup.task_id, followup.task_version, followup.occurrence, now)
                if task is None:
                    return False
                row = db.execute("SELECT * FROM presentations WHERE task_id=? AND delivery_id=?",
                                 (task.task_id, followup.delivery_id)).fetchone()
                if (row is None or row["native_actions"] or row["auto_followup_suppressed"]
                        or native_followup_due(task, row, now) is None):
                    return False
                key = (task.task_id, task.occurrence)
                if mark_painted:
                    if not db.execute("INSERT OR IGNORE INTO native_followup_paints VALUES (?,?)", key).rowcount:
                        return False
                    self._event(db, followup.delivery_id, task.task_id, task.version, "pet_card",
                                "native_followup_painted", now, row["state"])
                elif not include_painted and db.execute("SELECT 1 FROM native_followup_paints "
                                                       "WHERE task_id=? AND occurrence=?", key).fetchone():
                    return False
                return True
        except sqlite3.OperationalError:
            return False

    def foreground_target(self, identifier, now):
        with self._transaction() as db:
            row = db.execute("SELECT * FROM presentations WHERE os_id=? AND channel='os_notification'",
                             (identifier,)).fetchone()
            if (row is None or not self._scoped_native_owner(row, identifier)
                    or row["state"] not in NATIVE_OWNER_STATES):
                return None, None
            return dict(row), self._current_native_task(db, row, now)

    @staticmethod
    def _current_native_task(db, row, now):
        view = db.execute("SELECT view FROM tasks WHERE task_id=?", (row["task_id"],)).fetchone()
        task = TaskView.model_validate_json(view[0]) if view else None
        deadline = datetime.fromisoformat(row["deadline"]) if row["deadline"] else None
        if (task is None or not task.active_reminder or task.occurrence != row["occurrence"]
                or task.draft.hard_deadline != deadline or task.displayed_at
                or (task.draft.expires_at and now >= task.draft.expires_at)):
            return None
        return task

    def claim_foreground_pet(self, row, now, *, grouped=False):
        """Switch at the OS presentation decision, with no withdrawal before the deadline."""
        try:
            with self._transaction() as db:
                current = db.execute("SELECT * FROM presentations WHERE delivery_id=?",
                                     (row["delivery_id"],)).fetchone()
                if (current is None or current["os_id"] != row["os_id"]
                        or current["channel"] != "os_notification" or current["state"] not in NATIVE_OWNER_STATES):
                    return False
                task = self._current_native_task(db, current, now)
                if task is None:
                    return "stale"
                if not current["deadline"] or now < datetime.fromisoformat(current["deadline"]):
                    return False
                self._event(db, row["delivery_id"], task.task_id, task.version, "os_notification",
                            "released_to_foreground", now, None)
                self._write_owner(db, row["delivery_id"], task, "pet_badge" if grouped else "pet_card",
                                  "passive" if grouped else "permitted", now, identifier=current["os_id"],
                                  reason="foreground_batch" if grouped else "foreground_takeover")
                return "claimed"
        except sqlite3.OperationalError:
            return False

    def record_passive(self, task, delivery_id, now):
        try:
            with self._transaction() as db:
                task = self._current(db, task.task_id, task.version, task.occurrence, now)
                if task is None:
                    return False
                for row in db.execute("""SELECT * FROM presentations WHERE task_id=? AND occurrence=?
                    AND channel='os_notification' AND native_actions=0 AND auto_followup_suppressed=0""",
                    (task.task_id, task.occurrence)).fetchall():
                    if native_followup_due(task, row, now) is not None:
                        db.execute("UPDATE presentations SET auto_followup_suppressed=1 WHERE delivery_id=?",
                                   (row["delivery_id"],))
                deadline = task.draft.hard_deadline
                if deadline is None or deadline > now or self._other_owner(db, task, delivery_id):
                    return False
                row = db.execute("SELECT state FROM presentations WHERE delivery_id=?", (delivery_id,)).fetchone()
                if row is not None and row["state"] not in RELEASED_STATES:
                    return False
                self._write_owner(db, delivery_id, task, "pet_badge", "passive", now, reason="resume_catchup")
                return True
        except sqlite3.OperationalError:
            return False

    def native_response_target(self, identifier, action, now, *, record_exposure=False):
        """Resolve an OS click from the local profile's durable presentation owner."""
        if action not in {"open", "ack", "snooze"}:
            return None
        with self._transaction() as db:
            rows = db.execute("""SELECT p.*, t.view FROM presentations p JOIN tasks t USING(task_id)
                WHERE p.os_id=? AND p.channel='os_notification' LIMIT 2""", (identifier,)).fetchall()
            if len(rows) != 1:
                return None
            row = rows[0]
            if row["native_actions"]:
                if not self._scoped_native_owner(row, identifier):
                    return None
            elif action != "open":
                return None
            task = TaskView.model_validate_json(row["view"])
            if (not task.active_reminder or (task.draft.expires_at and now >= task.draft.expires_at)
                    or native_followup_due(task, row, now, platform_response=record_exposure) is None):
                return None
            if record_exposure and row["state"] != "delivered":
                db.execute("UPDATE presentations SET state='delivered', reason=?, updated_at=? WHERE delivery_id=?",
                           ("user_response", now.isoformat(), row["delivery_id"]))
                self._event(db, row["delivery_id"], row["task_id"], row["task_version"], "os_notification",
                            "delivered", now, "user_response")
            return task

    def _scoped_native_owner(self, row, identifier):
        return row["native_actions"] and identifier == notification_id(row["delivery_id"], scope=self.path.as_uri())

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
                return [dict(row) for row in db.execute("""SELECT p.*, f.task_id IS NOT NULL AS followup_painted
                    FROM presentations p LEFT JOIN native_followup_paints f USING(task_id, occurrence)
                    ORDER BY task_version""")]
            finally:
                db.close()
        except sqlite3.OperationalError:
            return []
