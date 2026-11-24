"""Privacy rules and the maximum scope of an autonomous System 2 turn."""

import os


def semantic_background_enabled() -> bool:
    return os.environ.get("MOCHI_SYSTEM2_SEMANTIC_BACKGROUND", "1").strip().lower() in {"1", "true", "on", "yes"}


def text_sharing_allowed(settings, route_snapshot, lock_state):
    route = route_snapshot.route
    return (not settings.observation_paused and lock_state is not True
            and (route is None or not route.is_cloud or route_snapshot.permission))


def timing_context_unavailable(item):
    rule = item.get("rule") or {}
    return rule.get("has_timing_context") is True and not rule.get("timing_context")


def _occurrences(items):
    from .intent import ref_id

    return list(dict.fromkeys(identity for item in items if (identity := ref_id(item.get("occurrence_id")))))


def semantic_background_grants(context: dict) -> dict[str, dict]:
    """Grant candidate subjects and opt-in effects, never inferred availability."""
    from .intent import ref_id

    candidates = context.get("eligible_items", ())
    rechecks = context.get("recheck_items", ())
    eligible = _occurrences(item for item in candidates if not timing_context_unavailable(item))
    reviewable = _occurrences((*candidates, *rechecks))
    withheld = _occurrences(item for item in (*candidates, *rechecks) if timing_context_unavailable(item))
    focus = context["enabled_settings"].get("auto_adjust", False)
    purposes = ["pet_presentation"]
    grants = {"pet.present": {"semantic_background": True}}
    if focus:
        purposes.append("focus_suggestion")
        current = context.get("current_phase") or {}
        target = ref_id(current.get("id"))
        phase_actions = ["phase.start"]
        if target:
            phase_actions += ["phase.pause", "phase.resume", "phase.adjust", "phase.end"]
        grants["proposal.offer"] = {"background": True, "semantic_background": True,
                                    "target": target, "functions": phase_actions,
                                    "predecessor": context.get("phase_predecessor")}
    grants["decision.recheck"] = {"semantic_background": True, "purposes": list(purposes)}
    if reviewable:
        purposes.append("reminder_recheck")
        grants["reminder.recheck"] = {"semantic_background": True, "occurrences": reviewable}
        if withheld:
            grants["reminder.recheck"]["timing_context_unavailable"] = withheld
    if eligible:
        grants["reminder.present"] = {"semantic_background": True, "occurrences": eligible}
    grants["observation.wait_fresh"] = {"semantic_background": True, "occurrences": reviewable,
                                        "focus": focus, "purposes": purposes}
    return grants


def background_grants(context: dict) -> dict[str, dict]:
    """Apply the configured autonomous policy to the current candidates."""
    from .intent import ref_id

    if semantic_background_enabled():
        return semantic_background_grants(context)
    candidates = context["eligible_items"]
    rechecks = context.get("recheck_items", ())
    eligible = _occurrences(item for item in candidates if not timing_context_unavailable(item))
    reviewable = _occurrences((*candidates, *rechecks))
    withheld = _occurrences(item for item in (*candidates, *rechecks) if timing_context_unavailable(item))
    focus = context["enabled_settings"].get("auto_adjust", False)
    state = (context.get("current") or {}).get("state") if context["current_status"] == "fresh" else None
    grants = {}
    if state == "available" and context.get("opportunity_id"):
        grants["pet.present"] = {}
    if focus or reviewable:
        grants["observation.wait_fresh"] = {"occurrences": reviewable, "focus": focus}
    if eligible:
        grants["reminder.present"] = {"occurrences": eligible}
    if reviewable:
        grants["reminder.recheck"] = {"occurrences": reviewable}
        if withheld:
            grants["reminder.recheck"]["timing_context_unavailable"] = withheld
    if focus and state in {"working", "available"}:
        phase = ref_id((context.get("current_phase") or {}).get("id"))
        grants["proposal.offer"] = {"target": phase, "background": True,
                                    "functions": ["phase.start", "phase.pause", "phase.resume", "phase.adjust"]}
    return grants
