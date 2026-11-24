"""Exercise the production host, pinned DSH, and SQLite across a real model wire."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.util import find_spec
import json
import sqlite3
from threading import Thread

import pytest

from deskpet.m0.observation_input import DesktopObservationInput
from deskpet.mocks.clock import FakeClock
from deskpet.mocks.delivery import FakeDeliveryPort
from deskpet.observation_v2.context import ObservationContextReader
from deskpet.observation_v2.pipeline import ObservationStore
from deskpet.observation_v2.routes import SYSTEM2, CloudRoute
from deskpet.observation_v2.state import Observation
from deskpet.settings import SettingsStore
from deskpet.system2.host import System2Host
from deskpet.system2.items import ItemService
from deskpet.system2.scheduler import Scheduler
from deskpet.system2.tools import System2ToolExecutor
from deskpet.task_service.sqlite import SQLiteTaskService
from deskpet.work_sessions import PersistentWorkSessionService


class Desktop:
    def __init__(self, directory):
        self.directory = directory
        self.database = directory / "business.sqlite3"
        self.observation_db = directory / "observation.sqlite3"
        self.clock = FakeClock(datetime.now(UTC))
        self.settings = SettingsStore(self.database)
        self.tasks = SQLiteTaskService(
            self.clock, FakeDeliveryPort(self.clock), lambda *_: None,
            path=self.database, auto_opportunity=False,
        )
        self.phases = PersistentWorkSessionService(
            path=self.database, clock=self.clock, settings_store=self.settings,
        )
        self.items = ItemService(self.tasks)
        self.observation = ObservationStore(self.observation_db, live=True, boot_id="runtime-audit")
        self.observation.begin_live_run(
            self.clock.wall_now(), monotonic_s=0, poll_interval_s=30, policy_generation=0,
        )

        class PairedClockReader(DesktopObservationInput):
            def read_observation_context(reader, as_of):
                return reader.reader.read_realtime_context(
                    as_of, monotonic_s=self.clock.monotonic_now(), policy_generation=0,
                )

        self.reader = PairedClockReader(self.observation_db, settings_path=self.database)
        self.reader.reader = ObservationContextReader(self.observation_db, boot_id="runtime-audit")
        self.scheduler = Scheduler(self.tasks, self.items, clock=self.clock, observation=self.reader)
        self.gateway = System2ToolExecutor(
            self.database, items=self.items, phases=self.phases, settings=self.settings, scheduler=self.scheduler,
        )

    def publish(self, revision, summary, *, state="working"):
        now = self.clock.wall_now()
        with self.observation.connection:
            self.observation._record_live_sample(now, self.clock.monotonic_now())
        self.observation.save_observation(
            Observation("work", state, summary, (), now, now + timedelta(seconds=15), revision),
            published_at=now, monotonic_s=self.clock.monotonic_now(),
        )

    def host(self, engine=None):
        return System2Host(
            self.directory, scheduler=self.scheduler, gateway=self.gateway, settings_store=self.settings,
            clock=self.clock, context_reader=self.reader, manage_scheduler=False, engine=engine,
        )

    def route(self, origin, *, share=False):
        route = CloudRoute("openai_compatible", origin + "/v1", "audit-model", "Runtime audit", timeout_s=5)
        current = self.settings.routes.configure(SYSTEM2, route)
        if share:
            current = self.settings.routes.set_permission(SYSTEM2, True, expected=current)
        return self.settings.routes.record_result(current, probe=True)

    def close(self):
        self.observation.close()
        self.phases.close()
        self.tasks.close()


@pytest.fixture
def desktop(tmp_path):
    app = Desktop(tmp_path)
    try:
        yield app
    finally:
        app.close()


@contextmanager
def model_wire(responses):
    """Provide scripted model replies at the endpoint, after DSH and the HTTP gateway."""
    if find_spec("deepseek_harness") is None:
        pytest.skip("The pinned DSH runtime is unavailable")
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            ordinal = len(requests)
            requests.append(body)
            response = responses[ordinal] if ordinal < len(responses) else "Unexpected model request."
            if callable(response):
                response = response(body)
            if isinstance(response, tuple):
                name, arguments = response
                delta = {"tool_calls": [{
                    "index": 0, "id": f"native-call-{ordinal}", "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }]}
                reason = "tool_calls"
            else:
                delta = {"content": response}
                reason = "stop"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for fields, finish in [({"role": "assistant"}, None), (delta, None), ({}, reason)]:
                frame = {
                    "id": f"completion-{ordinal}", "object": "chat.completion.chunk", "created": 1,
                    "model": "audit-model", "choices": [{"index": 0, "delta": fields, "finish_reason": finish}],
                }
                self.wfile.write(("data: " + json.dumps(frame) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")

    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    except PermissionError:
        pytest.skip("Loopback bind is unavailable")
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def last_tool(body):
    return json.loads(next(message["content"] for message in reversed(body["messages"])
                           if message["role"] == "tool"))


def test_native_read_scope_reply_and_budget_survive_a_host_restart(desktop):
    timer = desktop.gateway.execute_local(
        "phase.start", {"planned_duration_s": 1500, "description": "LOCAL-TIMER-NOTES"},
        event_id="existing-timer",
    )
    visible = desktop.gateway.execute_local(
        "item.create", {"type": "issue", "title": "Report", "description": "LOCAL-ITEM-NOTES"},
        event_id="visible-item",
    ).view
    hidden = desktop.gateway.execute_local(
        "item.create", {"type": "issue", "title": "UNRELATED-ITEM-TITLE"}, event_id="hidden-item",
    ).view
    assert timer.status == "applied"
    desktop.publish(71, "OLD-ACTIVITY-NOTES")
    desktop.clock.advance(5)
    desktop.publish(72, "CURRENT-ACTIVITY-NOTES")
    script = [
        ("phase_get", {}),
        ("item_get", {"id": hidden["id"]}),
        ("item_get", {"id": visible["id"]}),
        ("item_list", {}),
        ("context_read", {}),
        "好，刚刚帮你创建了新事项。要查看详情吗？",
        "当前是 25 分钟专注。",
    ]
    with model_wire(script) as (origin, wire):
        desktop.route(origin)
        host = desktop.host()
        try:
            admission = host.submit_user("查看这条事项的概要。", event_id="timer-query", selected_ref=f"item:{visible['id']}")
            assert host.pump()
            reply, = host.ui_events()
            assert reply.reply_to_message_id == "timer-query"
            assert reply.payload["status"] == "failed"
            assert len(wire) == 6
            assert last_tool(wire[1])["view"]["planned_duration_s"] == 1500
            assert last_tool(wire[2])["error_code"] == "NOT_AUTHORIZED"
            assert last_tool(wire[3])["view"]["id"] == visible["id"]
            assert [item["id"] for item in last_tool(wire[4])["view"]] == [visible["id"]]
            assert last_tool(wire[5])["error_code"] == "TOOL_BUDGET_EXHAUSTED"
            with sqlite3.connect(desktop.database) as db:
                steps = db.execute("SELECT step_id FROM s2_calls WHERE admission_id=? ORDER BY rowid", (admission,)).fetchall()
            assert [step.rsplit(":", 2)[1:] for step, in steps] == [["1", str(index)] for index in range(1, 5)]
        finally:
            host.close()
        restored = desktop.host()
        try:
            assert restored.submit_user("查看这条事项的概要。", event_id="timer-query",
                                        selected_ref=f"item:{visible['id']}") == admission
            assert not restored.pump()
            restored.submit_user("我目前有什么需要注意？", event_id="later-query")
            assert restored.pump()
            assert len(restored.ui_events()) == 2
            assert len(wire) == 7
        finally:
            restored.close()
    for body in wire:
        packet = json.dumps(body)
        assert all(marker not in packet for marker in (
            "LOCAL-TIMER-NOTES", "LOCAL-ITEM-NOTES", "UNRELATED-ITEM-TITLE",
            "OLD-ACTIVITY-NOTES", "CURRENT-ACTIVITY-NOTES",
        ))
        assert "phase_start" not in {tool["function"]["name"] for tool in body["tools"]}
    assert len(desktop.phases.list_phases()) == 1
    assert len(desktop.tasks.list_tasks()) == 2


def test_context_read_crosses_ttl_without_authorizing_historical_head(desktop, monkeypatch):
    monkeypatch.setenv("MOCHI_SYSTEM2_SEMANTIC_BACKGROUND", "0")
    desktop.publish(70, "earlier observation")
    desktop.clock.advance(5)
    desktop.publish(71, "current observation", state="available")
    as_of = desktop.clock.wall_now()
    reminder = desktop.gateway.execute_local(
        "item.create", {
            "type": "reminder", "title": "Take a break",
            "reminder_rule": {
                "target_at": (as_of + timedelta(minutes=1)).isoformat(),
                "window_open_at": as_of.isoformat(),
                "window_close_at": (as_of + timedelta(minutes=2)).isoformat(),
            },
        }, event_id="bound-reminder",
    )
    assert reminder.status == "applied"
    occurrence = reminder.view["occurrence"]["id"]
    desktop.scheduler.resume()

    views = {}

    class Boundary:
        def run(self, request, call):
            views["before"] = call("context_read", "before", {})["view"]
            desktop.clock.advance(15)
            views["at_ttl"] = call("context_read", "at-ttl", {})["view"]
            views["historical"] = call("context_read", "historical", {"as_of": as_of.isoformat()})["view"]
            views["present"] = call("reminder_present", "present", {
                "occurrence_ids": [occurrence], "presentation": {"sound": False}, "reason_code": "available",
            })
            return "quiet"

        def close(self):
            pass

    host = desktop.host(Boundary())
    try:
        assert host.pump()
        before, at_ttl, historical = (views[key] for key in ("before", "at_ttl", "historical"))
        assert before["current_status"] == "fresh"
        assert before["current"]["context_revision"] == 71
        assert at_ttl["current"] is None
        assert at_ttl["current_status"] == "expired"
        assert at_ttl["history"]["stats"]["total_samples"] == 2
        assert historical["current"]["context_revision"] == 71
        assert historical["current_status"] == "fresh"
        assert views["present"]["error_code"] == "STALE_CONTEXT"
        assert not host.ui_events()
    finally:
        host.close()

def test_native_chat_amendment_and_restarted_old_button_share_one_phase(desktop):
    desktop.settings.update(expected_version=0, auto_adjust=True)
    desktop.publish(83, "AVAILABLE-FOR-A-FOCUS-SUGGESTION", state="available")
    desktop.scheduler.resume()
    script = [
        ("proposal_offer", {
            "function": "phase.start", "args": {"kind": "focus", "planned_duration_s": 1500},
            "display_text": "现在开始 25 分钟专注吗？",
        }),
        "quiet",
    ]
    with model_wire(script) as (origin, wire):
        desktop.route(origin)
        host = desktop.host()
        try:
            assert host.pump()
            card, = host.pending_proposals()
            assert desktop.phases.current_phase() is None
            assert not host.ui_events()
            script.extend([
                ("proposal_resolve", {
                    "id": card["id"], "answer": "accept", "amendments": {"planned_duration_s": 2400},
                }),
                "已开始 · 40 分钟",
            ])
            host.submit_user("改成 40 分钟开始", event_id="amend",
                             reply_to_proposal_id=card["id"], reply_to_proposal_version=card["version"])
            assert host.pump()
            current = desktop.phases.current_phase()
            assert current.planned_duration_s == 2400
            answer, = host.ui_events()
            assert answer.payload["text"] == "已开始 · 40 分钟"
            assert len(wire) == 4
            assert last_tool(wire[3])["view"]["planned_duration_s"] == 2400
            assert "phase_start" not in {tool["function"]["name"] for tool in wire[0]["tools"]}
            assert "proposal_resolve" not in {tool["function"]["name"] for tool in wire[0]["tools"]}
        finally:
            host.close()
        restarted = desktop.host()
        try:
            old_button = restarted.resolve_proposal(card["id"], "accept", version=card["version"], event_id="old")
            assert restarted.pump()
            assert restarted.resolve_proposal(card["id"], "accept", version=card["version"], event_id="old") == old_button
            assert not restarted.pump()
            assert restarted.ui_events()[-1].payload["text"] == "已开始 · 40 分钟"
            assert len(wire) == 4
            assert [phase.id for phase in desktop.phases.list_phases()] == [current.id]
        finally:
            restarted.close()


@pytest.mark.parametrize("change", ["route_consent", "observation_policy"])
def test_policy_change_after_model_callback_check_blocks_the_business_write(desktop, monkeypatch, change):
    monkeypatch.setenv("MOCHI_SYSTEM2_SEMANTIC_BACKGROUND", "1")
    desktop.settings.update(expected_version=0, auto_adjust=True)
    desktop.scheduler.resume()
    recommendation = {
        "function": "phase.start", "args": {"planned_duration_s": 900},
        "display_text": "开始 15 分钟专注吗？",
    }
    with model_wire([("proposal_offer", recommendation)]) as (origin, requests):
        route = desktop.route(origin, share=True)
        execute = desktop.gateway.execute
        outcomes = []

        def revoke_then_execute(function, args, authority):
            assert function == "proposal.offer"
            if change == "route_consent":
                desktop.settings.routes.set_permission(SYSTEM2, False, expected=route)
            else:
                desktop.settings.update(expected_version=desktop.settings.read().version, observation_paused=True)
            result = execute(function, args, authority)
            outcomes.append(result)
            return result

        monkeypatch.setattr(desktop.gateway, "execute", revoke_then_execute)
        host = desktop.host()
        try:
            assert host.pump()
            result, = outcomes
            assert result.error_code == "MODEL_AUTHORIZATION_CHANGED"
            assert not host.pending_proposals()
            assert not desktop.gateway.pending_presentations()
            assert len(requests) == 1
        finally:
            host.close()


def test_question_shaped_write_claim_is_replaced_with_host_owned_text(desktop):
    class FakeModel:
        def run(self, *_):
            return "给你整理好了。可以看一下这个结果吗？"

        def close(self):
            pass

    host = desktop.host(FakeModel())
    try:
        host.submit_user("记下整理实验记录", event_id="unacknowledged-write")
        assert host.pump()
        event, = host.ui_events()
        assert event.payload == {"status": "failed", "text": "这次没有执行成功。"}
        assert not desktop.tasks.list_tasks()
    finally:
        host.close()
