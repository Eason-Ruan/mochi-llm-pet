import pytest

from deskpet.system2.host import _result_reply
from deskpet.system2.intent import allows, chat_grants, proposal_button_grants
from deskpet.system2.semantic_policy import semantic_background_grants
from deskpet.system2.tool_schema import READ_TOOLS, tool_definitions


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


@pytest.mark.parametrize("utterance", [
    "目前是什么状态，还剩多久？",
    "「开始二十分钟专注」",
    "我先把待办补上，计时就免了。",
])
def test_chat_tool_scope_is_independent_of_the_message(utterance, monkeypatch):
    monkeypatch.setenv("MOCHI_SYSTEM2_SEMANTIC_CHAT", "0")
    grants = chat_grants({"utterance": utterance}, {"items": []})
    background_only = {"pet.present", "reminder.present", "reminder.recheck", "decision.recheck",
                       "observation.wait_fresh"}
    registered = {tool.canonical_name for tool in tool_definitions}
    assert READ_TOOLS | grants.keys() == registered - background_only


def test_chat_write_scope_checks_bound_objects_and_protected_phase_fields():
    grants = chat_grants({"utterance": "安排一下", "reply_to_proposal_id": "card-1",
                          "reply_to_proposal_version": 3},
                         {"items": [{"id": "issue-1", "type": "issue"}, {"id": "other", "type": "reminder"}]})
    versions = {"item:issue-1": 1, "item:other": 2, "proposal:card-1": 3, "proposal:card-2": 2}
    assert allows(grants, "item.archive", {"id": "issue-1"}, versions=versions)
    assert not allows(grants, "item.archive", {"id": "guessed"}, versions=versions)
    assert allows(grants, "phase.start", {"issue_links": ["issue-1"]}, versions=versions)
    assert not allows(grants, "phase.start", {"issue_links": ["other"]}, versions=versions)
    assert not allows(grants, "phase.start", {"previous_phase_id": "previous"}, versions=versions)
    accepted = {"id": "card-1", "answer": "accept", "expected_version": 3}
    assert allows(grants, "proposal.resolve", accepted, versions=versions)
    assert not allows(grants, "proposal.resolve", accepted | {"id": "card-2"}, versions=versions)
    assert not allows(grants, "proposal.resolve", accepted | {"expected_version": 2}, versions=versions)

    button_grants = proposal_button_grants(accepted)
    assert allows(button_grants, "proposal.resolve", accepted)
    assert not allows(button_grants, "proposal.resolve", accepted | {"answer": "reject"})
    assert not allows(button_grants, "proposal.resolve", accepted | {"amendments": {"title": "unexpected"}})
