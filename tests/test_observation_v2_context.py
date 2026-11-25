from dataclasses import replace
from datetime import UTC, datetime, timedelta
import sqlite3

import pytest

from deskpet.contracts import DecisionEnvelope, ErrorCode, TaskCommand
from deskpet.m0.composition import build_environment
from deskpet.m0.observation_input import DesktopObservationInput
from deskpet.mocks.clock import FakeClock
from deskpet.mocks.policy import ScriptedSlowPlanner
from deskpet.observation_v2.activity import Activity, ActivityRecorder, ActivitySample
from deskpet.observation_v2.context import ObservationContextReader, project_observation_context
from deskpet.observation_v2.pipeline import ObservationStore, _live_writer
from deskpet.observation_v2.state import Observation
from deskpet.observation_v2.vision import MultimodalScreenObservation, ScreenObservation
from deskpet.system2.dsh_projection import project_context

NOW = datetime(2026, 11, 23, 2, tzinfo=UTC)


def at(offset):
    return NOW + timedelta(seconds=offset)


def record_activity(store, start, end, app_name=None, **fields):
    with store.connection:
        return store._insert(Activity(0, at(start), at(end), end - start, app_name, **fields)).activity_id


def record_frame(store, activity_id, offset, kind, *, scene="video", summary=None, late=False, available=None):
    store.save_screen(MultimodalScreenObservation(
        f"frame-{activity_id}-{offset}", activity_id, at(offset), "ready", "fixture",
        scene, activity_kind=kind, screen_summary=summary, unfiltered_desktop=summary is not None,
    ), processed_at=at(offset if available is None else available), realtime_eligible=not late)


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


@pytest.mark.parametrize("app_name", ["Editor", "Visual Studio Code", None])
def test_unfiltered_summary_reaches_activity_context_under_existing_sharing_controls(tmp_path, app_name):
    path = tmp_path / "live.sqlite"
    summary = "A spreadsheet is comparing model validation results across experiments."
    title = "analysis.py — Visual Studio Code" if app_name == "Visual Studio Code" else None
    new_sample = NOW + timedelta(seconds=1)
    with ObservationStore(path, live=True) as store:
        recorder = ActivityRecorder(store, document_local_sources=("app:vscode",))
        recorder.observe(ActivitySample(
            NOW, 100, app_name=app_name, window_id=1, window_title=title, screen_locked=False, idle_s=1,
        ))
        activity_id = recorder.current_activity.activity_id
        store.save_screen(MultimodalScreenObservation(
            "raw", activity_id, NOW, "ready", "qwen", "document", screen_summary=summary,
            activity_kind="work", scene_elements=("spreadsheet grid",), unfiltered_desktop=True,
        ))
        recorder.observe(ActivitySample(
            new_sample, 101, app_name=app_name, window_id=1, window_title=title, screen_locked=False, idle_s=1,
        ))

    context = ObservationContextReader(path).read_observation_context(new_sample)
    view = project_observation_context(context, share_summary=True, restrict_legacy_summaries=True)
    assert view["last_observed_activity"]["summary"] == summary
    assert view["history_1h"]["segments"][0]["summary"] == summary
    assert project_context(view, share=True)["last_observed_activity"]["summary"] == summary
    assert summary not in str(project_context(view, share=False))
    assert summary not in str(project_observation_context(context))
    if app_name:
        assert summary not in str(project_observation_context(
            context, share_summary=True, excluded_apps=(app_name.casefold(),),
        ))


