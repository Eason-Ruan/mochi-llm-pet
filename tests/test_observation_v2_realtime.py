from concurrent.futures import Future
from datetime import UTC, datetime, timedelta
import json
import sqlite3
from types import SimpleNamespace

import pytest

from deskpet.observation_v2 import pipeline
from deskpet.observation_v2.activity import ActivityRecorder, ActivitySample
from deskpet.observation_v2.context import ObservationContextReader
from deskpet.observation_v2.state import Observation, StateObserver


EPOCH = datetime(2026, 11, 23, tzinfo=UTC)


def at(seconds):
    return EPOCH + timedelta(seconds=seconds)


class Vision:
    model = "test-vision"

    def complete(self, *, image, **_):
        assert image == b"synthetic jpeg"
        return {"screen_kind": "ide", "visible_text_redacted": None, "screen_summary": "A code editor."}


class Text:
    model = "test-text"

    def __init__(self, state="working"):
        self.state = state
        self.windows = []

    def complete(self, *, prompt, **_):
        self.windows.append(json.loads(prompt))
        return {"activity_kind": "work", "state": self.state, "summary": None, "reason_codes": []}


def stable(mono):
    return ActivitySample(at(mono), mono, "Editor", window_id=1, idle_s=0, screen_locked=False, session_id="A")


def install_runtime(monkeypatch, sample=stable, *, vision_delay=0):
    clock = SimpleNamespace(mono=0.0, samples=[])

    class WallTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return sample(clock.mono).observed_at

    def sleep(seconds):
        clock.mono += seconds

    class Desktop:
        platform = "linux"
        errors = {}

        def __init__(self, *, browser_metadata):
            pass

        def sample(self):
            clock.samples.append(clock.mono)
            return sample(clock.mono)

    class ControlledFuture(Future):
        def __init__(self, operation, args, kwargs, delay):
            super().__init__()
            self.operation = lambda: operation(*args, **kwargs)
            self.ready_at = clock.mono + delay

        def done(self):
            if not super().done() and clock.mono >= self.ready_at:
                self.set_result(self.operation())
            return super().done()

        def result(self):
            clock.mono = max(clock.mono, self.ready_at)
            self.done()
            return super().result()

    class ControlledExecutor:
        def __init__(self, *, max_workers):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def submit(self, operation, *args, **kwargs):
            delay = vision_delay if operation is pipeline.analyze_screen else 0
            future = ControlledFuture(operation, args, kwargs, delay)
            future.done()
            return future

    monkeypatch.setattr(pipeline, "datetime", WallTime)
    monkeypatch.setattr(pipeline, "time", SimpleNamespace(monotonic=lambda: clock.mono, sleep=sleep))
    monkeypatch.setattr(pipeline, "NativeActivitySampler", Desktop)
    monkeypatch.setattr(pipeline, "ThreadPoolExecutor", ControlledExecutor)
    monkeypatch.setattr(pipeline, "capture_desktop", lambda: (WallTime.now(UTC), b"synthetic jpeg"))
    monkeypatch.setattr(pipeline, "system_boot_id", lambda: "test-boot")
    return clock


def test_settings_enabled_local_capture_uses_only_verified_window(tmp_path, monkeypatch):
    def sample(mono):
        return ActivitySample(at(mono), mono, "Editor", window_id=1, window_title="File",
                              idle_s=0, screen_locked=False, session_id="A")

    clock = install_runtime(monkeypatch, sample)
    monkeypatch.setattr(pipeline.NativeActivitySampler, "platform", "darwin")
    captures = []

    def capture_window(foreground, *, platform):
        captures.append((foreground.window_id, platform))
        return at(clock.mono), b"synthetic jpeg"

    def capture_desktop():
        pytest.fail("the user's current-window preference cannot authorize the whole desktop")

    monkeypatch.setattr("deskpet.observation_v2.media.capture_window", capture_window)
    monkeypatch.setattr(pipeline, "capture_desktop", capture_desktop)
    database = tmp_path / "live.sqlite"
    report = pipeline.run_live(
        database, Vision(), StateObserver(Text()), duration_s=1, capture_screen=True,
        capture_policy=lambda: 0, producer_policy=lambda: 0, policy_generation=0,
    )
    assert report["screens"] == {"ready": 1}
    assert captures == [(1, "darwin")]


