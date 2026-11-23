"""Standalone Observation runs and SQLite persistence."""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
import json
import math
import os
import sqlite3
from pathlib import Path
from threading import Event
import time
from uuid import uuid4

from .activity import Activity, ActivityRecorder, ActivitySample, ActivityStore, NativeActivitySampler
from .context import _timestamp
from .media import capture_desktop, recording_frames
from .realtime import boot_id as system_boot_id, clocks_agree
from .state import Observation, StateObserver, context_revision
from .vision import ScreenModel, ScreenObservation, analyze_screen

REPLAY_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)
LIVE_UPDATE_INTERVAL_S = 30.0
LIVE_VISION_WAIT_S = 2.0
_JOB_CHECK_INTERVAL_S = 0.05


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
    def __init__(self, path: Path, *, live: bool = False, boot_id: str | None = None):
        path = Path(path).resolve()
        self._live = live
        self._boot_id = boot_id
        self._poll_interval_s = None
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
            if live:
                self.connection.execute("PRAGMA journal_mode = WAL")
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
            if live:
                self.connection.execute("""
                    CREATE TABLE IF NOT EXISTS realtime_observation_head (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        generation TEXT NOT NULL,
                        policy_generation INTEGER,
                        boot_id TEXT,
                        sampled_at TEXT,
                        sampled_monotonic_s REAL,
                        sample_deadline_s REAL,
                        publication_cursor INTEGER REFERENCES effective_observations (observation_id),
                        published_monotonic_s REAL,
                        valid_until_monotonic_s REAL
                    )
                """)
                if not any(row["name"] == "policy_generation" for row in
                           self.connection.execute("PRAGMA table_info(realtime_observation_head)")):
                    self.connection.execute("ALTER TABLE realtime_observation_head ADD COLUMN policy_generation INTEGER")
        except Exception:
            self.close()
            raise

    def begin_live_run(
        self, as_of: datetime, *, monotonic_s: float | None = None, poll_interval_s: float = 1.5,
        policy_generation: int | None = None,
    ) -> int:
        if not self._live:
            raise ValueError("Realtime production requires a live Observation database")
        self._poll_interval_s = poll_interval_s
        # The estimator's counters are run-local. Persist a unique revision
        # range for each producer start so a restart cannot revive a decision.
        with self.connection:
            self._invalidate_current(as_of)
            previous = self.connection.execute("""
                SELECT observation_id, context_revision FROM effective_observations ORDER BY observation_id DESC LIMIT 1
            """).fetchone()
            self.connection.execute("""
                INSERT OR REPLACE INTO realtime_observation_head
                (singleton, generation, boot_id, publication_cursor, policy_generation)
                VALUES (1, ?, ?, ?, ?)
            """, (uuid4().hex, self._boot_id or system_boot_id(),
                  previous[0] if previous is not None else None, policy_generation))
            if monotonic_s is not None:
                self._record_live_sample(as_of, monotonic_s)
        return previous[1] if previous is not None else 0

    def invalidate_current(self, as_of: datetime) -> None:
        with self.connection:
            self._invalidate_current(as_of)

    def _invalidate_current(self, as_of: datetime) -> None:
        """Record invalidation inside the caller's transaction."""
        latest = self.connection.execute("""
            SELECT observation_id, published_at, invalidated_at FROM effective_observations
            ORDER BY observation_id DESC LIMIT 1
        """).fetchone()
        if latest is None or latest["invalidated_at"] is not None:
            return
        published = datetime.fromisoformat(latest["published_at"])
        if as_of < published:
            # A clock rollback invalidates realtime immediately. Keep the
            # earlier publication visible at its original historical instant.
            as_of = published + timedelta(microseconds=1)
        self.connection.execute("""
            UPDATE effective_observations SET invalidated_at = ? WHERE observation_id = ?
        """, (_timestamp(as_of), latest["observation_id"]))

    def _record_live_sample(self, as_of: datetime, monotonic_s: float) -> None:
        """Refresh the source clock evidence inside the Activity transaction."""
        self.connection.execute("""
            UPDATE realtime_observation_head
            SET sampled_at = ?, sampled_monotonic_s = ?, sample_deadline_s = ?
            WHERE singleton = 1
        """, (_timestamp(as_of), monotonic_s, monotonic_s + 3 * self._poll_interval_s))

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

    def save_observation(
        self, observation: Observation, *, published_at: datetime, monotonic_s: float | None = None,
    ) -> None:
        data = observation.to_dict()
        data["reason_codes"] = json.dumps(data["reason_codes"], separators=(",", ":"))
        data["observed_at"] = _timestamp(observation.observed_at)
        data["valid_until"] = _timestamp(observation.valid_until)
        data["published_at"] = _timestamp(published_at)
        with self.connection:
            cursor = self.connection.execute("""
                INSERT INTO effective_observations
                    (activity_kind, state, summary, reason_codes, observed_at, valid_until,
                     context_revision, published_at)
                VALUES (:activity_kind, :state, :summary, :reason_codes, :observed_at, :valid_until,
                        :context_revision, :published_at)
            """, data)
            if self._live:
                deadline = None
                if monotonic_s is not None:
                    deadline = monotonic_s + (observation.valid_until - published_at).total_seconds()
                self.connection.execute("""
                    UPDATE realtime_observation_head
                    SET publication_cursor = ?, published_monotonic_s = ?, valid_until_monotonic_s = ?
                    WHERE singleton = 1
                """, (cursor.lastrowid, monotonic_s, deadline))

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