def test_scene_visual_outlives_history_range_while_sampler_is_current(tmp_path):
    path = tmp_path / "live.sqlite"
    reader = ObservationContextReader(path, boot_id="test-boot")
    with ObservationStore(path, live=True, boot_id="test-boot") as store:
        store.begin_live_run(NOW, monotonic_s=100, policy_generation=5)
        recorder = ActivityRecorder(store)
        initial = ActivitySample(NOW, 100, app_name="Editor", screen_locked=False, idle_s=1,
                                 system_sampled_at=NOW)
        recorder.observe(initial)
        activity = recorder.current_activity
        with store.connection:
            store._record_live_sample(NOW, 100, sample=initial, activity=activity)
        store.save_screen(MultimodalScreenObservation(
            "retained", activity.activity_id, NOW, "ready", "fixture", "document", None, None,
            activity_kind="work", scene_elements=("page canvas",),
        ), processed_at=NOW)
        store.save_observation(
            Observation("work", "working", "The visible interface includes page canvas.",
                        ("screen_context",), NOW, None, 1, visual_capture_id="retained"),
            published_at=NOW, monotonic_s=100,
        )

        # Summarize continuous sampling after an hour and small cumulative wall-clock slew.
        wall = NOW + timedelta(seconds=3610.1)
        mono = 3710
        confirmed_activity = replace(activity, ended_at=wall, duration_s=3610, system_sampled_at=wall)
        refreshed = replace(initial, observed_at=wall, monotonic_s=mono, system_sampled_at=wall)
        with store.connection:
            store._extend(confirmed_activity)
            store._record_live_sample(wall, mono, sample=refreshed, activity=confirmed_activity)

        raced = reader.read_realtime_head(wall - timedelta(milliseconds=5), monotonic_s=mono - 0.005, policy_generation=5)
        assert raced.current is None and not raced.scene_invalidated
        admission_at = wall - timedelta(milliseconds=1)
        admitted = reader.read_realtime_context(admission_at, current_at=wall, monotonic_s=mono, policy_generation=5)
        assert admitted.current is not None and admitted.current_context.observed_at is None
        view = reader.read_realtime_context(wall, monotonic_s=mono, policy_generation=5).to_dict()
        assert view["current"]["observed_at"] == NOW.isoformat()
        assert view["current"]["valid_until"] is None
        assert view["last_observed_activity"]["relation"] == "current"
        assert view["last_observed_activity"]["observed_at"] == NOW.isoformat()
        assert view["last_observed_activity"]["scene_elements"] == ["page canvas"]
        assert all(segment["first_observed_at"] >= view["history_1h"]["from_at"]
                   for segment in view["history_1h"]["segments"])

        rollback_reader = ObservationContextReader(path, boot_id="test-boot")
        assert rollback_reader.read_realtime_head(wall, monotonic_s=mono, policy_generation=5).current
        rollback = rollback_reader.read_realtime_head(
            wall - timedelta(seconds=1), monotonic_s=mono + 1, policy_generation=5,
        )
        assert rollback.current is None and rollback.scene_invalidated

        missing_at = wall + timedelta(seconds=5)
        lost = reader.read_realtime_head(missing_at, monotonic_s=mono + 5, policy_generation=5)
        assert lost.current is None and lost.scene_invalidated
        assert reader.read_current(missing_at) == (None, "context_changed")
        assert reader.read_current(wall)[0].visual_capture_id == "retained"


