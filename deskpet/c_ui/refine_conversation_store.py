"""Durable visible conversation and draft for the desktop chat."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import json
import sqlite3
from uuid import uuid4


class ConversationStore:
    def __init__(self, path):
        self.path = path
        with self._transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS refine_chat_messages (
                sequence INTEGER PRIMARY KEY, speaker TEXT NOT NULL, body TEXT NOT NULL
            )""")
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

    def append_message(self, speaker, message):
        with self._transaction() as db:
            db.execute("INSERT INTO refine_chat_messages(speaker, body) VALUES (?, ?)", (speaker, message))

    def begin_turn(self, speaker, message, turn):
        with self._transaction() as db:
            event_id = turn.action["event_id"] if turn.action else uuid4().hex
            sequence = db.execute("INSERT INTO refine_chat_messages(speaker, body) VALUES (?, ?)",
                                  (speaker, message)).lastrowid
            db.execute("INSERT INTO refine_system2_chat_turns VALUES (?, ?, '{}', ?, ?)",
                       (event_id, sequence, f"local:{event_id}", int(turn.action is not None)))
            if turn.action is None:
                self._insert_answer(db, f"local-answer:{event_id}", event_id, turn.text)
                db.execute("UPDATE refine_chat_state SET draft = '' WHERE singleton = 1")
            else:
                pending = json.dumps({
                    "action": turn.action, "text": turn.text, "follow_up": turn.follow_up,
                }, ensure_ascii=False)
                db.execute("UPDATE refine_chat_state SET draft = '', event_id = ?, turn = ? WHERE singleton = 1",
                           (event_id, pending))

    def finish_turn(self, event_id, reply):
        with self._transaction() as db:
            updated = db.execute("""UPDATE refine_chat_state SET event_id = NULL, turn = NULL
                WHERE singleton = 1 AND event_id = ?""", (event_id,))
            if updated.rowcount:
                self._insert_answer(db, f"local-answer:{event_id}", event_id, reply)

    def queue_turn(self, message_id, text, context):
        context = {**context, "queued_at": datetime.now(UTC).isoformat()}
        with self._transaction() as db:
            sequence = db.execute(
                "INSERT INTO refine_chat_messages(speaker, body) VALUES ('你', ?)", (text,)
            ).lastrowid
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

    def record_answer(self, answer_id, reply_to_message_id, text):
        with self._transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM refine_system2_chat_replies WHERE answer_id=?", (answer_id,)).fetchone():
                return
            self._insert_answer(db, answer_id, reply_to_message_id, text)

    @staticmethod
    def _insert_answer(db, answer_id, reply_to_message_id, text):
        sequence = db.execute(
            "INSERT INTO refine_chat_messages(speaker, body) VALUES ('Mochi', ?)", (text,)
        ).lastrowid
        db.execute("INSERT INTO refine_system2_chat_replies VALUES (?, ?, ?)",
                   (answer_id, sequence, reply_to_message_id))
        db.execute("UPDATE refine_system2_chat_turns SET pending=0 WHERE message_id=?", (reply_to_message_id,))

    def projection_version(self):
        with self._transaction() as db:
            return db.execute("SELECT COALESCE(MAX(sequence), 0) FROM refine_chat_messages").fetchone()[0]

    def projected_messages(self):
        with self._transaction() as db:
            return db.execute("""SELECT m.speaker, m.body, t.message_id,
                       CASE WHEN t.admission_id LIKE 'local:%' THEN 0 ELSE COALESCE(t.pending, 0) END,
                       r.reply_to_message_id, original.body
                FROM refine_chat_messages m
                LEFT JOIN refine_system2_chat_turns t ON t.message_seq=m.sequence
                LEFT JOIN refine_system2_chat_replies r ON r.message_seq=m.sequence
                LEFT JOIN refine_system2_chat_turns target ON target.message_id=r.reply_to_message_id
                LEFT JOIN refine_chat_messages original ON original.sequence=target.message_seq
                ORDER BY m.sequence""").fetchall()
