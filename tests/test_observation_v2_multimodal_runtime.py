from concurrent.futures import Future
from contextlib import closing
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
from deskpet.observation_v2.context import ObservationContextReader
from deskpet.observation_v2.routes import (
    BAILIAN_BASE_URL, BAILIAN_OBSERVATION_MODEL, OLLAMA, VISION, CloudRoute, ObservationCloudSettings,
)
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
        self.desktop_captures = []
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
        monkeypatch.setattr(media, "capture_filtered_desktop", self.capture_filtered_desktop)

    def now(self):
        return EPOCH + timedelta(seconds=self.elapsed)

    def sleep(self, seconds):
        self.elapsed += seconds

    def capture(self, sample, *, platform):
        self.captures.append(self.elapsed)
        return self.now(), self.jpeg(self.elapsed)

    def capture_filtered_desktop(self, sample, *, platform, excluded_apps, excluded_domains, foreground_excluded=False):
        self.desktop_captures.append((sample, platform, excluded_apps, excluded_domains, foreground_excluded))
        acquired_at, jpeg = self.capture(sample, platform=platform)
        return media.FilteredDesktopCapture(acquired_at, jpeg, foreground_excluded)


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
            (_time(0), "unknown"), (_time(6), "available"), (_time(21), "working"),
        ]
        assert conn.execute("SELECT app_name, screen_locked FROM realtime_observation_head").fetchone() == ("Browser", 0)
        assert conn.execute("SELECT count(*) FROM screen_observations").fetchone() == (0,)


def test_live_vision_runtime_failure_keeps_native_observation_and_never_calls_ollama_directly(tmp_path, monkeypatch):
    attempts, direct_calls = [], []

    class UnavailableRuntime:
        def complete(self, **kwargs):
            attempts.append(kwargs["model"])
            raise RuntimeError("Synthetic runtime failure")

        def close(self):
            pass

    def direct_call(*args, **kwargs):
        direct_calls.append(kwargs)
        raise AssertionError("The production loop must own the vision request")

    FakeRuntime(monkeypatch)
    database = tmp_path / "desktop.sqlite"
    monkeypatch.setattr(cli, "ObservationDSH", UnavailableRuntime)
    monkeypatch.setattr(cli.OllamaClient, "complete", direct_call)
    monkeypatch.setattr(sys, "argv", [
        "observation_v2", "live", "--database", str(database), "--duration-s", "6", "--capture-screen",
    ])
    cli.main()

    assert attempts == ["qwen3-vl:2b-instruct"] and direct_calls == []
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT status FROM observation_capture_health").fetchone() == ("model_failed",)
        assert conn.execute("SELECT status, model_id FROM screen_observations").fetchone() == (
            "failed", "qwen3-vl:2b-instruct",
        )
        assert conn.execute("SELECT app_name, screen_locked FROM realtime_observation_head").fetchone() == ("Editor", 0)


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


@pytest.mark.parametrize("change", ["window", "capture_policy", "screen_recording"])
def test_stable_live_scene_survives_a_minute_until_scope_changes(tmp_path, monkeypatch, change):
    native_capture = media.capture_window
    runtime = FakeRuntime(
        monkeypatch, metadata=lambda at: {"window_id": 2} if change == "window" and at >= 66 else {},
    )
    if change == "screen_recording":
        monkeypatch.setitem(sys.modules, "AppKit", SimpleNamespace())
        monkeypatch.setitem(sys.modules, "Quartz", SimpleNamespace(CGPreflightScreenCaptureAccess=lambda: False))

        def capture(sample, *, platform):
            if runtime.elapsed >= 66:
                return native_capture(sample, platform=platform)
            return runtime.capture(sample, platform=platform)

        monkeypatch.setattr(media, "capture_window", capture)
    database = tmp_path / "desktop.sqlite"
    monkeypatch.setattr(pipeline, "system_boot_id", lambda: "test-boot")
    reader = ObservationContextReader(database, boot_id="test-boot")
    observed = {}

    def sample_while_running(seconds):
        if runtime.elapsed in (12, 33, 64.5, 66):
            head = reader.read_realtime_head(runtime.now(), monotonic_s=runtime.elapsed)
            details = reader.read_realtime_context(runtime.now(), monotonic_s=runtime.elapsed).to_dict()
            observed[runtime.elapsed] = head, details
        runtime.sleep(seconds)

    monkeypatch.setattr(pipeline.time, "sleep", sample_while_running)
    vision = Vision()
    report = pipeline.run_live_multimodal(
        database, vision, duration_s=67, capture_screen=True,
        capture_policy=lambda: None if change == "capture_policy" and runtime.elapsed >= 66 else 1,
    )

    stable, stable_detail = observed[12]
    assert stable.current.state == "working"
    assert stable.current.valid_until is None
    assert stable.current.observed_at == EPOCH + timedelta(seconds=10.5)
    assert stable_detail["last_observed_activity"]["relation"] == "current"
    assert stable_detail["last_observed_activity"]["observed_at"] == (EPOCH + timedelta(seconds=3)).isoformat()
    for seconds in (33, 64.5):
        head, detail = observed[seconds]
        assert head == stable
        assert detail["last_observed_activity"] == stable_detail["last_observed_activity"]
        assert detail["interruptibility"]["current"]["valid_until"] is None
    updated, detail = observed[66]
    assert updated.publication_cursor > stable.publication_cursor
    assert updated.current.visual_capture_id is None
    assert updated.current.activity_kind == "unknown"
    assert any(segment.get("scene_elements") == ["page canvas"] for segment in detail["history_1h"]["segments"])
    if change in ("capture_policy", "screen_recording"):
        assert detail["last_observed_activity"]["relation"] == "previous"
    assert len(vision.calls) == 1
    assert report["last_observation"]["valid_until"] is None
    assert reader.read_realtime_head(runtime.now(), monotonic_s=runtime.elapsed).scene_invalidated


