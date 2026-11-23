"""Standalone Observation runs and SQLite persistence."""

from __future__ import annotations

from collections import Counter, deque
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
import json
import math
import os
import sqlite3
from pathlib import Path
import time
from uuid import uuid4

from .activity import ActivityRecorder, ActivitySample, ActivityStore, NativeActivitySampler
from .context import _timestamp
from .media import capture_desktop, recording_frames
from .state import Observation, StateObserver, context_revision
from .vision import ScreenModel, ScreenObservation, analyze_screen

REPLAY_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)
LIVE_UPDATE_INTERVAL_S = 30.0


_LIVE_DATABASE_ID = 0x4D4F4348
_REPLAY_DATABASE_ID = 0x4D4F4352


@contextmanager
def _live_writer(path: Path):
    # Hold a process-safe writer lock independently of the reader-facing DB.
    lock_path = path.with_name(path.name + ".writer-lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    os.close(descriptor)
    with closing(sqlite3.connect(lock_path, timeout=0)) as connection:
        try:
            connection.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError as exc:
            raise RuntimeError("Could not lock the live Observation database for writing") from exc
        yield


class ObservationStore(ActivityStore):
    def __init__(self, path: Path, *, live: bool = False):
        path = Path(path).resolve()
        flags = os.O_WRONLY | os.O_CREAT
        if not live:
            flags |= os.O_EXCL
        descriptor = os.open(path, flags, 0o600)
        os.close(descriptor)

        if live:
            with closing(sqlite3.connect(path)) as connection:
                app_id = connection.execute("PRAGMA application_id").fetchone()[0]
                has_tables = connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
                if app_id != _LIVE_DATABASE_ID:
                    if app_id or has_tables:
                        raise ValueError("Use a dedicated live database. Replay and legacy logs can be read with context.")
                    # Tag the empty DB before ActivityStore commits any tables.
                    # A restart can finish interrupted schema creation.
                    connection.execute(f"PRAGMA application_id = {_LIVE_DATABASE_ID}")
        super().__init__(path)
        try:
            application_id = _LIVE_DATABASE_ID if live else _REPLAY_DATABASE_ID
            self.connection.executescript(f"""
                BEGIN;
                CREATE TABLE IF NOT EXISTS screen_observations (
                    capture_id TEXT PRIMARY KEY,
                    activity_id INTEGER NOT NULL REFERENCES activities (activity_id),
                    captured_at TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('ready', 'failed', 'redacted_out')),
                    model_id TEXT NOT NULL,
                    screen_kind TEXT NOT NULL,
                    visible_text_redacted TEXT,
                    screen_summary TEXT
                );
                CREATE INDEX IF NOT EXISTS screen_captured_at ON screen_observations (captured_at);
                CREATE TABLE IF NOT EXISTS effective_observations (
                    observation_id INTEGER PRIMARY KEY,
                    activity_kind TEXT NOT NULL,
                    state TEXT NOT NULL,
                    summary TEXT,
                    reason_codes TEXT NOT NULL CHECK (json_valid(reason_codes)),
                    observed_at TEXT NOT NULL,
                    valid_until TEXT NOT NULL,
                    context_revision INTEGER NOT NULL,
                    published_at TEXT NOT NULL,
                    invalidated_at TEXT
                );
                CREATE INDEX IF NOT EXISTS observations_observed_at ON effective_observations (observed_at);
                CREATE INDEX IF NOT EXISTS observations_published_at ON effective_observations (published_at);
                PRAGMA application_id = {application_id};
                COMMIT;
            """)
        except Exception:
            self.close()
            raise

    def begin_live_run(self, as_of: datetime) -> int:
        self.invalidate_current(as_of)
        # The estimator's counters are run-local. Persist a unique revision
        # range for each producer start so a restart cannot revive a decision.
        revision = self.connection.execute("""
            SELECT context_revision FROM effective_observations ORDER BY observation_id DESC LIMIT 1
        """).fetchone()
        return revision[0] if revision is not None else 0

    def invalidate_current(self, as_of: datetime) -> None:
        stamp = _timestamp(as_of)
        with self.connection:
            self.connection.execute("""
                UPDATE effective_observations SET invalidated_at = ?
                WHERE observation_id = (SELECT max(observation_id) FROM effective_observations)
                  AND invalidated_at IS NULL AND valid_until > ?
            """, (stamp, stamp))

    def save_screen(self, screen: ScreenObservation) -> None:
        data = asdict(screen)
        data["captured_at"] = _timestamp(screen.captured_at)
        with self.connection:
            self.connection.execute("""
                INSERT INTO screen_observations
                VALUES (:capture_id, :activity_id, :captured_at, :status, :model_id,
                        :screen_kind, :visible_text_redacted, :screen_summary)
            """, data)

    def recent_screens(self, since: datetime) -> list[dict]:
        rows = self.connection.execute(
            "SELECT * FROM screen_observations WHERE captured_at >= ? ORDER BY captured_at, rowid",
            (_timestamp(since),),
        )
        return [dict(row) for row in rows]

    def save_observation(self, observation: Observation, *, published_at: datetime) -> None:
        data = observation.to_dict()
        data["reason_codes"] = json.dumps(data["reason_codes"], separators=(",", ":"))
        data["observed_at"] = _timestamp(observation.observed_at)
        data["valid_until"] = _timestamp(observation.valid_until)
        data["published_at"] = _timestamp(published_at)
        with self.connection:
            self.connection.execute("""
                INSERT INTO effective_observations
                    (activity_kind, state, summary, reason_codes, observed_at, valid_until,
                     context_revision, published_at)
                VALUES (:activity_kind, :state, :summary, :reason_codes, :observed_at, :valid_until,
                        :context_revision, :published_at)
            """, data)

    def counts(self) -> dict:
        activities = self.connection.execute("SELECT count(*) FROM activities").fetchone()[0]
        screens = dict(self.connection.execute(
            "SELECT status, count(*) FROM screen_observations GROUP BY status"
        ).fetchall())
        observations = self.connection.execute("SELECT count(*) FROM effective_observations").fetchone()[0]
        return {"activities": activities, "screens": screens, "observations": observations}


def run_replay(
    recording: Path, database: Path, vision: ScreenModel, observer: StateObserver,
    *, interval_s: float, max_frames: int,
) -> dict:
    frames = recording_frames(recording, interval_s=interval_s, max_frames=max_frames)
    outcomes: Counter[str] = Counter()
    latest = None
    with ObservationStore(database) as store:
        recorder = ActivityRecorder(store)
        previous_at = REPLAY_EPOCH
        for frame in frames:
            captured_at = REPLAY_EPOCH + timedelta(seconds=frame.seconds)
            activity = recorder.add_recording_interval(previous_at, captured_at, capture_status="captured")
            previous_at = captured_at
            screen = analyze_screen(
                vision, image=frame.jpeg, activity_id=activity.activity_id,
                capture_id=uuid4().hex, captured_at=captured_at,
            )
            store.save_screen(screen)
            since = captured_at - timedelta(seconds=observer.window_seconds)
            activities = store.list_activities(since=since)
            screens = store.recent_screens(since)
            observation = observer.observe(activities, screens, as_of=captured_at, now=lambda: captured_at)
            outcomes["model" if observation is not None else "fallback"] += 1
            if observation is None:
                observation = observer.fallback(activities, screens, as_of=captured_at)
            if observation is not None:
                store.save_observation(observation, published_at=captured_at)
                latest = observation.to_dict()
        return {**store.counts(), "state_runs": dict(outcomes), "last_observation": latest}


def _desktop_identity(sample: ActivitySample) -> tuple:
    return (
        sample.session_id, sample.screen_locked, sample.private_window,
        sample.app_name, sample.window_id, sample.window_title, sample.url,
    )


def _live_snapshot(
    store: ObservationStore, metrics: deque[dict], observer: StateObserver,
    as_of: datetime, *, privacy_hidden: bool,
):
    since = as_of - timedelta(seconds=observer.window_seconds)
    while metrics and metrics[0]["started_at"] < since:
        metrics.popleft()
    activities = [] if privacy_hidden else [*store.list_activities(since=since), *metrics]
    screens = [] if privacy_hidden else store.recent_screens(since)
    signature = context_revision(
        activities, screens, as_of=as_of, window_seconds=observer.window_seconds,
        ttl_seconds=observer.ttl_seconds,
    )
    return activities, screens, signature


def run_live(
    database: Path, vision: ScreenModel, observer: StateObserver, *, duration_s: float,
    capture_screen: bool = False, browser_metadata: bool = False,
    model_interval_s: float = LIVE_UPDATE_INTERVAL_S,
    excluded_apps: tuple[str, ...] = (), excluded_domains: tuple[str, ...] = (),
) -> dict:
    if any(not math.isfinite(value) or value <= 0 for value in (duration_s, model_interval_s)):
        raise ValueError("Live duration and model interval must be positive")

    sampler = NativeActivitySampler(browser_metadata=browser_metadata)
    metrics: deque[dict] = deque()
    outcomes: Counter[str] = Counter()
    latest: Observation | None = None
    current_signature = None
    published_signature = None
    pending_signature = None
    privacy_hidden = False
    last_request_at = float("-inf")
    screen_job: Future[ScreenObservation] | None = None
    state_job: Future[Observation | None] | None = None

    def signature() -> str | None:
        return current_signature

    database = Path(database).resolve()
    with (
        _live_writer(database), ObservationStore(database, live=True) as store,
        ThreadPoolExecutor(max_workers=2) as workers,
    ):
        revision_base = store.begin_live_run(datetime.now(UTC))
        recorder = ActivityRecorder(
            store, platform=sampler.platform, capture_enabled=capture_screen,
            excluded_apps=excluded_apps, excluded_domains=excluded_domains,
        )
        end_at = time.monotonic() + duration_s
        next_poll_at = time.monotonic()

        def publish_round(result: Observation | None, activities: list, screens: list, as_of: datetime) -> None:
            nonlocal latest, published_signature
            if result is not None and pending_signature == current_signature and result.valid_until > as_of:
                outcomes["model"] += 1
            else:
                outcomes["discarded"] += 1
                result = observer.fallback(activities, screens, as_of=as_of)
                if result is None:
                    return
                outcomes["fallback"] += 1
            result = replace(result, context_revision=result.context_revision + revision_base)
            store.save_observation(result, published_at=as_of)
            latest = result
            published_signature = current_signature

        try:
            while time.monotonic() < end_at:
                if screen_job is not None and screen_job.done():
                    store.save_screen(screen_job.result())
                    screen_job = None
                sample = sampler.sample()
                recorder.capture_enabled = capture_screen and screen_job is None and sample.screen_locked is False
                request = recorder.observe(sample)
                activity = recorder.current_activity
                privacy_hidden = recorder.last_capture_reason == "privacy_excluded"
                if activity and activity.system_sampled_at == sample.observed_at:
                    metrics.append({
                        "started_at": sample.observed_at, "ended_at": sample.observed_at,
                        "system_sampled_at": sample.observed_at, "idle_s": activity.idle_s,
                        "screen_locked": activity.screen_locked,
                    })

                if request is not None:
                    try:
                        acquired_at, image = capture_desktop()
                    except (OSError, RuntimeError):
                        store.set_capture_status(request.activity.activity_id, "failed")
                    else:
                        # The desktop can switch between polling and acquisition. Keep that
                        # frame local and discard it unless the foreground still matches.
                        after_capture = sampler.sample()
                        if _desktop_identity(sample) == _desktop_identity(after_capture):
                            store.set_capture_status(request.activity.activity_id, "captured")
                            screen_job = workers.submit(
                                analyze_screen, vision, image=image, capture_id=uuid4().hex,
                                activity_id=request.activity.activity_id, captured_at=acquired_at,
                            )
                        del image

                as_of = datetime.now(UTC)
                activities, screens, current_signature = _live_snapshot(
                    store, metrics, observer, as_of, privacy_hidden=privacy_hidden,
                )

                if latest is not None and (published_signature != current_signature or latest.valid_until <= as_of):
                    store.invalidate_current(as_of)
                    latest = None

                if state_job is not None and state_job.done():
                    publish_round(state_job.result(), activities, screens, as_of)
                    state_job = None

                request_at = time.monotonic()
                if state_job is None and request_at - last_request_at >= model_interval_s:
                    pending_signature = current_signature
                    last_request_at = request_at
                    state_job = workers.submit(
                        observer.observe, activities, screens, as_of=as_of,
                        current_context_revision=signature, now=lambda: datetime.now(UTC),
                    )
                next_poll_at = max(next_poll_at + recorder.poll_interval_s, time.monotonic())
                time.sleep(max(0.0, min(end_at, next_poll_at) - time.monotonic()))
        finally:
            recorder.close()
        # Complete already acquired images so their timestamps and terminal status stay inspectable.
        if screen_job is not None:
            store.save_screen(screen_job.result())
        result = state_job.result() if state_job is not None else None
        as_of = datetime.now(UTC)
        activities, screens, current_signature = _live_snapshot(
            store, metrics, observer, as_of, privacy_hidden=privacy_hidden,
        )
        if latest is not None and published_signature != current_signature:
            store.invalidate_current(as_of)
            latest = None
        if state_job is not None:
            publish_round(result, activities, screens, as_of)
        active = latest is not None and published_signature == current_signature and latest.valid_until > as_of
        return {
            **store.counts(), "state_runs": dict(outcomes), "last_observation": (
                latest.to_dict() if active else None
            ), "native_probe_errors": sampler.errors,
        }
