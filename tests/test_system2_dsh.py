"""Full-wire regressions for the pinned native System 2 runtime and its host gateway."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
from threading import Event, Thread
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from deskpet.observation_v2.openai_cloud import store_route_credential
from deskpet.observation_v2.routes import (
    BAILIAN, BAILIAN_BASE_URL, BAILIAN_MODEL, STATE, SYSTEM2, VISION, CloudRoute, CloudRouteError,
)
from deskpet.settings import SettingsStore
from deskpet.system2.dsh import DSHEngine, verify_system2_route
from deskpet.system2.dsh_projection import project_input, project_tool_result
from deskpet.system2.prompt import PROMPT_CORE, PROMPT_SECTIONS, PROMPT_VERSION, SPLIT_PROMPT_VERSION, SYSTEM_PROMPT
from deskpet.system2.tool_schema import tool_definitions

SUMMARY = "SYNTHETIC-PRIVATE-ACTIVITY"
RECENT = "SYNTHETIC-PRIVATE-RECENT"
SEGMENT = "SYNTHETIC-PRIVATE-SEGMENT"
SAMPLE = "SYNTHETIC-PRIVATE-SAMPLE"
FRAME = "SYNTHETIC-RAW-FRAME"
RECEIPT = "SYNTHETIC-TOOL-RECEIPT"
TITLE = "SYNTHETIC-USER-REMINDER-TITLE"


def definitions():
    return [
        SimpleNamespace(name=name, description="Read a structured test datum.", input_schema={
            "type": "object", "properties": {"item_id": {"type": "string"}},
            "required": ["item_id"], "additionalProperties": False,
        })
        for name in ("context_read", "item_get")
    ]


def snapshot():
    at = "2026-11-23T11:14:15+00:00"
    return {
        "as_of": at, "current_status": "fresh",
        "current": {
            "observed_at": at, "valid_until": at, "state": "working", "context_revision": 3,
            "summary": SUMMARY, "screen_summary": FRAME, "window_title": FRAME,
        },
        "history": {
            "recent": [{"observed_at": at, "summary": RECENT, "visible_text_redacted": FRAME}],
            "segments": [{"summary": SEGMENT, "summary_samples": [{"observed_at": at, "summary": SAMPLE}]}],
            "other": {"screen_text": FRAME},
        },
        "eligible_items": [{"id": "test-item", "title": TITLE}],
        "screen_text": FRAME,
    }


def turn(text="Read item-7, then repeat the receipt", *, tools=("context.read",)):
    return SimpleNamespace(
        admission_id=uuid4().hex, system_prompt="Test setup. Use the tool for requested current data.",
        user_scope="default", prompt_version="test.zh.v1", source="user", allowed_tools=tools,
        input_json=json.dumps({"context": snapshot(), "user_intent": {"utterance": text}}),
    )


def reply(name, native_call_id, arguments, *, step_id):
    assert name == "context_read"
    assert native_call_id == "call-fixture"
    assert arguments == {"item_id": "item-7"}
    assert step_id.endswith(":1:1")
    return {"status": "applied", "message": RECEIPT, "view": snapshot()}


def install_route(settings, fixture, *, permission=False, credential_ref=None):
    route = CloudRoute(
        provider_kind="openai_compatible", base_url=fixture.origin + "/v1", model="test-model",
        display_name="Synthetic fixture", credential_ref=credential_ref, timeout_s=5,
    )
    snapshot = settings.routes.configure(SYSTEM2, route)
    if permission:
        snapshot = settings.routes.set_permission(SYSTEM2, True, expected=snapshot)
    return settings.routes.record_result(snapshot, probe=True)


@pytest.fixture
def endpoints():
    if importlib.util.find_spec("deepseek_harness") is None:
        pytest.skip("Pinned DSH runtime is unavailable")
    models = []

    class Model:
        def __init__(self):
            self.requests = []
            self.authorizations = []
            self.hosts = []
            self.path = "/v1/chat/completions"
            self.reasoning_parts = ()
            self.prompt_tokens = 10
            self.failure = None
            self.failure_body = None
            self.redirect_to = None
            self.verify = False
            self.respond = None
            self.stall_started = None
            self.release_stall = None
            model = self

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *_):
                    pass

                def write_event(self, payload):
                    if not model.reasoning_parts:
                        self.wfile.write(payload)
                        return
                    # Force HTTPX to decode across a UTF-8 character split between HTTP chunks.
                    split = next((index + 1 for index, byte in enumerate(payload) if byte >= 128), len(payload))
                    for chunk in (payload[:split], payload[split:]):
                        if chunk:
                            self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))

                def do_POST(self):
                    assert self.path == model.path
                    body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    model.requests.append(body)
                    model.authorizations.append(self.headers.get("Authorization"))
                    model.hosts.append(self.headers.get("Host"))
                    if model.stall_started is not None:
                        model.stall_started.set()
                        if not model.release_stall.wait(6):
                            return
                    if model.failure is not None:
                        self.send_response(model.failure)
                        if model.redirect_to:
                            self.send_header("Location", model.redirect_to)
                        self.end_headers()
                        if model.failure_body is not None:
                            self.wfile.write(json.dumps(model.failure_body).encode())
                        return
                    result = next((message["content"] for message in reversed(body["messages"])
                                   if message["role"] == "tool"), None)
                    if model.respond is not None:
                        delta, finish = model.respond(body)
                    elif result is not None:
                        text = json.loads(result)["code"] if model.verify else RECEIPT
                        delta, finish = {"content": text}, "stop"
                    elif not body.get("tools"):
                        delta, finish = {"content": RECEIPT}, "stop"
                    else:
                        prompt = body["messages"][-1]["content"]
                        item_id = prompt.split("查询 ", 1)[-1].split("，", 1)[0] if model.verify else "item-7"
                        name = "mochi_verify" if model.verify else "context_read"
                        delta, finish = {"tool_calls": [{
                            "index": 0, "id": "call-fixture", "type": "function",
                            "function": {"name": name, "arguments": json.dumps({"item_id": item_id})},
                        }]}, "tool_calls"
                    deltas = [{"role": "assistant"}, *({"reasoning_content": part} for part in model.reasoning_parts), delta]
                    if model.reasoning_parts and delta.get("tool_calls"):
                        function = delta["tool_calls"][0]["function"]
                        arguments = function["arguments"]
                        function["arguments"] = arguments[:5]
                        deltas.append({"tool_calls": [{"index": 0, "function": {"arguments": arguments[5:]}}]})
                    pieces = [
                        *({"choices": [{"index": 0, "delta": delta, "finish_reason": None}]} for delta in deltas),
                        {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
                        {"choices": [], "usage": {"prompt_tokens": model.prompt_tokens, "completion_tokens": 5,
                                                  "total_tokens": model.prompt_tokens + 5}},
                    ]
                    if model.reasoning_parts:
                        self.protocol_version = "HTTP/1.1"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    if model.reasoning_parts:
                        self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    for piece in pieces:
                        frame = {"id": "fixture", "object": "chat.completion.chunk", "created": 1,
                                 "model": body["model"], **piece}
                        self.write_event(("data: " + json.dumps(frame, ensure_ascii=False) + "\n\n").encode())
                    self.write_event(b"data: [DONE]\n\n")
                    if model.reasoning_parts:
                        self.wfile.write(b"0\r\n\r\n")

            try:
                self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            except PermissionError:
                pytest.skip("Loopback bind is unavailable")
            self.origin = f"http://127.0.0.1:{self.server.server_port}"
            self.thread = Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()

        def close(self):
            if self.release_stall is not None:
                self.release_stall.set()
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=1)

    def create():
        model = Model()
        models.append(model)
        return model

    yield create
    for model in models:
        model.close()


def assert_full_wire(requests, *, share=False):
    assert requests
    for request in requests:
        assert request["stream"]
        text = json.dumps(request)
        assert FRAME not in text
        assert TITLE in text
        assert all((marker in text) == share for marker in (SUMMARY, RECENT, SEGMENT, SAMPLE))
        assert [tool["function"]["name"] for tool in request["tools"]] == ["context_read"]


def test_semantic_projection_preserves_decision_evidence_and_separates_user_intent():
    at = "2026-11-23T19:14:15+08:00"
    transition = {"phase_id": "focus-1", "phase_version": 3, "elapsed_at": at,
                  "successor": {"id": "break-1", "kind": "short_break", "version": 1}}
    contact = {"kind": "proposal", "delivery_id": "proposal-ui-1", "at": at,
               "channel": "pet_card", "response": "refused", "response_at": at, "subject_ref": "proposal:1",
               "primary_channel": "pet_card", "exposure": "painted", "passive_entry": "proposal_panel",
               "sound_requested": True, "sound_heard": "unknown",
               "surfaces": [{"channel": "pet_card", "exposure": "painted", "active": True}]}
    context = snapshot() | {
        "timezone": "Asia/Shanghai", "phase_elapsed": [transition],
        "recent_contacts": [contact | {"screen_text": FRAME,
                                        "surfaces": [contact["surfaces"][0] | {"summary": FRAME}]}],
        "recent_user_actions": [{"subject_ref": "reminder:1", "at": at}],
        "candidate_items_truncated": True,
        "last_observed_activity": {"observed_at": at, "summary_observed_at": at,
                                   "scene_observed_at": at, "summary": SUMMARY, "relation": "previous"},
    }
    intent = {"requested_at": at, "timezone": "Asia/Shanghai", "reply_to_proposal_version": 3}
    payload = {"origin": "user", "semantic_chat": True, "semantic_background": True,
               "event": {"kinds": ["recheck", "resume"], "trigger_reasons": ["phase_elapsed", "resume"],
                         "phase_elapsed": [transition]}, "user_intent": intent | {"utterance": "稍后提醒我"},
               "context": context}

    utterance, projected = project_input(payload, share=True, origin="user")
    assert utterance == "稍后提醒我" and projected["origin"] == "user"
    assert projected["user_intent"] == intent
    assert projected["semantic_chat"] is True and projected["semantic_background"] is True
    assert projected["event"] == payload["event"]
    assert projected["context"]["phase_elapsed"] == [transition]
    assert projected["context"]["recent_contacts"] == [contact]
    assert projected["context"]["recent_user_actions"] == context["recent_user_actions"]
    assert projected["context"]["candidate_items_truncated"] is True
    assert projected["context"]["last_observed_activity"]["summary_observed_at"] == at
    assert FRAME not in json.dumps(projected)

    utterance, projected = project_input(payload, share=False, origin="background")
    assert utterance is None and projected["origin"] == "background" and "user_intent" not in projected
    presentation = {"channel": "pet_card", "delivery_id": "proposal-ui-1", "state": "painted"}
    receipt = {"status": "applied", "outcome": "shown", "presentation_receipt": presentation, "view": context}
    wire = project_tool_result("context_read", receipt, share=False)
    assert wire["outcome"] == "shown" and wire["presentation_receipt"] == presentation
    assert wire["view"]["recent_contacts"] == [contact]
    assert wire["view"]["last_observed_activity"]["observed_at"] == at
    assert "scene_observed_at" not in wire["view"]["last_observed_activity"]
    assert SUMMARY not in json.dumps(wire)


@pytest.mark.parametrize("provider, cloud", [("openai_compatible", True), ("local_ollama", False)])
def test_semantic_background_session_and_document_scope(tmp_path, monkeypatch, provider, cloud):
    monkeypatch.setenv("MOCHI_SYSTEM2_SEMANTIC_BACKGROUND", "1")

    class RuntimeStub:
        def __init__(self, path, policy, *, session_id, document_sources, private_storage, **_):
            self.session_id = session_id
            self.document_sources = document_sources
            self.storage = private_storage

        def close(self):
            if self.storage:
                self.storage.cleanup()

    monkeypatch.setattr("deskpet.system2.dsh._Runtime", RuntimeStub)
    settings = SettingsStore(tmp_path / "business.sqlite")
    base_url = "https://example.test/v1" if cloud else "http://127.0.0.1:11434"
    configured = settings.routes.configure(SYSTEM2, CloudRoute(provider, base_url, "test-model", "Fixture"))
    if cloud:
        configured = settings.routes.set_permission(SYSTEM2, True, expected=configured)
    configured = settings.routes.record_result(configured, probe=True)
    settings.update(expected_version=settings.read().version,
                    document_local_sources=("Word", "VS Code"), document_cloud_sources=("Word",))
    engine = DSHEngine(tmp_path, settings=settings, tools=definitions())
    try:
        request = turn()
        user = engine._select_runtime(request, configured)
        assert user.storage is not None
        request.observation_document_query = True
        document_user = engine._select_runtime(request, configured)
        assert document_user is user
        request.source = "background"
        background = engine._select_runtime(request, configured)
        assert background.session_id not in (user.session_id, document_user.session_id)
        assert document_user.document_sources == (("app:microsoft-word",) if cloud else ("app:microsoft-word", "app:vscode"))
        assert background.document_sources == (() if cloud else document_user.document_sources)
        assert bool(background.storage) is not cloud
    finally:
        engine.close()


def test_native_projection_tools_metadata_and_restart(tmp_path, endpoints, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "MUST-NOT-LEAK")
    endpoint = endpoints()
    settings = SettingsStore(tmp_path / "business.sqlite")
    install_route(settings, endpoint)
    engine = DSHEngine(tmp_path, settings=settings, tools=definitions())
    try:
        result = engine.run(turn(), reply)
        assert result.text == RECEIPT
        assert result.metadata["tool_calls"] == [{
            "name": "context_read", "call_id": "call-fixture", "step_id": f"{result.session_id}:1:1",
        }]
        assert result.metadata["model_requests"] == 2
        assert result.metadata["token_usage"]["total_tokens"] == 30
        assert result.metadata["duration_s"] >= 0
        assert result.metadata["request_timeout_s"] == 5
        assert "item_id" not in json.dumps(result.metadata)
        assert all(value is None for value in endpoint.authorizations)
    finally:
        engine.close()

    engine = DSHEngine(tmp_path, settings=settings, tools=definitions())
    try:
        replayed = []
        def record_replay(*args, **kwargs):
            replayed.append((args, kwargs))
            return {"status": "unexpected_replay"}
        resumed = engine.run(turn("Repeat your previous receipt."), record_replay)
        assert not replayed
        assert resumed.text == RECEIPT and resumed.session_id == result.session_id
        assert resumed.metadata["model_requests"] == 1
        assert len(endpoint.requests) == 3
        assert_full_wire(endpoint.requests)
        assert RECEIPT not in json.dumps(endpoint.requests[0])
        assert all(RECEIPT in json.dumps(request) for request in endpoint.requests[1:])
    finally:
        engine.close()


@pytest.mark.parametrize("reason,semantic,split,version", [
    ("tick", True, True, SPLIT_PROMPT_VERSION),
    ("unrecognized", True, True, PROMPT_VERSION),
    ("tick", False, True, PROMPT_VERSION),
    ("tick", True, False, PROMPT_VERSION),
])
def test_native_prompt_sections_and_fallback(tmp_path, endpoints, monkeypatch, reason, semantic, split, version):
    monkeypatch.setenv("MOCHI_SYSTEM2_SPLIT_PROMPT", "1" if split else "0")
    monkeypatch.setenv("MOCHI_SYSTEM2_SEMANTIC_BACKGROUND", "1" if semantic else "0")
    endpoint = endpoints()
    settings = SettingsStore(tmp_path / "business.sqlite")
    install_route(settings, endpoint)
    request = turn()
    request.source, request.prompt_version, request.system_prompt = "background", PROMPT_VERSION, SYSTEM_PROMPT
    context = snapshot()
    request.input_json = json.dumps({
        "origin": request.source, "semantic_background": semantic,
        "event": {"kind": reason, "kinds": [reason], "trigger_reasons": [reason]},
        "context": context, "grants": {},
    })

    def respond(body):
        if body["messages"][-1]["role"] == "tool":
            return {"content": RECEIPT}, "stop"
        return {"tool_calls": [{
            "index": 0, "id": "fixture-read", "type": "function",
            "function": {"name": "context_read", "arguments": "{}"},
        }]}, "tool_calls"

    def tool_reply(name, call_id, arguments, *, step_id):
        assert name == "context_read" and call_id == "fixture-read" and arguments == {}
        assert step_id.endswith(":1:1")
        return {"status": "applied", "message": RECEIPT, "view": context}

    endpoint.respond = respond
    context_tool = next(tool for tool in tool_definitions if tool.canonical_name == "context.read")
    engine = DSHEngine(tmp_path, settings=settings, tools=[context_tool, definitions()[1]])
    try:
        result = engine.run(request, tool_reply)
        assert result.metadata["prompt_version"] == version
        assert result.metadata["model_requests"] == 2
        assert len(endpoint.requests) == 2
        system = [next(msg["content"] for msg in body["messages"] if msg["role"] == "system")
                  for body in endpoint.requests]
        assert system[0] == system[1]
        if version == SPLIT_PROMPT_VERSION:
            assert system[0] == "\n\n".join((
                PROMPT_CORE, PROMPT_SECTIONS["background_arbitration"], PROMPT_SECTIONS["periodic_check"],
            ))
        else:
            assert system[0].strip() == SYSTEM_PROMPT.strip()
        for body in endpoint.requests:
            assert [tool["function"]["name"] for tool in body["tools"]] == ["context_read"]
            properties = body["tools"][0]["function"]["parameters"]["properties"]
            assert ("activity_window" in properties) is (version == SPLIT_PROMPT_VERSION)
            assert FRAME not in json.dumps(body) and SUMMARY not in json.dumps(body)
    finally:
        engine.close()


def test_native_bridge_rejects_unbound_and_mismatched_steps():
    executable = shutil.which("node")
    if executable is None:
        pytest.skip("Node is unavailable for the bridge contract test")
    test = Path(__file__).with_name("system2_dsh_bridge.mjs")
    process = subprocess.run([executable, "--test", str(test)], capture_output=True, text=True, timeout=15)
    if process.returncode and "listen EPERM" in process.stdout:
        pytest.skip("Loopback bind is unavailable")
    assert process.returncode == 0, process.stdout + process.stderr


def test_native_scope_change_stops_tool_continuation(tmp_path, endpoints, monkeypatch):
    monkeypatch.setenv("MOCHI_SYSTEM2_SPLIT_PROMPT", "1")
    monkeypatch.setenv("MOCHI_SYSTEM2_SEMANTIC_BACKGROUND", "1")
    endpoint = endpoints()
    settings = SettingsStore(tmp_path / "business.sqlite")
    install_route(settings, endpoint)
    request = turn()
    request.source, request.prompt_version, request.system_prompt = "background", PROMPT_VERSION, SYSTEM_PROMPT
    request.input_json = json.dumps({
        "semantic_background": True, "event": {"kinds": ["tick"], "trigger_reasons": ["tick"]},
        "context": snapshot(),
    })
    authorised = Event()
    authorised.set()
    request.is_scope_current = authorised.is_set

    def change_scope(*args, **kwargs):
        authorised.clear()
        return reply(*args, **kwargs)

    engine = DSHEngine(tmp_path, settings=settings, tools=definitions())
    try:
        with pytest.raises(CloudRouteError, match="requires a new prompt admission"):
            engine.run(request, change_scope)
        assert len(endpoint.requests) == 1
        authorised.set()
        request.admission_id = uuid4().hex
        assert engine.run(request, reply).metadata["prompt_version"] == SPLIT_PROMPT_VERSION
        assert len(endpoint.requests) == 3
    finally:
        engine.close()


def test_native_split_chat_restores_only_recent_text_in_same_privacy_epoch(tmp_path, endpoints, monkeypatch):
    monkeypatch.setenv("MOCHI_SYSTEM2_SPLIT_PROMPT", "1")
    endpoint = endpoints()
    settings = SettingsStore(tmp_path / "business.sqlite")
    install_route(settings, endpoint, permission=True)
    historical = "SYNTHETIC-PRIVATE-CHAT-RESTART"

    def run(message):
        request = turn(message, tools=())
        request.prompt_version, request.system_prompt = PROMPT_VERSION, SYSTEM_PROMPT
        request.input_json = json.dumps({
            "semantic_chat": True, "event": {"kind": "user_message", "trigger_reasons": ["user_message"]},
            "user_intent": {"utterance": message}, "context": snapshot(),
        }, ensure_ascii=False)
        engine = DSHEngine(tmp_path, settings=settings, tools=definitions())
        try:
            return engine.run(request, reply)
        finally:
            engine.close()

    first = run(historical)
    assert first.metadata["prompt_version"] == SPLIT_PROMPT_VERSION
    paths = list(tmp_path.rglob("recent-chat.json"))
    assert len(paths) == 1
    assert json.loads(paths[0].read_text()) == [{"user": historical, "assistant": RECEIPT}]
    assert not list(tmp_path.rglob("session.v4.jsonl"))
    second = run("Continue the same conversation")
    assert second.session_id != first.session_id
    messages = endpoint.requests[-1]["messages"]
    assert len(messages) == 3
    assert historical in json.dumps(messages) and RECEIPT in json.dumps(messages)
    assert messages[0]["content"] == "\n\n".join((
        PROMPT_CORE, PROMPT_SECTIONS["chat_exchange"], PROMPT_SECTIONS["chat_items_reminders"],
        PROMPT_SECTIONS["chat_timers_habits_settings"],
    ))
    assert json.dumps(messages).count(SUMMARY) <= 1

    settings.routes.set_permission(SYSTEM2, False, expected=settings.routes.snapshot(SYSTEM2))
    post_revoke = run("Check in after changing permission")
    assert post_revoke.session_id not in (first.session_id, second.session_id)
    assert historical not in json.dumps(endpoint.requests[-1])
    assert RECEIPT not in json.dumps(endpoint.requests[-1])


def test_revocation_between_tool_and_follow_up_rotates_all_history(tmp_path, endpoints):
    endpoint = endpoints()
    settings = SettingsStore(tmp_path / "business.sqlite")
    allowed = install_route(settings, endpoint, permission=True)
    engine = DSHEngine(tmp_path, settings=settings, tools=definitions())

    def revoke(name, native_call_id, arguments, *, step_id):
        settings.routes.set_permission(SYSTEM2, False, expected=allowed)
        return reply(name, native_call_id, arguments, step_id=step_id)

    try:
        with pytest.raises(CloudRouteError):
            engine.run(turn(), revoke)
        assert len(endpoint.requests) == 1
        assert_full_wire(endpoint.requests, share=True)
        new_result = engine.run(turn(), reply)
        assert new_result.text == RECEIPT
        assert len(endpoint.requests) == 3
        assert_full_wire(endpoint.requests[1:])
        assert len(endpoint.requests[1]["messages"]) == 3
    finally:
        engine.close()


@pytest.mark.parametrize("split", [False, True])
def test_document_titles_reach_the_wire_only_on_authorized_tool_queries(tmp_path, endpoints, monkeypatch, split):
    monkeypatch.setenv("MOCHI_SYSTEM2_SPLIT_PROMPT", "1" if split else "0")
    endpoint = endpoints()
    settings = SettingsStore(tmp_path / "business.sqlite")
    install_route(settings, endpoint, permission=True)
    settings.update(expected_version=settings.read().version,
                    document_local_sources=("Word", "VS Code"), document_cloud_sources=("Word",))
    shared = "WireAcceptance.docx"
    local_only = "PrivateWorkspace.py"
    context = snapshot()
    at = context["as_of"]
    context.update(
        current_context={"observed_at": at, "foreground_app": "Mochi", "screen_locked": False},
        last_observed_activity={"observed_at": at, "activity_kind": "work", "relation": "previous",
                                "document_name": shared, "document_source": "app:microsoft-word"},
        history_1h={"segments": [{"first_observed_at": at, "last_observed_at": at,
                                  "document_name": local_only, "document_source": "app:vscode"}]},
    )

    def run(text, *, source="user", include_document_source=None, read=True):
        args = {} if include_document_source is None else {"include_document_source": include_document_source}

        def respond(body):
            last = body["messages"][-1]
            if last["role"] == "tool":
                visible = json.loads(last["content"])["view"]["last_observed_activity"]
                return {"content": visible.get("document_name", RECEIPT)}, "stop"
            if not body.get("tools"):
                return {"content": RECEIPT}, "stop"
            return {"tool_calls": [{
                "index": 0, "id": "context-fixture", "type": "function",
                "function": {"name": "context_read", "arguments": json.dumps(args)},
            }]}, "tool_calls"

        def result(name, call_id, invoked, *, step_id):
            assert name == "context_read" and invoked == args
            return {"status": "applied", "view": context}

        endpoint.respond = respond
        request = turn(text, tools=("context.read",) if read else ())
        request.source = source
        request.observation_document_query = source == "background"
        request.observation_generation = settings.read().observation_generation
        payload = {"user_intent": {"utterance": text}, "context": context}
        if split and source == "user":
            request.prompt_version, request.system_prompt = PROMPT_VERSION, SYSTEM_PROMPT
            payload.update(semantic_chat=True,
                           event={"kind": "user_message", "trigger_reasons": ["user_message"]})
        request.input_json = json.dumps(payload, ensure_ascii=False)
        start = len(endpoint.requests)
        response = engine.run(request, result)
        if split and source == "user":
            assert response.metadata["prompt_version"] == SPLIT_PROMPT_VERSION
        return response.text, [json.dumps(body, ensure_ascii=False) for body in endpoint.requests[start:]]

    engine = DSHEngine(tmp_path, settings=settings, tools=[tool for tool in tool_definitions
                                                          if tool.canonical_name == "context.read"])
    try:
        ordinary = "刚才在忙什么？"
        answer, packets = run(ordinary)
        assert answer == RECEIPT
        assert all(shared not in body and local_only not in body for body in packets)

        answer, packets = run("刚才打开的是哪份文档？", include_document_source=True)
        assert answer == shared
        assert shared not in packets[0] and shared in packets[-1]
        assert all(local_only not in body and "app:microsoft-word" not in body for body in packets)

        answer, packets = run("再说说别的", read=False)
        assert answer == RECEIPT and ordinary in packets[-1]
        assert "本轮涉及观察到的文档标题" in packets[-1]
        assert all(shared not in body and local_only not in body for body in packets)

        answer, packets = run("背景事件", source="background", include_document_source=True)
        assert answer == RECEIPT
        assert all(shared not in body and local_only not in body and ordinary not in body for body in packets)

        settings.update(expected_version=settings.read().version, document_cloud_sources=())
        answer, packets = run("这次打开的是哪份文档？", include_document_source=True)
        assert answer == RECEIPT
        assert all(shared not in body and local_only not in body for body in packets)
        assert all(shared not in path.read_text() and local_only not in path.read_text()
                   for path in tmp_path.rglob("recent-chat.json"))
    finally:
        engine.close()


def test_removed_chat_flag_keeps_the_same_native_session(tmp_path, endpoints, monkeypatch):
    endpoint = endpoints()
    settings = SettingsStore(tmp_path / "business.sqlite")
    install_route(settings, endpoint)
    first_message = "SYNTHETIC-FIRST-SESSION-MESSAGE"
    monkeypatch.setenv("MOCHI_SYSTEM2_SEMANTIC_CHAT", "1")
    engine = DSHEngine(tmp_path, settings=settings, tools=definitions())
    try:
        first = engine.run(turn(first_message, tools=()), reply)
        monkeypatch.setenv("MOCHI_SYSTEM2_SEMANTIC_CHAT", "0")
        follow_up = engine.run(turn("Continue the same conversation", tools=()), reply)
        assert follow_up.session_id == first.session_id
        assert first_message in json.dumps(endpoint.requests[-1])
    finally:
        engine.close()


def test_endpoint_switch_during_send_prevents_first_packet(tmp_path, endpoints):
    first, second = endpoints(), endpoints()
    settings = SettingsStore(tmp_path / "business.sqlite")
    install_route(settings, first, permission=True, credential_ref="first-service")
    entering, release = Event(), Event()

    def credential(_route):
        entering.set()
        assert release.wait(10)
        return "test-recipient-secret"

    engine = DSHEngine(tmp_path, settings=settings, tools=definitions(), credential=credential)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            run = executor.submit(engine.run, turn(), reply)
            try:
                assert entering.wait(12)
                install_route(settings, second)
            finally:
                release.set()
            with pytest.raises(CloudRouteError):
                run.result(timeout=10)
        assert first.requests == []
        assert engine.run(turn(), reply).text == RECEIPT
        assert first.requests == []
        assert_full_wire(second.requests)
    finally:
        engine.close()


def test_capability_probe_and_no_http_503_retry(tmp_path, endpoints):
    endpoint = endpoints()
    endpoint.verify = True
    settings = SettingsStore(tmp_path / "business.sqlite")
    configured = install_route(settings, endpoint)
    assert verify_system2_route(settings.routes, expected=configured).verified
    assert len(endpoint.requests) == 2
    assert all(marker not in json.dumps(endpoint.requests) for marker in (SUMMARY, FRAME, TITLE))
    endpoint.failure = 503
    engine = DSHEngine(tmp_path, settings=settings, tools=definitions())
    try:
        with pytest.raises(CloudRouteError):
            engine.run(turn(), reply)
        assert len(endpoint.requests) == 3
    finally:
        engine.close()


@pytest.mark.parametrize("base_url", [
    BAILIAN_BASE_URL,
    "https://llm-fixture.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
])
def test_bailian_native_tool_continuation_and_credential_scope(tmp_path, endpoints, monkeypatch, base_url):
    endpoint, redirect = endpoints(), endpoints()
    endpoint.path = "/compatible-mode/v1/chat/completions"
    endpoint.reasoning_parts = ("核对", "合成项目")
    # Bailian can report prompt usage beyond the generic 32,768-token DSH profile.
    endpoint.prompt_tokens = 33_500
    public_url = httpx.URL(base_url + "/chat/completions")
    loopback = httpx.URL(endpoint.origin)
    original_handle = httpx.HTTPTransport.handle_request

    def local_wire(transport, request):
        if request.url.host != public_url.host:
            return original_handle(transport, request)
        assert request.url == public_url
        assert request.headers["Host"] == public_url.host
        request.url = public_url.copy_with(scheme=loopback.scheme, host=loopback.host, port=loopback.port)
        try:
            return original_handle(transport, request)
        finally:
            request.url = public_url

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", local_wire)
    settings = SettingsStore(tmp_path / "business.sqlite")
    route = CloudRoute(BAILIAN, base_url, BAILIAN_MODEL, "百炼", "synthetic-bailian", timeout_s=5)
    key = "sk-fixture-private"
    password_store = {}
    keyring = SimpleNamespace(
        set_password=lambda service, ref, value: password_store.__setitem__((service, ref), value),
        get_password=lambda service, ref: password_store.get((service, ref)),
    )
    monkeypatch.setattr("deskpet.observation_v2.openai_cloud._keyring", lambda: keyring)
    store_route_credential(route, key)
    configured = settings.routes.configure(SYSTEM2, route)
    admissions = []
    original_admit = settings.routes.admit

    @contextmanager
    def admit(expected, *, probe=False):
        with original_admit(expected, probe=probe):
            assert expected.route == route
            admissions.append(probe)
            yield

    monkeypatch.setattr(settings.routes, "admit", admit)
    endpoint.verify = True
    assert verify_system2_route(settings.routes, expected=configured).verified
    endpoint.verify = False

    engine = DSHEngine(tmp_path, settings=settings, tools=definitions())
    try:
        first = engine.run(turn(), reply)
        follow_up = engine.run(turn("Repeat the receipt."), reply)
        assert first.text == follow_up.text == RECEIPT
        assert [row["prompt_tokens"] for row in first.metadata["provider_request_usage"]] == [33_500, 33_500]
        assert [row["prompt_tokens"] for row in follow_up.metadata["provider_request_usage"]] == [33_500]
        assert admissions == [True, True, False, False, False]
        reasoning = "".join(endpoint.reasoning_parts)
        for first, continuation in [(0, 1), (2, 3)]:
            assert reasoning not in json.dumps(endpoint.requests[first], ensure_ascii=False)
            assistant, tool_result = endpoint.requests[continuation]["messages"][-2:]
            assert assistant["role"] == "assistant" and assistant["reasoning_content"] == reasoning
            assert assistant["tool_calls"][0]["id"] == tool_result["tool_call_id"] == "call-fixture"
        assert reasoning not in json.dumps(endpoint.requests[4], ensure_ascii=False)
        assert all(body["model"] == BAILIAN_MODEL and body["stream"] for body in endpoint.requests)
        assert all(body["enable_thinking"] and body["preserve_thinking"] for body in endpoint.requests)
        assert all(0 < body["thinking_budget"] < body["max_completion_tokens"] for body in endpoint.requests)
        assert all(key not in json.dumps(body) for body in endpoint.requests)
        assert endpoint.hosts == [public_url.host] * 5
        assert endpoint.authorizations == [f"Bearer {key}"] * 5
        assert key.encode() not in settings.path.read_bytes()
        histories = list(tmp_path.rglob("session.v4.jsonl"))
        assert histories
        for path in histories:
            history = path.read_text(encoding="utf-8")
            assert key not in history
            assert all(part not in history and json.dumps(part)[1:-1] not in history for part in endpoint.reasoning_parts)

        endpoint.failure = 307
        endpoint.redirect_to = redirect.origin + "/v1/chat/completions"
        with pytest.raises(CloudRouteError, match="attempted to redirect"):
            engine.run(turn(), reply)
        assert len(endpoint.requests) == 6
        assert redirect.requests == []

        endpoint.failure = 400
        endpoint.redirect_to = None
        endpoint.failure_body = {"error": {"code": "InvalidParameter", "message": SUMMARY}}
        with pytest.raises(CloudRouteError, match=r"HTTP 400 \(InvalidParameter\)"):
            engine.run(turn(), reply)
        failure = settings.routes.snapshot(SYSTEM2).last_failure
        assert failure == "System 2 endpoint returned HTTP 400 (InvalidParameter)"
        assert len(endpoint.requests) == 7
    finally:
        engine.close()


@pytest.mark.parametrize("base_url", [
    "http://localhost/compatible-mode/v1",
    "https://dashscope.aliyuncs.com.attacker.invalid/compatible-mode/v1",
    "https://example.invalid/compatible-mode/v1",
    "https://dashscope.aliyuncs.com:8443/compatible-mode/v1",
    "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
    "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
])
def test_bailian_rejects_other_recipients(base_url):
    with pytest.raises(ValueError, match="official Alibaba Cloud Model Studio HTTPS"):
        CloudRoute(BAILIAN, base_url, BAILIAN_MODEL, "百炼", "synthetic-bailian")


def test_bailian_route_is_system2_only_and_requires_credential_and_model(tmp_path):
    routes = SettingsStore(tmp_path / "business.sqlite").routes
    route = CloudRoute(BAILIAN, BAILIAN_BASE_URL, BAILIAN_MODEL, "百炼", "synthetic-bailian")
    for role in (VISION, STATE):
        with pytest.raises(ValueError, match="System 2 only"):
            routes.configure(role, route)
        assert routes.snapshot(role).route is None
    with pytest.raises(ValueError, match="requires qwen4-max and a local credential reference"):
        CloudRoute(BAILIAN, BAILIAN_BASE_URL, BAILIAN_MODEL, "百炼")
    with pytest.raises(ValueError, match="requires qwen4-max and a local credential reference"):
        CloudRoute(BAILIAN, BAILIAN_BASE_URL, "qwen4-max-preview", "百炼", "synthetic-bailian")
