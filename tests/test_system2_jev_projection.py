from datetime import UTC, datetime, timedelta
import json

from deskpet.system2.jev_projection import JevReview, capture_jev_review, project_jev_snapshot


NOW = datetime(2026, 7, 10, 10, tzinfo=UTC)


def _stamp(at, seconds=0):
    return (at + timedelta(seconds=seconds)).isoformat()


def _frame(at, *, kind="work", elements=("document editor",), late=False, **private):
    return {"observed_at": at.isoformat(), "activity_kind": kind, "late": late,
            "scene_elements": list(elements), **private}


def _context(at=NOW, *, frames=None, phase=None, auto=True, cards=True, gaps=None):
    frames = [_frame(at - timedelta(seconds=5))] if frames is None else frames
    latest = frames[-1] if frames else {"activity_kind": "unknown", "observed_at": None}
    return {
        "as_of": at.isoformat(),
        "trigger_reasons": ["tick"],
        "current_phase": phase,
        "enabled_settings": {"auto_adjust": auto, "version": 1},
        "display_capabilities": {"pet_card": cards},
        "eligible_items": [],
        "pending_proposals": [],
        "recent_contacts": [],
        "current_context": {"screen_locked": False},
        "interruptibility": {"status": "fresh", "current": {"state": "working"}},
        "last_observed_activity": {**latest, "relation": "current", "scene_observed_at": latest["observed_at"]},
        "activity_windows": {"last_15m": {"from_at": _stamp(at, -900), "to_at": _stamp(at), "seconds": {
            "work": 75, "study": 0, "leisure": 0, "other": 0, "unknown": 10, "unobserved": 815,
        }}},
        "focus_evidence_2m": {
            "from_at": _stamp(at, -120), "to_at": _stamp(at), "complete": True,
            "gaps": gaps or [], "visual_observations": frames,
        },
    }


def _phase(at=NOW, *, title="Write a report", **kwargs):
    return {"kind": "focus", "state": "running", "begin_at": _stamp(at, -90),
            "id": "private-phase-id", "version": 2, "title": title, **kwargs}


def _reminder(at, *, until=600):
    return {"id": "private-task-id", "version": 3, "title": "Private task name",
            "occurrence": {"id": "private-task-id:1"}, "rule": {"remind_by": _stamp(at, until)}}


def _project(context, review=None, *, share_text=True):
    return project_jev_snapshot(context, next_tick_in_s=58, share_text=share_text, last_review=review)


def test_projection_exposes_only_closed_semantic_facts_in_read_only_state():
    frame = _frame(NOW - timedelta(seconds=10), kind="study", elements=("lecture video", "alice.example"),
                   summary="Alice opens Project Maple at alice.example", screenshot="raw screenshot",
                   capture_id="private-capture", window_title="Project Maple", document_name="Project Maple")
    context = _context(frames=[frame], phase=_phase())
    context["eligible_items"] = [_reminder(NOW)]
    context["jev_activity_scope"] = "private-domain.example|private document"
    context["publication_cursor"] = 800
    context["history_1h"] = {"transcript": "private transcript"}
    before = json.dumps(context, sort_keys=True)

    snapshot = _project(context)

    assert snapshot.skip_reason is None
    assert snapshot.state["phase"] == {
        "kind": "focus", "state": "running", "begin_at": _stamp(NOW, -90), "goal_summary": "Write a report.",
    }
    assert snapshot.state["activity"] == {
        "kind": "study", "state": "working", "relation": "current", "scene_observed_at": _stamp(NOW, -10),
        "interruptibility_status": "fresh", "summary": "The visible interface includes lecture video.",
    }
    assert snapshot.state["candidates"] == [
        {"kind": "reminder", "seconds_to_latest": 600}, {"kind": "focus_pause", "seconds_to_latest": None},
    ]
    assert snapshot.state["last_15m"]["seconds"]["unobserved"] == 815
    assert snapshot.state["focus_2m"]["captures"] == [{
        "observed_at": _stamp(NOW, -10), "late": False, "activity_kind": "study",
        "summary": "The visible interface includes lecture video.",
    }]
    persisted = snapshot.review.to_dict()
    output = json.dumps(snapshot.state) + json.dumps(persisted) + repr(snapshot)
    assert not any(fragment in output for fragment in (
        "Project Maple", "alice.example", "private-", "raw screenshot", "private transcript", "publication_cursor",
    ))
    assert JevReview.from_dict(persisted) == snapshot.review
    assert JevReview.from_dict({**persisted, "activity": "raw activity"}) is None
    assert json.dumps(context, sort_keys=True) == before


