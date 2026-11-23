"""Durable visible conversation and draft for the desktop chat."""
from __future__ import annotations

from contextlib import contextmanager
import json
import sqlite3


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
            db.execute("INSERT INTO refine_chat_messages(speaker, body) VALUES (?, ?)", (speaker, message))
            if turn.action is None:
                db.execute("INSERT INTO refine_chat_messages(speaker, body) VALUES ('Mochi', ?)", (turn.text,))
                db.execute("UPDATE refine_chat_state SET draft = '' WHERE singleton = 1")
            else:
                pending = json.dumps({
                    "action": turn.action, "text": turn.text, "follow_up": turn.follow_up,
                }, ensure_ascii=False)
                db.execute("UPDATE refine_chat_state SET draft = '', event_id = ?, turn = ? WHERE singleton = 1",
                           (turn.action["event_id"], pending))

    def finish_turn(self, event_id, reply):
        with self._transaction() as db:
            updated = db.execute("""UPDATE refine_chat_state SET event_id = NULL, turn = NULL
                WHERE singleton = 1 AND event_id = ?""", (event_id,))
            if updated.rowcount:
                db.execute("INSERT INTO refine_chat_messages(speaker, body) VALUES ('Mochi', ?)", (reply,))
