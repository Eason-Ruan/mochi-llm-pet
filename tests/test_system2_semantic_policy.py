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


@pytest.mark.parametrize(("function", "error_code", "text"), [
    ("item.update", "NOT_FOUND", "找不到所请求的内容。"),
    ("item.update", "NOT_AUTHORIZED", "这次操作未获授权，未执行。"),
    ("phase.start", "DUPLICATE", "这次重复请求未再次执行。"),
    ("phase.start", "IDEMPOTENCY_MISMATCH", "这次请求与先前的操作冲突，未执行。"),
])
def test_rejected_action_receipt_reports_the_actual_error(function, error_code, text):
    reply = _result_reply(function, {"call_id": "try", "status": "rejected", "error_code": error_code})
    assert reply["status"] == "rejected"
    assert reply["outcome"] == "rejected"
    assert reply["error_code"] == error_code
    assert reply["text"] == text


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
