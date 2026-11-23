from datetime import UTC, datetime, timedelta
import sqlite3

import pytest

from deskpet.contracts import DecisionEnvelope, ErrorCode, TaskCommand
from deskpet.m0.composition import build_environment
from deskpet.mocks.clock import FakeClock
from deskpet.mocks.policy import ScriptedSlowPlanner
from deskpet.observation_v2.context import ObservationContextReader
from deskpet.observation_v2.pipeline import ObservationStore, _live_writer
from deskpet.observation_v2.state import Observation

NOW = datetime(2026, 11, 23, 2, tzinfo=UTC)


def publish(store, seconds, *, state="working", kind="work", summary=None, revision=1, latency=0):
    observed = NOW + timedelta(seconds=seconds)
    store.save_observation(
        Observation(kind, state, summary, (), observed, observed + timedelta(seconds=15), revision),
        published_at=observed + timedelta(seconds=latency),
    )


def test_context_is_as_of_and_preserves_history_when_current_changes_or_expires(tmp_path):
    path = tmp_path / "live.sqlite"
    reader = ObservationContextReader(path)
    assert reader.read_observation_context(NOW).current_status == "no_data"
    assert not path.exists()

    with ObservationStore(path, live=True) as store:
        publish(store, -3600, summary="exclusive boundary")
        publish(store, -3599, summary="inside the hour")
        publish(store, -150, summary="screen A")
        publish(store, -105)
        publish(store, -59, summary="screen B")
        publish(store, -6, state="available", revision=2)
        publish(store, 0, summary="late response", revision=3, latency=2)

        context = reader.read_observation_context(NOW)
        assert context.current is not None and context.current.state == "available"
        assert context.current_status == "fresh"
        assert context.history.total_samples == 5
        assert context.history.gap_count == 3
        assert [segment.sample_count for segment in context.history.segments] == [1, 2, 1, 1]
        assert [segment.summary for segment in context.history.segments] == [
            "inside the hour", "screen A", "screen B", None,
        ]
        assert [entry.observed_at for entry in context.history.recent] == [
            NOW - timedelta(seconds=seconds) for seconds in (150, 105, 59)
        ]
        assert context.history.to_dict()["stats"]["state_counts"] == {
            "working": 4, "available": 1, "away": 0, "unknown": 0,
        }

        store.invalidate_current(NOW + timedelta(seconds=3))
        assert reader.read_current(NOW + timedelta(seconds=2))[1] == "fresh"
        invalid = reader.read_observation_context(NOW + timedelta(seconds=3))
        assert invalid.current is None and invalid.current_status == "context_changed"
        assert invalid.history.recent[-1].summary == "late response"
        expired = reader.read_observation_context(NOW + timedelta(seconds=15))
        assert expired.current is None and expired.current_status == "expired"


def test_history_prioritizes_available_and_away_and_counts_omissions(tmp_path):
    path = tmp_path / "live.sqlite"
    entries = [
        ("working", "work"), ("available", "work"), ("working", "study"),
        ("away", "study"), ("unknown", "unknown"), ("working", "work"),
        ("available", "leisure"), ("away", "leisure"), ("working", "work"),
        ("unknown", "work"), ("available", "work"), ("working", "study"),
    ]
    with ObservationStore(path, live=True) as store:
        for index, (state, kind) in enumerate(entries):
            publish(store, -360 + 30 * index, state=state, kind=kind, summary=str(index))
    history = ObservationContextReader(path).read_observation_context(NOW).history
    assert [segment.summary for segment in history.segments] == ["1", "3", "6", "7", "8", "9", "10", "11"]
    assert history.total_samples == 12
    assert history.omitted_segment_count == 4
    assert dict(history.state_counts) == {"working": 5, "available": 3, "away": 2, "unknown": 2}


def test_live_restart_retains_history_without_reusing_previous_current(tmp_path):
    path = tmp_path / "shared.sqlite"
    reader = ObservationContextReader(path)
    with _live_writer(path), ObservationStore(path, live=True) as store:
        assert store.begin_live_run(NOW - timedelta(seconds=8)) == 0
        publish(store, -8, state="available", revision=5)
        with pytest.raises(RuntimeError, match="lock"):
            with _live_writer(path):
                pytest.fail("concurrent producer acquired the same database")
    with _live_writer(path), ObservationStore(path, live=True) as store:
        revision_base = store.begin_live_run(NOW)
        assert revision_base == 5
        context = reader.read_observation_context(NOW)
        assert context.current is None and context.current_status == "context_changed"
        assert context.history.recent[-1].state == "available"
        publish(store, 1, state="unknown", revision=revision_base + 1)
    context = reader.read_observation_context(NOW + timedelta(seconds=1))
    assert context.current_status == "fresh" and context.current.context_revision == 6
    assert context.history.total_samples == 2
    assert len(context.history.recent) == 1
    with pytest.raises(FileExistsError):
        ObservationStore(path)


