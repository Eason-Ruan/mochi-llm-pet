"""Persist intent ownership and authorize effects at the business commit boundary."""

from __future__ import annotations

from contextlib import closing, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from deskpet.observation_v2.routes import CloudRouteError

from .intent import allows, ref_id
from .protocol import ToolAuthority, UiEvent
from .tool_schema import READ_TOOLS, canonical_tool_name


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def stable_id(*values: str) -> str:
    return sha256(json_text(values).encode()).hexdigest()


def timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


_CHAT_HISTORY_SCHEMA = """CREATE TABLE IF NOT EXISTS s2_chat_history(
    user_scope TEXT PRIMARY KEY, generation INTEGER NOT NULL, admission_cutoff INTEGER NOT NULL)"""
_CLEARED_REPLY = "{}"


def chat_history_generation(db, scope):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='s2_chat_history'").fetchone():
        return 0
    row = db.execute("SELECT generation FROM s2_chat_history WHERE user_scope = ?", (scope,)).fetchone()
    return row[0] if row else 0


def clear_chat_admissions(db, scope):
    """Advance the chat boundary in the same transaction as the desktop history wipe."""
    has_admissions = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='s2_admissions'").fetchone()
    if has_admissions and db.execute("""
        SELECT 1 FROM s2_admissions WHERE user_scope = ? AND source = 'user'
            AND state IN ('queued', 'running') AND json_type(user_intent, '$.utterance') = 'text' LIMIT 1
    """, (scope,)).fetchone():
        return False
    cutoff = db.execute("""
        SELECT COALESCE(MAX(sequence), 0) FROM s2_admissions WHERE user_scope = ?
    """, (scope,)).fetchone()[0] if has_admissions else 0
    db.execute(_CHAT_HISTORY_SCHEMA)
    db.execute("""
        INSERT INTO s2_chat_history VALUES (?, 1, ?) ON CONFLICT(user_scope) DO UPDATE
        SET generation = generation + 1, admission_cutoff = excluded.admission_cutoff
    """, (scope, cutoff))
    if has_admissions:
        db.execute("""
            UPDATE s2_admissions SET user_intent = json_remove(user_intent, '$.utterance')
            WHERE user_scope = ? AND source = 'user' AND json_type(user_intent, '$.utterance') = 'text'
        """, (scope,))
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='s2_ui_outbox'").fetchone():
        # The proposal card still needs the completion event ID to release its buttons.
        db.execute("DELETE FROM s2_ui_outbox WHERE user_scope = ? AND acknowledged = 1", (scope,))
        db.execute("UPDATE s2_ui_outbox SET payload = ? WHERE user_scope = ?", (_CLEARED_REPLY, scope))
    return True


def current_chat_reply(db, scope, sequence, answer_id):
    return db.execute("""
        SELECT 1 FROM s2_ui_outbox WHERE user_scope = ? AND sequence = ? AND answer_id = ? AND payload <> ?
    """, (scope, sequence, answer_id, _CLEARED_REPLY)).fetchone() is not None


