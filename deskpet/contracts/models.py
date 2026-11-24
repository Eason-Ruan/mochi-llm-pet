"""M0 v1 interchange models. This module has no GUI, OS or network dependencies."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = 1


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    @field_validator("*", mode="after")
    @classmethod
    def aware_datetimes(cls, value):
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("timestamps must have a UTC offset")
            return value.astimezone(timezone.utc)
        return value


class ErrorCode(str, Enum):
    STALE_TASK = "STALE_TASK"
    STALE_CONTEXT = "STALE_CONTEXT"
    STALE_SURFACE = "STALE_SURFACE"
    EXPIRED_REQUEST = "EXPIRED_REQUEST"
    CANCELLED = "CANCELLED"
    OUTSIDE_WINDOW = "OUTSIDE_WINDOW"
    DUPLICATE = "DUPLICATE"
    INVALID_TRANSITION = "INVALID_TRANSITION"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    NOT_FOUND = "NOT_FOUND"
    UNKNOWN_TOOL = "UNKNOWN_TOOL"
    TIMEOUT = "TIMEOUT"
    FAILED = "FAILED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    UNSUPPORTED = "UNSUPPORTED"


class RawSignals(Model):
    """Timestamped measurements, never a conclusion about the user's state."""
    sample_id: str
    sampler_id: str = "unspecified"
    sampled_at: datetime
    valid_until: datetime
    source: Literal["scripted", "live"] = "scripted"
    capabilities: dict[str, bool] = Field(default_factory=dict)
    application: str | None = None
    idle_s: float | None = Field(default=None, ge=0)
    input_active: bool | None = None
    cursor_position: tuple[float, float] | None = None
    window_count: int | None = Field(default=None, ge=0)
    errors: tuple[str, ...] = ()

    @model_validator(mode="after")
    def lifetime(self):
        if self.valid_until <= self.sampled_at:
            raise ValueError("raw signal validity must follow sampled_at")
        return self


class StateEvidence(Model):
    ref: str
    signal: str
    value: Any = None
    sampled_at: datetime
    valid_until: datetime
    source: str


class UserCorrection(Model):
    origin: Literal["user", "scripted", "automation"] = "user"
    correction_id: str
    state: Literal["working", "available", "away", "unknown"]
    reason: str = Field(min_length=1, max_length=500)
    created_at: datetime
    valid_until: datetime

    @model_validator(mode="after")
    def lifetime(self):
        if self.valid_until <= self.created_at:
            raise ValueError("correction validity must follow created_at")
        return self


class UserStateEstimate(Model):
    estimator_id: str = "unspecified"
    based_on_sample_id: str | None = None
    evidence_details: tuple[StateEvidence, ...] = ()
    state: Literal["working", "available", "away", "unknown"] = "unknown"
    evidence: tuple[str, ...] = ()
    source: Literal["rule", "scripted", "user", "model"] = "scripted"
    valid_until: datetime | None = None


class Observation(Model):
    raw: RawSignals | None = None
    correction: UserCorrection | None = None
    context_revision: int = Field(default=1, ge=1)
    observed_at: datetime
    estimate: UserStateEstimate = Field(default_factory=UserStateEstimate)
    capabilities: dict[str, bool] = Field(default_factory=dict)
    application: str | None = None
    idle_s: float | None = Field(default=None, ge=0)
    source: Literal["scripted", "live"] = "scripted"


class ObservationFrame(Model):
    """Inspectable pipeline state, including the estimate before user correction."""
    observation: Observation
    raw: RawSignals | None = None
    raw_status: Literal["fresh", "expired", "missing", "future", "error"]
    estimated: UserStateEstimate
    correction: UserCorrection | None = None
    correction_status: Literal["none", "active", "expired", "cleared"] = "none"
    sampling_name: str
    estimator_name: str
    error: str | None = None


