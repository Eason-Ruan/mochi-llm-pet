"""Rollout switches and the maximum scope of an autonomous System 2 turn."""

import os


def _enabled(variable: str) -> bool:
    return os.environ.get(variable, "1").strip().lower() in {"1", "true", "on", "yes"}


def semantic_chat_enabled() -> bool:
    return _enabled("MOCHI_SYSTEM2_SEMANTIC_CHAT")


def semantic_background_enabled() -> bool:
    return _enabled("MOCHI_SYSTEM2_SEMANTIC_BACKGROUND")


def semantic_background_grants(context: dict) -> dict[str, dict]:
    """Grant candidate subjects and opt-in effects, never inferred availability."""
    from .intent import ref_id

    def occurrences(items):
        return list(dict.fromkeys(identity for item in items if (identity := ref_id(item.get("occurrence_id")))))

    eligible = occurrences(context.get("eligible_items", ()))
    reviewable = list(dict.fromkeys(eligible + occurrences(context.get("recheck_items", ()))))
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
    if eligible:
        grants["reminder.present"] = {"semantic_background": True, "occurrences": eligible}
    grants["observation.wait_fresh"] = {"semantic_background": True, "occurrences": reviewable,
                                        "focus": focus, "purposes": purposes}
    return grants