@pytest.mark.parametrize("vision_delay", [0, 4])
def test_live_round_uses_its_own_fast_frame_or_falls_back_to_system_signals(tmp_path, monkeypatch, vision_delay):
    clock = install_runtime(monkeypatch, vision_delay=vision_delay)
    database = tmp_path / "live.sqlite"
    text = Text()
    report = pipeline.run_live(
        database, Vision(), StateObserver(text), duration_s=65, capture_screen=True, vision_wait_s=2,
    )
    assert report["observations"] == 3
    assert report["state_runs"] == {"model": 3}
    assert report["screens"] == {"ready": 3}
    assert set(range(65)).issubset(clock.samples)
    assert [window["current_screen"]["captured_at"] if window["current_screen"] else None for window in text.windows] == (
        [None] * 3 if vision_delay else [at(seconds).isoformat() for seconds in (0, 30, 60)]
    )
    if vision_delay:
        assert all(not window["recent_screen_results"] for window in text.windows)
    with sqlite3.connect(database) as connection:
        observations = connection.execute("""
            SELECT state, observed_at, valid_until, invalidated_at FROM effective_observations ORDER BY observation_id
        """).fetchall()
    assert [row[0] for row in observations] == ["unknown", "working", "working"]
    for seconds, row in zip((0, 30, 60), observations):
        assert datetime.fromisoformat(row[1]) == at(seconds + (2 if vision_delay else 0))
        assert datetime.fromisoformat(row[2]) <= at(seconds + 15)
    assert observations[-1][3] is None


def test_archived_late_frame_can_inform_a_later_round_without_extending_its_deadline(tmp_path, monkeypatch):
    install_runtime(monkeypatch, vision_delay=2.5)
    database = tmp_path / "live.sqlite"
    text = Text()
    report = pipeline.run_live(database, Vision(), StateObserver(text), duration_s=4,
                               model_interval_s=3, capture_screen=True, vision_wait_s=1)
    assert report["observations"] == 2
    assert text.windows[0]["current_screen"] is None
    assert text.windows[1]["current_screen"]["captured_at"] == at(0).isoformat()
    assert datetime.fromisoformat(report["last_observation"]["valid_until"]) == at(15)


def test_lock_sample_revokes_current_in_its_commit_and_session_boundary_drops_evidence(tmp_path, monkeypatch):
    def sample(mono):
        if mono >= 3:
            return ActivitySample(at(mono), mono, idle_s=0, screen_locked=False, session_id="B")
        if mono >= 2:
            return ActivitySample(at(mono), mono, idle_s=31, screen_locked=True, session_id="A")
        return ActivitySample(at(mono), mono, "Editor", window_id=1, idle_s=30 if mono < 1 else 0,
                              screen_locked=False, session_id="A")

    install_runtime(monkeypatch, sample)
    database = tmp_path / "live.sqlite"
    reader = ObservationContextReader(database, boot_id="test-boot")
    saw_lock = False

    class CheckLockCommit(ActivityRecorder):
        def observe(self, sample):
            nonlocal saw_lock
            if sample.screen_locked:
                before = reader.read_realtime_head(sample.observed_at, monotonic_s=sample.monotonic_s)
                assert before.current is not None and before.current.state == "available"
            result = super().observe(sample)
            if sample.screen_locked:
                with sqlite3.connect(database) as connection:
                    assert connection.execute("SELECT screen_locked FROM activities ORDER BY activity_id DESC LIMIT 1").fetchone() == (1,)
                after = reader.read_realtime_head(sample.observed_at, monotonic_s=sample.monotonic_s)
                assert after.current is None and after.current_status == "context_changed"
                assert after.publication_cursor == before.publication_cursor
                saw_lock = True
            return result

    monkeypatch.setattr(pipeline, "ActivityRecorder", CheckLockCommit)
    text = Text("available")
    report = pipeline.run_live(database, Vision(), StateObserver(text), duration_s=4, model_interval_s=1, capture_screen=True)
    assert saw_lock and report["state_runs"] == {"model": 4}
    assert report["last_observation"]["state"] == "unknown"
    assert text.windows[1]["input_recovered"]
    assert text.windows[2]["screen_locked"] is True
    assert text.windows[3]["current_foreground"] is None
    assert text.windows[3]["current_screen"] is None
    assert text.windows[3]["recent_screen_results"] == []
    assert not text.windows[3]["input_recovered"]


def test_restarted_producer_uses_fresh_continuity_even_without_foreground(tmp_path, monkeypatch):
    def sample(mono):
        if mono < 2:
            return ActivitySample(at(mono), mono, "Editor", window_id=1, idle_s=30,
                                  screen_locked=False, session_id="A")
        return ActivitySample(at(mono), mono, idle_s=0, session_id="B")

    clock = install_runtime(monkeypatch, sample)
    database = tmp_path / "live.sqlite"
    reader = ObservationContextReader(database, boot_id="test-boot")
    pipeline.run_live(database, Vision(), StateObserver(Text()), duration_s=1, capture_screen=True)
    first = reader.read_realtime_head(at(1), monotonic_s=1)
    clock.mono = 7
    text = Text("available")
    report = pipeline.run_live(database, Vision(), StateObserver(text), duration_s=1, capture_screen=True)
    assert report["observations"] == 2
    assert report["last_observation"]["state"] == "unknown"
    following = reader.read_realtime_head(at(8), monotonic_s=8)
    assert following.producer_generation != first.producer_generation
    assert following.publication_cursor == first.publication_cursor + 1
    window, = text.windows
    assert window["current_screen"] is None and not window["recent_screen_results"]
    assert window["recent_os_input"] and not window["input_recovered"]


