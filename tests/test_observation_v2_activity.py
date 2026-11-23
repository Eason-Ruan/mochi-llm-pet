from datetime import UTC, datetime, timedelta
import sqlite3

import pytest

from deskpet.observation_v2 import activity as module
from deskpet.observation_v2.activity import ActivityRecorder, ActivitySample, ActivityStore, NativeActivitySampler


ORIGIN = datetime(2026, 1, 1, tzinfo=UTC)


def sample(offset, app="Editor", *, wall_offset=None, **kwargs):
    wall = offset if wall_offset is None else wall_offset
    return ActivitySample(ORIGIN + timedelta(seconds=wall), offset, app_name=app, **kwargs)


def test_contiguous_segments_account_for_elapsed_poll_time(tmp_path):
    path = tmp_path / "activity.sqlite3"
    with ActivityStore(path) as store:
        recorder = ActivityRecorder(store, platform="darwin")
        assert recorder.poll_interval_s == 1.5
        recorder.observe(sample(0, idle_s=3, window_id=12, window_title="private-project"))
        first_id = recorder.current_activity.activity_id
        recorder.observe(sample(1.6, idle_s=0.1, window_id=12, window_title="private-project"))
        assert recorder.current_activity.activity_id == first_id
        assert recorder.current_activity.system_sampled_at == sample(1.6).observed_at
        recorder.observe(sample(3.0, "Terminal", idle_s=0.3))
        recorder.observe(sample(4.8, "Terminal", idle_s=2.1))
        recorder.close()
        assert recorder.current_activity is None

    with ActivityStore(path) as reopened:
        editor, terminal = reopened.list_activities()
        assert editor.activity_id == first_id
        assert isinstance(first_id, int)
        assert editor.duration_s == pytest.approx(3.0)
        assert terminal.duration_s == pytest.approx(1.8)
        assert editor.ended_at == terminal.started_at
        assert editor.window_title_redacted == "[redacted]"
        assert editor.idle_s == 0.1
        assert editor.system_sampled_at == sample(1.6).observed_at
        assert reopened.list_activities(since=sample(3.1).observed_at) == [terminal]
    assert b"private-project" not in path.read_bytes()


def test_lock_session_gap_and_wall_drift_break_continuity():
    with ActivityStore(":memory:") as store:
        recorder = ActivityRecorder(store, platform="linux")
        assert recorder.poll_interval_s == 1.0
        for point in [
            sample(0, screen_locked=False, session_id="first"),
            sample(1, screen_locked=False, session_id="first"),
            sample(2, screen_locked=True, session_id="first", window_title="secret"),
            sample(3, screen_locked=True, session_id="first"),
            sample(4, screen_locked=False, session_id="first"),
            sample(5, screen_locked=False, session_id="second"),
            sample(40, screen_locked=False, session_id="second"),
            sample(41, wall_offset=71, screen_locked=False, session_id="second"),
            sample(42, wall_offset=72, screen_locked=False, session_id="second"),
        ]:
            recorder.observe(point)
        activities = store.list_activities()
        assert [row.duration_s for row in activities] == [1, 1, 0, 0, 0, 1]
        assert [row.started_at for row in activities] == [sample(t).observed_at for t in (0, 2, 4, 5, 40, 71)]
        locked = activities[1]
        assert locked.screen_locked is True
        assert locked.app_name is locked.window_title_redacted is locked.domain is None
        assert locked.capture_status == "skipped"


def test_wall_rollback_leaves_no_overlapping_activity():
    with ActivityStore(":memory:") as store:
        recorder = ActivityRecorder(store, poll_interval_s=1)
        first = sample(0)
        recorder.observe(first)
        recorder.observe(first)
        assert recorder.last_capture_reason == "duplicate_sample"
        recorder.observe(sample(1))
        recorder.observe(sample(2, wall_offset=0))
        assert recorder.last_capture_reason == "clock_rollback"
        assert recorder.current_activity is None
        recorder.observe(sample(3, wall_offset=1))
        recorder.observe(sample(4, wall_offset=2))
        before, after = store.list_activities()
        assert before.duration_s == after.duration_s == 1
        assert before.ended_at == after.started_at


