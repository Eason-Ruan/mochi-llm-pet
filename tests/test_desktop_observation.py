import io
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import closing
from datetime import UTC, datetime, timedelta
from threading import Event
from types import SimpleNamespace

import pytest

from deskpet.contracts import TaskCommand
from deskpet.m0.composition import build_environment
from deskpet.m0.observation_input import DesktopObservationInput
from deskpet.mocks.clock import FakeClock
from deskpet.observation_v2.context import ObservationContextReader
from deskpet.observation_v2.desktop import DesktopObserver
from deskpet.observation_v2.pipeline import ObservationStore
from deskpet.observation_v2.state import Observation
from deskpet.settings import SettingsStore
from deskpet.work_sessions.models import PhaseCommand


@pytest.mark.parametrize("locked_database", ["business", "observation"])
def test_full_desktop_context_waits_for_a_short_sqlite_commit(tmp_path, monkeypatch, locked_database):
    monkeypatch.setattr("deskpet.observation_v2.context.system_boot_id", lambda: "test-boot")
    database = tmp_path / "observation.sqlite3"
    business = tmp_path / "business.db"
    SettingsStore(business)
    with ObservationStore(database, live=True, boot_id="test-boot") as producer:
        now, mono = datetime.now(UTC), time.monotonic()
        producer.begin_live_run(now, monotonic_s=mono, policy_generation=0)
        producer.save_observation(
            Observation("work", "available", None, (), now, now + timedelta(seconds=10), 71),
            published_at=now, monotonic_s=mono,
        )
    path = business if locked_database == "business" else database
    reader = DesktopObservationInput(database, settings_path=business)
    started = Event()

    def read_context():
        started.set()
        return reader.read_observation_context(datetime.now(UTC))

    with closing(sqlite3.connect(path)) as writer, ThreadPoolExecutor(max_workers=1) as pool:
        writer.execute("PRAGMA journal_mode = DELETE")
        writer.execute("BEGIN EXCLUSIVE")
        assert reader.read_current(datetime.now(UTC))[0] is None
        assert reader.read_observation_context(datetime.now(UTC)).current is None
        future = pool.submit(read_context)
        assert started.wait(1)
        with pytest.raises(FutureTimeout):
            future.result(timeout=0.02)
        writer.rollback()
        context = future.result(timeout=2)

    assert context.current_status == "fresh" and context.current.context_revision == 71

    # Even if a transient wait returns an older context, the consumer cannot
    # authorize an observation whose validity ended during the read.
    at, timestamp = datetime.now(UTC), time.monotonic()
    elapsed = 0
    read_realtime_context = reader.reader.read_realtime_context

    def delayed_read(*args, **kwargs):
        nonlocal elapsed
        context = read_realtime_context(*args, **kwargs)
        if kwargs["include_current"]:
            assert context.current is not None
            elapsed = 11
        return context

    monkeypatch.setattr("deskpet.m0.observation_input.sample_realtime_clock", lambda *_: (at, timestamp))
    monkeypatch.setattr("deskpet.m0.observation_input.monotonic", lambda: timestamp + elapsed)
    monkeypatch.setattr(reader.reader, "read_realtime_context", delayed_read)
    assert reader.read_observation_context(datetime.now(UTC)).current is None


def test_locked_optional_database_does_not_delay_a_hard_deadline(tmp_path):
    path = tmp_path / "observation.sqlite3"
    clock = FakeClock()
    with closing(sqlite3.connect(path)) as other:
        other.execute("CREATE TABLE marker (value TEXT)")
        env = build_environment(profile="realtime", clock=clock, observation_database=path)
        due = clock.wall_now() + timedelta(seconds=10)
        draft = {"content": "火车", "mode": "hard", "remind_after": due, "remind_by": due}
        created = env.tasks.apply(TaskCommand(command_id="create", kind="create", task_id="train",
                                              payload={"draft": draft}))
        assert created.status == "applied"
        result = env.tasks.apply(TaskCommand(command_id="accept", kind="confirm", task_id="train", expected_version=1))
        assert result.status == "applied"
        other.execute("BEGIN EXCLUSIVE")
        clock.advance(10)
        before = time.monotonic()
        env.pump()
        assert time.monotonic() - before < 1
        assert [command.task_id for command in env.delivery.commands] == ["train"]
        assert env.observation_context_reader.read_observation_context(clock.wall_now()).current is None
        other.rollback()
        env.pump()
        assert len(env.delivery.commands) == 1


