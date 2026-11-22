"""Exercise the script's confirmation boundary independently of the Qt widgets."""
from deskpet.c_ui.refine_dialogue import DemoConversation, MILK_CONFIRM, MILK_REQUEST
from deskpet.contracts import UiAction


def _snapshot(now="2026-11-22T09:00:00Z"):
    return {
        "now": now,
        "tasks": [{
            "task_id": "milk",
            "status": "registered",
            "version": 4,
            "draft": {"content": "买牛奶", "mode": "soft"},
        }],
    }


def test_milk_confirm_uses_the_reviewed_absolute_time_and_version():
    chat = DemoConversation()
    assert chat.reply(MILK_REQUEST, _snapshot()).action is None
    accepted = chat.reply(MILK_CONFIRM, _snapshot("2026-11-22T18:00:00Z"))
    action = UiAction.model_validate(accepted.action)
    assert action.action_id == "reminder.edit"
    assert action.expected_task_version == action.surface_revision == 4
    assert action.payload["earliest_at"] == "2026-11-23T18:00:00+08:00"
    assert action.payload["mode"] == "hard"
    assert "今天 18:00" in accepted.text
    assert chat.reply("确认延后。", _snapshot()).action is None


def test_milk_confirmation_cannot_apply_after_target_or_refusal():
    chat = DemoConversation()
    chat.reply(MILK_REQUEST, _snapshot())
    assert chat.reply("否。", _snapshot()).action is None
    assert chat.reply("是。", _snapshot()).action is None
    chat.reply(MILK_REQUEST, _snapshot())
    expired = chat.reply(MILK_CONFIRM, _snapshot("2026-11-23T10:00:01Z"))
    assert expired.action is None and expired.follow_up