_SCHEMA = (
    _CHAT_HISTORY_SCHEMA,
    """CREATE TABLE IF NOT EXISTS s2_owners(
        user_scope TEXT PRIMARY KEY, token TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS s2_admissions(
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        admission_id TEXT NOT NULL UNIQUE, user_scope TEXT NOT NULL, source TEXT NOT NULL,
        event TEXT NOT NULL, user_intent TEXT, fingerprint TEXT NOT NULL,
        created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
        state TEXT NOT NULL, intent_generation INTEGER NOT NULL DEFAULT 1,
        superseded_by TEXT, supersedes TEXT,
        grants TEXT, authority_context TEXT, initial_step_id TEXT, owner TEXT,
        replans INTEGER NOT NULL DEFAULT 0)""",
    "CREATE INDEX IF NOT EXISTS s2_queue ON s2_admissions(user_scope, state, sequence)",
    """CREATE TABLE IF NOT EXISTS s2_revoked_actions(
        admission_id TEXT NOT NULL, function TEXT NOT NULL, superseded_by TEXT NOT NULL,
        PRIMARY KEY(admission_id, function))""",
    """CREATE TABLE IF NOT EXISTS s2_steps(
        admission_id TEXT NOT NULL, step_id TEXT NOT NULL,
        intent_generation INTEGER NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(admission_id, step_id))""",
    """CREATE TABLE IF NOT EXISTS s2_calls(
        command_id TEXT PRIMARY KEY, admission_id TEXT NOT NULL, step_id TEXT NOT NULL,
        native_call_id TEXT NOT NULL, function TEXT NOT NULL,
        fingerprint TEXT NOT NULL, result TEXT, declared_reason TEXT, review_key TEXT,
        UNIQUE(admission_id, native_call_id))""",
    """CREATE TABLE IF NOT EXISTS s2_decisions(
        decision_id TEXT PRIMARY KEY, user_scope TEXT NOT NULL, as_of TEXT NOT NULL,
        record TEXT NOT NULL, model_reviewed_at TEXT, jev_review TEXT)""",
    """CREATE TABLE IF NOT EXISTS s2_ui_outbox(
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, user_scope TEXT NOT NULL,
        answer_id TEXT NOT NULL UNIQUE, reply_to_message_id TEXT NOT NULL, payload TEXT NOT NULL,
        acknowledged INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS s2_sessions(
        user_scope TEXT PRIMARY KEY, prompt_version TEXT NOT NULL,
        session_id TEXT NOT NULL, updated_at TEXT NOT NULL)""",
)
_RECOVERABLE = frozenset({"STALE_CONTEXT", "STALE_TASK", "STALE_VERSION", "STALE_PROPOSAL"})
_TOOL_BUDGET = 4
_REPLAN_BUDGET = 1


@dataclass(frozen=True, slots=True)
class Admission:
    admission_id: str
    source: str
    event: dict
    user_intent: dict | None
    created_at: datetime
    expires_at: datetime
    generation: int
    step_id: str | None
    state: str
    supersedes: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Admission:
        return cls(
            row["admission_id"], row["source"], json.loads(row["event"]),
            json.loads(row["user_intent"]) if row["user_intent"] else None,
            datetime.fromisoformat(row["created_at"]), datetime.fromisoformat(row["expires_at"]),
            row["intent_generation"], row["initial_step_id"], row["state"], row["supersedes"],
        )


