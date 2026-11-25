import pytest

from deskpet.system2.prompt import (
    PROMPT_CORE, PROMPT_SECTIONS, PROMPT_VERSION, SPLIT_PROMPT_VERSION, SYSTEM_PROMPT, select_prompt,
)
from deskpet.system2.semantic_policy import semantic_background_grants


def _context(*, phase=None, auto_adjust=True, **fields):
    return {
        "as_of": "2026-11-25T10:00:00+00:00",
        "enabled_settings": {"auto_adjust": auto_adjust},
        "current_phase": phase,
        **fields,
    }


def _background(context, *wakes):
    return select_prompt(
        "background", {"wakes": list(wakes)}, context, semantic_background_grants(context),
        semantic_background=True,
    )


def test_real_chat_loads_all_three_static_sections_with_a_small_initial_snapshot():
    selection = select_prompt(
        "user", {"kind": "user_message", "trigger_reasons": ["user_message"]}, _context(), {},
    )
    assert selection.version == SPLIT_PROMPT_VERSION
    assert selection.section_names == ("chat_exchange", "chat_items_reminders", "chat_timers_habits_settings")
    assert selection.domains == {"chat"}
    assert selection.text == "\n\n".join((PROMPT_CORE, *(PROMPT_SECTIONS[name] for name in selection.section_names)))
    assert not selection.activity_15m
    assert not selection.focus_evidence_2m


@pytest.mark.parametrize(("kind", "reason"), [
    ("tick", "tick"), ("resume", "resume"), ("recheck", "focus_suggestion"),
    ("observation_ready", "focus_suggestion"),
])
def test_focus_wakes_can_suggest_starting_when_no_phase_exists(kind, reason):
    selection = _background(_context(), {"kind": kind, "reason": reason})
    assert selection.version == SPLIT_PROMPT_VERSION
    assert "focus_adjustment" in selection.section_names
    assert "focus_entertainment" not in selection.section_names
    assert selection.activity_15m
    assert not selection.focus_evidence_2m


@pytest.mark.parametrize(("state", "auto_adjust", "expects_evidence"), [
    ("running", True, True), ("paused", True, False), ("running", False, False),
])
def test_tick_adds_raw_pause_evidence_only_for_authorized_running_focus(state, auto_adjust, expects_evidence):
    phase = {"id": "f1", "version": 1, "kind": "focus", "state": state}
    selection = _background(_context(phase=phase, auto_adjust=auto_adjust), {"kind": "tick", "reason": "tick"})
    assert selection.activity_15m
    assert ("focus_adjustment" in selection.section_names) == auto_adjust
    assert ("focus_entertainment" in selection.section_names) == expects_evidence
    assert selection.focus_evidence_2m == expects_evidence


def test_pause_evidence_requires_grant_for_current_phase():
    context = _context(phase={"id": "f1", "version": 1, "kind": "focus", "state": "running"})
    grants = semantic_background_grants(context)
    grants["proposal.offer"]["target"] = "older-phase"
    selection = select_prompt(
        "background", {"kinds": ["tick"], "trigger_reasons": ["tick"]}, context, grants,
        semantic_background=True,
    )
    assert "focus_adjustment" in selection.section_names
    assert "focus_entertainment" not in selection.section_names
    assert not selection.focus_evidence_2m


def test_multi_reason_union_is_ordered_and_deduplicated_when_successor_started():
    phase = {"id": "rest", "version": 1, "kind": "short_break", "state": "running"}
    context = _context(phase=phase, eligible_items=[{"occurrence_id": "laundry:1"}])
    wakes = [
        {"kind": "recheck", "reason": "phase_elapsed",
         "phase_elapsed": {"phase_id": "f1", "phase_version": 2, "successor": phase}},
        {"kind": "reminder_ready", "reason": "window_open", "occurrence_id": "laundry:1"},
        {"kind": "tick", "reason": "tick"},
    ]
    expected = ("background_arbitration", "periodic_check", "phase_elapsed", "soft_reminder",
                "window_open", "recheck", "presentation")
    assert _background(context, *wakes).section_names == expected
    assert _background(context, *reversed(wakes)).section_names == expected
    legacy = {"kinds": ["recheck", "reminder_ready", "tick"],
              "trigger_reasons": ["phase_elapsed", "window_open", "tick"],
              "phase_elapsed": [wakes[0]["phase_elapsed"]]}
    assert select_prompt(
        "background", legacy, context, semantic_background_grants(context), semantic_background=True,
    ).section_names == expected


