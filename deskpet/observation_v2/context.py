"""The read-only Observation snapshot consumed by System 2."""

from __future__ import annotations

from collections import Counter
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
import json
import math
from pathlib import Path
import sqlite3
from threading import Lock
import time
from typing import Literal

from .documents import document_source_allowed, is_mochi_app, native_document_source, sanitize_document_name
from .realtime import boot_id as system_boot_id, clocks_agree
from .state import Observation, _KINDS, _REASONS
from .vision import (
    cloud_screen_text, multimodal_summary, validated_scene_elements, validated_screen_summary, validated_visible_changes,
)

_HOUR = timedelta(hours=1)
_GAP = timedelta(seconds=45)
_RECENT_LIMIT = 3
_SEGMENT_LIMIT = 8
_KEEP_LATEST = 4
CurrentStatus = Literal["fresh", "expired", "context_changed", "no_data"]
_STATES = ("working", "available", "away", "unknown")
CAPTURE_HEALTH_STATUSES = {"ready", "skipped", "source_restricted", "capture_failed", "model_failed", "filtered"}
CAPTURE_HEALTH_REASONS = {
    "capture_disabled", "privacy_excluded", "screen_locked", "mochi_foreground", "private_window",
    "foreground_unavailable", "browser_url_unavailable", "lock_state_unavailable", "window_unavailable",
    "video_source", "recipient_unavailable", "foreground_changed_during_capture", "waiting_for_visual_change",
    "screen_recording_permission_denied", "filtered_desktop_unavailable", "desktop_privacy_restricted",
    "desktop_display_inventory_unverified", "desktop_window_inventory_unverified", "desktop_session_unverified",
    "desktop_inventory_changed", "desktop_no_visible_windows",
    "foreground_context_only", "unfiltered_desktop_unavailable",
}
CAPTURE_HEALTH_TIMES = ("last_ready_at", "last_failure_at", "last_skipped_at")


