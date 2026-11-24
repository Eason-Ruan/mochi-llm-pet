"""Single transactional command gateway for System 2 and explicit desktop actions."""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from deskpet.contracts import ErrorCode
from deskpet.observation_v2.routes import SYSTEM2
from deskpet.settings import HabitBehavior, SettingsConflict
from deskpet.work_sessions.models import PhaseCommand, PhaseResult, PhaseView

from . import proposals
from .protocol import ToolAuthority
from .semantic_policy import semantic_background_enabled
from .tool_schema import (
    HabitPatch, PROPOSABLE_ACTIONS, READ_TOOLS, SettingsPatch, canonical_tool_name, tool_definitions, validate_tool_args,
)

_MESSAGES = {
    "STALE_TASK": "内容已更新，请刷新后再试。",
    "STALE_CONTEXT": "当前活动已变化，等待重新判断。",
    "EXPIRED_REQUEST": "该请求已过期。",
    "INVALID_ARGUMENT": "无法使用这些参数。",
    "INVALID_TRANSITION": "当前状态无法执行这项操作。",
    "NOT_FOUND": "找不到所请求的内容。",
    "UNKNOWN_TOOL": "不支持这项操作。",
    "NEEDS_CONFIRMATION": "这项操作需要你确认。",
    "ALREADY_RESOLVED": "这张确认卡已处理。",
    "SUPERSEDED": "已由你更新的指令替代。",
    "MODEL_AUTHORIZATION_CHANGED": "模型授权已变更，这次操作没有执行。",
    "DISABLED": "此功能已关闭。",
    "UNSUPPORTED_CHANNEL": "当前无法使用这个展示渠道。",
}
_APPLIED = {
    "item.create": "已创建事项。", "item.update": "已更新事项。", "item.set_status": "已更新事项状态。",
    "item.archive": "已归档事项。", "reminder.configure": "已更新提醒时间。",
    "reminder.snooze": "已推迟提醒。", "reminder.ack": "已确认提醒。",
    "phase.start": "已开始。", "phase.pause": "已暂停。", "phase.resume": "已继续。",
    "phase.adjust": "已调整时长。", "phase.end": "已结束。", "phase.skip_break": "已跳过休息。",
}
_BACKGROUND_WRITES = frozenset({
    "proposal.offer", "reminder.present", "reminder.recheck", "observation.wait_fresh", "pet.present", "decision.recheck",
})
_PROPOSABLE = frozenset(PROPOSABLE_ACTIONS)
_AMENDABLE = {
    "phase.start": {"planned_duration_s", "title", "description"},
    "phase.adjust": {"total_s", "add_s"},
    "item.create": {"title", "description", "due_at", "reminder_rule"},
    "item.update": {"patch"},
    "reminder.configure": {"rule"},
}
_EXTERNAL_READS = frozenset({"context.read"})
_QT_BUSY_TIMEOUT = 0.025


@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: str
    status: str
    error_code: str | None = None
    subject_ref: str | None = None
    actual_version: int | None = None
    delivery_id: str | None = None
    original_status: str | None = None
    retry_at: str | None = None
    message: str | None = None
    view: Any = None
    outcome: str | None = None
    retryable: bool | None = None
    presentation_receipt: dict | None = None
    resolved_function: str | None = None

    def to_dict(self):
        return {field: getattr(self, field) for field in self.__slots__ if getattr(self, field) is not None}


class CommandRejected(ValueError):
    def __init__(self, code, **result_fields):
        self.code = code
        self.result_fields = result_fields
        super().__init__(code)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _stable_id(kind, *parts):
    return uuid5(NAMESPACE_URL, "mochi:system2:" + kind + ":" + _json(parts)).hex


def _when(value):
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def _error_code(code):
    return getattr(code, "value", code)


def _rejected(call_id, code, **fields):
    code = _error_code(code)
    return ToolResult(call_id, "rejected", error_code=code,
                      message=_MESSAGES.get(code, "这次操作未完成。"), outcome="rejected",
                      retryable=code in {"STALE_CONTEXT", "STALE_TASK", "BUSY"}, **fields)


def _model_view(model):
    return model.model_dump(mode="json") if model is not None else None


def _settings_view(settings):
    return {key: value for key, value in settings.to_dict().items()
            if not key.startswith(("observation_", "document_"))}


