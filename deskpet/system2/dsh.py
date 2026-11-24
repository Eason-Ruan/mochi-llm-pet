"""Host adapter for the pinned official DeepSeek Harness runtime."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
from queue import Empty, Queue
import re
import secrets
import signal
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread
from types import SimpleNamespace
import time

import httpx

from deskpet.observation_v2.openai_cloud import _resolve_credential
from deskpet.observation_v2.routes import SYSTEM2, CloudRoute, CloudRouteError, CloudRouteSnapshot, ObservationCloudSettings
from deskpet.settings import SettingsStore

from .bailian import BailianTurn
from .dsh_profile import isolated_environment, write_profile
from .dsh_projection import project_input, project_tool_result
from .gateway import ModelGateway, SendPolicy
from .semantic_policy import semantic_background_enabled, semantic_chat_enabled, text_sharing_allowed
from .tool_schema import READ_TOOLS, canonical_tool_name

_WIRE_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_PROFILE_VERSION = "mochi-dsh-0.2.2rc2-v2"
_INIT_TIMEOUT = 12.0
_MAX_MODEL_STEPS = 6
_STOP = object()


class System2RunError(CloudRouteError):
    """A failed turn with operational metadata and no model or user text."""

    def __init__(self, message: str, metadata: dict):
        super().__init__(message)
        self.metadata = metadata


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _schemas(tools: Iterable) -> list[dict]:
    registered = []
    names = set()
    for tool in tools:
        name = tool.name
        parameters = tool.input_schema
        if (not isinstance(name, str) or not _WIRE_NAME.fullmatch(name) or name == "run_code"
                or name in names or not isinstance(parameters, dict) or parameters.get("type") != "object"
                or not isinstance(tool.description, str) or not tool.description):
            raise ValueError("Invalid registered System 2 tool")
        names.add(name)
        registered.append({"name": name, "description": tool.description, "parameters": parameters})
    # Own a detached copy of exactly the schemas supplied by the host registry.
    return json.loads(_json(registered))


class _SDKWorker:
    def __init__(self, home: Path, token: str) -> None:
        if not sys.executable:
            raise CloudRouteError("System 2 requires a Python executable")
        worker = Path(__file__).with_name("dsh_worker.py")
        self._process = subprocess.Popen(
            [sys.executable, "-I", "-u", str(worker)], cwd=home,
            env=isolated_environment(home, token), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1,
            start_new_session=os.name == "posix",
        )
        self._stdin_lock = Lock()
        self._output: Queue = Queue()
        self._reader = Thread(target=self._read, daemon=True, name="mochi-dsh-sdk")
        self._reader.start()

    def _read(self) -> None:
        assert self._process.stdout is not None
        try:
            for line in self._process.stdout:
                self._output.put(json.loads(line))
        except (ValueError, OSError):
            pass
        finally:
            self._output.put(_STOP)

    def _write(self, value: dict) -> None:
        assert self._process.stdin is not None
        with self._stdin_lock:
            self._process.stdin.write(_json(value) + "\n")
            self._process.stdin.flush()

    def request(self, value: dict, *, timeout: float, expected: str, cancelled: Event | None = None) -> dict:
        try:
            self._write(value)
        except (OSError, ValueError):
            raise CloudRouteError("System 2 runtime is unavailable") from None
        expires = time.monotonic() + timeout
        while True:
            if cancelled is not None and cancelled.is_set():
                raise CloudRouteError("System 2 runtime was closed")
            remaining = expires - time.monotonic()
            if remaining <= 0:
                raise CloudRouteError("System 2 runtime timed out")
            try:
                response = self._output.get(timeout=min(0.15, remaining))
                break
            except Empty:
                continue
        if response is _STOP or not isinstance(response, dict):
            raise CloudRouteError("System 2 runtime exited unexpectedly")
        if response.get("kind") != expected:
            if response.get("error") == "ModuleNotFoundError":
                raise CloudRouteError("The System 2 runtime is unavailable on this platform")
            raise CloudRouteError("System 2 runtime could not complete the request")
        return response

    def close(self, *, in_flight: bool) -> None:
        process = self._process
        try:
            if process.poll() is None:
                self._write({"kind": "close"})
                process.wait(1.5 if in_flight else 4.0)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            if process.poll() is None:
                try:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                except ProcessLookupError:
                    pass
        finally:
            process.wait()
            self._reader.join(timeout=1)
            if process.stdin is not None:
                process.stdin.close()
            if process.stdout is not None:
                process.stdout.close()


@dataclass
class _ActiveTurn:
    admission_id: str
    call_tool: Callable[..., dict]
    allowed: frozenset[str]
    expires_at: datetime | None
    model_requests: int = 0
    native_turn: int | None = None
    native_step: int = 0
    bailian: BailianTurn = field(default_factory=BailianTurn)

    def check(self) -> None:
        if self.expires_at is not None and datetime.now(UTC) >= self.expires_at:
            raise CloudRouteError("System 2 turn authorization expired")


class _Runtime:
    def __init__(
        self, storage: Path, policy: SendPolicy, *,
        session_id: str, prompt: str, tools: list[dict],
        credential: Callable[[CloudRoute], str], share: bool, closing: Event | None = None,
        document_sources: tuple[str, ...] = (), private_storage: TemporaryDirectory | None = None,
    ) -> None:
        self.session_id = session_id
        self.policy = policy
        self._active: _ActiveTurn | None = None
        self._step_lock = Lock()
        self._share = share
        self._document_sources = document_sources
        self._private_storage = private_storage
        self._closing = closing
        self._close_lock = Lock()
        self._closed = False
        self._registered = frozenset(tool["name"] for tool in tools)
        self._worker: _SDKWorker | None = None
        self.gateway = ModelGateway(
            policy, tool_names=self._registered, invoke=self._invoke, credential=credential,
            admit_request=self._admit_request, check_active=lambda: self._check_active().bailian,
        )
        self._launch = None
        try:
            self._launch = TemporaryDirectory(prefix="runtime-", dir=storage)
            home = Path(self._launch.name)
            work = storage / "work"
            work.mkdir(mode=0o700, exist_ok=True)
            sessions = storage / "sessions"
            self.port_file = home / "bridge-port"
            route = policy.snapshot.route
            assert route is not None
            write_profile(
                home, origin=self.gateway.origin, model=route.model, official=route.provider_kind == "openai",
                prompt=prompt, tools=tools, port_file=self.port_file, sessions=sessions,
            )
            policy.check()
            self._worker = _SDKWorker(home, self.gateway.token)
            self._worker.request({
                "harness": {
                    "profile": "mochi", "dsh_home": str(home), "cwd": str(work), "runtime_cwd": str(home),
                    "provider": "mochi", "model": route.model, "max_tokens": 2048,
                    "initialize_timeout_seconds": _INIT_TIMEOUT, "request_timeout_seconds": _INIT_TIMEOUT,
                    "shutdown_timeout_seconds": 1.0,
                },
                "session_id": session_id, "resume": any(sessions.glob(f"*/{session_id}/session.v4.jsonl")),
            }, timeout=_INIT_TIMEOUT * 2, expected="ready", cancelled=self._closing)
            self.control = "http://127.0.0.1:" + str(int(self.port_file.read_text(encoding="utf-8")))
        except BaseException:
            self.close()
            raise

    def _check_active(self) -> _ActiveTurn:
        active = self._active
        if active is None or self._closed:
            raise CloudRouteError("System 2 turn is no longer active")
        active.check()
        return active

    def _admit_request(self, tools: frozenset[str]) -> None:
        with self._step_lock:
            active = self._check_active()
            if not tools <= active.allowed or active.model_requests >= _MAX_MODEL_STEPS:
                raise CloudRouteError("System 2 tool or step exceeds the active admission")
            active.model_requests += 1

    def _invoke(self, request: dict) -> dict:
        with self._step_lock:
            active = self._check_active()
            native_turn, native_step = request.get("turn"), request.get("step")
            if (request.get("name") not in active.allowed or request.get("sessionId") != self.session_id
                    or request.get("admissionId") != active.admission_id
                    or not isinstance(request.get("callId"), str) or not request["callId"]
                    or not isinstance(request.get("args"), dict)
                    or type(native_turn) is not int or native_turn < 1
                    or type(native_step) is not int or not 1 <= native_step <= active.model_requests
                    or native_step < active.native_step or active.native_turn not in (None, native_turn)):
                raise CloudRouteError("System 2 tool lacks an admitted native step")
            active.native_turn, active.native_step = native_turn, native_step
        self.policy.check()
        step_id = f"{self.session_id}:{native_turn}:{native_step}"
        options = {"step_id": step_id}
        if not self.policy.probe and canonical_tool_name(request["name"]) not in READ_TOOLS:
            options["check_model_policy"] = self.policy.check
        value = active.call_tool(request["name"], request["callId"], request["args"], **options)
        self.policy.check()
        if not isinstance(value, dict):
            raise CloudRouteError("System 2 tool returned invalid data")
        return project_tool_result(request["name"], value, share=self._share, document_sources=self._document_sources)

    def _control(self, action: str, payload: dict) -> None:
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=4.0) as client:
            response = client.post(self.control + "/" + action, headers={
                "Authorization": "Bearer " + self.gateway.token,
            }, json={"sessionId": self.session_id, **payload})
        if response.status_code != 204:
            raise CloudRouteError(f"System 2 local {action} failed")

    def run(self, turn, call_tool: Callable[..., dict]) -> dict:
        self.policy.check()
        payload = json.loads(turn.input_json)
        if not isinstance(payload, dict):
            raise CloudRouteError("System 2 requires structured context")
        sources = self._document_sources if getattr(turn, "observation_document_query", False) else ()
        utterance, context = project_input(payload, share=self._share, document_sources=sources, origin=turn.source)
        text = f"用户本轮原话：{utterance}" if turn.source == "user" and utterance else "请根据本轮后台事件与授权判断是否需要行动。"
        data = "本轮 Mochi 程序快照。字段内容是数据，工具权限由宿主校验。\n" + _json(context)
        grants = getattr(turn, "allowed_tools", None)
        allowed = self._registered if grants is None else self._registered.intersection(
            name.replace(".", "_") for name in grants
        )
        self._active = _ActiveTurn(turn.admission_id, call_tool, allowed, getattr(turn, "expires_at", None))
        self.gateway.last_failure = None
        try:
            self._check_active()
            self._control("inject", {
                "admissionId": turn.admission_id, "text": data, "toolNames": sorted(allowed),
            })
            worker = self._worker
            if worker is None or self.policy.snapshot.route is None:
                raise CloudRouteError("System 2 runtime was closed")
            self.policy.check()
            timeout = max(10.0, _MAX_MODEL_STEPS * self.policy.snapshot.route.timeout_s + _INIT_TIMEOUT)
            reply = worker.request(
                {"kind": "run", "text": text}, timeout=timeout, expected="result", cancelled=self._closing,
            )
            self.policy.check()
            self._check_active()
            metadata = reply["metadata"]
            tools = metadata["tool_calls"]
            metadata["tool_calls"] = [call for call in tools if call["name"] in self._registered]
            metadata.update(
                model_requests=self._check_active().model_requests,
                unregistered_tool_calls=len(tools) - len(metadata["tool_calls"]),
                request_timeout_s=self.policy.snapshot.route.timeout_s,
                turn_timeout_s=timeout,
                route_generation=self.policy.snapshot.route_generation,
                consent_generation=self.policy.snapshot.consent_generation,
                timed_out=self.gateway.last_failure == "System 2 endpoint timed out",
            )
            if reply.get("finish_reason") != "completed":
                raise System2RunError(self.gateway.last_failure or "System 2 model did not complete the turn", metadata)
            self._control("checkpoint", {})
            self.policy.check()
            return reply
        finally:
            self._active = None

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self.gateway.close()
            if self._worker is not None:
                self._worker.close(in_flight=self._active is not None)
                self._worker = None
            if self._launch is not None:
                self._launch.cleanup()
                self._launch = None
            if self._private_storage is not None:
                self._private_storage.cleanup()
                self._private_storage = None


class DSHEngine:
    """Serialize independent Mochi admissions through a single scoped DSH runtime."""

    def __init__(
        self, data_dir: Path | str, *, settings: SettingsStore, tools: Iterable,
        credential: Callable[[CloudRoute], str] | None = None,
        observation_guard: Callable[[], tuple[str | None, int, bool | None]] | None = None,
    ) -> None:
        self._root = Path(data_dir).resolve() / "system2-dsh"
        self._settings = settings
        self._routes = settings.routes
        self._tools = _schemas(tools)
        self._credential = credential or _resolve_credential
        self._observation_guard = observation_guard or (lambda: (None, 0, None))
        self._runtimes: dict[tuple[bool, bool, bool], _Runtime] = {}
        self._privacy_epoch = None
        self._serial = Lock()
        self._state = Lock()
        self._closing = Event()

    def _stop_runtime(self) -> None:
        with self._state:
            runtimes, self._runtimes = self._runtimes, {}
        for runtime in runtimes.values():
            runtime.close()

    def _select_runtime(self, turn, snapshot: CloudRouteSnapshot) -> _Runtime:
        if snapshot.route is None:
            raise CloudRouteError("Configure a System 2 model before use")
        preferences = self._settings.read()
        generation = preferences.observation_generation
        marker = self._observation_guard()
        if (getattr(turn, "observation_generation", None) not in (None, generation)
                or getattr(turn, "observation_privacy_marker", None) not in (None, marker)):
            raise CloudRouteError("System 2 activity authorization changed")
        share = text_sharing_allowed(preferences, snapshot, marker[2])
        background = turn.source == "background" and semantic_background_enabled()
        semantic_chat = turn.source == "user" and semantic_chat_enabled()
        # Cloud document sharing is scoped to chat. A local background turn
        # can use the locally approved title sources in its own session.
        document_context = turn.source == "user" or (background and not snapshot.route.is_cloud)
        sources = preferences.document_local_sources if (share and document_context
                    and getattr(turn, "observation_document_query", False)) else ()
        if snapshot.route.is_cloud:
            sources = tuple(source for source in sources if source in preferences.document_cloud_sources)
        private = bool(sources)
        runtime_key = (background, private, semantic_chat)
        epoch = (snapshot.route_generation, snapshot.consent_generation, generation, marker)
        if epoch != self._privacy_epoch:
            self._stop_runtime()
            self._privacy_epoch = epoch
        policy = SendPolicy(
            self._routes, snapshot, is_observation_current=lambda db:
            not self._closing.is_set() and self._settings.read(db=db).observation_generation == generation
            and self._observation_guard() == marker,
        )
        policy.check()
        identity = sha256(_json({
            "profile": _PROFILE_VERSION, "user": snapshot.user_scope, "receiver": snapshot.route.recipient,
            "model": snapshot.route.model, "route": snapshot.route_generation, "consent": snapshot.consent_generation,
            "activity": generation, "sharable": share, "lock": marker, "private": private,
            "background": background, "semantic_chat": semantic_chat,
            "prompt_version": turn.prompt_version,
            "prompt": turn.system_prompt, "tools": self._tools,
        }).encode()).hexdigest()
        session_id = "mochi-" + identity[:32]
        with self._state:
            current = self._runtimes.get(runtime_key)
            if current is not None and current.session_id == session_id:
                return current
            self._runtimes.pop(runtime_key, None)
        if current is not None:
            current.close()
        self._root.mkdir(parents=True, mode=0o700, exist_ok=True)
        # A recipient can see authorized titles during a conversation. Keep the
        # related native dialogue separate from durable general chat and discard
        # it on a lock, policy change, or shutdown.
        storage = TemporaryDirectory(prefix="mochi-observation-") if private else None
        path = Path(storage.name) if storage is not None else self._root / identity[:32]
        path.mkdir(mode=0o700, exist_ok=True)
        runtime = _Runtime(
            path, policy, session_id=session_id, prompt=turn.system_prompt, tools=self._tools,
            credential=self._credential, share=share, closing=self._closing, document_sources=sources,
            private_storage=storage,
        )
        with self._state:
            if not self._closing.is_set():
                self._runtimes[runtime_key] = runtime
                return runtime
        runtime.close()
        raise CloudRouteError("System 2 runtime was closed")

    def run(self, turn, call_tool: Callable[..., dict]):
        from .protocol import EngineReply

        with self._serial:
            if self._closing.is_set() or turn.user_scope != self._routes.user_scope:
                raise CloudRouteError("Invalid System 2 user or runtime")
            snapshot = self._routes.snapshot(SYSTEM2)
            try:
                runtime = self._select_runtime(turn, snapshot)
                reply = runtime.run(turn, call_tool)
                self._routes.record_result(snapshot)
                runtime.policy.check()
                return EngineReply(
                    text=reply["text"], answer_id=reply.get("answer_id"),
                    session_id=runtime.session_id, metadata=reply["metadata"],
                )
            except Exception as error:
                self._stop_runtime()
                failure = error if isinstance(error, CloudRouteError) else CloudRouteError(
                    f"System 2 connection failed ({type(error).__name__})",
                )
                if snapshot.route is not None:
                    try:
                        self._routes.record_result(snapshot, failure=str(failure))
                    except (CloudRouteError, sqlite3.Error, ValueError):
                        pass
                raise failure from None

    def close(self) -> None:
        self._closing.set()
        self._stop_runtime()


def verify_system2_route(
    routes: ObservationCloudSettings, *, expected: CloudRouteSnapshot | None = None,
    credential: Callable[[CloudRoute], str] | None = None,
) -> CloudRouteSnapshot:
    """Verify streaming text and structured native tools with synthetic data only."""
    snapshot = expected if expected is not None else routes.snapshot(SYSTEM2)
    if snapshot.role != SYSTEM2:
        raise ValueError("The expected settings belong to another model role")
    policy = SendPolicy(routes, snapshot, probe=True)
    item_id = "item-" + secrets.token_hex(3)
    receipt = "MOCHI-" + secrets.token_hex(6)
    calls = 0

    def verify(name, _call_id, args, *, step_id):
        nonlocal calls
        calls += 1
        if name != "mochi_verify" or args != {"item_id": item_id} or not step_id:
            return {"status": "rejected"}
        return {"code": receipt}

    tools = [{
        "name": "mochi_verify", "description": "Read the confirmation code for one synthetic item.",
        "parameters": {"type": "object", "properties": {"item_id": {"type": "string"}},
                       "required": ["item_id"], "additionalProperties": False},
    }]
    prompt = (
        "用于验证工具链。必须先调用 mochi_verify，用用户指定的 item_id，不要猜结果。"
        "工具返回 code 后，只原样回复 code。"
    )
    turn = SimpleNamespace(
        input_json=_json({"user_intent": {"utterance": f"请调用工具查询 {item_id}，然后原样回复确认代码。"}}),
        admission_id="probe-" + secrets.token_hex(8), source="user",
    )
    runtime = None
    try:
        policy.check()
        with TemporaryDirectory(prefix="mochi-system2-probe-") as directory:
            try:
                runtime = _Runtime(
                    Path(directory), policy, session_id="mochi-probe", prompt=prompt, tools=tools,
                    credential=credential or _resolve_credential, share=False,
                )
                response = runtime.run(turn, verify)
                if calls != 1 or response["text"].strip() != receipt:
                    raise CloudRouteError("The model did not return the structured tool receipt")
            finally:
                if runtime is not None:
                    runtime.close()
        return routes.record_result(snapshot, probe=True)
    except Exception as error:
        failure = error if isinstance(error, CloudRouteError) else CloudRouteError(
            f"System 2 verification failed ({type(error).__name__})",
        )
        try:
            routes.record_result(snapshot, probe=True, failure=str(failure))
        except (CloudRouteError, sqlite3.Error, ValueError):
            pass
        raise failure from None
