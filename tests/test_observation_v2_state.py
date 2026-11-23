import json
from datetime import UTC, datetime, timedelta

from deskpet.observation_v2.state import StateObserver, aggregate_window, context_revision
from deskpet.observation_v2.vision import ScreenObservation


EPOCH = datetime(2026, 11, 22, 12, tzinfo=UTC)


def at(seconds):
    return EPOCH + timedelta(seconds=seconds)


def activity(start, end, app="Editor", *, idle=None, locked=None, sampled=None, duration=None):
    return {
        "started_at": at(start), "ended_at": at(end), "duration_s": end - start if duration is None else duration,
        "app_name": app, "idle_s": idle, "screen_locked": locked,
        "system_sampled_at": at(sampled) if sampled is not None else None,
    }


def screen(seconds, summary="Source code is visible.", *, kind="ide", status="ready"):
    return ScreenObservation(str(seconds), 1, at(seconds), status, "vision", kind, None, summary)


class TextModel:
    model = "text-model"

    def __init__(self, state="working", **overrides):
        self.output = {
            "activity_kind": "work", "state": state, "summary": None,
            "reason_codes": ["sustained_activity"], **overrides,
        }
        self.calls = []
        self.on_complete = lambda: None

    def complete(self, *, system, prompt, schema, image=None):
        assert image is None
        assert "untrusted data" in system
        assert set(schema["required"]) == {"activity_kind", "state", "summary", "reason_codes"}
        self.calls.append(json.loads(prompt))
        self.on_complete()
        return self.output


def test_window_clips_observed_durations_and_does_not_count_gaps_or_overlap():
    features = aggregate_window(
        [activity(-10, 20), activity(10, 25, "Browser", duration=12), activity(40, 70, None),
         activity(61, 70, "Future", idle=0, sampled=62)],
        [screen(10), screen(60, "Newest screen"), screen(61, "Future screen")], as_of=at(60),
    )
    assert features["activity_coverage_s"] == 44
    assert features["unobserved_s"] == 16
    assert features["foreground_coverage_s"] == 24
    assert features["foreground_durations"] == [
        {"app_name": "Editor", "window_title_redacted": None, "domain": None, "duration_s": 20},
        {"app_name": "Browser", "window_title_redacted": None, "domain": None, "duration_s": 4},
    ]
    assert features["current_foreground"] is None
    assert features["input_coverage_s"] == 0
    assert features["idle_s"] is None
    assert features["locked_s"] is None
    assert features["current_screen"]["screen_summary"] == "Newest screen"
    assert len(features["recent_screen_results"]) == 2


def test_contiguous_switch_and_measured_input_recovery_support_available():
    signals = [activity(4, 12, idle=8, sampled=12), activity(12, 16, "Browser", idle=0, sampled=16)]
    observer = StateObserver(TextModel("available"))
    features = observer.features(signals, [], as_of=at(16))
    assert features["recently_switched"] is True
    assert features["input_recovered"] is True
    assert features["idle_s"] == 0
    assert features["input_coverage_s"] == 12
    result = observer.observe(signals, [], as_of=at(16), now=lambda: at(16))
    assert result.state == "available"
    assert result.reason_codes == ("activity_transition",)
    assert result.valid_until == at(27)

    # A current app beyond a gap supplies no observed switching event.
    gap = [activity(4, 10), activity(12, 16, "Browser", idle=0, sampled=16)]
    assert not observer.features(gap, [], as_of=at(17))["recently_switched"]
    assert observer.observe(gap, [], as_of=at(17), now=lambda: at(17)).state == "unknown"


def test_video_classifies_activity_but_has_no_os_input_or_interruptibility():
    observer = StateObserver(TextModel("available", reason_codes=["input_recovered", "screen_context"]))
    recording = [activity(0, 20, None)]
    result = observer.observe(recording, [screen(20)], as_of=at(20), now=lambda: at(20))
    assert result.to_dict() == {
        "activity_kind": "work", "state": "unknown", "summary": None,
        "reason_codes": ["screen_context", "insufficient_signals"],
        "observed_at": at(20).isoformat(), "valid_until": at(35).isoformat(), "context_revision": 1,
    }
    assert observer.features(recording, [screen(20)], as_of=at(20))["recent_os_input"] is False


def test_current_lock_determines_away_and_expires_instead_of_inventing_presence():
    observer = StateObserver(TextModel())
    locked = [activity(0, 20, "Private window", locked=True, sampled=20)]
    first = observer.observe(locked, [], as_of=at(30), now=lambda: at(30))
    assert first.state == "away"
    assert first.valid_until == at(35)
    assert first.reason_codes == ("screen_locked",)
    assert "Private window" not in str(observer.transport.calls)
    expired = observer.observe(locked, [], as_of=at(35), now=lambda: at(35))
    assert expired.state == "unknown"
    assert expired.context_revision == first.context_revision + 1


