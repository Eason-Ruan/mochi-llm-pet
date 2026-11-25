"""Build text-only System 2 inputs from registered business and Observation fields."""

from __future__ import annotations

from datetime import datetime, timedelta
import math

from deskpet.contracts.models import REMINDER_TIMING_CONTEXT_LIMIT
from deskpet.observation_v2.context import (
    CAPTURE_HEALTH_REASONS, CAPTURE_HEALTH_STATUSES, CAPTURE_HEALTH_TIMES, VISUAL_OBSERVATION_LIMIT,
)
from deskpet.observation_v2.documents import document_source_allowed, sanitize_document_name
from deskpet.observation_v2.state import _KINDS, _REASONS
from deskpet.observation_v2.vision import validated_scene_elements, validated_visible_changes

from .contact_history import CONTACT_LIMIT

_CONTEXT_FIELDS = {
    "as_of", "timezone", "trigger_reasons", "items", "habits", "eligible_items", "current_phase",
    "enabled_settings", "pending_proposals", "display_capabilities", "recent_decisions",
    "opportunity_id", "publication_cursor", "recheck_items", "selected_phase", "selected_interval",
    "selected_recovery", "recent_user_actions", "candidate_items_truncated", "candidate_habits_truncated",
    "phase_elapsed", "phase_predecessor",
}
_STATES = {"working", "available", "away", "unknown"}
_CURRENT_STATUSES = {"fresh", "expired", "context_changed", "no_data"}
_PRIVATE_FIELDS = {
    "summary", "summaries", "summary_samples", "screenshot", "screenshots", "image",
    "images", "image_url", "image_data", "capture", "captured_frame", "frame",
    "frames", "screen", "recent_screen_results", "current_screen", "screen_result",
    "screen_summary", "screen_text", "visible_text", "visible_text_redacted", "transcript",
    "transcription", "raw_input", "raw_keys", "keystrokes", "window_title",
    "window_title_redacted", "app_name", "domain", "foreground", "current_foreground",
    "previous_foreground", "credential_ref", "api_key", "authorization", "notes", "note",
    "description", "timing_context", "review", "confirmed_outcomes", "next_steps", "password", "access_token",
    "document_name", "document_source", "document_local_sources", "document_cloud_sources", "document_change",
    "scene_elements", "visible_changes", "visual_observations", "current_context", "last_observed_activity",
    "history_1h", "capture_health", "activity_windows", "focus_evidence_2m", "representative_summaries",
}


def _clean(value):
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()
                if isinstance(key, str) and key.casefold() not in _PRIVATE_FIELDS
                and not key.casefold().startswith("observation_")}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    return None


def _stamp(value) -> str | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        stamp = datetime.fromisoformat(value)
        return value if stamp.utcoffset() is not None else None
    except ValueError:
        return None


def _count(value) -> int:
    return value if type(value) is int and value >= 0 else 0


def _enum(value, choices, default):
    return value if isinstance(value, str) and value in choices else default


def _summary(value) -> str | None:
    return value[:512] if isinstance(value, str) else None


def _sample(value: dict, *, current: bool, share: bool) -> dict:
    reasons = value.get("reason_codes")
    result = {
        "observed_at": _stamp(value.get("observed_at")),
        "activity_kind": _enum(value.get("activity_kind"), _KINDS, "unknown"),
        "state": _enum(value.get("state"), _STATES, "unknown"),
        "reason_codes": [reason for reason in reasons if isinstance(reason, str) and reason in _REASONS]
        if isinstance(reasons, list) else [],
    }
    if current:
        result["valid_until"] = _stamp(value.get("valid_until"))
        result["context_revision"] = _count(value.get("context_revision")) or None
    if share:
        result["summary"] = _summary(value.get("summary"))
    return result


def _items(value, limit: int) -> list[dict]:
    return [item for item in value[:limit] if isinstance(item, dict)] if isinstance(value, list) else []


def _segment(value: dict, *, share: bool) -> dict:
    result = {
        "first_observed_at": _stamp(value.get("first_observed_at")),
        "last_observed_at": _stamp(value.get("last_observed_at")),
        "activity_kind": _enum(value.get("activity_kind"), _KINDS, "unknown"),
        "state": _enum(value.get("state"), _STATES, "unknown"),
        "sample_count": _count(value.get("sample_count")),
    }
    if share:
        result["summary"] = _summary(value.get("summary"))
        result["summary_samples"] = [
            {"observed_at": _stamp(sample.get("observed_at")), "summary": _summary(sample.get("summary"))}
            for sample in _items(value.get("summary_samples"), 2)
        ]
    return result