def test_wall_rollback_keeps_historical_as_of_and_requires_a_new_live_head(tmp_path):
    database = tmp_path / "live.sqlite"
    reader = ObservationContextReader(database, boot_id="test-boot")
    with pipeline.ObservationStore(database, live=True, boot_id="test-boot") as store:
        store.begin_live_run(at(0), monotonic_s=100, poll_interval_s=5)

        def publish(wall_s, mono, state):
            store.save_observation(
                Observation("work", state, None, (), at(wall_s), at(wall_s + 15), 71),
                published_at=at(wall_s), monotonic_s=mono,
            )

        publish(0, 100, "available")
        first = reader.read_realtime_head(at(1), monotonic_s=101)
        assert first.current.state == "available"
        publish(10, 110, "working")
        working = reader.read_realtime_head(at(11), monotonic_s=111)
        assert working.current.context_revision == first.current.context_revision
        assert working.publication_cursor == first.publication_cursor + 1
        assert working.producer_generation == first.producer_generation

        historical = reader.read_observation_context(at(3))
        assert historical.current.state == "available" and historical.current_status == "fresh"
        rolled_back = reader.read_realtime_head(at(3), monotonic_s=112)
        assert rolled_back.current is None and rolled_back.current_status == "context_changed"
        assert rolled_back.publication_cursor == working.publication_cursor
        assert reader.read_realtime_current(at(13), monotonic_s=113) == (None, "context_changed")

        with store.connection:
            store._record_live_sample(at(14), 114)
        publish(14, 114, "available")
        next_head = reader.read_realtime_head(at(14), monotonic_s=114)
        assert next_head.current.state == "available" and next_head.publication_cursor > working.publication_cursor
        wrong_boot = ObservationContextReader(database, boot_id="other-boot")
        assert wrong_boot.read_realtime_current(at(14), monotonic_s=114) == (None, "context_changed")


def test_resume_gap_revokes_head_before_the_producer_runs_again(tmp_path):
    database = tmp_path / "live.sqlite"
    reader = ObservationContextReader(database, boot_id="test-boot")
    with pipeline.ObservationStore(database, live=True, boot_id="test-boot") as store:
        store.begin_live_run(at(0), monotonic_s=100, poll_interval_s=1)
        store.save_observation(Observation("work", "available", None, (), at(0), at(15), 1),
                               published_at=at(0), monotonic_s=100)
        assert reader.read_realtime_head(at(1), monotonic_s=101).current is not None
        assert reader.read_realtime_current(at(4), monotonic_s=104) == (None, "context_changed")
        assert reader.read_current(at(4))[1] == "fresh"


@pytest.mark.parametrize("next_generation", [False, True])
def test_older_read_transaction_cannot_revive_a_superseded_head(tmp_path, next_generation):
    database = tmp_path / "live.sqlite"
    reader = ObservationContextReader(database, boot_id="test-boot")
    with pipeline.ObservationStore(database, live=True, boot_id="test-boot") as store:
        store.begin_live_run(at(0), monotonic_s=100)
        store.save_observation(Observation("work", "available", None, (), at(0), at(15), 1),
                               published_at=at(0), monotonic_s=100)
        with reader._snapshot(realtime=True) as older:
            assert reader._live_head(older, at(1), 101).current.state == "available"
            if next_generation:
                store.begin_live_run(at(2), monotonic_s=102)
                assert reader.read_realtime_current(at(2), monotonic_s=102) == (None, "context_changed")
            else:
                store.save_observation(Observation("work", "working", None, (), at(2), at(17), 1),
                                       published_at=at(2), monotonic_s=102)
                assert reader.read_realtime_head(at(2), monotonic_s=102).current.state == "working"
            assert reader._live_head(older, at(1), 101).current is None


def test_producer_can_publish_in_a_new_clock_interval_after_rollback(tmp_path, monkeypatch):
    def sample(mono):
        wall = mono if mono < 2 else mono - 8
        return ActivitySample(at(wall), mono, "Editor", idle_s=0, screen_locked=False, session_id="A")

    install_runtime(monkeypatch, sample)
    database = tmp_path / "live.sqlite"
    text = Text("available")
    pipeline.run_live(database, Vision(), StateObserver(text), duration_s=3, model_interval_s=1)
    reader = ObservationContextReader(database, boot_id="test-boot")
    live = reader.read_realtime_head(at(-5), monotonic_s=3)
    assert live.current is not None and live.current.observed_at == at(-6)
    assert live.publication_cursor == 3
    assert reader.read_current(at(1))[0].observed_at == at(1)
    assert text.windows[2]["activity_coverage_s"] == 0
