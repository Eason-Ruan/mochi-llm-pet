"""Rollout switches and the maximum scope of an autonomous System 2 turn."""

import os


def _enabled(variable: str) -> bool:
    return os.environ.get(variable, "1").strip().lower() in {"1", "true", "on", "yes"}


def semantic_chat_enabled() -> bool:
    return _enabled("MOCHI_SYSTEM2_SEMANTIC_CHAT")


def semantic_background_enabled() -> bool:
    return _enabled("MOCHI_SYSTEM2_SEMANTIC_BACKGROUND")


def text_sharing_allowed(settings, route_snapshot, lock_state):
    route = route_snapshot.route
    return (not settings.observation_paused and lock_state is not True
            and (route is None or not route.is_cloud or route_snapshot.permission))


def timing_context_unavailable(item):
    rule = item.get("rule") or {}
    return rule.get("has_timing_context") is True and not rule.get("timing_context")


def semantic_background_grants(context: dict) -> dict[str, dict]:
    """Grant candidate subjects and opt-in effects, never inferred availability."""
    from .intent import ref_id

    def occurrences(items):
        return list(dict.fromkeys(identity for item in items if (identity := ref_id(item.get("occurrence_id")))))

    candidates = context.get("eligible_items", ())
    rechecks = context.get("recheck_items", ())
    eligible = occurrences(item for item in candidates if not timing_context_unavailable(item))
    reviewable = occurrences((*candidates, *rechecks))
    withheld = occurrences(item for item in (*candidates, *rechecks) if timing_context_unavailable(item))
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
