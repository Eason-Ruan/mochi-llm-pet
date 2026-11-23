"""The System 2 item API over the same tasks, occurrences and outbox as the desktop."""
from __future__ import annotations

import json
from datetime import datetime
from uuid import NAMESPACE_URL, uuid5

from deskpet.contracts import ErrorCode, TaskCommand, TaskDraft, TaskView
from deskpet.system2.tool_schema import ItemFilter, validate_tool_args
from deskpet.timezones import resolve_timezone


RULE_FIELDS = (
    "remind_after", "target_at", "remind_by", "expires_at", "window_open_at", "window_close_at",
    "urgency_until", "urgency_reason",
)


def _iso(value):
    return value.isoformat() if value is not None else None


def _status(task):
    if task.draft.item_type == "issue":
        return task.effective_issue_status
    if task.expired_at:
        return "expired"
    if task.status == "completed":
        return "finished"
    if task.status == "cancelled":
        return "cancelled"
    return "postponing" if task.last_snoozed_at else "created"


def _occurrence_status(task):
    if task.acknowledged_at:
        return "acknowledged"
    if task.expired_at:
        return "expired"
    if task.status == "draft":
        return "draft"
    if task.status in {"completed", "cancelled"}:
        return "cancelled"
    return "delivered" if task.displayed_at else "pending"