def test_desktop_reads_retained_observation_while_its_producer_is_live(tmp_path, monkeypatch):
    monkeypatch.setattr("deskpet.observation_v2.context.system_boot_id", lambda: "test-boot")
    database = tmp_path / "observation.sqlite3"
    settings = SettingsStore(tmp_path / "business.sqlite3")
    initial = datetime(2026, 11, 25, tzinfo=UTC)
    at = initial + timedelta(seconds=90)
    mono = 190.0
    observation = Observation("leisure", "working", "A video site is visible.", (), initial, None, 71)
    with ObservationStore(database, live=True, boot_id="test-boot") as producer:
        producer.begin_live_run(initial, monotonic_s=100.0, policy_generation=0)
        producer.save_observation(observation, published_at=initial, monotonic_s=100.0)
        with producer.connection:
            producer._record_live_sample(at, mono)
    monkeypatch.setattr("deskpet.m0.observation_input.sample_realtime_clock", lambda *_: (at, mono))
    monkeypatch.setattr("deskpet.m0.observation_input.monotonic", lambda: mono)
    reader = DesktopObservationInput(database, settings_path=settings.path)

    assert reader.read_current(at) == (observation, "fresh")
    assert reader.read_observation_context(at).current == observation
    late_reader = DesktopObservationInput(database, settings_path=settings.path)
    original_read = late_reader.reader.read_realtime_context

    def time_passes_during_read(*args, **kwargs):
        nonlocal at, mono
        result = original_read(*args, **kwargs)
        if kwargs["include_current"]:
            at += timedelta(seconds=5)
            mono += 5
        return result

    monkeypatch.setattr(late_reader.reader, "read_realtime_context", time_passes_during_read)
    assert late_reader.read_observation_context(at).current is None

    settings.update(expected_version=settings.read().version, observation_paused=True)
    assert reader.read_current(at)[0] is None
    assert reader.read_observation_context(at).current is None


def test_supervisor_releases_child_on_pause_and_starts_with_updated_policy(tmp_path, monkeypatch):
    children = []

    class Child:
        def __init__(self, args, **kwargs):
            self.args = args
            self.stdin = io.BytesIO()
            self.exit_code = None
            children.append(self)

        def poll(self):
            return self.exit_code

        def wait(self, timeout=None):
            self.exit_code = 0
            return self.exit_code

    monkeypatch.setattr("deskpet.observation_v2.desktop.subprocess.Popen", Child)
    observer = DesktopObserver(tmp_path)
    prefs = observer.settings.read()
    observer.start()
    observer.refresh()
    assert len(children) == 1 and "--capture-screen-from-settings" in children[0].args
    assert children[0].args[children[0].args.index("--observation-generation") + 1] == "0"
    observer.settings.update(expected_version=prefs.version, observation_screen_scope="all_displays")
    observer.refresh()
    assert children[0].stdin.closed
    children[0].exit_code = 0
    observer.refresh()
    assert len(children) == 2
    assert children[-1].args[children[-1].args.index("--observation-generation") + 1] == "1"
    prefs = observer.settings.read()
    observer.settings.update(expected_version=prefs.version, observation_paused=True)
    observer.refresh()
    assert children[-1].stdin.closed
    children[-1].exit_code = 0
    observer.refresh()
    assert len(children) == 2
    prefs = observer.settings.read()
    observer.settings.update(expected_version=prefs.version, observation_paused=False,
                             observation_excluded_domains=("example.net",))
    observer.refresh()
    assert len(children) == 3 and "example.net" in children[-1].args
    assert children[-1].args[children[-1].args.index("--observation-generation") + 1] == "3"
    observer.stop()
    observer.refresh()
    assert children[-1].stdin.closed and len(children) == 3


