from concurrent.futures import Future
from datetime import UTC, datetime, timedelta
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

from PIL import Image, ImageDraw
import pytest

from deskpet.observation_v2 import __main__ as cli, media, openai_cloud, pipeline, vision as vision_module
from deskpet.observation_v2.activity import ActivitySample
from deskpet.observation_v2.sampling import CaptureSchedule, frame_thumbnail
from deskpet.observation_v2.state import StateObserver


EPOCH = datetime(2020, 1, 1, tzinfo=UTC)


def _frame(*, area=None):
    image = Image.new("RGB", (960, 540), "#334455")
    if area is not None:
        ImageDraw.Draw(image).rectangle(area, fill="white")
    with BytesIO() as stream:
        image.save(stream, "JPEG")
        return stream.getvalue()


class Vision:
    model = "test-vision"

    def __init__(self):
        self.calls = []

    def complete(self, *, system, prompt, schema, image):
        assert image.startswith(b"\xff\xd8")
        self.calls.append((json.loads(prompt), schema["properties"].keys()))
        result = {
            "screen_kind": "document", "activity_kind": "work", "scene_elements": ["page canvas"],
            "visible_changes": [], "next_check": "check_soon",
        }
        if "document_title" in schema["properties"]:
            result["document_title"] = {"area": "active_editor_title", "name": "Quarterly Outlook"}
        return result


class FakeRuntime:
    def __init__(self, monkeypatch, *, metadata=None, jpeg=None, model_delay=0):
        self.elapsed = 0.0
        self.captures = []
        runtime = self
        self.metadata = metadata or (lambda _: {})
        self.jpeg = jpeg or (lambda _: _frame())

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return runtime.now()

        class Sampler:
            platform = "darwin"
            errors = {}

            def __init__(self, **kwargs):
                self.settings = kwargs

            def sample(self):
                defaults = dict(app_name="Editor", window_id=1, window_title="Scratchpad", idle_s=0,
                                screen_locked=False, session_id="session", private_window=False)
                return ActivitySample(runtime.now(), runtime.elapsed, **(defaults | runtime.metadata(runtime.elapsed)))

        class Executor:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                pass

            def submit(self, operation):
                result = operation()
                if not model_delay:
                    completed = Future()
                    completed.set_result(result)
                    return completed
                ready = runtime.elapsed + model_delay
                return SimpleNamespace(done=lambda: runtime.elapsed >= ready, result=lambda: result)

        monkeypatch.setattr(pipeline, "datetime", Clock)
        monkeypatch.setattr(pipeline, "time", SimpleNamespace(monotonic=lambda: self.elapsed, sleep=self.sleep))
        monkeypatch.setattr(pipeline, "NativeActivitySampler", Sampler)
        monkeypatch.setattr(pipeline, "ThreadPoolExecutor", Executor)
        monkeypatch.setattr(media, "capture_window", self.capture)

    def now(self):
        return EPOCH + timedelta(seconds=self.elapsed)

    def sleep(self, seconds):
        self.elapsed += seconds

    def capture(self, sample, *, platform):
        self.captures.append(self.elapsed)
        return self.now(), self.jpeg(self.elapsed)


def _time(second):
    return (EPOCH + timedelta(seconds=second)).isoformat(timespec="microseconds")


def test_production_cli_uses_local_state_without_a_text_transport(tmp_path, monkeypatch):
    class NoTextTransport(Vision):
        def complete(self, **kwargs):
            raise AssertionError("The collector must not infer without a screenshot")

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            pass

    constructed = []

    def create_transport(model, **kwargs):
        constructed.append(model)
        return NoTextTransport()

    FakeRuntime(monkeypatch, metadata=lambda at: {"app_name": "Browser"} if at >= 6 else {})
    database = tmp_path / "desktop.sqlite"
    monkeypatch.setattr(cli, "OllamaClient", create_transport)
    monkeypatch.setattr(sys, "argv", ["observation_v2", "live", "--database", str(database), "--duration-s", "31"])
    cli.main()

    assert constructed == ["qwen3-vl:2b-instruct"]
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT observed_at, state FROM effective_observations ORDER BY observation_id").fetchall() == [
            (_time(0), "unknown"), (_time(6), "available"), (_time(30), "working"),
        ]
        assert conn.execute("SELECT app_name, screen_locked FROM realtime_observation_head").fetchone() == ("Browser", 0)
        assert conn.execute("SELECT count(*) FROM screen_observations").fetchone() == (0,)