def _document_detail(value: dict, *, share: bool, document_sources: tuple[str, ...]) -> dict:
    result = {"activity_kind": _enum(value.get("activity_kind"), _KINDS, "unknown")}
    if share:
        result.update(summary=_summary(value.get("summary")),
                      scene_elements=list(validated_scene_elements(value.get("scene_elements"))),
                      visible_changes=list(validated_visible_changes(value.get("visible_changes"))))
        for key in ("summary_observed_at", "scene_observed_at", "change_observed_at"):
            if stamp := _stamp(value.get(key)):
                result[key] = stamp
    source = value.get("document_source")
    if share and isinstance(source, str) and document_source_allowed(source, document_sources):
        if name := sanitize_document_name(value.get("document_name"), source):
            result["document_name"] = name
            change = value.get("document_change")
            if (isinstance(change, dict) and change.get("type") == "unsaved_changes_appeared"
                    and (observed_at := _stamp(change.get("observed_at")))):
                result["document_change"] = {"type": "unsaved_changes_appeared", "observed_at": observed_at}
    return result


def _activity_segment(value: dict, *, share: bool, document_sources: tuple[str, ...]) -> dict:
    result = {**_segment(value, share=False), **_document_detail(value, share=share, document_sources=document_sources)}
    if share:
        captures = {}
        for item in _items(value.get("visual_observations"), VISUAL_OBSERVATION_LIMIT):
            if (stamp := _stamp(item.get("observed_at"))) and stamp not in captures:
                captures[stamp] = {
                    "observed_at": stamp,
                    "activity_kind": _enum(item.get("activity_kind"), _KINDS, "unknown"),
                    "summary": _summary(item.get("summary")),
                    "scene_elements": list(validated_scene_elements(item.get("scene_elements"))),
                }
        if captures:
            result["visual_observations"] = list(captures.values())
    return result


def _gap_projection(history: dict) -> list[dict]:
    return [
        {"last_observed_at": _stamp(item.get("last_observed_at")),
         "next_observed_at": _stamp(item.get("next_observed_at"))}
        for item in _items(history.get("gaps"), 8)
    ]


_WINDOW_SECONDS = {"last_15m": 900, "last_60m": 3600}
_DURATION_KINDS = (*_KINDS, "unobserved")


def window_within_range(requested: str, at: datetime, start: datetime | None, end: datetime | None) -> bool:
    duration = _WINDOW_SECONDS["last_60m"] if requested == "both" else _WINDOW_SECONDS[requested]
    return (start is None or start <= at - timedelta(seconds=duration)) and (end is None or end >= at)


def _requested_windows(request: dict, view: dict) -> tuple[str, ...]:
    key = request.get("activity_window")
    if not isinstance(key, str) or key not in (*_WINDOW_SECONDS, "both"):
        return ()
    if request.get("history_start") or request.get("history_end"):
        try:
            at = datetime.fromisoformat(view["as_of"])
            start = datetime.fromisoformat(request["history_start"]) if request.get("history_start") else None
            end = datetime.fromisoformat(request["history_end"]) if request.get("history_end") else None
            if at.utcoffset() is None or not window_within_range(key, at, start, end):
                return ()
        except (KeyError, ValueError, TypeError, OverflowError):
            return ()
    return tuple(_WINDOW_SECONDS) if key == "both" else (key,)


