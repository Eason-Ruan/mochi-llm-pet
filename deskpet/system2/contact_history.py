"""Project durable contact evidence without exposing reminder or activity text."""
from datetime import UTC, datetime, timedelta
import json


CONTACT_LIMIT = 8
_HORIZON = timedelta(hours=1)
_PASSIVE = {"reminder": "reminder_menu", "proposal": "proposal_panel"}
_EXPOSURE = {"none": 0, "unknown": 1, "painted": 2, "reported_delivered": 2}
_VISIBLE_STATES = {"painted": "painted", "badge_shown": "painted", "delivered": "reported_delivered"}
_PASSIVE_STATES = {"grouped", "passive"}
_UNCERTAIN_STATES = {"submitted", "submitting", "scheduled", "unknown", "cancel_intent", "cancelling"}
_CONTACT_STATES = set(_VISIBLE_STATES) | _PASSIVE_STATES | _UNCERTAIN_STATES
_EXPOSURE_EVENTS = {
    "card.painted": ("pet_card", "painted", True),
    "presentation.native_followup_painted": ("pet_card", "painted", True),
    "presentation.menu_painted": ("reminder_menu", "painted", False),
    "badge.painted": ("pet_badge", "painted", False),
    "os.delivered": ("os_notification", "reported_delivered", True),
}


def stamp(at):
    return at.astimezone(UTC).isoformat(timespec="microseconds")


def _table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()


def _contact(kind, at, delivery_id, channel, *, subject_ref=None, occurrence_id=None):
    return {"kind": kind, "at": at, "delivery_id": delivery_id, "channel": channel,
            "subject_ref": subject_ref, "occurrence_id": occurrence_id, "primary_channel": "none",
            "passive_entry": _PASSIVE.get(kind, "none"), "surfaces": {}, "sound_requested": False,
            "response": "none"}


def _surface(contact, channel, exposure, active, *, primary=False):
    prior = contact["surfaces"].get(channel)
    if prior is None or _EXPOSURE[exposure] >= _EXPOSURE[prior["exposure"]]:
        contact["surfaces"][channel] = {"channel": channel, "exposure": exposure, "active": active}
    if primary:
        contact["primary_channel"] = channel
        contact["channel"] = channel


def _display_rows(db, user_scope, since, through):
    return db.execute("""SELECT p.*, CASE
            WHEN p.state IN ('scheduled','cancel_intent','cancelling') AND p.deadline IS NOT NULL THEN p.deadline
            ELSE p.updated_at END AS evidence_at
        FROM presentations p LEFT JOIN system2_reminder_authority a USING(delivery_id)
        WHERE coalesce(a.user_scope, 'default')=?
        AND p.state IN ('painted','badge_shown','delivered','grouped','passive','submitted','submitting',
                        'scheduled','unknown','cancel_intent','cancelling')
        AND julianday(p.updated_at)<=julianday(?)
        AND julianday(evidence_at) BETWEEN julianday(?) AND julianday(?)
        ORDER BY julianday(evidence_at) DESC, p.delivery_id""", (user_scope, through, since, through))


def _reminder(contacts, row, at):
    task_id, occurrence = row["task_id"], row["occurrence"]
    key = (task_id, occurrence)
    if key not in contacts:
        contacts[key] = _contact("reminder", at, row["delivery_id"], row["channel"],
                                 subject_ref=f"item:{task_id}", occurrence_id=f"{task_id}:{occurrence}")
    contact = contacts[key]
    contact["at"] = max(contact["at"], at)
    return contact


def _record_display(contacts, row, at):
    contact = _reminder(contacts, row, at)
    state, channel = row["state"], row["channel"]
    if state in _PASSIVE_STATES:
        return
    if state == "painted" and row["reason"] == "menu_painted":
        _surface(contact, "reminder_menu", "painted", False)
        return
    active = channel != "pet_badge"
    primary = active and contact["primary_channel"] == "none"
    _surface(contact, channel, _VISIBLE_STATES.get(state, "unknown"), active, primary=primary)
    if primary:
        contact["delivery_id"] = row["delivery_id"]