@pytest.mark.parametrize("change", ["classification", "document"])
def test_pending_visual_refresh_keeps_scene_until_substantive_change(tmp_path, monkeypatch, change):
    first, changed = _frame(), _frame(area=(100, 100, 560, 140))
    runtime = FakeRuntime(
        monkeypatch, jpeg=lambda at: first if at < 37.5 else changed, model_delay=2,
        metadata=lambda _: {
            "app_name": "Google Chrome", "window_title": "Google Docs", "active_tab_title": "Google Docs",
            "url": "https://docs.google.com/document/d/synthetic/edit",
        },
    )
    database = tmp_path / "desktop.sqlite"
    monkeypatch.setattr(pipeline, "system_boot_id", lambda: "test-boot")
    reader = ObservationContextReader(database, boot_id="test-boot")
    observed = {}

    class ChangingVision(Vision):
        def complete(self, **kwargs):
            result = super().complete(**kwargs)
            if len(self.calls) == 2:
                if change == "classification":
                    result.update(screen_kind="meeting", activity_kind="work", scene_elements=["video call"])
                else:
                    result["document_title"]["name"] = "Weekly Plan"
            return result

    def sample_while_running(seconds):
        for key in (39, 42):
            if runtime.elapsed >= key and key not in observed:
                observed[key] = reader.read_realtime_head(runtime.now(), monotonic_s=runtime.elapsed)
        if len(observed) == 2 and "revoked" not in observed:
            prior, later = observed[39], observed[42]
            observed["revoked"] = reader.presentation_context_changed(
                prior.publication_cursor, later.publication_cursor, revision=prior.current.context_revision,
                generation=prior.producer_generation, now=runtime.now(),
            )
        runtime.sleep(seconds)

    monkeypatch.setattr(pipeline.time, "sleep", sample_while_running)
    pipeline.run_live_multimodal(
        database, ChangingVision(), duration_s=43, capture_screen=True, browser_metadata=True,
        document_local_sources=("domain:docs.google.com",),
    )
    prior, later = observed[39], observed[42]
    assert "page canvas" in prior.current.summary
    if change == "classification":
        assert "meeting view" in later.current.summary
    else:
        assert later.current.summary == prior.current.summary
        with sqlite3.connect(database) as db:
            assert db.execute(
                "SELECT document_name FROM screen_observations ORDER BY captured_at"
            ).fetchall() == [("Quarterly Outlook",), ("Weekly Plan",)]
    assert prior.current.state == later.current.state == "working"
    assert later.current.context_revision > prior.current.context_revision
    assert observed["revoked"] is True