class ItemService:
    def __init__(self, tasks, *, timezone="UTC"):
        self.tasks = tasks
        self.timezone = resolve_timezone(timezone).key

    def get(self, item_id, *, db=None):
        task = self.tasks.get(item_id, db=db)
        return self._item(task, self.tasks._db if db is None else db) if task else None

    def list(self, filter=None, *, db=None):
        query = ItemFilter.model_validate(filter or {})
        db = self.tasks._db if db is None else db
        return [
            self._item(task, db) for task in self.tasks.list_tasks(db=db)
            if (query.include_archived or not task.archived_at)
            and (query.type is None or task.draft.item_type == query.type)
            and (query.status is None or _status(task) == query.status)
            and (not query.active_only or (task.status == "registered" and not task.expired_at))
        ]

    def eligible(self, *, now=None, db=None):
        """List window candidates. The scheduler separately authorizes any presentation."""
        now = now or self.tasks.clock.wall_now()
        db = self.tasks._db if db is None else db
        tasks = (
            task for task in self.tasks.list_tasks(db=db)
            if task.active_reminder and task.displayed_at is None
            and (task.draft.opens_at is None or task.draft.opens_at <= now)
            and (task.draft.opportunity_close_at is None or now < task.draft.opportunity_close_at)
        )
        def urgency_key(task):
            relevant = (task.draft.hard_deadline, task.draft.expires_at, task.draft.urgency_until)
            edge = min((value for value in relevant if value is not None and value > now),
                       default=datetime.max.replace(tzinfo=now.tzinfo))
            return edge, task.confirmed_at or task.created_at or now, task.task_id

        return [self._item(task, db) for task in sorted(tasks, key=urgency_key)]

    def resolve_occurrence(self, occurrence_id, *, db=None):
        task_id, separator, ordinal = occurrence_id.rpartition(":")
        if not separator or not ordinal:
            return None
        task = self.tasks.get(task_id, db=db)
        return task if task and task.draft.reminder_enabled and task.occurrence_id == occurrence_id else None

    def get_occurrence(self, occurrence_id, *, db=None):
        db = self.tasks._db if db is None else db
        current = self.resolve_occurrence(occurrence_id, db=db)
        if current is not None:
            return self._occurrence(current, db)
        task_id, _, ordinal = occurrence_id.rpartition(":")
        if not ordinal.isascii() or not ordinal.isdecimal():
            return None
        row = db.execute("SELECT prior_view, closed_as FROM item_occurrence_history WHERE task_id=? AND ordinal=?",
                         (task_id, ordinal)).fetchone()
        return self._occurrence(TaskView.model_validate_json(row[0]), db, status=row[1]) if row else None

    def flush(self):
        self.tasks.flush()

    def estimate_window(self, occurrence_id, *, window_open_at, window_close_at, command_id, expected_version, db):
        """Re-estimate the current occurrence without replacing its user's rule."""
        task = self.resolve_occurrence(occurrence_id, db=db)
        if task is None:
            return self._rejection(ErrorCode.STALE_TASK)
        result = self.tasks.apply_in_transaction(db, TaskCommand(
            command_id=command_id, expected_version=expected_version, task_id=task.task_id, kind="estimate_window",
            payload={"window_open_at": window_open_at.isoformat(), "window_close_at": window_close_at.isoformat()},
        ))
        return result.model_dump(mode="json", exclude={"view"}) | {
            "subject_ref": f"item:{task.task_id}", "view": self._item(result.view, db) if result.view else None,
        }

    def execute(self, function, args, *, command_id, expected_version, db=None, snoozed_at: datetime | None = None):
        """Use an executor-owned business transaction, or commit a standalone local call."""
        if db is not None:
            if not db.in_transaction:
                raise ValueError("a write transaction is required")
            return self._execute(db, function, args, command_id, expected_version, snoozed_at)
        with self.tasks._transaction():
            result = self._execute(self.tasks._db, function, args, command_id, expected_version, snoozed_at)
        self.flush()
        return result

    def _execute(self, db, function, args, command_id, expected_version, snoozed_at):
        if not isinstance(command_id, str) or not command_id.strip():
            return self._rejection(ErrorCode.INVALID_ARGUMENT)
        try:
            args = validate_tool_args(function, args)
            inputs = [function, args, expected_version]
            if snoozed_at is not None:
                inputs.append(snoozed_at.isoformat())
            fingerprint = json.dumps(inputs, ensure_ascii=False, sort_keys=True)
        except (KeyError, TypeError, ValueError):
            return self._rejection(ErrorCode.INVALID_ARGUMENT)
        saved = db.execute("SELECT fingerprint,result FROM item_api_commands WHERE id=?", (command_id,)).fetchone()
        if saved:
            if fingerprint != saved[0]:
                return self._rejection(ErrorCode.INVALID_ARGUMENT)
            result = json.loads(saved[1])
            return result | {"status": "duplicate", "original_status": result["status"]}
        db.execute("SAVEPOINT item_api_mutation")
        try:
            result = self._apply(db, function, args, command_id, expected_version, snoozed_at)
        except (TypeError, ValueError, OverflowError):
            db.execute("ROLLBACK TO item_api_mutation")
            result = self._rejection(ErrorCode.INVALID_ARGUMENT)
        db.execute("RELEASE item_api_mutation")
        db.execute("INSERT INTO item_api_commands VALUES (?,?,?)",
                   (command_id, fingerprint, json.dumps(result, ensure_ascii=False, sort_keys=True)))
        return result

    def _apply(self, db, function, args, command_id, version, snoozed_at):
        payload = {}
        if function == "item.create":
            task_id = uuid5(NAMESPACE_URL, f"mochi:item:{command_id}").hex
            task = None
            kind = "create_registered"
            draft = self._rule_patch(args.get("reminder_rule"), item_type=args["type"], timezone=self.timezone)
            payload["draft"] = TaskDraft.model_validate(draft | {
                "content": args["title"].strip(), "item_type": args["type"],
                "description": args.get("description"), "due_at": args.get("due_at"),
            }).model_dump(mode="json")
        else:
            task_id = args.get("id") or args.get("item_id")
            occurrence = args.get("occurrence_id")
            if occurrence:
                task = self.resolve_occurrence(occurrence, db=db)
                if not task:
                    prior = self.get_occurrence(occurrence, db=db)
                    return self._rejection(ErrorCode.STALE_TASK if prior else ErrorCode.NOT_FOUND)
                task_id = task.task_id
            else:
                task = self.tasks.get(task_id, db=db)
            if not task:
                return self._rejection(ErrorCode.NOT_FOUND)
            if task.version != version:
                return self._rejection(ErrorCode.STALE_TASK, task)
            if function == "item.update":
                kind = "edit"
                patch = args["patch"]
                if not patch:
                    return self._rejection(ErrorCode.INVALID_ARGUMENT, task)
                updates = {key: patch[key] for key in ("description", "due_at") if key in patch}
                if "title" in patch:
                    updates["content"] = (patch["title"] or "").strip()
                if "reminder_rule" in patch:
                    kind = "configure"
                    updates.update(self._rule_patch(patch["reminder_rule"], item_type=task.draft.item_type,
                                                    timezone=task.draft.timezone))
                payload["draft"] = TaskDraft.model_validate(task.draft.model_dump() | updates).model_dump(mode="json")
            elif function == "reminder.configure":
                kind = "configure"
                updates = self._rule_patch(args["rule"], item_type=task.draft.item_type, timezone=task.draft.timezone)
                payload["draft"] = TaskDraft.model_validate(task.draft.model_dump() | updates).model_dump(mode="json")
            elif function == "item.set_status":
                kind = "set_status"
                status = args["status"]
                if task.draft.item_type == "issue":
                    payload["status"] = status
                elif status in {"cancelled", "finished"}:
                    kind = "cancel" if status == "cancelled" else "complete"
                else:
                    return self._rejection(ErrorCode.INVALID_TRANSITION, task)
            elif function == "item.archive":
                kind = "archive"
            elif function == "reminder.ack":
                kind = "ack"
            elif function == "reminder.snooze":
                kind = "snooze"
                payload = {"seconds": args["delay_s"], "mode": args["mode"]}
                if snoozed_at is not None:
                    payload["snoozed_at"] = snoozed_at.isoformat()
                payload.update((key, args[key]) for key in ("window_before_s", "window_after_s")
                               if args.get(key) is not None)
            else:
                return self._rejection(ErrorCode.UNKNOWN_TOOL)
        command = TaskCommand(command_id=command_id, kind=kind, task_id=task_id,
                              expected_version=version, payload=payload)
        result = self.tasks.apply_in_transaction(db, command)
        return result.model_dump(mode="json", exclude={"view"}) | {
            "subject_ref": f"item:{task_id}",
            "view": self._item(result.view, db) if result.view else None,
        }

    def _rule_patch(self, source, *, item_type, timezone):
        enabled = source is not None or item_type == "reminder"
        fields = source or {}
        after = fields.get("remind_after")
        by = fields.get("remind_by")
        exact = after is not None and after == by
        return {
            "reminder_enabled": enabled, "mode": "hard" if exact else "soft",
            "earliest_at": None, "latest_first_delivery_at": None,
            "timezone": fields.get("timezone", timezone),
            **{name: fields.get(name) for name in RULE_FIELDS},
            "window_computed_at": self.tasks.clock.wall_now()
            if fields.get("window_open_at") or fields.get("window_close_at") else None,
        }

    def _item(self, task, db):
        draft = task.draft.model_dump(mode="json")
        result = {
            "id": task.task_id, "version": task.version, "type": task.draft.item_type,
            "title": task.draft.content, "description": task.draft.description,
            "created_at": _iso(task.created_at or task.confirmed_at), "updated_at": _iso(task.updated_at or task.confirmed_at),
            "status": _status(task), "archived_at": _iso(task.archived_at), "due_at": draft["due_at"],
            "rule": None, "occurrence": None,
        }
        if task.draft.reminder_enabled:
            result["rule"] = {name: draft[name] for name in RULE_FIELDS} | {
                "id": f"rule:{task.task_id}", "item_id": task.task_id, "version": task.rule_version,
                "timezone": draft["timezone"], "status": "active" if task.active_reminder else "inactive",
                "remind_after": draft["remind_after"] or draft["earliest_at"],
                "remind_by": draft["remind_by"] or draft["latest_first_delivery_at"],
            }
            result["occurrence"] = self._occurrence(task, db)
        if task.draft.item_type == "issue":
            result.update(self._focus_summary(task.task_id, db))
        return result

    @staticmethod
    def _occurrence(task, db, *, status=None):
        draft = task.draft
        recheck = None
        if status is None:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='system2_wakes'").fetchone():
                recheck = db.execute("""SELECT at FROM system2_wakes
                    WHERE task_id=? AND task_version=? AND state='pending' AND reason='scheduled_recheck'
                    ORDER BY at LIMIT 1""", (task.task_id, task.version)).fetchone()
            if recheck is None:
                recheck = db.execute("SELECT run_at FROM rechecks WHERE task_id=? AND version=?",
                                     (task.task_id, task.version)).fetchone()
        delivery = db.execute("""SELECT delivery_id FROM deliveries WHERE task_id=? AND occurrence=?
                               ORDER BY status='displayed' DESC, task_version DESC LIMIT 1""",
                              (task.task_id, task.occurrence)).fetchone()
        if not delivery:
            delivery = db.execute("""SELECT delivery_id FROM presentations WHERE task_id=? AND occurrence=?
                                  ORDER BY task_version DESC LIMIT 1""", (task.task_id, task.occurrence)).fetchone()
        return {
            "id": task.occurrence_id, "item_id": task.task_id, "ordinal": task.occurrence,
            "version": task.version, "rule_version": task.occurrence_rule_version or task.rule_version,
            "status": status or _occurrence_status(task),
            "window_open_at": _iso(draft.opens_at) if draft.window_open_at else None,
            "window_close_at": _iso(draft.opportunity_close_at) if draft.window_close_at else None,
            "last_snoozed_at": _iso(task.last_snoozed_at), "next_check_at": recheck[0] if recheck else None,
            "delivery_id": delivery[0] if delivery else None, "displayed_at": _iso(task.displayed_at),
            "acknowledged_at": _iso(task.acknowledged_at),
        }

    @staticmethod
    def _focus_summary(issue_id, db):
        summary = {"first_focus_at": None, "actual_focus_s": 0.0, "session_links": []}
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='phase_intervals'").fetchone() is None:
            return summary
        intervals = [json.loads(row[0]) for row in db.execute("""
            SELECT interval FROM phase_intervals WHERE json_extract(interval,'$.primary_issue_id')=?
        """, (issue_id,))]
        summary["first_focus_at"] = min((entry["started_at"] for entry in intervals), default=None)
        summary["actual_focus_s"] = sum(entry["seconds"] for entry in intervals)
        phases = {entry["phase_id"] for entry in intervals}
        summary["session_links"] = [row[0] for row in db.execute("SELECT id,view FROM phase_views ORDER BY rowid")
                                    if row[0] in phases or issue_id in json.loads(row[1])["issue_links"]]
        return summary

    @staticmethod
    def _rejection(code, task=None):
        return {"status": "rejected", "error_code": code.value,
                "actual_version": task.version if task else None,
                "subject_ref": f"item:{task.task_id}" if task else None}