@dataclass
class _VisionJob:
    capture_id: str
    captured_at: datetime
    future: Future[ScreenObservation]


@dataclass
class _LiveRound:
    deadline: datetime
    state_deadline_s: float
    vision_deadline_s: float
    capture_id: str | None
    changed: bool = False
    signature: str | None = None


def run_live(
    database: Path, vision: ScreenModel, observer: StateObserver, *, duration_s: float | None,
    capture_screen: bool | Callable[[], bool] = False, browser_metadata: bool = False,
    model_interval_s: float = LIVE_UPDATE_INTERVAL_S, vision_wait_s: float = LIVE_VISION_WAIT_S,
    excluded_apps: tuple[str, ...] = (), excluded_domains: tuple[str, ...] = (),
    stop_event: Event | None = None, capture_policy: Callable[[], int | None] | None = None,
    producer_policy: Callable[[], int | None] | None = None, policy_generation: int | None = None,
) -> dict:
    if (not math.isfinite(model_interval_s) or model_interval_s <= 0
            or duration_s is not None and (not math.isfinite(duration_s) or duration_s <= 0)):
        raise ValueError("Live duration and model interval must be positive")
    if not math.isfinite(vision_wait_s) or vision_wait_s < 0:
        raise ValueError("Live vision wait must be finite and nonnegative")

    from .media import capture_window, cloud_window_eligible
    from .openai_cloud import LOCAL_CAPTURE, prepare_vision_capture

    def capture_enabled() -> bool:
        try:
            return capture_screen() if callable(capture_screen) else capture_screen
        except (OSError, ValueError, sqlite3.Error):
            return False

    def capture_stamp() -> int | None:
        try:
            return capture_policy() if capture_policy is not None else 0
        except (OSError, ValueError, sqlite3.Error):
            return None

    if producer_policy is not None and policy_generation is None:
        policy_generation = producer_policy()

    def stop_requested() -> bool:
        return (stop_event is not None and stop_event.is_set()
                or producer_policy is not None
                and (policy_generation is None or producer_policy() != policy_generation))

    sampler = NativeActivitySampler(browser_metadata=browser_metadata)
    metrics: deque[dict] = deque()
    outcomes: Counter[str] = Counter()
    latest: Observation | None = None
    current_signature = None
    published_signature = None
    active_screens: list[dict] = []
    activity_floor: int | None = None
    live_activity_id: int | None = None
    last_sample: ActivitySample | None = None
    last_request_at = float("-inf")
    screen_job: _VisionJob | None = None
    state_job: Future[Observation | None] | None = None
    active_round: _LiveRound | None = None

    database = Path(database).resolve()
    with (
        _live_writer(database), ObservationStore(database, live=True) as store,
        ThreadPoolExecutor(max_workers=2) as workers,
    ):
        def record_sample(sample: ActivitySample, activity: Activity | None, boundary: bool, switched: bool) -> None:
            nonlocal activity_floor, last_sample, latest, active_screens, current_signature, live_activity_id
            live_activity_id = activity.activity_id if activity is not None else None
            if boundary:
                activity_floor = live_activity_id
                active_screens = []
                metrics.clear()
                if last_sample is not None and sample.observed_at < last_sample.observed_at:
                    observer.reset_live_context()
            if switched or boundary:
                store._invalidate_current(sample.observed_at)
                latest = None
                current_signature = None
                if active_round is not None:
                    active_round.changed = True
            store._record_live_sample(sample.observed_at, sample.monotonic_s)
            last_sample = sample
            if activity is not None:
                metrics.append({
                    "started_at": sample.observed_at, "ended_at": sample.observed_at,
                    "system_sampled_at": sample.system_sampled_at or sample.observed_at,
                    "idle_s": sample.idle_s, "screen_locked": sample.screen_locked,
                })

        recorder = ActivityRecorder(
            store, platform=sampler.platform, excluded_apps=excluded_apps, excluded_domains=excluded_domains,
            on_sample_write=record_sample, reset_wall_on_rollback=True,
        )
        revision_base = store.begin_live_run(
            datetime.now(UTC), poll_interval_s=recorder.poll_interval_s, policy_generation=policy_generation,
        )
        next_poll_at = time.monotonic()
        end_at = next_poll_at + duration_s if duration_s is not None else math.inf

        def activities_at(at: datetime) -> list:
            since = at - timedelta(seconds=observer.window_seconds)
            while metrics and metrics[0]["started_at"] < since:
                metrics.popleft()
            if activity_floor is None:
                return []
            activities = [row for row in store.list_activities(since=since) if row.activity_id >= activity_floor]
            return [*activities, *metrics]

        def refresh(at: datetime) -> list:
            nonlocal current_signature, latest
            activities = activities_at(at)
            current_signature = context_revision(
                activities, active_screens, as_of=at, window_seconds=observer.window_seconds,
                ttl_seconds=observer.ttl_seconds, live=True,
            )
            if latest is not None:
                if latest.valid_until <= at:
                    latest = None
                elif published_signature != current_signature:
                    store.invalidate_current(at)
                    latest = None
            return activities

        def coverage_is_current(at: datetime, mono: float) -> bool:
            return (
                last_sample is not None and mono - last_sample.monotonic_s <= 3 * recorder.poll_interval_s
                and clocks_agree(at, mono, last_sample.observed_at, last_sample.monotonic_s)
            )

        def publish_round(round_: _LiveRound, result: Observation | None, at: datetime, mono: float) -> None:
            nonlocal latest, published_signature, active_round
            activities = refresh(at)
            active_round = None
            if stop_requested() or not coverage_is_current(at, mono) or at >= round_.deadline:
                outcomes["discarded"] += 1
                return
            if (result is not None and not round_.changed and round_.signature == current_signature
                    and result.valid_until > at):
                outcomes["model"] += 1
            else:
                outcomes["discarded"] += 1
                result = observer.fallback(activities, active_screens, as_of=at, live=True)
                if result is None:
                    return
                outcomes["fallback"] += 1
            result = replace(result, context_revision=result.context_revision + revision_base,
                             valid_until=min(result.valid_until, round_.deadline))
            store.save_observation(result, published_at=at, monotonic_s=mono)
            latest = result
            published_signature = current_signature

        def observe_sample(sample: ActivitySample, *, acquire: bool) -> _VisionJob | None:
            previous = last_sample
            if previous is not None and (sample.observed_at, sample.monotonic_s) != (previous.observed_at, previous.monotonic_s):
                if not clocks_agree(sample.observed_at, sample.monotonic_s, previous.observed_at, previous.monotonic_s):
                    recorder.close()
            stamp = capture_stamp() if acquire and capture_enabled() and sample.screen_locked is False else None
            capture_owner = None

            def permitted() -> bool:
                return (stamp is not None and not stop_requested() and capture_enabled() and capture_stamp() == stamp
                        and (capture_owner is None or capture_owner == live_activity_id))

            grant = prepare_vision_capture(vision, permitted=permitted) if stamp is not None else None
            cloud = grant is not None and grant is not LOCAL_CAPTURE
            scoped_window = cloud or capture_policy is not None

            def eligible(candidate: ActivitySample) -> bool:
                return not scoped_window or cloud_window_eligible(
                    candidate, browser_url_required=bool(excluded_domains), platform=sampler.platform,
                )

            recorder.capture_enabled = grant is not None and eligible(sample)
            request = recorder.observe(sample)
            if request is None:
                return None
            capture_owner = request.activity.activity_id
            if not permitted():
                return None

            try:
                if scoped_window:
                    acquired_at, image = capture_window(sample, platform=sampler.platform)
                else:
                    acquired_at, image = capture_desktop()
            except (OSError, RuntimeError):
                store.set_capture_status(request.activity.activity_id, "failed")
                return None
            after_capture = sampler.sample()
            if (_desktop_identity(sample) != _desktop_identity(after_capture)
                    or not clocks_agree(after_capture.observed_at, after_capture.monotonic_s,
                                        sample.observed_at, sample.monotonic_s)
                    or after_capture.monotonic_s - sample.monotonic_s > 3 * recorder.poll_interval_s):
                # Commit any lock or switch observed during capture before continuing.
                observe_sample(after_capture, acquire=False)
                return None
            if not eligible(after_capture) or not permitted():
                return None
            store.set_capture_status(request.activity.activity_id, "captured")
            capture_id = uuid4().hex
            grant_args = {"image_grant": grant} if cloud else {}
            job = workers.submit(
                analyze_screen, vision, image=image, capture_id=capture_id,
                activity_id=request.activity.activity_id, captured_at=acquired_at, **grant_args,
            )
            return _VisionJob(capture_id, acquired_at, job)

        def eligible_screens(at: datetime) -> list[dict]:
            if activity_floor is None:
                return []
            since = at - timedelta(seconds=observer.window_seconds)
            return [screen for screen in store.recent_screens(since)
                    if screen["activity_id"] >= activity_floor]

        def close_vision() -> tuple[str, float] | None:
            nonlocal screen_job
            if screen_job is None or not screen_job.future.done():
                return None
            completed_at = time.monotonic()
            store.save_screen(screen_job.future.result())
            capture_id = screen_job.capture_id
            screen_job = None
            return capture_id, completed_at

        def advance_round(completed: tuple[str, float] | None) -> None:
            nonlocal state_job, active_screens
            round_ = active_round
            if round_ is None:
                return
            at, mono = datetime.now(UTC), time.monotonic()
            if round_.changed or mono >= round_.state_deadline_s or not coverage_is_current(at, mono):
                publish_round(round_, None, at, mono)
                return
            if state_job is None:
                waiting = round_.capture_id is not None and screen_job is not None and screen_job.capture_id == round_.capture_id
                if waiting and mono < round_.vision_deadline_s:
                    return
                late = waiting or (completed is not None and completed[0] == round_.capture_id
                                   and completed[1] > round_.vision_deadline_s)
                active_screens = [] if late else eligible_screens(at)
                activities = refresh(at)
                round_.signature = current_signature

                def signature() -> str | None:
                    return current_signature if active_round is round_ and not round_.changed and not stop_requested() else None

                state_job = workers.submit(
                    observer.observe, activities, active_screens, as_of=at,
                    current_context_revision=signature, now=lambda: datetime.now(UTC), live=True,
                )
            if state_job.done():
                result, state_job = state_job.result(), None
                publish_round(round_, result, datetime.now(UTC), time.monotonic())

        try:
            while not stop_requested():
                mono = time.monotonic()
                if mono >= end_at:
                    break
                completed = close_vision()
                if active_round is None and state_job is not None and state_job.done():
                    state_job.result()
                    state_job = None
                if mono >= next_poll_at:
                    sample = sampler.sample()
                    if stop_requested():
                        break
                    captured = observe_sample(sample, acquire=screen_job is None)
                    if captured is not None:
                        screen_job = captured
                    refresh(datetime.now(UTC))
                    if (not stop_requested() and active_round is None and state_job is None
                            and mono - last_request_at >= model_interval_s):
                        last_request_at = mono
                        deadline = sample.observed_at + timedelta(seconds=observer.ttl_seconds)
                        capture_id = captured.capture_id if captured is not None else None
                        if captured is not None:
                            deadline = min(deadline, captured.captured_at + timedelta(seconds=observer.ttl_seconds))
                        at = datetime.now(UTC)
                        remaining = (deadline - at).total_seconds()
                        # Reserve part of the evidence lifetime for text inference.
                        budget = min(vision_wait_s, max(0.0, remaining - observer.ttl_seconds / 2))
                        started_s = time.monotonic()
                        finish_s = started_s + remaining - min(_JOB_CHECK_INTERVAL_S, observer.ttl_seconds / 4)
                        active_round = _LiveRound(deadline, finish_s, started_s + budget, capture_id)
                    next_poll_at = max(next_poll_at + recorder.poll_interval_s, time.monotonic())
                # A completed vision request can dispatch text immediately, even
                # while the next native sample is still scheduled in the future.
                completed = close_vision() or completed
                advance_round(completed)
                wake = min(end_at, next_poll_at)
                if screen_job is not None or state_job is not None:
                    wake = min(wake, time.monotonic() + _JOB_CHECK_INTERVAL_S)
                if active_round is not None and state_job is None:
                    wake = min(wake, active_round.vision_deadline_s)
                if active_round is not None:
                    wake = min(wake, active_round.state_deadline_s)
                wait_s = max(0.0, wake - time.monotonic())
                if stop_event is None:
                    time.sleep(wait_s)
                else:
                    stop_event.wait(wait_s)
        finally:
            recorder.close()

        # Already acquired results remain inspectable at their original capture
        # time. Shutdown never turns these records into a new live publication.
        if stop_requested():
            store.invalidate_current(datetime.now(UTC))
            latest = None
        if screen_job is not None:
            store.save_screen(screen_job.future.result())
        as_of = datetime.now(UTC)
        return {
            **store.counts(), "state_runs": dict(outcomes), "last_observation": (
                latest.to_dict() if latest is not None and latest.valid_until > as_of
                and coverage_is_current(as_of, time.monotonic()) else None
            ), "native_probe_errors": sampler.errors,
        }