def _timestamp(value: datetime) -> str:
    if value.utcoffset() is None:
        raise ValueError("Observation timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class SummarySample:
    observed_at: datetime
    summary: str


@dataclass(frozen=True)
class ActivityDetail:
    observed_at: datetime
    activity_id: int
    app_name: str | None = None
    domain: str | None = None
    activity_kind: str = "unknown"
    summary: str | None = None
    document_name: str | None = None
    document_source: str | None = None
    scene_elements: tuple[str, ...] = ()
    visible_changes: tuple[str, ...] = ()
    late: bool = False
    confirmed_through: datetime | None = None
    document_change_at: datetime | None = None
    visual: bool = False
    hidden: bool = False


@dataclass(frozen=True)
class CurrentContext:
    observed_at: datetime | None = None
    app_name: str | None = None
    screen_locked: bool | None = None
    idle_s: float | None = None
    activity_id: int | None = None
    domain: str | None = None


@dataclass(frozen=True)
class HistorySegment:
    first_observed_at: datetime
    last_observed_at: datetime
    activity_kind: str
    state: str
    sample_count: int
    summary: str | None
    summary_samples: tuple[SummarySample, ...] = ()

    def to_dict(self, *, share_summary: bool = True) -> dict:
        result = {
            "first_observed_at": self.first_observed_at.isoformat(),
            "last_observed_at": self.last_observed_at.isoformat(),
            "activity_kind": self.activity_kind if self.activity_kind in _KINDS else "unknown",
            "state": self.state if self.state in _STATES else "unknown",
            "sample_count": self.sample_count if type(self.sample_count) is int else 0,
        }
        if share_summary:
            result["summary"] = self.summary
            result["summary_samples"] = [
                {"observed_at": sample.observed_at.isoformat(), "summary": sample.summary}
                for sample in self.summary_samples
            ]
        return result


def _observation_projection(obs: Observation, *, current: bool, share_summary: bool) -> dict:
    result = {
        "observed_at": obs.observed_at.isoformat(),
        "activity_kind": obs.activity_kind if obs.activity_kind in _KINDS else "unknown",
        "state": obs.state if obs.state in _STATES else "unknown",
        "reason_codes": [reason for reason in obs.reason_codes if reason in _REASONS],
    }
    if current:
        result["valid_until"] = obs.valid_until.isoformat()
        result["context_revision"] = obs.context_revision if type(obs.context_revision) is int else None
    if share_summary:
        result["summary"] = obs.summary
    return result


@dataclass(frozen=True)
class ObservationHistory:
    recent: tuple[Observation, ...]
    segments: tuple[HistorySegment, ...]
    gaps: tuple[tuple[datetime, datetime], ...]
    total_samples: int
    state_counts: tuple[tuple[str, int], ...]
    gap_count: int
    omitted_segment_count: int

    def to_dict(self, *, share_summary: bool = True) -> dict:
        return {
            "recent": [
                _observation_projection(obs, current=False, share_summary=share_summary)
                for obs in self.recent
            ],
            "segments": [segment.to_dict(share_summary=share_summary) for segment in self.segments],
            "gaps": [
                {"last_observed_at": last.isoformat(), "next_observed_at": following.isoformat()}
                for last, following in self.gaps
            ],
            "stats": {
                "total_samples": self.total_samples if type(self.total_samples) is int else 0,
                "state_counts": {state: count for state, count in self.state_counts
                                 if state in _STATES and type(count) is int},
                "gap_count": self.gap_count if type(self.gap_count) is int else 0,
                "omitted_segment_count": self.omitted_segment_count if type(self.omitted_segment_count) is int else 0,
            },
        }


@dataclass(frozen=True)
class ObservationContext:
    as_of: datetime
    current: Observation | None
    current_status: CurrentStatus
    history: ObservationHistory
    current_context: CurrentContext = CurrentContext()
    activities: tuple[ActivityDetail, ...] = ()
    capture_health: dict | None = None
    history_start: datetime | None = None
    history_end: datetime | None = None
    recent_activities: tuple[ActivityDetail, ...] | None = None

    @classmethod
    def empty(cls, as_of: datetime) -> ObservationContext:
        return cls(as_of, None, "no_data", _history([], None))

    def to_dict(self) -> dict:
        return project_observation_context(self, share_summary=True)


@dataclass(frozen=True)
class RealtimeHead:
    current: Observation | None
    current_status: CurrentStatus
    publication_cursor: int | None = None
    producer_generation: str | None = None
    foreground_key: str | None = None


@dataclass(frozen=True)
class RealtimeBridge:
    head: RealtimeHead
    departure_serial: int | None = None
    departure_through_cursor: int | None = None
    departure_reason: str | None = None


OBSERVATION_CONTEXT_FIELDS = frozenset({
    "current", "current_status", "history", "current_context", "last_observed_activity", "interruptibility",
    "history_1h", "capture_health",
})


def _detail_projection(detail: ActivityDetail, *, share_summary: bool, document_sources: tuple[str, ...],
                       include_document_source: bool) -> dict:
    observed_at = detail.observed_at.isoformat()
    result = {"observed_at": observed_at,
              "activity_kind": detail.activity_kind if detail.activity_kind in _KINDS else "unknown"}
    if share_summary:
        result.update(summary=detail.summary, scene_elements=list(detail.scene_elements),
                      visible_changes=list(detail.visible_changes))
        if detail.summary:
            result["summary_observed_at"] = observed_at
        if detail.visual:
            result["scene_observed_at"] = observed_at
        if detail.visible_changes:
            result["change_observed_at"] = observed_at
    if (detail.document_name and detail.document_source
            and document_source_allowed(detail.document_source, document_sources)):
        result["document_name"] = detail.document_name
        if detail.document_change_at is not None:
            result["document_change"] = {"type": "unsaved_changes_appeared",
                                         "observed_at": detail.document_change_at.isoformat()}
        if include_document_source:
            result["document_source"] = detail.document_source
    return result


def _activity_history(details: tuple[ActivityDetail, ...], *, retained: tuple[ActivityDetail, ...],
                      share_summary: bool, document_sources: tuple[str, ...], include_document_source: bool) -> dict:
    segments: list[dict] = []
    gaps: list[dict] = []
    previous = None
    previous_visual = None
    allowed = set(retained)
    withheld = False
    for detail in details:
        if detail not in allowed:
            withheld = True
            continue
        timestamp = detail.observed_at.isoformat()
        previous_coverage = (previous.confirmed_through or previous.observed_at) if previous is not None else None
        gap = previous_coverage is not None and (withheld or detail.observed_at - previous_coverage > _GAP)
        if gap:
            last_seen = max(previous.observed_at, min(previous_coverage, detail.observed_at))
            gaps.append({"last_observed_at": last_seen.isoformat(), "next_observed_at": timestamp})
        visual_changed = detail.visual and previous_visual is not None and (
            set(previous_visual.scene_elements) != set(detail.scene_elements)
            or previous_visual.activity_kind != detail.activity_kind
        )
        if (previous is None or gap or previous.activity_id != detail.activity_id
                or visual_changed or previous.document_name != detail.document_name):
            segment = _detail_projection(detail, share_summary=share_summary, document_sources=document_sources,
                                         include_document_source=include_document_source)
            segment.pop("observed_at")
            segments.append(dict(segment, first_observed_at=timestamp, last_observed_at=timestamp, sample_count=1))
            previous_visual = None
        else:
            segment = segments[-1]
            segment["last_observed_at"] = timestamp
            segment["sample_count"] += 1
            if share_summary:
                if detail.summary and (detail.visual or not segment["summary"]):
                    segment["summary"] = detail.summary
                    segment["summary_observed_at"] = timestamp
                if detail.visual:
                    segment["activity_kind"] = detail.activity_kind
                    segment["scene_elements"] = list(detail.scene_elements)
                if detail.visible_changes:
                    segment["visible_changes"] = list(detail.visible_changes)
        if share_summary and detail.visible_changes:
            segments[-1]["change_observed_at"] = timestamp
        if share_summary and detail.visual:
            segments[-1]["scene_observed_at"] = timestamp
        if detail.document_change_at is not None and "document_name" in segments[-1]:
            segments[-1]["document_change"] = {"type": "unsaved_changes_appeared",
                                               "observed_at": detail.document_change_at.isoformat()}
        previous = detail
        if detail.visual:
            previous_visual = detail
        withheld = False
    return {"segments": segments[-_SEGMENT_LIMIT:], "gaps": gaps[-_SEGMENT_LIMIT:],
            "stats": {"total_samples": len(retained), "gap_count": len(gaps),
                      "omitted_segment_count": max(0, len(segments) - _SEGMENT_LIMIT)}}


def _excluded(detail: ActivityDetail, apps: tuple[str, ...], domains: tuple[str, ...]) -> bool:
    app = (detail.app_name or "").casefold()
    domain = (detail.domain or "").casefold()
    return (detail.hidden or any(part in app for part in apps)
            or any(domain == host or domain.endswith(f".{host}") for host in domains))


def _restrict_legacy_summaries(view: dict) -> None:
    if "summary" in view:
        view["summary"] = cloud_screen_text(view["summary"], summary=True)
    for value in view.values():
        if isinstance(value, dict):
            _restrict_legacy_summaries(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _restrict_legacy_summaries(item)


def project_observation_context(
    context: ObservationContext, *, share_summary: bool = False, document_sources: tuple[str, ...] = (),
    excluded_apps: tuple[str, ...] = (), excluded_domains: tuple[str, ...] = (), paused: bool = False,
    suppress_details: bool = False, include_capture_health: bool = False, include_document_source: bool = False,
    historical: bool = False, restrict_legacy_summaries: bool = False,
) -> dict:
    """Allowlist the System 2 context, including only explicitly shared descriptions."""
    current_signals = context.current_context if not paused else CurrentContext()
    protected = paused or suppress_details or current_signals.screen_locked is True
    share_summary = share_summary and not protected
    document_sources = document_sources if not protected else ()
    current = None if protected else context.current
    status = "context_changed" if protected else context.current_status
    now = current_signals.observed_at
    foreground = current_signals.app_name if share_summary or is_mochi_app(current_signals.app_name) else None
    if foreground and _excluded(ActivityDetail(context.as_of, 0, foreground, current_signals.domain),
                                excluded_apps, excluded_domains):
        foreground = None
    details = () if paused else tuple(detail for detail in context.activities
                                     if not _excluded(detail, excluded_apps, excluded_domains))
    recent_details = context.recent_activities if context.recent_activities is not None else details
    history = (_history([], None) if paused else context.history).to_dict(share_summary=share_summary)
    if not paused and (context.activities or excluded_apps or excluded_domains):
        # Legacy descriptions have no source metadata to recheck against exclusions.
        history = context.history.to_dict(share_summary=False)
    if restrict_legacy_summaries:
        _restrict_legacy_summaries(history)
    latest = None if paused else next((detail for detail in reversed(recent_details)
                                       if not _excluded(detail, excluded_apps, excluded_domains)), None)
    if latest is not None:
        activity = _detail_projection(latest, share_summary=share_summary, document_sources=document_sources,
                                      include_document_source=include_document_source)
        activity["relation"] = ("current" if not protected and not latest.late and
                                current_signals.activity_id == latest.activity_id else "previous")
    elif not context.activities and not recent_details and not paused and not excluded_apps and not excluded_domains:
        legacy = max((item for item in ((*context.history.recent, context.current)
                                        if context.current else context.history.recent)),
                     key=lambda item: item.observed_at, default=None)
        activity = _observation_projection(legacy, current=False, share_summary=share_summary) if legacy else None
        if activity is not None:
            activity["relation"] = "current" if legacy is current else "previous"
            if restrict_legacy_summaries:
                _restrict_legacy_summaries(activity)
    else:
        activity = None
    if context.activities:
        history_1h = _activity_history(context.activities, retained=details, share_summary=share_summary,
                                      document_sources=document_sources, include_document_source=include_document_source)
    else:
        history_1h = {key: data for key, data in history.items() if key != "recent"}
    interrupt = _observation_projection(current, current=True, share_summary=False) if current and not historical else None
    result = {
        "as_of": context.as_of.isoformat(),
        "current": _observation_projection(
            current, current=True, share_summary=share_summary and not context.activities and not excluded_apps
            and not excluded_domains,
        ) if current else None,
        "current_status": status,
        "history": history,
        "current_context": {"observed_at": now.isoformat() if now else None,
                            "foreground_app": foreground if not protected else None,
                            "screen_locked": current_signals.screen_locked,
                            "idle_s": current_signals.idle_s},
        "last_observed_activity": activity,
        "interruptibility": {"status": status if not historical else "context_changed", "current": interrupt},
        "history_1h": {**history_1h,
                        "from_at": (context.history_start or context.as_of - _HOUR).isoformat(),
                        "to_at": (context.history_end or context.as_of).isoformat()},
    }
    if include_capture_health:
        result["capture_health"] = context.capture_health if not protected else None
    if restrict_legacy_summaries and result["current"] is not None:
        _restrict_legacy_summaries(result["current"])
    return result


def _decode(row: sqlite3.Row) -> Observation:
    return Observation(
        row["activity_kind"], row["state"], row["summary"], tuple(json.loads(row["reason_codes"])),
        datetime.fromisoformat(row["observed_at"]), datetime.fromisoformat(row["valid_until"]),
        row["context_revision"],
    )


def _current(row: sqlite3.Row | None, as_of: datetime) -> tuple[Observation | None, CurrentStatus]:
    if row is None:
        return None, "no_data"
    observation = _decode(row)
    if observation.valid_until <= as_of:
        return None, "expired"
    if "invalidated_at" not in row.keys():
        # Old databases do not record publication or context invalidation.
        return None, "context_changed"
    invalidated_at = row["invalidated_at"]
    if invalidated_at and datetime.fromisoformat(invalidated_at) <= as_of:
        return None, "context_changed"
    return observation, "fresh"


def _history(rows: list[sqlite3.Row], current_id: int | None) -> ObservationHistory:
    observations = [_decode(row) for row in rows]
    counts = Counter(obs.state for obs in observations)
    segments: list[HistorySegment] = []
    priorities: list[int] = []
    gaps: list[tuple[datetime, datetime]] = []
    for obs in observations:
        previous = segments[-1] if segments else None
        gap = previous is not None and obs.observed_at - previous.last_observed_at > _GAP
        description = " ".join(obs.summary.split()) if obs.summary else ""
        sample = SummarySample(obs.observed_at, description) if description else None
        if gap:
            gaps.append((previous.last_observed_at, obs.observed_at))
        if previous and not gap and (previous.activity_kind, previous.state) == (obs.activity_kind, obs.state):
            samples = previous.summary_samples
            if sample is not None:
                if not samples:
                    samples = (sample,)
                elif samples[0].summary != sample.summary:
                    samples = (samples[0], sample)
            segments[-1] = replace(
                previous, last_observed_at=obs.observed_at, sample_count=previous.sample_count + 1,
                summary=obs.summary if sample is not None else previous.summary,
                summary_samples=samples,
            )
            continue
        changed_kind = previous is not None and previous.activity_kind != obs.activity_kind
        priorities.append(2 if obs.state in {"available", "away"} else int(changed_kind))
        segments.append(HistorySegment(obs.observed_at, obs.observed_at, obs.activity_kind, obs.state, 1,
                                       obs.summary if sample is not None else None, (sample,) if sample else ()))

    omitted = max(0, len(segments) - _SEGMENT_LIMIT)
    if omitted:
        boundary = len(segments) - _KEEP_LATEST
        older = sorted(range(boundary), key=lambda index: (priorities[index], index), reverse=True)
        selected = sorted(older[:_SEGMENT_LIMIT - _KEEP_LATEST]) + list(range(boundary, len(segments)))
        segments = [segments[index] for index in selected]
    recent = tuple(obs for row, obs in zip(rows, observations) if row["observation_id"] != current_id)
    return ObservationHistory(
        recent[-_RECENT_LIMIT:], tuple(segments), tuple(gaps[-_SEGMENT_LIMIT:]), len(observations),
        tuple((state, counts[state]) for state in _STATES), len(gaps), omitted,
    )


def _columns(connection: sqlite3.Connection | None, table: str) -> set[str]:
    return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")} if connection else set()


def _known_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    stamp = datetime.fromisoformat(value)
    return stamp if stamp.utcoffset() is not None else None


def _row_value(row: sqlite3.Row, name: str):
    return row[name] if name in row.keys() else None


def _tokens(value: str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    parts = json.loads(value)
    return tuple(item for item in parts[:8] if isinstance(item, str)) if isinstance(parts, list) else ()


def _legacy_activity_kind(connection, frame: sqlite3.Row, as_of: datetime) -> str:
    columns = _columns(connection, "effective_observations")
    if "published_at" in columns:
        row = connection.execute("""
            SELECT activity_kind FROM effective_observations WHERE observed_at = ? AND published_at <= ?
            ORDER BY observation_id DESC LIMIT 1
        """, (frame["captured_at"], _timestamp(as_of))).fetchone()
        if row is not None and row[0] in _KINDS:
            return row[0]
    return "unknown"


def _activity_details(connection, *, as_of: datetime, since: datetime, until: datetime) -> tuple[ActivityDetail, ...]:
    if not _columns(connection, "activities"):
        return ()
    activities = connection.execute("""
        SELECT * FROM activities WHERE ended_at >= ? AND started_at <= ? ORDER BY started_at, activity_id
    """, (_timestamp(since), _timestamp(until))).fetchall()
    screens_by_activity: dict[int, list[sqlite3.Row]] = {}
    screen_columns = _columns(connection, "screen_observations")
    if screen_columns:
        available = "AND (processed_at IS NULL OR processed_at <= ?)" if "processed_at" in screen_columns else ""
        params = (_timestamp(since), _timestamp(until))
        for row in connection.execute(f"""
            SELECT * FROM screen_observations WHERE captured_at > ? AND captured_at <= ? AND status = 'ready'
            {available} ORDER BY captured_at, rowid
        """, params + (_timestamp(as_of),) if available else params):
            screens_by_activity.setdefault(row["activity_id"], []).append(row)
    details: list[ActivityDetail] = []
    for row in activities:
        activity_id, app = row["activity_id"], row["app_name"]
        document_status = _row_value(row, "document_status")
        started = _known_time(row["started_at"])
        if started is None:
            continue
        if row["screen_locked"] == 1 or is_mochi_app(app) or document_status == "private_window":
            if since < started <= until:
                details.append(ActivityDetail(started, activity_id, hidden=True))
            continue
        frames = screens_by_activity.get(activity_id, ())
        if not app and not frames:  # Legacy recording intervals have no OS app.
            continue
        sampled = _known_time(row["system_sampled_at"])
        name, source = _row_value(row, "document_name"), _row_value(row, "document_source")
        name = sanitize_document_name(name, source) if document_status == "identified" else None
        known_source = native_document_source(app, None)
        seen_kind = {"app:vscode": "ide", "app:microsoft-word": "document",
                     "app:microsoft-excel": "document"}.get(known_source, "unknown")
        if name and source.startswith("domain:"):
            seen_kind = "document"
        coverage = sampled if sampled is not None and sampled <= until else None
        base = ActivityDetail(started, activity_id, app, row["domain"], document_name=name, document_source=source,
                              summary=multimodal_summary(seen_kind, ()), confirmed_through=coverage)
        change = (_known_time(_row_value(row, "document_change_at"))
                  if name and _row_value(row, "document_change") == "unsaved_changes_appeared" else None)
        change = change if change is not None and since < change <= until else None
        if change is not None:
            details.append(replace(base, observed_at=change, document_change_at=change))
        if not frames:
            if since < started <= until:
                details.append(base)
        latest_capture = None
        latest_unfiltered = False
        # Keep the screen timestamp as evidence, with the start as a scope boundary.
        # A late response can enrich that segment without changing the live head.
        for frame in frames:
            captured_at = _known_time(frame["captured_at"])
            if captured_at is None:
                continue
            latest_capture = captured_at
            latest_unfiltered = _row_value(frame, "unfiltered_desktop") == 1
            screen_source = _row_value(frame, "document_source")
            screen_name = sanitize_document_name(_row_value(frame, "document_name"), screen_source)
            elements = validated_scene_elements(_tokens(_row_value(frame, "scene_elements")))
            changes = validated_visible_changes(_tokens(_row_value(frame, "visible_changes")))
            activity_kind = _row_value(frame, "activity_kind")
            summary = multimodal_summary(frame["screen_kind"], elements)
            if activity_kind not in _KINDS:
                activity_kind = _legacy_activity_kind(connection, frame, as_of)
                summary = cloud_screen_text(frame["screen_summary"], summary=True) or summary
            if latest_unfiltered:
                summary = validated_screen_summary(frame["screen_summary"])
            detail = replace(
                base, observed_at=captured_at, activity_kind=activity_kind,
                summary=summary,
                document_name=name or (screen_name if screen_source else None),
                document_source=source if name else screen_source,
                scene_elements=elements, visible_changes=changes,
                late=_row_value(frame, "realtime_eligible") == 0,
                document_change_at=change if change is not None and change <= captured_at else None,
                visual=True,
            )
            details.append(detail)
        if sampled is not None and max(started, since, latest_capture or started, change or started) < sampled <= until:
            if not frames or (name and not latest_unfiltered):
                details.append(replace(base, observed_at=sampled, document_change_at=change))
    return tuple(sorted(details, key=lambda detail: (detail.observed_at, detail.activity_id)))


def _recent_activity_details(connection, *, as_of: datetime, since: datetime, until: datetime,
                             details: tuple[ActivityDetail, ...]) -> tuple[ActivityDetail, ...] | None:
    if since != as_of - _HOUR or until != as_of:
        details = _activity_details(connection, as_of=as_of, since=as_of - _HOUR, until=as_of)
        if any(not detail.hidden for detail in details):
            return details
    elif any(not detail.hidden for detail in details):
        return None  # The history already contains the latest evidence.
    columns = _columns(connection, "activities")
    if not columns:
        return ()
    private = "AND document_status IS NOT 'private_window'" if "document_status" in columns else ""
    row = connection.execute(f"""
        SELECT started_at FROM activities
        WHERE started_at <= ? AND screen_locked IS NOT 1 AND app_name IS NOT NULL AND lower(app_name) != 'mochi'
        {private} ORDER BY ended_at DESC, activity_id DESC LIMIT 1
    """, (_timestamp(as_of - _HOUR),)).fetchone()
    started = _known_time(row["started_at"]) if row else None
    if started is None:
        return ()
    return _activity_details(connection, as_of=as_of, since=started - timedelta(microseconds=1), until=as_of)


def _capture_health(connection, as_of: datetime) -> dict | None:
    if not _columns(connection, "observation_capture_health"):
        return None
    row = connection.execute("""
        SELECT * FROM observation_capture_health WHERE singleton = 1 AND sampled_at <= ?
    """, (_timestamp(as_of),)).fetchone()
    if row is None or row["status"] not in CAPTURE_HEALTH_STATUSES:
        return None
    health = {"status": row["status"], "observed_at": row["sampled_at"]}
    reason = _row_value(row, "reason")
    if reason in CAPTURE_HEALTH_REASONS:
        health["reason"] = reason
    for key in CAPTURE_HEALTH_TIMES:
        stamp = _known_time(_row_value(row, key))
        if stamp is not None and stamp <= as_of:
            health[key] = stamp.isoformat()
    return health


class ObservationContextReader:
    """Read a transactionally consistent context without invoking either model."""

    def __init__(self, path: Path | str, *, boot_id: str | None = None):
        self.path = Path(path).resolve()
        self._boot_id = boot_id
        self._clock_guard = Lock()
        self._highest_cursor = 0
        self._rejected_cursor: int | None = None
        self._latest_read: tuple[tuple[str, int], datetime, float] | None = None

    @contextmanager
    def _snapshot(self, *, realtime: bool = False, busy_timeout_s: float = 0):
        try:
            connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True,
                                         timeout=busy_timeout_s if realtime else 5)
        except sqlite3.OperationalError:
            if self.path.exists():
                raise
            yield None
            return
        with closing(connection):
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN")
            yield connection

    @staticmethod
    def _tables(connection: sqlite3.Connection | None) -> tuple[bool, bool]:
        columns = _columns(connection, "effective_observations")
        return bool(columns), "published_at" in columns and "invalidated_at" in columns

    @staticmethod
    def _latest(connection: sqlite3.Connection, stamp: str, managed: bool) -> sqlite3.Row | None:
        if managed:
            return connection.execute("""
                SELECT * FROM effective_observations
                WHERE published_at <= ? AND observed_at <= ?
                ORDER BY published_at DESC, observation_id DESC LIMIT 1
            """, (stamp, stamp)).fetchone()
        return connection.execute("""
            SELECT * FROM effective_observations WHERE observed_at <= ?
            ORDER BY observed_at DESC, observation_id DESC LIMIT 1
        """, (stamp,)).fetchone()

    def read_current(self, as_of: datetime) -> tuple[Observation | None, CurrentStatus]:
        """Reconstruct the current row at a historical wall timestamp."""
        stamp = _timestamp(as_of)
        with self._snapshot() as connection:
            exists, managed = self._tables(connection)
            row = self._latest(connection, stamp, managed) if exists else None
            return _current(row, as_of)

    @staticmethod
    def _history_rows(connection, stamp: str, since: datetime, until: datetime,
                      exists: bool, managed: bool) -> list[sqlite3.Row]:
        if managed:
            return connection.execute("""
                SELECT * FROM effective_observations
                WHERE observed_at > ? AND observed_at <= ? AND published_at <= ?
                ORDER BY observed_at, observation_id
            """, (_timestamp(since), _timestamp(until), stamp)).fetchall()
        if not exists:
            return []
        # Legacy ISO timestamps can omit fractional seconds. Apply
        # the exact exclusive lower bound after decoding the rows.
        legacy_rows = connection.execute("""
            SELECT * FROM effective_observations WHERE observed_at >= ? AND observed_at <= ?
        """, (since.replace(microsecond=0).isoformat(), _timestamp(until))).fetchall()
        return sorted(
            (row for row in legacy_rows if datetime.fromisoformat(row["observed_at"]) > since),
            key=lambda row: (datetime.fromisoformat(row["observed_at"]), row["observation_id"]),
        )

    @staticmethod
    def _history_range(as_of, history_start, history_end):
        until = history_end if history_end is not None else as_of
        since = history_start if history_start is not None else as_of - _HOUR
        _timestamp(since)
        _timestamp(until)
        if not as_of - _HOUR <= since < until <= as_of:
            raise ValueError("Observation history range must be within the last hour")
        return since, until

    @staticmethod
    def _historical_signals(connection, as_of: datetime) -> CurrentContext:
        if not _columns(connection, "activities"):
            return CurrentContext()
        row = connection.execute("""
            SELECT * FROM activities WHERE started_at <= ? ORDER BY started_at DESC, activity_id DESC LIMIT 1
        """, (_timestamp(as_of),)).fetchone()
        if row is None:
            return CurrentContext()
        sampled = _known_time(row["system_sampled_at"])
        started = _known_time(row["started_at"])
        if started is None:
            return CurrentContext()
        lock = row["screen_locked"]
        if sampled is None or not as_of - _GAP < sampled <= as_of:
            ended = _known_time(row["ended_at"])
            if not (ended is not None and started <= as_of <= ended or as_of - _GAP < started <= as_of):
                return CurrentContext()
            return CurrentContext(started, row["app_name"], None if lock is None else bool(lock), activity_id=row["activity_id"],
                                  domain=row["domain"])
        return CurrentContext(sampled, row["app_name"], None if lock is None else bool(lock), row["idle_s"],
                              row["activity_id"], row["domain"])

    def read_observation_context(self, as_of: datetime, *, history_start: datetime | None = None,
                                 history_end: datetime | None = None) -> ObservationContext:
        stamp = _timestamp(as_of)
        as_of = as_of.astimezone(UTC)
        since, until = self._history_range(as_of, history_start, history_end)
        with self._snapshot() as connection:
            exists, managed = self._tables(connection)
            latest = self._latest(connection, stamp, managed) if exists else None
            rows = self._history_rows(connection, stamp, since, until, exists, managed)
            current, status = _current(latest, as_of)
            signals = self._historical_signals(connection, as_of)
            activities = _activity_details(connection, as_of=as_of, since=since, until=until)
            recent = _recent_activity_details(connection, as_of=as_of, since=since, until=until, details=activities)
            health = _capture_health(connection, as_of)
        current_id = latest["observation_id"] if current is not None else None
        return ObservationContext(as_of, current, status, _history(rows, current_id), signals, activities,
                                  health, since, until, recent)

    def read_privacy_marker(self) -> tuple[str | None, int, bool | None]:
        """Read the producer's lock interval without depending on a current estimator result."""
        with self._snapshot(realtime=True, busy_timeout_s=0.1) as connection:
            if "screen_locked" not in _columns(connection, "realtime_observation_head"):
                return None, 0, None
            head = connection.execute("SELECT * FROM realtime_observation_head WHERE singleton = 1").fetchone()
            if head is None or head["boot_id"] != (self._boot_id or system_boot_id()):
                return None, 0, None
            status = head["screen_locked"]
        return head["generation"], _row_value(head, "lock_epoch") or 0, bool(status) if status in (0, 1) else None

    def _live_signals(self, connection, at: datetime, mono: float, policy_generation: int | None) -> CurrentContext:
        if "system_sampled_at" not in _columns(connection, "realtime_observation_head"):
            return CurrentContext()
        head = connection.execute("SELECT * FROM realtime_observation_head WHERE singleton = 1").fetchone()
        if head is None or head["boot_id"] != (self._boot_id or system_boot_id()):
            return CurrentContext()
        locked = head["screen_locked"]
        sampled = _known_time(head["sampled_at"])
        sampled_mono, deadline = head["sampled_monotonic_s"], head["sample_deadline_s"]
        if (head["policy_generation"] != policy_generation or sampled is None or sampled > at or sampled_mono is None
                or deadline is None or not sampled_mono <= mono <= deadline
                or not clocks_agree(at, mono, sampled, sampled_mono)):
            return CurrentContext(screen_locked=True if locked == 1 else None)
        system_at = _known_time(head["system_sampled_at"])
        input_fresh = system_at is not None and 0 <= (at - system_at).total_seconds() <= deadline - sampled_mono
        idle = head["idle_s"] if input_fresh else None
        locked = locked if input_fresh or locked == 1 else None
        domain = None
        if head["activity_id"] is not None and _columns(connection, "activities"):
            activity = connection.execute("SELECT domain FROM activities WHERE activity_id = ?",
                                          (head["activity_id"],)).fetchone()
            domain = activity[0] if activity is not None else None
        return CurrentContext(sampled, head["app_name"], None if locked is None else bool(locked), idle,
                              head["activity_id"], domain)

    def _live_head(
        self, connection, at: datetime, monotonic_s: float, *, policy_generation: int | None = None,
        visible_at: datetime | None = None,
    ) -> RealtimeHead:
        exists, managed = self._tables(connection)
        if not exists:
            return RealtimeHead(None, "no_data")
        has_head = managed and connection.execute("""
            SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'realtime_observation_head'
        """).fetchone()
        head = connection.execute("SELECT * FROM realtime_observation_head WHERE singleton = 1").fetchone() if has_head else None
        if head is None:
            any_rows = connection.execute("SELECT 1 FROM effective_observations LIMIT 1").fetchone()
            return RealtimeHead(None, "context_changed" if any_rows else "no_data")
        generation, cursor = head["generation"], head["publication_cursor"]
        if cursor is None:
            return RealtimeHead(None, "no_data", producer_generation=generation)
        row = connection.execute("SELECT * FROM effective_observations WHERE observation_id = ?", (cursor,)).fetchone()
        changed = RealtimeHead(None, "context_changed", cursor, generation)
        head_policy = head["policy_generation"] if "policy_generation" in head.keys() else None
        if head_policy != policy_generation:
            return changed
        required = ("sampled_at", "sampled_monotonic_s", "sample_deadline_s", "published_monotonic_s", "valid_until_monotonic_s")
        trusted_boot = self._boot_id or system_boot_id()
        with self._clock_guard:
            # An older read transaction may resume after a different thread
            # has already observed a later publication or its invalidation.
            if cursor < self._highest_cursor or self._rejected_cursor == cursor:
                return changed
            self._highest_cursor = cursor
            if (row is None or row["invalidated_at"] is not None or not trusted_boot or trusted_boot != head["boot_id"]
                    or any(head[field] is None for field in required)):
                self._rejected_cursor = cursor
                return changed
            published_s, sampled_s = head["published_monotonic_s"], head["sampled_monotonic_s"]
            if not math.isfinite(monotonic_s) or monotonic_s < max(published_s, sampled_s):
                # A writer may have committed after the caller read its clock.
                return changed
            published = datetime.fromisoformat(row["published_at"])
            sampled = datetime.fromisoformat(head["sampled_at"])
            if visible_at is not None and (published > visible_at or sampled > visible_at):
                return changed
            token = generation, cursor
            previous = self._latest_read
            rollback = previous is not None and previous[0] == token and monotonic_s >= previous[2] and at < previous[1]
            if rollback or not clocks_agree(at, monotonic_s, published, published_s) or not clocks_agree(at, monotonic_s, sampled, sampled_s):
                self._rejected_cursor = cursor
                return changed
            observation = _decode(row)
            if observation.valid_until <= at or monotonic_s >= head["valid_until_monotonic_s"]:
                return RealtimeHead(None, "expired", cursor, generation)
            if monotonic_s > head["sample_deadline_s"]:
                self._rejected_cursor = cursor
                return changed
            if previous is None or previous[0] != token or monotonic_s >= previous[2]:
                self._latest_read = token, at, monotonic_s
        return RealtimeHead(observation, "fresh", cursor, generation, _row_value(row, "foreground_key"))

    def prior_wait_evidence(self, cursor, generation):
        """Read the local state paired with a decision when a new publication won the race."""
        if not cursor or generation is None:
            return None
        with self._snapshot(realtime=True) as db:
            if db is None or not _columns(db, "realtime_observation_head"):
                return None
            head = db.execute("SELECT generation, publication_cursor FROM realtime_observation_head").fetchone()
            if (head is None or head["generation"] != generation or head["publication_cursor"] is None
                    or head["publication_cursor"] <= cursor):
                return None
            row = db.execute("SELECT * FROM effective_observations WHERE observation_id=?", (cursor,)).fetchone()
            if row is None or _row_value(row, "producer_generation") not in (None, generation):
                return None
            locked = _row_value(row, "screen_locked")
            return {
                "activity_state": row["state"],
                "activity_context": [row["activity_kind"], _row_value(row, "foreground_key")],
                "lock_state": bool(locked) if locked in (0, 1) else None,
            }

    def same_published_state(self, first, last, *, revision, state, generation):
        """Check the entire publication interval, including away-and-back transitions."""
        with self._snapshot(realtime=True) as db:
            if db is None or first is None or last is None or first >= last:
                return False
            head = db.execute("SELECT generation, publication_cursor FROM realtime_observation_head").fetchone()
            if not head or tuple(head) != (generation, last):
                return False
            count, different, original = db.execute("""
                SELECT count(*), sum(state != ?), max(CASE WHEN observation_id=? THEN context_revision END)
                FROM effective_observations WHERE observation_id BETWEEN ? AND ?
                """, (state, first, first, last)).fetchone()
            return count == last - first + 1 and not different and original == revision

    def presentation_context_changed(self, first, last, *, revision, generation, now):
        """Read only local activity facts that could invalidate a queued presentation."""
        with self._snapshot(realtime=True) as db:
            if db is None:
                return False
            head = db.execute("""SELECT generation, publication_cursor, activity_id
                FROM realtime_observation_head WHERE singleton=1""").fetchone()
            if head is None or (head["generation"], head["publication_cursor"]) != (generation, last):
                return False
            original = db.execute("""SELECT state, observed_at, context_revision FROM effective_observations
                WHERE observation_id=?""", (first,)).fetchone()
            if original is None or (revision is not None and revision != original["context_revision"]):
                return False
            state = original["state"]
            different = db.execute("""SELECT 1 FROM effective_observations
                WHERE observation_id>? AND observation_id<=? AND state<>?
                AND (state='away' OR (?<>'unknown' AND state<>'unknown')) LIMIT 1""",
                (first, last, state, state)).fetchone()
            if different is not None:
                return True
            activity_id = head["activity_id"]
            if activity_id is None or not _columns(db, "screen_observations"):
                return False
            original_at = datetime.fromisoformat(original["observed_at"])
            if not original_at <= now:
                return False
            meeting = db.execute("""SELECT screen_kind FROM screen_observations
                WHERE activity_id=? AND captured_at>? AND captured_at<=? AND processed_at<=?
                AND status='ready' AND realtime_eligible=1 ORDER BY captured_at DESC LIMIT 1""",
                (activity_id, _timestamp(max(original_at, now - _GAP)), _timestamp(now), _timestamp(now))).fetchone()
            if meeting is None or meeting[0] != "meeting":
                return False
            # A meeting already visible to the model is part of its decision.
            previous = db.execute("""SELECT screen_kind FROM screen_observations
                WHERE captured_at>? AND captured_at<=? AND processed_at<=?
                AND status='ready' AND realtime_eligible=1 ORDER BY captured_at DESC LIMIT 1""",
                (_timestamp(original_at - _GAP), _timestamp(original_at), _timestamp(original_at))).fetchone()
            return previous is None or previous[0] != "meeting"

    def read_realtime_head(
        self, as_of: datetime | None = None, *, monotonic_s: float | None = None,
        policy_generation: int | None = None,
    ) -> RealtimeHead:
        """Validate the latest committed head against paired realtime clocks."""
        at = as_of if as_of is not None else datetime.now(UTC)
        _timestamp(at)
        mono = monotonic_s if monotonic_s is not None else time.monotonic()
        with self._snapshot(realtime=True) as connection:
            return self._live_head(connection, at, mono, policy_generation=policy_generation)

    def read_realtime_bridge(
        self, as_of: datetime, *, monotonic_s: float, policy_generation: int | None = None,
    ) -> RealtimeBridge:
        """Read the live head and a replayable watermark of intervening departures."""
        _timestamp(as_of)
        with self._snapshot(realtime=True) as connection:
            head = self._live_head(connection, as_of, monotonic_s, policy_generation=policy_generation)
            if connection is None or not connection.execute("""
                SELECT 1 FROM sqlite_master WHERE name='realtime_observation_departure' AND type='table'
            """).fetchone():
                return RealtimeBridge(head)
            departure = connection.execute("""
                SELECT serial, through_cursor, reason, boot_id, policy_generation
                FROM realtime_observation_departure WHERE singleton = 1
            """).fetchone()
            if (departure is None or departure["boot_id"] != (self._boot_id or system_boot_id())
                    or departure["policy_generation"] != policy_generation):
                return RealtimeBridge(head)
            return RealtimeBridge(head, departure["serial"], departure["through_cursor"], departure["reason"])

    def read_realtime_current(
        self, as_of: datetime | None = None, *, monotonic_s: float | None = None,
        policy_generation: int | None = None,
    ) -> tuple[Observation | None, CurrentStatus]:
        head = self.read_realtime_head(as_of, monotonic_s=monotonic_s, policy_generation=policy_generation)
        return head.current, head.current_status

    def read_realtime_context(
        self, as_of: datetime | None = None, *, monotonic_s: float | None = None,
        policy_generation: int | None = None, include_current: bool = True, busy_timeout_s: float = 0,
        history_start: datetime | None = None, history_end: datetime | None = None,
        current_at: datetime | None = None,
    ) -> ObservationContext:
        """Read the validated live head and wall-time history in one transaction."""
        at = as_of if as_of is not None else datetime.now(UTC)
        stamp = _timestamp(at)
        at = at.astimezone(UTC)
        mono = monotonic_s if monotonic_s is not None else time.monotonic()
        live_at = current_at if current_at is not None else at
        since, until = self._history_range(at, history_start, history_end)
        with self._snapshot(realtime=True, busy_timeout_s=busy_timeout_s) as connection:
            head = (self._live_head(connection, live_at, mono, policy_generation=policy_generation, visible_at=at)
                    if include_current else RealtimeHead(None, "context_changed"))
            exists, managed = self._tables(connection)
            rows = self._history_rows(connection, stamp, since, until, exists, managed)
            signals = self._live_signals(connection, live_at, mono, policy_generation) if include_current else CurrentContext()
            if signals.observed_at is not None and signals.observed_at > at:
                signals = CurrentContext(screen_locked=True if signals.screen_locked else None)
            activities = _activity_details(connection, as_of=at, since=since, until=until)
            recent = _recent_activity_details(connection, as_of=at, since=since, until=until, details=activities)
            health = _capture_health(connection, at)
        current_id = head.publication_cursor if head.current is not None else None
        return ObservationContext(at, head.current, head.current_status, _history(rows, current_id), signals,
                                  activities, health, since, until, recent)
