"""Durable visible conversation and draft for the desktop chat."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import json
import sqlite3
from uuid import uuid4


def _part(source, text):
    return {"source": source, "text": text}


def _answer_parts(parts):
    if not isinstance(parts, (list, tuple)) or not parts:
        raise ValueError("A structured answer requires at least one part")
    if any(not isinstance(part, dict) or part.get("source") not in ("program", "model", "status")
           or not isinstance(part.get("text"), str) or not part["text"].strip() for part in parts):
        raise ValueError("Each answer part requires a recognized source and nonempty text")
    return tuple(_part(part["source"], part["text"]) for part in parts)


def _joined_text(parts):
    return "\n".join(part["text"] for part in parts)


def _outbox_parts(raw_payload, body):
    if raw_payload is None:
        return None
    try:
        payload = json.loads(raw_payload)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("text") != body:
        return None
    if "parts" in payload:
        try:
            parts = _answer_parts(payload["parts"])
        except ValueError:
            return None
        return parts if _joined_text(parts) == body else None

    # Older aggregate replies joined the stored receipt texts in order, then added
    # the model follow-up with the same newline delimiter. Titles can contain newlines.
    if "actions" in payload:
        actions = payload["actions"]
        if not isinstance(actions, list) or not actions or any(
            not isinstance(action, dict) or not isinstance(action.get("text"), str) or not action["text"]
            for action in actions
        ):
            return None
        parts = tuple(_part("program", action["text"]) for action in actions)
        prefix = _joined_text(parts)
        if body == prefix:
            return parts
        follow_up = body.removeprefix(prefix + "\n")
        if follow_up != body and follow_up.strip():
            return (*parts, _part("model", follow_up))
        return None
    if payload.get("command_id") and payload.get("outcome"):
        return (_part("program", body),)
    return None


class ConversationStore:
    def __init__(self, path):
        self.path = path
        with self._transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS refine_chat_messages (
                sequence INTEGER PRIMARY KEY, speaker TEXT NOT NULL, body TEXT NOT NULL, parts TEXT
            )""")
            columns = {column[1] for column in db.execute("PRAGMA table_info(refine_chat_messages)")}
            if "parts" not in columns:
                db.execute("ALTER TABLE refine_chat_messages ADD COLUMN parts TEXT")
            db.execute("""CREATE TABLE IF NOT EXISTS refine_chat_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                draft TEXT NOT NULL DEFAULT '', event_id TEXT, turn TEXT
            )""")
            db.execute("INSERT OR IGNORE INTO refine_chat_state(singleton) VALUES (1)")
            db.execute("""CREATE TABLE IF NOT EXISTS refine_system2_chat_turns (
                message_id TEXT PRIMARY KEY, message_seq INTEGER NOT NULL UNIQUE,
                input TEXT NOT NULL, admission_id TEXT, pending INTEGER NOT NULL DEFAULT 1
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS refine_system2_chat_replies (
                answer_id TEXT PRIMARY KEY, message_seq INTEGER NOT NULL UNIQUE, reply_to_message_id TEXT
            )""")
            self._restore_outbox_parts(db)

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.path, timeout=3)
        try:
            with db:
                yield db
        finally:
            db.close()

    def load(self):
        with self._transaction() as db:
            messages = db.execute("SELECT speaker, body FROM refine_chat_messages ORDER BY sequence").fetchall()
            draft, event_id, turn = db.execute(
                "SELECT draft, event_id, turn FROM refine_chat_state WHERE singleton = 1"
            ).fetchone()
        return messages, draft, (event_id, json.loads(turn)) if event_id else None

    def save_draft(self, draft):
        with self._transaction() as db:
            db.execute("UPDATE refine_chat_state SET draft = ? WHERE singleton = 1", (draft,))

    @staticmethod
    def _insert_message(db, speaker, message, parts=None):
        encoded = json.dumps(parts, ensure_ascii=False) if parts is not None else None
        return db.execute("INSERT INTO refine_chat_messages(speaker, body, parts) VALUES (?, ?, ?)",
                          (speaker, message, encoded)).lastrowid

    def append_message(self, speaker, message, *, source=None):
        if source not in (None, "user", "program"):
            raise ValueError("Local messages need a user or program source")
        with self._transaction() as db:
            self._insert_message(db, speaker, message, parts=(_part(source, message),) if source else None)

    def begin_turn(self, speaker, message, turn):
        with self._transaction() as db:
            event_id = turn.action["event_id"] if turn.action else uuid4().hex
            sequence = self._insert_message(db, speaker, message, (_part("user", message),))
            db.execute("INSERT INTO refine_system2_chat_turns VALUES (?, ?, '{}', ?, ?)",
                       (event_id, sequence, f"local:{event_id}", int(turn.action is not None)))
            if turn.action is None:
                self._insert_answer(db, f"local-answer:{event_id}", event_id, turn.text,
                                    (_part("program", turn.text),))
                db.execute("UPDATE refine_chat_state SET draft = '' WHERE singleton = 1")
            else:
                pending = json.dumps({
                    "action": turn.action, "text": turn.text, "follow_up": turn.follow_up,
                }, ensure_ascii=False)
                db.execute("UPDATE refine_chat_state SET draft = '', event_id = ?, turn = ? WHERE singleton = 1",
                           (event_id, pending))
        return event_id

    def finish_turn(self, event_id, reply):
        with self._transaction() as db:
            updated = db.execute("""UPDATE refine_chat_state SET event_id = NULL, turn = NULL
                WHERE singleton = 1 AND event_id = ?""", (event_id,))
            if updated.rowcount:
                self._insert_answer(db, f"local-answer:{event_id}", event_id, reply, (_part("program", reply),))

    def queue_turn(self, message_id, text, context):
        context = {**context, "queued_at": datetime.now(UTC).isoformat()}
        with self._transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM refine_system2_chat_turns WHERE message_id=?", (message_id,)).fetchone():
                return
            sequence = self._insert_message(db, "你", text, (_part("user", text),))
            db.execute("INSERT INTO refine_system2_chat_turns(message_id, message_seq, input) VALUES (?, ?, ?)",
                       (message_id, sequence, json.dumps(context, ensure_ascii=False)))
            db.execute("UPDATE refine_chat_state SET draft = '' WHERE singleton = 1")

    def queued_turns(self):
        with self._transaction() as db:
            rows = db.execute("""SELECT t.message_id, m.body, t.input
                FROM refine_system2_chat_turns t JOIN refine_chat_messages m ON m.sequence=t.message_seq
                WHERE t.admission_id IS NULL AND t.pending=1 ORDER BY t.message_seq""").fetchall()
        return [(message_id, body, json.loads(context)) for message_id, body, context in rows]

    def record_admission(self, message_id, admission_id):
        with self._transaction() as db:
            db.execute("UPDATE refine_system2_chat_turns SET admission_id=? WHERE message_id=?",
                       (admission_id, message_id))

    def admission_for(self, message_id):
        with self._transaction() as db:
            row = db.execute("SELECT admission_id FROM refine_system2_chat_turns WHERE message_id=?",
                             (message_id,)).fetchone()
        return row[0] if row else None

    def record_status(self, answer_id, reply_to_message_id, text):
        self.record_answer(answer_id, reply_to_message_id, parts=(_part("status", text),))

    def record_answer(self, answer_id, reply_to_message_id, text=None, *, parts=None):
        """Store a host answer once, accepting flat text from pre-parts outbox rows."""
        if parts is not None:
            try:
                parts = _answer_parts(parts)
            except ValueError:
                if not isinstance(text, str) or not text.strip():
                    raise
                parts = None
            else:
                text = _joined_text(parts)
        with self._transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM refine_system2_chat_replies WHERE answer_id=?", (answer_id,)).fetchone():
                return
            if not isinstance(text, str) or not text.strip():
                raise ValueError("An answer requires text or structured parts")
            if parts is None and self._has_outbox(db):
                entry = db.execute("""SELECT payload FROM s2_ui_outbox
                    WHERE answer_id=? AND reply_to_message_id=?""", (answer_id, reply_to_message_id)).fetchone()
                if entry:
                    parts = _outbox_parts(entry[0], text)
            self._insert_answer(db, answer_id, reply_to_message_id, text, parts)

    def _insert_answer(self, db, answer_id, reply_to_message_id, text, parts):
        sequence = self._insert_message(db, "Mochi", text, parts)
        db.execute("INSERT INTO refine_system2_chat_replies VALUES (?, ?, ?)",
                   (answer_id, sequence, reply_to_message_id))
        db.execute("UPDATE refine_system2_chat_turns SET pending=0 WHERE message_id=?", (reply_to_message_id,))

    @staticmethod
    def _has_outbox(db):
        return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='s2_ui_outbox'").fetchone()

    def _restore_outbox_parts(self, db):
        if not self._has_outbox(db):
            return
        rows = db.execute("""SELECT m.sequence, m.body, o.payload FROM refine_chat_messages m
            JOIN refine_system2_chat_replies r ON r.message_seq=m.sequence
            JOIN s2_ui_outbox o ON o.answer_id=r.answer_id AND o.reply_to_message_id=r.reply_to_message_id
            WHERE m.parts IS NULL""").fetchall()
        for sequence, body, payload in rows:
            if (parts := _outbox_parts(payload, body)) is not None:
                db.execute("UPDATE refine_chat_messages SET parts=? WHERE sequence=?",
                           (json.dumps(parts, ensure_ascii=False), sequence))

    def projection_version(self):
        with self._transaction() as db:
            return db.execute("SELECT COALESCE(MAX(sequence), 0) FROM refine_chat_messages").fetchone()[0]

    def projected_messages(self, *, include_parts=False):
        """Read the original chronological projection with optional provenance for each message."""
        with self._transaction() as db:
            rows = db.execute("""SELECT m.speaker, m.body, t.message_id,
                       CASE WHEN t.admission_id LIKE 'local:%' THEN 0 ELSE COALESCE(t.pending, 0) END,
                       r.reply_to_message_id, original.body, m.parts,
                       CASE WHEN t.message_id IS NOT NULL OR m.speaker IN ('你', '你 · 模拟语音') THEN 'user'
                            WHEN target.admission_id = 'local:' || r.reply_to_message_id
                                 AND r.answer_id = 'local-answer:' || r.reply_to_message_id THEN 'program'
                            WHEN r.answer_id IN ('expired-' || r.reply_to_message_id,
                                                 'submission-' || r.reply_to_message_id) THEN 'program'
                            ELSE 'legacy' END
                FROM refine_chat_messages m
                LEFT JOIN refine_system2_chat_turns t ON t.message_seq=m.sequence
                LEFT JOIN refine_system2_chat_replies r ON r.message_seq=m.sequence
                LEFT JOIN refine_system2_chat_turns target ON target.message_id=r.reply_to_message_id
                LEFT JOIN refine_chat_messages original ON original.sequence=target.message_seq
                ORDER BY m.sequence""").fetchall()
        if not include_parts:
            return [row[:6] for row in rows]
        return [(*row[:6], tuple(json.loads(row[6])) if row[6] is not None else (_part(row[7], row[1]),))
                for row in rows]
