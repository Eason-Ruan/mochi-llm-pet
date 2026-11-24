"""Bind admitted tools to their source and versioned domain objects."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .tool_schema import PROPOSABLE_ACTIONS, READ_TOOLS, RELATIVE_TIME_FIELDS, canonical_tool_name


_CHAT_ACTIONS = frozenset(PROPOSABLE_ACTIONS) | {
    "phase.skip_break", "phase.credit_sleep", "phase.credit_recovery", "phase.reassign_interval",
    "phase.set_details", "phase.review", "reminder.snooze", "reminder.ack", "proposal.resolve", "proposal.offer",
}
_PROTECTED_PHASE_FIELDS = {
    "previous_phase_id", "expected_previous_version", "habit_run_id", "expected_habit_run_version",
}


def ref_id(value: object) -> str | None:
    if isinstance(value, dict):
        value = value.get("id", value.get("task_id", value.get("subject_id")))
    if isinstance(value, str) and value:
        kind, split, identity = value.partition(":")
        return identity if split and kind in {"item", "reminder", "occurrence", "phase", "habit", "proposal", "interval", "recovery"} else value
    return None


def ref_kind(value: object) -> str | None:
    if isinstance(value, dict):
        return value.get("kind", value.get("type"))
    if isinstance(value, str) and ":" in value:
        return value.partition(":")[0]
    return None


def proposal_button_grants(button: dict) -> dict[str, dict]:
    """A structured response authorizes the choice made on that specific card."""
    return {"proposal.resolve": {"target": button["id"], "equal": button,
                                 "amendments": button.get("amendments", {})}}


def chat_grants(user: dict, context: dict) -> dict[str, dict]:
    """Offer chat capabilities with the current turn's object bindings."""
    issues = [item["id"] for item in context["items"] if item.get("type") == "issue"]
    grants = {name: {"semantic_chat": True} for name in _CHAT_ACTIONS}
    for name in ("phase.start", "phase.set_links", "phase.reassign_interval", "proposal.offer"):
        grants[name]["issues"] = issues
    if proposal_id := user.get("reply_to_proposal_id"):
        grants["proposal.resolve"]["target"] = proposal_id
        grants["proposal.resolve"]["proposal_version"] = user.get("reply_to_proposal_version")
    return grants


def materialize_chat_times(function: str, args: dict, requested_at: datetime, timezone: str) -> dict:
    """Resolve elapsed durations from the captured message instant, including across DST."""
    if function == "proposal.offer":
        action = canonical_tool_name(args["function"])
        return {**args, "args": materialize_chat_times(action, args["args"], requested_at, timezone)}

    def resolve(values, *, rule=False):
        values = dict(values)
        for offset, field in RELATIVE_TIME_FIELDS.items():
            if offset not in values:
                continue
            delta = values.pop(offset)
            if values.get(field) is not None or delta is None:
                raise ValueError(f"provide exactly one of {offset} and {field}")
            values[field] = (requested_at.astimezone(UTC) + timedelta(seconds=delta)).isoformat()
        for key in ("patch", "reminder_rule", "rule", "amendments"):
            if isinstance(values.get(key), dict):
                values[key] = resolve(values[key], rule=key in {"reminder_rule", "rule"})
        if rule:
            if provided_zone := values.get("timezone"):
                try:
                    ZoneInfo(provided_zone)
                except (ValueError, ZoneInfoNotFoundError):
                    raise ValueError("invalid reminder timezone") from None
        return values

    result = resolve(args)
    if (function == "item.create" and result.get("type") == "reminder"
            and not any((result.get("reminder_rule") or {}).get(key) for key in (
                "remind_after", "remind_by", "target_at", "expires_at", "timing_context",
            ))):
        raise ValueError("a new reminder needs a requested time")
    if function == "item.create" and (rule := result.get("reminder_rule")):
        rule.setdefault("timezone", timezone)
    return result


