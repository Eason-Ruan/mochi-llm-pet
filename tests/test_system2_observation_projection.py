"""Which time scales reach the model on each route, including native tool continuations."""

from copy import deepcopy

import pytest

from deskpet.system2.dsh_projection import project_input, project_tool_result
from deskpet.system2.host import _pause_evidence_available
from deskpet.system2.prompt import select_prompt
from deskpet.system2.tool_schema import validate_tool_args
from evaluation.system2_prompt_ab import ago, fixture


def wire(case):
    data = fixture(case)
    selection = select_prompt(data["origin"], data["event"], data["context"], data["grants"],
                              semantic_background=data["semantic_background"])
    return project_input(data, share=True, selection=selection)[1]["context"]


def test_time_windows_and_frames_follow_reason_and_scope():
    chat = wire("chat_hour")
    assert "activity_windows" not in chat and "focus_evidence_2m" not in chat
    tick = wire("quiet_tick")
    assert set(tick["activity_windows"]) == {"last_15m"}
    assert "focus_evidence_2m" not in tick and "history_1h" not in tick
    focused = wire("focus_entertainment")
    assert len(focused["focus_evidence_2m"]["visual_observations"]) == 3
    assert focused["focus_evidence_2m"]["complete"]
    full = fixture("chat_hour")["context"]
    for share in (True, False):
        args = {"activity_window": "last_60m"}
        view = project_tool_result("context_read", {"view": full}, share=share, args=args)["view"]
        assert view.get("as_of") == ago(0)
        if share:
            assert set(view) == {"as_of", "activity_windows"}
            assert set(view["activity_windows"]) == {"last_60m"}
            assert view["activity_windows"]["last_60m"]["percent"]["work"] == 60
        else:
            assert set(view) == {"as_of"}
    narrow = project_tool_result("context_read", {"view": full}, share=True, args={"history_start": ago(35)})["view"]
    assert "activity_windows" not in narrow and "focus_evidence_2m" not in narrow
    default = project_tool_result("context_read", {"view": full}, share=True, args={})["view"]
    assert "history_1h" in default and "activity_windows" not in default
    matching = {"activity_window": "last_60m", "as_of": ago(0), "history_start": ago(3600), "history_end": ago(0)}
    matched = project_tool_result("context_read", {"view": full}, share=True, args=matching)["view"]
    assert set(matched["activity_windows"]) == {"last_60m"}
    outside = project_tool_result("context_read", {"view": full}, share=True,
                                  args={**matching, "history_start": ago(10)})["view"]
    assert "activity_windows" not in outside


def test_combined_wakes_keep_one_object_and_distinct_event_bindings():
    data = fixture("elapsed_and_reminder")
    ctx = data["context"]
    ctx["recheck_items"] = deepcopy(ctx["eligible_items"])
    selection = select_prompt("background", data["event"], ctx, data["grants"], semantic_background=True)
    projected = project_input(data, share=True, selection=selection)[1]
    wakes = projected["event"]["wakes"]
    assert wakes[1]["phase_elapsed"]["phase_id"] == "example-phase"
    assert wakes[2]["occurrence_id"] == "example-laundry:1"
    assert "phase_elapsed" not in projected["context"] and "phase_elapsed" not in projected["event"]
    assert len(projected["context"]["eligible_items"]) == 1 and projected["context"]["recheck_items"] == []
    assert data["context"]["phase_elapsed"] and data["context"]["recheck_items"]


def test_provider_encoded_presentation_keeps_the_intended_channel_and_sound():
    args = {"occurrence_ids": ["example:1"], "reason_code": "available",
            "presentation": '{"channel":"os_notification","level":"actionable","sound":false}'}
    result = validate_tool_args("reminder.present", args)
    assert result["presentation"] == {"channel": "os_notification", "level": "actionable", "sound": False}
    assert validate_tool_args("reminder.present", {**args, "presentation": "pet_badge"})["presentation"] == "pet_badge"
    with pytest.raises(ValueError):
        validate_tool_args("reminder.present", {**args, "presentation": '{"channel":"invalid-channel"}'})


@pytest.mark.parametrize("defect", ["gap", "late", "previous", "same_time", "truncated", "before_focus"])
def test_pause_requires_structural_evidence_from_this_focus(defect):
    ctx = wire("focus_entertainment")
    assert _pause_evidence_available(ctx)
    evidence = ctx["focus_evidence_2m"]
    if defect == "gap":
        evidence["gaps"] = [{"from_at": ago(22), "to_at": ago(10)}]
    elif defect == "late":
        evidence["visual_observations"][-1]["late"] = True
    elif defect == "previous":
        ctx["last_observed_activity"]["relation"] = "previous"
    elif defect == "same_time":
        frame = evidence["visual_observations"][-1]
        evidence["visual_observations"] = [frame, deepcopy(frame)]
    elif defect == "truncated":
        evidence["complete"] = False
    else:
        ctx["current_phase"]["begin_at"] = ago(15)
    assert not _pause_evidence_available(ctx)
