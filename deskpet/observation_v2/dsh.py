"""Run a single image through an isolated DeepSeek Harness model turn."""

from __future__ import annotations

import base64
from collections.abc import Callable
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
from io import BytesIO
import json
from pathlib import Path
import secrets
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread
from time import monotonic
from uuid import uuid4

from PIL import Image

from deskpet.dsh_process import SDKWorker
from deskpet.dsh_profile import write_observation_profile

from .routes import CloudRouteError

_BOOT_TIMEOUT = 10.0
_MAX_IMAGE_BYTES = 32 * 1024 * 1024
_MAX_REPLY_BYTES = 256 * 1024
_IMAGE_URL = "data:image/jpeg;base64,"


class _VisionGateway:
    """Give one native DSH model request access to the host's scoped transport."""

    def __init__(
        self, *, model: str, system: str, prompt: str, schema: dict, image_digest: bytes,
        infer: Callable[..., dict], cancelled: Callable[[], bool], check_send: Callable[[], None],
    ) -> None:
        self.token = secrets.token_urlsafe(32)
        self.failure: CloudRouteError | None = None
        self.used = False
        lock = Lock()
        token = ("Bearer " + self.token).encode()
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                self.request.settimeout(4)
                super().setup()

            def log_message(self, *_):
                pass

            def do_POST(self):
                authorization = self.headers.get("Authorization", "").encode()
                if self.path != "/v1/chat/completions" or not hmac.compare_digest(authorization, token):
                    self.send_error(403)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= (4 * _MAX_IMAGE_BYTES // 3) + 65536 or self.headers.get("Transfer-Encoding"):
                        raise CloudRouteError("Invalid Observation runtime request size")
                    body = json.loads(self.rfile.read(length))
                    image = gateway._image(body, model, system, prompt)
                    if sha256(image).digest() != image_digest:
                        raise CloudRouteError("Observation runtime did not supply the captured image")
                    with lock:
                        if gateway.used:
                            raise CloudRouteError("Observation runtime exceeded its single request")
                        check_send()
                        gateway.used = True
                    result = infer(
                        system=body["messages"][0]["content"], prompt=body["messages"][1]["content"][0]["text"],
                        schema=schema, image=image, cancelled=cancelled,
                    )
                    if cancelled():
                        raise CloudRouteError("Observation runtime was closed")
                    response = json.dumps(result, ensure_ascii=False, allow_nan=False)
                    if not isinstance(result, dict) or len(response.encode()) > _MAX_REPLY_BYTES:
                        raise CloudRouteError("Invalid Observation model response")
                    data = gateway._stream(model, response)
                except Exception as error:
                    gateway.failure = CloudRouteError(
                        str(error) if isinstance(error, CloudRouteError)
                        else f"Observation model request failed ({type(error).__name__})"
                    )
                    self.send_error(502, "Observation request failed")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        class Server(ThreadingHTTPServer):
            def handle_error(self, *_):
                pass

        self._server = Server(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self._server.server_port}"
        self._thread = Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True, name="mochi-observation-dsh-model",
        )
        self._thread.start()

    @staticmethod
    def _image(body: object, model: str, system: str, prompt: str) -> bytes:
        if (not isinstance(body, dict) or body.get("model") != model or body.get("stream") is not True
                or body.get("tools") not in (None, []) or body.get("tool_choice") not in (None, "none")):
            raise CloudRouteError("Unexpected Observation runtime model request")
        messages = body.get("messages")
        if (not isinstance(messages, list) or len(messages) != 2
                or messages[0] != {"role": "system", "content": system}
                or not isinstance(messages[1], dict) or messages[1].keys() != {"role", "content"}
                or messages[1]["role"] != "user"):
            raise CloudRouteError("Unexpected Observation runtime message")
        content = messages[1]["content"]
        if (not isinstance(content, list) or len(content) != 3
                or content[0] != {"type": "text", "text": prompt}
                or not isinstance(content[1], dict) or content[1].get("type") != "text"
                or not isinstance(content[2], dict) or content[2].get("type") != "image_url"
                or not isinstance(content[2].get("image_url"), dict)):
            raise CloudRouteError("The Observation runtime requires a single JPEG")
        url = content[2]["image_url"].get("url")
        if not isinstance(url, str) or not url.startswith(_IMAGE_URL):
            raise CloudRouteError("The Observation runtime requires a single JPEG")
        return base64.b64decode(url[len(_IMAGE_URL):], validate=True)

    @staticmethod
    def _stream(model: str, text: str) -> bytes:
        chunks = [
            {"delta": {"role": "assistant"}, "finish_reason": None},
            {"delta": {"content": text}, "finish_reason": None},
            {"delta": {}, "finish_reason": "stop"},
        ]
        frames = [{
            "id": "observation-capture", "object": "chat.completion.chunk", "created": 1, "model": model,
            "choices": [{"index": 0, **chunk}],
        } for chunk in chunks]
        return "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames).encode() + b"data: [DONE]\n\n"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=1)