def test_activity_history_keeps_distinct_visual_times_under_existing_sharing_controls(tmp_path):
    path = tmp_path / "live.sqlite"
    reader = ObservationContextReader(path, boot_id="test-boot")

    def at(seconds):
        return NOW + timedelta(seconds=seconds)

    with ObservationStore(path, live=True, boot_id="test-boot") as store:
        store.begin_live_run(NOW, monotonic_s=0, policy_generation=9)
        recorder = ActivityRecorder(store, document_local_sources=("app:vscode",), on_sample_write=
                                    lambda sample, activity, _boundary, _changed: store._record_live_sample(
                                        sample.observed_at, sample.monotonic_s, sample=sample, activity=activity))

        def observe(seconds, *, locked=False):
            recorder.observe(ActivitySample(at(seconds), seconds, app_name="Visual Studio Code", window_id=1,
                                            window_title="notes.py — Visual Studio Code", screen_locked=locked))

        def save_capture(seconds, *, suffix="", kind="ide", late=False, summary=None):
            store.save_screen(MultimodalScreenObservation(
                f"capture-{seconds}{suffix}", recorder.current_activity.activity_id, at(seconds), "ready", "fixture",
                kind, activity_kind="work", scene_elements=("code editor",), screen_summary=summary,
                unfiltered_desktop=summary is not None,
            ), processed_at=at(seconds), realtime_eligible=not late)

        summaries = {30: "The editor shows a work document.", 60: "The user is browsing a gaming article."}
        for seconds in range(122):
            observe(seconds)
            if seconds % 30 == 0:
                save_capture(seconds, summary=summaries.get(seconds))
            if seconds == 90:
                save_capture(seconds, suffix="-same-time")
            if seconds == 105:
                save_capture(seconds, late=True)
        for seconds in (10, 40, 70, 100):
            publish(store, seconds)
        store.save_observation(Observation(
            "work", "working", None, ("screen_context",), at(121), None, 2, visual_capture_id="capture-120",
        ), published_at=at(121), monotonic_s=121)

        context = reader.read_realtime_context(at(121), monotonic_s=121, policy_generation=9)
        view = project_observation_context(context, share_summary=True)
        segment, = view["history_1h"]["segments"]
        visual_times = [at(seconds).isoformat() for seconds in (30, 60, 90, 120)]
        captures = segment["visual_observations"]
        assert [capture["observed_at"] for capture in captures] == visual_times
        assert [capture["summary"] for capture in captures] == [
            summaries[30], summaries[60], "The visible interface includes code editor.",
            "The visible interface includes code editor.",
        ]
        assert all(capture["activity_kind"] == "work" and capture["scene_elements"] == ["code editor"]
                   for capture in captures)
        focus = view["focus_evidence_2m"]
        assert focus["complete"] and not focus["gaps"]
        assert [entry["observed_at"] for entry in focus["visual_observations"]] == [
            at(seconds).isoformat() for seconds in (30, 60, 90, 90, 105, 120)
        ]
        assert [entry["late"] for entry in focus["visual_observations"]] == [
            False, False, False, False, True, False,
        ]
        assert segment["last_observed_at"] == at(121).isoformat()
        assert segment["sample_count"] == 8
        assert view["last_observed_activity"]["relation"] == "current"
        assert view["last_observed_activity"]["scene_observed_at"] == visual_times[-1]
        assert project_context(view, share=True)["history_1h"]["segments"][0]["visual_observations"] == captures
        assert "visual_observations" not in str(project_context(view, share=False))
        assert "visual_observations" not in str(project_observation_context(context))
        assert "visual_observations" not in str(project_observation_context(
            context, share_summary=True, excluded_apps=("visual studio code",),
        ))

        observe(122)
        save_capture(122, kind="browser")
        observe(123, locked=True)
        locked = project_observation_context(reader.read_observation_context(at(123)), share_summary=True)
        assert "visual_observations" not in str(locked)
        observe(124)
        save_capture(124, kind="browser")
        observe(125)
        late_summary = "A training video is visible."
        save_capture(125, kind="browser", late=True, summary=late_summary)
        observe(126)
        save_capture(126, kind="browser")
        after_gap = reader.read_observation_context(at(126)).to_dict()["history_1h"]
        assert after_gap["gaps"] == [{"last_observed_at": at(122).isoformat(), "next_observed_at": at(124).isoformat()}]
        assert after_gap["segments"][3]["summary"] == late_summary
        assert [[capture["observed_at"] for capture in part.get("visual_observations", ())]
                for part in after_gap["segments"]] == [
            visual_times, [at(122).isoformat()], [at(124).isoformat()], [], [at(126).isoformat()],
        ]