def test_redacts_model_summary_and_excludes_future_and_unavailable_input():
    model = TextModel("working", summary="Contact jane@example.org about editing.")
    observer = StateObserver(model)
    finished = activity(0, 20, idle=1, sampled=19)
    unavailable = activity(20, 21, sampled=21)
    future = activity(21, 24, idle=0, sampled=24)
    pending_screen = {"captured_at": at(20), "processed_at": at(22), "status": "ready", "screen_kind": "game"}

    first = observer.observe([finished], [pending_screen], as_of=at(20), now=lambda: at(20))
    assert first.state == "working"
    assert first.summary == "Contact [邮箱] about editing."
    assert first.valid_until == at(33)
    assert model.calls[0]["recent_screen_results"] == []
    second = observer.observe([finished, unavailable, future], [], as_of=at(22), now=lambda: at(22))
    assert second.state == "unknown"
    assert model.calls[1]["idle_s"] is None


def test_reuses_revision_for_duplicate_samples_and_rejects_changed_context_in_flight():
    model = TextModel()
    observer = StateObserver(model)
    initial = [activity(0, 20, idle=0, sampled=20)]
    same_context = [activity(0, 21, idle=0, sampled=21)]
    updated = [*same_context, activity(21, 22, "Browser", idle=0, sampled=22)]

    one = observer.observe(initial, [], as_of=at(20), now=lambda: at(20))
    two = observer.observe(same_context, [], as_of=at(21), now=lambda: at(21))
    assert two.context_revision == one.context_revision
    assert context_revision(initial, [], as_of=at(20)) == context_revision(same_context, [], as_of=at(21))

    model.on_complete = lambda: observer.refresh_context(updated, [], as_of=at(22))
    assert observer.observe(same_context, [], as_of=at(21), now=lambda: at(22)) is None
    model.on_complete = lambda: None
    observed = observer.observe(updated, [], as_of=at(22), now=lambda: at(22))
    assert observed.context_revision == one.context_revision + 1
    assert observer.observe(initial, [], as_of=at(20), now=lambda: at(22)) is None


def test_stale_ttl_or_malformed_completion_is_discarded():
    model = TextModel()
    observer = StateObserver(model)
    signals = [activity(0, 20, idle=10, sampled=20)]
    times = iter((at(20), at(25)))
    assert observer.observe(signals, [], as_of=at(20), now=lambda: next(times)) is None
    assert len(model.calls) == 1
    assert observer.observe(signals, [], as_of=at(20), now=lambda: at(35)) is None
    assert len(model.calls) == 1

    model.output["reason_codes"] = ["open_ended_text"]
    assert observer.observe(signals, [], as_of=at(20), now=lambda: at(20)) is None


def test_output_semantics_change_revision_and_unknown_empty_window_is_nullable():
    model = TextModel()
    observer = StateObserver(model)
    first = observer.observe([], [], as_of=at(0), now=lambda: at(0))
    assert (first.activity_kind, first.state, first.summary) == ("unknown", "unknown", None)

    signals = [activity(0, 20, idle=0, sampled=20)]
    working = observer.observe(signals, [], as_of=at(20), now=lambda: at(20))
    model.output["state"] = "unknown"
    unknown = observer.observe(signals, [], as_of=at(20), now=lambda: at(20))
    assert unknown.state == "unknown"
    assert unknown.context_revision == working.context_revision + 1


def test_screen_scene_changes_stay_separate_from_os_activity_and_text_is_redacted():
    observations = [
        screen(2, kind="document"), screen(6, kind="ide"), screen(9, status="failed"),
        ScreenObservation("12", 1, at(12), "ready", "vision", "game", "Contact jane@example.org", None),
    ]
    model = TextModel("available")
    observer = StateObserver(model)
    assert observer.observe([], observations, as_of=at(12), now=lambda: at(12)).state == "unknown"
    prompt = model.calls[-1]
    assert prompt["screen_scene_switches"] == 2
    assert prompt["recently_switched_screen_scene"] is True
    assert prompt["recently_switched"] is False
    assert prompt["recent_os_input"] is False
    assert prompt["current_screen"]["visible_text_redacted"] == "Contact [邮箱]"
    assert not observer.features([], observations, as_of=at(28))["recently_switched_screen_scene"]