def test_all_displays_observes_ordinary_chrome_without_window_or_browser_metadata(tmp_path, monkeypatch):
    frames = [_frame(area=(100 + x, 100, 240 + x, 320)) for x in (0, 180, 360)]
    runtime = FakeRuntime(
        monkeypatch,
        metadata=lambda at: {
            "app_name": "Google Chrome", "window_id": None, "window_title": None, "private_window": None,
        },
        jpeg=lambda at: frames[int(at / 1.5) % len(frames)],
    )

    def single_window(*args, **kwargs):
        pytest.fail("All-displays capture must use the filtered desktop source")

    class DesktopVision(Vision):
        def complete(self, *, system, **kwargs):
            assert "only the application window that visibly has focus" in system
            assert "background music or video" in system
            assert "Google Chrome" not in system
            result = super().complete(system=system, **kwargs)
            if len(self.calls) == 1:
                result["screen_kind"] = "video"
            return result

    monkeypatch.setattr(media, "capture_window", single_window)
    database = tmp_path / "desktop.sqlite"
    vision = DesktopVision()
    report = pipeline.run_live_multimodal(
        database, vision, duration_s=45, capture_screen=True, screen_scope="all_displays",
        excluded_apps=("blacklisted",),
    )

    assert report["visual_attempts"] == len(vision.calls) == 2
    assert all(platform == "darwin" and apps == ("blacklisted",) and domains == () and not foreground_excluded
               for _, platform, apps, domains, foreground_excluded in runtime.desktop_captures)
    assert all(sample.window_title is None and sample.window_id is None and sample.private_window is None
               for sample, *_ in runtime.desktop_captures)
    assert all("document_title" not in fields and "Google Chrome" not in json.dumps(prompt)
               for prompt, fields in vision.calls)
    with sqlite3.connect(database) as conn:
        frames_at = [datetime.fromisoformat(row[0]) for row in conn.execute(
            "SELECT captured_at FROM screen_observations ORDER BY captured_at",
        )]
    assert frames_at[0] == EPOCH + timedelta(seconds=3)
    assert 30 <= (frames_at[1] - frames_at[0]).total_seconds() <= 35


