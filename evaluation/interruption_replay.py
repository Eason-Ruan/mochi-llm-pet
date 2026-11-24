"""Synthetic notification transport and metrics for the System 2 shadow replay."""

from collections import Counter, defaultdict
from datetime import datetime


class SimulatedNotifications:
    """Keep a native inventory on the fake clock without talking to the OS."""

    available = True
    sound_available = True
    can_schedule = True
    can_verify_removal = True
    can_verify_early_transfer = True
    can_respond = True
    status = "simulated"

    def __init__(self, clock):
        self.clock = clock
        self.pending = {}
        self.delivered = {}

    def refresh(self, *, request=False):
        pass

    def submit(self, identifier, due_at, *, sound, complete, snooze=True):
        if due_at is None:
            self.delivered[identifier] = self.clock.wall_now()
        else:
            self.pending[identifier] = due_at
        complete("submitted" if due_at is None else "scheduled", None)

    def inventory(self, complete):
        now = self.clock.wall_now()
        for identifier, due in tuple(self.pending.items()):
            if due <= now:
                self.delivered[identifier] = due
                self.pending.pop(identifier)
        complete(set(self.pending), set(self.delivered))

    def remove_pending(self, identifier):
        self.pending.pop(identifier, None)
        return True


def score_interruption_case(case, deadlines):
    """Count observable synthetic receipts and versioned actions within one run."""
    primaries = defaultdict(list)
    sounds = Counter()
    completed = set()
    blocked = {}
    refused = set()
    stale = Counter()
    quiet_checks = []
    repeated_batches = 0
    for step in case["steps"]:
        for outcome in step["invalidated_occurrences"]:
            blocked[outcome["occurrence"]] = outcome["reason"]
        for outcome in step["prompt_responses"]:
            occurrence = outcome["occurrence"]
            if outcome["status"] == "applied":
                if outcome["action"] == "reject":
                    refused.add(outcome["subject"])
                if outcome["action"] in {"ack", "accept", "snooze"}:
                    first = primaries[occurrence][:1]
                    if first and first[0].get("actionable") and first[0]["id"] == outcome["delivery_id"]:
                        completed.add(occurrence)
                if outcome["action"] == "snooze":
                    blocked[occurrence] = "snoozed"
        for display in step["simulated_deliveries"]:
            if display["status"] != "shown":
                continue
            occurrence = display.get("occurrence")
            if not occurrence:
                continue
            if display.get("primary"):
                primaries[occurrence].append(display)
            if occurrence in blocked:
                stale[blocked[occurrence]] += 1
            elif display.get("subject") in refused:
                stale["refused"] += 1
        for request in step["synthetic_sound_requests"]:
            sounds[request["occurrence"]] += 1
        if step.get("same_batch"):
            repeated_batches += max(0, len(step["synthetic_sound_requests"]) - 1)
        if step.get("quiet_check") is not None:
            quiet_checks.append(step["quiet_check"])
    hard_timing = {"expected": len(deadlines), "on_time": 0, "early": 0, "late": 0, "missing": 0}
    for occurrence, target in deadlines.items():
        if not primaries[occurrence]:
            hard_timing["missing"] += 1
        else:
            shown = datetime.fromisoformat(primaries[occurrence][0]["at"])
            due = datetime.fromisoformat(target)
            hard_timing["on_time" if shown == due else "early" if shown < due else "late"] += 1
    return {
        "hard_timing": hard_timing,
        "duplicate_primary_displays": sum(max(0, len(displays) - 1) for displays in primaries.values()),
        "sound_requests": sum(sounds.values()),
        "repeat_sound_requests": sum(count - 1 for count in sounds.values()),
        "extra_same_batch_sound_requests": repeated_batches,
        "first_prompt": {
            "completed": len(completed),
            "actionable": sum(bool(displays and displays[0].get("actionable")) for displays in primaries.values()),
        },
        "stale_displays": {reason: stale[reason] for reason in ("refused", "snoozed", "expired")},
        "quiet_checks": len(quiet_checks),
        "quiet_failures": quiet_checks.count(False),
    }


def summarize_interruptions(cases):
    scores = [case["interruption"] for case in cases if "interruption" in case]
    hard = Counter()
    completion = Counter()
    stale = Counter()
    for score in scores:
        hard.update(score["hard_timing"])
        completion.update(score["first_prompt"])
        stale.update(score["stale_displays"])
    return {
        "scenarios": len(scores),
        "hard_timing": {field: hard[field] for field in ("expected", "on_time", "early", "late", "missing")},
        **{key: sum(score[key] for score in scores) for key in (
            "duplicate_primary_displays", "sound_requests", "repeat_sound_requests",
            "extra_same_batch_sound_requests", "quiet_checks", "quiet_failures",
        )},
        "first_prompt": {"completed": completion["completed"], "actionable": completion["actionable"],
                         "completion_fraction": (completion["completed"] / completion["actionable"]
                                                 if completion["actionable"] else None)},
        "stale_displays": {reason: stale[reason] for reason in ("refused", "snoozed", "expired")},
        "audio_heard": None,
        "real_user_completion": None,
    }