def _activity_windows(windows: dict, requested: tuple[str, ...]) -> dict:
    result = {}
    for key in requested:
        window = windows.get(key)
        if not isinstance(window, dict):
            continue
        duration = _WINDOW_SECONDS[key]
        start, end = _stamp(window.get("from_at")), _stamp(window.get("to_at"))
        if not start or not end or (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() != duration:
            continue
        seconds = window.get("seconds")
        if not isinstance(seconds, dict) or any(
            type(seconds.get(kind)) not in (int, float) or not 0 <= seconds[kind] <= duration
            for kind in _DURATION_KINDS
        ):
            continue
        if not math.isclose(sum(seconds[k] for k in _DURATION_KINDS), duration, abs_tol=0.01):
            continue
        result[key] = {
            "from_at": start, "to_at": end, "duration_s": duration,
            "seconds": {kind: seconds[kind] for kind in _DURATION_KINDS},
            "percent": {kind: round(100 * seconds[kind] / duration, 2) for kind in _DURATION_KINDS},
            "representative_summaries": [
                {"observed_at": stamp, "activity_kind": _enum(item.get("activity_kind"), _KINDS, "unknown"),
                 "summary": summary}
                for item in _items(window.get("representative_summaries"), 2)
                if (summary := _summary(item.get("summary"))) and (stamp := _stamp(item.get("observed_at")))
            ],
        }
    return result


def _focus_evidence(value: dict) -> dict:
    start, end = _stamp(value.get("from_at")), _stamp(value.get("to_at"))
    captures, gaps = [], []
    complete = value.get("complete") is True and bool(start and end)
    for key in ("visual_observations", "gaps"):
        entries = value.get(key)
        if not isinstance(entries, list):
            complete = False
            continue
        for item in entries:
            if not isinstance(item, dict):
                complete = False
            elif key == "gaps":
                left, right = _stamp(item.get("from_at")), _stamp(item.get("to_at"))
                if left and right:
                    gaps.append({"from_at": left, "to_at": right})
                else:
                    complete = False
            elif (stamp := _stamp(item.get("observed_at"))) and type(item.get("late")) is bool:
                captures.append({"observed_at": stamp, "late": item["late"],
                                 "activity_kind": _enum(item.get("activity_kind"), _KINDS, "unknown"),
                                 "summary": _summary(item.get("summary")),
                                 "scene_elements": list(validated_scene_elements(item.get("scene_elements")))})
            else:
                complete = False
    return {"from_at": start, "to_at": end, "complete": complete,
            "gaps": gaps, "visual_observations": captures}


def _contains_name(value, names: set[str]):
    return any(name in value.casefold() for name in names) if isinstance(value, str) else False


def _remove_document_text(value, names: set[str]):
    # Document names only travel in their structured field, regardless of recipient.
    if not isinstance(value, dict) or not names:
        return
    for key, data in value.items():
        if key in {"summary", "foreground_app"} and _contains_name(data, names):
            value[key] = None
        elif isinstance(data, list):
            for entry in data:
                _remove_document_text(entry, names)
        elif isinstance(data, dict):
            _remove_document_text(data, names)


def observed_document_names(value) -> set[str]:
    if isinstance(value, dict):
        return {item.casefold() for key, item in value.items() if key == "document_name" and isinstance(item, str) and item} | {
            name for item in value.values() for name in observed_document_names(item)
        }
    if isinstance(value, (tuple, list)):
        return {name for item in value for name in observed_document_names(item)}
    return set()


def _contact(value: dict) -> dict:
    channels = {"pet_card", "pet_badge", "os_notification", "qt", "reminder_menu", "proposal_panel"}
    exposures = {"painted", "reported_delivered", "unknown", "none"}
    identifiers = ("delivery_id", "subject_ref", "occurrence_id")
    result = {key: value[key] for key in identifiers if isinstance(value.get(key), str)}
    for key, choices in (
        ("kind", {"reminder", "proposal", "pet", "phase_end"}),
        ("channel", channels),
        ("primary_channel", channels | {"none"}),
        ("exposure", exposures),
        ("passive_entry", {"reminder_menu", "proposal_panel", "none"}),
        ("sound_heard", {"unknown", "none"}),
        ("response", {"none", "dismissed", "refused", "snoozed", "acknowledged", "accepted", "rejected"}),
    ):
        if choice := _enum(value.get(key), choices, None):
            result[key] = choice
    if type(value.get("sound_requested")) is bool:
        result["sound_requested"] = value["sound_requested"]
    if "surfaces" in value:
        result["surfaces"] = [
            {"channel": item["channel"], "exposure": item["exposure"], "active": item["active"]}
            for item in _items(value["surfaces"], len(channels))
            if _enum(item.get("channel"), channels, None) and _enum(item.get("exposure"), exposures, None)
            and type(item.get("active")) is bool
        ]
    for key in ("at", "response_at"):
        if at := _stamp(value.get(key)):
            result[key] = at
    return result


def include_candidate_timing_context(value: dict, result: dict, *, share: bool) -> None:
    for field in ("eligible_items", "recheck_items"):
        candidates, projected = value.get(field), result.get(field)
        if not isinstance(candidates, (list, tuple)) or not isinstance(projected, list):
            continue
        for original, sanitized in zip(candidates, projected):
            rule = original.get("rule") if isinstance(original, dict) else None
            target = sanitized.get("rule") if isinstance(sanitized, dict) else None
            if isinstance(rule, dict) and isinstance(target, dict) and isinstance(text := rule.get("timing_context"), str):
                if text.strip() and len(text) <= REMINDER_TIMING_CONTEXT_LIMIT:
                    target["has_timing_context"] = True
                    if share:
                        target["timing_context"] = text.strip()


def project_context(value: dict, *, share: bool, document_sources: tuple[str, ...] = (),
                    windows: tuple[str, ...] = (), focus_evidence: bool = False) -> dict:
    result = {key: _clean(data) for key, data in value.items() if key in _CONTEXT_FIELDS}
    include_candidate_timing_context(value, result, share=share)
    if "recent_contacts" in value:
        result["recent_contacts"] = [_contact(item) for item in _items(value["recent_contacts"], CONTACT_LIMIT)]
    current = value.get("current")
    history = value.get("history")
    result["current_status"] = _enum(value.get("current_status"), _CURRENT_STATUSES, "no_data")
    result["current"] = _sample(current, current=True, share=share) if isinstance(current, dict) else None
    if isinstance(history, dict):
        result["history"] = {
            "recent": [_sample(item, current=False, share=share) for item in _items(history.get("recent"), 3)],
            "segments": [_segment(item, share=share) for item in _items(history.get("segments"), 8)],
            "gaps": _gap_projection(history),
        }
        stats = history.get("stats")
        if isinstance(stats, dict):
            result["history"]["stats"] = {key: _count(stats.get(key)) for key in (
                "total_samples", "gap_count", "omitted_segment_count",
            )}
            counts = stats.get("state_counts")
            if isinstance(counts, dict):
                result["history"]["stats"]["state_counts"] = {
                    state: _count(count) for state, count in counts.items() if state in _STATES
                }
    if isinstance(signals := value.get("current_context"), dict):
        idle = signals.get("idle_s")
        locked = signals.get("screen_locked")
        foreground = signals.get("foreground_app")
        result["current_context"] = {
            "observed_at": _stamp(signals.get("observed_at")),
            "screen_locked": locked if type(locked) is bool else None,
            "idle_s": idle if type(idle) in (float, int) and 0 <= idle and math.isfinite(idle) else None,
            "foreground_app": _summary(foreground) if share or foreground == "Mochi" else None,
        }
    if isinstance(last := value.get("last_observed_activity"), dict):
        result["last_observed_activity"] = {
            **_document_detail(last, share=share, document_sources=document_sources),
            "observed_at": _stamp(last.get("observed_at")),
            "relation": _enum(last.get("relation"), {"current", "previous"}, "previous"),
        }
    elif "last_observed_activity" in value:
        result["last_observed_activity"] = None
    if isinstance(interrupt := value.get("interruptibility"), dict):
        status = _enum(interrupt.get("status"), _CURRENT_STATUSES, "no_data")
        sample = interrupt.get("current")
        result["interruptibility"] = {"status": status, "current": _sample(sample, current=True, share=False)
                                      if status == "fresh" and isinstance(sample, dict) else None}
    if isinstance(hour := value.get("history_1h"), dict):
        hour_segments = _items(hour.get("segments"), 8)
        if any("state" not in segment for segment in hour_segments) and "history" in result:
            # Fresh desktop segments supersede the old estimator-only timeline.
            for key in ("recent", "segments", "gaps"):
                result["history"].pop(key, None)
        result["history_1h"] = {
            "from_at": _stamp(hour.get("from_at")), "to_at": _stamp(hour.get("to_at")),
            "segments": [_activity_segment(item, share=share, document_sources=document_sources)
                         for item in hour_segments],
            "gaps": _gap_projection(hour),
        }
        if isinstance(stats := hour.get("stats"), dict):
            result["history_1h"]["stats"] = {key: _count(stats.get(key)) for key in (
                "total_samples", "gap_count", "omitted_segment_count",
            )}
    if "capture_health" in value:
        status = value["capture_health"]
        result["capture_health"] = None
        if share and isinstance(status, dict):
            result["capture_health"] = {
                "status": _enum(status.get("status"), CAPTURE_HEALTH_STATUSES, None),
                "observed_at": _stamp(status.get("observed_at")),
                "reason": _enum(status.get("reason"), CAPTURE_HEALTH_REASONS, None),
                **{key: _stamp(status.get(key)) for key in CAPTURE_HEALTH_TIMES},
            }
    if share and windows and isinstance(value.get("activity_windows"), dict):
        result["activity_windows"] = _activity_windows(value["activity_windows"], windows)
    if share and focus_evidence and isinstance(value.get("focus_evidence_2m"), dict):
        result["focus_evidence_2m"] = _focus_evidence(value["focus_evidence_2m"])
    _remove_document_text(result, observed_document_names(value))
    return result


def _initial_fields(selection) -> set[str]:
    fields = {"as_of", "timezone", "current", "current_status", "current_context", "last_observed_activity",
              "interruptibility", "current_phase", "enabled_settings", "pending_proposals",
              "display_capabilities", "recent_contacts", "recent_decisions", "publication_cursor"}
    if "chat" in selection.domains:
        fields.update({"recent_user_actions", "items", "habits", "selected_phase", "selected_interval",
                       "selected_recovery", "candidate_items_truncated", "candidate_habits_truncated"})
    if "reminder" in selection.domains:
        fields.update({"eligible_items", "recheck_items", "opportunity_id"})
    if "phase_elapsed" in selection.domains or "focus" in selection.domains or "resume" in selection.domains:
        fields.update({"phase_elapsed", "phase_predecessor"})
    if selection.activity_15m:
        fields.add("activity_windows")
    if selection.focus_evidence_2m:
        fields.add("focus_evidence_2m")
    return fields


def _deduplicate_wake_data(data: dict) -> None:
    event = data.get("event") or {}
    context = data["context"]
    elapsed = [w["phase_elapsed"] for w in event.get("wakes", ()) if isinstance(w, dict) and w.get("phase_elapsed")]
    if elapsed:
        for section in (event, context):
            if section.get("phase_elapsed") == elapsed:
                section.pop("phase_elapsed")
    def candidate_key(item):
        if isinstance(item, dict) and isinstance(item.get("occurrence_id"), str) and type(item.get("version")) is int:
            return (item["occurrence_id"], item["version"])
        return None

    eligible = {key: item for item in context.get("eligible_items", ()) if (key := candidate_key(item))}
    if eligible and "recheck_items" in context:
        remaining = []
        for item in context["recheck_items"]:
            other = eligible.get(candidate_key(item))
            if other is None or any(k in other and other[k] != v for k, v in item.items()):
                remaining.append(item)
            else:
                other.update(item)
        context["recheck_items"] = remaining


def project_input(payload: dict, *, share: bool, document_sources: tuple[str, ...] = (),
                  origin: str | None = None, selection=None) -> tuple[str | None, dict]:
    context = payload.get("context")
    data = {key: _clean(payload[key]) for key in ("event", "grants") if key in payload}
    origin = _enum(origin or payload.get("origin"), {"user", "background"}, None)
    if origin:
        data["origin"] = origin
    for key in ("semantic_chat", "semantic_background"):
        if type(payload.get(key)) is bool:
            data[key] = payload[key]
    split = selection is not None and bool(selection.section_names)
    if not split and isinstance(data.get("event"), dict):
        data["event"].pop("wakes", None)
    data["context"] = (project_context(context, share=share, document_sources=document_sources,
                                       windows=("last_15m",) if split and selection.activity_15m else (),
                                       focus_evidence=split and selection.focus_evidence_2m)
                       if isinstance(context, dict) else {})
    if split:
        fields = _initial_fields(selection)
        data["context"] = {k: v for k, v in data["context"].items() if k in fields}
        _deduplicate_wake_data(data)
    intent = payload.get("user_intent")
    if origin == "background" or not isinstance(intent, dict):
        return None, data
    data["user_intent"] = {
        key: _clean(intent[key]) for key in ("message_id", "selected_ref", "reply_to_proposal_id",
                                           "reply_to_proposal_version", "timezone") if key in intent
    }
    if requested_at := _stamp(intent.get("requested_at")):
        data["user_intent"]["requested_at"] = requested_at
    utterance = intent.get("utterance")
    return utterance if isinstance(utterance, str) else None, data


def project_business_result(value: dict) -> dict:
    return _clean(value)


def project_tool_result(name: str, value: dict, *, share: bool, document_sources: tuple[str, ...] = (),
                        args: dict | None = None, include_aggregates: bool = True) -> dict:
    result = project_business_result(value)
    if name in {"context_read", "context.read"}:
        for key in ("view", "data"):
            if isinstance(view := value.get(key), dict):
                request = args or {}
                requested = request.get("activity_window")
                windows = _requested_windows(request, view) if include_aggregates else ()
                projected = project_context(view, share=share, document_sources=document_sources,
                                            windows=windows)
                if requested and include_aggregates:
                    projected = {k: v for k, v in projected.items() if k in {"as_of", "activity_windows"}}
                result[key] = projected
    return result