def test_activity_windows_count_authorized_time_before_visual_and_history_compression(tmp_path):
    path = tmp_path / "observation.sqlite"
    with ObservationStore(path, live=True) as store:
        work = record_activity(store, -4000, -600, "Editor")
        record_frame(store, work, -3900, "work", scene="ide", summary="An older editor view.")
        record_frame(store, work, -700, "work", scene="ide")
        study = record_activity(store, -600, -400, "Browser")
        record_frame(store, study, -600, "study")
        record_frame(store, study, -500, "leisure")
        record_activity(store, -400, -300, "Unclassified")
        other = record_activity(store, -300, -175, "Other")
        record_frame(store, other, -300, "other")
        record_activity(store, -260, -200, screen_locked=True)
        private = record_activity(store, -200, -100, "Browser", domain="sub.private.example")
        record_frame(store, private, -200, "leisure", summary="This account is excluded.")
        pending = record_activity(store, -50, 0, "Player")
        record_frame(store, pending, -50, "leisure", available=1)

    reader = ObservationContextReader(path)
    context = reader.read_observation_context(NOW)
    view = project_observation_context(context, share_summary=True, excluded_domains=("private.example",))
    windows = view["activity_windows"]
    assert windows["last_15m"]["seconds"] == {
        "work": 300, "study": 100, "leisure": 100, "other": 40, "unknown": 150, "unobserved": 210,
    }
    assert windows["last_60m"]["seconds"] == {**windows["last_15m"]["seconds"], "work": 3000}
    assert windows["last_15m"]["to_at"] == windows["last_60m"]["to_at"] == NOW.isoformat()
    assert windows["last_15m"]["percent"]["unobserved"] == 23.33
    assert windows["last_60m"]["percent"]["work"] == 83.33
    for window in windows.values():
        assert sum(window["seconds"].values()) == window["duration_s"]
        assert [item["observed_at"] for item in window["representative_summaries"]] == [
            at(-700).isoformat(), at(-500).isoformat(),
        ]
    assert "older editor view" not in str(windows) and "account is excluded" not in str(windows)
    assert "focus_evidence_2m" not in project_observation_context(context, share_summary=True, historical=True)
    assert project_observation_context(context)["activity_windows"]["last_60m"]["seconds"]["unobserved"] == 3600

    bounded = reader.read_observation_context(NOW, history_start=at(-700), history_end=at(-600)).to_dict()
    assert "activity_windows" not in bounded and "focus_evidence_2m" not in bounded


def test_focus_evidence_keeps_evicted_and_late_contradictions_and_reports_gaps(tmp_path):
    path = tmp_path / "observation.sqlite"
    doc_name = "research-cases.py"
    with ObservationStore(path, live=True) as store:
        editor = record_activity(store, -170, -119, "Visual Studio Code", document_name=doc_name,
                                 document_source="app:vscode", document_status="identified")
        record_frame(store, editor, -140, "work", scene="ide")
        first = record_activity(store, -119, -56, "Player")
        record_frame(store, first, -115, "study", summary="An online course is playing.")
        for offset in (-110, -100, -90, -80, -70):
            record_frame(store, first, offset, "leisure", scene="video" if offset % 20 else "game")
        record_activity(store, -59, -55, screen_locked=True)
        record_frame(store, first, -59, "leisure", summary="The hidden interval.")
        second = record_activity(store, -53, 0, "Player")
        for offset in (-50, -40, -30, -20, -10):
            record_frame(store, second, offset, "leisure", scene="video" if offset % 20 else "game",
                         summary=f"A clip mentioned {doc_name}." if offset == -10 else None)
        record_frame(store, second, -35, "study", summary="A lecture is visible.", late=True)

    reader = ObservationContextReader(path)
    view = reader.read_observation_context(NOW).to_dict()
    history = view["history_1h"]
    assert history["stats"]["omitted_segment_count"] > 0
    assert at(-115).isoformat() not in str(history["segments"])
    assert doc_name not in str(history["segments"][:-1])
    focus = view["focus_evidence_2m"]
    assert focus["complete"]
    assert focus["from_at"] == at(-120).isoformat() and focus["to_at"] == NOW.isoformat()
    assert {"from_at": at(-59).isoformat(), "to_at": at(-53).isoformat()} in focus["gaps"]
    frames = {entry["observed_at"]: entry for entry in focus["visual_observations"]}
    assert frames[at(-115).isoformat()]["activity_kind"] == "study"
    assert frames[at(-35).isoformat()]["activity_kind"] == "study"
    assert frames[at(-35).isoformat()]["late"] is True
    assert frames[at(-10).isoformat()]["summary"] is None
    assert at(-59).isoformat() not in frames
    assert doc_name not in str(view["activity_windows"])
    assert doc_name not in str(view)
    bounded = project_observation_context(
        reader.read_observation_context(NOW, history_start=at(-20), history_end=at(-5)),
        share_summary=True, historical=True,
    )
    assert bounded["history_1h"]["segments"][-1]["summary"] is None
    assert doc_name not in str(bounded)
    with_titles = project_observation_context(reader.read_observation_context(NOW), share_summary=True,
                                              document_sources=("app:vscode",))
    approved = project_observation_context(
        reader.read_observation_context(NOW, history_start=at(-160), history_end=at(-125)),
        share_summary=True, document_sources=("app:vscode",), historical=True,
    )
    assert doc_name not in str(with_titles)
    assert approved["history_1h"]["segments"][-1]["document_name"] == doc_name


