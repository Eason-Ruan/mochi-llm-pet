"""Standalone Observation runs and SQLite persistence."""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack, closing, contextmanager, nullcontext
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
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
from .documents import is_mochi_app
from .media import capture_desktop, recording_frames
from .realtime import boot_id as system_boot_id, clocks_agree
from .routes import CloudRouteError, CloudRouteSnapshot
from .sampling import ACTIVITY_POLL_S, CaptureSchedule, frame_thumbnail, known_video_source
from .state import Observation, StateObserver, context_revision, local_outcome
from .vision import MultimodalScreenObservation, ScreenModel, ScreenObservation, analyze_screen

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
            self._add_columns("screen_observations", {
                "processed_at": "TEXT", "activity_kind": "TEXT", "scene_elements": "TEXT",
                "visible_changes": "TEXT", "next_check": "TEXT", "document_name": "TEXT",
                "document_source": "TEXT", "filtered_fields": "TEXT",
                "realtime_eligible": "INTEGER NOT NULL DEFAULT 1",
            })
            if live:
                self._add_columns("effective_observations", {
                    "foreground_key": "TEXT", "producer_generation": "TEXT", "screen_locked": "INTEGER",
                })
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
                self.connection.execute("""
                    CREATE TABLE IF NOT EXISTS realtime_observation_departure (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        serial INTEGER NOT NULL, through_cursor INTEGER NOT NULL,
                        reason TEXT NOT NULL, boot_id TEXT, policy_generation INTEGER
                    )
                """)
                self._add_columns("realtime_observation_head", {
                    "policy_generation": "INTEGER", "activity_id": "INTEGER", "app_name": "TEXT",
                    "screen_locked": "INTEGER", "idle_s": "REAL", "system_sampled_at": "TEXT",
                    "lock_epoch": "INTEGER NOT NULL DEFAULT 0",
                })
                self.connection.execute("""
                    CREATE TABLE IF NOT EXISTS observation_capture_health (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        status TEXT NOT NULL, sampled_at TEXT NOT NULL
                    )
                """)
                self._add_columns("observation_capture_health", {
                    "reason": "TEXT", "last_ready_at": "TEXT", "last_failure_at": "TEXT", "last_skipped_at": "TEXT",
                })
                self.connection.execute("""
                    CREATE TABLE IF NOT EXISTS observation_upload_budget (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        boot_id TEXT, attempted_at TEXT NOT NULL, attempted_monotonic_s REAL NOT NULL
                    )
                """)
        except Exception:
            self.close()
            raise

    def _add_columns(self, table: str, columns: dict[str, str]) -> None:
        existing = {row["name"] for row in self.connection.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in existing:
                self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

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

    def _record_live_departure(self, reason: str) -> None:
        """Persist a positive departure with the current publication watermark."""
        self.connection.execute("""
            INSERT INTO realtime_observation_departure
            SELECT 1, 1, coalesce(publication_cursor, 0), ?, boot_id, policy_generation
            FROM realtime_observation_head WHERE singleton = 1
            ON CONFLICT(singleton) DO UPDATE SET serial = serial + 1,
                through_cursor = excluded.through_cursor, reason = excluded.reason,
                boot_id = excluded.boot_id, policy_generation = excluded.policy_generation
        """, (reason,))

    def _record_live_sample(
        self, as_of: datetime, monotonic_s: float, *, sample: ActivitySample | None = None,
        activity: Activity | None = None,
    ) -> None:
        """Refresh the source clock evidence inside the Activity transaction."""
        self.connection.execute("""
            UPDATE realtime_observation_head
            SET sampled_at = ?, sampled_monotonic_s = ?, sample_deadline_s = ?,
                lock_epoch = lock_epoch + CASE WHEN ? = 1 AND screen_locked IS NOT 1 THEN 1 ELSE 0 END,
                activity_id = ?, app_name = ?, screen_locked = ?, idle_s = ?, system_sampled_at = ?
            WHERE singleton = 1
        """, (
            _timestamp(as_of), monotonic_s, monotonic_s + 3 * self._poll_interval_s,
            sample.screen_locked if sample else None,
            activity.activity_id if activity else None, activity.app_name if activity else None,
            sample.screen_locked if sample else None, sample.idle_s if sample else None,
            _timestamp(sample.system_sampled_at) if sample and sample.system_sampled_at else None,
        ))

    def save_capture_health(
        self, status: str, at: datetime, *, reason: str | None = None, preserve_status: bool = False,
    ) -> None:
        timestamp = _timestamp(at)
        ready = timestamp if status == "ready" else None
        failure = timestamp if status in {"capture_failed", "model_failed"} else None
        skipped = timestamp if status in {"skipped", "source_restricted"} else None
        with self.connection:
            self.connection.execute("""
                INSERT INTO observation_capture_health
                    (singleton, status, sampled_at, reason, last_ready_at, last_failure_at, last_skipped_at)
                VALUES (1, :status, :at, :reason, :ready, :failure, :skipped)
                ON CONFLICT(singleton) DO UPDATE SET
                    status = CASE WHEN :preserve THEN status ELSE excluded.status END,
                    sampled_at = CASE WHEN :preserve THEN sampled_at ELSE excluded.sampled_at END,
                    reason = CASE WHEN :preserve THEN reason ELSE excluded.reason END,
                    last_ready_at = coalesce(excluded.last_ready_at, last_ready_at),
                    last_failure_at = coalesce(excluded.last_failure_at, last_failure_at),
                    last_skipped_at = coalesce(excluded.last_skipped_at, last_skipped_at)
            """, dict(status=status, at=timestamp, reason=reason, ready=ready, failure=failure,
                      skipped=skipped, preserve=preserve_status))

    def last_upload(self, at: datetime, mono: float) -> float:
        previous = self.connection.execute("SELECT * FROM observation_upload_budget WHERE singleton = 1").fetchone()
        if previous is None:
            return float("-inf")
        identity = self._boot_id or system_boot_id()
        if identity is not None and previous["boot_id"] == identity and previous["attempted_monotonic_s"] <= mono:
            return previous["attempted_monotonic_s"]
        elapsed = (at - datetime.fromisoformat(previous["attempted_at"])).total_seconds()
        return mono - max(0.0, elapsed)

    def record_upload(self, at: datetime, mono: float) -> None:
        with self.connection:
            self.connection.execute("""
                INSERT OR REPLACE INTO observation_upload_budget VALUES (1, ?, ?, ?)
            """, (self._boot_id or system_boot_id(), _timestamp(at), mono))

    def save_screen(
        self, screen: ScreenObservation, *, processed_at: datetime | None = None,
        realtime_eligible: bool = True, document_source: str | None = None,
    ) -> None:
        data = asdict(screen)
        data["captured_at"] = _timestamp(screen.captured_at)
        data["processed_at"] = _timestamp(processed_at) if processed_at else None
        data["realtime_eligible"] = int(realtime_eligible)
        data["document_source"] = document_source if data.get("document_name") else None
        for field in ("scene_elements", "visible_changes", "filtered_fields"):
            data[field] = json.dumps(data[field], ensure_ascii=False) if field in data else None
        for field in ("activity_kind", "next_check", "document_name"):
            data.setdefault(field, None)
        with self.connection:
            self.connection.execute("""
                INSERT INTO screen_observations
                    (capture_id, activity_id, captured_at, status, model_id,
                     screen_kind, visible_text_redacted, screen_summary, processed_at,
                     activity_kind, scene_elements, visible_changes, next_check,
                     document_name, document_source, filtered_fields, realtime_eligible)
                VALUES (:capture_id, :activity_id, :captured_at, :status, :model_id,
                        :screen_kind, :visible_text_redacted, :screen_summary, :processed_at,
                        :activity_kind, :scene_elements, :visible_changes, :next_check,
                        :document_name, :document_source, :filtered_fields, :realtime_eligible)
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
                self._record_publication_evidence(cursor.lastrowid)
                self.connection.execute("""
                    UPDATE realtime_observation_head
                    SET publication_cursor = ?, published_monotonic_s = ?, valid_until_monotonic_s = ?
                    WHERE singleton = 1
                """, (cursor.lastrowid, monotonic_s, deadline))
                if (observation.state in {"working", "away"} and monotonic_s is not None
                        and observation.observed_at <= published_at < observation.valid_until):
                    self._record_live_departure(observation.state)

    def _record_publication_evidence(self, cursor):
        # Correlate a pending decision with the exact sample it received. Keep
        # only an opaque local foreground fingerprint and the sampled lock flag.
        row = self.connection.execute("""
            SELECT h.generation, h.app_name, h.screen_locked, a.domain, a.document_status
            FROM realtime_observation_head h LEFT JOIN activities a ON a.activity_id=h.activity_id
            WHERE h.singleton=1
        """).fetchone()
        if row is None:
            return
        app = row["app_name"]
        key = None
        if (row["screen_locked"] == 0 and app and not is_mochi_app(app)
                and row["document_status"] != "private_window"):
            key = sha256(json.dumps([app.casefold(), row["domain"]]).encode()).hexdigest()
        self.connection.execute("""
            UPDATE effective_observations SET producer_generation=?, foreground_key=?, screen_locked=?
            WHERE observation_id=?
        """, (row["generation"], key, row["screen_locked"], cursor))

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


def run_replay_multimodal(
    recording: Path, database: Path, vision: ScreenModel, *, interval_s: float, max_frames: int,
    observer: StateObserver | None = None,
) -> dict:
    """Replay visual candidates on the media clock using the live sampling budget."""
    from .vision import VisionSignals, analyze_multimodal

    frames = recording_frames(recording, interval_s=interval_s, max_frames=max_frames)
    observer = observer or StateObserver()
    schedule = CaptureSchedule()
    schedule.observe("recording", frames[0].seconds)
    latest = previous = None
    attempts = 0
    next_update = frames[0].seconds
    outcomes: Counter[str] = Counter()
    with ObservationStore(database) as store:
        recorder = ActivityRecorder(store)
        activity = recorder.add_recording_interval(
            REPLAY_EPOCH, REPLAY_EPOCH + timedelta(seconds=frames[-1].seconds),
        )
        for frame in frames:
            at = REPLAY_EPOCH + timedelta(seconds=frame.seconds)
            update = frame.seconds >= next_update
            if update:
                next_update = frame.seconds + LIVE_UPDATE_INTERVAL_S
            outcome = "local"
            if schedule.due(frame.seconds, interactive=True) and schedule.inspect(frame_thumbnail(frame.jpeg), frame.seconds):
                schedule.sent(frame.seconds)
                attempts += 1
                store.set_capture_status(activity.activity_id, "captured")
                screen = analyze_multimodal(
                    vision, image=frame.jpeg, activity_id=activity.activity_id,
                    capture_id=uuid4().hex, captured_at=at, signals=VisionSignals(previous=previous),
                )
                store.save_screen(screen, processed_at=at)
                if screen.status == "ready":
                    previous, update, outcome = screen, True, "model"
                    schedule.recommend(screen.next_check, frame.seconds, video=screen.screen_kind == "video")
            if update:
                result = observer.observe_local(
                    [], [previous] if previous is not None else [], as_of=at, live=False,
                    activity_kind=previous.activity_kind if previous is not None else "unknown",
                )
                if result is not None:
                    store.save_observation(result, published_at=at)
                    latest = result.to_dict()
                    outcomes[outcome] += 1
        return {**store.counts(), "visual_attempts": attempts, "state_runs": dict(outcomes), "last_observation": latest}


def _desktop_identity(sample: ActivitySample) -> tuple:
    return (
        sample.session_id, sample.screen_locked, sample.private_window,
        sample.app_name, sample.window_id, sample.window_title, sample.url,
        sample.app_id, sample.active_tab_title,
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
    """Exercise the legacy two-model pipeline with injected evaluation fixtures."""
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
            store._record_live_sample(sample.observed_at, sample.monotonic_s, sample=sample, activity=activity)
            if sample.screen_locked is True and (last_sample is None or last_sample.screen_locked is not True):
                store._record_live_departure("locked")
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


@dataclass(frozen=True)
class _MultimodalJob:
    activity_id: int
    document_source: str | None
    video_hint: bool
    permitted: Callable[[], bool]
    owns_scene: Callable[[], bool]
    scope: CloudRouteSnapshot | None
    future: Future[ScreenObservation]


@dataclass(frozen=True)
class _ScopedScreen:
    screen: MultimodalScreenObservation
    scope: CloudRouteSnapshot | None

    def comparable_to(self, scope: CloudRouteSnapshot | None) -> bool:
        if self.scope is None or scope is None:
            return self.scope is scope
        return (self.scope.route_generation, self.scope.consent_generation) == (
            scope.route_generation, scope.consent_generation,
        )


def run_live_multimodal(
    database: Path, vision: ScreenModel, *, duration_s: float | None,
    observer: StateObserver | None = None, capture_screen: bool | Callable[[], bool] = False,
    screen_scope: str = "foreground_window",
    browser_metadata: bool = False, model_interval_s: float = LIVE_UPDATE_INTERVAL_S,
    excluded_apps: tuple[str, ...] = (), excluded_domains: tuple[str, ...] = (),
    document_local_sources: tuple[str, ...] = (), stop_event: Event | None = None,
    capture_policy: Callable[[], int | None] | None = None,
    producer_policy: Callable[[], int | None] | None = None, policy_generation: int | None = None,
    close_vision: Callable[[], None] | None = None,
) -> dict:
    """Publish a local desk state and sample stable, materially changed views."""
    from .media import DesktopCaptureRestricted, capture_filtered_desktop, capture_window, cloud_window_eligible
    from .openai_cloud import LOCAL_CAPTURE, RoutedObservationModel, prepare_vision_capture
    from .vision import VisionSignals, analyze_multimodal

    if (not math.isfinite(model_interval_s) or model_interval_s <= 0
            or duration_s is not None and (not math.isfinite(duration_s) or duration_s <= 0)):
        raise ValueError("Live duration and update interval must be positive and finite")
    if screen_scope not in ("foreground_window", "all_displays"):
        raise ValueError("Invalid live screen scope")
    all_displays = screen_scope == "all_displays"
    routed_vision = vision if isinstance(vision, RoutedObservationModel) else None
    protected_foreground_reasons = {"privacy_excluded", "private_window", "browser_url_unavailable", "mochi_foreground"}

    def admit_scope(scope: CloudRouteSnapshot | None):
        if routed_vision is None or scope is None:
            return nullcontext(True)
        return routed_vision.admit_vision_result(scope)

    def capture_stamp() -> int | None:
        try:
            enabled = capture_screen() if callable(capture_screen) else capture_screen
            if enabled:
                return capture_policy() if capture_policy is not None else 0
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            pass
        return None

    if producer_policy is not None and policy_generation is None:
        policy_generation = producer_policy()

    def stopped() -> bool:
        return (stop_event is not None and stop_event.is_set()
                or producer_policy is not None
                and (policy_generation is None or producer_policy() != policy_generation))

    observer = observer or StateObserver()
    sampler = NativeActivitySampler(
        browser_metadata=browser_metadata, document_local_sources=document_local_sources,
        excluded_apps=excluded_apps, excluded_domains=excluded_domains,
    )
    outcomes: Counter[str] = Counter()
    metrics: deque[dict] = deque()
    activity_floor = None
    activity_id = None
    last_sample = None
    last_desktop_identity = None
    latest = None
    visual = None
    previous_visual = None
    job = None
    uploads = 0
    immediate_update = False

    database = Path(database).resolve()
    with (
        ThreadPoolExecutor(max_workers=1) as workers,
        _live_writer(database), ObservationStore(database, live=True) as store,
        ExitStack() as vision_lifetime,
    ):
        if close_vision is not None:
            vision_lifetime.callback(close_vision)
        revision_base = store.begin_live_run(
            datetime.now(UTC), poll_interval_s=ACTIVITY_POLL_S, policy_generation=policy_generation,
        )
        next_poll = next_update = time.monotonic()
        end_at = next_poll + duration_s if duration_s is not None else math.inf
        schedule = CaptureSchedule(last_upload_s=store.last_upload(datetime.now(UTC), next_poll))

        def record_sample(sample: ActivitySample, activity: Activity | None, boundary: bool, _switched: bool) -> None:
            nonlocal activity_floor, activity_id, last_sample, latest, visual, previous_visual, immediate_update
            prior_activity = activity_id
            activity_id = activity.activity_id if activity is not None else None
            changed = (
                prior_activity != activity_id or last_sample is None
                or sample.screen_locked is not last_sample.screen_locked or sample.session_id != last_sample.session_id
                or sample.monotonic_s - last_sample.monotonic_s > 3 * ACTIVITY_POLL_S
                or not clocks_agree(sample.observed_at, sample.monotonic_s, last_sample.observed_at, last_sample.monotonic_s)
            )
            if boundary:
                activity_floor = activity_id
                metrics.clear()
                if last_sample is not None and sample.observed_at < last_sample.observed_at:
                    observer.reset_live_context()
            if changed:
                store._invalidate_current(sample.observed_at)
                latest = visual = previous_visual = None
                immediate_update = True
            if sample.screen_locked is True and (last_sample is None or last_sample.screen_locked is not True):
                store._record_live_departure("locked")
            store._record_live_sample(sample.observed_at, sample.monotonic_s, sample=sample, activity=activity)
            last_sample = sample
            metrics.append({
                "started_at": sample.observed_at, "ended_at": sample.observed_at,
                "system_sampled_at": sample.system_sampled_at, "idle_s": sample.idle_s,
                "screen_locked": sample.screen_locked,
            })

        recorder = ActivityRecorder(
            store, platform=sampler.platform, poll_interval_s=ACTIVITY_POLL_S,
            excluded_apps=excluded_apps, excluded_domains=excluded_domains, document_local_sources=document_local_sources,
            on_sample_write=record_sample, reset_wall_on_rollback=True,
        )

        def observe_sample(sample: ActivitySample) -> None:
            nonlocal last_desktop_identity
            if all_displays:
                # Only an in-memory comparison is needed to revoke a stale frame.
                last_desktop_identity = sha256(repr(_desktop_identity(sample)).encode(errors="replace")).digest()
            recorder.observe(sample)

        def activities_at(at: datetime) -> list:
            since = at - timedelta(seconds=observer.window_seconds)
            while metrics and metrics[0]["started_at"] < since:
                metrics.popleft()
            activities = (
                [row for row in store.list_activities(since=since) if row.activity_id >= activity_floor]
                if activity_floor is not None else []
            )
            return [*activities, *metrics]

        def coverage_current(at: datetime, mono: float) -> bool:
            return (last_sample is not None and mono - last_sample.monotonic_s <= 3 * ACTIVITY_POLL_S
                    and clocks_agree(at, mono, last_sample.observed_at, last_sample.monotonic_s))

        def refresh(*, publish: bool, outcome: str = "local") -> None:
            nonlocal latest
            at, mono = datetime.now(UTC), time.monotonic()
            if stopped() or not coverage_current(at, mono):
                if latest is not None:
                    store.invalidate_current(at)
                    latest = None
                return
            activities = activities_at(at)
            screens = [visual.screen] if visual is not None else []
            activity_kind = visual.screen.activity_kind if visual is not None else "unknown"
            estimate = local_outcome(observer.features(activities, screens, as_of=at, live=True), activity_kind)
            if latest is not None:
                published = (latest.activity_kind, latest.state, latest.summary, latest.reason_codes)
                if latest.valid_until <= at or estimate != published:
                    store.invalidate_current(at)
                    latest = None
            if not publish:
                # A short availability opportunity can fall between periodic publications.
                publish = latest is None and estimate[1] == "available"
            if not publish:
                return
            result = observer.observe_local(activities, screens, as_of=at, activity_kind=activity_kind)
            if result is not None:
                latest = replace(result, context_revision=result.context_revision + revision_base)
                store.save_observation(latest, published_at=at, monotonic_s=mono)
                outcomes[outcome] += 1

        def refresh_with_cached_screen(*, publish: bool) -> None:
            nonlocal visual, previous_visual
            with admit_scope(visual.scope if visual is not None else None) as authorized:
                if not authorized:
                    visual = previous_visual = None
                refresh(publish=publish)

        def acquire(sample: ActivitySample) -> None:
            nonlocal immediate_update, job, uploads, visual, previous_visual
            activity = recorder.current_activity
            stamp = capture_stamp()
            restriction = recorder.capture_ineligibility
            foreground_excluded = False
            context_only = False
            if all_displays:
                if sampler.platform != "darwin":
                    restriction = "filtered_desktop_unavailable"
                elif sample.screen_locked is False:
                    foreground_excluded = restriction in protected_foreground_reasons
                    context_only = foreground_excluded or restriction == "foreground_unavailable"
                    if context_only:
                        restriction = None
            elif not cloud_window_eligible(
                sample, browser_url_required=bool(excluded_domains), platform=sampler.platform,
            ):
                restriction = restriction or "window_unavailable"
            if stamp is None or restriction is not None or (activity is None and not context_only):
                schedule.observe(None, sample.monotonic_s)
                store.save_capture_health(
                    "skipped" if stamp is None else "source_restricted", sample.observed_at,
                    reason="capture_disabled" if stamp is None else restriction or "foreground_unavailable",
                )
                return
            video_hint = not context_only and activity is not None and known_video_source(
                sample.app_name, sample.window_title, activity.domain,
            )
            foreground_activity_id = activity.activity_id if activity is not None else None
            window = ((sample.session_id, "foreground_unattributed") if context_only else
                      (sample.session_id, sample.app_id or sample.app_name, sample.window_id))
            schedule.observe(window if context_only else foreground_activity_id, sample.monotonic_s, window=window)
            if job is not None or not schedule.due(sample.monotonic_s, interactive=sample.idle_s is not None and sample.idle_s < 5):
                return

            def permitted() -> bool:
                return not stopped() and capture_stamp() == stamp

            grant = prepare_vision_capture(vision, permitted=permitted)
            scope = None
            if grant is not None and routed_vision is not None:
                try:
                    scope = routed_vision.capture_result_scope(grant)
                except (CloudRouteError, OSError, sqlite3.Error, ValueError):
                    grant = None
            if grant is None or not permitted():
                store.save_capture_health("source_restricted", sample.observed_at, reason="recipient_unavailable")
                return
            failure = None
            try:
                if all_displays:
                    desktop = capture_filtered_desktop(
                        sample, platform=sampler.platform, excluded_apps=excluded_apps, excluded_domains=excluded_domains,
                        foreground_excluded=foreground_excluded,
                    )
                    acquired_at, image = desktop.captured_at, desktop.jpeg
                    context_only = context_only or desktop.foreground_excluded
                else:
                    acquired_at, image = capture_window(sample, platform=sampler.platform)
                thumbnail = None if context_only else frame_thumbnail(image)
            except DesktopCaptureRestricted as exc:
                failure = ("source_restricted", exc.reason)
            except (OSError, RuntimeError, ValueError):
                failure = ("capture_failed", None)

            after = sampler.sample()
            if (_desktop_identity(sample) != _desktop_identity(after)
                    or not clocks_agree(after.observed_at, after.monotonic_s, sample.observed_at, sample.monotonic_s)
                    or after.monotonic_s - sample.monotonic_s > 3 * ACTIVITY_POLL_S):
                observe_sample(after)
                schedule.observe(None, after.monotonic_s)
                store.save_capture_health("source_restricted", after.observed_at,
                                          reason="foreground_changed_during_capture")
                if immediate_update:
                    refresh(publish=True)
                    immediate_update = False
                return
            if failure is not None:
                status, reason = failure
                schedule.defer_probe(after.monotonic_s)
                if status == "capture_failed" and activity is not None and not context_only:
                    store.set_capture_status(activity.activity_id, "failed")
                store.save_capture_health(status, after.observed_at, reason=reason)
                return
            if not permitted() or not sample.observed_at <= acquired_at <= after.observed_at:
                return
            if context_only:
                schedule.defer_probe(after.monotonic_s)
                store.save_capture_health("skipped", acquired_at, reason="foreground_context_only")
                if visual is not None or previous_visual is not None:
                    visual = previous_visual = None
                    refresh(publish=True)
                return
            assert activity is not None and thumbnail is not None
            now = time.monotonic()
            if not schedule.inspect(thumbnail, now):
                store.save_capture_health("skipped", acquired_at, reason="waiting_for_visual_change",
                                          preserve_status=previous_visual is not None)
                return
            store.set_capture_status(activity.activity_id, "captured")
            signals = observer.features(activities_at(acquired_at), [], as_of=acquired_at, live=True)
            # A desktop image can show titles from several apps under one native foreground source.
            document_source = (
                activity.document_source if not all_displays and activity.document_status == "title_unavailable" else None
            )
            evidence = VisionSignals(
                sampled_at=sample.system_sampled_at or sample.observed_at,
                foreground_run_s=signals["current_foreground_run_s"],
                foreground_switches=signals["foreground_switches"], screen_locked=signals["screen_locked"],
                recent_os_input=signals["recent_os_input"] if signals["idle_s"] is not None else None,
                previous=(previous_visual.screen if previous_visual is not None and previous_visual.comparable_to(scope)
                          else None),
            )
            capture_id = uuid4().hex
            deadline_s = after.monotonic_s + observer.ttl_seconds - (after.observed_at - acquired_at).total_seconds()
            scene_identity = last_desktop_identity

            def still_owns_scene() -> bool:
                at, mono = datetime.now(UTC), time.monotonic()
                return (activity_id == activity.activity_id and mono < deadline_s and coverage_current(at, mono)
                        and last_sample is not None and last_sample.screen_locked is False
                        and recorder.capture_eligible
                        and (not all_displays or last_desktop_identity == scene_identity))

            if grant is not LOCAL_CAPTURE:
                grant = replace(grant, send_permitted=still_owns_scene)

            def permitted_to_send() -> bool:
                return permitted() and still_owns_scene()

            def request() -> ScreenObservation:
                if not permitted_to_send():
                    return ScreenObservation(capture_id, activity.activity_id, acquired_at, "failed", vision.model)
                return analyze_multimodal(
                    vision, image=image, activity_id=activity.activity_id, capture_id=capture_id,
                    captured_at=acquired_at, image_grant=None if grant is LOCAL_CAPTURE else grant,
                    signals=evidence, document_source=document_source, all_displays=all_displays,
                    local_permitted=permitted_to_send, deadline_s=min(deadline_s, end_at),
                    scoped_snapshot=scope,
                )

            store.record_upload(datetime.now(UTC), now)
            schedule.sent(now)
            if video_hint:
                schedule.recommend(None, now, video=True)
            uploads += 1
            job = _MultimodalJob(
                activity.activity_id, document_source, video_hint, permitted, still_owns_scene, scope,
                workers.submit(request),
            )

        def collect(*, finishing: bool = False) -> None:
            nonlocal job
            if job is None or not (job.future.done() or finishing):
                return
            finished, job = job, None
            result = finished.future.result()
            with admit_scope(finished.scope) as authorized:
                accept(finished, result, finishing=finishing, permission=authorized and finished.permitted())

        def accept(finished: _MultimodalJob, result: ScreenObservation, *, finishing: bool, permission: bool) -> None:
            nonlocal visual, previous_visual
            at, mono = datetime.now(UTC), time.monotonic()
            same_activity = finished.activity_id == activity_id
            realtime = (not finishing and permission and finished.owns_scene()
                        and at < result.captured_at + timedelta(seconds=observer.ttl_seconds))
            if not permission:
                result = ScreenObservation(result.capture_id, result.activity_id, result.captured_at, "redacted_out", result.model_id)
            store.save_screen(result, processed_at=max(at, result.captured_at), realtime_eligible=realtime,
                              document_source=finished.document_source)
            if same_activity and not finishing:
                health = {"ready": "ready", "failed": "model_failed", "redacted_out": "filtered"}[result.status]
                if getattr(result, "filtered_fields", ()):
                    health = "filtered"
                store.save_capture_health(health, at)
            if isinstance(result, MultimodalScreenObservation) and result.status == "ready" and same_activity:
                previous_visual = _ScopedScreen(result, finished.scope)
                if realtime:
                    visual = previous_visual
                    # Only a native foreground hint can enable the slower video cadence for a desktop composite.
                    video = finished.video_hint if all_displays else (
                        result.screen_kind == "video" or (finished.video_hint and result.screen_kind == "unknown")
                    )
                    schedule.recommend(result.next_check, mono, video=video)
                    refresh(publish=True, outcome="model")
            if not realtime:
                outcomes["discarded"] += 1

        try:
            while not stopped() and (mono := time.monotonic()) < end_at:
                if mono >= next_poll:
                    sample = sampler.sample()
                    if stopped():
                        break
                    if last_sample is not None and not clocks_agree(
                        sample.observed_at, sample.monotonic_s, last_sample.observed_at, last_sample.monotonic_s,
                    ):
                        recorder.close()
                    observe_sample(sample)
                    periodic = mono >= next_update
                    refresh_with_cached_screen(publish=periodic or immediate_update)
                    immediate_update = False
                    if periodic:
                        next_update = mono + model_interval_s
                    acquire(sample)
                    next_poll = max(next_poll + ACTIVITY_POLL_S, time.monotonic())
                collect()
                wake = min(end_at, next_poll)
                if job is not None:
                    wake = min(wake, time.monotonic() + _JOB_CHECK_INTERVAL_S)
                wait_s = max(0.0, wake - time.monotonic())
                if stop_event is None:
                    time.sleep(wait_s)
                else:
                    stop_event.wait(wait_s)
        finally:
            recorder.close()
        halted = stopped()
        if halted:
            store.invalidate_current(datetime.now(UTC))
            latest = None
        # Interrupt pending runtime work before the executor joins its thread.
        if job is not None and not job.future.done():
            vision_lifetime.close()
        if not halted or (job is not None and job.future.done()):
            collect(finishing=True)
        as_of = datetime.now(UTC)
        return {
            **store.counts(), "visual_attempts": uploads, "state_runs": dict(outcomes),
            "last_observation": latest.to_dict() if latest is not None and latest.valid_until > as_of
            and coverage_current(as_of, time.monotonic()) else None, "native_probe_errors": sampler.errors,
        }