@pytest.mark.parametrize("foreground,exclusions", [
    ({"app_name": "Mochi"}, {}),
    ({"screen_locked": True}, {}),
    ({"app_name": "Restricted Editor"}, {"excluded_apps": ("restricted",)}),
    ({"app_name": "Chrome", "url": "https://internal.example.net/doc"}, {"excluded_domains": ("example.net",)}),
    ({"app_name": "Chrome", "url": None, "private_window": None}, {"excluded_domains": ("example.net",)}),
    ({"app_name": "Chrome", "private_window": None, "window_title": "Incognito"}, {}),
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


@pytest.mark.parametrize("locked", [True, None])
def test_all_displays_never_captures_when_locked_or_lock_unknown(tmp_path, monkeypatch, locked):
    runtime = FakeRuntime(monkeypatch, metadata=lambda _: {"screen_locked": locked})
    report = pipeline.run_live_multimodal(
        tmp_path / "desktop.sqlite", Vision(), duration_s=6, capture_screen=True, screen_scope="all_displays",
    )

    assert runtime.captures == []
    assert report["visual_attempts"] == 0


@pytest.mark.parametrize("foreground,exclusions", [
    ({"app_name": "Mochi"}, {}),
    ({"app_name": "Restricted Editor"}, {"excluded_apps": ("restricted",)}),
    ({"app_name": "Chrome", "url": "https://internal.example.net/doc"}, {"excluded_domains": ("example.net",)}),
    ({"app_name": "Chrome", "url": None, "private_window": None}, {"excluded_domains": ("example.net",)}),
    ({"app_name": "Chrome", "private_window": None, "window_title": "Incognito"}, {}),
    ({"private_window": True}, {}),
])
def test_all_displays_keeps_allowed_background_when_foreground_is_excluded(
    tmp_path, monkeypatch, foreground, exclusions,
):
    runtime = FakeRuntime(monkeypatch, metadata=lambda _: foreground)
    database = tmp_path / "desktop.sqlite"
    vision = Vision()
    report = pipeline.run_live_multimodal(
        database, vision, duration_s=6, capture_screen=True, screen_scope="all_displays", **exclusions,
    )

    assert report["visual_attempts"] == len(vision.calls) == 0
    assert len(runtime.desktop_captures) == 1 and runtime.desktop_captures[0][-1] is True
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM screen_observations").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM observation_upload_budget").fetchone() == (0,)
        assert conn.execute("SELECT status, reason FROM observation_capture_health").fetchone() == (
            "skipped", "foreground_context_only",
        )


def test_all_displays_respects_foreground_exclusion_reported_by_provider(tmp_path, monkeypatch):
    runtime = FakeRuntime(monkeypatch)

    def capture(sample, **options):
        assert not options.pop("foreground_excluded")
        return runtime.capture_filtered_desktop(sample, foreground_excluded=True, **options)

    monkeypatch.setattr(media, "capture_filtered_desktop", capture)
    database = tmp_path / "desktop.sqlite"
    vision = Vision()
    report = pipeline.run_live_multimodal(
        database, vision, duration_s=6, capture_screen=True, screen_scope="all_displays",
    )

    assert report["visual_attempts"] == len(vision.calls) == 0
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT reason FROM observation_capture_health").fetchone() == ("foreground_context_only",)


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


@pytest.mark.parametrize("change", ["cloud_revoke", "local_switch", "authorized"])
def test_collector_rechecks_recipient_after_vision_result_validation(tmp_path, monkeypatch, change):
    class CompletedRuntime:
        def complete(self, **kwargs):
            assert kwargs["model"] == "original-model"
            return {"screen_kind": "ide", "activity_kind": "work", "scene_elements": ["code editor"],
                    "visible_changes": [], "next_check": None}

    FakeRuntime(monkeypatch)
    settings = ObservationCloudSettings(tmp_path / "settings.sqlite")
    route = CloudRoute(
        "openai_compatible" if change == "cloud_revoke" else OLLAMA,
        "http://127.0.0.1:9", "original-model", "Synthetic provider",
    )
    snapshot = settings.configure(VISION, route)
    if route.is_cloud:
        snapshot = settings.set_permission(VISION, True, expected=snapshot)
    settings.record_result(snapshot, probe=True)
    vision = openai_cloud.RoutedObservationModel(VISION, settings, Vision(), vision_runtime=CompletedRuntime())
    original = vision_module.analyze_multimodal

    def change_authorization(*args, **kwargs):
        result = original(*args, **kwargs)
        assert result.status == "ready"
        current = settings.snapshot(VISION)
        if route.is_cloud:
            settings.set_permission(VISION, False, expected=current)
        elif change == "local_switch":
            replacement = CloudRoute(OLLAMA, route.base_url, "different-model", route.display_name)
            settings.record_result(settings.configure(VISION, replacement, expected=current), probe=True)
        return result

    monkeypatch.setattr(vision_module, "analyze_multimodal", change_authorization)
    commits = []
    save_screen = pipeline.ObservationStore.save_screen
    save_observation = pipeline.ObservationStore.save_observation

    def verify_privacy_gate(operation):
        gate = settings.path.with_name(settings.path.name + ".observation-privacy-lock")
        with closing(sqlite3.connect(gate, timeout=0)) as lock:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                lock.execute("BEGIN IMMEDIATE")
        commits.append(operation)

    def save_scoped_screen(store, screen, **kwargs):
        if screen.status == "ready":
            verify_privacy_gate("screen")
        save_screen(store, screen, **kwargs)

    def publish_scoped_screen(store, observation, **kwargs):
        if observation.activity_kind == "work":
            verify_privacy_gate("publication")
        save_observation(store, observation, **kwargs)

    monkeypatch.setattr(pipeline.ObservationStore, "save_screen", save_scoped_screen)
    monkeypatch.setattr(pipeline.ObservationStore, "save_observation", publish_scoped_screen)
    database = tmp_path / "desktop.sqlite"
    report = pipeline.run_live_multimodal(database, vision, duration_s=6, capture_screen=True)

    with sqlite3.connect(database) as conn:
        stored = conn.execute("""
            SELECT status, model_id, realtime_eligible, screen_summary, scene_elements FROM screen_observations
        """).fetchone()
    if change == "authorized":
        assert report["state_runs"]["model"] == 1
        assert commits == ["screen", "publication"]
        assert stored[:3] == ("ready", "original-model", 1)
    else:
        assert report["state_runs"].get("model", 0) == 0 and commits == []
        assert stored == ("redacted_out", "original-model", 0, None, None)


@pytest.mark.parametrize("authorized", ["qwen", "no_consent", "other_model", "revoked_before_capture"])
def test_unfiltered_desktop_requires_qwen_and_publishes_its_summary(tmp_path, monkeypatch, authorized):
    runtime = FakeRuntime(monkeypatch, metadata=lambda at: {
        "app_name": "Blocked App", "private_window": True,
        "window_id": 1 if at < 4 else 2,
    })
    uploaded = []
    summary = "正在检查一份数据表里的汇总结果。"

    class CompletedRuntime:
        def complete(self, **kwargs):
            uploaded.append(kwargs)
            return {
                "screen_kind": "document", "activity_kind": "work", "scene_elements": ["spreadsheet grid"],
                "visible_changes": [], "next_check": None, "screen_summary": summary,
            }

    def capture_unfiltered(sample, *, platform):
        at, jpeg = runtime.capture(sample, platform=platform)
        return media.FilteredDesktopCapture(at, jpeg, False)

    monkeypatch.setattr(media, "capture_unfiltered_desktop", capture_unfiltered)
    settings = ObservationCloudSettings(tmp_path / "settings.sqlite")
    route = CloudRoute(
        "openai_compatible", BAILIAN_BASE_URL,
        "other-model" if authorized == "other_model" else BAILIAN_OBSERVATION_MODEL, "Test provider",
    )
    snapshot = settings.configure(VISION, route)
    if authorized != "no_consent":
        snapshot = settings.set_permission(VISION, True, expected=snapshot)
        settings.record_result(snapshot, probe=True)
    vision = openai_cloud.RoutedObservationModel(VISION, settings, Vision(), vision_runtime=CompletedRuntime())
    if authorized == "revoked_before_capture":
        scope_for = vision.capture_result_scope

        def revoke_before_capture(grant):
            settings.set_permission(VISION, False, expected=settings.snapshot(VISION))
            return scope_for(grant)

        monkeypatch.setattr(vision, "capture_result_scope", revoke_before_capture)
    database = tmp_path / "desktop.sqlite"
    report = pipeline.run_live_multimodal(
        database, vision, duration_s=6, capture_screen=True, screen_scope="all_displays_unfiltered",
        excluded_apps=("Blocked App",),
    )

    assert not runtime.desktop_captures
    if authorized != "qwen":
        assert report["visual_attempts"] == 0
        assert not runtime.captures and not uploaded
        return
    assert report["visual_attempts"] == 1
    assert report["state_runs"]["model"] == 1
    assert runtime.captures == [3]
    assert len(uploaded) == 1 and uploaded[0]["image"] == _frame()
    with sqlite3.connect(database) as conn:
        assert conn.execute("""
            SELECT a.app_name, s.status, s.screen_summary, s.unfiltered_desktop
            FROM screen_observations s JOIN activities a USING (activity_id)
        """).fetchone() == (None, "ready", summary, 1)
        assert conn.execute("""
            SELECT summary FROM effective_observations WHERE activity_kind='work'
        """).fetchone() == (summary,)
        assert conn.execute("""
            SELECT count(*) FROM activities WHERE app_name IS NULL
        """).fetchone() == (2,)


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


@pytest.mark.parametrize("changed", ["privacy", "pause"])
def test_all_displays_rechecks_permission_and_foreground_after_capture(tmp_path, monkeypatch, changed):
    acquired = False
    runtime = FakeRuntime(
        monkeypatch,
        metadata=lambda _: {"private_window": True} if acquired and changed == "privacy" else {},
    )

    def capture(sample, **options):
        nonlocal acquired
        result = runtime.capture_filtered_desktop(sample, **options)
        acquired = True
        return result

    monkeypatch.setattr(media, "capture_filtered_desktop", capture)
    database = tmp_path / "desktop.sqlite"
    vision = Vision()
    report = pipeline.run_live_multimodal(
        database, vision, duration_s=4.5, capture_screen=True, screen_scope="all_displays",
        capture_policy=lambda: None if acquired and changed == "pause" else 0,
    )

    assert runtime.captures == [3]
    assert report["visual_attempts"] == len(vision.calls) == 0
    assert report["screens"] == {}


def test_all_displays_reports_screen_recording_restriction(tmp_path, monkeypatch):
    FakeRuntime(monkeypatch)

    def capture(sample, **options):
        raise media.DesktopCaptureRestricted(
            "Screen Recording permission unavailable", reason="screen_recording_permission_denied",
        )

    monkeypatch.setattr(media, "capture_filtered_desktop", capture)
    database = tmp_path / "desktop.sqlite"
    vision = Vision()
    report = pipeline.run_live_multimodal(
        database, vision, duration_s=6, capture_screen=True, screen_scope="all_displays",
    )

    assert report["visual_attempts"] == len(vision.calls) == 0
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT status, reason FROM observation_capture_health").fetchone() == (
            "source_restricted", "screen_recording_permission_denied",
        )


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