def test_focus_evidence_does_not_treat_partially_recorded_spans_as_continuous(tmp_path):
    path = tmp_path / "observation.sqlite"
    with ObservationStore(path, live=True) as store:
        player = record_activity(store, -120, 0, "Player")
        record_frame(store, player, -80, "leisure")
        record_frame(store, player, -40, "leisure")
        with store.connection:
            store.connection.execute("UPDATE activities SET duration_s=1 WHERE activity_id=?", (player,))

    view = ObservationContextReader(path).read_observation_context(NOW).to_dict()
    assert view["activity_windows"]["last_15m"]["seconds"]["unobserved"] == 899
    focus = view["focus_evidence_2m"]
    assert focus["complete"] and len(focus["visual_observations"]) == 2
    assert focus["gaps"] == [{"from_at": at(-120).isoformat(), "to_at": NOW.isoformat()}]


def test_activity_timeline_policy_generation_uses_recorded_order_across_clock_rollback(tmp_path):
    path = tmp_path / "observation.sqlite"
    reader = ObservationContextReader(path, boot_id="boot")
    with ObservationStore(path, live=True, boot_id="boot") as store:
        store.begin_live_run(at(-80), policy_generation=4)
        old = record_activity(store, -70, -40, "Player")
        record_frame(store, old, -60, "leisure", summary="An older scope.")
        store.begin_live_run(at(-110), policy_generation=5)
        current = record_activity(store, -100, -30, "Editor")
        record_frame(store, current, -100, "work", scene="ide")
        store.begin_live_run(at(-25), policy_generation=5)
        record_activity(store, -20, 0, "Browser")

    matched = reader.read_realtime_context(NOW, include_current=False, policy_generation=5).to_dict()
    assert matched["activity_windows"]["last_15m"]["seconds"]["work"] == 70
    assert [entry["observed_at"] for entry in matched["focus_evidence_2m"]["visual_observations"]] == [
        at(-100).isoformat(),
    ]
    mismatch = reader.read_realtime_context(NOW, include_current=False, policy_generation=6).to_dict()
    assert mismatch["activity_windows"]["last_15m"]["seconds"]["unobserved"] == 900
    assert not mismatch["focus_evidence_2m"]["complete"]
    assert mismatch["focus_evidence_2m"]["visual_observations"] == []


def test_older_overlapping_owner_cannot_override_a_newer_locked_interval(tmp_path):
    path = tmp_path / "observation.sqlite"
    with ObservationStore(path, live=True) as store:
        editor = record_activity(store, -60, -20, "Editor")
        record_frame(store, editor, -50, "work", scene="ide")
        record_activity(store, -80, -30, screen_locked=True)
        record_activity(store, -25, 0, "Player")

    view = ObservationContextReader(path).read_observation_context(NOW).to_dict()
    assert view["activity_windows"]["last_15m"]["seconds"]["work"] == 0
    assert view["focus_evidence_2m"]["visual_observations"] == []


def test_desktop_discards_activity_timeline_if_policy_changed_without_a_live_estimate(tmp_path, monkeypatch):
    path = tmp_path / "observation.sqlite"
    with ObservationStore(path, live=True) as store:
        store.begin_live_run(at(-100), policy_generation=3)
        app = record_activity(store, -90, 0, "Editor")
        record_frame(store, app, -70, "work")

    reader = DesktopObservationInput(path)
    policies = iter(((True, 3), (True, 4)))
    monkeypatch.setattr(reader, "_policy", lambda **_: next(policies))
    view = reader.read_observation_context(NOW).to_dict()
    assert view["current_status"] == "context_changed"
    assert view["activity_windows"]["last_15m"]["seconds"]["unobserved"] == 900
    assert not view["focus_evidence_2m"]["complete"]


