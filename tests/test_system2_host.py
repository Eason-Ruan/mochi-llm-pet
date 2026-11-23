"""Replay admissions through the production host and real business transactions."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
from threading import Event

import pytest

from deskpet.system2.host import System2Host
from deskpet.system2.protocol import EngineReply
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
        outcomes.append(call("phase_start", "bad", {"planned_duration_s": 1500}, step_id="dsh:2:1"))
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
    assert [outcome["error_code"] for outcome in outcomes] == ["NOT_AUTHORIZED", "NOT_AUTHORIZED"]
    assert business.phases.list_phases() == []
    event, = host.ui_events()
    assert event.reply_to_message_id == "query" and event.payload["text"] == "现在没有正在运行的计时。"
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
def test_start_supersession_keeps_independent_item_and_actual_results(runtime, commit_before_cancel):
    business, create = runtime
    entered, release = Event(), Event()

    def act(request, data, call):
        text = data["user_intent"]["utterance"]
        if text.startswith("能帮我"):
            assert "phase.start" in request.allowed_tools
            if commit_before_cancel:
                result = call("phase_start", "start", {"planned_duration_s": 100}, step_id="dsh:1:2")
                assert result["status"] == "applied"
            entered.set()
            assert release.wait(4)
            result = call("phase_start", "start", {"planned_duration_s": 100}, step_id="dsh:1:2")
            assert result["status"] == "applied" if commit_before_cancel else result["error_code"] == "SUPERSEDED"
            return "已经开了。"
        assert text == "记一下整理数据"
        first = call("item_create", "write", {"type": "issue", "title": "整理数据"}, step_id="dsh:2:1")
        duplicate = call("item_create", "unnecessary-new-id", {"type": "issue", "title": "整理数据"}, step_id="dsh:2:2")
        assert first["status"] == "applied" and first == duplicate
        return "已建两个事项。"

    host = create(act)
    start_id = host.submit_user("能帮我开个25分钟的番茄钟吗？", event_id="start")
    with ThreadPoolExecutor(max_workers=1) as worker:
        pending = worker.submit(host.pump)
        assert entered.wait(4)
        host.submit_user("先别开钟，记一下整理数据", event_id="compound", supersedes_admission_id=start_id)
        release.set()
        assert pending.result(timeout=4)
    assert host.pump()
    assert host.pump()
    assert not host.pump()
    assert [task.draft.content for task in business.tasks.list_tasks()] == ["整理数据"]
    phases = business.phases.list_phases()
    assert [phase.planned_duration_s for phase in phases] == ([1500] if commit_before_cancel else [])
    replies = host.ui_events()
    assert [reply.reply_to_message_id for reply in replies] == ["start", "compound", "compound"]
    assert replies[0].payload["status"] == ("applied" if commit_before_cancel else "superseded")
    assert replies[2].payload["text"] == "已创建事项。"
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
    assert restored.ui_events()[0].payload["text"] == "已开始 · 25 分钟"
    assert len(business.phases.list_phases()) == 1

    recovered = create(lambda *_: pytest.fail("Recovery called the model with an interrupted write"))
    assert not recovered.pump()
    assert not recovered.pump()
    replies = recovered.ui_events()
    assert [reply.reply_to_message_id for reply in replies] == ["lost", "interrupted"]
    assert replies[-1].payload["status"] == "interrupted"
    assert len(business.phases.list_phases()) == 1
    recovered.close()


def test_explicit_parameters_and_compound_commands_use_independent_admissions(runtime):
    business, create = runtime

    def act(_request, data, call):
        text = data["user_intent"]["utterance"]
        if text.startswith("开"):
            result = call("phase_start", "native", {"kind": "focus", "planned_duration_s": 99})
            assert result["status"] == "applied"
        elif text.startswith("记"):
            assert call("item_create", "native", {"type": "issue", "title": "整理数据"})["status"] == "applied"
        else:
            phase_id = data["context"]["current_phase"]["id"]
            result = call("phase_adjust", "native", {"id": phase_id, "add_s": 2000})
            assert result["status"] == "applied" and result["view"]["planned_duration_s"] == 2400
        return "随意的模型文案"

    host = create(act)
    host.submit_user("开25分钟＋记下整理数据", event_id="batch")
    assert host.pump() and host.pump()
    host.submit_user("改成 40 分钟", event_id="adjust")
    assert host.pump()
    assert [request.source for request in host.engine.turns] == ["user", "user", "user"]
    assert len({request.admission_id for request in host.engine.turns}) == 3
    replies = host.ui_events()
    assert [reply.reply_to_message_id for reply in replies] == ["batch", "batch", "adjust"]
    assert [reply.payload["text"] for reply in replies] == ["已开始 · 25 分钟", "已创建事项。", "已调整 · 40 分钟"]
    assert business.phases.current_phase().planned_duration_s == 2400
    host.close()


def test_failed_or_expired_model_request_cannot_execute_or_claim_success(runtime):
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
    assert [event.payload["status"] for event in host.ui_events()] == ["model_unavailable", "failed", "expired_request"]
    assert not business.phases.list_phases()
    host.close()



def test_completed_no_tool_model_uses_literal_timer_request_once(runtime):
    business, create = runtime
    host = create(lambda *_: "专注时长已调整为 40 分钟。")
    host.submit_user("能帮我开个25分钟的番茄钟吗？", event_id="start")
    assert host.pump()
    host.submit_user("改成 40 分钟", event_id="adjust")
    assert host.pump()
    assert business.phases.current_phase().planned_duration_s == 2400
    assert len(business.phases.list_phases()) == 1
    replies = host.ui_events()
    assert [event.payload["text"] for event in replies] == ["已开始 · 25 分钟", "已调整 · 40 分钟"]
    assert [event.payload["status"] for event in replies] == ["applied", "applied"]
    host.submit_user("改成 40 分钟", event_id="adjust")
    assert not host.pump()
    assert host.ui_events() == replies
    host.close()



def test_remaining_time_uses_authoritative_phase_seconds_without_model(runtime):
    business, create = runtime

    def act(_request, data, call):
        assert data["user_intent"]["message_id"] == "start"
        return call("phase_start", "start", {"planned_duration_s": 2400})["message"]

    host = create(act)
    host.submit_user("还剩多久？", event_id="empty")
    assert host.pump()
    host.submit_user("开始40分钟专注", event_id="start")
    assert host.pump()
    business.clock.advance(2.45)
    host.submit_user("还剩多久？", event_id="remaining")
    assert host.pump()
    assert [event.payload["text"] for event in host.ui_events()] == [
        "当前没有活动计时。", "已开始 · 40 分钟", "还剩 39 分 58 秒。",
    ]
    assert len(host.engine.turns) == 1
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
            assert data["grants"]["proposal.resolve"]["amendments"] == {"planned_duration_s": 2400}
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
            result = call("settings_update", "write", {
                "scope": "habit", "id": habit.id, "patch": {"behavior": {"focus_s": 2700}},
            })
        else:
            target = data["context"]["current_phase"]["id"]
            result = call("phase_adjust", "write", {"id": target, "total_s": 20})
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
