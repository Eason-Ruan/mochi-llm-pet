from contextlib import closing
from datetime import UTC, datetime
from hashlib import sha256
import json
import sqlite3
from types import SimpleNamespace

import pytest

from deskpet.c_ui.refine_conversation_store import ConversationStore


def test_structured_reply_is_atomic_and_preserves_turn_on_retry_and_restart(tmp_path):
    path = tmp_path / "business.sqlite3"
    store = ConversationStore(path)
    store.queue_turn("message", "先提醒我，再问一下", {"selected_ref": "original"})
    store.save_draft("unsent draft")
    store.queue_turn("message", "重复提交", {"selected_ref": "replacement"})
    assert store.queued_turns()[0][2]["selected_ref"] == "original"

    with pytest.raises(ValueError):
        store.record_answer("answer", "message", parts=[{"source": "user", "text": "bad answer"}])
    assert store.projection_version() == 1
    assert store.projected_messages()[0][3] == 1

    parts = (
        {"source": "program", "text": "已设置提醒。"},
        {"source": "model", "text": "还有一件事需要确认。\n你希望几点？"},
    )
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("""CREATE TRIGGER reject_reply BEFORE INSERT ON refine_system2_chat_replies
            BEGIN SELECT RAISE(ABORT, 'reply rejected'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="reply rejected"):
        store.record_answer("answer", "message", parts=parts)
    assert store.projection_version() == 1
    assert store.projected_messages()[0][3] == 1
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("DROP TRIGGER reject_reply")

    store.record_admission("message", "admission")
    store.record_answer("answer", "message", parts=parts)

    reopened = ConversationStore(path)
    reopened.record_answer("answer", "message", parts=parts)
    text = "\n".join(part["text"] for part in parts)
    assert reopened.projection_version() == 2
    assert reopened.admission_for("message") == "admission"
    assert not reopened.queued_turns()
    assert reopened.load() == ([("你", "先提醒我，再问一下"), ("Mochi", text)], "unsent draft", None)
    assert reopened.projected_messages() == [
        ("你", "先提醒我，再问一下", "message", 0, None, None),
        ("Mochi", text, None, 0, "message", "先提醒我，再问一下"),
    ]
    assert [row[-1] for row in reopened.projected_messages(include_parts=True)] == [
        ({"source": "user", "text": "先提醒我，再问一下"},), parts,
    ]


def test_dialogue_and_desktop_messages_are_labeled_on_restart(tmp_path):
    path = tmp_path / "business.sqlite3"
    store = ConversationStore(path)
    action = {"event_id": "business-action"}
    turn = SimpleNamespace(action=action, text="已调整计时。", follow_up=False)
    assert store.begin_turn("你 · 模拟语音", "调整计时", turn) == "business-action"

    reopened = ConversationStore(path)
    assert reopened.load()[2] == ("business-action", vars(turn))
    reopened.finish_turn("business-action", turn.text)
    reopened.finish_turn("business-action", "重复完成")
    reopened.append_message("Mochi", "本地更新", source="program")
    reopened.append_message("Mochi", "来源不详")
    reopened.record_status("expired-desktop", "desktop", "消息已过期。")
    rows = ConversationStore(path).projected_messages(include_parts=True)
    assert [row[6][0] for row in rows] == [
        {"source": "user", "text": "调整计时"},
        {"source": "program", "text": "已调整计时。"},
        {"source": "program", "text": "本地更新"},
        {"source": "legacy", "text": "来源不详"},
        {"source": "status", "text": "消息已过期。"},
    ]
    assert rows[1][4:6] == ("business-action", "调整计时")


def test_reply_with_unrecognized_parts_keeps_legacy_text(tmp_path):
    store = ConversationStore(tmp_path / "business.sqlite3")
    store.queue_turn("message", "继续", {})
    store.record_answer("answer", "message", "保留这条消息。", parts=[{"source": "unexpected", "text": "来源不明"}])
    user, reply = store.projected_messages(include_parts=True)
    assert not user[3]
    assert reply[6] == ({"source": "legacy", "text": "保留这条消息。"},)


def test_legacy_backfill_requires_matching_outbox_and_exact_receipt_prefix(tmp_path):
    path = tmp_path / "business.sqlite3"
    receipt = "已设置提醒「汇丰转账\n补记」。"
    follow_up = "还需要补充金额。\n你希望转多少？"
    full_reply = f"{receipt}\n{follow_up}"
    single_receipt = "已取消提醒。"
    aggregated = {"text": full_reply, "status": "partial", "actions": [{"text": receipt}]}
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript("""
            CREATE TABLE refine_chat_messages (
                sequence INTEGER PRIMARY KEY, speaker TEXT NOT NULL, body TEXT NOT NULL
            );
            CREATE TABLE refine_system2_chat_turns (
                message_id TEXT PRIMARY KEY, message_seq INTEGER NOT NULL UNIQUE,
                input TEXT NOT NULL, admission_id TEXT, pending INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE refine_system2_chat_replies (
                answer_id TEXT PRIMARY KEY, message_seq INTEGER NOT NULL UNIQUE, reply_to_message_id TEXT
            );
            CREATE TABLE s2_ui_outbox (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, user_scope TEXT NOT NULL,
                answer_id TEXT NOT NULL UNIQUE, reply_to_message_id TEXT NOT NULL,
                payload TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO refine_chat_messages VALUES (1, '你', '稍后提醒我汇丰转账');
            INSERT INTO refine_system2_chat_turns VALUES ('turn', 1, '{}', 'admission', 0);
        """)
        entries = [
            (full_reply, "turn", aggregated),
            (full_reply, "different-turn", aggregated),
            (full_reply, "turn", aggregated | {"actions": [{"text": "已设置提醒。"}]}),
            (full_reply, "turn", aggregated | {"text": full_reply + "！"}),
            ("今天天气不错。", "turn", {"text": "今天天气不错。", "status": "answered"}),
            (single_receipt, "turn", {"text": single_receipt, "outcome": "committed", "command_id": "command"}),
            (full_reply, "turn", '{"malformed"'),
        ]
        for seq, (body, outbox_turn, event) in enumerate(entries, start=2):
            answer_id = f"answer-{seq}"
            db.execute("INSERT INTO refine_chat_messages VALUES (?, 'Mochi', ?)", (seq, body))
            db.execute("INSERT INTO refine_system2_chat_replies VALUES (?, ?, 'turn')", (answer_id, seq))
            payload = json.dumps(event, ensure_ascii=False) if isinstance(event, dict) else event
            db.execute("""INSERT INTO s2_ui_outbox(user_scope, answer_id, reply_to_message_id, payload, acknowledged)
                VALUES ('default', ?, ?, ?, 1)""", (answer_id, outbox_turn, payload))
        db.execute("""INSERT INTO s2_ui_outbox(user_scope, answer_id, reply_to_message_id, payload)
            VALUES ('default', 'redelivered', 'turn', ?)""", (json.dumps(aggregated),))

    reopened = ConversationStore(path)
    rows = reopened.projected_messages(include_parts=True)
    assert rows[1][1:6] == (full_reply, None, 0, "turn", "稍后提醒我汇丰转账")
    assert rows[1][6] == ({"source": "program", "text": receipt}, {"source": "model", "text": follow_up})
    for index in (2, 3, 4, 5, 7):
        assert rows[index][6] == ({"source": "legacy", "text": rows[index][1]},)
    assert rows[6][6] == ({"source": "program", "text": single_receipt},)
    assert reopened.projection_version() == 8

    reopened.record_answer("redelivered", "turn", full_reply)
    restarted = ConversationStore(path)
    assert restarted.projected_messages(include_parts=True)[:-1] == rows
    assert restarted.projected_messages(include_parts=True)[-1][6] == rows[1][6]
    assert restarted.projection_version() == 9


def test_clear_history_requires_completed_turns_and_preserves_draft_across_restart(tmp_path):
    path = tmp_path / "business.sqlite3"
    store = ConversationStore(path)
    store.queue_turn("old", "旧问题", {})
    store.save_draft("未发送的草稿")
    assert not store.clear_history()
    assert store.load() == ([("你", "旧问题")], "未发送的草稿", None)
    assert store.queued_turns()[0][:2] == ("old", "旧问题")
    store.record_answer("old-answer", "old", "旧回复")

    turn = SimpleNamespace(action={"event_id": "local"}, text="专注已开始", follow_up=False)
    store.begin_turn("你", "开始专注", turn)
    assert not store.clear_history()
    store.finish_turn("local", turn.text)
    store.save_draft("未发送的草稿")
    last_version = store.projection_version()

    assert store.clear_history()
    reopened = ConversationStore(path)
    assert reopened.load() == ([], "未发送的草稿", None)
    assert not reopened.queued_turns()
    assert not reopened.projected_messages(include_parts=True)
    assert reopened.projection_version() > last_version

    cleared_version = reopened.projection_version()
    reopened.queue_turn("new", "新问题", {})
    assert reopened.projection_version() > cleared_version
    assert reopened.projected_messages()[0][1:4] == ("新问题", "new", 1)


def test_desktop_clear_fences_old_replies_and_context_without_a_model_route(tmp_path, monkeypatch):
    from deskpet.c_ui.desktop_system2 import DesktopSystem2
    from deskpet.mocks.clock import FakeClock
    from deskpet.settings import SettingsStore
    from deskpet.system2.host import System2Host

    path = tmp_path / "business.sqlite3"
    now = datetime.now(UTC)
    clock = FakeClock(now)
    settings = SettingsStore(path)
    store = ConversationStore(path)
    host = System2Host(tmp_path, scheduler=None, gateway=SimpleNamespace(definitions=()),
                       settings_store=settings, clock=clock)
    desktop = SimpleNamespace(chat=store, host=host)

    def artifact(directory, name):
        path = tmp_path / "system2-dsh" / directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic history", encoding="utf-8")
        return path

    historical_notes = [artifact(digit * 32, "recent-chat.json") for digit in ("1", "2")]
    legacy_chat = artifact("3" * 32, "sessions/work/mochi-chat/session.v4.jsonl")
    host.engine._mark_legacy_chat_profile(legacy_chat.parents[3])
    background = artifact("4" * 32, "sessions/work/mochi-background/session.v4.jsonl")
    another_scope = artifact("5" * 32, "recent-chat.json")
    owner = artifact("5" * 32, ".mochi-chat-owner")
    owner.write_text(sha256(b"another-scope").hexdigest()[:32], encoding="ascii")

    def finish_admission(admission, text):
        decision = {
            "as_of": now.isoformat(),
            "action_results": [{"action": "item.create", "subject_ref": "existing-item", "outcome": "committed"}],
        }
        host._store.finish(admission, decision, {"text": text}, None, "test", expected_state="running")

    try:
        host.submit_user("历史聊天文本", event_id="old")
        assert not DesktopSystem2.clear_chat_history(desktop)
        finish_admission(host._store.claim(now), "历史模型回复")
        old_reply, = host.ui_events()
        store.record_answer(old_reply.answer_id, "old", "历史模型回复", outbox_sequence=old_reply.sequence)
        host.resolve_proposal("proposal", "accept", version=1, event_id="proposal-click")
        proposal_admission = host._store.claim(now)

        assert DesktopSystem2.clear_chat_history(desktop)
        assert all(not path.exists() for path in (*historical_notes, legacy_chat))
        assert background.exists() and another_scope.exists()
        store.record_answer(old_reply.answer_id, "old", old_reply.payload["text"],
                            outbox_sequence=old_reply.sequence)
        assert store.load() == ([], "", None)
        assert host._store.decisions(current_chat_only=True) == ()
        assert len(host.decision_records()) == 1
        old_admission_id = host.admission_for_message("old")
        assert "utterance" not in host._store.state(old_admission_id).user_intent

        finish_admission(proposal_admission, "提议已生效")
        events = DesktopSystem2.pump(desktop)
        assert [(event.kind, event.reply_to_message_id, event.payload) for event in events] == [
            ("reply_cleared", "old", {}),
            ("reply_cleared", "proposal-click", {}),
        ]
        assert not host.ui_events()
        assert not store.projected_messages()

        host.resolve_proposal("another-proposal", "accept", version=1, event_id="fresh-proposal-click")
        finish_admission(host._store.claim(now), "新的提议已生效")
        event, = DesktopSystem2.pump(desktop)
        assert event.kind == "reply"
        assert store.projected_messages()[0][1] == "新的提议已生效"
        assert len(host._store.decisions(current_chat_only=True)) == 1

        def unavailable_cleanup():
            raise sqlite3.OperationalError("temporarily busy")

        monkeypatch.setattr(host.engine, "clear_chat_history", unavailable_cleanup)
        assert DesktopSystem2.clear_chat_history(desktop)
        assert store.load() == ([], "", None)
        assert host._store.decisions(current_chat_only=True) == ()
    finally:
        host.close()