def test_legacy_logs_supply_history_without_authorizing_current(tmp_path):
    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("""
            CREATE TABLE effective_observations (
                observation_id INTEGER PRIMARY KEY, activity_kind TEXT, state TEXT,
                summary TEXT, reason_codes TEXT, observed_at TEXT, valid_until TEXT, context_revision INTEGER
            )
        """)
        connection.execute("INSERT INTO effective_observations VALUES (1,'work','available','screen','[]',?,?,1)",
                           (NOW.isoformat(), (NOW + timedelta(seconds=15)).isoformat()))
    reader = ObservationContextReader(path)
    context = reader.read_observation_context(NOW)
    assert context.current is None and context.current_status == "context_changed"
    assert context.history.recent[-1].summary == "screen"
    with pytest.raises(ValueError, match="dedicated live database"):
        ObservationStore(path, live=True)


@pytest.mark.parametrize("scenario", ["fresh", "expired", "changed", "invalidated_during_plan", "expired_during_plan",
                                      "revision_changed_during_plan", "task_changed_during_plan"])
def test_system2_reads_history_but_soft_reminder_requires_current_evidence(tmp_path, scenario):
    path = tmp_path / "live.sqlite"
    clock = FakeClock(NOW)
    with ObservationStore(path, live=True) as store:
        publish(store, -90, state="working", summary="Working in the editor")
        publish(store, -30 if scenario == "expired" else -5, state="available", summary="Switch observed")
        if scenario == "changed":
            store.invalidate_current(NOW - timedelta(seconds=2))
        env = build_environment(clock=clock, observation_database=path)
        draft = {
            "content": "Review the report", "mode": "soft", "earliest_at": NOW.isoformat(),
            "latest_first_delivery_at": (NOW + timedelta(minutes=5)).isoformat(),
        }
        created = env.tasks.apply(TaskCommand(command_id="create", kind="create", task_id="report",
                                               payload={"draft": draft}))
        assert created.status == "applied"
        env.tasks.apply(TaskCommand(command_id="confirm", kind="confirm", task_id="report", expected_version=1))
        observation = env.refresh_observation()
        task = env.tasks.get("report")
        envelope = DecisionEnvelope(
            request_id="decide", task_id=task.task_id, task_version=task.version,
            context_revision=observation.context_revision, created_at=NOW,
            expires_at=NOW + timedelta(minutes=1),
        )

        class Planner(ScriptedSlowPlanner):
            def plan_with_context(self, snapshot, task, envelope, results=()):
                self.snapshot = snapshot
                plan = super().plan_with_context(snapshot, task, envelope, results)
                if scenario == "invalidated_during_plan":
                    store.invalidate_current(clock.wall_now())
                elif scenario == "expired_during_plan":
                    clock.advance(10)
                elif scenario == "revision_changed_during_plan":
                    publish(store, 0, state="available", revision=2)
                elif scenario == "task_changed_during_plan":
                    env.tasks.apply(TaskCommand(command_id="cancel", kind="cancel", task_id=task.task_id,
                                                expected_version=task.version))
                return plan

        planner = Planner(clock=clock)
        results = env.runtime.run(observation, task, planner, envelope)

    assert planner.snapshot.history.total_samples == 2
    assert planner.snapshot.history.segments[0].summary == "Working in the editor"
    if scenario == "fresh":
        assert results[0].status == "succeeded"
        assert len(env.delivery.commands) == 1
    elif scenario in {"expired", "changed"}:
        assert planner.snapshot.current is None
        assert planner.snapshot.history.recent[-1].state == "available"
        assert results[0].status == "succeeded"
        assert not env.delivery.commands
        assert any(event.kind == "recheck.scheduled" for event in env.trace.events)
        env.pump()
        assert not env.delivery.commands
        clock.advance(300)
        env.pump()
        assert len(env.delivery.commands) == 1
        assert env.delivery.commands[0].reason == "deadline_fallback"
    else:
        expected = ErrorCode.CANCELLED if scenario == "task_changed_during_plan" else ErrorCode.STALE_CONTEXT
        assert results[0].error_code == expected
        assert not env.delivery.commands