def test_capture_schedule_commits_activity_first_and_filters_browser_metadata(tmp_path):
    path = tmp_path / "activity.sqlite3"
    first_url = "https://an-account:secret@example.org/first?token=secret"
    second_url = "https://example.org/second?token=a-second-secret"
    with ActivityStore(path) as store:
        recorder = ActivityRecorder(store, platform="win32", capture_enabled=True)
        assert recorder.poll_interval_s == 0.5
        first = recorder.observe(sample(0, "chrome.exe", url=first_url, window_id=12, window_title="private-page"))
        assert first.reason == "initial"
        with sqlite3.connect(path) as independent_reader:
            assert independent_reader.execute("SELECT activity_id, domain FROM activities").fetchone() == (
                first.activity.activity_id, "example.org",
            )
        assert recorder.observe(sample(0.5, "chrome.exe", url=first_url, window_id=12, window_title="private-page (2)")) is None
        assert recorder.current_activity.activity_id == first.activity.activity_id
        assert recorder.observe(sample(1, "chrome.exe", url=second_url, window_id=12)) is None
        assert recorder.last_capture_reason == "cooldown"
        second = recorder.observe(sample(1.25, "chrome.exe", url=second_url, window_id=12))
        assert second.reason == "browser_page_change"
        assert second.activity.activity_id != first.activity.activity_id

        for t in (1.5, 2.5, 3.5, 4.0):
            assert recorder.observe(sample(t, "Editor")) is None
        third = recorder.observe(sample(4.25, "Editor"))
        assert third.reason == "app_change"
        for tick in range(5, 35):
            assert recorder.observe(sample(tick, "Editor")) is None
        due = recorder.observe(sample(34.25, "Editor"))
        assert due.reason == "periodic"
        assert due.activity.activity_id == third.activity.activity_id
        store.set_capture_status(due.activity.activity_id, "captured")
        store.set_capture_status(due.activity.activity_id, "failed")
        assert recorder.current_activity.capture_status == "captured"
    assert b"private-page" not in path.read_bytes()
    assert b"a-second-secret" not in path.read_bytes()


@pytest.mark.parametrize("url", [None, "https://[bad/path", "file:///private/doc", "https://192.168.1.11"])
def test_missing_or_private_browser_url_reports_fallback(url):
    with ActivityStore(":memory:") as store:
        recorder = ActivityRecorder(store, capture_enabled=True)
        result = recorder.observe(sample(0, "Safari", url=url))
        assert result.activity.domain is None
        assert recorder.observe(sample(1, "Safari", url=url)) is None
        assert recorder.last_capture_reason == "browser_url_unavailable"


def test_input_agnostic_recording_interval_has_no_system_evidence():
    with ActivityStore(":memory:") as store:
        recorder = ActivityRecorder(store)
        started_at, ended_at = ORIGIN, sample(7.5).observed_at
        recording = recorder.add_recording_interval(started_at, ended_at)
        assert store.get_activity(recording.activity_id) == recording
        assert recording.to_dict() == {
            "activity_id": 1,
            "started_at": "2026-01-01T00:00:00.000000+00:00",
            "ended_at": "2026-01-01T00:00:07.500000+00:00",
            "duration_s": 7.5,
            "app_name": None,
            "window_title_redacted": None,
            "domain": None,
            "idle_s": None,
            "screen_locked": None,
            "system_sampled_at": None,
            "capture_status": "skipped",
        }
        with pytest.raises(ValueError):
            recorder.add_recording_interval(ended_at, started_at)


def test_mac_session_keeps_unknown_state_when_quartz_symbol_lookup_fails(monkeypatch):
    import sys

    class Quartz:
        def __getattr__(self, name):
            raise KeyError(name)

    monkeypatch.setitem(sys.modules, "Quartz", Quartz())
    assert module._mac_session() == (None, None)