def test_distinct_contiguous_activities_and_zero_duration_os_history():
    model = TextModel("available")
    observer = StateObserver(model)
    segments = [
        dict(activity(0, 12), activity_id=11),
        dict(activity(12, 20, idle=0, sampled=20), activity_id=12),
        activity(17, 17, idle=8, sampled=17),
    ]
    features = observer.features(segments, [], as_of=at(20))
    assert features["activity_coverage_s"] == 20
    assert features["input_coverage_s"] == 8
    assert features["recently_switched"] is True
    assert features["input_recovered"] is True
    assert features["current_foreground_run_s"] == 8
    assert observer.observe(segments, [], as_of=at(20), now=lambda: at(20)).state == "available"
    assert "activity_id" not in json.dumps(model.calls)


def test_fallback_uses_current_time_and_invalidates_old_estimate():
    model = TextModel()
    observer = StateObserver(model)
    segments = [activity(0, 20, idle=0, sampled=20)]
    screens = [screen(20)]
    observed = observer.observe(segments, screens, as_of=at(20), now=lambda: at(20))

    def timeout():
        raise TimeoutError

    model.on_complete = timeout
    assert observer.observe(segments, screens, as_of=at(21), now=lambda: at(21)) is None
    fallback = observer.fallback(segments, screens, as_of=at(22))
    assert fallback.state == "unknown"
    assert fallback.activity_kind == "unknown"
    assert fallback.summary == "Source code is visible."
    assert fallback.reason_codes == ("insufficient_signals", "screen_context")
    assert fallback.context_revision == observed.context_revision + 1
    assert fallback.observed_at == at(22)
    assert fallback.valid_until == at(35)


def test_same_context_refresh_during_model_call_does_not_discard_result():
    model = TextModel()
    observer = StateObserver(model)
    before = [activity(0, 20, idle=0, sampled=20)]
    after = [activity(0, 21, idle=0, sampled=21)]
    model.on_complete = lambda: observer.refresh_context(after, [], as_of=at(21))
    result = observer.observe(before, [], as_of=at(20), now=lambda: at(21))
    assert result is not None
    assert result.context_revision == 1


def test_new_activity_point_changes_context_and_delayed_screen_stays_historical():
    editor = dict(activity(0, 20, idle=0, sampled=18), activity_id=1)
    browser = dict(activity(20, 20, "Browser", idle=0, sampled=20), activity_id=2)
    delayed = screen(19, "Editor screen")
    features = aggregate_window([editor, browser], [delayed], as_of=at(20))
    assert features["current_foreground"]["app_name"] == "Browser"
    assert features["current_foreground_run_s"] == 0
    assert features["activity_coverage_s"] == 20
    assert features["recently_switched"] is True
    assert features["current_screen"] is None
    assert features["recent_screen_results"][0]["screen_summary"] == "Editor screen"
    assert context_revision([editor], [delayed], as_of=at(20)) != context_revision([editor, browser], [delayed], as_of=at(20))
    current = ScreenObservation("20", 2, at(20), "ready", "vision", "browser", None, "Browser screen")
    assert aggregate_window([editor, browser], [delayed, current], as_of=at(20))["current_screen"]["screen_summary"] == "Browser screen"


def test_input_recovery_does_not_backfill_an_unobserved_gap():
    signals = [activity(0, 10, idle=8, sampled=10), activity(30, 31, idle=0, sampled=31)]
    features = aggregate_window(signals, [], as_of=at(31))
    assert features["recent_os_input"] is True
    assert features["input_recovered"] is False


def test_regular_capture_cadence_retains_visible_changes_and_distinct_foreground_revisions():
    scenes = [screen(0, kind="ide"), screen(30, kind="video")]
    assert aggregate_window([], scenes, as_of=at(30))["screen_scene_switches"] == 1

    first = dict(activity(0, 10), activity_id=1)
    second = dict(activity(10, 11), activity_id=2)
    third = dict(activity(11, 11), activity_id=3)
    previous = [first, second]
    current = [first, second, third]
    assert aggregate_window(previous, [], as_of=at(11))["recently_switched"] is True
    assert aggregate_window(current, [], as_of=at(11))["foreground_switches"] == 2
    assert context_revision(previous, [], as_of=at(11)) != context_revision(current, [], as_of=at(11))


def test_duplicate_model_reasons_are_normalized_and_lock_masks_historical_screen():
    model = TextModel("away", reason_codes=["screen_locked", "screen_locked"])
    observer = StateObserver(model)
    locked = [activity(0, 20, locked=True, sampled=20)]
    old_picture = [screen(18)]
    result = observer.observe(locked, old_picture, as_of=at(20), now=lambda: at(20))
    assert result.state == "away"
    assert result.reason_codes == ("screen_locked",)
    assert model.calls[-1]["current_screen"] is None
    assert model.calls[-1]["recent_screen_results"]
