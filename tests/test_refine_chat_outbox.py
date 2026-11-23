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
    user, reply = store.projected_messages()
    assert user[2] == reply[4] == message_id
    assert not user[3] and not store.queued_turns()
    assert acknowledgements == ([1] if admitted else [])
    assert ("原操作已生效" if admitted else "已过期") in reply[1]
