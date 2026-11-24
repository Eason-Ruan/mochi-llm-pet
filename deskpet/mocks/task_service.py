"""Single-owner in-memory reference for the M0 TaskService contract.

Construct and call this service on one domain thread. It has no Qt, OS, network,
SQLite, or hidden clock dependencies. ``pump`` consumes the injected clock; GUI
clients reach it through the queued gateway. B can replace this adapter with a
transactional repository while retaining the public ports and contract suite.

Persistence/restart recovery is deliberately not implemented by this adapter.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import math
from typing import Callable
from uuid import uuid4

from pydantic import ValidationError

from deskpet.contracts import (
    Clock, CommandResult, DecisionEnvelope, DeliveryPort, DisplayCommand,
    DisplayReceipt, ErrorCode, Observation, ProposedPlan, TaskCommand, TaskDraft,
    TaskView, ToolResult,
)


@dataclass
class _Attempt:
    command: DisplayCommand
    last_attempt_mono: float
    status: str = "pending"
    attempts: int = 0
    receipt: DisplayReceipt | None = None


class MemoryTaskService:
    """Reference semantics, including CAS, cancellation and receipt-driven delivery.

    ``emit`` accepts a TraceRecorder-compatible ``emit(kind, **fields)`` callable.
    Missing acknowledgements time out after ``receipt_timeout_s``; an explicit
    failure is retried no sooner than ``retry_interval_s``. Every retry reuses the
    delivery ID, so an idempotent GUI reuses one card rather than displaying twice.
    """

    def __init__(
        self, clock: Clock, delivery: DeliveryPort, emit: Callable,
        *, receipt_timeout_s: float = 5.0, retry_interval_s: float = 1.0, enforce_opportunities=False,
    ):
        if (not math.isfinite(receipt_timeout_s) or receipt_timeout_s <= 0
                or not math.isfinite(retry_interval_s) or retry_interval_s <= 0):
            raise ValueError("delivery timeout and retry interval must be finite and positive")
        self.clock = clock
        self.delivery = delivery
        self.emit = emit
        self.receipt_timeout_s = receipt_timeout_s
        self.retry_interval_s = retry_interval_s
        self.enforce_opportunities = enforce_opportunities
        self.auto_opportunity = True
        self._opportunity: dict[str, str | None] = {}
        self._external_owner: set[tuple[str, int]] = set()
        self._focus_running = False
        self._tasks: dict[str, TaskView] = {}
        self._commands: dict[str, tuple[str, CommandResult]] = {}
        self._observation: Observation | None = None
        self._decisions: dict[str, str] = {}
        self._rechecks: dict[str, tuple[int, datetime, str]] = {}
        self._attempts: dict[str, _Attempt] = {}
        self._active_delivery: dict[str, str] = {}
        self._last_deferred: dict[str, tuple[int, int, str]] = {}

    def get(self, task_id: str) -> TaskView | None:
        return self._tasks.get(task_id)

    def list_tasks(self) -> list[TaskView]:
        return [self._tasks[key] for key in sorted(self._tasks)]

    @staticmethod
    def _fingerprint(model) -> str:
        fields = model.model_dump(mode="json")
        if isinstance(model, TaskCommand) and model.kind in {"create", "create_registered", "edit", "configure"}:
            if set(model.payload) == {"draft"}:
                try:
                    fields["payload"] = {"draft": TaskDraft.model_validate(model.payload["draft"]).model_dump(mode="json")}
                except (ValueError, TypeError):
                    pass  # Fingerprint rejected raw input too.
        return json.dumps(fields, sort_keys=True,
                          separators=(",", ":"), ensure_ascii=False)

    def _command_rejection(
        self, command: TaskCommand, code: ErrorCode, message: str,
    ) -> CommandResult:
        view = self.get(command.task_id)
        self.emit("command.rejected", task_id=command.task_id,
                  task_version=view.version if view else None,
                  payload={"command_id": command.command_id, "kind": command.kind,
                           "error_code": code.value, "message": message})
        return CommandResult(command_id=command.command_id, status="rejected",
                             error_code=code, actual_version=view.version if view else None,
                             view=view, message=message)

    def apply(self, command: TaskCommand) -> CommandResult:
        """Commit one versioned command. Retries replay its original result.

        A command ID is bound to its original complete input and terminal result,
        including rejection. Reusing it with another payload is invalid. A client
        retries a changed operation with a new ID and current expected_version.
        """
        fingerprint = self._fingerprint(command)
        previous = self._commands.get(command.command_id)
        if previous:
            matches = fingerprint == previous[0]
            if not matches and command.kind in {"create", "create_registered", "edit", "configure"}:
                # Schema-one receipts recorded raw TaskDraft JSON with the older field set.
                matches = fingerprint == self._fingerprint(TaskCommand.model_validate_json(previous[0]))
            if not matches:
                return self._command_rejection(
                    command, ErrorCode.INVALID_ARGUMENT, "command_id was reused with different input")
            original = previous[1]
            self.emit("command.duplicate", task_id=command.task_id,
                      task_version=original.actual_version,
                      payload={"command_id": command.command_id,
                               "original_status": original.status})
            return original.model_copy(update={
                "status": "duplicate", "error_code": ErrorCode.DUPLICATE,
                "message": f"replayed original {original.status} result",
            })

        result = self._apply_once(command)
        self._commands[command.command_id] = (fingerprint, result)
        return result

    def _apply_once(self, command: TaskCommand) -> CommandResult:
        reject = lambda code, message: self._command_rejection(command, code, message)
        if not command.command_id.strip() or not command.task_id.strip():
            return reject(ErrorCode.INVALID_ARGUMENT, "command_id and task_id must be nonempty")
        view = self.get(command.task_id)
        if command.kind in {"create", "create_registered"}:
            if view is not None or command.expected_version != 0:
                return reject(ErrorCode.STALE_TASK, "create requires absent task and expected_version=0")
        else:
            if view is None:
                return reject(ErrorCode.NOT_FOUND, "task does not exist")
            if command.expected_version != view.version:
                return reject(ErrorCode.STALE_TASK, "expected_version does not match task")
            if view.archived_at is not None:
                return reject(ErrorCode.CANCELLED, "task was archived")
            if view.status == "cancelled" and command.kind not in {"set_status", "archive"}:
                return reject(ErrorCode.CANCELLED, "task was cancelled")
            if view.status == "completed" and command.kind not in {"set_status", "archive"}:
                return reject(ErrorCode.INVALID_TRANSITION, "completed task is terminal")

        expected_fields = {"draft"} if command.kind in {"create", "create_registered", "edit", "configure"} else (
            {"seconds"} if command.kind == "snooze" else {"status"} if command.kind == "set_status" else set())
        if command.kind == "estimate_window":
            expected_fields = {"window_open_at", "window_close_at"}
        fields = set(command.payload)
        if fields != expected_fields and not (command.kind == "snooze" and "seconds" in fields
                and fields <= {"seconds", "mode", "window_before_s", "window_after_s", "snoozed_at"}):
            return reject(ErrorCode.INVALID_ARGUMENT,
                          f"{command.kind} payload requires exactly {sorted(expected_fields)}")
        try:
            if command.kind in {"create", "create_registered", "edit", "configure"}:
                draft = TaskDraft.model_validate(command.payload["draft"])
            elif command.kind == "snooze":
                seconds = command.payload["seconds"]
                mode = command.payload.get("mode", "legacy")
                snoozed_at = (datetime.fromisoformat(command.payload["snoozed_at"])
                              if "snoozed_at" in command.payload else None)
                if snoozed_at is not None and snoozed_at.utcoffset() is None:
                    raise ValueError("snooze event time must include a UTC offset")
                if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
                    raise ValueError("seconds must be numeric")
                if not math.isfinite(seconds) or seconds <= 0:
                    raise ValueError("seconds must be finite and positive")
                if mode not in ("legacy", "flexible_target", "no_earlier", "exact"):
                    raise ValueError("invalid snooze mode")
                for name in ("window_before_s", "window_after_s"):
                    if name in command.payload:
                        extra = command.payload[name]
                        if (mode != "flexible_target" or isinstance(extra, bool)
                                or not isinstance(extra, (int, float)) or not math.isfinite(extra)
                                or not 0 <= extra < seconds):
                            raise ValueError("snooze window offsets must be shorter than the delay")
        except (ValidationError, ValueError, TypeError) as exc:
            return reject(ErrorCode.INVALID_ARGUMENT, str(exc))

        now = self.clock.wall_now()
        if command.kind in {"create", "create_registered"}:
            registered = command.kind == "create_registered"
            updated = TaskView(task_id=command.task_id, version=1, draft=draft,
                               status="registered" if registered else "draft",
                               confirmed_at=now if registered else None, created_at=now, updated_at=now,
                               issue_status="created" if draft.item_type == "issue" else None)
        elif command.kind == "confirm":
            if view.status != "draft":
                return reject(ErrorCode.INVALID_TRANSITION, "only a draft can be confirmed")
            updated = view.model_copy(update={"status": "registered", "version": view.version+1,
                                               "confirmed_at": now})
        elif command.kind in {"edit", "configure"}:
            if draft.item_type != view.draft.item_type:
                return reject(ErrorCode.INVALID_TRANSITION, "an existing item cannot change type")
            unchanged_rule = command.kind == "edit" and all(
                getattr(draft, name) == getattr(view.draft, name) for name in type(draft).model_fields
                if name not in {"content", "description", "due_at"}
            )
            new_rule = not unchanged_rule and (draft.reminder_enabled or view.draft.reminder_enabled)
            updated = view.model_copy(update={"draft": draft, "version": view.version+1,
                                               "displayed_at": view.displayed_at if unchanged_rule else None,
                                               "acknowledged_at": view.acknowledged_at if unchanged_rule else None,
                                               "expired_at": view.expired_at if unchanged_rule else None,
                                               "last_snoozed_at": view.last_snoozed_at if unchanged_rule else None,
                                               "rule_version": view.rule_version + int(new_rule),
                                               "occurrence_rule_version": (view.rule_version+1 if new_rule
                                                                           else view.occurrence_rule_version),
                                               "occurrence": view.occurrence + int(
                                                   view.status == "registered" and new_rule and view.draft.reminder_enabled)})
        elif command.kind == "estimate_window":
            current = view.draft
            target_only = (current.target_at is not None and current.window_open_at is None
                           and current.window_close_at is None)
            closed = current.window_close_at is not None and current.window_close_at <= now
            if (not view.active_reminder or view.displayed_at is not None or current.mode != "soft"
                    or not (target_only or closed)):
                return reject(ErrorCode.INVALID_TRANSITION, "an active soft estimate cannot be replaced early")
            try:
                draft = TaskDraft.model_validate(current.model_dump() | command.payload | {"window_computed_at": now})
            except (ValidationError, ValueError, TypeError) as exc:
                return reject(ErrorCode.INVALID_ARGUMENT, str(exc))
            if draft.window_open_at is None or draft.window_close_at is None:
                return reject(ErrorCode.INVALID_ARGUMENT, "an estimate requires both window endpoints")
            start = max(edge for edge in (draft.opens_at, draft.earliest_at) if edge is not None)
            close = draft.opportunity_close_at
            if close <= max(start, now):
                return reject(ErrorCode.OUTSIDE_WINDOW, "the estimate must have a remaining valid opportunity")
            # Persist the intersection so the UI and scheduler show the same window.
            updated = view.model_copy(update={
                "draft": draft.model_copy(update={"window_open_at": start, "window_close_at": close}),
                "version": view.version + 1,
            })
        elif command.kind == "snooze":
            if not view.active_reminder or (view.draft.expires_at and now >= view.draft.expires_at):
                return reject(ErrorCode.INVALID_TRANSITION, "only a registered task can be snoozed")
            if snoozed_at is not None and snoozed_at > now:
                return reject(ErrorCode.INVALID_ARGUMENT, "snooze event time cannot be in the future")
            snoozed_at = snoozed_at or now
            try:
                target = snoozed_at + timedelta(seconds=seconds)
                old = view.draft
                if (mode == "legacy" and old.earliest_at is not None
                        and old.latest_first_delivery_at is not None):
                    bounds = {"earliest_at": target, "latest_first_delivery_at":
                              target + (old.latest_first_delivery_at - old.earliest_at)}
                else:
                    before = command.payload.get("window_before_s")
                    after = command.payload.get("window_after_s")
                    if mode == "flexible_target" and old.target_at is not None:
                        if before is None and old.window_open_at is not None:
                            before = min(seconds / 3, max(0, (old.target_at - old.window_open_at).total_seconds()))
                        if after is None and old.window_close_at is not None:
                            after = min(seconds / 3, max(0, (old.window_close_at - old.target_at).total_seconds()))
                    bounds = {"mode": "hard" if mode == "exact" else "soft", "latest_first_delivery_at": None,
                              "earliest_at": None,
                              "window_close_at": target + timedelta(seconds=after) if after else None,
                              "window_open_at": target - timedelta(seconds=before) if before else None,
                              "window_computed_at": now if before or after else None,
                              "remind_after": target if mode in ("exact", "no_earlier") else None,
                              "target_at": target if mode in ("legacy", "flexible_target", "exact") else None,
                              "remind_by": target if mode == "exact" else None}
                if old.urgency_until and old.urgency_until <= target:
                    bounds.update(urgency_until=None, urgency_reason=None)
                draft = TaskDraft.model_validate(old.model_dump() | bounds)
            except OverflowError:
                return reject(ErrorCode.INVALID_ARGUMENT, "snooze exceeds supported datetime range")
            except ValidationError as exc:
                return reject(ErrorCode.INVALID_ARGUMENT, str(exc))
            updated = view.model_copy(update={"draft": draft, "version": view.version+1,
                                               "occurrence": view.occurrence+1, "displayed_at": None,
                                               "rule_version": view.rule_version+1, "last_snoozed_at": snoozed_at,
                                               "occurrence_rule_version": view.rule_version+1,
                                               "acknowledged_at": None, "expired_at": None})
        elif command.kind == "ack":
            if not view.active_reminder or (view.draft.expires_at and now >= view.draft.expires_at):
                return reject(ErrorCode.INVALID_TRANSITION, "only an active reminder can be acknowledged")
            updated = view.model_copy(update={"version": view.version+1, "acknowledged_at": now,
                                              "status": "completed" if view.draft.item_type == "reminder" else "registered"})
        elif command.kind == "cancel":
            updated = view.model_copy(update={"status": "cancelled", "version": view.version+1,
                                              "issue_status": "cancelled" if view.draft.item_type == "issue" else None})
        elif command.kind == "complete":
            if view.status != "registered":
                return reject(ErrorCode.INVALID_TRANSITION, "only a registered task can be completed")
            updated = view.model_copy(update={"status": "completed", "version": view.version+1,
                                              "issue_status": "completed" if view.draft.item_type == "issue" else None})
        elif command.kind == "set_status":
            status = command.payload["status"]
            allowed = ("created", "in_progress", "paused", "completed", "cancelled")
            if view.draft.item_type != "issue" or status not in allowed or status == view.effective_issue_status:
                return reject(ErrorCode.INVALID_TRANSITION, "invalid issue status transition")
            if view.status in {"completed", "cancelled"}:
                if status != "created":
                    return reject(ErrorCode.INVALID_TRANSITION, "reopen the issue before changing its status")
                draft = TaskDraft(content=view.draft.content, description=view.draft.description,
                                  item_type="issue", reminder_enabled=False, due_at=view.draft.due_at,
                                  timezone=view.draft.timezone)
                updated = view.model_copy(update={"draft": draft, "status": "registered", "issue_status": status,
                                                  "version": view.version+1, "rule_version": view.rule_version+1,
                                                  "occurrence_rule_version": None,
                                                  "occurrence": view.occurrence+1, "displayed_at": None,
                                                  "acknowledged_at": None, "expired_at": None, "last_snoozed_at": None})
            else:
                updated = view.model_copy(update={"issue_status": status, "version": view.version+1,
                                                  "status": status if status in {"completed", "cancelled"} else view.status})
        elif command.kind == "archive":
            status = "completed" if view.status == "completed" else "cancelled"
            updated = view.model_copy(update={"archived_at": now, "status": status, "version": view.version+1,
                                              "issue_status": status if view.draft.item_type == "issue" else None})
        else:  # Defensive for non-validated callers; normal TaskCommand rejects unknown kinds.
            return reject(ErrorCode.INVALID_ARGUMENT, "unknown command")

        rule_closed = view is not None and view.active_reminder and (
            not updated.active_reminder or updated.occurrence != view.occurrence)
        if (rule_closed and updated.status in {"completed", "cancelled"}
                and updated.draft.item_type == "issue"):
            updated = updated.model_copy(update={
                "rule_version": view.rule_version + 1,
                "occurrence_rule_version": view.occurrence_rule_version or view.rule_version,
            })
        updated = updated.model_copy(update={"updated_at": now})
        self._tasks[command.task_id] = updated
        if command.kind in {"edit", "configure", "estimate_window", "snooze", "ack", "cancel", "complete", "archive", "set_status"}:
            recheck = self._rechecks.get(command.task_id)
            self._invalidate_pending(command.task_id)
            if recheck and not rule_closed and updated.active_reminder:
                self._rechecks[command.task_id] = (updated.version, *recheck[1:])
        kind = {"create": "created", "create_registered": "created", "confirm": "confirmed", "edit": "edited",
                "configure": "edited", "estimate_window": "window_estimated",
                "snooze": "snoozed", "ack": "acknowledged", "cancel": "cancelled", "complete": "completed",
                "archive": "archived", "set_status": "status_changed"}[command.kind]
        self.emit(f"task.{kind}", task_id=updated.task_id, task_version=updated.version,
                  payload={"command_id": command.command_id, "status": updated.status,
                           "occurrence": updated.occurrence})
        return CommandResult(command_id=command.command_id, status="applied",
                             actual_version=updated.version, view=updated)

    def _invalidate_pending(self, task_id: str) -> None:
        self._active_delivery.pop(task_id, None)
        self._rechecks.pop(task_id, None)
        self._last_deferred.pop(task_id, None)
        try:
            self.delivery.dismiss(task_id)
        except Exception as exc:  # State is already committed; rendering cannot roll it back.
            self.emit("delivery.dismiss_failed", task_id=task_id,
                      task_version=self._tasks[task_id].version,
                      payload={"error": type(exc).__name__})

    def observe(self, observation: Observation) -> None:
        if self._observation and observation.context_revision < self._observation.context_revision:
            self.emit("observation.rejected", payload={"error_code": ErrorCode.STALE_CONTEXT.value,
                                                       "context_revision": observation.context_revision})
            return
        previous = self._observation
        self._observation = observation
        if self.enforce_opportunities:
            state = self._effective_state()
            if state in {"away", "working"}:
                self._opportunity = {}
            elif state == "available" and not self._opportunity:
                self._opportunity = {"id": uuid4().hex, "claim": None}
        # Keep the newest sampling time/TTL even if the semantic state is
        # unchanged. Polling is not a new experimental observation event.
        def trace_key(value):
            return (value.context_revision, value.estimate.state,
                    value.estimate.evidence, value.estimate.source,
                    value.source, value.capabilities, value.application)
        if previous is not None and trace_key(previous) == trace_key(observation):
            return
        self.emit("observation.updated", source=observation.source,
                  simulated=observation.source != "live",
                  payload={"context_revision": observation.context_revision,
                           "state": observation.estimate.state,
                           "estimate_source": observation.estimate.source})

    def validate(self, envelope: DecisionEnvelope) -> ErrorCode | None:
        view = self.get(envelope.task_id)
        if view is None:
            return ErrorCode.NOT_FOUND
        if view.status == "cancelled":
            return ErrorCode.CANCELLED
        if envelope.task_version != view.version:
            return ErrorCode.STALE_TASK
        if not view.active_reminder:
            return ErrorCode.INVALID_TRANSITION
        now = self.clock.wall_now()
        if now >= envelope.expires_at:
            return ErrorCode.EXPIRED_REQUEST
        if envelope.created_at > now:
            return ErrorCode.INVALID_ARGUMENT
        # Evidence can expire without any subsequent observation arriving. Do
        # not let a still-valid request keep relying on expired user/rule state.
        if self._observation is not None:
            until = self._observation.estimate.valid_until
            if (self._observation.observed_at > now
                    or (until is not None and until <= now)):
                return ErrorCode.STALE_CONTEXT
        current_revision = self._observation.context_revision if self._observation else 1
        if envelope.context_revision != current_revision:
            return ErrorCode.STALE_CONTEXT
        return None

    def accept_decision(self, plan: ProposedPlan) -> ErrorCode | None:
        error = self.validate(plan.envelope)
        fingerprint = self._fingerprint(plan)
        previous = self._decisions.get(plan.envelope.request_id)
        if error is None and previous:
            error = ErrorCode.DUPLICATE if previous == fingerprint else ErrorCode.INVALID_ARGUMENT
        if error is None:
            self._decisions[plan.envelope.request_id] = fingerprint
        self.emit("decision.accepted" if error is None else "decision.rejected",
                  task_id=plan.envelope.task_id, task_version=plan.envelope.task_version,
                  request_id=plan.envelope.request_id,
                  payload={"error_code": error.value if error else None})
        return error

    def schedule_recheck(self, envelope: DecisionEnvelope, run_at: datetime) -> ErrorCode | None:
        error = self.validate(envelope)
        if error is not None:
            return error
        if run_at.tzinfo is None or run_at.utcoffset() is None:
            return ErrorCode.INVALID_ARGUMENT
        view = self._tasks[envelope.task_id]
        deadline = view.draft.hard_deadline
        if (run_at < self.clock.wall_now() or (deadline and run_at > deadline)
                or (view.draft.expires_at and run_at >= view.draft.expires_at)):
            return ErrorCode.OUTSIDE_WINDOW
        self._rechecks[view.task_id] = (view.version, run_at, envelope.request_id)
        self.emit("recheck.scheduled", task_id=view.task_id, task_version=view.version,
                  request_id=envelope.request_id, payload={"run_at": run_at.isoformat()})
        return None

    def request_delivery(self, envelope: DecisionEnvelope) -> ToolResult:
        # Revalidate authority even when fetching a previously accepted call's
        # outcome: cancellation/version/context/expiry still take precedence.
        error = self.validate(envelope)
        view = self.get(envelope.task_id)
        if error is not None:
            return ToolResult(call_id=envelope.request_id, status="rejected", error_code=error,
                              state_version=view.version if view else None)
        if (view.task_id, view.occurrence) in self._external_owner:
            return ToolResult(call_id=envelope.request_id, status="pending", state_version=view.version,
                              evidence={"channel": "os_notification"})
        existing = self._active_delivery.get(view.task_id)
        if existing:
            attempt = self._attempts[existing]
            if (attempt.command.task_version == view.version
                    and attempt.command.occurrence == view.occurrence):
                # A window bounds creation of a new effect, not retrieval of a
                # real receipt. GUI acknowledgement may legitimately arrive
                # after the earliest/latest edge (especially a hard due time).
                return self._delivery_result(envelope, view, attempt)
        now = self.clock.wall_now()
        start, deadline = view.draft.opens_at, view.draft.hard_deadline
        close = view.draft.window_close_at
        if ((start is not None and now < start) or (deadline is not None and now > deadline)
                or (view.draft.expires_at and now >= view.draft.expires_at)):
            return ToolResult(call_id=envelope.request_id, status="rejected",
                              error_code=ErrorCode.OUTSIDE_WINDOW, state_version=view.version)
        if deadline is not None and now == deadline:
            reason = "hard_due" if view.draft.mode == "hard" else "deadline_fallback"
            return self._delivery_result(envelope, view, self._ensure_delivery(view, reason))
        if close is not None and now >= close:
            return ToolResult(call_id=envelope.request_id, status="rejected",
                              error_code=ErrorCode.OUTSIDE_WINDOW, state_version=view.version)
        if self.enforce_opportunities and self.claim_opportunity(self._delivery_id(view)) is None:
            return ToolResult(call_id=envelope.request_id, status="rejected",
                              error_code=ErrorCode.STALE_CONTEXT, state_version=view.version)
        return self._delivery_result(envelope, view, self._ensure_delivery(view, "tool"))

    def claim_opportunity(self, claim_id: str) -> str | None:
        if not claim_id or self._focus_running or not self._available() or not self._opportunity:
            return None
        if self._opportunity["claim"] not in (None, claim_id):
            return None
        self._opportunity["claim"] = claim_id
        return self._opportunity["id"]

    @staticmethod
    def _delivery_id(view: TaskView) -> str:
        return f"delivery:{view.task_id}:{view.occurrence}:v{view.version}"

    @staticmethod
    def _delivery_result(envelope: DecisionEnvelope, view: TaskView, attempt: _Attempt) -> ToolResult:
        result_status = "succeeded" if attempt.status == "displayed" else (
            "failed" if attempt.status == "failed" else "pending")
        return ToolResult(call_id=envelope.request_id, status=result_status,
                          receipt_id=attempt.command.delivery_id if result_status == "succeeded" else None,
                          state_version=view.version,
                          error_code=(attempt.receipt.error_code if attempt.receipt else None),
                          evidence={"delivery_id": attempt.command.delivery_id,
                                    "receipt_source": attempt.receipt.source if attempt.receipt else None})

    def _ensure_delivery(self, view: TaskView, reason: str) -> _Attempt:
        existing = self._active_delivery.get(view.task_id)
        if existing:
            return self._attempts[existing]
        now = self.clock.wall_now()
        delivery_id = self._delivery_id(view)
        early = reason in {"opportunity", "tool"}
        observed = self._observation if early else None
        command = DisplayCommand(delivery_id=delivery_id, task_id=view.task_id,
                                 task_version=view.version, occurrence=view.occurrence,
                                 content=view.draft.content, requested_at=now,
                                 expires_at=self._lease_end(view, now), reason=reason,
                                 context_revision=observed.context_revision if observed else None,
                                 context_valid_until=observed.estimate.valid_until if observed else None,
                                 sound=None if early else "reminder")
        attempt = _Attempt(command=command, last_attempt_mono=self.clock.monotonic_now())
        self._attempts[delivery_id] = attempt
        self._active_delivery[view.task_id] = delivery_id
        self._dispatch(attempt)
        return attempt

    def _lease_end(self, view: TaskView, now: datetime) -> datetime:
        lease = now + timedelta(seconds=max(30.0, self.receipt_timeout_s * 2))
        return min(lease, view.draft.expires_at) if view.draft.expires_at else lease

    def _dispatch(self, attempt: _Attempt) -> None:
        now = self.clock.wall_now()
        command = attempt.command
        view = self._tasks[command.task_id]
        # Renew an expired execution lease without inventing a new delivery.
        if now >= command.expires_at:
            command = command.model_copy(update={
                "expires_at": self._lease_end(view, now),
            })
            attempt.command = command
        attempt.status = "pending"
        attempt.attempts += 1
        attempt.last_attempt_mono = self.clock.monotonic_now()
        self.emit("delivery.requested", task_id=command.task_id, task_version=command.task_version,
                  delivery_id=command.delivery_id,
                  payload={"reason": command.reason, "attempt": attempt.attempts,
                           "occurrence": command.occurrence})
        try:
            receipt = self.delivery.show(command)
        except Exception as exc:
            attempt.status = "failed"
            self.emit("delivery.failed", task_id=command.task_id, task_version=command.task_version,
                      delivery_id=command.delivery_id,
                      payload={"error_code": ErrorCode.FAILED.value, "error": type(exc).__name__})
            return
        if receipt is not None:
            try:
                receipt = DisplayReceipt.model_validate(receipt)
            except (ValidationError, ValueError, TypeError):
                attempt.status = "failed"
                self.emit("delivery.failed", task_id=command.task_id,
                          task_version=command.task_version, delivery_id=command.delivery_id,
                          payload={"error_code": ErrorCode.INVALID_ARGUMENT.value,
                                   "error": "invalid display receipt"})
            else:
                self.record_receipt(receipt)

    def record_receipt(self, receipt: DisplayReceipt) -> ErrorCode | None:
        attempt = self._attempts.get(receipt.delivery_id)
        view = self.get(receipt.task_id)
        error = None
        if attempt is None or view is None:
            error = ErrorCode.NOT_FOUND
        elif view.status == "cancelled":
            error = ErrorCode.CANCELLED
        elif (receipt.task_id != attempt.command.task_id
              or receipt.task_version != attempt.command.task_version
              or receipt.task_version != view.version):
            error = ErrorCode.STALE_TASK
        elif view.status != "registered":
            error = ErrorCode.INVALID_TRANSITION
        elif (receipt.rendered_at < attempt.command.requested_at
              or receipt.rendered_at > self.clock.wall_now()
              or receipt.rendered_at > attempt.command.expires_at):
            error = ErrorCode.INVALID_ARGUMENT
        elif attempt.status == "displayed":
            error = ErrorCode.DUPLICATE
        if error is not None:
            self.emit("delivery.duplicate" if error == ErrorCode.DUPLICATE else "delivery.rejected",
                      task_id=receipt.task_id, task_version=receipt.task_version,
                      delivery_id=receipt.delivery_id, source=receipt.source,
                      simulated=receipt.source != "native",
                      payload={"error_code": error.value, "outcome": receipt.outcome})
            return error

        attempt.receipt = receipt
        attempt.status = "displayed" if receipt.outcome == "displayed" else "failed"
        if receipt.outcome == "displayed":
            self._tasks[view.task_id] = view.model_copy(update={"displayed_at": receipt.rendered_at})
            self._rechecks.pop(view.task_id, None)
        deadline = view.draft.hard_deadline
        self.emit("delivery.displayed" if receipt.outcome == "displayed" else "delivery.failed",
                  task_id=receipt.task_id, task_version=receipt.task_version,
                  delivery_id=receipt.delivery_id, source=receipt.source,
                  simulated=receipt.source != "native",
                  payload={"outcome": receipt.outcome,
                           "error_code": receipt.error_code.value if receipt.error_code else None,
                           "late_by_s": max(0.0, (receipt.rendered_at - deadline).total_seconds()) if deadline else 0.0})
        return None

    def _effective_state(self) -> str:
        obs = self._observation
        now = self.clock.wall_now()
        if obs is None or obs.observed_at > now:
            return "unknown"
        until = obs.estimate.valid_until
        if (until is None and self.enforce_opportunities) or (until is not None and until <= now):
            return "unknown"
        return obs.estimate.state

    def _available(self) -> bool:
        return self._effective_state() == "available"

    def pump(self) -> None:
        """Process due domain work once, in stable order, without sleeping.

        Command-vs-deadline races are decided by caller submission order: the
        replay runner orders equal-time inputs by seq and explicitly pumps; Qt
        serializes its queued commands and timer ticks on the service thread.
        """
        now = self.clock.wall_now()
        mono = self.clock.monotonic_now()
        for task_id, (version, run_at, request_id) in sorted(
            list(self._rechecks.items()), key=lambda item: (item[1][1], item[0])
        ):
            if run_at <= now:
                self._rechecks.pop(task_id, None)
                view = self.get(task_id)
                if view and view.status == "registered" and view.version == version:
                    self.emit("recheck.fired", task_id=task_id, task_version=version,
                              request_id=request_id, payload={"run_at": run_at.isoformat()})
        for view in sorted(self._tasks.values(), key=lambda v: (v.draft.opens_at or now, v.task_id)):
            if view.active_reminder and view.draft.expires_at and now >= view.draft.expires_at:
                self._tasks[view.task_id] = view.model_copy(update={
                    "expired_at": view.draft.expires_at, "updated_at": now, "version": view.version+1,
                })
                self._invalidate_pending(view.task_id)
                self.emit("task.expired", task_id=view.task_id, task_version=view.version+1)
                continue
            if (not view.active_reminder or view.displayed_at is not None
                    or (view.task_id, view.occurrence) in self._external_owner):
                continue
            deadline = view.draft.hard_deadline
            existing = self._active_delivery.get(view.task_id)
            if existing:
                attempt = self._attempts[existing]
                if deadline and now >= deadline and attempt.command.reason in {"opportunity", "tool"}:
                    attempt.command = attempt.command.model_copy(update={
                        "reason": "deadline_fallback", "context_revision": None, "context_valid_until": None,
                        "expires_at": self._lease_end(view, now), "sound": "reminder",
                        "channel": "pet_card", "position_hint": None, "animation": None,
                    })
                    self._dispatch(attempt)
                    continue
                if self.enforce_opportunities and attempt.command.reason in {"opportunity", "tool"}:
                    if not self.auto_opportunity:
                        continue
                    obs = self._observation
                    if (self._focus_running or not self._available()
                            or (view.draft.window_close_at is not None and now >= view.draft.window_close_at)):
                        continue
                    if obs.context_revision != attempt.command.context_revision or (attempt.command.context_valid_until
                            and now >= attempt.command.context_valid_until):
                        if self.claim_opportunity(existing) is None:
                            continue
                        attempt.command = attempt.command.model_copy(update={
                            "context_revision": obs.context_revision, "context_valid_until": obs.estimate.valid_until,
                            "requested_at": now, "expires_at": self._lease_end(view, now),
                        })
                        self._dispatch(attempt)
                        continue
                elapsed = mono - attempt.last_attempt_mono
                if attempt.status == "pending" and elapsed >= self.receipt_timeout_s:
                    self.emit("delivery.timeout", task_id=view.task_id, task_version=view.version,
                              delivery_id=existing,
                              payload={"error_code": ErrorCode.TIMEOUT.value,
                                       "attempt": attempt.attempts})
                    self._dispatch(attempt)
                elif attempt.status == "failed" and elapsed >= self.retry_interval_s:
                    self._dispatch(attempt)
                continue
            if deadline is not None and now >= deadline:
                self._ensure_delivery(view, "hard_due" if view.draft.mode == "hard" else "deadline_fallback")
            elif view.draft.opens_at is not None and now < view.draft.opens_at:
                continue
            elif (self.auto_opportunity and self._available()
                  and (view.draft.window_close_at is None or now < view.draft.window_close_at)
                  and (not self.enforce_opportunities or self.claim_opportunity(self._delivery_id(view)) is not None)):
                self._ensure_delivery(view, "opportunity")
            else:
                context = self._observation.context_revision if self._observation else 1
                state = self._effective_state()
                key = (view.version, context, state)
                if self._last_deferred.get(view.task_id) != key:
                    self._last_deferred[view.task_id] = key
                    self.emit("delivery.deferred", task_id=view.task_id, task_version=view.version,
                              payload={"state": state})


FakeTaskService = MemoryTaskService