def test_stable_material_change_has_one_call_each_and_keeps_capture_time(tmp_path, monkeypatch):
    base = _frame()
    cursor = _frame(area=(100, 40, 110, 50))
    changed = _frame(area=(100, 100, 560, 140))
    FakeRuntime(monkeypatch, jpeg=lambda at: base if at < 30 else cursor if at < 37.5 else changed)
    vision = Vision()
    database = tmp_path / "desktop.sqlite"
    report = pipeline.run_live_multimodal(database, vision, duration_s=70, capture_screen=True)

    assert report["visual_attempts"] == len(vision.calls) == 2
    assert [prompt["captured_at"] for prompt, _ in vision.calls] == [
        (EPOCH + timedelta(seconds=seconds)).isoformat() for seconds in (3, 39)
    ]
    assert "previous_scene" in vision.calls[1][0]
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT captured_at, realtime_eligible FROM screen_observations").fetchall() == [
            (_time(3), 1), (_time(39), 1),
        ]
        assert conn.execute("SELECT status, last_ready_at FROM observation_capture_health").fetchone() == (
            "ready", _time(39),
        )


@pytest.mark.parametrize("foreground,exclusions", [
    ({"app_name": "Mochi"}, {}),
    ({"screen_locked": True}, {}),
    ({"app_name": "Restricted Editor"}, {"excluded_apps": ("restricted",)}),
    ({"app_name": "Chrome", "url": "https://internal.example.net/doc"}, {"excluded_domains": ("example.net",)}),
    ({"private_window": True}, {}),
])
def test_restricted_app_or_window_never_acquires_an_image(tmp_path, monkeypatch, foreground, exclusions):
    runtime = FakeRuntime(monkeypatch, metadata=lambda at: foreground)
    vision = Vision()
    report = pipeline.run_live_multimodal(
        tmp_path / "desktop.sqlite", vision, duration_s=10, capture_screen=True, **exclusions,
    )

    assert runtime.captures == []
    assert report["visual_attempts"] == len(vision.calls) == 0
    assert report["observations"] == 1
    if foreground.get("screen_locked"):
        assert report["last_observation"]["state"] == "away"


@pytest.mark.parametrize("revoked", [False, True])
def test_late_result_uses_original_activity_and_cannot_replace_current(tmp_path, monkeypatch, revoked):
    runtime = FakeRuntime(
        monkeypatch, metadata=lambda at: {"app_name": "Mochi"} if at >= 6 else {}, model_delay=20,
    )
    vision = Vision()
    database = tmp_path / "desktop.sqlite"
    report = pipeline.run_live_multimodal(
        database, vision, duration_s=31, capture_screen=True,
        capture_policy=lambda: None if revoked and runtime.elapsed >= 9 else 0,
    )

    assert report["visual_attempts"] == len(vision.calls) == 1
    assert report["state_runs"].get("model", 0) == 0
    with sqlite3.connect(database) as conn:
        frame = conn.execute("""
            SELECT app_name, captured_at, status, realtime_eligible, screen_summary
            FROM screen_observations JOIN activities USING (activity_id)
        """).fetchone()
        assert frame[:4] == ("Editor", _time(3), "redacted_out" if revoked else "ready", 0)
        assert frame[4] is None if revoked else "page canvas" in frame[4]
        assert conn.execute("SELECT app_name FROM realtime_observation_head").fetchone() == ("Mochi",)


@pytest.mark.parametrize("allow_title", [True, False])
def test_visual_title_fallback_requires_trusted_native_document_source(tmp_path, monkeypatch, allow_title):
    FakeRuntime(monkeypatch, metadata=lambda at: {
        "app_name": "Google Chrome", "window_title": "Google Docs", "active_tab_title": "Google Docs",
        "url": "https://docs.google.com/document/d/synthetic/edit?session=never-share",
    })
    vision = Vision()
    database = tmp_path / "desktop.sqlite"
    pipeline.run_live_multimodal(
        database, vision, duration_s=5, capture_screen=True, browser_metadata=True,
        document_local_sources=("domain:docs.google.com",) if allow_title else (),
    )

    [(prompt, fields)] = vision.calls
    assert ("document_title" in fields) == allow_title
    assert "never-share" not in json.dumps(prompt) and "Google Docs" not in json.dumps(prompt)
    with sqlite3.connect(database) as conn:
        name_and_source = conn.execute("SELECT document_name, document_source FROM screen_observations").fetchone()
        assert name_and_source == (("Quarterly Outlook", "domain:docs.google.com") if allow_title else (None, None))