class System2ToolExecutor:
    """The receipt, generation check and local effects commit together."""

    definitions = tool_definitions

    def __init__(self, path, *, items, phases, settings, scheduler, pet=None,
                 context_reader=None, now=None, authorization=None, user_scope=None, after_local_commit=None):
        self.path = Path(path)
        self.items = items
        self.phases = phases
        self.settings = settings
        self.routes = settings.routes
        self.scheduler = scheduler
        self.pet = pet
        self.context_reader = context_reader
        self.now = now or phases.clock.wall_now
        self.authorization = authorization
        self.after_local_commit = after_local_commit
        self.user_scope = user_scope or getattr(scheduler, "user_scope", settings.user_scope)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS system2_tool_schema (version INTEGER NOT NULL)")
            schema = db.execute("SELECT version FROM system2_tool_schema").fetchone()
            if schema is None:
                db.execute("INSERT INTO system2_tool_schema VALUES (1)")
            elif schema["version"] != 1:
                raise ValueError("Unsupported System 2 command schema")
            db.execute("""
                CREATE TABLE IF NOT EXISTS system2_command_receipts (
                    user_scope TEXT NOT NULL, command_id TEXT NOT NULL, function TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, result TEXT NOT NULL,
                    PRIMARY KEY(user_scope, command_id))
            """)
            proposals.create_tables(db)

    @contextmanager
    def _transaction(self, *, timeout=5):
        with closing(sqlite3.connect(self.path, timeout=timeout)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            yield db

    def _receipt(self, db, scope, command_id):
        return db.execute(
            "SELECT fingerprint, result, function FROM system2_command_receipts WHERE user_scope=? AND command_id=?",
            (scope, command_id),
        ).fetchone()

    def _remember(self, db, scope, function, fingerprint, result):
        db.execute("INSERT INTO system2_command_receipts VALUES (?, ?, ?, ?, ?)",
                   (scope, result.call_id, function, fingerprint, _json(result.to_dict())))

    def get_result(self, command_id, *, user_scope=None):
        scope = user_scope or self.user_scope
        if scope != self.user_scope:
            return None
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            row = self._receipt(db, scope, command_id)
            return self._receipt_with_presentation(db, scope, row) if row else None

    def _receipt_with_presentation(self, db, scope, row):
        result = ToolResult(**json.loads(row["result"]))
        if not result.delivery_id:
            return result
        channel = None
        state = None
        if row["function"] == "proposal.offer":
            delivery = proposals.delivery(db, scope, result.delivery_id)
            if delivery:
                channel, state = delivery["channel"], delivery["state"]
        elif row["function"] == "pet.present" and self.pet is not None:
            state = self.pet.state(result.delivery_id, scope, db=db)
            channel = "qt"
        elif row["function"] == "reminder.present":
            delivery = db.execute("SELECT channel,state,reason FROM presentations WHERE delivery_id=?",
                                  (result.delivery_id,)).fetchone()
            if delivery:
                channel, state = delivery["channel"], delivery["state"]
            else:
                authority = db.execute("SELECT state FROM system2_reminder_authority WHERE delivery_id=? AND user_scope=?",
                                       (result.delivery_id, scope)).fetchone()
                if authority and authority["state"] == "revoked":
                    state = "revoked"
        if state is None:
            return result
        outcome = {
            "displayed": "shown", "painted": "shown", "badge_shown": "shown", "delivered": "shown",
            "revoked": "rejected", "cancelled": "rejected", "failed": "failed",
            "unavailable": "failed", "unknown": "deferred", "sound_requested": "deferred",
        }.get(state, "pending_display")
        return replace(result, outcome=outcome, retryable=state in {"failed", "released", "unavailable"},
                       presentation_receipt={"delivery_id": result.delivery_id, "channel": channel, "state": state})

    def execute(self, function, args, authority):
        return self._admit(function, args, authority, local=False)

    def execute_local(self, function, args, *, event_id, new_phase_id=None, source="manual"):
        """Admit one exact event from a trusted desktop widget without the model worker."""
        if not isinstance(event_id, str) or not event_id or len(event_id) > 500:
            raise ValueError("A stable UI event ID is required")
        if source not in ("manual", "chat"):
            raise ValueError("A supported local event source is required")
        function = canonical_tool_name(function)
        if function in _BACKGROUND_WRITES or function in _EXTERNAL_READS:
            return _rejected(event_id, "NEEDS_CONFIRMATION")
        now = self.now()
        authority = ToolAuthority(
            f"ui:{event_id}", f"ui:{source}:{event_id}", event_id, 1, self.user_scope, "user", event_id,
            now, now + timedelta(seconds=30),
            subject_refs=(f"new_phase:{new_phase_id}",) if new_phase_id and function == "phase.start" else (),
        )
        return self._admit(function, args, authority, local=True)

    def execute_local_phase(self, command):
        """Translate the existing Qt timer command into the shared domain gateway."""
        args = dict(command.payload)
        if command.kind in ("end", "review"):
            args = {"review": args}
        if command.kind != "start":
            args["id"] = command.phase_id
        args["expected_version"] = command.expected_version
        outcome = self.execute_local(f"phase.{command.kind}", args, event_id=command.command_id,
                                     new_phase_id=command.phase_id if command.kind == "start" else None,
                                     source=command.source)
        if outcome.view and "kind" in outcome.view:
            view = PhaseView.model_validate(outcome.view)
        elif outcome.status == "applied" and command.kind == "reassign_interval":
            view = self.phases.get_phase(command.phase_id)
        else:
            view = None
        error = outcome.error_code
        original = outcome.original_status
        result = PhaseResult(command_id=outcome.call_id, status=outcome.status, view=view,
                             error_code=error if error in ErrorCode._value2member_map_ else None,
                             original_status=original)
        self.phases.emit_phase_result(command, result)
        return result

    def _admit(self, function, args, authority, *, local):
        function = canonical_tool_name(function)
        call_id = authority.call_id
        if authority.user_scope != self.user_scope:
            return _rejected(call_id, "NEEDS_CONFIRMATION")
        try:
            args = validate_tool_args(function, args)
            fingerprint = _json([function, args, authority.admission_id, authority.step_id,
                                 authority.intent_generation, authority.origin, authority.subject_refs])
        except KeyError:
            return _rejected(call_id, "UNKNOWN_TOOL")
        except (ValueError, TypeError):
            return _rejected(call_id, "INVALID_ARGUMENT")

        with self._transaction() as db:
            if function not in READ_TOOLS:
                previous = self._receipt(db, authority.user_scope, call_id)
                if previous:
                    if previous["fingerprint"] != fingerprint:
                        return _rejected(call_id, "INVALID_ARGUMENT")
                    result = self._receipt_with_presentation(db, authority.user_scope, previous)
                    return replace(result, status="duplicate", original_status=result.original_status or result.status)
            now = self.now()
            if not local:
                authorize = self.authorization
                if authorize is None:
                    from .admission import authorize_tool
                    authorize = authorize_tool
                rejection = authorize(db, function, args, authority, now=now)
                if rejection:
                    return _rejected(call_id, rejection)
            if now >= authority.expires_at:
                return _rejected(call_id, "EXPIRED_REQUEST")
            if authority.origin != "user" and function not in (READ_TOOLS | _BACKGROUND_WRITES):
                return _rejected(call_id, "NEEDS_CONFIRMATION")
            if function not in _EXTERNAL_READS:
                result = self._guarded_execute(db, function, args, authority, now)
                if function not in READ_TOOLS:
                    if result.outcome is None:
                        outcome = {
                            "applied": "committed", "rejected": "rejected",
                            "pending": "pending_display" if result.delivery_id else "deferred",
                        }.get(result.status, "failed")
                        result = replace(result, outcome=outcome)
                    if local and result.status == "applied" and self.after_local_commit:
                        self.after_local_commit(db, function, args, authority.trigger_id)
                    self._remember(db, authority.user_scope, function, fingerprint, result)
                return result

        # Project the route's currently authorized view outside the business write lock.
        if self.context_reader is None:
            return _rejected(call_id, "DISABLED")
        return ToolResult(call_id, "applied", view=self._read_context(authority, args))

    def _read_context(self, authority, args):
        as_of = _when(args.get("as_of"))
        if callable(self.context_reader):
            return self.context_reader(authority, as_of)
        from deskpet.observation_v2.context import project_observation_context

        reader = self.context_reader
        if as_of is not None and hasattr(reader, "reader"):
            reader = reader.reader
        options = {key: _when(args[key]) for key in ("history_start", "history_end") if args.get(key)}
        context = reader.read_observation_context(as_of or self.now(), **options)
        view = project_observation_context(context, share_summary=False, historical=as_of is not None,
                                           include_capture_health=args.get("include_capture_health") is True)
        if as_of is not None:
            view.update(current=None, current_status="context_changed")
        return view

    def _guarded_execute(self, db, function, args, authority, now):
        db.execute("SAVEPOINT system2_effect")
        try:
            result = self._execute(db, function, args, authority, now)
        except SettingsConflict:
            db.execute("ROLLBACK TO system2_effect")
            result = _rejected(authority.call_id, "STALE_TASK")
        except (ValueError, OverflowError, TypeError) as exc:
            db.execute("ROLLBACK TO system2_effect")
            result = _rejected(authority.call_id, getattr(exc, "code", "INVALID_ARGUMENT"),
                               **getattr(exc, "result_fields", {}))
        db.execute("RELEASE system2_effect")
        return result

    def _execute(self, db, function, args, authority, now):
        call_id = authority.call_id
        if function in READ_TOOLS:
            return self._read(db, function, args, authority)
        if function == "proposal.offer":
            return self._offer_proposal(db, args, authority, now)
        if function == "proposal.resolve":
            return self._resolve_proposal(db, args, authority, now)
        if function.startswith("settings."):
            return self._settings_update(db, args, authority, now)
        if function.startswith("phase."):
            return self._phase(db, function, args, authority)
        if function.startswith("habit."):
            return self._habit(db, function, args, authority, now)
        if function == "reminder.present":
            for occurrence_id in args["occurrence_ids"]:
                self._require_version(db, f"occurrence:{occurrence_id}", args, authority)
            outcome = self.scheduler.present(
                occurrence_ids=args["occurrence_ids"], presentation=args["presentation"],
                reason_code=args["reason_code"], authority=authority, now=now, db=db,
            )
            return self._from_outcome(call_id, function, outcome)
        if function in ("reminder.recheck", "observation.wait_fresh", "decision.recheck"):
            return self._wait_or_recheck(db, function, args, authority, now)
        if function == "pet.present":
            if self.pet is None:
                return _rejected(call_id, "UNSUPPORTED_CHANNEL")
            return self._from_outcome(call_id, function, self.pet.present(**args, authority=authority, now=now, db=db))

        refs = self._target_refs(function, args)
        expected = self._require_version(db, refs[0], args, authority) if refs else self._new_version(args)
        payload = {key: value for key, value in args.items() if key != "expected_version"}
        snoozed_at = authority.created_at if function == "reminder.snooze" and authority.origin == "user" else None
        outcome = self.items.execute(function, payload, command_id=call_id, expected_version=expected,
                                     db=db, snoozed_at=snoozed_at)
        return self._from_outcome(call_id, function, outcome)

    @staticmethod
    def _from_outcome(call_id, function, outcome):
        if not isinstance(outcome, Mapping):
            outcome = _model_view(outcome)
        fields = {key: outcome[key] for key in ToolResult.__slots__ if key in outcome and key != "call_id"}
        fields["error_code"] = _error_code(fields.get("error_code"))
        if not fields.get("message"):
            if fields["error_code"]:
                fields["message"] = _MESSAGES.get(fields["error_code"], "这次操作未完成。")
            elif fields.get("status") == "applied":
                fields["message"] = _APPLIED.get(function, "已更新。")
        return ToolResult(call_id, **fields)

    @staticmethod
    def _view_result(call_id, ref, view):
        if view is None:
            return _rejected(call_id, "NOT_FOUND")
        return ToolResult(call_id, "applied", subject_ref=ref, actual_version=view.get("version"), view=view)

    def _read(self, db, function, args, authority):
        call_id = authority.call_id
        subject_id = args.get("id")
        if function == "command.get_result":
            row = self._receipt(db, authority.user_scope, args["command_id"])
            if not row:
                return _rejected(call_id, "NOT_FOUND")
            return ToolResult(call_id, "applied", view=self._receipt_with_presentation(db, authority.user_scope, row).to_dict())
        if function == "item.list":
            return ToolResult(call_id, "applied", view=self.items.list(args.get("filter"), db=db))
        if function == "habit.list":
            habits = self.settings.list_habits(db=db)
            return ToolResult(call_id, "applied", view=[_model_view(habit) for habit in habits])
        if function == "item.get":
            return self._view_result(call_id, f"item:{subject_id}", self.items.get(subject_id, db=db))
        if function == "habit.get" or args.get("scope") == "habit":
            habit = self.settings.get_habit(subject_id, db=db)
            return self._view_result(call_id, f"habit:{subject_id}", _model_view(habit))
        if function == "phase.get" or args.get("scope") == "current":
            phase = self.phases.get_phase(subject_id, db=db) if subject_id else self.phases.current_phase_in_transaction(db)
            if phase is None and not subject_id:
                return ToolResult(call_id, "applied", message="当前没有活动计时。")
            return self._view_result(call_id, f"phase:{phase.id if phase else subject_id}", _model_view(phase))
        return self._view_result(call_id, "settings:default", _settings_view(self.settings.read(db=db)))

    @staticmethod
    def _target_refs(function, args):
        if function in ("item.update", "item.set_status", "item.archive"):
            return (f"item:{args['id']}",)
        if function == "reminder.configure":
            return (f"item:{args['item_id']}",)
        if function in ("reminder.snooze", "reminder.ack", "reminder.recheck", "observation.wait_fresh"):
            return (f"occurrence:{args['occurrence_id']}",) if args.get("occurrence_id") else ()
        if function == "reminder.present":
            return tuple(f"occurrence:{key}" for key in args["occurrence_ids"])
        if function.startswith("habit.") and args.get("id"):
            return (f"habit:{args['id']}",)
        if function == "settings.update":
            scope = args["scope"]
            if scope == "default":
                return ("settings:default",)
            if args.get("id"):
                return (f"{'phase' if scope == 'current' else 'habit'}:{args['id']}",)
            return ()
        if not function.startswith("phase."):
            return ()
        refs = []
        if function == "phase.reassign_interval":
            refs.append(f"interval:{args['interval_id']}")
        elif function in ("phase.credit_recovery", "phase.credit_sleep"):
            refs.append(f"recovery:{args.get('recovery_interval_id') or args['interval_id']}")
        elif function == "phase.start" and args.get("previous_phase_id"):
            refs.append(f"phase:{args['previous_phase_id']}")
        elif function != "phase.start" and args.get("id"):
            refs.append(f"phase:{args['id']}")
        if args.get("habit_id"):
            refs.append(f"habit:{args['habit_id']}")
        links = set(args.get("issue_links", ()))
        links.update(args[key] for key in ("primary_issue_id", "target_issue_id") if args.get(key))
        refs.extend(f"item:{key}" for key in sorted(links))
        return tuple(refs)

    def _actual_version(self, db, ref):
        kind, _, object_id = ref.partition(":")
        if kind == "item":
            view = self.items.get(object_id, db=db)
            return view["version"] if view else None
        if kind == "occurrence":
            task = self.items.resolve_occurrence(object_id, db=db)
            return task.version if task else None
        if kind == "phase":
            model = self.phases.get_phase(object_id, db=db)
        elif kind == "habit":
            model = self.settings.get_habit(object_id, db=db)
        elif kind == "settings":
            model = self.settings.read(db=db)
        elif kind == "interval":
            model = self.phases.get_focus_interval(object_id, db=db)
        elif kind == "recovery":
            model = self.phases.get_recovery_interval(object_id, db=db)
        elif kind == "proposal":
            model, _ = proposals.get(db, self.user_scope, object_id)
        else:
            model = None
        return model.version if model else None

    @staticmethod
    def _bound_version(ref, authority):
        prefix, _, key = ref.partition(":")
        expected = authority.expected_versions
        version = expected.get(ref, expected.get(key))
        if version is None and prefix == "occurrence":
            task_id, _, _ = key.rpartition(":")
            version = expected.get(f"item:{task_id}", expected.get(task_id))
        return version

    def _require_version(self, db, ref, args, authority, *, field="expected_version", optional=False):
        supplied = args.get(field)
        expected = self._bound_version(ref, authority)
        actual = self._actual_version(db, ref)
        if actual is None:
            raise CommandRejected("STALE_TASK")
        if expected is None:
            expected = supplied
        if expected is None and optional:
            return actual
        if actual != expected or (supplied is not None and supplied != expected):
            raise CommandRejected("STALE_TASK", subject_ref=ref, actual_version=actual)
        return expected

    @staticmethod
    def _new_version(args):
        if args.get("expected_version") not in (0, None):
            raise CommandRejected("STALE_TASK")
        return 0

    def _phase(self, db, function, args, authority):
        action = function.partition(".")[2]
        call_id = authority.call_id
        phase_id = args.get("id")
        payload = {key: value for key, value in args.items() if key not in ("id", "expected_version")}
        if action == "start":
            phase_id = _stable_id("phase", authority.user_scope, call_id)
            if authority.origin == "user" and authority.admission_id == f"ui:{call_id}" and authority.subject_refs:
                selected = authority.subject_refs[0]
                if selected.startswith("new_phase:"):
                    phase_id = selected.partition(":")[2]
            version = self._new_version(args)
            if args.get("habit_id"):
                payload["expected_habit_version"] = self._require_version(
                    db, f"habit:{args['habit_id']}", args, authority, field="expected_habit_version",
                )
        elif action == "reassign_interval":
            self._require_version(db, f"interval:{args['interval_id']}", args, authority,
                                  field="expected_interval_version")
            interval = self.phases.get_focus_interval(args["interval_id"], db=db)
            if phase_id and phase_id != interval.phase_id:
                raise CommandRejected("STALE_TASK")
            phase_id = interval.phase_id
            version = self._require_version(db, f"phase:{phase_id}", args, authority, optional=True)
        elif action in ("credit_sleep", "credit_recovery"):
            interval_id = args.get("recovery_interval_id") or args["interval_id"]
            interval = self.phases.get_recovery_interval(interval_id, db=db)
            if interval is None or (phase_id and interval.phase_id != phase_id):
                raise CommandRejected("STALE_TASK")
            phase_id = interval.phase_id
            if action == "credit_recovery":
                recovery_args = args
                if not args.get("id") and args.get("expected_recovery_version") is None:
                    recovery_args = {**args, "expected_recovery_version": args.get("expected_version")}
                    args = {key: value for key, value in args.items() if key != "expected_version"}
                expected = self._require_version(db, f"recovery:{interval_id}", recovery_args, authority,
                                                 field="expected_recovery_version")
                payload["expected_recovery_version"] = expected
            version = self._require_version(db, f"phase:{phase_id}", args, authority, optional=True)
        else:
            version = self._require_version(db, f"phase:{phase_id}", args, authority)

        for ref in self._target_refs(function, args):
            if ref.startswith("item:"):
                self._require_version(db, ref, {}, authority, optional=True)
        if action in ("end", "review"):
            payload = dict(args.get("review") or {})
            if args.get("count_as_completed") is not None:
                if "count_as_completed" in payload and payload["count_as_completed"] != args["count_as_completed"]:
                    raise CommandRejected("INVALID_ARGUMENT")
                payload["count_as_completed"] = args["count_as_completed"]
        source = {"proposal": "proposal", "habit": "habit"}.get(authority.origin, "chat")
        if authority.origin == "user" and authority.admission_id == f"ui:{call_id}":
            source = authority.step_id.split(":", 2)[1]
        result = self.phases.apply_phase_in_transaction(db, PhaseCommand(
            command_id=call_id, phase_id=phase_id, kind=action, expected_version=version,
            source=source, payload=payload,
        ))
        view = _model_view(result.view)
        outcome = self._from_outcome(call_id, function, {
            "status": result.status, "error_code": result.error_code, "original_status": result.original_status,
            "actual_version": result.view.version if result.view else None,
            "subject_ref": f"phase:{result.view.id}" if result.view else f"phase:{phase_id}", "view": view,
        })
        if result.status == "applied" and action == "reassign_interval":
            interval = self.phases.get_focus_interval(args["interval_id"], db=db)
            return replace(outcome, subject_ref=f"interval:{interval.id}", actual_version=interval.version,
                           view=_model_view(interval))
        return outcome

    def _habit(self, db, function, args, authority, now):
        action = function.partition(".")[2]
        habit_id = args.get("id")
        current = self.settings.get_habit(habit_id, db=db) if habit_id else None
        if action == "create":
            version = self._new_version(args)
            title, behavior, status = args["title"], args["behavior"], args.get("status", "enabled")
        else:
            version = self._require_version(db, f"habit:{habit_id}", args, authority)
            patch = args.get("patch", {})
            if action == "update" and not patch:
                raise CommandRejected("INVALID_ARGUMENT")
            if action == "archive":
                patch = {"status": "archived"}
            if current is None:
                raise CommandRejected("NOT_FOUND")
            title = patch.get("title", current.title)
            behavior = {**current.behavior.model_dump(), **(patch.get("behavior") or {})}
            status = patch.get("status", current.status)
        habit = self.settings.save_habit(
            title=title, behavior=HabitBehavior.model_validate(behavior), expected_version=version,
            habit_id=habit_id, status=status, now=now, db=db,
        )
        if habit.status != "enabled":
            self._invalidate_proposals(db, authority.user_scope, lambda p: f"habit:{habit.id}" in p.subject_refs)
        return ToolResult(authority.call_id, "applied", subject_ref=f"habit:{habit.id}",
                          actual_version=habit.version, view=_model_view(habit), message="已更新习惯。")

    def _settings_update(self, db, args, authority, now):
        scope, patch = args["scope"], args["patch"]
        expected = args.get("expected_version")
        if not patch:
            raise CommandRejected("INVALID_ARGUMENT")
        if scope == "habit":
            if not args.get("id"):
                raise CommandRejected("INVALID_ARGUMENT")
            patch = HabitPatch.model_validate(patch).model_dump(exclude_unset=True)
            return self._habit(db, "habit.update", {"id": args["id"], "patch": patch,
                                                   "expected_version": expected}, authority, now)
        if scope == "current":
            phase = self.phases.current_phase_in_transaction(db)
            if not phase or (args.get("id") and phase.id != args["id"]):
                raise CommandRejected("STALE_TASK")
            phase_args = validate_tool_args("phase.adjust", {"id": phase.id, **patch, "expected_version": expected})
            if phase_args["id"] != phase.id:
                raise CommandRejected("INVALID_ARGUMENT")
            return self._phase(db, "phase.adjust", phase_args, authority)
        if args.get("id") is not None:
            raise CommandRejected("INVALID_ARGUMENT")
        version = self._require_version(db, "settings:default", args, authority)
        changes = SettingsPatch.model_validate(patch).model_dump(exclude_unset=True)
        updated = self.settings.update(expected_version=version, before_commit=self.phases.apply_settings_update,
                                       db=db, **changes)
        if not updated.auto_adjust:
            self.scheduler.invalidate_focus_waits(db=db)
        if not updated.auto_adjust or not updated.habits_enabled or not updated.link_issues:
            self._invalidate_proposals(
                db, authority.user_scope, lambda proposal: not self._proposal_enabled(updated, proposal),
            )
        return ToolResult(authority.call_id, "applied", subject_ref="settings:default",
                          actual_version=updated.version, view=_settings_view(updated), message="已更新设置。")

    def _wait_or_recheck(self, db, function, args, authority, now):
        call_id = authority.call_id
        occurrence_id = args.get("occurrence_id")
        version = self._require_version(db, f"occurrence:{occurrence_id}", args, authority) if occurrence_id else None
        run_at = _when(args.get("recheck_at") if function == "decision.recheck" else args.get("run_at"))
        register = function == "observation.wait_fresh" or args.get("on_next_fresh")
        if run_at and register and semantic_background_enabled():
            raise CommandRejected("INVALID_ARGUMENT")
        window_open, window_close = (_when(args.get(key)) for key in ("window_open_at", "window_close_at"))
        if window_open is not None or window_close is not None:
            if window_open is None or window_close is None or run_at or register:
                raise CommandRejected("INVALID_ARGUMENT")
            outcome = self.items.estimate_window(
                occurrence_id, window_open_at=window_open, window_close_at=window_close,
                command_id=call_id, expected_version=version, db=db,
            )
            return self._from_outcome(call_id, function, outcome)
        if not run_at and not register:
            raise CommandRejected("INVALID_ARGUMENT")
        view = {}
        if run_at:
            event_id = self.scheduler.schedule_recheck(occurrence_id=occurrence_id, purpose=args.get("purpose"),
                                                        run_at=run_at, now=now, db=db)
            view.update(event_id=event_id, run_at=run_at.isoformat())
        if register:
            purpose = args.get("purpose", "reminder_recheck")
            if (purpose == "reminder_recheck") != bool(occurrence_id):
                raise CommandRejected("INVALID_ARGUMENT")
            settings = self.settings.read(db=db)
            if purpose == "focus_suggestion" and not settings.auto_adjust:
                raise CommandRejected("DISABLED")
            after_cursor = max(args.get("after_cursor", 0), authority.observation_cursor or 0)
            cursor = {"publication_cursor": after_cursor, "producer_generation": authority.observation_generation}
            valid_until = _when(args.get("valid_until")) or now + timedelta(minutes=2)
            view.update(self.scheduler.register_wait(purpose=purpose, after_cursor=cursor, valid_until=valid_until,
                                                     occurrence_id=occurrence_id, settings_version=settings.version,
                                                     now=now, db=db, changes=args.get("changes")))
        return ToolResult(call_id, "pending", view=view, retry_at=run_at.isoformat() if run_at else None)

    @staticmethod
    def _proposal_enabled(settings, proposal):
        function, args = proposal.function, proposal.args
        setting_scope = args.get("scope") if function == "settings.update" else None
        if (function.startswith("phase.") or setting_scope == "current") and not settings.auto_adjust:
            return False
        if (args.get("habit_id") or function.startswith("habit.") or setting_scope == "habit") and not settings.habits_enabled:
            return False
        return settings.link_issues or not args.get("issue_links")

    def _release_proposal_delivery(self, db, scope, proposal_id):
        proposals.revoke_queued(db, scope, proposal_id,
                                release=lambda delivery_id: self.scheduler.release_opportunity(
                                    delivery_id=delivery_id, db=db,
                                ))

    def _retire_proposal(self, db, scope, proposal, state, *, resolution=None):
        updated = proposal.model_copy(update={"state": state, "version": proposal.version + 1})
        proposals.save(db, scope, updated, resolution=resolution)
        self._release_proposal_delivery(db, scope, proposal.id)
        return updated

    def _invalidate_proposals(self, db, scope, predicate):
        for proposal in proposals.pending(db, scope):
            if predicate(proposal):
                self._retire_proposal(db, scope, proposal, "superseded")

    def _proposal_is_stale(self, db, proposal):
        if proposal.function == "phase.start":
            active = self.phases.current_phase_in_transaction(db)
            if active and active.state in ("running", "paused"):
                return True
            if proposal.args.get("previous_phase_id") and self.phases.phase_start_error(db, proposal.args, active):
                return True
        return any(self._actual_version(db, ref) != version for ref, version in proposal.expected_versions.items())

    def _expire_proposals(self, db, scope, now):
        settings = self.settings.read(db=db)
        for proposal in proposals.pending(db, scope):
            delivery = proposals.latest_delivery(db, scope, proposal.id)
            if now >= proposal.valid_until:
                self._retire_proposal(db, scope, proposal, "expired")
            elif ((delivery and not self._proposal_route_valid(db, delivery))
                  or not self._proposal_enabled(settings, proposal) or self._proposal_is_stale(db, proposal)):
                self._retire_proposal(db, scope, proposal, "superseded")

    def _proposal_versions(self, db, refs, authority):
        versions = {}
        for ref in refs:
            actual = self._actual_version(db, ref)
            if actual is None:
                raise CommandRejected("STALE_TASK")
            version = self._bound_version(ref, authority)
            if version is None and authority.origin != "user":
                raise CommandRejected("NEEDS_CONFIRMATION")
            if version is not None and version != actual:
                raise CommandRejected("STALE_TASK")
            versions[ref] = actual
        return versions

    def _offer_proposal(self, db, args, authority, now):
        scope = authority.user_scope
        call_id = authority.call_id
        function = canonical_tool_name(args["function"])
        if function not in _PROPOSABLE:
            return _rejected(call_id, "INVALID_ARGUMENT")
        payload = validate_tool_args(function, args["args"])
        targets = self._target_refs(function, payload)
        if args.get("subject_refs"):
            target_set = set(targets) | {ref.partition(":")[2] for ref in targets}
            if set(args["subject_refs"]) - target_set:
                return _rejected(call_id, "NEEDS_CONFIRMATION")
        versions = self._proposal_versions(db, targets, authority)
        if function == "phase.start":
            self._new_version(payload)
        elif targets and payload.get("expected_version") is not None:
            if versions[targets[0]] != payload["expected_version"]:
                return _rejected(call_id, "STALE_TASK")
        payload.pop("expected_version", None)
        semantic = semantic_background_enabled() and authority.origin != "user"
        opportunity = None if semantic else self.scheduler.get_opportunity(db=db)
        if (not semantic and not function.startswith("phase.") and authority.opportunity_id
                and authority.opportunity_id != opportunity):
            return _rejected(call_id, "STALE_CONTEXT")
        active = self.phases.current_phase_in_transaction(db)
        if function == "phase.start":
            error = None
            if active and active.state in ("running", "paused"):
                error = "INVALID_TRANSITION"
            elif semantic or payload.get("previous_phase_id"):
                error = self.phases.phase_start_error(db, payload, active)
            if error:
                return _rejected(call_id, error, subject_ref=f"phase:{active.id}" if active else None,
                                 actual_version=active.version if active else None, view=_model_view(active))
        kind = payload.get("kind", "focus") if function == "phase.start" else None
        phase = (active.id, active.state) if active else None
        if semantic:
            if phase is None and function == "phase.start":
                last = db.execute("SELECT id FROM phase_views ORDER BY rowid DESC LIMIT 1").fetchone()
                phase = (last["id"], "finished") if last else None
            business_key = _json(["semantic", function, kind, versions, phase])
        else:
            business_key = _json([function, kind, targets, phase, opportunity])
        previous = self._current_proposal(db, scope, business_key, now, authority, semantic)
        if previous and previous.state in {"rejected", "expired"}:
            return _rejected(call_id, "ALREADY_RESOLVED", subject_ref=f"proposal:{previous.id}",
                             actual_version=previous.version, view=_model_view(previous))
        new = previous is None
        proposal = proposals.Proposal(
            id=_stable_id("proposal", scope, call_id) if new else previous.id,
            version=1 if new else previous.version + 1,
            subject_refs=targets, expected_versions=versions,
            function=function, args=payload, origin_trigger=authority.trigger_id,
            created_at=now if new else previous.created_at, valid_until=now + timedelta(minutes=5),
            display_text=args["display_text"],
        )
        if not self._proposal_enabled(self.settings.read(db=db), proposal):
            return _rejected(call_id, "DISABLED")
        displayed = proposals.latest_delivery(db, scope, previous.id) if semantic and previous else None
        if not new and ((displayed and displayed["state"] in {"permitted", "displayed", "unknown"})
                        or (previous.args == proposal.args and previous.expected_versions == proposal.expected_versions)):
            proposal = previous
        else:
            if not new:
                self._release_proposal_delivery(db, scope, previous.id)
            proposals.save(db, scope, proposal, key=business_key if new else None)
        channel = (args.get("presentation") or {}).get("channel", "pet_card") if semantic else "pet_card"
        delivery_id, denial = self._queue_proposal(db, scope, proposal, authority, now, opportunity, semantic, channel)
        if semantic and denial:
            locked = self.scheduler._lock_state()
            reason = ("desktop_locked" if locked is True else "lock_state_unknown" if locked is None else
                      "presentation_context_changed" if denial == "STALE_CONTEXT" else "presentation_expired")
            return ToolResult(call_id, "pending", subject_ref=f"proposal:{proposal.id}",
                              actual_version=proposal.version, view=_model_view(proposal),
                              error_code=denial, outcome="deferred", retryable=denial == "STALE_CONTEXT",
                              presentation_receipt={"channel": channel, "state": "deferred", "reason": reason},
                              message="建议已加入待确认列表，当前无法显示提示。")
        return ToolResult(call_id, "pending", subject_ref=f"proposal:{proposal.id}",
                          actual_version=proposal.version, delivery_id=delivery_id, view=_model_view(proposal),
                          message="等待你确认。")

    def _current_proposal(self, db, scope, business_key, now, authority, semantic):
        self._expire_proposals(db, scope, now)
        for previous in proposals.get_business(db, scope, business_key):
            if previous.state == "pending" or (previous.state == "rejected" and authority.origin != "user"
                                               and (semantic or authority.opportunity_id or previous.valid_until > now)):
                return previous
            if semantic and previous.state == "expired":
                delivery = proposals.latest_delivery(db, scope, previous.id)
                if delivery and delivery["state"] in {"displayed", "unknown"}:
                    return previous
        return None

    def _queue_proposal(self, db, scope, proposal, authority, now, opportunity, semantic, channel):
        if authority.origin == "user" or (not semantic and authority.context_revision is None):
            return None, None
        valid_until = min(proposal.valid_until, authority.expires_at)
        if not semantic and not proposal.function.startswith("phase.") and authority.observation_valid_until:
            valid_until = min(valid_until, authority.observation_valid_until)
        if valid_until <= now:
            return None, "EXPIRED_REQUEST"
        bounds = {"context_revision": authority.context_revision, "context_valid_until": valid_until,
                  "observation_cursor": authority.observation_cursor,
                  "observation_generation": authority.observation_generation}
        delivery_id = _stable_id("proposal-delivery", scope, proposal.id, proposal.version, opportunity)
        previous = proposals.delivery(db, scope, delivery_id)
        if previous and previous["state"] != "revoked":
            return delivery_id, None
        options = {"recheck_key": delivery_id} if semantic else {}
        if not self.scheduler.authorize_proposal(**bounds, now=now, db=db, **options):
            return None, "STALE_CONTEXT"
        route_binding = self._proposal_route_binding(db) if semantic else None
        proposals.enqueue(db, scope, proposal, delivery_id=delivery_id, opportunity_id="",
                          context_revision=authority.context_revision or 0, valid_until=valid_until,
                          observation_cursor=authority.observation_cursor,
                          observation_generation=authority.observation_generation,
                          channel=channel, route_binding=route_binding)
        return delivery_id, None

    def _proposal_route_binding(self, db):
        route = self.routes._get(db, SYSTEM2)
        return _json([route.route_generation, route.consent_generation, route.verified])

    def _proposal_response(self, db, scope, proposal, answer, now):
        delivery = proposals.latest_delivery(db, scope, proposal.id)
        if delivery and delivery["route_binding"] is not None:
            self.scheduler.record_contact(delivery_id=delivery["delivery_id"], kind="proposal", at=now,
                                          channel=delivery["channel"], subject_ref=proposal.id,
                                          response=answer, db=db)

    def _resolve_proposal(self, db, args, authority, now):
        scope, call_id = authority.user_scope, authority.call_id
        proposal_id = args["id"]
        if authority.proposal_id not in (None, proposal_id):
            return _rejected(call_id, "NEEDS_CONFIRMATION")
        proposal, resolution = proposals.get(db, scope, proposal_id)
        if proposal is None:
            return _rejected(call_id, "NOT_FOUND")
        ref = f"proposal:{proposal.id}"
        if proposal.state != "pending":
            if resolution:
                previous = ToolResult(**resolution)
                return replace(previous, call_id=call_id, status="duplicate",
                               original_status=previous.original_status or previous.status)
            code = {"expired": "EXPIRED_REQUEST", "superseded": "SUPERSEDED"}.get(proposal.state, "ALREADY_RESOLVED")
            return _rejected(call_id, code, subject_ref=ref, actual_version=proposal.version,
                             view=_model_view(proposal))
        if now >= proposal.valid_until:
            proposal = self._retire_proposal(db, scope, proposal, "expired")
            return _rejected(call_id, "EXPIRED_REQUEST", subject_ref=ref, actual_version=proposal.version)
        self._require_version(db, ref, args, authority)
        if args["answer"] != "accept":
            if args.get("amendments"):
                return _rejected(call_id, "INVALID_ARGUMENT")
            result = ToolResult(call_id, "applied", subject_ref=ref, actual_version=proposal.version + 1,
                                message="已拒绝建议。" if args["answer"] == "reject" else "已取消建议。")
            self._retire_proposal(db, scope, proposal, "rejected", resolution=result.to_dict())
            self._proposal_response(db, scope, proposal, "rejected", now)
            return result

        if not self._proposal_enabled(self.settings.read(db=db), proposal):
            self._retire_proposal(db, scope, proposal, "superseded")
            return _rejected(call_id, "DISABLED")
        if self._proposal_is_stale(db, proposal):
            self._retire_proposal(db, scope, proposal, "superseded")
            return _rejected(call_id, "STALE_TASK")
        payload = self._amend(proposal, args.get("amendments"))
        command_id = _stable_id("proposal-command", scope, proposal.id, proposal.version)
        accepted = replace(authority, call_id=command_id, origin="proposal",
                           expected_versions=proposal.expected_versions, proposal_id=proposal.id)
        result = self._guarded_execute(db, proposal.function, payload, accepted, now)
        self._remember(db, scope, proposal.function, _json([proposal.id, proposal.version, payload]), result)
        updated = proposal.model_copy(update={
            "state": "accepted" if result.status == "applied" else "superseded",
            "version": proposal.version + 1,
            "applied_command_id": command_id if result.status == "applied" else None,
            "args": payload,
        })
        display_result = replace(result, call_id=call_id, resolved_function=proposal.function)
        proposals.save(db, scope, updated, resolution=display_result.to_dict())
        self._release_proposal_delivery(db, scope, proposal_id)
        if result.status == "applied":
            self._proposal_response(db, scope, proposal, "accepted", now)
        return display_result

    def _amend(self, proposal, amendments):
        if not amendments:
            return proposal.args
        if set(amendments) - _AMENDABLE.get(proposal.function, set()):
            raise CommandRejected("NEEDS_CONFIRMATION")
        args = {**proposal.args, **amendments}
        if proposal.function == "phase.adjust":
            if len(amendments) != 1:
                raise CommandRejected("INVALID_ARGUMENT")
            args.pop("total_s" if "add_s" in amendments else "add_s", None)
        if proposal.function == "item.update":
            args["patch"] = {**proposal.args["patch"], **amendments["patch"]}
        args = validate_tool_args(proposal.function, args)
        if self._target_refs(proposal.function, args) != proposal.subject_refs:
            raise CommandRejected("NEEDS_CONFIRMATION")
        return args

    def pending_proposals(self, user_scope=None, as_of=None):
        scope = user_scope or self.user_scope
        if scope != self.user_scope:
            return []
        with self._transaction() as db:
            self._expire_proposals(db, scope, as_of or self.now())
            return [_model_view(proposal) for proposal in proposals.pending(db, scope)]

    def pending_presentations(self, user_scope=None):
        scope = user_scope or self.user_scope
        if scope != self.user_scope:
            return []
        with self._transaction() as db:
            now = self.now()
            self._expire_proposals(db, scope, now)
            queued = []
            for row in proposals.deliveries(db, scope, state="queued"):
                if now < _when(row["valid_until"]) and self._proposal_route_valid(db, row):
                    queued.append(row)
                else:
                    self._revoke_presentation(db, scope, row["delivery_id"])
            for row in proposals.deliveries(db, scope, state="permitted"):
                if now >= _when(row["valid_until"]):
                    proposals.set_delivery(db, scope, row["delivery_id"], "unknown")
            return queued

    def _revoke_presentation(self, db, scope, delivery_id):
        proposals.set_delivery(db, scope, delivery_id, "revoked")
        self.scheduler.release_opportunity(delivery_id=delivery_id, db=db)

    def _proposal_route_valid(self, db, row):
        return (row["route_binding"] is None or (semantic_background_enabled()
                and row["route_binding"] == self._proposal_route_binding(db)))

    def _live_presentation(self, db, scope, row, now):
        proposal, _ = proposals.get(db, scope, row["proposal_id"])
        if (proposal is None or proposal.state != "pending" or proposal.version != row["proposal_version"]
                or not self._proposal_route_valid(db, row)
                or now >= proposal.valid_until or not self._proposal_enabled(self.settings.read(db=db), proposal)):
            return None
        if self._proposal_is_stale(db, proposal):
            return None
        if not self.scheduler.proposal_permit(
            opportunity_id=row["opportunity_id"], delivery_id=row["delivery_id"],
            context_revision=row["context_revision"] or None, context_valid_until=_when(row["valid_until"]),
            observation_cursor=row["observation_cursor"], observation_generation=row["observation_generation"],
            now=now, db=db,
        ):
            return None
        return proposal

    @staticmethod
    def _matching_lease(row, generation):
        return row is not None and (generation is None or row["presentation_generation"] == generation)

    def permit_presentation(self, delivery_id, user_scope=None, *, generation=None):
        scope = user_scope or self.user_scope
        if scope != self.user_scope:
            return None
        with self._transaction(timeout=_QT_BUSY_TIMEOUT) as db:
            row = proposals.delivery(db, scope, delivery_id)
            if not self._matching_lease(row, generation) or row["state"] != "queued":
                return None
            proposal = self._live_presentation(db, scope, row, self.now())
            if proposal is None:
                self._revoke_presentation(db, scope, delivery_id)
                return None
            proposals.set_delivery(db, scope, delivery_id, "permitted")
            return _model_view(proposal)

    def validate_presentation(self, delivery_id, user_scope=None, *, generation=None):
        """Recheck the one-shot permission immediately before the GUI draws."""
        scope = user_scope or self.user_scope
        if scope != self.user_scope:
            return False
        with self._transaction(timeout=_QT_BUSY_TIMEOUT) as db:
            row = proposals.delivery(db, scope, delivery_id)
            return bool(self._matching_lease(row, generation) and row["state"] == "permitted"
                        and self._live_presentation(db, scope, row, self.now()))

    def ack_presentation(self, delivery_id, user_scope=None, *, displayed, generation=None):
        scope = user_scope or self.user_scope
        if scope != self.user_scope:
            return False
        with self._transaction(timeout=_QT_BUSY_TIMEOUT) as db:
            row = proposals.delivery(db, scope, delivery_id)
            if not self._matching_lease(row, generation) or row["state"] not in {"permitted", "unknown"}:
                return False
            if displayed:
                proposals.set_delivery(db, scope, delivery_id, "displayed")
                if row["route_binding"] is not None:
                    self.scheduler.record_contact(delivery_id=delivery_id, kind="proposal", at=self.now(),
                                                  channel=row["channel"], subject_ref=row["proposal_id"], db=db)
            else:
                self._revoke_presentation(db, scope, delivery_id)
            return True
