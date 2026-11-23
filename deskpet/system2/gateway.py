"""Authenticated DSH loopback gateway and the per-send System 2 privacy fence."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import closing, contextmanager
from dataclasses import dataclass
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import secrets
from threading import Event, Thread

import httpcore
import httpx

from deskpet.observation_v2.routes import BAILIAN, CloudRoute, CloudRouteError, CloudRouteSnapshot, ObservationCloudSettings

from .bailian import BailianTurn

_LOG = logging.getLogger(__name__)
_MAX_BODY = 4 * 1024 * 1024
_BAILIAN_BAD_REQUEST_CODES = frozenset({
    "InvalidParameter", "InternalError.Algo.InvalidParameter", "DataInspectionFailed",
    "InvalidParameter.DataInspection", "invalid_parameter_error", "context_length_exceeded",
})


def _bailian_bad_request_code(response: httpx.Response) -> str | None:
    try:
        with closing(response.iter_bytes(chunk_size=4097)) as chunks:
            data = next(chunks, b"")
        if len(data) > 4096:
            return None
        payload = json.loads(data)
    except (httpx.HTTPError, ValueError, RecursionError):
        return None
    if isinstance(payload, dict):
        error = payload.get("error", payload)
        if isinstance(error, dict):
            code = error.get("code")
            if isinstance(code, str) and code in _BAILIAN_BAD_REQUEST_CODES:
                return code
    return None


@dataclass(frozen=True)
class SendPolicy:
    routes: ObservationCloudSettings
    snapshot: CloudRouteSnapshot
    probe: bool = False
    is_observation_current: Callable[[], bool] | None = None

    def _check_observation(self) -> None:
        if self.is_observation_current is not None and not self.is_observation_current():
            raise CloudRouteError("System 2 activity authorization changed")

    def check(self) -> None:
        self.routes.check(self.snapshot, probe=self.probe)
        self._check_observation()

    @contextmanager
    def admit(self):
        with self.routes.admit(self.snapshot, probe=self.probe):
            self._check_observation()
            yield


class ModelGateway:
    """Only this host process sees the configured endpoint or its OS credential."""

    def __init__(
        self, policy: SendPolicy, *, tool_names: frozenset[str],
        invoke: Callable[[dict], dict], credential: Callable[[CloudRoute], str],
        admit_request: Callable[[frozenset[str]], None], check_active: Callable[[], BailianTurn],
    ) -> None:
        self.policy = policy
        self._tool_names = tool_names
        self._invoke = invoke
        self._credential = credential
        self._admit_request = admit_request
        self._check_active = check_active
        self.token = secrets.token_urlsafe(32)
        self.last_failure: str | None = None
        self._stopping = Event()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self._thread.start()

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    def close(self) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=1)

    def _check(self) -> BailianTurn:
        if self._stopping.is_set():
            raise CloudRouteError("System 2 transport was closed")
        self.policy.check()
        return self._check_active()

    def _validate_model_body(self, body: object) -> dict:
        route = self.policy.snapshot.route
        assert route is not None
        if not isinstance(body, dict) or body.get("model") != route.model or body.get("stream") is not True:
            raise CloudRouteError("Unexpected System 2 model request")
        tools = body.get("tools", [])
        if not isinstance(tools, list):
            raise CloudRouteError("Invalid System 2 tool list")
        names = [
            tool["function"].get("name") for tool in tools
            if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
        ]
        if len(names) != len(tools) or any(not isinstance(name, str) or name not in self._tool_names for name in names):
            raise CloudRouteError("System 2 attempted to expose an unsupported tool")
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise CloudRouteError("Missing System 2 messages")
        for message in messages:
            if not isinstance(message, dict) or message.get("role") not in {"system", "developer", "user", "assistant", "tool"}:
                raise CloudRouteError("Invalid System 2 message")
            if message.keys() - {"role", "content", "tool_calls", "tool_call_id", "name"}:
                raise CloudRouteError("Unexpected System 2 message fields")
            content = message.get("content")
            if content is not None and not isinstance(content, str):
                if not isinstance(content, list) or any(
                    not isinstance(block, dict) or set(block) != {"type", "text"}
                    or block["type"] != "text" or not isinstance(block["text"], str)
                    for block in content
                ):
                    raise CloudRouteError("System 2 accepts text only")
        allowed = {"model", "stream", "stream_options", "messages", "tools", "tool_choice",
                   "max_tokens", "max_completion_tokens", "parallel_tool_calls"}
        forwarded = {key: value for key, value in body.items() if key in allowed}
        if route.provider_kind == "openai":
            forwarded["store"] = False
        self._admit_request(frozenset(names))
        return forwarded

    def _handler(self):
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                self.request.settimeout(4)
                super().setup()

            def log_message(self, *_):
                pass

            def _write(self, status: int, payload: dict) -> None:
                data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:
                authorization = self.headers.get("Authorization", "")
                if not hmac.compare_digest(authorization.encode(), ("Bearer " + gateway.token).encode()):
                    self._write(403, {"error": "System 2 bridge authorization failed"})
                    return
                if self.path not in {"/v1/chat/completions", "/tool"}:
                    self._write(404, {"error": "Unsupported System 2 bridge path"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= _MAX_BODY or self.headers.get("Transfer-Encoding"):
                        raise ValueError("invalid body length")
                    data = json.loads(self.rfile.read(length))
                    gateway._check()
                    if self.path == "/tool":
                        if not isinstance(data, dict) or data.get("name") not in gateway._tool_names:
                            raise CloudRouteError("Unsupported System 2 tool")
                        self._write(200, gateway._invoke(data))
                    else:
                        gateway._forward(gateway._validate_model_body(data), self)
                except httpx.TimeoutException:
                    gateway.last_failure = "System 2 endpoint timed out"
                    if not getattr(self, "_streaming", False):
                        self._write(504, {"error": gateway.last_failure})
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    return
                except (CloudRouteError, ValueError, TypeError):
                    if not getattr(self, "_streaming", False):
                        self._write(403, {"error": "System 2 request was rejected"})
                except Exception:
                    if not getattr(self, "_streaming", False):
                        self._write(502, {"error": "System 2 transport failed"})

        return Handler

    def _forward(self, body: dict, handler: BaseHTTPRequestHandler) -> None:
        route = self.policy.snapshot.route
        assert route is not None
        if httpcore.__version__ != "1.0.9":
            raise CloudRouteError("Unsupported System 2 HTTP transport")
        active = self._check()
        bailian = route.provider_kind == BAILIAN
        if bailian:
            active.prepare(body)
        base = route.base_url + ("" if route.is_cloud else "/v1")
        headers = {"Accept": "text/event-stream"}
        if route.credential_ref:
            headers["Authorization"] = "Bearer " + self._credential(route)
        self._check()
        finished = Event()
        revoked = Event()
        gate = None
        transport = httpx.HTTPTransport(retries=0, trust_env=False, http2=False)

        def release() -> None:
            nonlocal gate
            if gate is not None:
                admitted, gate = gate, None
                admitted.__exit__(None, None, None)

        def trace(event: str, _info: dict) -> None:
            nonlocal gate
            if event == "http11.send_request_headers.started":
                if self._stopping.is_set() or revoked.is_set():
                    raise CloudRouteError("System 2 transport was closed")
                admission = self.policy.admit()
                admission.__enter__()
                gate = admission
                self._check_active()
                _LOG.debug(
                    "System 2 send admitted role=%s route_generation=%s consent_generation=%s recipient=%s",
                    self.policy.snapshot.role, self.policy.snapshot.route_generation,
                    self.policy.snapshot.consent_generation, route.base_url,
                )
            elif event == "http11.send_request_body.complete":
                release()

        def monitor() -> None:
            while not finished.wait(0.1):
                try:
                    self._check()
                except Exception:
                    revoked.set()
                    transport.close()
                    return

        watcher = Thread(target=monitor, name="system2-send-policy", daemon=True)
        watcher.start()
        try:
            with httpx.Client(
                transport=transport, trust_env=False, follow_redirects=False,
                timeout=httpx.Timeout(route.timeout_s, write=min(2.0, route.timeout_s)),
            ) as http:
                request = http.build_request("POST", base + "/chat/completions", headers=headers, json=body)
                request.extensions["trace"] = trace
                with closing(http.send(request, stream=True)) as response:
                    self._check()
                    if response.status_code != 200:
                        status = response.status_code
                        if 300 <= status <= 399:
                            self.last_failure = "System 2 provider attempted to redirect"
                            status = 502
                        else:
                            self.last_failure = f"System 2 endpoint returned HTTP {status}"
                            if bailian and status == 400 and (code := _bailian_bad_request_code(response)):
                                self.last_failure += f" ({code})"
                            if not 400 <= status <= 599:
                                status = 502
                        handler._write(status, {"error": "System 2 provider rejected the request"})
                        return
                    if "text/event-stream" not in response.headers.get("content-type", "").lower():
                        raise CloudRouteError("System 2 requires a streaming chat endpoint")
                    handler._streaming = True
                    handler.send_response(200)
                    handler.send_header("Content-Type", "text/event-stream")
                    handler.end_headers()
                    chunks = active.stream(response.iter_lines()) if bailian else response.iter_bytes()
                    for chunk in chunks:
                        self._check()
                        handler.wfile.write(chunk)
                        handler.wfile.flush()
        finally:
            release()
            finished.set()
            watcher.join(timeout=0.3)