def test_observation_policy_waits_for_a_short_business_write(tmp_path, monkeypatch):
    from deskpet.observation_v2.__main__ import _desktop_policies

    child = SimpleNamespace(stdin=io.BytesIO(), poll=lambda: None)
    monkeypatch.setattr("deskpet.observation_v2.desktop.subprocess.Popen", lambda *args, **kwargs: child)
    observer = DesktopObserver(tmp_path)
    current = observer.settings.update(expected_version=0, observation_screenshots=True)
    observer.start()

    stop = Event()
    args = SimpleNamespace(
        cloud_settings=observer.settings_path, user_scope=observer.settings.user_scope,
        observation_generation=current.observation_generation,
        browser_metadata=False, exclude_app=(), exclude_domain=(),
    )
    request_policy, capture_policy, _, _, _ = _desktop_policies(args, stop)

    def start_reader(pool, read):
        started = Event()

        def run():
            started.set()
            return read()

        future = pool.submit(run)
        assert started.wait(1)
        return future

    with closing(sqlite3.connect(observer.settings_path)) as writer, ThreadPoolExecutor(max_workers=2) as pool:
        writer.execute("PRAGMA journal_mode = DELETE")
        writer.execute("BEGIN EXCLUSIVE")
        capture = start_reader(pool, capture_policy)
        refresh = start_reader(pool, observer.refresh)
        for reader in (capture, refresh):
            with pytest.raises(FutureTimeout):
                reader.result(timeout=0.02)
        writer.rollback()
        assert capture.result(timeout=1) == current.observation_generation
        refresh.result(timeout=1)
        assert not child.stdin.closed

        writer.execute("BEGIN EXCLUSIVE")
        assert capture_policy() is None
        writer.rollback()
        assert capture_policy() == current.observation_generation

        writer.execute("BEGIN EXCLUSIVE")
        request = start_reader(pool, request_policy)
        with pytest.raises(FutureTimeout):
            request.result(timeout=0.02)
        stop.set()
        writer.rollback()
        assert request.result(timeout=1) is None


def test_managed_video_review_requires_opt_in_and_running_focus(tmp_path, monkeypatch):
    from deskpet.observation_v2.__main__ import _desktop_policies

    monkeypatch.setenv("MOCHI_SYSTEM2_SEMANTIC_BACKGROUND", "1")
    path = tmp_path / "business.sqlite3"
    env = build_environment(profile="realtime", phase_database=path)
    store = SettingsStore(path)
    settings = store.update(expected_version=store.read().version, observation_screenshots=True)
    stop = Event()
    args = SimpleNamespace(
        cloud_settings=path, user_scope=store.user_scope, observation_generation=settings.observation_generation,
        browser_metadata=False, exclude_app=(), exclude_domain=(),
    )
    *_, review = _desktop_policies(args, stop)
    assert not review()
    started = env.sessions.apply_phase(PhaseCommand(command_id="start", phase_id="focus", kind="start")).view
    assert started is not None and not review()
    store.update(expected_version=settings.version, auto_adjust=True)
    assert review()
    paused = env.sessions.apply_phase(PhaseCommand(command_id="pause", phase_id="focus", kind="pause",
                                                    expected_version=started.version)).view
    assert paused is not None and paused.state == "paused" and not review()
    resumed = env.sessions.apply_phase(PhaseCommand(command_id="resume", phase_id="focus", kind="resume",
                                                     expected_version=paused.version)).view
    assert resumed is not None and review()
    stop.set()
    assert not review()


@pytest.mark.parametrize("scope", ["all_displays", "all_displays_unfiltered"])
def test_managed_cli_pins_scope_and_revokes_previous_worker(tmp_path, monkeypatch, scope):
    from deskpet.observation_v2 import __main__ as cli

    observer = DesktopObserver(tmp_path)
    current = observer.settings.update(
        expected_version=0, observation_screenshots=True, observation_screen_scope=scope,
    )
    args = observer._arguments(current)
    monkeypatch.setattr(cli, "platform", "darwin")
    monkeypatch.setattr(sys, "argv", ["deskpet.observation_v2", *args[3:]])

    def verify_live_policy(database, vision, **options):
        assert database == observer.database
        assert options["screen_scope"] == scope
        assert options["capture_policy"]() == options["producer_policy"]() == current.observation_generation
        observer.settings.update(expected_version=current.version, observation_screen_scope="foreground_window")
        assert options["capture_policy"]() is None and options["producer_policy"]() is None
        return {}

    monkeypatch.setattr(cli, "run_live_multimodal", verify_live_policy)
    cli.main()