class TaskDraft(Model):
    content: str = Field(min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=4000)
    mode: Literal["hard", "soft"] = "soft"
    timezone: str = "UTC"
    earliest_at: datetime | None = None
    latest_first_delivery_at: datetime | None = None
    item_type: Literal["reminder", "issue"] = "reminder"
    reminder_enabled: bool = True
    remind_after: datetime | None = None
    target_at: datetime | None = None
    remind_by: datetime | None = None
    expires_at: datetime | None = None
    due_at: datetime | None = None
    window_open_at: datetime | None = None
    window_close_at: datetime | None = None
    window_computed_at: datetime | None = None
    urgency_until: datetime | None = None
    urgency_reason: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("timezone")
    @classmethod
    def iana_timezone(cls, value):
        # Windows without a tzdata package still accepts the names used by
        # legacy desktop records. Every stored time already includes an offset.
        if value in {"UTC", "Asia/Shanghai"}:
            return value
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must be an IANA time zone") from exc
        return value

    @property
    def explicit_start_at(self) -> datetime | None:
        return max((value for value in (self.earliest_at, self.remind_after) if value is not None), default=None)

    @property
    def opens_at(self) -> datetime | None:
        planned_start = self.window_open_at or self.earliest_at or self.target_at
        edges = [value for value in (planned_start, self.remind_after) if value is not None]
        return max(edges, default=None)

    @property
    def hard_deadline(self) -> datetime | None:
        return self.remind_by or self.latest_first_delivery_at

    @property
    def opportunity_close_at(self) -> datetime | None:
        return min((value for value in (self.window_close_at, self.hard_deadline, self.expires_at)
                    if value is not None), default=None)

    @model_validator(mode="after")
    def window_valid(self):
        start, deadline = self.opens_at, self.hard_deadline
        if not self.model_fields_set.intersection({"mode", "earliest_at", "latest_first_delivery_at", "item_type",
                                                   "reminder_enabled", "remind_after", "target_at", "remind_by",
                                                   "expires_at", "due_at"}):
            raise ValueError("specify reminder timing or an untimed item type")
        if start is not None and deadline is not None and deadline < start:
            raise ValueError("remind_by precedes the opening time")
        if self.reminder_enabled and self.mode == "hard" and (deadline is None or deadline != start):
            raise ValueError("exact reminders require one confirmed time")
        if self.window_close_at is not None and start is not None and self.window_close_at < start:
            raise ValueError("window_close_at precedes the opening time")
        explicit_start = self.explicit_start_at
        if self.target_at is not None and ((explicit_start and self.target_at < explicit_start)
                                           or (deadline and self.target_at > deadline)):
            raise ValueError("target_at is outside the explicit reminder bounds")
        if self.expires_at is not None and any(
            value is not None and value >= self.expires_at for value in (start, self.target_at, deadline)
        ):
            raise ValueError("expires_at must follow the other reminder times")
        if self.item_type == "reminder" and (self.due_at is not None or not self.reminder_enabled):
            raise ValueError("only issues have work deadlines or can omit reminders")
        if bool(self.urgency_reason) != (self.urgency_until is not None):
            raise ValueError("time-sensitive reminders require a reason and time limit")
        if self.urgency_until is not None and start is not None and self.urgency_until <= start:
            raise ValueError("the time-sensitive interval must extend past the opening time")
        return self


IssueStatus = Literal["created", "in_progress", "paused", "completed", "cancelled"]


class TaskView(Model):
    task_id: str
    version: int = Field(ge=1)
    draft: TaskDraft
    status: Literal["draft", "registered", "completed", "cancelled"] = "draft"
    occurrence: int = Field(default=1, ge=1)
    confirmed_at: datetime | None = None
    displayed_at: datetime | None = None
    acknowledged_at: datetime | None = None
    issue_status: IssueStatus | None = None
    rule_version: int = Field(default=1, ge=1)
    occurrence_rule_version: int | None = Field(default=None, ge=1)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_snoozed_at: datetime | None = None
    archived_at: datetime | None = None
    expired_at: datetime | None = None

    @field_validator("issue_status", mode="before")
    @classmethod
    def legacy_issue_status(cls, value):
        return "paused" if value == "issue_hangging" else value

    @property
    def active_reminder(self) -> bool:
        return (self.status == "registered" and self.draft.reminder_enabled
                and self.acknowledged_at is None and self.expired_at is None and self.archived_at is None)

    @property
    def occurrence_id(self) -> str:
        return f"{self.task_id}:{self.occurrence}"

    @property
    def effective_issue_status(self) -> IssueStatus:
        return self.issue_status or (self.status if self.status in {"completed", "cancelled"} else "created")


class TaskCommand(Model):
    command_id: str
    kind: Literal["create", "create_registered", "confirm", "edit", "configure", "estimate_window", "snooze", "ack", "cancel", "complete",
                  "set_status", "archive"]
    task_id: str
    expected_version: int = Field(default=0, ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)


class CommandResult(Model):
    command_id: str
    status: Literal["applied", "rejected", "duplicate"]
    actual_version: int | None = None
    error_code: ErrorCode | None = None
    view: TaskView | None = None
    message: str = ""


class DecisionEnvelope(Model):
    request_id: str
    task_id: str
    task_version: int = Field(ge=1)
    context_revision: int = Field(ge=1)
    created_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def expiry_valid(self):
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must follow created_at")
        return self


class ToolCall(Model):
    call_id: str
    tool_name: str
    args: dict[str, Any] = Field(default_factory=dict)
    envelope: DecisionEnvelope
    idempotency_key: str


class ProposedPlan(Model):
    envelope: DecisionEnvelope
    calls: tuple[ToolCall, ...] = ()

    @model_validator(mode="after")
    def matching_envelopes(self):
        if any(c.envelope != self.envelope for c in self.calls):
            raise ValueError("all calls must carry the plan envelope")
        return self


