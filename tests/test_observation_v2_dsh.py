"""Wire regressions for the native tool-free Observation runtime."""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
from io import BytesIO
import json
import socket
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic

from PIL import Image
import pytest

from deskpet.observation_v2 import dsh
from deskpet.observation_v2.ollama import OllamaClient
from deskpet.observation_v2.openai_cloud import RoutedObservationModel
from deskpet.observation_v2.routes import VISION, CloudRoute, CloudRouteError, ObservationCloudSettings

SCHEMA = {"type": "object", "properties": {"screen_kind": {"type": "string"}}}
RESULT = {"screen_kind": "document"}
SYSTEM = "Identify visible document UI."
PROMPT = "Classify this frame."


@pytest.fixture
def frame():
    if importlib.util.find_spec("deepseek_harness") is None:
        pytest.skip("Pinned DSH SDK is unavailable")
    with closing(socket.socket()) as sock:
        try:
            sock.bind(("127.0.0.1", 0))
        except PermissionError:
            pytest.skip("Loopback bind is unavailable")
    with BytesIO() as buffer:
        Image.new("RGB", (24, 16), "#ff3680").save(buffer, format="JPEG")
        return buffer.getvalue()


def test_native_vision_passes_the_jpeg_without_session_or_attachment_files(monkeypatch, tmp_path, frame):
    monkeypatch.setattr(dsh, "TemporaryDirectory", partial(TemporaryDirectory, dir=tmp_path))
    runtime = dsh.ObservationDSH()
    runtime_directories = []

    def infer(*, system, prompt, schema, image, cancelled):
        assert (system, prompt, schema, image) == (SYSTEM, PROMPT, SCHEMA, frame)
        assert not cancelled()
        (home,) = tmp_path.iterdir()
        runtime_directories.append(home.name)
        profile = home / "profiles" / "mochi-observation" / "cordis.patch.yml"
        plugins = {entry["id"] for entry in json.loads(profile.read_text())[0]["insert"]}
        assert "mochi-observation-image" in plugins
        assert "mochi-bridge" not in plugins
        assert "session-persistence-jsonl" not in plugins
        for path in home.rglob("*"):
            if path.is_file():
                assert path.suffix not in {".jpg", ".jpeg", ".jsonl"}
                data = path.read_bytes()
                assert frame not in data
                assert base64.b64encode(frame) not in data
        return RESULT

    for _ in range(2):
        assert runtime.complete(
            model="test-vision", system=SYSTEM, prompt=PROMPT, schema=SCHEMA, image=frame,
            infer=infer, timeout=3, deadline_s=monotonic() + 15,
        ) == RESULT
        assert list(tmp_path.iterdir()) == []
    assert len(set(runtime_directories)) == 2


def test_send_permission_change_during_runtime_start_keeps_the_frame_local(frame):
    runtime = dsh.ObservationDSH()
    checked_before_start = Event()

    def permit():
        if checked_before_start.is_set():
            return False
        checked_before_start.set()
        return True

    def infer(**_):
        pytest.fail("An expired capture reached the model")

    with pytest.raises(CloudRouteError, match="no longer eligible"):
        runtime.complete(
            model="test-vision", system=SYSTEM, prompt=PROMPT, schema=SCHEMA, image=frame,
            infer=infer, timeout=3, send_permitted=permit, deadline_s=monotonic() + 15,
        )


def test_capture_consent_is_checked_after_native_completion(tmp_path):
    settings = ObservationCloudSettings(tmp_path / "routes.db")
    route = CloudRoute("openai_compatible", "http://127.0.0.1:7979/v1", "test-vision", "Vision fixture")
    snapshot = settings.configure(VISION, route)
    snapshot = settings.set_permission(VISION, True, expected=snapshot)
    snapshot = settings.record_result(snapshot, probe=True)

    class Runtime:
        def complete(self, **_):
            settings.set_permission(VISION, False, expected=snapshot)
            return RESULT

    with OllamaClient("test-local") as local:
        model = RoutedObservationModel(VISION, settings, local, vision_runtime=Runtime())
        grant = model.prepare_vision_capture(permitted=lambda: True)
        scope = model.capture_result_scope(grant)
        with model.admit_vision_result(scope) as allowed:
            assert allowed
        with pytest.raises(CloudRouteError, match="authorization changed"):
            model.complete_multimodal_image(
                grant, image=b"synthetic frame", signals=None, captured_at=datetime.now(UTC),
                activity_id=1, allow_document_title=False, scoped_snapshot=scope,
            )
        with model.admit_vision_result(scope) as allowed:
            assert not allowed


def test_closing_native_vision_interrupts_a_blocked_local_model(frame):
    started = Event()
    disconnected = Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            assert self.path == "/api/chat"
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert base64.b64decode(request["messages"][1]["images"][0]) == frame
            self.connection.settimeout(5)
            started.set()
            if self.connection.recv(1) == b"":
                disconnected.set()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    runtime = dsh.ObservationDSH()
    try:
        with OllamaClient("test-vision", endpoint=f"http://127.0.0.1:{server.server_port}", timeout=10) as local:
            model = RoutedObservationModel(VISION, None, local, timeout_limit=10, vision_runtime=runtime)
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(model.complete, system=SYSTEM, prompt=PROMPT, schema=SCHEMA, image=frame)
                assert started.wait(10)
                runtime.close()
                with pytest.raises(CloudRouteError, match="runtime was closed"):
                    future.result(timeout=2)
                assert disconnected.wait(2)
    finally:
        runtime.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
