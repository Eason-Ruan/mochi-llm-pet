import io
import sqlite3
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
    observer.settings.update(expected_version=prefs.version, observation_paused=True)
    observer.refresh()
    assert children[0].stdin.closed
    children[0].exit_code = 0
    observer.refresh()
    assert len(children) == 1
    prefs = observer.settings.read()
    observer.settings.update(expected_version=prefs.version, observation_paused=False,
                             observation_excluded_domains=("example.net",))
    observer.refresh()
    assert len(children) == 2 and "example.net" in children[-1].args
    assert children[-1].args[children[-1].args.index("--observation-generation") + 1] == "2"
    observer.stop()
    observer.refresh()
    assert children[-1].stdin.closed and len(children) == 2


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
