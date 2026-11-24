from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from deskpet.c_ui.desktop_system2 import DesktopSystem2
from deskpet.c_ui.refine_conversation_store import ConversationStore
from deskpet.mocks.clock import FakeClock
from deskpet.system2.protocol import UiEvent


@pytest.mark.parametrize("admitted", [False, True])
def test_expired_chat_outbox_reconciles_existing_admission_without_resending(tmp_path, admitted):
    message_id = "before-desktop-restart"
    store = ConversationStore(tmp_path / "business.sqlite3")
    store.queue_turn(message_id, "开始 25 分钟专注", {})
    now = datetime.now(UTC) + timedelta(minutes=5)
    acknowledgements = []
    queued_events = (() if not admitted else
                     (UiEvent(1, "reply", "existing-answer", message_id, {"text": "原操作已生效。"}),))

    def unexpected_submission(*args, **kwargs):
        pytest.fail("A past-due desktop input was submitted again")

    desktop = SimpleNamespace(
        chat=store, env=SimpleNamespace(clock=FakeClock(now)),
        host=SimpleNamespace(
            turn_timeout=timedelta(seconds=45),
            admission_for_message=lambda _: "original-admission" if admitted else None,
            submit_user=unexpected_submission,
            ui_events=lambda: queued_events, ack_ui_event=acknowledgements.append,
        ),
    )
    DesktopSystem2.pump(desktop)
    user, reply = store.projected_messages(include_parts=True)
    assert user[2] == reply[4] == message_id
    assert not user[3] and not store.queued_turns()
    assert acknowledgements == ([1] if admitted else [])
    assert ("原操作已生效" if admitted else "已过期") in reply[1]
    assert reply[6] == ({"source": "legacy" if admitted else "program", "text": reply[1]},)


def test_desktop_pump_projects_parts_for_chat_and_proposal_answers(tmp_path):
    store = ConversationStore(tmp_path / "business.sqlite3")
    message_id = "chat-message"
    store.queue_turn(message_id, "开始专注，再帮我看下今天的安排", {})
    store.record_admission(message_id, "chat-admission")
    parts = (
        {"source": "program", "text": "已开始专注。"},
        {"source": "model", "text": "你想先看今天的哪项安排？"},
    )
    proposal_parts = ({"source": "program", "text": "提议已接受。"},)
    events = [
        UiEvent(1, "reply", "chat-answer", message_id, {"parts": parts, "text": "旧版兼容正文"}),
        UiEvent(2, "reply", "proposal-answer", "proposal-button", {"parts": proposal_parts}),
        UiEvent(3, "reply", "invalid-answer", "other-message", {"parts": [{}]}),
    ]
    acknowledgements = []
    desktop = SimpleNamespace(chat=store, host=SimpleNamespace(
        ui_events=lambda: events, ack_ui_event=acknowledgements.append,
    ))

    assert DesktopSystem2.pump(desktop) == events

    user, reply, proposal, invalid = store.projected_messages(include_parts=True)
    assert acknowledgements == [1, 2, 3]
    assert not user[3] and reply[4:7] == (message_id, user[1], parts)
    assert reply[1] == "\n".join(part["text"] for part in parts)
    assert proposal[4:7] == ("proposal-button", None, proposal_parts)
    assert invalid[4:7] == ("other-message", None, ({"source": "program", "text": invalid[1]},))
    assert "无法显示" in invalid[1]