class AdmissionStore:
    def __init__(self, path: Path, scope: str):
        self.path = path
        self.scope = scope
        self.owner = uuid4().hex
        with self.write() as db:
            for statement in _SCHEMA:
                db.execute(statement)
            call_columns = {row["name"] for row in db.execute("PRAGMA table_info(s2_calls)")}
            if "declared_reason" not in call_columns:
                db.execute("ALTER TABLE s2_calls ADD COLUMN declared_reason TEXT")
            if "review_key" not in call_columns:
                db.execute("ALTER TABLE s2_calls ADD COLUMN review_key TEXT")
            decision_columns = {row["name"] for row in db.execute("PRAGMA table_info(s2_decisions)")}
            for column in ("model_reviewed_at", "jev_review"):
                if column not in decision_columns:
                    db.execute(f"ALTER TABLE s2_decisions ADD COLUMN {column} TEXT")
            db.execute("""
                INSERT INTO s2_owners VALUES (?, ?) ON CONFLICT(user_scope)
                DO UPDATE SET token = excluded.token
            """, (scope, self.owner))
            db.execute("""
                UPDATE s2_admissions SET state = 'interrupted', intent_generation = intent_generation + 1
                WHERE user_scope = ? AND state = 'running'
            """, (scope,))

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            yield db

    @contextmanager
    def write(self):
        with self.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def _owned(self, db):
        return db.execute(
            "SELECT 1 FROM s2_owners WHERE user_scope = ? AND token = ?", (self.scope, self.owner),
        ).fetchone() is not None

    def stop(self):
        with self.write() as db:
            db.execute("""
                UPDATE s2_admissions SET state = 'interrupted', intent_generation = intent_generation + 1
                WHERE user_scope = ? AND state = 'running' AND owner = ?
            """, (self.scope, self.owner))

    def interrupt_background(self, db):
        """Fence an in-flight background judgment at the observed sleep boundary."""
        if self._owned(db):
            db.execute("""
                UPDATE s2_admissions SET state = 'interrupted', intent_generation = intent_generation + 1
                WHERE user_scope = ? AND source = 'background' AND state = 'running' AND owner = ?
            """, (self.scope, self.owner))

    def _insert(self, db, admission_id, source, event, user, at, expires, fingerprint, *, supersedes=None):
        db.execute("""
            INSERT INTO s2_admissions(
                admission_id, user_scope, source, event, user_intent, fingerprint, created_at,
                expires_at, state, supersedes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)
        """, (admission_id, self.scope, source, json_text(event), json_text(user) if user else None,
              fingerprint, timestamp(at), timestamp(expires), supersedes))

    def submit(self, event: dict, user: dict, at: datetime, ttl: timedelta, *,
               explicit_supersedes: str | None = None) -> str:
        with self.write() as db:
            if not self._owned(db):
                raise RuntimeError("System 2 host was replaced")
            event_id = event["trigger_ids"][0]
            admission_id = "s2a-" + stable_id(self.scope, "user", event_id)
            fingerprint = stable_id(json_text(user), explicit_supersedes or "")
            prior = db.execute("SELECT fingerprint FROM s2_admissions WHERE admission_id = ?",
                               (admission_id,)).fetchone()
            if prior is not None:
                if prior["fingerprint"] != fingerprint:
                    raise ValueError("A user event ID already refers to a different input")
                return admission_id
            target = None
            if explicit_supersedes:
                previous = db.execute("""
                    SELECT admission_id FROM s2_admissions
                    WHERE user_scope = ? AND source = 'user' AND admission_id = ?
                """, (self.scope, explicit_supersedes)).fetchone()
                if previous:
                    target = previous["admission_id"]
                    db.execute("""
                        UPDATE s2_admissions SET state = CASE
                            WHEN state IN ('queued', 'running') THEN 'superseded' ELSE state END,
                            intent_generation = intent_generation + 1, superseded_by = ?
                        WHERE admission_id = ?
                    """, (admission_id, target))
            self._insert(db, admission_id, "user", event, user, at, at + ttl, fingerprint, supersedes=target)
            return admission_id

    def supersede_direct(self, function, subject_ref, event_id, *, db=None):
        if function != "phase.start" and subject_ref is None:
            return
        fence = _action_fence(function, subject_ref)
        with self.write() if db is None else nullcontext(db) as db:
            if not self._owned(db):
                return
            # A newer, committed button action wins for the same operation.
            # Leave the model free to answer and take unrelated actions.
            db.execute("""
                INSERT OR IGNORE INTO s2_revoked_actions
                SELECT admission_id, ?, ? FROM s2_admissions
                WHERE user_scope = ? AND source = 'user' AND state IN ('queued', 'running')
                    AND json_type(user_intent, '$.utterance') = 'text'
            """, (fence, event_id, self.scope))

    def _queue_head(self, db):
        return db.execute("""
            SELECT * FROM s2_admissions WHERE user_scope = ? AND state = 'queued'
            ORDER BY source = 'user' DESC, sequence LIMIT 1
        """, (self.scope,)).fetchone()

    def claim(self, at, *, background=None, ack_wakes=None, claim_wakes=None):
        with self.write() as db:
            if not self._owned(db):
                return None
            row = self._queue_head(db)
            if row is None and background is not None:
                if claim_wakes is not None:
                    background = claim_wakes(background[0]["trigger_ids"], at, db)
                    if background is None:
                        return None
                event, expires = background
                fingerprint = stable_id(*event["trigger_ids"])
                identity = "s2a-" + stable_id(self.scope, "background", fingerprint)
                if db.execute("SELECT 1 FROM s2_admissions WHERE admission_id = ?", (identity,)).fetchone() is None:
                    self._insert(db, identity, "background", event, None, at, expires, fingerprint)
                if claim_wakes is None:
                    ack_wakes(event["trigger_ids"], db=db)
                row = self._queue_head(db)
            if row is None:
                return None
            admission_id = row["admission_id"]
            step_id = "s2step-" + uuid4().hex
            db.execute("""
                UPDATE s2_admissions SET state = 'running', initial_step_id = ?, owner = ?
                WHERE admission_id = ?
            """, (step_id, self.owner, admission_id))
            return Admission.from_row(db.execute(
                "SELECT * FROM s2_admissions WHERE admission_id = ?", (admission_id,),
            ).fetchone())

    def bind(self, admission: Admission, grants: dict, authority_context: dict) -> bool:
        with self.write() as db:
            update = db.execute("""
                UPDATE s2_admissions SET grants = ?, authority_context = ?
                WHERE admission_id = ? AND state = 'running' AND intent_generation = ? AND owner = ?
            """, (json_text(grants), json_text(authority_context), admission.admission_id,
                  admission.generation, self.owner))
            return update.rowcount == 1

    def state(self, admission_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM s2_admissions WHERE admission_id = ?", (admission_id,)).fetchone()
            return Admission.from_row(row) if row else None

    def interrupted(self):
        with self.connect() as db:
            if not self._owned(db):
                return ()
            return tuple(Admission.from_row(row) for row in db.execute("""
                SELECT * FROM s2_admissions AS a WHERE a.user_scope = ?
                AND a.state IN ('interrupted', 'superseded')
                AND NOT EXISTS (SELECT 1 FROM s2_decisions AS d WHERE d.decision_id = a.admission_id)
                ORDER BY a.sequence
            """, (self.scope,)))

    def register_call(self, admission, step_id, native_call_id, function, args, now, *, validation_error=None,
                      attempt_target=None):
        fingerprint = stable_id(function, json_text(args))
        command_id = "s2c-" + stable_id(admission.admission_id, native_call_id)
        with self.write() as db:
            prior = db.execute("SELECT fingerprint, result FROM s2_calls WHERE command_id = ?",
                               (command_id,)).fetchone()
            if prior:
                if prior["fingerprint"] != fingerprint:
                    return None, _failure(command_id, "IDEMPOTENCY_MISMATCH")
                if prior["result"]:
                    return None, json.loads(prior["result"])
            row = db.execute("SELECT * FROM s2_admissions WHERE admission_id = ?",
                             (admission.admission_id,)).fetchone()
            if not self._owned(db) or row["state"] != "running" or row["intent_generation"] != admission.generation:
                return None, _failure(command_id, "SUPERSEDED")
            if now < admission.created_at or admission.expires_at <= now:
                return None, _failure(command_id, "EXPIRED_REQUEST")
            revoked = db.execute("""
                SELECT 1 FROM s2_revoked_actions WHERE admission_id = ? AND function IN (?, ?)
            """, (admission.admission_id, function, _call_fence(function, args))).fetchone()
            if revoked:
                return None, _failure(command_id, "SUPERSEDED")
            grants = json.loads(row["grants"] or "{}")
            versions = json.loads(row["authority_context"] or "{}").get("expected_versions", {})
            semantic = admission.source == "user" and grants.get(function, {}).get("semantic_chat")
            rejection = None
            if validation_error is not None and semantic:
                rejection = _failure(command_id, "INVALID_ARGUMENT") | {"invalid_fields": validation_error}
            elif not allows(grants, function, args, versions=versions):
                if not semantic:
                    return None, _failure(command_id, "NOT_AUTHORIZED")
                rejection = _failure(command_id, "NOT_AUTHORIZED")
            if rejection is not None and attempt_target is not None:
                rejection["_attempt_target"] = attempt_target
            if prior:
                return None, _failure(command_id, "RESULT_PENDING", status="pending")
            review_key = None
            if admission.source == "background" and function not in READ_TOOLS:
                review_key = _background_review_key(function, args)
                if origin := _scope_review_origin(admission.event):
                    prior_effect = self._scope_review_prior_effect(db, origin, command_id, fingerprint, review_key)
                    if prior_effect is not None:
                        return None, prior_effect
            calls = db.execute("""
                SELECT function, fingerprint, result FROM s2_calls WHERE admission_id = ? ORDER BY rowid
            """, (admission.admission_id,)).fetchall()
            if len(calls) >= _TOOL_BUDGET:
                return None, _failure(command_id, "TOOL_BUDGET_EXHAUSTED")
            recoverable = _RECOVERABLE | {"INVALID_ARGUMENT", "NOT_AUTHORIZED"} if semantic else _RECOVERABLE
            if function not in READ_TOOLS:
                for call in reversed(calls):
                    if call["function"] != function or not call["result"]:
                        continue
                    if not semantic or call["fingerprint"] == fingerprint:
                        result = json.loads(call["result"])
                        if admission.source == "user" and result.get("error_code") not in recoverable:
                            return None, result
                        break
                write_results = [json.loads(call["result"]) for call in calls
                                 if call["function"] not in READ_TOOLS and call["result"]]
                if write_results and write_results[-1].get("error_code") in recoverable:
                    if row["replans"] >= _REPLAN_BUDGET:
                        return None, _failure(command_id, "REPLAN_BUDGET_EXHAUSTED")
                    db.execute("UPDATE s2_admissions SET replans = replans + 1 WHERE admission_id = ?",
                               (admission.admission_id,))
            db.execute("INSERT OR IGNORE INTO s2_steps VALUES (?, ?, ?, ?)",
                       (admission.admission_id, step_id, admission.generation, timestamp(now)))
            db.execute("""
                INSERT INTO s2_calls(
                    command_id, admission_id, step_id, native_call_id, function, fingerprint, declared_reason,
                    result, review_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (command_id, admission.admission_id, step_id, native_call_id, function, fingerprint,
                  args.get("reason_code") if function == "reminder.present" else None,
                  json_text(rejection) if rejection else None, review_key))
        return (None, rejection) if rejection else (command_id, None)

    def save_call_result(self, command_id, result):
        with self.write() as db:
            db.execute("UPDATE s2_calls SET result = ? WHERE command_id = ? AND result IS NULL",
                       (json_text(result), command_id))

    def calls(self, admission_id, *, db=None):
        with (self.connect() if db is None else nullcontext(db)) as db:
            return tuple((row["command_id"], row["function"], row["declared_reason"],
                          json.loads(row["result"]) if row["result"] else None)
                         for row in db.execute("SELECT * FROM s2_calls WHERE admission_id = ? ORDER BY rowid",
                                               (admission_id,)))

    def _background_write_results(self, db, admission_id):
        # Business receipts are written in the same transaction as the effect.
        # An interruption may land after that commit but before s2_calls is saved.
        durable_receipts = db.execute("""SELECT 1 FROM sqlite_master WHERE type='table'
            AND name='system2_command_receipts'""").fetchone()
        rows = db.execute("""
            SELECT c.command_id, c.function, c.fingerprint, c.review_key, c.result
            FROM s2_calls c JOIN s2_admissions a ON a.admission_id=c.admission_id
            WHERE c.admission_id=? AND a.user_scope=? AND a.source='background'
            ORDER BY c.rowid
        """, (admission_id, self.scope))
        for call in rows:
            if call["function"] in READ_TOOLS:
                continue
            recorded = json.loads(call["result"]) if call["result"] else None
            result = recorded
            if durable_receipts:
                stored = db.execute("""SELECT result FROM system2_command_receipts
                    WHERE user_scope=? AND command_id=?""", (self.scope, call["command_id"])).fetchone()
                if stored:
                    result = json.loads(stored["result"])
            yield call, result, bool(durable_receipts)

    def _background_effect_committed(self, db, admission_id):
        for _, result, durable_receipts in self._background_write_results(db, admission_id):
            if _committed_status(result) or not durable_receipts and _result_unknown(result):
                return True
        return False

    def _scope_review_prior_effect(self, db, origin, command_id, fingerprint, review_key):
        uncertain = False
        for call, result, _ in self._background_write_results(db, origin):
            if call["fingerprint"] != fingerprint and (review_key is None or call["review_key"] != review_key):
                continue
            if status := _committed_status(result):
                return {"call_id": command_id, "status": "duplicate", "original_status": status,
                        "outcome": "duplicate"}
            uncertain = uncertain or _result_unknown(result)
        return _failure(command_id, "RESULT_UNKNOWN") if uncertain else None

    def finish(self, admission, decision, reply, session_id, prompt_version, *, expected_state,
               recover_wakes=None, review_scope=None, model_reviewed_at=None, jev_review=None):
        with self.write() as db:
            if not self._owned(db):
                return
            row = db.execute("SELECT state, sequence FROM s2_admissions WHERE admission_id = ?",
                             (admission.admission_id,)).fetchone()
            if not row or row["state"] != expected_state:
                return
            inserted = db.execute("""INSERT OR IGNORE INTO s2_decisions
                (decision_id, user_scope, as_of, record, model_reviewed_at, jev_review) VALUES (?, ?, ?, ?, ?, ?)""",
                                  (admission.admission_id, self.scope, decision["as_of"], json_text(decision),
                                   timestamp(model_reviewed_at) if model_reviewed_at is not None else None,
                                   json_text(jev_review) if jev_review is not None else None))
            if inserted.rowcount != 1:
                return
            if admission.source == "background" and not _scope_review_origin(admission.event):
                if review_scope is not None:
                    review_scope(db)
                elif recover_wakes is not None and not self._background_effect_committed(db, admission.admission_id):
                    recover_wakes(db)
            if reply is not None:
                event_id = admission.user_intent["message_id"]
                reset = db.execute("SELECT admission_cutoff FROM s2_chat_history WHERE user_scope = ?",
                                   (self.scope,)).fetchone()
                payload = _CLEARED_REPLY if reset and row["sequence"] <= reset[0] else json_text(reply)
                db.execute("""
                    INSERT OR IGNORE INTO s2_ui_outbox(user_scope, answer_id, reply_to_message_id, payload)
                    VALUES (?, ?, ?, ?)
                """, (self.scope, "s2r-" + stable_id(admission.admission_id), event_id, payload))
            if session_id:
                db.execute("""
                    INSERT INTO s2_sessions VALUES (?, ?, ?, ?) ON CONFLICT(user_scope)
                    DO UPDATE SET prompt_version=excluded.prompt_version, session_id=excluded.session_id,
                        updated_at=excluded.updated_at
                """, (self.scope, prompt_version, session_id, decision["as_of"]))
            db.execute("""
                UPDATE s2_admissions SET state = 'complete' WHERE admission_id = ? AND state = 'running'
            """, (admission.admission_id,))

    def jev_tick_current(self, admission_id, *, db) -> bool:
        return self._owned(db) and db.execute("""
            SELECT 1 FROM s2_admissions AS tick
            WHERE tick.admission_id=? AND tick.user_scope=? AND tick.owner=?
              AND tick.state IN ('running','complete')
              AND NOT EXISTS (
                SELECT 1 FROM s2_admissions AS newer
                WHERE newer.user_scope=tick.user_scope AND (newer.sequence>tick.sequence
                    OR (newer.source='user' AND newer.state IN ('queued','running'))))
        """, (admission_id, self.scope, self.owner)).fetchone() is not None

    def jev_previous_review(self, admission_id, *, db):
        return db.execute("""
            SELECT model_reviewed_at, jev_review FROM s2_decisions
            WHERE user_scope=? AND decision_id!=? AND model_reviewed_at IS NOT NULL
            ORDER BY rowid DESC LIMIT 1
        """, (self.scope, admission_id)).fetchone()

    def decisions(self, count=12, *, background_only=False, current_chat_only=False, db=None):
        with (self.connect() if db is None else nullcontext(db)) as db:
            statement = """
                SELECT d.record FROM s2_decisions AS d
                JOIN s2_admissions AS a ON a.admission_id = d.decision_id
                WHERE d.user_scope = ? AND (? = 0 OR a.source = 'background')
                AND (? = 0 OR (a.source = 'user' AND a.sequence > COALESCE((
                    SELECT admission_cutoff FROM s2_chat_history WHERE user_scope = a.user_scope
                ), 0)))
                ORDER BY d.rowid DESC LIMIT ?
            """
            return tuple(json.loads(row["record"]) for row in db.execute(
                statement, (self.scope, background_only, current_chat_only, count),
            ))

    def ui_events(self):
        with self.connect() as db:
            return tuple(UiEvent(row["sequence"], "reply_cleared" if row["payload"] == _CLEARED_REPLY else "reply",
                                 row["answer_id"], row["reply_to_message_id"], json.loads(row["payload"]))
                         for row in db.execute("""
                SELECT * FROM s2_ui_outbox WHERE user_scope = ? AND acknowledged = 0 ORDER BY sequence
            """, (self.scope,)))

    def ack_ui_event(self, sequence):
        with self.write() as db:
            db.execute("UPDATE s2_ui_outbox SET acknowledged = 1 WHERE user_scope = ? AND sequence = ?",
                       (self.scope, sequence))
            db.execute("DELETE FROM s2_ui_outbox WHERE user_scope = ? AND sequence = ? AND payload = ?",
                       (self.scope, sequence, _CLEARED_REPLY))


def _action_fence(function, subject_ref):
    return f"{function}:{subject_ref}" if subject_ref is not None else function


def _call_fence(function, args):
    target = next((ref_id(args[key]) for key in ("id", "item_id", "occurrence_id") if key in args), None)
    return _action_fence(function, target)


def _scope_review_origin(event):
    return next((wake["scope_review_of"] for wake in event.get("wakes", ()) if wake.get("scope_review_of")), None)


def _background_review_key(function, args):
    if function == "proposal.offer":
        offered = canonical_tool_name(args["function"])
        payload = args["args"]
        target = ref_id(payload.get("id") or payload.get("previous_phase_id"))
        kind = payload.get("kind", "focus") if offered == "phase.start" else None
        identity = (offered, target, kind)
    elif function == "reminder.present":
        identity = tuple(ref_id(target) for target in args["occurrence_ids"])
    elif function in {"reminder.recheck", "observation.wait_fresh", "decision.recheck"}:
        identity = (args.get("purpose"), ref_id(args.get("occurrence_id")))
    elif function == "pet.present":
        identity = ()
    else:
        return None
    return stable_id(function, json_text(identity))


def _committed_status(result):
    if result and result.get("outcome") != "quiet":
        status = result.get("original_status") or result.get("status")
        if status in {"applied", "accepted", "pending"}:
            return status
    return None


def _result_unknown(result):
    return result is None or result.get("status") == "timeout"


def _failure(call_id, code, *, status="rejected"):
    return {"call_id": call_id, "status": status, "error_code": code}


def authorize_tool(db: sqlite3.Connection, function: str, args: dict, authority: ToolAuthority, *,
                   now: datetime | None = None) -> str | None:
    """Recheck the exact admitted callback in the transaction that performs its effect."""
    now = now if now is not None else datetime.now(UTC)
    row = db.execute("""
        SELECT a.state, a.intent_generation, a.created_at, a.expires_at, a.grants,
            a.source, a.event, a.user_intent, a.authority_context, c.fingerprint
        FROM s2_admissions AS a
        JOIN s2_owners AS o ON o.user_scope = a.user_scope AND o.token = a.owner
        JOIN s2_steps AS s ON s.admission_id = a.admission_id AND s.intent_generation = a.intent_generation
        JOIN s2_calls AS c ON c.admission_id = a.admission_id AND c.step_id = s.step_id
        WHERE a.admission_id = ? AND a.user_scope = ? AND s.step_id = ?
            AND c.command_id = ? AND c.function = ? AND c.result IS NULL
            AND NOT EXISTS (SELECT 1 FROM s2_revoked_actions AS r
                WHERE r.admission_id = a.admission_id AND r.function IN (c.function, ?))
    """, (authority.admission_id, authority.user_scope, authority.step_id, authority.call_id, function,
          _call_fence(function, args))).fetchone()
    if row is None:
        return "SUPERSEDED"
    state, generation, created_at, expires_at, grants, source, event, user, context, fingerprint = row
    if state != "running" or generation != authority.intent_generation:
        return "SUPERSEDED"
    if not datetime.fromisoformat(created_at) <= now < datetime.fromisoformat(expires_at):
        return "EXPIRED_REQUEST"
    event = json.loads(event)
    user = json.loads(user) if user else {}
    if (authority.origin != ("user" if source == "user" else "agent")
            or authority.trigger_id != event["trigger_ids"][0]
            or authority.subject_refs != tuple(event["subject_refs"])
            or authority.proposal_id != user.get("reply_to_proposal_id")
            or timestamp(authority.created_at) != created_at or timestamp(authority.expires_at) != expires_at
            or fingerprint != stable_id(function, json_text(args))):
        return "NOT_AUTHORIZED"
    expected = json.loads(context)
    if (authority.context_revision != expected.get("context_revision")
            or authority.opportunity_id != expected.get("opportunity_id")
            or authority.observation_cursor != expected.get("observation_cursor")
            or authority.observation_generation != expected.get("observation_generation")
            or (authority.observation_valid_until.isoformat() if authority.observation_valid_until else None)
            != expected.get("observation_valid_until")
            or authority.expected_versions != expected.get("expected_versions")):
        return "NOT_AUTHORIZED"
    grants = json.loads(grants)
    if grants.get(function, {}).get("semantic_chat") and (
        source != "user" or "user_message" not in event["trigger_reasons"] or not user.get("utterance")
    ):
        return "NOT_AUTHORIZED"
    if not allows(grants, function, args, versions=expected.get("expected_versions", {})):
        return "NOT_AUTHORIZED"
    if authority.check_model_policy is not None:
        # Route and Observation policy edits serialize against this business write.
        try:
            authority.check_model_policy(db)
        except CloudRouteError:
            return "MODEL_AUTHORIZATION_CHANGED"
    return None