def test_capture_budget_and_duplicate_filter_outlive_foreground_cycle():
    schedule = CaptureSchedule()
    first = frame_thumbnail(_frame())
    new = frame_thumbnail(_frame(area=(100, 100, 500, 200)))
    schedule.observe(1, 0, window="editor")
    assert not schedule.inspect(first, 1.5)
    assert schedule.inspect(first, 3)
    schedule.sent(3)
    schedule.recommend("check_soon", 3)
    assert schedule.soon_s == 33
    schedule.recommend("check_soon", 4)
    assert schedule.soon_s == 33
    schedule.observe(2, 6, window="browser")
    assert not schedule.due(25, interactive=True)
    schedule.observe(3, 12, window="editor")
    assert not schedule.inspect(first, 31.5)
    assert not schedule.inspect(first, 33)
    assert not schedule.inspect(new, 35)
    assert schedule.inspect(new, 37)


def test_active_motion_does_not_starve_first_or_followup_sample(tmp_path, monkeypatch):
    frames = [_frame(area=(100 + x, 100, 240 + x, 320)) for x in (0, 180, 360)]
    FakeRuntime(monkeypatch, jpeg=lambda at: frames[int(at / 1.5) % len(frames)])
    vision = Vision()
    database = tmp_path / "desktop.sqlite"
    report = pipeline.run_live_multimodal(database, vision, duration_s=45, capture_screen=True)

    assert report["visual_attempts"] == len(vision.calls) == 2
    with sqlite3.connect(database) as conn:
        frames_at = [datetime.fromisoformat(row[0]) for row in conn.execute(
            "SELECT captured_at FROM screen_observations ORDER BY captured_at",
        )]
        assert frames_at[0] == EPOCH + timedelta(seconds=3)
        assert 30 <= (frames_at[1] - frames_at[0]).total_seconds() <= 35


def test_upload_interval_survives_collector_restart(tmp_path, monkeypatch):
    runtime = FakeRuntime(monkeypatch)
    database = tmp_path / "desktop.sqlite"
    starts = Vision()
    pipeline.run_live_multimodal(database, starts, duration_s=5, capture_screen=True)
    assert len(starts.calls) == 1
    assert runtime.elapsed == 5

    restarted = Vision()
    pipeline.run_live_multimodal(database, restarted, duration_s=35, capture_screen=True)
    assert len(restarted.calls) == 1
    with sqlite3.connect(database) as conn:
        frames_at = [datetime.fromisoformat(row[0]) for row in conn.execute(
            "SELECT captured_at FROM screen_observations ORDER BY captured_at",
        )]
        assert (frames_at[1] - frames_at[0]).total_seconds() >= 30


def test_lock_detected_during_capture_discards_frame_and_records_departure(tmp_path, monkeypatch):
    locked = False
    runtime = FakeRuntime(monkeypatch, metadata=lambda _: {"screen_locked": locked})

    def capture(sample, *, platform):
        nonlocal locked
        locked = True
        return runtime.capture(sample, platform=platform)

    monkeypatch.setattr(media, "capture_window", capture)
    vision = Vision()
    database = tmp_path / "desktop.sqlite"
    report = pipeline.run_live_multimodal(database, vision, duration_s=3.1, capture_screen=True)

    assert runtime.captures == [3]
    assert report["visual_attempts"] == len(vision.calls) == 0
    assert report["last_observation"]["state"] == "away"
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT screen_locked, lock_epoch FROM realtime_observation_head").fetchone() == (1, 1)