def _record_receipt(contacts, row, event, at, through):
    contact = _reminder(contacts, row, at)
    needs_primary = contact["primary_channel"] == "none"
    if event == "sound.requested":
        contact["sound_requested"] = True
        if needs_primary and row["channel"] != "pet_badge":
            _surface(contact, row["channel"], "unknown", True, primary=True)
        return
    channel, exposure, active = _EXPOSURE_EVENTS[event]
    if needs_primary and event == "presentation.native_followup_painted":
        os_known = row["state"] == "delivered" and stamp(datetime.fromisoformat(row["updated_at"])) <= through
        _surface(contact, "os_notification", "reported_delivered" if os_known else "unknown", True, primary=True)
        needs_primary = False
    _surface(contact, channel, exposure, active, primary=active and needs_primary)


def _apply_reminder_responses(db, contacts, user_scope, since, through, responses, dismissals):
    for task_id, occurrence in responses.keys() | dismissals.keys():
        key = (task_id, occurrence)
        rows = db.execute("""SELECT p.*, coalesce(a.user_scope, 'default') AS user_scope FROM presentations p
            LEFT JOIN system2_reminder_authority a USING(delivery_id)
            WHERE p.task_id=? AND p.occurrence=? ORDER BY julianday(p.updated_at) DESC""", key).fetchall()
        # Task mutations carry an occurrence but no scope. Only attribute them when its owners agree.
        scopes = {row["user_scope"] for row in rows}
        response = responses.get(key) if scopes == {user_scope} else None
        response = response or dismissals.get(key)
        if response is None:
            continue
        for row in rows:
            if row["user_scope"] != user_scope:
                continue
            at = stamp(datetime.fromisoformat(row["updated_at"]))
            edge = (stamp(datetime.fromisoformat(row["deadline"])) if row["deadline"]
                    and row["state"] in {"scheduled", "cancel_intent", "cancelling"} else at)
            if at <= through and edge < since and row["state"] in _CONTACT_STATES:
                _record_display(contacts, row, edge)
            for receipt in db.execute("""SELECT kind, recorded_at FROM events
                WHERE json_extract(fields, '$.delivery_id')=?
                AND kind IN ('card.painted','badge.painted','os.delivered','presentation.native_followup_painted',
                             'presentation.menu_painted','sound.requested')
                AND julianday(recorded_at)<=julianday(?) ORDER BY julianday(recorded_at), id""",
                (row["delivery_id"], since)):
                at = stamp(datetime.fromisoformat(receipt["recorded_at"]))
                if at < since:
                    _record_receipt(contacts, row, receipt["kind"], at, through)
        if key in contacts:
            contacts[key]["response"], contacts[key]["response_at"] = response


def _reminders(db, user_scope, since, through):
    contacts = {}
    for row in _display_rows(db, user_scope, since, through):
        at = stamp(datetime.fromisoformat(row["evidence_at"]))
        if not since <= at <= through or stamp(datetime.fromisoformat(row["updated_at"])) > through:
            continue
        _record_display(contacts, row, at)

    if not _table(db, "events"):
        return contacts.values()
    responses, dismissals = {}, {}
    # Receipts survive ownership changes and can be newer than the owner row, as with legacy followups.
    for row in db.execute("""SELECT e.kind, e.recorded_at, e.fields, p.delivery_id, p.task_id, p.occurrence,
               p.channel, p.state, p.updated_at FROM events e
        LEFT JOIN presentations p ON p.delivery_id=json_extract(e.fields, '$.delivery_id')
        LEFT JOIN system2_reminder_authority a ON a.delivery_id=p.delivery_id
        WHERE e.kind IN ('task.acknowledged','task.snoozed','card.painted','badge.painted','os.delivered',
                         'presentation.native_followup_painted','presentation.menu_painted',
                         'presentation.dismissed','sound.requested')
        AND (e.kind IN ('task.acknowledged','task.snoozed')
             OR (p.delivery_id IS NOT NULL AND coalesce(a.user_scope, 'default')=?))
        AND julianday(e.recorded_at) BETWEEN julianday(?) AND julianday(?)
        ORDER BY julianday(e.recorded_at), e.id""", (user_scope, since, through)):
        at = stamp(datetime.fromisoformat(row["recorded_at"]))
        if not since <= at <= through:
            continue
        event = row["kind"]
        if event in {"task.acknowledged", "task.snoozed"}:
            fields = json.loads(row["fields"])
            occurrence = fields.get("payload", {}).get("occurrence")
            if type(occurrence) is int:
                key = (fields.get("task_id"), occurrence - (event == "task.snoozed"))
                responses[key] = (event.removeprefix("task."), at)
            continue
        if event == "presentation.dismissed":
            dismissals[(row["task_id"], row["occurrence"])] = ("dismissed", at)
            continue
        _record_receipt(contacts, row, event, at, through)
    _apply_reminder_responses(db, contacts, user_scope, since, through, responses, dismissals)
    return contacts.values()


