"""Build the minimized Jev state and compare review evidence locally."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
import hashlib
import json
import math

from deskpet.observation_v2.vision import cloud_screen_text, multimodal_summary, validated_scene_elements


JEV_STATE_MAX_BYTES = 8192
_MAX_CANDIDATES = 8
_MAX_CAPTURES = 12
_MAX_GAPS = 8
_FOCUS_WINDOW = timedelta(minutes=2)
_KINDS = ("work", "study", "leisure", "other", "unknown")
_DURATIONS = (*_KINDS, "unobserved")
_STATES = ("working", "available", "away", "unknown")
_FRESHNESS = ("fresh", "expired", "context_changed", "no_data")
_PHASE_KINDS = ("focus", "short_break", "long_break")
_PHASE_STATES = ("ready", "running", "paused", "elapsed", "ended")
_SCREEN_CATEGORIES = ("document", "ide", "meeting", "video", "game", "browser")
_GOAL_SUMMARIES = {
    "写报告": "Write a report.", "撰写报告": "Write a report.", "write a report": "Write a report.",
    "write the report": "Write a report.", "writing a report": "Write a report.",
    "write report": "Write a report.", "报告写作": "Write a report.",
    "编程": "Write code.", "写代码": "Write code.", "coding": "Write code.", "write code": "Write code.",
    "学习": "Study.", "study": "Study.", "学习课程": "Study course material.",
    "复习课程": "Study course material.", "study course material": "Study course material.",
    "watch a lecture": "Study course material.", "看课程": "Study course material.",
}
_RESPONSES = {
    "none": "unanswered", "dismissed": "dismissed", "refused": "refused",
    "rejected": "refused", "accepted": "accepted", "acknowledged": "acknowledged", "snoozed": "snoozed",
}


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _choice(value, choices):
    return value if type(value) is str and value in choices else None


def _at(value):
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        stamp = datetime.fromisoformat(value)
        return stamp.astimezone(UTC) if stamp.utcoffset() is not None else None
    except (ValueError, OverflowError):
        return None


def _iso(value):
    return value.isoformat() if value is not None else None


def _number(value, upper):
    return type(value) in (int, float) and 0 <= value <= upper


def _bool(value):
    return value if type(value) is bool else None


def _stamp(value):
    return _iso(_at(value))


def _local_value(value):
    return value if type(value) is int or isinstance(value, str) and len(value) <= 512 else None


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


@dataclass(frozen=True)
class JevReview:
    """A successful System 2 review baseline suitable for local persistence."""

    as_of: str | None
    phase: str
    candidate: str
    activity: str
    evidence: str
    feedback: str
    fingerprint: str
    work_samples: int
    settings: str

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or value.keys() != cls.__dataclass_fields__.keys():
            return None
        if _at(value["as_of"]) is None or type(value["work_samples"]) is not int or not 0 <= value["work_samples"] <= 2:
            return None
        if any(not isinstance(value[key], str) or len(value[key]) != 64
               or any(char not in "0123456789abcdef" for char in value[key])
               for key in value.keys() - {"as_of", "work_samples"}):
            return None
        return cls(**value)


@dataclass(frozen=True)
class JevSnapshot:
    state: dict = field(repr=False)
    review: JevReview
    skip_reason: str | None
    local_defer_reason: str | None

    @property
    def fingerprint(self):
        return self.review.fingerprint


def _scene_summary(value):
    # Generate descriptions from registered public UI elements. Only accept
    # existing text when it exactly matches a known generic description.
    if elements := validated_scene_elements(value.get("scene_elements")):
        return multimodal_summary("unknown", sorted(elements))
    summary = value.get("summary")
    for kind in _SCREEN_CATEGORIES:
        generic = multimodal_summary(kind, ())
        if summary == generic:
            return generic
    return cloud_screen_text(summary, summary=True)


def _goal_summary(phase):
    goal = phase.get("goal", phase.get("title"))
    if not isinstance(goal, str) or len(goal) > 120:
        return None
    return _GOAL_SUMMARIES.get(" ".join(goal.casefold().split()).rstrip(".。"))


def _activity(context, as_of, *, share_text):
    last = _mapping(context.get("last_observed_activity"))
    interruptibility = _mapping(context.get("interruptibility"))
    status = _choice(interruptibility.get("status", context.get("current_status")), _FRESHNESS)
    current = _mapping(interruptibility.get("current", context.get("current"))) if status == "fresh" else {}
    scene_at = _at(last.get("scene_observed_at"))
    return {
        "kind": _choice(last.get("activity_kind"), _KINDS) or _choice(current.get("activity_kind"), _KINDS),
        "state": _choice(current.get("state"), _STATES),
        "summary": _scene_summary(last) if share_text else None,
        "scene_observed_at": _iso(scene_at) if as_of is not None and scene_at is not None and scene_at <= as_of else None,
        "relation": _choice(last.get("relation"), ("current", "previous")),
        "interruptibility_status": status,
    }


def _trend(context, as_of):
    window = _mapping(_mapping(context.get("activity_windows")).get("last_15m"))
    start, end = _at(window.get("from_at")), _at(window.get("to_at"))
    seconds = _mapping(window.get("seconds"))
    if not (start and end and end == as_of and end - start == timedelta(minutes=15)):
        return None
    if (any(not _number(seconds.get(kind), 900) for kind in _DURATIONS)
            or not math.isclose(sum(seconds[kind] for kind in _DURATIONS), 900, abs_tol=0.01)):
        return None
    return {"seconds": {kind: seconds[kind] for kind in _DURATIONS}}


def _capture_window(value, as_of, *, began=None):
    if as_of is None:
        return None
    start = as_of - _FOCUS_WINDOW
    scoped_start = max(start, began) if began else start
    value = _mapping(value)
    if (value.get("complete") is not True or value.get("truncated") is True or _at(value.get("from_at")) != start
            or _at(value.get("to_at")) != as_of):
        return None
    rows, gap_rows = value.get("visual_observations"), value.get("gaps")
    # Never send a partial list of supporting and contrary evidence.
    if (not isinstance(rows, list) or not isinstance(gap_rows, list)
            or len(rows) > 4 * _MAX_CAPTURES or len(gap_rows) > 4 * _MAX_GAPS):
        return None
    captures, capture_ids, gaps = {}, {}, []
    for row in map(_mapping, rows):
        observed = _at(row.get("observed_at"))
        kind, late = _choice(row.get("activity_kind"), _KINDS), _bool(row.get("late"))
        if observed is None or not start <= observed <= as_of or late is None or kind is None:
            return None
        if observed < scoped_start or (began and observed == began):
            continue
        fingerprint = {"observed_at": _iso(observed), "late": late,
                       "activity_kind": kind, "summary": _scene_summary(row)}
        identity = _local_value(row.get("capture_id"))
        if identity is not None and capture_ids.setdefault(identity, observed) != observed:
            return None
        if captures.setdefault(observed, fingerprint) != fingerprint or len(captures) > _MAX_CAPTURES:
            return None
    for row in map(_mapping, gap_rows):
        left, right = _at(row.get("from_at")), _at(row.get("to_at"))
        if left is None or right is None or not start <= left < right <= as_of:
            return None
        if right > scoped_start:
            gaps.append((max(left, scoped_start), right))
    if len(gaps) > _MAX_GAPS:
        return None
    return {"from_at": _iso(scoped_start), "to_at": _iso(as_of), "complete": True,
            "gaps": [{"from_at": _iso(left), "to_at": _iso(right)} for left, right in sorted(gaps)],
            "captures": [captures[stamp] for stamp in sorted(captures)]}


def _live_scene(activity):
    return activity["interruptibility_status"] == "fresh" and activity["relation"] == "current"


def _capture_evidence(window, activity, *, pause):
    if window is None:
        return None, (None, ())
    captures, gaps = window["captures"], window["gaps"]
    scene = _at(activity["scene_observed_at"])
    attached = bool(captures and _live_scene(activity) and captures[-1]["observed_at"] == _iso(scene)
                    and not captures[-1]["late"])
    work_samples = 0
    work_span = None
    continuous = False
    profiles = []
    spaced_runs = []
    earliest = max((gap["to_at"] for gap in gaps), default=window["from_at"])
    previous = None
    for record in reversed(captures):
        observed = record["observed_at"]
        profile = (record["activity_kind"], record["summary"], record["late"], observed >= earliest)
        if previous != profile:
            profiles.append(profile)
            spaced_runs.append(False)
        previous = profile
        if not attached or observed < earliest or record["late"]:
            continue
        since_scene = (scene - _at(observed)).total_seconds()
        if activity["kind"] in ("work", "study") and all(item[0] == activity["kind"] for item in profiles):
            work_samples = min(2, work_samples + 1)
            work_span = since_scene
            continuous = continuous or (work_span > 0 and bool(profile[1]) and len(profiles) == 1)
        if pause and since_scene >= 25:
            # Keep proof of a second spaced capture in the same run separate
            # from an older work or course capture across a scene boundary.
            spaced_runs[-1] = True
    work = {"sample_count": work_samples, "span_s": work_span, "continuous": continuous}
    return work, (attached, tuple(reversed(list(zip(profiles, spaced_runs, strict=True))))) if pause else (None, ())


def _phase_candidate(phase):
    if phase is None or phase["state"] in ("ready", "elapsed", "ended"):
        return "focus_start"
    if phase["kind"] == "focus":
        return "focus_pause" if phase["state"] == "running" else "focus_resume"
    if phase["state"] in ("running", "paused"):
        return "phase_adjust"
    return None


def _reminder_candidates(context, as_of, next_tick):
    items = context.get("eligible_items")
    if not isinstance(items, (list, tuple)) or as_of is None:
        return None, [], "candidate_unavailable"
    if context.get("candidate_items_truncated") is True or len(items) > _MAX_CANDIDATES:
        return None, [], "candidate_truncated"
    candidates, keys, reason = [], [], None
    for item in map(_mapping, items):
        rule, occurrence = _mapping(item.get("rule")), _mapping(item.get("occurrence"))
        identity = _local_value(item.get("occurrence_id", occurrence.get("id")))
        if not rule and (draft := _mapping(item.get("draft"))):
            # A scheduler without ItemService exposes its older TaskView shape.
            rule = {**draft, "remind_by": draft.get("remind_by") or draft.get("latest_first_delivery_at")}
            task_id, ordinal = item.get("task_id"), item.get("occurrence")
            if isinstance(task_id, str) and type(ordinal) is int:
                identity = _local_value(f"{task_id}:{ordinal}")
        version = _local_value(item.get("version", occurrence.get("version")))
        if not identity or version is None or not rule:
            return None, [], "candidate_unavailable"
        times = {}
        for key in ("remind_by", "expires_at", "window_close_at", "target_at", "urgency_until", "next_check_at"):
            raw = occurrence.get(key) or rule.get(key)
            if raw is not None and (at := _at(raw)) is None:
                return None, [], "candidate_unavailable"
            if raw is not None:
                times[key] = at
        latest = min((times[key] for key in ("remind_by", "expires_at") if key in times), default=None)
        seconds = (latest - as_of).total_seconds() if latest else None
        if next_tick is not None:
            upcoming_edge = any(0 <= (edge - as_of).total_seconds() <= next_tick for edge in times.values())
            promised_check = times.get("next_check_at")
            if upcoming_edge or (promised_check and (promised_check - as_of).total_seconds() <= next_tick):
                reason = "deadline_near"
        if seconds is not None and seconds <= 0:
            reason = "deadline_near"
        if rule.get("has_timing_context") or rule.get("timing_context"):
            # Jev never receives custom timing instructions. They may change
            # whether this opportunity is actionable before the next tick.
            reason = reason or "candidate_context_unavailable"
        candidates.append({"kind": "reminder", "seconds_to_latest": seconds})
        keys.append((identity, version, tuple((key, _iso(at)) for key, at in times.items()),
                     item.get("passive_available") is True))
    return candidates, keys, reason


def _matching_offer(offer, kind, phase):
    functions = {"focus_start": ("phase.start",), "focus_pause": ("phase.pause",),
                 "focus_resume": ("phase.resume",), "phase_adjust": ("phase.adjust", "phase.end")}
    if offer.get("function") not in functions.get(kind, ()):
        return False
    args = _mapping(offer.get("args"))
    if kind == "focus_start":
        return args.get("kind", "focus") == "focus" and not offer.get("subject_refs") and not args.get("previous_phase_id")
    phase_id = phase.get("id")
    return bool(phase_id and args.get("id") == phase_id and
                _mapping(offer.get("expected_versions")).get(f"phase:{phase_id}") == phase.get("version"))


def _same_scene(offer, last_review, activity_key, phase_key, *, exact):
    origin = offer.get("jev_origin_activity")
    if isinstance(origin, str) and len(origin) == 64 and _at(offer.get("jev_origin_review_at")) is not None:
        origin_phase = offer.get("jev_origin_phase")
        return origin == activity_key and (origin_phase is None or origin_phase == phase_key)
    if exact or last_review is None:
        return None
    reviewed_at, created_at = _at(last_review.as_of), _at(offer.get("created_at"))
    if reviewed_at is None or created_at is None or not timedelta(0) <= created_at - reviewed_at <= _FOCUS_WINDOW:
        return None
    return activity_key == last_review.activity and phase_key == last_review.phase


def _feedback(context, kind, phase, reminder_keys, as_of, last_review, activity_key, phase_key):
    pending_list = context.get("pending_proposals")
    contacts = context.get("recent_contacts")
    offers = _mapping(context.get("jev_feedback_proposals"))
    exact = context.get("jev_exact_feedback_provenance") is True
    pending, same, response, key = None, None, None, None
    if as_of is None:
        return pending, response, same, key, "feedback_unavailable"
    if isinstance(pending_list, (list, tuple)) and len(pending_list) <= _MAX_CANDIDATES:
        current = [offer for offer in map(_mapping, pending_list)
                   if (_matching_offer(offer, kind, phase) and offer.get("state") == "pending"
                       and (until := _at(offer.get("valid_until"))) is not None and until > as_of)]
        pending = bool(current)
        if current:
            offer = max(current, key=lambda item: _at(item.get("created_at")) or as_of)
            same = _same_scene(offer, last_review, activity_key, phase_key, exact=exact)
            key = (_local_value(offer.get("id")), _local_value(offer.get("version")),
                   not exact or offer.get("jev_displayed") is True)
    if not isinstance(contacts, (list, tuple)) or len(contacts) > 8:
        return pending, response, same, key, "feedback_unavailable"
    identities = {item[0] for item in reminder_keys}
    for receipt in sorted(map(_mapping, contacts), key=lambda row: _at(row.get("response_at") or row.get("at")) or as_of,
                          reverse=True):
        contact_at = _at(receipt.get("response_at") or receipt.get("at"))
        contact_kind = receipt.get("kind")
        feedback = _RESPONSES.get(_choice(receipt.get("response"), _RESPONSES))
        if feedback is None or contact_at is None or contact_at > as_of:
            continue
        if feedback == "unanswered" and receipt.get("exposure") not in ("painted", "reported_delivered"):
            continue
        if contact_kind == "reminder" and _local_value(receipt.get("occurrence_id")) in identities:
            if kind is None and len(identities) == 1:
                return pending, feedback, True, _local_value(receipt.get("delivery_id")), None
        if contact_kind == "proposal" and (ref := _local_value(receipt.get("subject_ref"))) and isinstance(ref, str):
            ref = ref.removeprefix("proposal:")
            offer = _mapping(offers.get(ref))
            if key and key[0] == ref:
                return pending, feedback, same, key, None
            if not pending and not identities and _matching_offer(offer, kind, phase):
                same = _same_scene(offer, last_review, activity_key, phase_key, exact=exact)
                return pending, feedback, same, ref, None
    return pending, response, same, key, "feedback_unavailable" if pending is None else None


def _changes(review, last_review):
    if last_review is None:
        return None
    changes = [reason for field, reason in (
        ("phase", "phase_changed"), ("settings", "settings_changed"), ("candidate", "candidates_changed"),
        ("activity", "activity_changed"), ("evidence", "evidence_changed"), ("feedback", "feedback_changed"),
    ) if getattr(review, field) != getattr(last_review, field)]
    if review.work_samples > last_review.work_samples:
        changes.append("work_evidence_accumulated")
    return changes


def project_jev_snapshot(context: dict, *, next_tick_in_s: float | None, share_text: bool,
                         last_review: JevReview | None = None) -> JevSnapshot:
    """Allowlist a pure-tick state and keep the comparison keys on the host."""
    as_of = _at(context.get("as_of"))
    next_tick = next_tick_in_s if _number(next_tick_in_s, 60) and next_tick_in_s > 0 else None
    pure_tick = context.get("trigger_reasons") in (["tick"], ("tick",))
    unlocked = _mapping(context.get("current_context")).get("screen_locked") is not True
    share_text = share_text is True and unlocked
    raw_phase, settings = _mapping(context.get("current_phase")), _mapping(context.get("enabled_settings"))
    auto_adjust = _bool(settings.get("auto_adjust"))
    pet_card = _bool(_mapping(context.get("display_capabilities")).get("pet_card"))
    began = _at(raw_phase.get("begin_at"))
    phase = ({"kind": _choice(raw_phase.get("kind"), _PHASE_KINDS),
              "state": _choice(raw_phase.get("state"), _PHASE_STATES), "begin_at": _iso(began),
              "goal_summary": _goal_summary(raw_phase) if unlocked else None} if raw_phase else None)
    activity = _activity(context if unlocked else {}, as_of, share_text=unlocked)
    activity_key = _digest((tuple((key, value) for key, value in activity.items() if key != "scene_observed_at"),
                            _local_value(context.get("jev_activity_scope")) if _live_scene(activity) else None))
    trend = _trend(context, as_of) if unlocked else None
    kind = _phase_candidate(phase) if auto_adjust and pet_card else None
    pause = kind == "focus_pause"
    needs_visual = kind in ("focus_start", "focus_pause")
    phase_window = (_capture_window(context.get("focus_evidence_2m"), as_of, began=began if pause else None)
                    if unlocked and needs_visual else None)
    work, evidence = _capture_evidence(phase_window, activity, pause=pause)
    work_samples = work["sample_count"] if work else 0
    candidates, keys, candidate_problem = _reminder_candidates(context, as_of, next_tick)
    if candidates is not None and kind:
        candidates.append({"kind": kind, "seconds_to_latest": None})
        if len(candidates) > _MAX_CANDIDATES:
            candidates, candidate_problem = None, "candidate_truncated"
    phase_key = _digest((phase, _local_value(raw_phase.get("id")), _local_value(raw_phase.get("version"))))
    pending, feedback, same, feedback_key, feedback_problem = _feedback(
        context, kind, raw_phase, keys, as_of, last_review, activity_key, phase_key,
    )
    painted_pending = pending is True and feedback == "unanswered" and isinstance(feedback_key, tuple) and feedback_key[2]
    settings_key = _digest((auto_adjust, pet_card, _local_value(settings.get("version"))))
    candidate_key = _digest((kind, sorted(keys, key=_json), candidate_problem in
                             ("candidate_unavailable", "candidate_truncated")))
    evidence_key = _digest((work_samples, work["continuous"] if work else None, evidence, trend is not None))
    feedback_key = _digest((pending, feedback, feedback_key, feedback_problem))
    fingerprint = _digest((phase_key, settings_key, candidate_key, activity_key, evidence_key, feedback_key))
    review = JevReview(_iso(as_of), phase_key, candidate_key, activity_key, evidence_key,
                       feedback_key, fingerprint, work_samples, settings_key)
    state = {
        "as_of": _iso(as_of), "wake_reasons": ["tick"] if pure_tick else [], "next_tick_in_s": next_tick,
        "phase": phase if phase is None or share_text else {**phase, "goal_summary": None},
        "auto_adjust": auto_adjust, "pet_card_available": pet_card,
        "activity": activity if share_text else {**activity, "summary": None},
        "last_15m": trend, "candidates": candidates,
        "pending_offer": pending, "last_feedback": feedback,
        "same_opportunity": same if candidates and len(candidates) == 1 else None,
        "last_system2_review_at": _stamp(last_review.as_of) if last_review else None,
        "changed_since_review": _changes(review, last_review),
    }
    if pause and unlocked:
        if phase_window is None:
            state["focus_2m"] = {"complete": False, "gaps": None, "captures": None}
        elif share_text:
            state["focus_2m"] = phase_window
        else:
            state["focus_2m"] = {**phase_window, "captures": [
                {**record, "summary": None} for record in phase_window["captures"]
            ]}
    if kind == "focus_start":
        state["work_evidence"] = work
    due = _at(raw_phase.get("phase_due_at"))
    skip = None
    if not pure_tick:
        skip = "non_periodic"
    elif not unlocked:
        skip = "observation_unavailable"
    elif as_of is None or next_tick is None:
        skip = "schedule_unavailable"
    elif auto_adjust is None or pet_card is None or "current_phase" not in context or (
        context["current_phase"] is not None and not raw_phase
    ) or (
        phase and (not phase["kind"] or not phase["state"] or began is None or began > as_of)
    ):
        skip = "phase_unavailable"
    elif candidate_problem:
        skip = candidate_problem
    elif due and phase["state"] == "running" and (due - as_of).total_seconds() <= next_tick:
        skip = "deadline_near"
    elif candidates and feedback_problem:
        skip = feedback_problem
    elif candidates and (trend is None or activity["interruptibility_status"] != "fresh" or activity["state"] is None):
        skip = "observation_unavailable"
    elif needs_visual and phase_window is None:
        skip = "evidence_incomplete"
    elif len(_json(state).encode()) > JEV_STATE_MAX_BYTES:
        state, skip = {}, "state_too_large"
    local_defer = None
    if skip is None:
        if candidates == []:
            local_defer = "no_candidate"
        elif painted_pending and same is True and not keys:
            local_defer = "pending_offer"
        elif last_review is not None and last_review.fingerprint == fingerprint:
            local_defer = "unchanged_snapshot"
    return JevSnapshot(state, review, skip, local_defer)


def capture_jev_review(context: dict, *, share_text: bool) -> JevReview:
    """Capture the evidence baseline from a completed System 2 review."""
    return project_jev_snapshot(context, next_tick_in_s=None, share_text=share_text).review