@pytest.mark.parametrize("new_foreground", [{"screen_locked": True}, {"app_name": "Mochi"}])
def test_cloud_send_checks_current_scene_while_already_sent_result_may_be_retained(
    tmp_path, monkeypatch, new_foreground,
):
    runtime = FakeRuntime(monkeypatch, metadata=lambda at: new_foreground if at >= 6 else {}, model_delay=8)
    admitted = []

    def grant(_model, *, permitted):
        return openai_cloud.VisionImageGrant(None, float("inf"), permitted)

    def analyze(_vision, *, image_grant, capture_id, activity_id, captured_at, **_kwargs):
        assert image_grant.send_permitted()
        admitted.append(image_grant)
        return vision_module.MultimodalScreenObservation(capture_id, activity_id, captured_at, "ready", "test")

    monkeypatch.setattr(openai_cloud, "prepare_vision_capture", grant)
    monkeypatch.setattr(vision_module, "analyze_multimodal", analyze)
    database = tmp_path / "desktop.sqlite"
    pipeline.run_live_multimodal(database, Vision(), duration_s=12, capture_screen=True)

    [old_grant] = admitted
    assert not old_grant.send_permitted()
    assert old_grant.permitted()
    assert runtime.elapsed == 12
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT status, realtime_eligible FROM screen_observations").fetchone() == ("ready", 0)


def test_video_recheck_can_resume_when_the_same_window_displays_a_stable_document():
    schedule = CaptureSchedule()
    video = frame_thumbnail(_frame())
    document = frame_thumbnail(_frame(area=(100, 100, 500, 200)))
    schedule.observe(1, 0)
    assert not schedule.inspect(video, 1.5)
    assert schedule.inspect(video, 3)
    schedule.sent(3)
    schedule.recommend("check_soon", 3, video=True)

    assert not schedule.due(33, interactive=True)
    assert schedule.due(123, interactive=False)
    assert not schedule.inspect(document, 123)
    assert schedule.due(124.5, interactive=False)
    assert schedule.inspect(document, 124.5)


@pytest.mark.parametrize("source", [
    {"app_name": "Windows Media Player"},
    {"app_name": "Google Chrome", "url": "https://www.youtube.com/watch?v=synthetic"},
])
@pytest.mark.parametrize("confirmed_video", [True, False])
def test_known_video_source_has_first_sample_and_rechecks_based_on_scene(
    tmp_path, monkeypatch, source, confirmed_video,
):
    class SourceVision(Vision):
        def complete(self, **kwargs):
            result = super().complete(**kwargs)
            if confirmed_video and len(self.calls) == 1:
                result.update(screen_kind="video", activity_kind="leisure", scene_elements=["video player"])
            return result

    first, changed = _frame(), _frame(area=(100, 100, 500, 200))
    runtime = FakeRuntime(monkeypatch, metadata=lambda _: source, jpeg=lambda at: first if at < 40 else changed)
    vision = SourceVision()
    database = tmp_path / "desktop.sqlite"
    pipeline.run_live_multimodal(database, vision, duration_s=130, capture_screen=True)

    assert runtime.captures[0] == 3
    assert len(vision.calls) == 2
    captures = [datetime.fromisoformat(prompt["captured_at"]) for prompt, _ in vision.calls]
    assert captures[1] == EPOCH + timedelta(seconds=124.5 if confirmed_video else 42)


def test_multimodal_replay_runs_adaptive_picker_and_leaves_native_signals_unknown(tmp_path, monkeypatch):
    frames = tuple(media.Frame(second, _frame()) for second in range(0, 61, 3))
    monkeypatch.setattr(pipeline, "recording_frames", lambda *_args, **_kwargs: frames)
    vision = Vision()
    report = pipeline.run_replay_multimodal(
        Path("unused.mp4"), tmp_path / "replay.sqlite", vision,
        observer=StateObserver(), interval_s=3, max_frames=21,
    )
    assert report["visual_attempts"] == len(vision.calls) == 1
    assert vision.calls[0][0]["system_signals"] == {}
    assert report["last_observation"]["state"] == "unknown"


def test_managed_startup_requires_a_confirmed_policy_before_sampling(tmp_path, monkeypatch):
    from deskpet.settings import SettingsStore

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(SettingsStore, "read_at", locked)
    arguments = SimpleNamespace(cloud_settings=tmp_path / "business.sqlite", user_scope="default")
    with pytest.raises(ValueError, match="Could not confirm the desktop Observation policy"):
        cli._desktop_policies(arguments, stop_event=None)