@pytest.mark.parametrize("missing_columns", [True, False])
def test_legacy_activity_timeline_keeps_coverage_and_marks_evidence_incomplete(tmp_path, missing_columns):
    path = tmp_path / "legacy.sqlite"
    with ObservationStore(path) as store:
        editor = record_activity(store, -100, 0, "Editor")
        record_frame(store, editor, -90, "work", scene="ide")
        with store.connection:
            if missing_columns:
                store.connection.execute("ALTER TABLE screen_observations DROP COLUMN processed_at")
                store.connection.execute("ALTER TABLE screen_observations DROP COLUMN realtime_eligible")
            else:
                store.connection.execute("UPDATE screen_observations SET processed_at = NULL")

    view = ObservationContextReader(path).read_observation_context(NOW).to_dict()
    seconds = view["activity_windows"]["last_15m"]["seconds"]
    assert (seconds["work"], seconds["unknown"], seconds["unobserved"]) == (90, 10, 800)
    focus = view["focus_evidence_2m"]
    assert not focus["complete"]
    assert [entry["late"] for entry in focus["visual_observations"]] == [True]


def test_document_history_stays_timed_and_separate_from_interruptibility(tmp_path):
    path = tmp_path / "live.sqlite"
    reader = ObservationContextReader(path, boot_id="test-boot")
    sources = ("app:vscode",)
    with ObservationStore(path, live=True, boot_id="test-boot") as store:
        store.begin_live_run(NOW, monotonic_s=100, policy_generation=9)
        recorder = ActivityRecorder(store, document_local_sources=sources, on_sample_write=
                                    lambda sample, activity, _boundary, _changed: store._record_live_sample(
                                        sample.observed_at, sample.monotonic_s, sample=sample, activity=activity))

        def observe(seconds, app, title, *, locked=False):
            at = NOW + timedelta(seconds=seconds)
            recorder.observe(ActivitySample(at, 100 + seconds, app_name=app, window_id=1, window_title=title,
                                            screen_locked=locked, idle_s=1))
            return recorder.current_activity, at

        a, at_a = observe(1, "Visual Studio Code", "notes-a.py — Visual Studio Code")
        _, at_dirty = observe(2, "Visual Studio Code", "● notes-a.py — Visual Studio Code")
        b, at_b = observe(61, "Visual Studio Code", "notes-b.py — Visual Studio Code")
        before = reader.read_realtime_context(at_b, monotonic_s=161, policy_generation=9)
        assert project_observation_context(before, document_sources=sources)["last_observed_activity"] == {
            "observed_at": at_b.isoformat(), "activity_kind": "unknown", "document_name": "notes-b.py",
            "relation": "current",
        }
        # A arrived late while B was on screen. Its actual capture time survives.
        store.save_screen(MultimodalScreenObservation(
            "a", a.activity_id, at_a, "ready", "fixture", "ide", None, "UNTRUSTED notes-a.py",
            activity_kind="work", scene_elements=("code editor",), visible_changes=("editor view changed",),
        ), processed_at=at_b, realtime_eligible=False)
        _, at_pet = observe(62, "Mochi", "Mochi chat sentinel")
        store.save_capture_health("ready", at_b)
        store.save_capture_health("skipped", at_pet, reason="mochi_foreground")
        context = reader.read_realtime_context(at_pet, monotonic_s=162, policy_generation=9)
        view = project_observation_context(context, share_summary=True, document_sources=sources,
                                           include_capture_health=True)
        assert view["current_context"] == {
            "foreground_app": "Mochi", "screen_locked": False, "idle_s": 1.0, "observed_at": at_pet.isoformat(),
        }
        assert view["last_observed_activity"]["relation"] == "previous"
        assert view["last_observed_activity"]["observed_at"] == at_b.isoformat()
        assert view["last_observed_activity"]["document_name"] == "notes-b.py"
        assert view["interruptibility"]["current"] is None
        segments = view["history_1h"]["segments"]
        assert [segment["document_name"] for segment in segments] == ["notes-a.py", "notes-b.py"]
        assert segments[0]["first_observed_at"] == at_a.isoformat()
        assert segments[0]["last_observed_at"] == at_dirty.isoformat()
        assert segments[0]["summary_observed_at"] == at_a.isoformat()
        assert segments[0]["scene_observed_at"] == at_a.isoformat()
        assert segments[0]["visible_changes"] == ["editor view changed"]
        assert segments[0]["change_observed_at"] == at_a.isoformat()
        assert segments[0]["document_change"] == {"type": "unsaved_changes_appeared",
                                                   "observed_at": at_dirty.isoformat()}
        assert view["history_1h"]["gaps"] == [{"last_observed_at": at_dirty.isoformat(),
                                               "next_observed_at": at_b.isoformat()}]
        assert view["capture_health"] == {"status": "skipped", "observed_at": at_pet.isoformat(timespec="microseconds"),
                                          "reason": "mochi_foreground", "last_ready_at": at_b.isoformat(),
                                          "last_skipped_at": at_pet.isoformat()}
        assert "Mochi chat sentinel" not in str(view)
        assert "UNTRUSTED" not in str(view)
        assert "notes-a.py" not in str(project_observation_context(context, share_summary=True))
        assert project_observation_context(context, document_sources=sources,
                                           excluded_apps=("visual studio",))["last_observed_activity"] is None

        expanded = reader.read_realtime_context(at_pet, monotonic_s=162, policy_generation=9,
                                                history_start=NOW, history_end=at_a)
        view = project_observation_context(expanded, share_summary=True, document_sources=sources)
        assert [part["document_name"] for part in view["history_1h"]["segments"]] == ["notes-a.py"]
        assert "document_change" not in view["history_1h"]["segments"][0]
        assert view["last_observed_activity"]["document_name"] == "notes-b.py"

        _, at_lock = observe(63, "Mochi", "Mochi chat sentinel", locked=True)
        locked = reader.read_realtime_context(at_lock, monotonic_s=163, policy_generation=9)
        view = project_observation_context(locked, share_summary=True, document_sources=sources,
                                           include_capture_health=True)
        assert view["current_context"]["screen_locked"] is True
        assert view["capture_health"] is None
        assert "notes-a.py" not in str(view) and "notes-b.py" not in str(view)
        lock_marker = reader.read_privacy_marker()
        observe(64, "Mochi", "Mochi chat sentinel")
        assert lock_marker[1:] == (1, True)
        assert reader.read_privacy_marker() == (lock_marker[0], 1, False)


