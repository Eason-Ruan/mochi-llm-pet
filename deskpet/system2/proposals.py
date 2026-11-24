"""Persistent confirmation cards and their one-shot desktop presentation."""
from __future__ import annotations

from datetime import datetime
import json

from deskpet.contracts.models import Model


class Proposal(Model):
    id: str
    version: int
    subject_refs: tuple[str, ...]
    expected_versions: dict[str, int]
    function: str
    args: dict
    origin_trigger: str
    state: str = "pending"
    created_at: datetime
    valid_until: datetime
    display_text: str
    applied_command_id: str | None = None


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS system2_proposals (
       user_scope TEXT NOT NULL, id TEXT NOT NULL, business_key TEXT NOT NULL,
       view TEXT NOT NULL, resolution TEXT, PRIMARY KEY(user_scope, id))""",
    """CREATE INDEX IF NOT EXISTS system2_proposal_business
       ON system2_proposals(user_scope, business_key)""",
    """CREATE TABLE IF NOT EXISTS system2_proposal_presentations (
       delivery_id TEXT PRIMARY KEY, user_scope TEXT NOT NULL, proposal_id TEXT NOT NULL,
       proposal_version INTEGER NOT NULL, opportunity_id TEXT NOT NULL,
       context_revision INTEGER NOT NULL, valid_until TEXT NOT NULL,
       observation_cursor INTEGER, observation_generation TEXT, presentation_generation INTEGER NOT NULL DEFAULT 1,
       state TEXT NOT NULL CHECK(state IN ('queued', 'permitted', 'displayed', 'revoked', 'unknown')),
       FOREIGN KEY(user_scope, proposal_id) REFERENCES system2_proposals(user_scope, id))""",
    """CREATE INDEX IF NOT EXISTS system2_proposal_dispatch
       ON system2_proposal_presentations(user_scope, state)""",
    """CREATE INDEX IF NOT EXISTS system2_proposal_delivery_subject
       ON system2_proposal_presentations(user_scope, proposal_id)""",
)


def create_tables(db):
    for statement in _SCHEMA:
        db.execute(statement)
    columns = {row[1] for row in db.execute("PRAGMA table_info(system2_proposal_presentations)")}
    if "channel" not in columns:
        db.execute("ALTER TABLE system2_proposal_presentations ADD COLUMN channel TEXT NOT NULL DEFAULT 'pet_card'")
    if "route_binding" not in columns:
        db.execute("ALTER TABLE system2_proposal_presentations ADD COLUMN route_binding TEXT")


def _decode(row):
    return Proposal.model_validate_json(row["view"]) if row else None


def get(db, scope, proposal_id):
    row = db.execute("SELECT view, resolution FROM system2_proposals WHERE user_scope=? AND id=?",
                     (scope, proposal_id)).fetchone()
    return _decode(row), json.loads(row["resolution"]) if row and row["resolution"] else None


def get_business(db, scope, key):
    rows = db.execute("""
        SELECT view FROM system2_proposals WHERE user_scope=? AND business_key=?
        ORDER BY rowid DESC
    """, (scope, key))
    return [_decode(row) for row in rows]


def pending(db, scope):
    rows = db.execute("""
        SELECT view FROM system2_proposals
        WHERE user_scope=? AND json_extract(view, '$.state')='pending'
        ORDER BY rowid
    """, (scope,))
    return [_decode(row) for row in rows]


def save(db, scope, proposal, *, key=None, resolution=None):
    payload = proposal.model_dump_json()
    if key is not None:
        db.execute("INSERT INTO system2_proposals VALUES (?, ?, ?, ?, NULL)",
                   (scope, proposal.id, key, payload))
        return
    db.execute("""
        UPDATE system2_proposals SET view=?, resolution=coalesce(?, resolution)
        WHERE user_scope=? AND id=?
    """, (payload, json.dumps(resolution, ensure_ascii=False) if resolution is not None else None,
          scope, proposal.id))


def enqueue(db, scope, proposal, *, delivery_id, opportunity_id, context_revision, valid_until,
            observation_cursor, observation_generation, channel="pet_card", route_binding=None):
    db.execute("""INSERT INTO system2_proposal_presentations
        (delivery_id, user_scope, proposal_id, proposal_version, opportunity_id, context_revision, valid_until,
         observation_cursor, observation_generation, channel, route_binding, state)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued')
        ON CONFLICT(delivery_id) DO UPDATE SET context_revision=excluded.context_revision,
        valid_until=excluded.valid_until, observation_cursor=excluded.observation_cursor,
        observation_generation=excluded.observation_generation, channel=excluded.channel,
        route_binding=excluded.route_binding, state='queued',
        presentation_generation=system2_proposal_presentations.presentation_generation + 1
        WHERE system2_proposal_presentations.state='revoked'""", (
        delivery_id, scope, proposal.id, proposal.version, opportunity_id, context_revision, valid_until.isoformat(),
        observation_cursor, observation_generation, channel, route_binding,
    ))


def deliveries(db, scope, *, state):
    rows = db.execute("""
        SELECT * FROM system2_proposal_presentations WHERE user_scope=? AND state=? ORDER BY rowid
    """, (scope, state))
    return [dict(row) for row in rows]


def delivery(db, scope, delivery_id):
    return db.execute("""
        SELECT * FROM system2_proposal_presentations WHERE user_scope=? AND delivery_id=?
    """, (scope, delivery_id)).fetchone()


def set_delivery(db, scope, delivery_id, state):
    db.execute("""
        UPDATE system2_proposal_presentations SET state=? WHERE user_scope=? AND delivery_id=?
    """, (state, scope, delivery_id))


def latest_delivery(db, scope, proposal_id):
    return db.execute("""SELECT * FROM system2_proposal_presentations
        WHERE user_scope=? AND proposal_id=? ORDER BY rowid DESC LIMIT 1""", (scope, proposal_id)).fetchone()


def revoke_queued(db, scope, proposal_id, *, release):
    rows = db.execute("""
        SELECT delivery_id FROM system2_proposal_presentations
        WHERE user_scope=? AND proposal_id=? AND state='queued'
    """, (scope, proposal_id)).fetchall()
    for row in rows:
        delivery_id = row["delivery_id"]
        set_delivery(db, scope, delivery_id, "revoked")
        release(delivery_id)
