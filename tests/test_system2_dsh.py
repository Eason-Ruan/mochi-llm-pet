"""Full-wire regressions for the pinned native System 2 runtime and its host gateway."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
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
                        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
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
        assert engine.run(turn(), reply).text == RECEIPT
        assert engine.run(turn("Repeat the receipt."), reply).text == RECEIPT
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