def test_native_sampler_isolated_failures_and_locked_foreground(monkeypatch):
    monkeypatch.setattr(module, "_mac_session", lambda: (True, "session"))

    def fail_foreground():
        pytest.fail("foreground lookup must be bypassed while the desktop is locked")

    def fail_idle():
        raise OSError("private OS detail")

    monkeypatch.setattr(module, "_mac_foreground", fail_foreground)
    monkeypatch.setattr(module, "_mac_idle", fail_idle)
    sampler = NativeActivitySampler()
    sampler.platform = "darwin"
    result = sampler.sample()
    assert result.screen_locked and result.session_id == "session"
    assert result.idle_s is result.app_name is None
    assert result.system_sampled_at == result.observed_at
    assert sampler.errors == {"idle": "OSError"}


@pytest.mark.parametrize("title, private", [("Page name - Incognito", None), ("unmarked", True)])
def test_private_window_omits_title_and_domain_and_never_requests_capture(title, private):
    with ActivityStore(":memory:") as store:
        recorder = ActivityRecorder(store, capture_enabled=True)
        result = recorder.observe(sample(0, "Google Chrome", window_title=title,
                                         url="https://example.org/private", private_window=private))
        assert result is None and recorder.last_capture_reason == "private_window"
        assert recorder.current_activity.app_name == "Google Chrome"
        assert recorder.current_activity.window_title_redacted is recorder.current_activity.domain is None


def test_mac_browser_automation_is_opt_in_and_checks_privacy_before_url(monkeypatch):
    monkeypatch.setattr(module, "_mac_session", lambda: (False, "session"))
    monkeypatch.setattr(module, "_mac_foreground", lambda: ("Google Chrome", 12, "page"))
    monkeypatch.setattr(module, "_mac_idle", lambda: 2)
    monkeypatch.setattr(module, "_mac_active_bundle_path", lambda bundle_id: "/Applications/Google Chrome.app")
    calls = []

    def osascript(*args, **kwargs):
        calls.append(args)
        assert args[:2] == ("/usr/bin/osascript", "-e")
        assert args[2].index('if mode is not "normal"') < args[2].index("URL of active tab")
        return "private" if len(calls) == 1 else "normal:https://example.org/page"

    monkeypatch.setattr(module, "_command", osascript)
    sampler = NativeActivitySampler()
    sampler.platform = "darwin"
    assert sampler.sample().url is None
    assert sampler.errors["browser_url"] == "native_url_disabled"
    assert not calls

    sampler = NativeActivitySampler(browser_metadata=True)
    sampler.platform = "darwin"
    hidden = sampler.sample()
    assert hidden.private_window is True
    assert hidden.window_title is hidden.url is None
    visible = sampler.sample()
    assert visible.private_window is False and visible.url == "https://example.org/page"


def test_default_and_configured_privacy_exclude_content_before_writes_or_capture():
    with ActivityStore(":memory:") as store:
        recorder = ActivityRecorder(store, capture_enabled=True, excluded_apps=("private app",),
                                    excluded_domains=("example.test",))
        assert recorder.observe(sample(0, "Editor")) is not None
        for observation in (
            sample(1, "1Password"),
            sample(2, "The Private App"),
            sample(3, "Chrome", url="https://sub.example.test/private"),
        ):
            assert recorder.observe(observation) is None
            assert recorder.last_capture_reason == "privacy_excluded"
            assert recorder.current_activity is None
        assert [row.app_name for row in store.list_activities()] == ["Editor"]
        assert store.list_activities()[0].duration_s == 1
        assert recorder.observe(sample(4, "Safari", window_title="Sample Bank Login")) is None
        row = store.list_activities()[-1]
        assert row.window_title_redacted is None and row.domain is None


def test_native_failed_input_and_lock_attempt_has_its_own_timestamp():
    with ActivityStore(":memory:") as store:
        recorder = ActivityRecorder(store)
        recorder.observe(sample(0, idle_s=0, screen_locked=None))
        failed_at = sample(1).observed_at
        recorder.observe(sample(1, system_sampled_at=failed_at))
        row = recorder.current_activity
        assert row.system_sampled_at == failed_at
        assert row.idle_s is row.screen_locked is None
