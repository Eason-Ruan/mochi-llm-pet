"""Versioned, local views and commands for the shared focus timer."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, model_validator

from deskpet.contracts import ErrorCode
from deskpet.contracts.models import Model

PhaseKind = Literal["focus", "short_break", "long_break"]
PhaseSource = Literal["manual", "chat", "proposal", "habit"]
Duration = Annotated[float, Field(gt=0, le=86400, strict=True)]


class PhaseView(Model):
    id: str
    version: int = Field(ge=1)
    kind: PhaseKind
    title: str
    description: str | None = None
    state: Literal["ready", "running", "paused", "elapsed", "ended"]
    planned_duration_s: Duration
    remaining_s: float = Field(ge=0)
    actual_focus_s: float = Field(ge=0)
    created_at: datetime
    begin_at: datetime
    ended_at: datetime | None = None
    phase_due_at: datetime | None = None
    source: PhaseSource
    issue_links: tuple[str, ...] = ()
    primary_issue_id: str | None = None
    habit_id: str | None = None
    applied_habit_version: int | None = None
    habit_run_id: str | None = None
    confirmed_outcomes: tuple[str, ...] = ()
    next_steps: tuple[str, ...] = ()


class PhaseCommand(Model):
    command_id: str = Field(min_length=1)
    phase_id: str = Field(min_length=1)
    expected_version: int = Field(default=0, ge=0)
    kind: Literal["start", "pause", "resume", "adjust", "end", "skip_break",
                  "set_links", "set_details", "review", "credit_sleep", "credit_recovery", "reassign_interval"]
    source: PhaseSource = "manual"
    payload: dict = Field(default_factory=dict)


class PhaseResult(Model):
    command_id: str
    status: Literal["applied", "rejected", "duplicate"]
    view: PhaseView | None = None
    error_code: ErrorCode | None = None
    original_status: Literal["applied", "rejected"] | None = None


class FocusInterval(Model):
    id: str
    phase_id: str
    version: int = Field(default=1, ge=1)
    primary_issue_id: str | None = None
    started_at: datetime
    ended_at: datetime | None = None
    seconds: float = Field(default=0, ge=0)
    origin: Literal["manual", "chat", "proposal", "habit", "sleep", "recovery"]


class RecoveryInterval(Model):
    id: str
    phase_id: str
    version: int = Field(default=1, ge=1)
    origin: Literal["sleep", "recovery"]
    started_at: datetime
    observed_until_at: datetime | None = None
    trusted_end_at: datetime | None = None
    credited_seconds: float | None = None


class HabitRun(Model):
    id: str
    version: int = Field(default=1, ge=1)
    habit_id: str
    applied_version: int = Field(ge=1)
    state: Literal["active", "paused", "ended"] = "active"
    focus_s_since_long_break: float = Field(default=0, ge=0)
    completed_phases_since_long_break: int = Field(default=0, ge=0)
    next_break_kind: Literal["short_break", "long_break"] = "short_break"


class LinksInput(Model):
    issue_links: tuple[str, ...] = ()
    primary_issue_id: str | None = None

    @model_validator(mode="after")
    def valid_links(self):
        if any(not link.strip() for link in self.issue_links):
            raise ValueError("issue links cannot be blank")
        if len(set(self.issue_links)) != len(self.issue_links):
            raise ValueError("issue links must be unique")
        if self.primary_issue_id is not None and self.primary_issue_id not in self.issue_links:
            raise ValueError("primary issue must be linked")
        return self


class StartInput(LinksInput):
    kind: PhaseKind = "focus"
    planned_duration_s: Duration | None = None
    title: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=4000)
    previous_phase_id: str | None = None
    expected_previous_version: int | None = Field(default=None, ge=1)
    habit_id: str | None = None
    expected_habit_version: int | None = Field(default=None, ge=1)
    habit_run_id: str | None = None
    expected_habit_run_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def paired_predecessor(self):
        if (self.previous_phase_id is None) != (self.expected_previous_version is None):
            raise ValueError("predecessor and expected version are both required for a transition")
        if (self.habit_id is None) != (self.expected_habit_version is None):
            raise ValueError("habit and expected version are both required")
        if (self.habit_run_id is None) != (self.expected_habit_run_version is None):
            raise ValueError("habit run and expected version are both required")
        if self.kind != "focus" and (self.description or self.issue_links):
            raise ValueError("a break cannot have work notes or linked issues")
        return self


class AdjustInput(Model):
    total_s: Duration | None = None
    add_s: Duration | None = None

    @model_validator(mode="after")
    def one_adjustment(self):
        if (self.total_s is None) == (self.add_s is None):
            raise ValueError("specify either total_s or add_s")
        return self


class DetailsInput(Model):
    title: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=4000)


class ReviewInput(Model):
    confirmed_outcomes: tuple[str, ...] = ()
    next_steps: tuple[str, ...] = ()
    count_as_completed: bool = False


class CreditRecoveryInput(Model):
    recovery_interval_id: str
    expected_recovery_version: int = Field(ge=1)
    started_at: datetime
    ended_at: datetime


class CreditSleepInput(Model):
    interval_id: str
    seconds: Duration


class ReassignInput(Model):
    interval_id: str
    expected_interval_version: int = Field(ge=1)
    target_issue_id: str | None = None