class ObservationDSH:
    """Own one fresh, tool-free, in-memory session for each desktop frame."""

    def __init__(self) -> None:
        self._closed = Event()
        self._round = Lock()

    def close(self) -> None:
        self._closed.set()

    def complete(
        self, *, model: str, system: str, prompt: str, schema: dict, image: bytes,
        infer: Callable[..., dict], timeout: float, send_permitted: Callable[[], bool] | None = None,
        deadline_s: float | None = None,
    ) -> dict:
        finished = Event()

        def cancelled() -> bool:
            return self._closed.is_set() or finished.is_set()

        def check_send() -> None:
            if (cancelled() or (deadline_s is not None and monotonic() >= deadline_s)
                    or (send_permitted is not None and not send_permitted())):
                raise CloudRouteError("Observation capture is no longer eligible for inference")

        check_send()
        if not self._round.acquire(blocking=False):
            raise CloudRouteError("Observation runtime already has an active capture")
        try:
            return self._complete(
                model=model, system=system, prompt=prompt, schema=schema, image=image, infer=infer,
                timeout=timeout, cancelled=cancelled, finished=finished, check_send=check_send,
                boot_timeout=_BOOT_TIMEOUT if deadline_s is None else min(_BOOT_TIMEOUT, deadline_s - monotonic()),
            )
        finally:
            self._round.release()

    def _complete(
        self, *, model: str, system: str, prompt: str, schema: dict, image: bytes, infer: Callable[..., dict],
        timeout: float, cancelled: Callable[[], bool], finished: Event, check_send: Callable[[], None],
        boot_timeout: float,
    ) -> dict:
        if not 0 < len(image) <= _MAX_IMAGE_BYTES:
            raise CloudRouteError("Observation capture exceeds its frame size limit")
        try:
            with Image.open(BytesIO(image), formats=("JPEG",)) as jpeg:
                width, height = jpeg.size
        except (OSError, ValueError, Image.DecompressionBombError):
            raise CloudRouteError("Invalid Observation JPEG") from None
        with TemporaryDirectory(prefix="mochi-observation-dsh-") as directory:
            home = Path(directory)
            work = home / "work"
            work.mkdir(mode=0o700)
            gateway = _VisionGateway(
                model=model, system=system, prompt=prompt, schema=schema, image_digest=sha256(image).digest(),
                infer=infer, cancelled=cancelled, check_send=check_send,
            )
            worker = None
            completed = False
            try:
                write_observation_profile(
                    home, origin=gateway.origin, model=model, prompt=system, width=width, height=height,
                    max_bytes=_MAX_IMAGE_BYTES,
                )
                worker = SDKWorker(home, gateway.token, label="Observation DSH")
                worker.request({
                    "harness": {
                        "profile": "mochi-observation", "dsh_home": str(home), "cwd": str(work),
                        "runtime_cwd": str(home), "provider": "mochi", "model": model, "max_tokens": 512,
                        "initialize_timeout_seconds": _BOOT_TIMEOUT, "request_timeout_seconds": _BOOT_TIMEOUT,
                        "shutdown_timeout_seconds": 0.5,
                    },
                    "session_id": "observation-" + uuid4().hex, "resume": False,
                }, timeout=boot_timeout, expected="ready", cancelled=self._closed)
                check_send()
                reply = worker.request({
                    "kind": "run", "input": [
                        {"type": "text", "text": prompt},
                        {"type": "image", "data": base64.b64encode(image).decode("ascii"), "mimeType": "image/jpeg"},
                    ],
                }, timeout=timeout + 2.0, expected="result", cancelled=self._closed)
                completed = True
                if cancelled():
                    raise CloudRouteError("Observation runtime was closed")
                if gateway.failure is not None:
                    raise gateway.failure
                if not gateway.used or reply.get("finish_reason") != "completed" or reply["metadata"]["tool_calls"]:
                    raise CloudRouteError("Observation did not complete a tool-free native model turn")
                result = json.loads(reply["text"])
                if not isinstance(result, dict):
                    raise CloudRouteError("Invalid Observation model response")
                return result
            finally:
                finished.set()
                gateway.close()
                if worker is not None:
                    worker.close(in_flight=not completed, abort=not completed)