def test_historical_foreground_keeps_covered_facts_and_shows_short_lock_gap(tmp_path):
    database = tmp_path / "context.sqlite"
    with ObservationStore(database, live=True) as store:
        recorder = ActivityRecorder(store, poll_interval_s=1)

        def observe(offset, app, locked=False):
            at = NOW + timedelta(seconds=offset)
            recorder.observe(ActivitySample(at, offset, app_name=app, window_id=1, idle_s=0, screen_locked=locked))
            return recorder.current_activity.activity_id

        activity_id = observe(0, "Editor")
        for offset in range(1, 100):
            observe(offset, "Editor")
        store.save_screen(ScreenObservation("legacy", activity_id, NOW, "ready", "legacy", "document", None,
                                            "A spreadsheet is open."))
        publish(store, 0, kind="work", summary="A spreadsheet is open.")
        observe(100, "Editor", locked=True)
        observe(101, "Editor", locked=True)
        observe(102, "Other Editor")

    reader = ObservationContextReader(database)
    historical = project_observation_context(reader.read_observation_context(NOW + timedelta(seconds=50)),
                                             share_summary=True, historical=True)
    assert historical["current_context"]["foreground_app"] == "Editor"
    assert historical["current_context"]["screen_locked"] is False
    assert historical["current_context"]["idle_s"] is None  # The exact old input sample is unavailable.
    assert historical["interruptibility"]["current"] is None
    assert historical["last_observed_activity"]["summary"] == "A spreadsheet is open."
    assert historical["last_observed_activity"]["activity_kind"] == "work"
    assert historical["last_observed_activity"]["summary_observed_at"] == historical["last_observed_activity"]["observed_at"]
    assert historical["last_observed_activity"]["scene_observed_at"] == historical["last_observed_activity"]["observed_at"]
    view = project_observation_context(reader.read_observation_context(NOW + timedelta(seconds=102)))
    assert view["history_1h"]["gaps"] == [{
        "last_observed_at": (NOW + timedelta(seconds=99)).isoformat(),
        "next_observed_at": (NOW + timedelta(seconds=102)).isoformat(),
    }]


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