def recent_contacts(db, user_scope, as_of):
    since, through = stamp(as_of - _HORIZON), stamp(as_of)
    sound_table = _table(db, "attention_sound_requests")
    sounds = ({row["delivery_id"]: row for row in db.execute("""SELECT * FROM attention_sound_requests
        WHERE user_scope=? AND requested_at BETWEEN ? AND ?""", (user_scope, since, through))}
        if sound_table else {})
    contacts = list(_reminders(db, user_scope, since, through)) if _table(db, "presentations") else []
    stored = db.execute("""SELECT * FROM system2_contacts WHERE user_scope=? AND at BETWEEN ? AND ?
        UNION ALL
        SELECT * FROM system2_contacts WHERE user_scope=? AND at<? AND response_at BETWEEN ? AND ?""",
        (user_scope, since, through, user_scope, since, since, through))
    for row in stored:
        kind, channel = row["kind"], row["channel"]
        contact = _contact(kind, row["at"], row["delivery_id"], channel, subject_ref=row["subject_ref"],
                           occurrence_id=row["occurrence_id"] or row["delivery_id"])
        exposure = row["exposure"]
        if exposure is None:
            # Earlier proposal responses could create a contact before any card painted.
            exposure = "unknown" if kind == "proposal" and row["response"] else "painted"
        if exposure != "none":
            active = channel != "pet_badge"
            _surface(contact, channel, exposure, active, primary=active)
        if row["response"] and (not row["response_at"] or row["response_at"] <= through):
            contact["response"] = {"rejected": "refused"}.get(row["response"], row["response"])
            contact["response_at"] = row["response_at"]
        previously_requested = (sound_table and row["at"] < since and db.execute("""SELECT 1
            FROM attention_sound_requests WHERE user_scope=? AND delivery_id=? AND requested_at<=?""",
            (user_scope, row["delivery_id"], through)).fetchone())
        contact["sound_requested"] = bool(row["sound_requested"] or row["delivery_id"] in sounds or previously_requested)
        if contact["sound_requested"] and contact["primary_channel"] == "none" and channel == "qt":
            contact["primary_channel"] = channel
        contacts.append(contact)

    for delivery_id, row in sounds.items():
        if row["contact_kind"] == "phase_end":
            phase_id = delivery_id.removeprefix("phase-end:").rsplit(":", 1)[0]
            contact = _contact("phase_end", row["requested_at"], delivery_id, "qt",
                               subject_ref=f"phase:{phase_id}", occurrence_id=delivery_id)
            contact["primary_channel"] = "qt"
            contact["sound_requested"] = True
            contacts.append(contact)

    unique = {}
    for contact in sorted(contacts, key=lambda c: (max(c["at"], c.get("response_at") or ""), c["delivery_id"]),
                          reverse=True):
        key = (contact["kind"], contact["occurrence_id"])
        if key in unique:
            continue
        surfaces = contact["surfaces"]
        primary = surfaces.get(contact["primary_channel"])
        contact["exposure"] = (primary["exposure"] if primary else
                               max((s["exposure"] for s in surfaces.values()), key=_EXPOSURE.get, default="none"))
        contact["surfaces"] = list(surfaces.values())
        contact["sound_heard"] = "unknown" if contact["sound_requested"] else "none"
        unique[key] = {field: value for field, value in contact.items() if value is not None}
        if len(unique) == CONTACT_LIMIT:
            break
    return list(unique.values())