def test_macos_recording_permission_preflight_and_explicit_request(monkeypatch):
    from deskpet.perception.macos import screen_recording_access

    events = []
    allowed = False

    def preflight():
        events.append("check")
        return allowed

    def request():
        events.append("request")
        return True

    monkeypatch.setitem(sys.modules, "Quartz", SimpleNamespace(
        CGPreflightScreenCaptureAccess=preflight, CGRequestScreenCaptureAccess=request,
    ))
    assert screen_recording_access() is False
    assert events == ["check"]
    assert screen_recording_access(request=True) is True
    assert events == ["check", "check", "request"]
    allowed = True
    assert screen_recording_access(request=True) is True
    assert events == ["check", "check", "request", "check"]
    monkeypatch.setitem(sys.modules, "Quartz", None)
    assert screen_recording_access(request=True) is None


@pytest.mark.parametrize("platform,request_flag,locked,expected", [
    ("cocoa", True, False, ["request", "pet", "exec", "quit", "unlock"]),
    ("cocoa", False, False, ["pet", "exec", "quit", "unlock"]),
    ("cocoa", True, True, []),
    ("offscreen", True, False, ["pet", "exec", "quit", "unlock"]),
])
def test_desktop_permission_launch_flag_requests_once_after_profile_lock(
    tmp_path, monkeypatch, platform, request_flag, locked, expected,
):
    pytest.importorskip("PyQt6.QtWidgets")
    from deskpet.__main__ import main
    from deskpet.c_ui import refine

    events = []
    app = SimpleNamespace(platformName=lambda: platform, exec=lambda: events.append("exec") or 0)

    def lock_file(path):
        assert path == str(tmp_path / "desktop.lock")
        return SimpleNamespace(setStaleLockTime=lambda _: None, tryLock=lambda: not locked,
                               unlock=lambda: events.append("unlock"))

    def launch(application, args):
        assert application is app and args.data_dir == tmp_path
        events.append("pet")
        return SimpleNamespace(quit=lambda: events.append("quit"))

    def permission(*, request):
        assert request
        events.append("request")
        return False

    monkeypatch.setattr(refine, "_desktop_profile", lambda path: (app, path))
    monkeypatch.setattr(refine, "QLockFile", lock_file)
    monkeypatch.setattr(refine.QGuiApplication, "platformName", lambda: "offscreen")
    monkeypatch.setattr(refine, "platform_notifications", lambda **_: None)
    monkeypatch.setattr(refine, "RefineSession", launch)
    monkeypatch.setattr(refine, "screen_recording_access", permission)
    flags = ["--request-screen-recording"] if request_flag else []
    assert main(["--data-dir", str(tmp_path), *flags]) == int(locked)
    assert events == expected


@pytest.mark.parametrize("platform,scope,flags,error", [
    ("darwin", "all-displays", [], "requires --capture-screen"),
    ("darwin", "all-displays-unfiltered", [], "requires --capture-screen"),
    ("darwin", "all-displays", ["--capture-screen", "--split-models"], "Use replay without --multimodal"),
    ("darwin", "foreground-window", ["--capture-screen", "--split-models"], "Use replay without --multimodal"),
    ("win32", "all-displays", ["--capture-screen"], "requires macOS 14.2 or later"),
    ("win32", "all-displays-unfiltered", ["--capture-screen"], "requires macOS 14.2 or later"),
    ("darwin", "all-displays-unfiltered", ["--capture-screen"], "requires --cloud-settings"),
    ("darwin", "all-displays", ["--capture-screen"], None),
])
def test_explicit_cli_requires_capture_opt_in_and_macos_multimodal(
    tmp_path, monkeypatch, capsys, platform, scope, flags, error,
):
    from deskpet.observation_v2 import __main__ as cli

    monkeypatch.setattr(cli, "platform", platform)
    monkeypatch.setattr(sys, "argv", [
        "deskpet.observation_v2", "live", "--database", str(tmp_path / "test.db"),
        "--screen-scope", scope, *flags,
    ])

    def verify_scope(database, vision, **options):
        assert error is None
        assert options["screen_scope"] == "all_displays" and options["capture_screen"]
        return {"scope": options["screen_scope"]}

    monkeypatch.setattr(cli, "run_live_multimodal", verify_scope)
    if error:
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2
        assert error in capsys.readouterr().err
    else:
        cli.main()
        assert '"scope": "all_displays"' in capsys.readouterr().out


