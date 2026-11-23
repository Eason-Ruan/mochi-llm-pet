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

from .realtime import boot_id as system_boot_id, clocks_agree
from .state import Observation, _KINDS, _REASONS

_HOUR = timedelta(hours=1)
_GAP = timedelta(seconds=45)
_RECENT_LIMIT = 3
_SEGMENT_LIMIT = 8
_KEEP_LATEST = 4
CurrentStatus = Literal["fresh", "expired", "context_changed", "no_data"]
_STATES = ("working", "available", "away", "unknown")


def _timestamp(value: datetime) -> str:
    if value.utcoffset() is None:
        raise ValueError("Observation timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class SummarySample:
    observed_at: datetime
    summary: str


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


@dataclass(frozen=True)
class RealtimeBridge:
    head: RealtimeHead
    departure_serial: int | None = None
    departure_through_cursor: int | None = None
    departure_reason: str | None = None


def project_observation_context(context: ObservationContext, *, share_summary: bool = False) -> dict:
    """Allowlist the System 2 context, including only explicitly shared descriptions."""
    return {
        "as_of": context.as_of.isoformat(),
        "current": _observation_projection(
            context.current, current=True, share_summary=share_summary,
        ) if context.current else None,
        "current_status": context.current_status
        if context.current_status in ("fresh", "expired", "context_changed", "no_data") else "no_data",
        "history": context.history.to_dict(share_summary=share_summary),
    }


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
        if connection is None:
            return False, False
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(effective_observations)")}
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
    def _history_rows(connection, stamp: str, since: datetime, exists: bool, managed: bool) -> list[sqlite3.Row]:
        if managed:
            return connection.execute("""
                SELECT * FROM effective_observations
                WHERE observed_at > ? AND observed_at <= ? AND published_at <= ?
                ORDER BY observed_at, observation_id
            """, (_timestamp(since), stamp, stamp)).fetchall()
        if not exists:
            return []
        # Legacy ISO timestamps can omit fractional seconds. Apply
        # the exact exclusive lower bound after decoding the rows.
        legacy_rows = connection.execute("""
            SELECT * FROM effective_observations WHERE observed_at >= ? AND observed_at <= ?
        """, (since.replace(microsecond=0).isoformat(), stamp)).fetchall()
        return sorted(
            (row for row in legacy_rows if datetime.fromisoformat(row["observed_at"]) > since),
            key=lambda row: (datetime.fromisoformat(row["observed_at"]), row["observation_id"]),
        )

    def read_observation_context(self, as_of: datetime) -> ObservationContext:
        stamp = _timestamp(as_of)
        as_of = as_of.astimezone(UTC)
        with self._snapshot() as connection:
            exists, managed = self._tables(connection)
            latest = self._latest(connection, stamp, managed) if exists else None
            rows = self._history_rows(connection, stamp, as_of - _HOUR, exists, managed)
            current, status = _current(latest, as_of)
        current_id = latest["observation_id"] if current is not None else None
        return ObservationContext(as_of, current, status, _history(rows, current_id))

    def _live_head(
        self, connection, at: datetime, monotonic_s: float, *, policy_generation: int | None = None,
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
        return RealtimeHead(observation, "fresh", cursor, generation)

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
    ) -> ObservationContext:
        """Read the validated live head and wall-time history in one transaction."""
        at = as_of if as_of is not None else datetime.now(UTC)
        stamp = _timestamp(at)
        at = at.astimezone(UTC)
        mono = monotonic_s if monotonic_s is not None else time.monotonic()
        with self._snapshot(realtime=True, busy_timeout_s=busy_timeout_s) as connection:
            head = (self._live_head(connection, at, mono, policy_generation=policy_generation)
                    if include_current else RealtimeHead(None, "context_changed"))
            exists, managed = self._tables(connection)
            rows = self._history_rows(connection, stamp, at - _HOUR, exists, managed)
        current_id = head.publication_cursor if head.current is not None else None
        return ObservationContext(at, head.current, head.current_status, _history(rows, current_id))
