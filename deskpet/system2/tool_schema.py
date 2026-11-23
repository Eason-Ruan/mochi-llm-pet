"""Domain tool names and shared validation for DSH and trusted local commands."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StrictBool, StrictFloat, StrictInt, create_model

from deskpet.contracts.models import Model
from deskpet.settings import HabitBehavior
from deskpet.work_sessions.models import AdjustInput, ReviewInput

Identifier = Annotated[str, Field(min_length=1, max_length=500)]
Version = Annotated[StrictInt, Field(ge=0)]
Duration = Annotated[StrictFloat, Field(gt=0, le=86400)]
Seconds = Annotated[StrictFloat, Field(ge=0, le=86400)]
Scope = Literal["default", "habit", "current"]
PhaseKind = Literal["focus", "short_break", "long_break"]
Status = Literal["created", "in_progress", "paused", "completed", "cancelled", "finished", "expired", "postponing"]
VersionFields = {"expected_version": (Version | None, None)}
Identified = {"id": (Identifier, ...)}
IdentifiedWrite = {**Identified, **VersionFields}
Links = {
    "issue_links": (list[Identifier], []),
    "primary_issue_id": (Identifier | None, None),
}
NameDescription = {
    "title": (Annotated[str, Field(max_length=500)] | None, None),
    "description": (Annotated[str, Field(max_length=4000)] | None, None),
}


class ReminderRuleInput(Model):
    timezone: str = Field(default_factory=lambda: "UTC", description=(
        "IANA time zone when explicitly specified. Omit to use the user's time zone for a new rule "
        "or keep the existing zone for an update. Send timestamps with explicit UTC offsets."
    ))
    remind_after: AwareDatetime | None = None
    target_at: AwareDatetime | None = None
    remind_by: AwareDatetime | None = None
    expires_at: AwareDatetime | None = None
    window_open_at: AwareDatetime | None = None
    window_close_at: AwareDatetime | None = None
    urgency_until: AwareDatetime | None = None
    urgency_reason: str | None = None


class ItemFilter(Model):
    type: Literal["reminder", "issue"] | None = None
    status: Status | None = None
    include_archived: StrictBool = False
    active_only: StrictBool = False


class ItemPatch(Model):
    title: Identifier | None = None
    description: str | None = None
    due_at: AwareDatetime | None = None
    reminder_rule: ReminderRuleInput | None = None


class HabitPatch(Model):
    title: Identifier | None = None
    status: Literal["enabled", "disabled", "archived"] | None = None
    behavior: dict | None = None


class SettingsPatch(Model):
    default_focus_s: Annotated[StrictInt, Field(ge=1, le=86400)] | None = None
    default_break_s: Annotated[StrictInt, Field(ge=1, le=86400)] | None = None
    auto_adjust: StrictBool | None = None
    habits_enabled: StrictBool | None = None
    link_issues: StrictBool | None = None
    allow_proactive_movement: StrictBool | None = None
    reduce_motion: StrictBool | None = None
    reminder_sound: StrictBool | None = None
    phase_end_sound: StrictBool | None = None
    allow_system_notifications: StrictBool | None = None
    lock_screen_detail: StrictBool | None = None
    input_mode: Literal["voice", "typing"] | None = None


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    canonical_name: str
    description: str
    input_schema: dict


_MODELS = {}
_DEFINITIONS = []


def _register(name, summary, **fields):
    wire_name = name.replace(".", "_")
    model = create_model(wire_name, __base__=Model, **fields)
    _MODELS[name] = model
    _DEFINITIONS.append(ToolDefinition(wire_name, name, summary, model.model_json_schema()))


_register("context.read", "Read decision evidence. Optional history_start and history_end select a time range "
          "within the last hour of as_of. Request capture health for observation diagnostics. "
          "Historical evidence never authorizes an early reminder.",
          as_of=(AwareDatetime | None, None), history_start=(AwareDatetime | None, None),
          history_end=(AwareDatetime | None, None), include_capture_health=(bool | None, None))
_register("item.get", "Read one reminder or issue by ID.", **Identified)
_register("item.list", "List reminders or issues.", filter=(ItemFilter | None, None))
_register("item.create", "Create a reminder or issue. An issue deadline does not create a notification.",
          type=(Literal["reminder", "issue"], ...), title=(Identifier, ...),
          description=(str | None, None), due_at=(AwareDatetime | None, None),
          reminder_rule=(ReminderRuleInput | None, None), **VersionFields)
_register("item.update", "Change the specified fields on an item. Omit unchanged timing fields.",
          patch=(ItemPatch, ...), **IdentifiedWrite)
_register("item.set_status", "Set issue status for an explicit user request.",
          status=(Status, ...), **IdentifiedWrite)
_register("item.archive", "Archive the item and cancel its pending reminders.", **IdentifiedWrite)
_register("reminder.configure", "Replace the item's reminder rule with the specified absolute times.",
          item_id=(Identifier, ...), rule=(ReminderRuleInput, ...), **VersionFields)
_register("reminder.snooze", "Reschedule the current occurrence from the time of the user's action.",
          occurrence_id=(Identifier, ...), delay_s=(Duration, ...),
          mode=(Literal["flexible_target", "no_earlier", "exact"], ...),
          window_before_s=(Seconds | None, None), window_after_s=(Seconds | None, None), **VersionFields)
_register("reminder.ack", "Acknowledge a reminder. An issue keeps its existing status.",
          occurrence_id=(Identifier, ...), **VersionFields)
_register("reminder.present", "Submit one eligible reminder for an authorized early presentation.",
          occurrence_ids=(Annotated[list[Identifier], Field(min_length=1)], ...),
          presentation=(str | dict, ...), reason_code=(Identifier, ...), **VersionFields)
_register("reminder.recheck", "Recheck an occurrence later or estimate its soft window when missing or elapsed.",
          occurrence_id=(Identifier, ...), run_at=(AwareDatetime | None, None),
          on_next_fresh=(StrictBool, False), window_open_at=(AwareDatetime | None, None),
          window_close_at=(AwareDatetime | None, None), **VersionFields)
_register("observation.wait_fresh", "Request one wake for the next fresh Observation.",
          purpose=(Literal["focus_suggestion", "reminder_recheck"], ...),
          after_cursor=(Version, ...), valid_until=(AwareDatetime, ...),
          occurrence_id=(Identifier | None, None), **VersionFields)

_register("proposal.offer", "Ask the user before making an inferred domain change.",
          function=(Identifier, ...), args=(dict, ...), subject_refs=(list[Identifier], []),
          display_text=(Identifier, ...))
_register("proposal.resolve", "Accept, decline, or cancel the proposal answered by the user.",
          answer=(Literal["accept", "reject", "cancel"], ...), amendments=(dict | None, None),
          **IdentifiedWrite)
_register("phase.get", "Read a phase by ID, or read the current phase if omitted.", id=(Identifier | None, None))
_register("phase.start", "Start a focus or break phase. A blank focus title uses 专注.",
          kind=(PhaseKind, "focus"), planned_duration_s=(Duration | None, None),
          habit_id=(Identifier | None, None), previous_phase_id=(Identifier | None, None),
          expected_previous_version=(Version | None, None), expected_habit_version=(Version | None, None),
          habit_run_id=(Identifier | None, None), expected_habit_run_version=(Version | None, None),
          **NameDescription, **Links, **VersionFields)
for action, description in {
    "pause": "Pause the current phase.", "resume": "Resume the paused phase.",
    "skip_break": "End the current break early.",
}.items():
    _register(f"phase.{action}", description, **IdentifiedWrite)
_register("phase.adjust", "Adjust the phase using exactly one of a new total or a duration to add.",
          total_s=(Duration | None, None), add_s=(Duration | None, None), **IdentifiedWrite)
_register("phase.end", "End the phase, with optional user-confirmed review.",
          review=(ReviewInput | None, None), count_as_completed=(StrictBool | None, None), **IdentifiedWrite)
_register("phase.review", "Save the user's review of an ended phase.", review=(ReviewInput, ...), **IdentifiedWrite)
_register("phase.set_links", "Change assignment of future focus time for the active phase.",
          **Links, **IdentifiedWrite)
_register("phase.set_details", "Change the active phase's title or description.", **NameDescription, **IdentifiedWrite)
_register("phase.credit_sleep", "Credit part of a verified sleep interval at the user's request.",
          id=(Identifier | None, None), interval_id=(Identifier, ...), seconds=(Duration, ...), **VersionFields)
_register("phase.credit_recovery", "Credit a user-confirmed bounded recovery interval.",
          id=(Identifier | None, None), recovery_interval_id=(Identifier, ...),
          expected_recovery_version=(Version | None, None), started_at=(AwareDatetime, ...),
          ended_at=(AwareDatetime, ...), **VersionFields)
_register("phase.reassign_interval", "Change the issue assigned to a sealed interval at the user's request.",
          id=(Identifier | None, None), interval_id=(Identifier, ...), expected_interval_version=(Version, ...),
          target_issue_id=(Identifier | None, None), **VersionFields)

_register("habit.get", "Read the specified named focus habit.", **Identified)
_register("habit.list", "Read the named focus habits.")
_register("habit.create", "Create a named focus habit.", title=(Identifier, ...),
          behavior=(HabitBehavior, ...), status=(Literal["enabled", "disabled"], "enabled"), **VersionFields)
_register("habit.update", "Change a named habit's future behavior or status.", patch=(HabitPatch, ...), **IdentifiedWrite)
_register("habit.archive", "Archive a named habit and stop its future automatic transitions.", **IdentifiedWrite)
_register("settings.get", "Read default preferences, a named habit, or the current phase.",
          scope=(Scope, ...), id=(Identifier | None, None))
_register("settings.update", "Change default preferences, a named habit, or the current phase.",
          scope=(Scope, ...), id=(Identifier | None, None),
          patch=(SettingsPatch | HabitPatch | AdjustInput, ...), **VersionFields)
_register("pet.present", "Request a temporary desktop pet presentation.",
          intent=(Identifier, ...), position_hint=(Literal["stay", "home", "screen_edge"] | None, None),
          animation=(Literal["idle", "look_up", "wave", "approach"] | None, None), sound=(StrictBool | None, None))
_register("command.get_result", "Look up a previous command receipt before attempting a fresh command.",
          command_id=(Identifier, ...))

tool_definitions = tuple(_DEFINITIONS)
_WIRE_NAMES = {definition.name: definition.canonical_name for definition in tool_definitions}
READ_TOOLS = frozenset({"context.read", "item.get", "item.list", "phase.get", "habit.get",
                        "habit.list", "settings.get", "command.get_result"})


def canonical_tool_name(name):
    return _WIRE_NAMES.get(name, name)


def validate_tool_args(name, args):
    return _MODELS[name].model_validate(args).model_dump(mode="json", exclude_unset=True)
