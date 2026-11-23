"""Build text-only System 2 inputs from registered business and Observation fields."""

from __future__ import annotations

from datetime import datetime
import math

from deskpet.observation_v2.state import _KINDS, _REASONS

_CONTEXT_FIELDS = {
    "as_of", "timezone", "trigger_reasons", "items", "habits", "eligible_items", "current_phase",
    "enabled_settings", "pending_proposals", "display_capabilities", "recent_decisions",
    "opportunity_id", "publication_cursor", "recheck_items", "selected_phase", "selected_interval",
    "selected_recovery",
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
    "description", "review", "confirmed_outcomes", "next_steps", "password", "access_token",
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


def project_context(value: dict, *, share: bool) -> dict:
    result = {key: _clean(data) for key, data in value.items() if key in _CONTEXT_FIELDS}
    current = value.get("current")
    history = value.get("history")
    result["current_status"] = _enum(value.get("current_status"), _CURRENT_STATUSES, "no_data")
    result["current"] = _sample(current, current=True, share=share) if isinstance(current, dict) else None
    if isinstance(history, dict):
        result["history"] = {
            "recent": [_sample(item, current=False, share=share) for item in _items(history.get("recent"), 3)],
            "segments": [_segment(item, share=share) for item in _items(history.get("segments"), 8)],
            "gaps": [
                {"last_observed_at": _stamp(item.get("last_observed_at")),
                 "next_observed_at": _stamp(item.get("next_observed_at"))}
                for item in _items(history.get("gaps"), 128)
            ],
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
    return result


def project_input(payload: dict, *, share: bool) -> tuple[str | None, dict]:
    context = payload.get("context")
    data = {key: _clean(payload[key]) for key in ("event", "grants") if key in payload}
    data["context"] = project_context(context, share=share) if isinstance(context, dict) else {}
    intent = payload.get("user_intent")
    if not isinstance(intent, dict):
        return None, data
    data["user_intent"] = {
        key: _clean(intent[key]) for key in ("message_id", "selected_ref", "reply_to_proposal_id") if key in intent
    }
    utterance = intent.get("utterance")
    return utterance if isinstance(utterance, str) else None, data


def project_business_result(value: dict) -> dict:
    return _clean(value)


def project_tool_result(name: str, value: dict, *, share: bool) -> dict:
    result = project_business_result(value)
    if name == "context_read":
        for key in ("view", "data"):
            if isinstance(view := value.get(key), dict):
                result[key] = project_context(view, share=share)
    return result
