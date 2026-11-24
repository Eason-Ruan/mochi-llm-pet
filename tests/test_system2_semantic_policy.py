import pytest

from deskpet.system2.host import _result_reply
from deskpet.system2.semantic_policy import semantic_background_grants


@pytest.mark.parametrize(("action", "text", "expected"), [
    ("phase.start", "已开始。", "已开始 · 15 分钟"),
    ("phase.pause", "已暂停。", "已暂停。"),
    ("phase.adjust", "已调整时长。", "已调整 · 15 分钟"),
    ("phase.resume", "已继续。", "已继续。"),
    ("phase.end", "已结束。", "已结束。"),
])
def test_proposal_confirmation_reports_the_phase_action_that_committed(action, text, expected):
    receipt = {"call_id": "resolved", "status": "applied", "message": text,
               "resolved_function": action, "view": {"phase": {"planned_duration_s": 900}}}
    assert _result_reply("proposal.resolve", receipt)["text"] == expected


def test_unknown_state_does_not_cancel_background_grants_or_authorize_writes():
    context = {"current_status": "unknown", "enabled_settings": {"auto_adjust": True},
               "eligible_items": [{"occurrence_id": "drink:2"}], "recheck_items": []}
    grants = semantic_background_grants(context)
    assert grants["reminder.present"]["occurrences"] == ["drink:2"]
    assert "focus_suggestion" in grants["decision.recheck"]["purposes"]
    assert "phase.start" in grants["proposal.offer"]["functions"]
    assert {"phase.start", "item.create", "settings.update"}.isdisjoint(grants)

    context["enabled_settings"]["auto_adjust"] = False
    grants = semantic_background_grants(context)
    assert "proposal.offer" not in grants
    assert "focus_suggestion" not in grants["decision.recheck"]["purposes"]
    assert "reminder.present" in grants
