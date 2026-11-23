from concurrent.futures import Future
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
from types import SimpleNamespace

import pytest

from deskpet.observation_v2 import pipeline
from deskpet.observation_v2.activity import ActivitySample
from deskpet.observation_v2.media import Frame, recording_frames
from deskpet.observation_v2.ollama import OllamaClient
from deskpet.observation_v2.state import StateObserver


class FakeVision:
    model = "test-vision"

    def complete(self, *, system, prompt, schema, image=None):
        assert image.startswith(b"\xff\xd8")
        return {
            "screen_kind": "ide",
            "visible_text_redacted": "Build log from alice@example.org",
            "screen_summary": "An editor displays a build log.",
        }


class FakeText:
    model = "test-text"

    def __init__(self, fail=False):
        self.windows = []
        self.fail = fail

    def complete(self, *, system, prompt, schema, image=None):
        assert image is None
        window = json.loads(prompt)
        self.windows.append(window)
        if self.fail:
            raise TimeoutError
        return {
            "activity_kind": "work",
            "state": "unknown",
            "summary": "An editor displays a build log.",
            "reason_codes": ["insufficient_signals"],
        }


@pytest.mark.parametrize("text_fails", [False, True])
def test_replay_persists_only_contract_data_and_keeps_os_signals_unknown(tmp_path, monkeypatch, text_fails):
    recording = Path("unused.mp4")
    frames = [Frame(0, b"\xff\xd8frame"), Frame(3, b"\xff\xd8frame")]
    monkeypatch.setattr(pipeline, "recording_frames", lambda *_args, **_kwargs: frames)
    database = tmp_path / "observation.sqlite"
    text = FakeText(fail=text_fails)
    report = pipeline.run_replay(recording, database, FakeVision(), StateObserver(text), interval_s=3, max_frames=2)
    assert report["activities"] == report["observations"] == 2
    assert report["screens"] == {"ready": 2}
    assert report["state_runs"] == {"fallback" if text_fails else "model": 2}
    assert report["last_observation"]["state"] == "unknown"
    assert report["last_observation"]["activity_kind"] == ("unknown" if text_fails else "work")
    assert all(window["screen_locked"] is None and window["idle_s"] is None for window in text.windows)
    assert text.windows[-1]["activity_coverage_s"] == 3.0
    with sqlite3.connect(database) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        rows = conn.execute("""
            SELECT screen_observations.activity_id, ended_at, captured_at, system_sampled_at
            FROM screen_observations JOIN activities USING (activity_id) ORDER BY captured_at
        """).fetchall()
        assert rows[0] == (1, "2000-01-01T00:00:00.000000+00:00", "2000-01-01T00:00:00.000000+00:00", None)
        assert rows[1] == (2, "2000-01-01T00:00:03.000000+00:00", "2000-01-01T00:00:03.000000+00:00", None)
        dump = "\n".join(conn.iterdump())
        assert "alice@example.org" not in dump
        assert "frame" not in dump
        assert "evidence_refs" not in dump and "user_correction" not in dump
    with pytest.raises(FileExistsError):
        pipeline.run_replay(recording, database, FakeVision(), StateObserver(text), interval_s=3, max_frames=2)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is required")
def test_ffmpeg_uses_media_timestamps(tmp_path):
    recording = tmp_path / "small.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=size=320x180:rate=10:duration=3.5",
        "-c:v", "mpeg4", str(recording),
    ], check=True)
    frames = recording_frames(recording, interval_s=1.5, max_frames=3)
    assert [frame.seconds for frame in frames] == [0.0, 1.5, 3.0]
    assert all(frame.jpeg.startswith(b"\xff\xd8") for frame in frames)


@pytest.mark.parametrize("endpoint", [
    "http://example.org", "http://127.0.0.1.evil.test", "http://user@localhost",
    "http://localhost/proxy", "file://localhost",
])
def test_model_endpoint_requires_local_origin(endpoint):
    with pytest.raises(ValueError, match="loopback"):
        OllamaClient("test", endpoint=endpoint)


def test_hosted_model_names_are_rejected():
    with pytest.raises(ValueError, match="Hosted"):
        OllamaClient("deepseek-v3.2:cloud")


@pytest.mark.parametrize("switches_during_capture", [False, True])
def test_live_checks_capture_identity_and_persists_the_acquisition_timestamp(
    tmp_path, monkeypatch, switches_during_capture,
):
    class Desktop:
        platform = "win32"
        errors = {}

        def __init__(self, *, browser_metadata):
            self.samples = 0

        def sample(self):
            self.samples += 1
            changed = self.samples > 1 and switches_during_capture
            return ActivitySample(
                datetime.now(UTC), time.monotonic(), "Editor", window_id=2 if changed else 1,
                screen_locked=False, private_window=changed, idle_s=0,
            )

    acquired = datetime.now(UTC)
    monkeypatch.setattr(pipeline, "NativeActivitySampler", Desktop)
    monkeypatch.setattr(pipeline, "capture_desktop", lambda: (acquired, b"\xff\xd8frame"))
    database = tmp_path / "live.sqlite"
    report = pipeline.run_live(
        database, FakeVision(), StateObserver(FakeText()), duration_s=0.06, capture_screen=True,
    )
    assert report["screens"] == ({} if switches_during_capture else {"ready": 1})
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT capture_status FROM activities").fetchone()[0] == (
            "skipped" if switches_during_capture else "captured"
        )
        capture_rows = conn.execute("SELECT captured_at FROM screen_observations").fetchall()
        assert capture_rows == ([] if switches_during_capture else [(acquired.isoformat(),)])


@pytest.mark.parametrize(("duration_s", "text_fails"), [(25, False), (65, False), (65, True)])
def test_live_publishes_once_per_30_second_round(tmp_path, monkeypatch, duration_s, text_fails):
    epoch = datetime(2000, 1, 1, tzinfo=UTC)
    elapsed = 0.0
    call_times = []

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return epoch + timedelta(seconds=elapsed)

    def sleep(seconds):
        nonlocal elapsed
        elapsed += seconds

    class Desktop:
        platform = "darwin"
        errors = {}

        def __init__(self, *, browser_metadata):
            pass

        def sample(self):
            app = "Editor" if elapsed < 6 else "Browser"
            return ActivitySample(Clock.now(UTC), elapsed, app, screen_locked=False, idle_s=0)

    class ImmediateExecutor:
        def __init__(self, *, max_workers):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc_info):
            pass

        def submit(self, operation, *args, **kwargs):
            call_times.append(elapsed)
            future = Future()
            future.set_result(operation(*args, **kwargs))
            return future

    monkeypatch.setattr(pipeline, "datetime", Clock)
    monkeypatch.setattr(pipeline, "time", SimpleNamespace(monotonic=lambda: elapsed, sleep=sleep))
    monkeypatch.setattr(pipeline, "NativeActivitySampler", Desktop)
    monkeypatch.setattr(pipeline, "ThreadPoolExecutor", ImmediateExecutor)
    database = tmp_path / "cadenced.sqlite"
    report = pipeline.run_live(
        database, FakeVision(), StateObserver(FakeText(fail=text_fails)), duration_s=duration_s,
    )

    assert call_times == ([0.0] if duration_s == 25 else [0.0, 30.0, 60.0])
    assert report["observations"] == len(call_times)
    assert report["state_runs"] == (
        {"discarded": len(call_times), "fallback": len(call_times)} if text_fails
        else {"model": len(call_times)}
    )
    assert (report["last_observation"] is None) == (duration_s == 25)
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM effective_observations").fetchone() == (len(call_times),)