def test_explicit_cli_passes_opted_in_unfiltered_scope_to_live_pipeline(tmp_path, monkeypatch, capsys):
    from deskpet.observation_v2 import __main__ as cli

    monkeypatch.setattr(cli, "platform", "darwin")
    monkeypatch.setattr(sys, "argv", [
        "deskpet.observation_v2", "live", "--database", str(tmp_path / "observation.db"),
        "--capture-screen", "--screen-scope", "all-displays-unfiltered",
        "--cloud-settings", str(tmp_path / "settings.db"),
    ])

    def verify_scope(database, vision, **options):
        assert options["capture_screen"]
        assert options["screen_scope"] == "all_displays_unfiltered"
        return {"scope": options["screen_scope"]}

    monkeypatch.setattr(cli, "run_live_multimodal", verify_scope)
    cli.main()
    assert '"scope": "all_displays_unfiltered"' in capsys.readouterr().out


def test_production_tool_and_qt_readers_reject_still_fresh_head_after_pause_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    qt = pytest.importorskip("PyQt6.QtWidgets")
    app = qt.QApplication.instance() or qt.QApplication([])
    from deskpet.c_ui.presentation import DesktopPresentation

    monkeypatch.setattr("deskpet.observation_v2.context.system_boot_id", lambda: "test-boot")
    business = tmp_path / "business.db"
    database = tmp_path / "observation.sqlite3"
    settings = SettingsStore(business)
    env = build_environment(profile="realtime", observation_database=database,
                            task_database=business, phase_database=business)
    presentation = DesktopPresentation(path=business, observation=database, synthetic=True)
    presentation.snapshot = {"clock": "wall"}
    now = datetime.now(UTC)
    until = now + timedelta(seconds=15)
    command = SimpleNamespace(reason="tool", context_revision=71, context_valid_until=until)
    with ObservationStore(database, live=True, boot_id="test-boot") as producer:
        mono = time.monotonic()
        producer.begin_live_run(now, monotonic_s=mono, policy_generation=0)
        producer.save_observation(Observation("work", "available", "synthetic activity", (), now, until, 71),
                                  published_at=now, monotonic_s=mono)
        assert env.refresh_observation().estimate.state == "available"
        assert presentation.soft_valid(command)
        assert ObservationContextReader(database).read_realtime_current()[0] is None

        changed = settings.update(expected_version=0, reminder_sound=False)
        assert changed.observation_generation == 0
        assert presentation.soft_valid(command)

        paused = settings.update(expected_version=changed.version, observation_paused=True)
        assert paused.observation_generation == 1
        assert env.refresh_observation().estimate.state == "unknown"
        assert not presentation.soft_valid(command)
        context = env.observation_context_reader.read_observation_context(datetime.now(UTC))
        assert context.current is None and context.history.recent[-1].summary == "synthetic activity"
        resumed = settings.update(expected_version=paused.version, observation_paused=False)
        assert resumed.observation_generation == 2
        assert env.refresh_observation().estimate.state == "unknown"
        assert not presentation.soft_valid(command)

        later, mono = datetime.now(UTC), time.monotonic()
        producer.begin_live_run(later, monotonic_s=mono, policy_generation=resumed.observation_generation)
        producer.save_observation(Observation("work", "available", None, (), later, until, 72),
                                  published_at=later, monotonic_s=mono)
        assert env.refresh_observation().context_revision == 72
        assert not presentation.soft_valid(command)
        command.context_revision = 72
        assert presentation.soft_valid(command)