class ToolResult(Model):
    call_id: str
    status: Literal["succeeded", "pending", "rejected", "failed", "timeout", "duplicate"]
    error_code: ErrorCode | None = None
    receipt_id: str | None = None
    state_version: int | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)


class DisplayCommand(Model):
    delivery_id: str
    task_id: str
    task_version: int
    occurrence: int
    content: str
    requested_at: datetime
    expires_at: datetime
    reason: Literal["hard_due", "opportunity", "deadline_fallback", "tool", "retry"]
    sound: Literal["reminder", "phase_end"] | None = None
    context_revision: int | None = None
    context_valid_until: datetime | None = None
    position_hint: Literal["stay", "home", "screen_edge"] | None = None
    animation: Literal["idle", "look_up", "wave", "approach"] | None = None
    channel: Literal["pet_card", "pet_badge", "os_notification"] = "pet_card"


class DisplayReceipt(Model):
    delivery_id: str
    task_id: str
    task_version: int
    rendered_at: datetime
    outcome: Literal["displayed", "failed", "rejected"]
    source: Literal["simulated", "native", "offscreen"]
    error_code: ErrorCode | None = None


class UiComponent(Model):
    component_id: str
    type: Literal["text", "card", "text_input", "select", "button", "list"]
    label: str = ""
    action_id: str | None = None
    options: tuple[str, ...] = ()
    data_key: str | None = None


class UiSpec(Model):
    schema_version: Literal[1] = 1
    surface_id: str
    surface_revision: int = Field(ge=1)
    task_id: str | None = None
    task_version: int | None = None
    components: tuple[UiComponent, ...] = ()
    data: dict[str, Any] = Field(default_factory=dict)


class UiAction(Model):
    event_id: str
    action_id: Literal["reminder.create", "reminder.confirm", "reminder.done", "reminder.snooze",
                       "reminder.edit", "reminder.cancel", "reminder.ack", "reminder.archive",
                       "reminder.set_status", "state.override", "session.start",
                       "session.pause", "session.resume", "session.end", "session.adjust",
                       "session.start_break", "session.skip_break", "session.set_links", "session.set_details",
                       "session.review",
                       "session.credit_recovery", "session.credit_sleep", "session.reassign_interval",
                       "state.clear_override"]
    task_id: str | None = None
    expected_task_version: int | None = None
    surface_revision: int = Field(ge=1)
    payload: dict[str, Any] = Field(default_factory=dict)


class WorkSession(Model):
    session_id: str
    version: int = Field(ge=1)
    task_id: str | None = None
    goal: str
    state: Literal["focusing", "paused", "break_due", "ended"] = "focusing"
    remaining_s: float = Field(ge=0)
    phase_due_at: datetime | None = None
    confirmed_outcomes: tuple[str, ...] = ()
    next_steps: tuple[str, ...] = ()


class SessionCommand(Model):
    command_id: str
    kind: Literal["start", "pause", "resume", "end"]
    session_id: str
    expected_version: int = Field(default=0, ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)


class SessionResult(Model):
    command_id: str
    status: Literal["applied", "rejected", "duplicate"]
    view: WorkSession | None = None
    error_code: ErrorCode | None = None


class TraceEvent(Model):
    schema_version: Literal[1] = 1
    seq: int = Field(ge=1)
    ts: datetime
    monotonic_s: float
    run_id: str
    scenario_id: str
    kind: str
    source: str
    simulated: bool
    clock: Literal["virtual", "wall"]
    task_id: str | None = None
    task_version: int | None = None
    request_id: str | None = None
    call_id: str | None = None
    delivery_id: str | None = None
    session_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ScenarioEvent(Model):
    seq: int = Field(ge=1)
    at_s: float = Field(ge=0)
    kind: Literal["command", "observation", "advance", "receipt", "session", "tool", "fault", "decision", "raw_sample", "correction", "clear_correction"]
    payload: dict[str, Any] = Field(default_factory=dict)


class Scenario(Model):
    schema_version: Literal[1] = 1
    scenario_id: str
    description: str = ""
    initial_time: datetime
    events: tuple[ScenarioEvent, ...]
    expected_outcomes: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def ordered(self):
        order = [(e.at_s, e.seq) for e in self.events]
        if order != sorted(order) or len({e.seq for e in self.events}) != len(order):
            raise ValueError("scenario events must have unique seq and be ordered by (at_s, seq)")
        return self


class RecoveryFixture(Model):
    """Portable recovery boundary for B; not an SQL schema or M0 restore implementation."""
    schema_version: Literal[1] = 1
    captured_at: datetime
    tasks: tuple[TaskView, ...]
    pending_deliveries: tuple[DisplayCommand, ...] = ()
    job_examples: tuple[dict[str, Any], ...] = ()
    expected_after_restart: dict[str, Any]
