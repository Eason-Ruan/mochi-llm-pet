"""The read-only Observation snapshot consumed by System 2."""

from __future__ import annotations

from collections import Counter
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3
from typing import Literal

from .state import Observation

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
class HistorySegment:
    first_observed_at: datetime
    last_observed_at: datetime
    activity_kind: str
    state: str
    sample_count: int
    summary: str | None

    def to_dict(self) -> dict:
        return {
            "first_observed_at": self.first_observed_at.isoformat(),
            "last_observed_at": self.last_observed_at.isoformat(),
            "activity_kind": self.activity_kind,
            "state": self.state,
            "sample_count": self.sample_count,
            "summary": self.summary,
        }


@dataclass(frozen=True)
class ObservationHistory:
    recent: tuple[Observation, ...]
    segments: tuple[HistorySegment, ...]
    gaps: tuple[tuple[datetime, datetime], ...]
    total_samples: int
    state_counts: tuple[tuple[str, int], ...]
    gap_count: int
    omitted_segment_count: int

    def to_dict(self) -> dict:
        return {
            "recent": [
                {key: value for key, value in obs.to_dict().items()
                 if key in ("observed_at", "activity_kind", "state", "reason_codes", "summary")}
                for obs in self.recent
            ],
            "segments": [segment.to_dict() for segment in self.segments],
            "gaps": [
                {"last_observed_at": last.isoformat(), "next_observed_at": following.isoformat()}
                for last, following in self.gaps
            ],
            "stats": {
                "total_samples": self.total_samples,
                "state_counts": dict(self.state_counts),
                "gap_count": self.gap_count,
                "omitted_segment_count": self.omitted_segment_count,
            },
        }


@dataclass(frozen=True)
class ObservationContext:
    as_of: datetime
    current: Observation | None
    current_status: CurrentStatus
    history: ObservationHistory

    def to_dict(self) -> dict:
        return {
            "as_of": self.as_of.isoformat(),
            "current": self.current.to_dict() if self.current else None,
            "current_status": self.current_status,
            "history": self.history.to_dict(),
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
        if gap:
            gaps.append((previous.last_observed_at, obs.observed_at))
        if previous and not gap and (previous.activity_kind, previous.state) == (obs.activity_kind, obs.state):
            segments[-1] = replace(
                previous, last_observed_at=obs.observed_at, sample_count=previous.sample_count + 1,
                summary=obs.summary or previous.summary,
            )
            continue
        changed_kind = previous is not None and previous.activity_kind != obs.activity_kind
        priorities.append(2 if obs.state in {"available", "away"} else int(changed_kind))
        segments.append(HistorySegment(obs.observed_at, obs.observed_at, obs.activity_kind, obs.state, 1,
                                       obs.summary or None))

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

    def __init__(self, path: Path | str):
        self.path = Path(path).resolve()

    @contextmanager
    def _snapshot(self):
        try:
            connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
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
        stamp = _timestamp(as_of)
        with self._snapshot() as connection:
            exists, managed = self._tables(connection)
            row = self._latest(connection, stamp, managed) if exists else None
            return _current(row, as_of)

    def read_observation_context(self, as_of: datetime) -> ObservationContext:
        stamp = _timestamp(as_of)
        as_of = as_of.astimezone(UTC)
        since = as_of - _HOUR
        with self._snapshot() as connection:
            exists, managed = self._tables(connection)
            latest = self._latest(connection, stamp, managed) if exists else None
            rows = []
            if managed:
                rows = connection.execute("""
                    SELECT * FROM effective_observations
                    WHERE observed_at > ? AND observed_at <= ? AND published_at <= ?
                    ORDER BY observed_at, observation_id
                """, (_timestamp(since), stamp, stamp)).fetchall()
            elif exists:
                # Legacy ISO timestamps can omit fractional seconds. Apply
                # the exact exclusive lower bound after decoding the rows.
                legacy_rows = connection.execute("""
                    SELECT * FROM effective_observations WHERE observed_at >= ? AND observed_at <= ?
                """, (since.replace(microsecond=0).isoformat(), stamp)).fetchall()
                rows = sorted(
                    (row for row in legacy_rows if datetime.fromisoformat(row["observed_at"]) > since),
                    key=lambda row: (datetime.fromisoformat(row["observed_at"]), row["observation_id"]),
                )
            current, status = _current(latest, as_of)
        current_id = latest["observation_id"] if current is not None else None
        return ObservationContext(as_of, current, status, _history(rows, current_id))