def test_structured_only_retains_current_and_focus_evidence_with_stable_local_digest():
    context = _context(phase=_phase())
    shared = _project(context)
    withheld = _project(context, share_text=False)

    assert withheld.skip_reason is None
    assert withheld.state["activity"]["summary"] is None
    assert withheld.state["phase"]["goal_summary"] is None
    assert withheld.state["focus_2m"]["captures"][0]["summary"] is None
    assert withheld.state["focus_2m"]["captures"][0]["activity_kind"] == "work"
    assert withheld.fingerprint == shared.fingerprint
    assert capture_jev_review(context, share_text=False) == shared.review


def test_nonallowlisted_goal_and_summary_are_omitted_and_locked_observation_is_unavailable():
    frame = _frame(NOW - timedelta(seconds=10), elements=(), summary="Maya editing Project Maple")
    context = _context(phase=_phase(title="Write a report for Maya"), frames=[frame])
    visible = _project(context)

    assert visible.state["phase"]["goal_summary"] is None
    assert visible.state["activity"]["summary"] is None
    assert visible.state["focus_2m"]["captures"][0]["summary"] is None

    context["current_context"]["screen_locked"] = True
    locked = _project(context)
    assert locked.skip_reason == "observation_unavailable"
    assert locked.state["activity"]["kind"] is None
    assert locked.state["last_15m"] is None
    assert "focus_2m" not in locked.state


def test_known_empty_candidate_set_is_distinct_from_read_failure_and_truncation():
    context = _context(auto=False)
    empty = _project(context)
    assert empty.state["candidates"] == [] and empty.local_defer_reason == "no_candidate"

    context["eligible_items"] = None
    unavailable = _project(context)
    assert unavailable.state["candidates"] is None and unavailable.skip_reason == "candidate_unavailable"
    assert unavailable.local_defer_reason is None

    context["eligible_items"] = [_reminder(NOW)]
    context["candidate_items_truncated"] = True
    truncated = _project(context)
    assert truncated.state["candidates"] is None and truncated.skip_reason == "candidate_truncated"
    assert truncated.local_defer_reason is None


def test_near_deadline_mixed_wake_and_custom_timing_preserve_system2_path():
    context = _context()
    context["eligible_items"] = [_reminder(NOW, until=58)]
    assert _project(context).skip_reason == "deadline_near"

    context["trigger_reasons"].append("scheduled_recheck")
    assert _project(context).skip_reason == "non_periodic"

    context["trigger_reasons"] = ["tick"]
    context["eligible_items"] = [_reminder(NOW)]
    context["eligible_items"][0]["rule"]["timing_context"] = "After the private call with Ana"
    snapshot = _project(context)
    assert snapshot.skip_reason == "candidate_context_unavailable"
    assert "Ana" not in json.dumps(snapshot.state)


def test_previous_activity_is_retained_but_stale_interruptibility_fails_open():
    context = _context()
    context["last_observed_activity"]["relation"] = "previous"
    context["interruptibility"]["status"] = "context_changed"
    snapshot = _project(context)

    assert snapshot.skip_reason == "observation_unavailable"
    assert snapshot.local_defer_reason is None
    assert snapshot.state["activity"]["relation"] == "previous"
    assert snapshot.state["activity"]["interruptibility_status"] == "context_changed"
    assert snapshot.state["activity"]["state"] is None


def test_active_focus_window_keeps_late_counterevidence_and_clips_at_phase_begin():
    cutoff = -90
    early = _frame(NOW - timedelta(seconds=100), kind="leisure", elements=("video player",))
    leisure = _frame(NOW - timedelta(seconds=70), kind="leisure", elements=("video player",))
    course = _frame(NOW - timedelta(seconds=55), kind="study", elements=("lecture video",), late=True)
    work = _frame(NOW - timedelta(seconds=15))
    gaps = [{"from_at": _stamp(NOW, -110), "to_at": _stamp(NOW, -85)}]
    context = _context(phase=_phase(), frames=[early, leisure, course, work], gaps=gaps)
    snapshot = _project(context)
    window = snapshot.state["focus_2m"]

    assert snapshot.skip_reason is None
    assert window["from_at"] == _stamp(NOW, cutoff)
    assert window["gaps"] == [{"from_at": _stamp(NOW, cutoff), "to_at": _stamp(NOW, -85)}]
    assert [capture["activity_kind"] for capture in window["captures"]] == ["leisure", "study", "work"]
    assert window["captures"][1]["late"] is True
    assert snapshot.state["activity"]["kind"] == "work"

    context["current_phase"]["state"] = "paused"
    assert "focus_2m" not in _project(context).state
    context["current_phase"]["state"] = "running"
    context["enabled_settings"]["auto_adjust"] = False
    assert "focus_2m" not in _project(context).state