def _allows_chat(binding, function, args, versions):
    def known(kind, value):
        return isinstance(value, str) and f"{kind}:{value}" in versions

    if function in {"item.update", "item.set_status", "item.archive"}:
        return known("item", args.get("id"))
    if function == "reminder.configure":
        return known("item", args.get("item_id"))
    if function in {"reminder.snooze", "reminder.ack"}:
        return known("occurrence", args.get("occurrence_id"))
    if function in {"habit.update", "habit.archive"}:
        return known("habit", args.get("id"))
    if function == "settings.update":
        scope, target = args["scope"], args.get("id")
        if scope == "default":
            return target is None and known("settings", "default")
        return known("phase" if scope == "current" else "habit", target)
    if function == "proposal.resolve":
        target = args.get("id")
        return (known("proposal", target) and binding.get("target", target) == target
                and (binding.get("proposal_version") is None
                     or args.get("expected_version", versions[f"proposal:{target}"]) == binding["proposal_version"]))
    if function == "proposal.offer":
        name = canonical_tool_name(args["function"])
        return name in PROPOSABLE_ACTIONS and _allows_chat(binding, name, args["args"], versions)
    if function.startswith("phase."):
        if function != "phase.start" and (target := args.get("id")) and not known("phase", target):
            return False
        if function in {"phase.credit_sleep", "phase.credit_recovery"}:
            return known("recovery", args.get("interval_id") or args.get("recovery_interval_id"))
        if function == "phase.reassign_interval" and not known("interval", args.get("interval_id")):
            return False
        if any(args.get(field) is not None for field in _PROTECTED_PHASE_FIELDS):
            return False
        if args.get("habit_id") and not known("habit", args["habit_id"]):
            return False
        links = set(args.get("issue_links") or ()) | {
            args[field] for field in ("primary_issue_id", "target_issue_id") if args.get(field)
        }
        return links.issubset(binding.get("issues", ()))
    return function in {"habit.create", "item.create"}


def _allows_background(binding, function, args):
    if function == "decision.recheck":
        return args.get("purpose") in binding["purposes"]
    if function == "reminder.present":
        return all(ref_id(identity) in binding["occurrences"] for identity in args["occurrence_ids"])
    if function == "reminder.recheck":
        occurrence = ref_id(args["occurrence_id"])
        return occurrence in binding["occurrences"] and (
            occurrence not in binding.get("timing_context_unavailable", ())
            or all(args.get(key) is None for key in ("window_open_at", "window_close_at"))
        )
    if function == "observation.wait_fresh":
        occurrence = args.get("occurrence_id")
        purpose = args["purpose"]
        if binding.get("semantic_background"):
            return purpose in binding["purposes"] and (
                ref_id(occurrence) in binding["occurrences"] if purpose == "reminder_recheck" else occurrence is None
            )
        if occurrence:
            return ref_id(occurrence) in binding["occurrences"]
        return binding.get("focus", False) and purpose == "focus_suggestion"
    if function == "proposal.offer":
        offered, payload = canonical_tool_name(args["function"]), args["args"]
        if offered != "phase.start":
            return (offered in binding["functions"] and bool(binding["target"])
                    and ref_id(payload.get("id")) == binding["target"])
        if any(payload.get(key) for key in ("habit_id", "primary_issue_id", "issue_links", "habit_run_id")):
            return False
        previous_id, previous_version = payload.get("previous_phase_id"), payload.get("expected_previous_version")
        if not binding.get("semantic_background"):
            return previous_id is None
        if previous_id is None and previous_version is None:
            return True
        predecessor = binding.get("predecessor") or {}
        return (previous_id is not None and ref_id(previous_id) == ref_id(predecessor.get("id"))
                and type(previous_version) is int and previous_version == predecessor.get("version")
                and (payload.get("kind", "focus") == "focus") != (predecessor.get("kind") == "focus"))
    return function == "pet.present"


def allows(grants: dict[str, dict], function: str, args: dict, *, versions=None) -> bool:
    if function in READ_TOOLS:
        return True
    if function not in grants:
        return False
    binding = grants[function]
    if binding.get("semantic_chat"):
        return _allows_chat(binding, function, args, versions or {})
    if function == "proposal.resolve":
        return (
            ref_id(args.get("id")) == binding["target"]
            and all(args.get(key) == value for key, value in binding["equal"].items() if key != "amendments")
            and (args.get("amendments") or {}) == binding["amendments"]
        )
    return _allows_background(binding, function, args)