def test_tick_includes_actual_reminder_candidate_dependencies():
    context = _context(
        eligible_items=[{"occurrence_id": "laundry:1"}],
        recheck_items=[{"occurrence_id": "report:2", "rule": {"target_at": "2026-11-25T10:10:00+00:00"}}],
    )
    selection = _background(context, {"kind": "tick", "reason": "tick"})
    assert {"soft_reminder", "window_estimate", "window_open"} <= set(selection.section_names)


@pytest.mark.parametrize(("reason", "kind"), [
    ("scheduled_recheck", "recheck"), ("reminder_recheck", "observation_ready"),
])
def test_reminder_recheck_loads_window_close_for_actual_candidate(reason, kind):
    context = _context(recheck_items=[{
        "occurrence": {"id": "stretch:1", "window_close_at": "2026-11-25T09:50:00+00:00"},
        "occurrence_id": "stretch:1",
    }])
    selection = _background(context, {"kind": kind, "reason": reason, "occurrence_id": "stretch:1"})
    assert {"soft_reminder", "window_open", "window_close", "recheck"} <= set(selection.section_names)
    assert "focus_adjustment" not in selection.section_names
    assert not selection.focus_evidence_2m


def test_focus_presentation_recheck_requires_verified_proposal_and_phase_versions():
    phase = {"id": "f1", "version": 1, "kind": "focus", "state": "running"}
    proposal = {"id": "proposal-1", "version": 2, "function": "phase.pause",
                "args": {"id": "f1", "expected_version": 1}}
    context = _context(phase=phase, pending_proposals=[proposal])
    original = {"kind": "proposal", "proposal_id": "proposal-1", "proposal_version": 2}
    wake = {"kind": "recheck", "reason": "presentation_context_changed", "original_presentation": original}
    assert _background(context, wake).focus_evidence_2m
    assert not _background(context, {**wake, "original_presentation": {}}).focus_evidence_2m
    assert not _background({**context, "pending_proposals": [{**proposal, "version": 3}]}, wake).focus_evidence_2m


@pytest.mark.parametrize("event", [
    {"wakes": [{"kind": "recheck", "reason": "unknown"}]},
    {"wakes": [{"kind": "tick", "reason": "resume"}]},
    {"wakes": [{"kind": "tick", "reason": "tick"}, {"kind": "future", "reason": "future"}]},
    {"kinds": ["tick"], "trigger_reasons": ["tick", "phase_elapsed"]},
])
def test_unknown_background_combinations_fall_back_to_v13(event):
    selection = select_prompt("background", event, _context(), {}, semantic_background=True)
    assert selection.version == PROMPT_VERSION
    assert selection.text == SYSTEM_PROMPT
    assert not selection.section_names


def test_v13_remains_available_for_nonsemantic_background_and_split_prompt_opt_out(monkeypatch):
    event = {"kind": "background", "kinds": ["tick"], "trigger_reasons": ["tick"]}
    assert select_prompt("background", event, _context(), {}).version == PROMPT_VERSION
    monkeypatch.setenv("MOCHI_SYSTEM2_SPLIT_PROMPT", "0")
    assert select_prompt("background", event, _context(), {}, semantic_background=True).version == PROMPT_VERSION
    chat = {"kind": "user_message", "trigger_reasons": ["user_message"]}
    assert select_prompt("user", chat, _context(), {}).version == PROMPT_VERSION