def test_truncated_or_conflicting_focus_window_cannot_produce_a_local_defer():
    frames = [_frame(NOW - timedelta(seconds=second)) for second in range(70, 4, -5)]
    context = _context(phase=_phase(), frames=frames)
    too_many = _project(context)
    assert too_many.skip_reason == "evidence_incomplete"
    assert too_many.local_defer_reason is None
    assert too_many.state["focus_2m"] == {"complete": False, "captures": None, "gaps": None}

    a = _frame(NOW - timedelta(seconds=50), capture_id="same-id")
    b = _frame(NOW - timedelta(seconds=30), capture_id="same-id")
    duplicate = _project(_context(phase=_phase(), frames=[a, b]))
    assert duplicate.skip_reason == "evidence_incomplete"

    start = _context()
    del start["focus_evidence_2m"]
    unavailable = _project(start)
    assert unavailable.state["work_evidence"] is None
    assert unavailable.skip_reason == "evidence_incomplete"


def test_fingerprint_ignores_routine_ticks_preserves_actual_evidence_delta():
    first = _project(_context())
    assert first.state["work_evidence"] == {"sample_count": 1, "span_s": 0, "continuous": False}
    assert "focus_2m" not in first.state

    tick = NOW + timedelta(seconds=60)
    same = _context(tick)
    same["publication_cursor"] = 100
    same["activity_windows"]["last_15m"]["seconds"].update(work=110, unobserved=780)
    unchanged = _project(same, first.review)
    assert unchanged.state["changed_since_review"] == []
    assert unchanged.fingerprint == first.fingerprint
    assert unchanged.local_defer_reason == "unchanged_snapshot"

    a, b = _frame(NOW - timedelta(seconds=5)), _frame(tick - timedelta(seconds=5))
    accumulated = _project(_context(tick, frames=[a, b]), first.review, share_text=False)
    assert accumulated.state["work_evidence"] == {"sample_count": 2, "span_s": 60, "continuous": True}
    assert "work_evidence_accumulated" in accumulated.state["changed_since_review"]
    assert accumulated.fingerprint != first.fingerprint

    changed = _context(tick, frames=[a, _frame(tick - timedelta(seconds=5), kind="leisure", elements=("video player",))])
    different_activity = _project(changed, first.review)
    assert different_activity.state["activity"]["summary"] == "The visible interface includes video player."
    assert "activity_changed" in different_activity.state["changed_since_review"]
    assert different_activity.local_defer_reason is None


def test_new_spaced_entertainment_capture_is_material_even_with_older_work_capture():
    work = _frame(NOW - timedelta(seconds=70))
    entertainment = _frame(NOW - timedelta(seconds=5), kind="leisure", elements=("video player",))
    first = _project(_context(phase=_phase(), frames=[work, entertainment]))

    tick = NOW + timedelta(seconds=30)
    second = _frame(tick - timedelta(seconds=5), kind="leisure", elements=("video player",))
    next_snapshot = _project(_context(tick, phase=_phase(), frames=[work, entertainment, second]), first.review)

    assert next_snapshot.review.activity == first.review.activity
    assert next_snapshot.fingerprint != first.fingerprint
    assert next_snapshot.state["changed_since_review"] == ["evidence_changed"]
    assert next_snapshot.local_defer_reason is None


def test_pending_and_feedback_require_exact_opportunity_and_real_display():
    first = _project(_context())
    at = NOW + timedelta(seconds=60)
    context = _context(at)
    context["jev_exact_feedback_provenance"] = True
    offer = {
        "function": "phase.start", "state": "pending", "id": "private-proposal-id", "version": 1,
        "args": {"kind": "focus"}, "subject_refs": [], "expected_versions": {},
        "created_at": _stamp(NOW, 5), "valid_until": _stamp(NOW, 300),
        "jev_origin_activity": first.review.activity, "jev_origin_phase": first.review.phase,
        "jev_origin_review_at": first.review.as_of, "jev_displayed": True,
    }
    receipt = {"kind": "proposal", "subject_ref": "private-proposal-id", "response": "none",
               "exposure": "painted", "at": _stamp(NOW, 10)}
    context["pending_proposals"] = [offer]
    context["recent_contacts"] = [receipt]
    same = _project(context)
    assert same.state["pending_offer"] is True
    assert same.state["same_opportunity"] is True
    assert same.state["last_feedback"] == "unanswered"
    assert same.local_defer_reason == "pending_offer"

    context["eligible_items"] = [_reminder(at)]
    competing = _project(context)
    assert competing.state["same_opportunity"] is None
    assert competing.local_defer_reason is None
    context["eligible_items"] = []

    offer["jev_displayed"] = False
    assert _project(context).local_defer_reason is None
    context["pending_proposals"] = []
    context["jev_feedback_proposals"] = {offer["id"]: offer}
    receipt["response"] = "refused"
    context["jev_activity_scope"] = "new-private-task"
    changed = _project(context)
    assert changed.state["last_feedback"] == "refused"
    assert changed.state["same_opportunity"] is False
    assert changed.local_defer_reason is None
