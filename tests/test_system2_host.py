"""Replay admissions through the production host and real business transactions."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from math import ceil
from zoneinfo import ZoneInfo
import json
from threading import Event

import pytest

from deskpet.system2.host import System2Host
from deskpet.system2.protocol import EngineReply
from deskpet.system2.tool_schema import validate_tool_args
from deskpet.system2.tools import System2ToolExecutor
from tests.test_system2_tools import LocalBusiness


class FixtureEngine:
    def __init__(self, turn):
        self.callback = turn
        self.turns = []

    def run(self, request, invoke):
        self.turns.append(request)
        return self.callback(request, json.loads(request.input_json), invoke)

    def close(self):
        pass


class ProcessLost(BaseException):
    pass


@pytest.fixture
def runtime(tmp_path):
    business = LocalBusiness(tmp_path)

    def host(callback):
        gateway = System2ToolExecutor(
            business.path, items=business.items, phases=business.phases, settings=business.settings,
            scheduler=business.scheduler,
        )
        return System2Host(
            tmp_path, scheduler=business.scheduler, gateway=gateway, engine=FixtureEngine(callback),
            settings_store=business.settings, clock=business.clock, manage_scheduler=False,
            context_reader=business.scheduler.observation, timezone="Asia/Shanghai",
        )

    yield business, host
    business.close()


def test_proposal_button_queued_during_pump_uses_time_of_execution(runtime, monkeypatch):
    business, create = runtime
    business.auto_adjust()
    snapshot = business.publish(71)
    proposal = business.command("proposal.offer", {
        "function": "phase.start", "args": {"planned_duration_s": 1500},
        "display_text": "现在开始专注？",
    }, "offer", origin="agent", snapshot=snapshot).view
    host = create(lambda *_: pytest.fail("A proposal button does not use the model"))

    def queue_rejection_before_claim(at):
        assert at == business.clock.wall_now()
        business.clock.advance(0.01)
        host.resolve_proposal(proposal["id"], "reject", version=proposal["version"], event_id="reject-new")
        return None

    try:
        monkeypatch.setattr(host, "_background", queue_rejection_before_claim)
        assert host.pump()
        assert not host.pending_proposals()
        rejection, = host.ui_events()
        assert rejection.reply_to_message_id == "reject-new"
        assert rejection.payload["status"] == "applied"
        assert rejection.payload["text"] == "已拒绝建议。"
        assert rejection.payload["parts"] == [{"source": "program", "text": "已拒绝建议。"}]
        assert not business.phases.list_phases()
    finally:
        host.close()


def test_background_and_overlapping_query_have_separate_authority_and_reply(runtime):
    business, create = runtime
    business.clock.advance(120)
    business.scheduler.poll()
    entered, release = Event(), Event()
    outcomes = []

    def act(request, data, call):
        if request.source == "background":
            entered.set()
            assert release.wait(4)
            outcomes.append(call("phase_start", "bad", {"planned_duration_s": 1500}, step_id="dsh:1:1"))
            return EngineReply('{"action":"quiet","reason_code":"busy","explanation":"omit me"}')
        assert data["user_intent"]["utterance"] == "目前是什么状态，还剩多久？"
        assert data["context"]["recent_decisions"][0]["reason_code"] == "busy"
        result = call("phase_get", "phase", {}, step_id="dsh:2:1")
        assert result["status"] == "applied" and result.get("view") is None
        return EngineReply("现在没有正在运行的计时。", answer_id="native-2")

    host = create(act)
    with ThreadPoolExecutor(max_workers=1) as worker:
        background = worker.submit(host.pump)
        assert entered.wait(4)
        query_id = host.submit_user("目前是什么状态，还剩多久？", event_id="query")
        assert host.submit_user("目前是什么状态，还剩多久？", event_id="query") == query_id
        assert not host.pump()
        release.set()
        assert background.result(timeout=4)
    assert not host.ui_events()
    assert host.pump()
    denied, = outcomes
    assert denied["error_code"] == "NOT_AUTHORIZED"
    assert business.phases.list_phases() == []
    event, = host.ui_events()
    assert event.reply_to_message_id == "query" and event.payload["text"] == "现在没有正在运行的计时。"
    assert event.payload["parts"] == [{"source": "model", "text": "现在没有正在运行的计时。"}]
    assert "omit me" not in json.dumps(host.decision_records(), ensure_ascii=False)
    host.close()

    restored = create(lambda *_: pytest.fail("A duplicate user event must not reenter the model"))
    assert restored.admission_for_message("query") == query_id
    assert restored.admission_for_message("unknown") is None
    assert restored.turn_timeout == timedelta(seconds=45)
    assert restored.submit_user("目前是什么状态，还剩多久？", event_id="query") == query_id
    assert not restored.pump()
    assert restored.ui_events() == (event,)
    restored.ack_ui_event(event.sequence)
    assert not restored.ui_events()
    restored.close()


@pytest.mark.parametrize("commit_before_cancel", [False, True])
def test_ui_supersession_and_compound_replacement_keep_actual_results(runtime, commit_before_cancel):
    business, create = runtime
    entered, release = Event(), Event()

    def act(request, data, call):
        text = data["user_intent"]["utterance"]
        if text.startswith("能帮我"):
            assert "phase.start" in request.allowed_tools
            if commit_before_cancel:
                result = call("phase_start", "start", {"planned_duration_s": 1500}, step_id="dsh:1:2")
                assert result["status"] == "applied"
            entered.set()
            assert release.wait(4)
            result = call("phase_start", "start", {"planned_duration_s": 1500}, step_id="dsh:1:2")
            assert result["status"] == "applied" if commit_before_cancel else result["error_code"] == "SUPERSEDED"
            return "已经开了。"
        assert text == "先别开钟，记一下整理数据"
        first = call("item_create", "write", {"type": "issue", "title": "整理数据"}, step_id="dsh:2:1")
        duplicate = call("item_create", "unnecessary-new-id", {"type": "issue", "title": "整理数据"}, step_id="dsh:2:2")
        assert first["status"] == "applied" and first == duplicate
        return "我记下整理数据了。"

    host = create(act)
    start_id = host.submit_user("能帮我开个25分钟的番茄钟吗？", event_id="start")
    with ThreadPoolExecutor(max_workers=1) as worker:
        pending = worker.submit(host.pump)
        assert entered.wait(4)
        host.submit_user("先别开钟，记一下整理数据", event_id="compound", supersedes_admission_id=start_id)
        release.set()
        assert pending.result(timeout=4)
    assert host.pump() and not host.pump()
    assert [task.draft.content for task in business.tasks.list_tasks()] == ["整理数据"]
    phases = business.phases.list_phases()
    assert [phase.planned_duration_s for phase in phases] == ([1500] if commit_before_cancel else [])
    replies = host.ui_events()
    assert [reply.reply_to_message_id for reply in replies] == ["start", "compound"]
    assert replies[0].payload["status"] == "superseded"
    assert replies[0].payload["parts"] == (
        ([{"source": "program", "text": "已开始 · 25 分钟"}] if commit_before_cancel else [])
        + [{"source": "status", "text": "这条请求已经被后续操作替代。"}]
    )
    assert replies[1].payload["parts"] == [
        {"source": "program", "text": "已创建事项。"},
        {"source": "model", "text": "我记下整理数据了。"},
    ]
    host.close()


def test_committed_lost_reply_is_reconciled_and_timed_out_start_never_runs_again(runtime):
    business, create = runtime

    def kill(request, data, call):
        if data["user_intent"]["message_id"] == "lost":
            result = call("phase_start", "first", {"planned_duration_s": 1500}, step_id="dsh:1:1")
            assert result["status"] == "applied"
        raise ProcessLost

    first = create(kill)
    first.submit_user("开始25分钟专注", event_id="lost")
    with pytest.raises(ProcessLost):
        first.pump()
    assert not first.ui_events()

    restored = create(kill)
    restored.submit_user("开始35分钟专注", event_id="interrupted")
    with pytest.raises(ProcessLost):
        restored.pump()
    assert restored.ui_events()[0].payload["parts"][0] == {"source": "program", "text": "已开始 · 25 分钟"}
    assert len(business.phases.list_phases()) == 1

    recovered = create(lambda *_: pytest.fail("Recovery called the model with an interrupted write"))
    assert not recovered.pump()
    assert not recovered.pump()
    replies = recovered.ui_events()
    assert [reply.reply_to_message_id for reply in replies] == ["lost", "interrupted"]
    assert replies[-1].payload["status"] == "interrupted"
    interrupted = {"source": "status", "text": "上一轮未完成，已核对保存的执行结果。"}
    assert [reply.payload["parts"] for reply in replies] == [
        [{"source": "program", "text": "已开始 · 25 分钟"}, interrupted],
        [interrupted],
    ]
    assert len(business.phases.list_phases()) == 1
    recovered.close()


def test_compound_message_and_explicit_parameters_go_through_the_model(runtime, monkeypatch):
    monkeypatch.setenv("MOCHI_SYSTEM2_SEMANTIC_CHAT", "0")
    business, create = runtime
    message = "  开25分钟＋记下整理数据  "

    def act(_request, data, call):
        text = data["user_intent"]["utterance"]
        if text == message:
            assert call("phase_start", "start", {"kind": "focus", "planned_duration_s": 1500})["status"] == "applied"
            assert call("item_create", "note", {"type": "issue", "title": "整理数据"})["status"] == "applied"
        else:
            assert text == "改成 40 分钟"
            phase_id = data["context"]["current_phase"]["id"]
            result = call("phase_adjust", "adjust", {"id": phase_id, "total_s": 2400})
            assert result["status"] == "applied" and result["view"]["planned_duration_s"] == 2400
        return "好的。"

    host = create(act)
    host.submit_user(message, event_id="batch")
    assert host.pump() and not host.pump()
    host.submit_user("改成 40 分钟", event_id="adjust")
    assert host.pump()
    assert [request.source for request in host.engine.turns] == ["user", "user"]
    assert len({request.admission_id for request in host.engine.turns}) == 2
    replies = host.ui_events()
    assert [reply.reply_to_message_id for reply in replies] == ["batch", "adjust"]
    assert [reply.payload["text"] for reply in replies] == [
        "已开始 · 25 分钟\n已创建事项。\n好的。", "已调整 · 40 分钟\n好的。",
    ]
    assert business.phases.current_phase().planned_duration_s == 2400
    host.close()


def test_failed_or_expired_model_cannot_commit_and_unverified_text_stays_model_output(runtime):
    business, create = runtime

    def act(_request, data, call):
        if data["user_intent"]["message_id"] == "offline":
            raise TimeoutError
        if data["user_intent"]["message_id"] == "expired":
            business.clock.advance(45)
            result = call("phase_start", "late", {"planned_duration_s": 1500})
            assert result["error_code"] == "EXPIRED_REQUEST"
        return "已开始 25 分钟。"

    host = create(act)
    for event_id, utterance in (("offline", "开始 25 分钟专注"),
                                ("fake-success", "你现在感觉怎么样？"),
                                ("expired", "开始 25 分钟专注")):
        host.submit_user(utterance, event_id=event_id)
        assert host.pump()
    replies = [event.payload for event in host.ui_events()]
    assert [reply["status"] for reply in replies] == ["model_unavailable", "answered", "expired_request"]
    assert [reply["parts"] for reply in replies] == [
        [{"source": "status", "text": "这次对话模型没能完成回复。"}],
        [{"source": "model", "text": "已开始 25 分钟。"}],
        [{"source": "status", "text": "这条请求已超过可执行时限。"}],
    ]
    assert not business.phases.list_phases()
    host.close()


def test_model_that_calls_no_tool_never_creates_a_timer_or_reminder(runtime):
    business, create = runtime
    host = create(lambda *_: "可以帮你安排。")
    messages = ("能帮我开个25分钟的番茄钟吗？", "改成 40 分钟", "十分钟后提示我休息", "十分钟后提醒我喝水")
    for index, message in enumerate(messages):
        host.submit_user(message, event_id=f"message-{index}")
        assert host.pump()
    assert not business.phases.list_phases()
    assert not business.tasks.list_tasks()
    assert len(host.engine.turns) == len(messages)
    replies = host.ui_events()
    for reply in replies:
        assert reply.payload == {
            "text": "可以帮你安排。", "status": "answered",
            "parts": [{"source": "model", "text": "可以帮你安排。"}],
        }
    host.submit_user(messages[-1], event_id=f"message-{len(messages) - 1}")
    assert not host.pump()
    assert host.ui_events() == replies
    host.close()


def test_model_reads_authoritative_phase_seconds_and_replies_without_receipt(runtime):
    business, create = runtime

    def act(_request, data, call):
        if data["user_intent"]["message_id"] == "start":
            assert call("phase_start", "start", {"planned_duration_s": 2400})["status"] == "applied"
            return ""
        assert data["user_intent"]["utterance"] == "还剩多久？"
        result = call("phase_get", "phase", {})
        assert result["status"] == "applied"
        if view := result.get("view"):
            minutes, seconds = divmod(ceil(view["remaining_s"]), 60)
            return f"还剩 {minutes} 分 {seconds:02d} 秒。"
        return "当前没有活动计时。"

    host = create(act)
    host.submit_user("还剩多久？", event_id="empty")
    assert host.pump()
    host.submit_user("开始40分钟专注", event_id="start")
    assert host.pump()
    business.clock.advance(2.45)
    host.submit_user("还剩多久？", event_id="remaining")
    assert host.pump()
    assert [event.payload["parts"] for event in host.ui_events()] == [
        [{"source": "model", "text": "当前没有活动计时。"}],
        [{"source": "program", "text": "已开始 · 40 分钟"}],
        [{"source": "model", "text": "还剩 39 分 58 秒。"}],
    ]
    assert len(host.engine.turns) == 3
    host.close()


def test_offer_amended_via_chat_and_late_button_return_one_phase(runtime):
    business, create = runtime
    business.auto_adjust()
    business.publish(71)
    business.scheduler.resume()
    offered = []

    def act(request, data, call):
        if request.source == "background":
            result = call("proposal_offer", "suggest", {
                "function": "phase.start", "args": {"kind": "focus", "planned_duration_s": 1500},
                "subject_refs": [], "display_text": "开始专注 25 分钟？",
            }, step_id="dsh:1:1")
            assert result["status"] == "pending"
            offered.append(result)
        else:
            assert "proposal.resolve" in request.allowed_tools
            intent = data["user_intent"]
            result = call("proposal_resolve", "apply", {
                "id": intent["reply_to_proposal_id"], "expected_version": intent["reply_to_proposal_version"],
                "answer": "accept", "amendments": {"planned_duration_s": 2400},
            }, step_id="dsh:2:1")
            assert result["status"] == "applied"
            return ""
        return "quiet"

    host = create(act)
    assert host.pump()
    assert offered and not host.ui_events()
    proposal, = host.pending_proposals()
    host.submit_user("改成 40 分钟开始", event_id="accept", reply_to_proposal_id=proposal["id"],
                     reply_to_proposal_version=proposal["version"])
    assert host.pump()
    assert not host.pending_proposals()
    assert host.ui_events()[0].payload["text"] == "已开始 · 40 分钟"
    host.close()

    recovered = create(lambda *_: pytest.fail("A typed proposal click must bypass the model"))
    recovered.resolve_proposal(proposal["id"], "accept", version=proposal["version"], event_id="late")
    assert recovered.pump()
    assert len(business.phases.list_phases()) == 1
    assert recovered.ui_events()[-1].payload["text"] == "已开始 · 40 分钟"
    recovered.close()


def test_settings_scopes_are_bound_to_default_habit_and_current_phase(runtime):
    from deskpet.settings import HabitBehavior

    business, create = runtime
    habit = business.settings.save_habit(
        title="深度工作", behavior=HabitBehavior(focus_s=1200), expected_version=0,
    )

    def act(_request, data, call):
        utterance = data["user_intent"]["utterance"]
        if utterance.startswith("开始"):
            result = call("phase_start", "write", {"planned_duration_s": 1500})
        elif "默认" in utterance:
            result = call("settings_update", "write", {"scope": "default", "patch": {"default_focus_s": 2100}})
        elif "深度工作" in utterance:
            assert data["context"]["habits"] == []
            known, = call("habit_list", "catalog", {})["view"]
            assert known["id"] == habit.id and known["title"] == "深度工作"
            result = call("settings_update", "write", {
                "scope": "habit", "id": habit.id, "patch": {"behavior": {"focus_s": 2700}},
            })
        else:
            target = data["context"]["current_phase"]["id"]
            result = call("phase_adjust", "write", {"id": target, "total_s": 2400})
        assert result["status"] == "applied"
        return ""

    host = create(act)
    for message in (
        "开始25分钟专注", "把默认专注设为35分钟", "把名为深度工作的习惯专注改为45分钟", "本轮专注改成40分钟",
    ):
        host.submit_user(message)
        assert host.pump()
    assert business.settings.read().default_focus_s == 2100
    assert business.settings.get_habit(habit.id).behavior.focus_s == 2700
    assert business.phases.current_phase().planned_duration_s == 2400
    assert all(event.payload["status"] == "applied" for event in host.ui_events())
    host.close()


def test_summary_not_in_default_context_and_taskless_wait_remains_quiet(runtime):
    from deskpet.observation_v2.state import Observation

    business, create = runtime
    business.auto_adjust()
    seen = []

    def act(_request, data, call):
        snap = data["context"]
        seen.append(snap)
        assert "PRIVATE_OBSERVATION_MARKER" not in json.dumps(data)
        if snap["current_status"] != "fresh":
            result = call("observation_wait_fresh", "wait", {
                "purpose": "focus_suggestion", "after_cursor": snap["publication_cursor"] or 0,
                "valid_until": (business.clock.wall_now() + timedelta(minutes=5)).isoformat(),
            })
            assert result["status"] in {"applied", "pending"}
        return "quiet"

    host = create(act)
    business.scheduler.resume()
    assert host.pump()
    assert not host.ui_events()
    assert not host.pump()
    now = business.clock.wall_now()
    business.observations.save_observation(Observation(
        "work", "available", "PRIVATE_OBSERVATION_MARKER", (), now, now + timedelta(seconds=15), 71,
    ), published_at=now, monotonic_s=business.clock.monotonic_now())
    business.scheduler.poll()
    assert host.pump()
    assert not host.pump()
    assert not host.ui_events()
    assert [snap["current_status"] for snap in seen] == ["no_data", "fresh"]
    assert "focus_suggestion" in seen[1]["trigger_reasons"]
    host.close()


def test_conversational_reminder_uses_message_time_and_reconciles_the_actual_write(runtime):
    business, create = runtime
    timer = business.command("phase.start", {"planned_duration_s": 900}, "existing-focus").view
    requested_at = business.clock.wall_now()

    def model(request, data, call):
        assert request.source == data["origin"] == "user" and data["semantic_chat"]
        assert data["user_intent"]["timezone"] == "Asia/Shanghai"
        assert datetime.fromisoformat(data["user_intent"]["requested_at"]) == requested_at
        assert data["context"]["current_phase"]["id"] == timer["id"]
        assert "item.create" in request.allowed_tools
        request_args = {"type": "reminder", "title": "去趟洗手间",
                        "reminder_rule": {"remind_after_offset_s": 600, "remind_by_offset_s": 600}}
        invalid = call("item_create", "invalid", request_args | {"reminder_rule": {"remind_by_offset_s": "soon"}})
        assert invalid["error_code"] == "INVALID_ARGUMENT"
        assert invalid["invalid_fields"][0]["field"] == "reminder_rule.remind_by_offset_s"
        result = call("item_create", "create", request_args)
        assert result["outcome"] == "committed"
        assert call("item_create", "do-not-duplicate", request_args) == result
        return "已开启了一个新的专注。"

    host = create(model)
    try:
        host.submit_user("十分钟后叫我去趟洗手间", event_id="oral-request", created_at=requested_at)
        business.clock.advance(14)
        assert host.pump() and not host.pump()
        task, = business.tasks.list_tasks()
        due = requested_at + timedelta(minutes=10)
        assert task.draft.hard_deadline == task.draft.opens_at == due
        assert business.phases.current_phase().id == timer["id"]
        event, = host.ui_events()
        assert [action["outcome"] for action in event.payload["actions"]] == ["committed"]
        receipt = f"已设置提醒「去趟洗手间」，{due.astimezone(ZoneInfo('Asia/Shanghai')):%H:%M:%S}。"
        assert event.payload["parts"] == [
            {"source": "program", "text": receipt},
            {"source": "model", "text": "已开启了一个新的专注。"},
        ]
    finally:
        host.close()


def test_real_turn_binds_two_same_tool_actions_and_resolves_recent_object(runtime):
    business, create = runtime
    requested_at = business.clock.wall_now()

    def model(_request, data, call):
        if data["user_intent"]["message_id"] == "two":
            for title, kind in (("填表", "issue"), ("收衣服", "reminder")):
                args = {"type": kind, "title": title}
                if kind == "reminder":
                    args["reminder_rule"] = {"target_offset_s": 1200}
                assert call("item_create", title, args)["outcome"] == "committed"
            return "已完成。"
        recent = data["context"]["recent_user_actions"]
        item = next(obj for obj in data["context"]["items"] if obj["type"] == "reminder")
        assert "title" not in item
        assert any(action["subject_ref"] == f"item:{item['id']}" for action in recent)
        assert call("reminder_snooze", "unknown-target", {
            "occurrence_id": "forged:1", "delay_s": 600, "mode": "exact", "expected_version": 1,
        })["error_code"] == "NOT_AUTHORIZED"
        assert call("reminder_snooze", "snooze", {
            "occurrence_id": item["occurrence_id"], "delay_s": 600, "mode": "no_earlier",
        })["outcome"] == "committed"
        return ""

    host = create(model)
    try:
        host.submit_user("填表这事先列着，屋里的衣服过二十分钟左右叫我收进来", event_id="two")
        assert host.pump() and not host.pump()
        assert len(host.engine.turns) == 1
        payload = host.ui_events()[0].payload
        assert len(payload["actions"]) == 2
        assert payload["parts"] == [
            *({"source": "program", "text": action["text"]} for action in payload["actions"]),
            {"source": "model", "text": "已完成。"},
        ]
        original = next(task for task in business.tasks.list_tasks() if task.draft.content == "收衣服")
        assert original.draft.target_at == requested_at + timedelta(seconds=1200)
        assert original.draft.remind_after is None and original.draft.remind_by is None
        business.clock.advance(3)
        later_at = business.clock.wall_now()
        host.submit_user("刚才提到的那个提醒先延延，十分钟里都先不要响", event_id="later")
        business.clock.advance(7)
        assert host.pump()
        updated = business.tasks.get(original.task_id)
        assert updated.occurrence != original.occurrence
        assert updated.draft.remind_after == later_at + timedelta(seconds=600)
        assert updated.draft.remind_by is None
    finally:
        host.close()


def test_semantic_hypothetical_negative_quote_and_ambiguity_produce_no_write(runtime):
    business, create = runtime
    capability_query = "你能设提醒吗？"
    hypothetical = "如果我让你十分钟后提醒，你会怎么做？"
    negative = "今天别再提醒我喝水了，我只是在举个例子。"
    quoted = "「十分钟后提醒我去上厕所」"
    ambiguous = "把那个活动延一延吧"

    def model(request, data, call):
        assert data["origin"] == "user"
        text = data["user_intent"]["utterance"]
        if text == capability_query:
            return "可以设置提醒。"
        if text == quoted:
            assert "item.create" in request.allowed_tools
            return "这是一条用于举例的句子。"
        if text == ambiguous:
            return "是要延后提醒，还是延长当前计时？"
        if text == hypothetical:
            return "你明确要求后，我会按消息时间计算十分钟，并告诉你保存结果。"
        assert text == negative
        return "了解。"

    host = create(model)
    try:
        for text in (capability_query, hypothetical, negative, quoted, ambiguous):
            host.submit_user(text)
            assert host.pump()
        assert len(host.engine.turns) == 5
        assert not business.tasks.list_tasks()
        assert not business.phases.list_phases()
        assert host.ui_events()[0].payload["parts"] == [{"source": "model", "text": "可以设置提醒。"}]
        reply = host.ui_events()[-1].payload
        question = "是要延后提醒，还是延长当前计时？"
        assert reply == {"status": "answered", "text": question, "parts": [{"source": "model", "text": question}]}
    finally:
        host.close()


def test_superseded_conversational_turn_cannot_commit_after_explicit_cancel(runtime):
    business, create = runtime
    entered, release = Event(), Event()

    def model(_request, data, call):
        if data["user_intent"]["message_id"] == "first":
            entered.set()
            assert release.wait(4)
            assert call("phase_start", "late", {"planned_duration_s": 1800})["error_code"] == "SUPERSEDED"
        return "好的。"

    host = create(model)
    try:
        first = host.submit_user("给我半个小时安静地写报告", event_id="first")
        with ThreadPoolExecutor(max_workers=1) as workers:
            old = workers.submit(host.pump)
            assert entered.wait(4)
            host.submit_user("刚才那条算了", supersedes_admission_id=first)
            release.set()
            assert old.result(4)
        assert host.pump()
        assert not business.phases.list_phases()
        assert host.ui_events()[0].payload["status"] == "superseded"
    finally:
        host.close()


@pytest.mark.parametrize("function,args", [
    ("phase.start", {"planned_duration_s": "900"}),
    ("phase.pause", {"planned_duration_s": 900}),
    ("item.update", {"id": "item-1", "patch": {"invented": True}}),
])
def test_proposal_arguments_are_validated_for_the_selected_action(function, args):
    with pytest.raises(ValueError):
        validate_tool_args("proposal.offer", {"function": function, "args": args, "display_text": "确认？"})


@pytest.mark.parametrize("direct_start", [False, True])
def test_direct_action_fences_only_conflicting_tool_and_text_cancellation_reaches_model(runtime, direct_start):
    business, create = runtime
    entered, release = Event(), Event()

    def model(_request, data, call):
        if data["user_intent"]["message_id"] == "cancel-only":
            assert data["user_intent"]["utterance"] == "先别开钟"
            return "我看一下当前计时。"
        entered.set()
        assert release.wait(4)
        result = call("phase_start", "start", {"planned_duration_s": 900})
        if direct_start:
            assert result["error_code"] == "SUPERSEDED"
        else:
            assert result["outcome"] == "committed"
        assert call("item_create", "keep", {"type": "issue", "title": "整理数据"})["outcome"] == "committed"
        return ""

    host = create(model)
    host.gateway.after_local_commit = host.fence_local
    try:
        host.submit_user("给我半个小时安静地写报告，另外记下整理数据", event_id="compound")
        with ThreadPoolExecutor(max_workers=1) as workers:
            turn = workers.submit(host.pump)
            try:
                assert entered.wait(4)
                if direct_start:
                    local = host.gateway.execute_local("phase.start", {"planned_duration_s": 1800}, event_id="ui-start")
                    assert local.status == "applied"
                host.submit_user("先别开钟", event_id="cancel-only")
            finally:
                release.set()
            assert turn.result(4)
        assert host.pump() and not host.pump()
        assert [phase.planned_duration_s for phase in business.phases.list_phases()] == [1800 if direct_start else 900]
        assert [task.draft.content for task in business.tasks.list_tasks()] == ["整理数据"]
        assert host.ui_events()[-1].payload["parts"] == [{"source": "model", "text": "我看一下当前计时。"}]
    finally:
        host.close()


@pytest.mark.parametrize("text", [
    "十分钟后提醒我喝水是你刚才说的示例",
    "十分钟后提醒我喝水可以作为测试示例吗",
    "过十分钟提醒我喝水同时帮我记下买牛奶",
])
def test_relative_prefix_with_free_form_tail_reaches_the_model_intact(runtime, text):
    business, create = runtime

    def model(_request, data, call):
        assert data["user_intent"]["utterance"] == text
        if text.endswith("买牛奶"):
            assert call("item_create", "reminder", {
                "title": "喝水", "type": "reminder",
                "reminder_rule": {"remind_after_offset_s": 600, "remind_by_offset_s": 600},
            })["outcome"] == "committed"
            assert call("item_create", "issue", {"title": "买牛奶", "type": "issue"})["outcome"] == "committed"
        return ""

    host = create(model)
    try:
        host.submit_user(text)
        assert host.pump()
        assert len(host.engine.turns) == 1
        titles = {task.draft.content for task in business.tasks.list_tasks()}
        assert titles == ({"喝水", "买牛奶"} if text.endswith("买牛奶") else set())
    finally:
        host.close()


def test_committed_scope_allows_same_turn_update_without_unrelated_title(runtime):
    business, create = runtime
    other = business.command("item.create", {"type": "issue", "title": "别的事项"}, "other").view

    def model(_request, data, call):
        assert other["id"] not in {item["id"] for item in data["context"]["items"]}
        created = call("item_create", "one", {"type": "issue", "title": "买牛奶"})
        view = created["view"]
        candidates = call("context_read", "current", {})["view"]["items"]
        assert [item["id"] for item in candidates] == [view["id"]]
        completed = call("item_set_status", "two", {"id": view["id"], "status": "completed"})
        assert completed["outcome"] == "committed" and completed["actual_version"] > created["actual_version"]
        return ""

    host = create(model)
    try:
        host.submit_user("把买牛奶记下来，并立刻标成完成")
        assert host.pump()
        tasks = {task.draft.content: task for task in business.tasks.list_tasks()}
        assert tasks["买牛奶"].effective_issue_status == "completed"
        assert tasks["别的事项"].effective_issue_status == "created"
        assert len(host.ui_events()[0].payload["actions"]) == 2
    finally:
        host.close()


def test_chat_creates_issue_and_starts_linked_one_off_focus(runtime):
    business, create = runtime
    title = "研究汇丰银行 QDII"

    def model(request, data, call):
        assert data["semantic_chat"]
        assert {"item.create", "phase.start"} <= set(request.allowed_tools)
        assert data["grants"]["phase.start"]["issues"] == []
        item = call("item_create", "create-issue", {"type": "issue", "title": title})
        assert item["outcome"] == "committed"
        issue_id = item["view"]["id"]
        # Start directly from the creation receipt in the same turn.
        start = call("phase_start", "start-focus", {
            "kind": "focus", "title": title, "planned_duration_s": 600,
            "issue_links": [issue_id], "primary_issue_id": issue_id,
        })
        assert start["outcome"] == "committed"
        return ""

    host = create(model)
    try:
        host.submit_user("我准备研究一下汇丰银行qdii 创建一个10分钟工作段并设置对应的番茄钟")
        assert host.pump() and not host.pump()
        task, = business.tasks.list_tasks()
        assert task.draft.item_type == "issue" and task.draft.content == title
        assert task.effective_issue_status == "in_progress" and not task.draft.reminder_enabled
        active, = business.phases.list_phases()
        assert active.kind == "focus" and active.state == "running" and active.planned_duration_s == 600
        assert active.issue_links == (task.task_id,) and active.primary_issue_id == task.task_id
        assert active.habit_id is None and not business.settings.list_habits()
        reply, = host.ui_events()
        assert reply.payload["status"] == "applied"
        actions = reply.payload["actions"]
        assert [action["result_ref"] for action in actions] == [f"item:{task.task_id}", f"phase:{active.id}"]
        assert [action["outcome"] for action in actions] == ["committed", "committed"]
        assert actions[1]["text"] == "已开始 · 10 分钟"
    finally:
        host.close()


@pytest.mark.parametrize("old_state", ["elapsed", "running", "paused"])
def test_chat_creates_and_starts_named_habit_with_existing_focus(runtime, old_state):
    business, create = runtime
    old = business.tools.execute_local("phase.start", {"planned_duration_s": 30}, event_id="old-focus").view
    if old_state == "elapsed":
        business.clock.advance(30)
    elif old_state == "paused":
        business.tools.execute_local("phase.pause", {"id": old["id"], "expected_version": old["version"]},
                                     event_id="pause-old-focus")
    previous = business.phases.current_phase()
    assert previous.state == old_state

    def model(_request, data, call):
        assert data["context"]["current_phase"]["id"] == previous.id
        habit = call("habit_create", "create-habit", {
            "title": "晨间阅读", "behavior": {"focus_s": 600, "short_break_s": 300},
        })
        assert habit["outcome"] == "committed"
        start = call("phase_start", "start-habit", {
            "kind": "focus", "title": habit["view"]["title"], "habit_id": habit["view"]["id"],
            "expected_habit_version": habit["actual_version"],
        })
        assert start["outcome"] == ("committed" if old_state == "elapsed" else "rejected")
        if old_state != "elapsed":
            assert start["error_code"] == "INVALID_TRANSITION"
        return ""

    host = create(model)
    try:
        host.submit_user("保存一个叫晨间阅读的番茄钟习惯，每次专注十分钟、短休息五分钟，并现在启动")
        assert host.pump()
        habit, = business.settings.list_habits()
        assert habit.behavior.focus_s == 600
        assert business.phases.get_phase(previous.id) == previous
        active = business.phases.current_phase()
        reply, = host.ui_events()
        if old_state == "elapsed":
            assert active.id != previous.id and active.state == "running"
            assert active.habit_id == habit.id and active.planned_duration_s == 600
            assert reply.payload["status"] == "applied"
            assert reply.payload["actions"][1]["text"] == "已开始 · 10 分钟"
        else:
            assert active == previous
            assert reply.payload["status"] == "partial"
    finally:
        host.close()


def test_chat_receipts_report_habit_created_and_rejected_start_attempts(runtime):
    business, create = runtime
    previous = business.tools.execute_local("phase.start", {"planned_duration_s": 30}, event_id="old-focus").view

    def model(_request, _data, call):
        habit = call("habit_create", "create-habit", {"title": "研究 QDII", "behavior": {"focus_s": 600}})
        assert habit["outcome"] == "committed"
        start_args = {"habit_id": habit["view"]["id"], "expected_habit_version": habit["actual_version"]}
        start = call("phase_start", "start-habit", start_args)
        assert start["error_code"] == "INVALID_TRANSITION"
        repeated = call("phase_start", "repeat-start", start_args)
        assert repeated == start
        retry = call("phase_start", "protected-start", {
            **start_args, "previous_phase_id": previous["id"], "expected_previous_version": previous["version"],
        })
        assert retry["error_code"] == "NOT_AUTHORIZED"
        return "已开始新的计时。要等这轮结束再开始吗？"

    host = create(model)
    try:
        host.submit_user("创建一个十分钟的研究 QDII 习惯并启动番茄钟")
        assert host.pump()
        habit, = business.settings.list_habits()
        assert habit.title == "研究 QDII"
        assert business.phases.current_phase().id == previous["id"]
        reply, = host.ui_events()
        assert reply.payload["status"] == "partial"
        assert [action["error_code"] for action in reply.payload["actions"]] == [
            None, "INVALID_TRANSITION", "NOT_AUTHORIZED",
        ]
        assert reply.payload["parts"] == [
            {"source": "program", "text": "已创建习惯「研究 QDII」。"},
            {"source": "status", "text": "当前状态无法开始新的计时。"},
            {"source": "status", "text": "这次启动请求未获授权，未开始新的计时。"},
            {"source": "model", "text": "已开始新的计时。要等这轮结束再开始吗？"},
        ]
    finally:
        host.close()


def test_receipt_and_concrete_clarification_both_reach_chat(runtime):
    business, create = runtime

    def model(_request, _data, call):
        assert call("item_create", "create", {"type": "issue", "title": "买牛奶"})["outcome"] == "committed"
        return "已推迟两条提醒。你想推迟收衣服的提醒，还是交材料的提醒？"

    host = create(model)
    try:
        host.submit_user("记下买牛奶，再把那个提醒推迟十分钟")
        assert host.pump()
        payload = host.ui_events()[0].payload
        assert payload["status"] == "applied"
        exact_reply = "已推迟两条提醒。你想推迟收衣服的提醒，还是交材料的提醒？"
        assert payload["text"] == f"已创建事项。\n{exact_reply}"
        assert payload["parts"] == [
            {"source": "program", "text": "已创建事项。"},
            {"source": "model", "text": exact_reply},
        ]
        assert len(payload["actions"]) == 1 and payload["actions"][0]["outcome"] == "committed"
        assert len(business.tasks.list_tasks()) == 1
    finally:
        host.close()


def test_polite_focus_request_goes_to_semantic_engine(runtime):
    business, create = runtime

    def model(_request, data, call):
        assert data["user_intent"]["utterance"] == "能帮我开始专注吗？"
        assert call("phase_start", "start", {"planned_duration_s": 900})["outcome"] == "committed"
        return ""

    host = create(model)
    try:
        host.submit_user("能帮我开始专注吗？")
        assert host.pump()
        assert business.phases.current_phase().planned_duration_s == 900
    finally:
        host.close()


def test_receipt_version_advance_does_not_adopt_another_user_change(runtime, monkeypatch):
    business, create = runtime

    def model(_request, _data, call):
        created = call("item_create", "new", {"type": "issue", "title": "买牛奶"})
        result = call("item_set_status", "finish", {"id": created["view"]["id"], "status": "completed"})
        assert result["error_code"] == "STALE_TASK"
        return ""

    host = create(model)
    advance = host._chat_after_commit

    def with_manual_change(admission, intent, bound, subject_ref, version, catalog):
        item_id = subject_ref.removeprefix("item:")
        result = business.tools.execute_local("item.update", {
            "id": item_id, "patch": {"title": "周末买牛奶"}, "expected_version": version,
        }, event_id="manual-rename")
        assert result.status == "applied"
        return advance(admission, intent, bound, subject_ref, version, catalog)

    try:
        monkeypatch.setattr(host, "_chat_after_commit", with_manual_change)
        host.submit_user("把买牛奶记下来，并立刻标成完成")
        assert host.pump()
        task, = business.tasks.list_tasks()
        assert task.draft.content == "周末买牛奶" and task.effective_issue_status == "created"
        payload = host.ui_events()[0].payload
        assert payload["status"] == "partial"
        assert payload["parts"] == [
            {"source": "program", "text": "已创建事项。"},
            {"source": "status", "text": "相关事项或设置已经更新。请刷新后再确认。"},
        ]
    finally:
        host.close()


def test_chat_snapshot_uses_one_reader_while_a_writer_is_waiting(runtime, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    business, create = runtime
    host = create(lambda *_: EngineReply(""))
    connect = host._store.connect
    reading = False

    @contextmanager
    def single_reader():
        nonlocal reading
        # With SQLite's rollback journal a pending writer blocks a second
        # connection, even when both reads belong to the same thread.
        assert not reading, "nested snapshot connection"
        with connect() as db:
            reading = True
            try:
                yield db
            finally:
                reading = False

    monkeypatch.setattr(host._store, "connect", single_reader)
    try:
        admission = SimpleNamespace(source="user", user_intent={"utterance": "刚才那个提醒"},
                                    admission_id="snapshot")
        assert host._business_context(admission, business.clock.wall_now())["recent_user_actions"] == []
    finally:
        host.close()


def test_model_reads_a_catalog_then_can_write_the_observed_occurrence(runtime):
    business, create = runtime
    at = business.clock.wall_now() + timedelta(minutes=20)
    reminder = business.tools.execute_local("item.create", {
        "type": "reminder", "title": "关窗", "description": "private local notes",
        "reminder_rule": {"remind_by": at.isoformat()},
    }, event_id="reminder").view
    business.tools.execute_local("item.create", {"type": "issue", "title": "查看备忘录"}, event_id="issue")
    occurrence_id = reminder["occurrence"]["id"]

    def model(_request, data, call):
        assert data["context"]["items"] == []
        assert call("item_get", "guess", {"id": reminder["id"]})["error_code"] == "NOT_AUTHORIZED"
        candidate, = call("item_list", "catalog", {"filter": {"type": "reminder"}})["view"]
        assert candidate["id"] == reminder["id"] and candidate["title"] == "关窗"
        assert "description" not in candidate
        assert call("reminder_snooze", "snooze", {
            "occurrence_id": occurrence_id, "delay_s": 600, "mode": "exact",
        })["outcome"] == "committed"
        return ""

    host = create(model)
    try:
        host.submit_user("今天穿堂风小，让夜一点的时候再提醒关窗。")
        assert host.pump()
        task = business.tasks.get(reminder["id"])
        assert task.occurrence_id != occurrence_id
        assert host.ui_events()[0].payload["parts"] == [{"source": "program", "text": "已推迟提醒。"}]
    finally:
        host.close()


def test_observation_document_query_is_selected_only_by_the_typed_tool(runtime, monkeypatch):
    _business, create = runtime
    queried = []

    def model(request, _data, call):
        assert request.observation_document_query is False
        assert call("context_read", "full", {"include_document_source": True})["status"] == "applied"
        assert call("context_read", "normal", {})["status"] == "applied"
        return "我看了一下。"

    host = create(model)
    observation = host._observation

    def read(*args, **options):
        queried.append(options.get("document_query", False))
        return observation(*args, **options)

    try:
        monkeypatch.setattr(host, "_observation", read)
        host.submit_user("十分钟前看的是哪份文件？")
        assert host.pump()
        assert queried == [False, True, False]
        assert host.ui_events()[0].payload["parts"] == [{"source": "model", "text": "我看了一下。"}]
    finally:
        host.close()
