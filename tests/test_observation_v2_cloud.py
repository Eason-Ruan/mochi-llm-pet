from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import logging
import sqlite3
from threading import Event, Thread
from types import SimpleNamespace

import h11
import httpcore
from PIL import Image
import pytest

from deskpet.observation_v2.activity import ActivitySample
from deskpet.observation_v2.media import capture_window, cloud_window_eligible
from deskpet.observation_v2.openai_cloud import (
    LOCAL_CAPTURE, RoutedObservationModel, prepare_vision_capture, verify_cloud_route,
)
from deskpet.observation_v2.routes import STATE, VISION, CloudRoute, CloudRouteError, ObservationCloudSettings
from deskpet.observation_v2.usage import ObservationUsage
from deskpet.observation_v2.vision import VisionSignals, analyze_multimodal, analyze_screen


class LocalModel:
    model = "local-test"

    def __init__(self):
        self.images = []

    def complete(self, *, image=None, **_kwargs):
        self.images.append(image)
        return {"screen_kind": "ide", "screen_summary": None, "visible_text_redacted": None}


@contextmanager
def _server(monkeypatch, *, wire_only=False):
    seen = []
    responded = Event()
    release_response = Event()
    upload_started = Event()
    behavior = {"status": 200, "hold": False}

    def reply(path, authorization, raw):
        payload = json.loads(raw)
        seen.append((path, authorization, payload))
        local_ollama = path == "/api/chat"
        schema = payload["format"] if local_ollama else payload["response_format"]["json_schema"]["schema"]
        message = payload["messages"][-1]
        user = message["content"]
        if "answer" in schema["properties"]:
            if isinstance(user, list) or "images" in message:
                encoded = message["images"][0] if local_ollama else user[-1]["image_url"]["url"].split(",")[1]
                with Image.open(BytesIO(base64.b64decode(encoded))) as image:
                    red, green, blue = image.getpixel((0, 0))
                answer = "yellow" if red > 125 and green > 125 else (
                    "red" if red > 125 else "green" if green > 125 else "blue"
                )
            else:
                answer = user.rsplit(" ", 1)[-1]
            content = {"answer": answer}
        elif "screen_kind" in schema["properties"]:
            content = behavior.get("vision_result") or {
                "screen_kind": "ide", "screen_summary": "A code editor is open.", "visible_text_redacted": "Explorer",
            }
        else:
            content = {"activity_kind": "work", "state": "unknown", "summary": None, "reason_codes": []}
        if behavior["hold"]:
            responded.set()
            assert release_response.wait(timeout=5), "response release timed out"
        status = behavior["status"]
        message = {"role": "assistant", "content": json.dumps(content)}
        response = {"model": payload["model"]}
        if local_ollama:
            response["message"] = message
        else:
            response.update(id="synthetic-completion", object="chat.completion", created=1,
                            choices=[{"index": 0, "finish_reason": "stop", "message": message}])
            if "usage" in behavior:
                response["usage"] = behavior["usage"]
        body = json.dumps(response).encode()
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
        if status == 307:
            headers.append((b"location", b"/unapproved/chat/completions"))
        return status, headers, body

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            status, headers, body = reply(
                self.path, self.headers.get("Authorization"),
                self.rfile.read(int(self.headers["Content-Length"])),
            )
            self.send_response(status)
            for name, value in headers:
                self.send_header(name.decode(), value.decode())
            self.end_headers()
            self.wfile.write(body)

    httpd = None
    if not wire_only:
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        except PermissionError:
            pass
    if httpd is None:
        # A sandbox can deny socket bind. In that case mock only TCP, leaving
        # SDK, HTTPX, httpcore, trace hooks and HTTP/1.1 serialization in use.
        class Wire(httpcore.NetworkStream):
            def __init__(self):
                self.connection = h11.Connection(h11.SERVER)
                self.data = bytearray()

            def write(self, buffer, timeout=None):
                before_upload = behavior.get("before_upload")
                if before_upload is not None and not upload_started.is_set():
                    upload_started.set()
                    before_upload()
                if (writes := behavior.get("writes")) is not None:
                    writes.append(bytes(buffer))
                self.connection.receive_data(buffer)

            def read(self, max_bytes, timeout=None):
                request = self.connection.next_event()
                assert isinstance(request, h11.Request)
                event = self.connection.next_event()
                while isinstance(event, h11.Data):
                    self.data.extend(event.data)
                    event = self.connection.next_event()
                assert isinstance(event, h11.EndOfMessage)
                header = dict(request.headers).get(b"authorization")
                status, headers, body = reply(request.target.decode(), header.decode() if header else None, self.data)
                return b"".join((
                    self.connection.send(h11.Response(status_code=status, headers=headers)),
                    self.connection.send(h11.Data(data=body)),
                    self.connection.send(h11.EndOfMessage()),
                ))

            def close(self):
                pass

        class SyntheticNetwork(httpcore.NetworkBackend):
            def connect_tcp(self, **_kwargs):
                if (before_connect := behavior.get("before_connect")) is not None:
                    before_connect()
                return Wire()

        with monkeypatch.context() as patch:
            patch.setattr("httpcore._sync.connection_pool.SyncBackend", SyntheticNetwork)
            try:
                yield "http://127.0.0.1:1", seen, behavior, responded, release_response
            finally:
                release_response.set()
        return

    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", seen, behavior, responded, release_response
    finally:
        release_response.set()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _route(base, model="test", credential_ref=None):
    return CloudRoute("openai_compatible", base, model, "Synthetic fixture", credential_ref, timeout_s=2)


def _state_request(model, window):
    return model.complete(system="OS_TITLE_PRIVATE_SYSTEM_MARKER", prompt=json.dumps(window), schema={})


def test_cloud_state_projects_only_typed_inputs_and_explicitly_approved_text(tmp_path, caplog, monkeypatch):
    with _server(monkeypatch) as (base, seen, _, _, _):
        settings = ObservationCloudSettings(tmp_path / "settings.db")
        route = _route(base + "/state/v1")
        configured = settings.configure(STATE, route)
        model = RoutedObservationModel(STATE, settings, LocalModel())
        window = {
            "current_foreground": {"app_name": "UNRESTRICTED_OS_APP", "window_title_redacted": "OS_TITLE_MARKER",
                                   "domain": "private-domain.example"},
            "previous_foreground": {"url": "https://private-domain.example/secret-path"},
            "screen_locked": False, "input_recovered": True, "idle_s": 3,
            "current_screen": {"status": "ready", "screen_kind": "ide", "captured_at": "2000-01-01T00:00:00Z",
                               "screen_summary": "A code editor is open.", "visible_text_redacted": "Explorer | Output"},
            "recent_screen_results": [
                {"status": "ready", "screen_kind": "document",
                 "screen_summary": "林澄测试名 is reading a private message from Ada Morrow",
                 "visible_text_redacted": "Don't tell anyone about our private synthetic chat"},
            ],
            "observation_image": "data:image/jpeg;base64,EXCLUDED_ADJACENT_WINDOW",
            "free_text": "private custom context",
        }
        with pytest.raises(CloudRouteError, match="Verify"):
            _state_request(model, window)
        assert not seen
        verify_cloud_route(settings, STATE)
        caplog.set_level(logging.DEBUG, logger="openai")
        _state_request(model, window)
        request = seen[-1][-1]
        projected = json.loads(request["messages"][-1]["content"])
        assert projected["screen_locked"] is False
        assert projected["input_recovered"] is True
        assert projected["idle_s"] == 3
        assert projected["current_foreground"] == {"present": True}
        assert projected["current_screen"]["screen_kind"] == "ide"
        assert "visible_text_redacted" not in projected["current_screen"]
        assert "screen_summary" not in projected["recent_screen_results"][0]
        settings.set_permission(STATE, True, expected=configured)
        _state_request(model, window)
        request = seen[-1][-1]
        projected = json.loads(request["messages"][-1]["content"])
        assert projected["current_screen"]["visible_text_redacted"] == "Explorer, Output"
        assert projected["current_screen"]["screen_summary"] == "A code editor is open."
        assert projected["recent_screen_results"][0]["visible_text_redacted"] is None
        assert projected["recent_screen_results"][0]["screen_summary"] is None
        for marker in ("OS_TITLE", "UNRESTRICTED", "private-domain", "secret-path", "Ada Morrow",
                       "林澄测试名", "private synthetic chat", "private custom", "ADJACENT_WINDOW", "image_url"):
            assert all(marker not in json.dumps(body, ensure_ascii=False) for _, _, body in seen)
            assert marker not in caplog.text
        assert all(path == "/state/v1/chat/completions" and auth is None for path, auth, _ in seen)
        assert not settings.snapshot(VISION).permission


def test_vision_consent_scope_recipient_switch_and_recording_isolation(tmp_path, monkeypatch):
    with _server(monkeypatch) as (base, seen, behavior, _, _):
        settings = ObservationCloudSettings(tmp_path / "settings.db")
        configured = settings.configure(VISION, _route(base + "/vision/v1"))
        local = LocalModel()
        model = RoutedObservationModel(VISION, settings, local)
        assert prepare_vision_capture(model) is None
        with pytest.raises(CloudRouteError, match="permission"):
            verify_cloud_route(settings, VISION)
        assert not seen
        settings.set_permission(VISION, True, expected=configured)
        verify_cloud_route(settings, VISION)
        screen_permission = True
        grant = prepare_vision_capture(model, permitted=lambda: screen_permission)
        assert grant is not None and grant is not LOCAL_CAPTURE

        sample = ActivitySample(
            datetime.now(UTC), 1, "Chrome", 93, "Editor", None,
            screen_locked=False, session_id="test-user", private_window=False,
        )
        assert not cloud_window_eligible(sample, browser_url_required=True, platform="darwin")
        assert not cloud_window_eligible(replace(sample, private_window=None), browser_url_required=False, platform="darwin")
        assert not cloud_window_eligible(replace(sample, screen_locked=None), browser_url_required=False, platform="darwin")
        assert not cloud_window_eligible(sample, browser_url_required=False, platform="linux")
        with pytest.raises(RuntimeError):
            capture_window(replace(sample, window_id=None), platform="darwin")
        calls = []
        def render_window(window_id):
            calls.append(window_id)
            return Image.new("RGB", (32, 16), "orange")

        monkeypatch.setattr("deskpet.observation_v2.media._mac_window", render_window)
        captured_at, jpeg = capture_window(sample, platform="darwin")
        assert calls == [93]
        screen_permission = False
        denied = analyze_screen(model, image=jpeg, image_grant=grant, capture_id="a", activity_id=1,
                                captured_at=captured_at)
        assert denied.status == "failed"
        assert len(seen) == 1
        screen_permission = True
        behavior["vision_result"] = {
            "screen_kind": "ide",
            "visible_text_redacted": "Remember to call 林澄测试名 about the private results",
            "screen_summary": "This is a private synthetic message from Ada Morrow.",
        }
        grant = prepare_vision_capture(model, permitted=lambda: screen_permission)
        allowed = analyze_screen(model, image=jpeg, image_grant=grant, capture_id="b", activity_id=1,
                                 captured_at=captured_at)
        assert allowed.status == "ready" and allowed.screen_kind == "ide"
        assert allowed.visible_text_redacted is None and allowed.screen_summary is None
        sent_image = seen[-1][-1]["messages"][-1]["content"][1]["image_url"]["url"]
        assert base64.b64decode(sent_image.split(",")[1]) == jpeg

        settings.configure(VISION, _route(base + "/different/v1"), expected=settings.snapshot(VISION))
        with pytest.raises(CloudRouteError, match="changed"):
            model.complete_image(grant, image=jpeg)
        assert prepare_vision_capture(model) is None
        assert len(seen) == 2
        recording = RoutedObservationModel(VISION, settings, local, local_vision_only=True)
        result = analyze_screen(recording, image=jpeg, capture_id="local", activity_id=1, captured_at=captured_at)
        assert result.status == "ready" and local.images == [jpeg] and len(seen) == 2


@pytest.mark.parametrize("permission_revoked", [False, True])
def test_cloud_multimodal_grant_admits_one_combined_call_and_checks_late_permission(
    tmp_path, monkeypatch, permission_revoked,
):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr("deskpet.observation_v2.openai_cloud.time", SimpleNamespace(monotonic=lambda: clock.now))
    with _server(monkeypatch) as (base, seen, behavior, responded, release_response):
        settings = ObservationCloudSettings(tmp_path / "settings.db")
        configured = settings.configure(VISION, _route(base + "/vision/v1", model="qwen3.7-flash"))
        image_permission = settings.set_permission(VISION, True, expected=configured)
        settings.set_vision_signals_permission(True, expected=image_permission)
        probe_usage, usage = [], []
        verify_cloud_route(settings, VISION, usage_observer=probe_usage.append)
        local = LocalModel()
        model = RoutedObservationModel(VISION, settings, local, usage_observer=usage.append)
        scene_eligible = True
        grant = replace(prepare_vision_capture(model), send_permitted=lambda: scene_eligible)
        captured_at = datetime.now(UTC)
        behavior.update(hold=True, usage={"prompt_tokens": 20, "completion_tokens": 0}, vision_result={
            "screen_kind": "document", "activity_kind": "work", "scene_elements": ["document table"],
            "next_check": "check_soon", "unapproved_output": "PRIVATE TEXT",
        })
        jpeg = b"synthetic-frame"

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                analyze_multimodal, model, image=jpeg, image_grant=grant, capture_id="a", activity_id=1,
                captured_at=captured_at, signals=VisionSignals(
                    sampled_at=captured_at - timedelta(seconds=1), foreground_run_s=3.0, screen_locked=False,
                ),
            )
            assert responded.wait(timeout=5)
            clock.now = grant.expires_at + 1
            scene_eligible = False
            if permission_revoked:
                settings.set_permission(VISION, False, expected=settings.snapshot(VISION))
            release_response.set()
            result = future.result(timeout=5)

        assert local.images == []
        assert probe_usage == [ObservationUsage("qwen3.7-flash", None, None, None)]
        assert len(seen) == 2  # Capability check plus the single combined request.
        _, _, wire = seen[-1]
        content = wire["messages"][-1]["content"]
        assert base64.b64decode(content[1]["image_url"]["url"].split(",")[-1]) == jpeg
        assert json.loads(content[0]["text"])["system_signals"]["foreground_run_s"] == 3.0
        assert "document_title" not in wire["response_format"]["json_schema"]["schema"]["properties"]
        assert not settings.snapshot(STATE).permission
        if permission_revoked:
            assert result.status == "failed" and usage == []
        else:
            assert result.status == "ready" and result.scene_elements == ("document table",)
            assert set(result.filtered_fields) == {"unexpected_fields", "visible_changes"}
            assert usage == [ObservationUsage("qwen3.7-flash", 20, 0, None)]
        assert "PRIVATE TEXT" not in repr(result)


@pytest.mark.parametrize("move_when", ["before_connect", "before_upload"])
def test_cloud_multimodal_window_change_during_transport_admission_blocks_image_body(tmp_path, monkeypatch, move_when):
    with _server(monkeypatch, wire_only=True) as (base, seen, behavior, _, _):
        settings = ObservationCloudSettings(tmp_path / "settings.db")
        configured = settings.configure(VISION, _route(base + "/vision/v1"))
        settings.set_permission(VISION, True, expected=configured)
        verify_cloud_route(settings, VISION)
        model = RoutedObservationModel(VISION, settings, LocalModel())
        scene_eligible = True
        grant = replace(prepare_vision_capture(model), send_permitted=lambda: scene_eligible)

        def change_window():
            nonlocal scene_eligible
            scene_eligible = False

        writes = []
        behavior.update({move_when: change_window, "writes": writes})
        image = b"synthetic-frame"
        result = analyze_multimodal(
            model, image=image, image_grant=grant, capture_id="a", activity_id=1,
            captured_at=datetime.now(UTC),
        )

        assert not scene_eligible
        assert result.status == "failed"
        assert base64.b64encode(image) not in b"".join(writes)
        assert bool(writes) is (move_when == "before_upload")
        assert len(seen) == 1  # Only the synthetic verification request reached the recipient.


def test_old_image_grant_migrates_without_expanding_payload_and_new_consent_scopes_comparisons(tmp_path, monkeypatch):
    with _server(monkeypatch) as (base, seen, behavior, _, _):
        database = tmp_path / "settings.db"
        settings = ObservationCloudSettings(database)
        configured = settings.configure(VISION, _route(base + "/vision/v1"))
        settings.set_permission(VISION, True, expected=configured)
        verify_cloud_route(settings, VISION)
        with sqlite3.connect(database) as connection:
            connection.execute("ALTER TABLE observation_cloud_routes DROP COLUMN vision_signals_permission")

        settings = ObservationCloudSettings(database)
        migrated = settings.snapshot(VISION)
        assert migrated.permission and migrated.verified and not migrated.vision_signals_permission
        model = RoutedObservationModel(VISION, settings, LocalModel())
        now = datetime.now(UTC)
        behavior["vision_result"] = {
            "screen_kind": "document", "activity_kind": "work", "scene_elements": ["page canvas"],
            "visible_changes": [], "next_check": None,
        }

        def observe(seconds, *, previous=None, grant=None):
            at = now + timedelta(seconds=seconds)
            return analyze_multimodal(
                model, image=b"synthetic-frame", image_grant=grant, capture_id=str(seconds), activity_id=1,
                captured_at=at, signals=VisionSignals(sampled_at=at, recent_os_input=True, previous=previous),
            )

        def wire_text():
            return seen[-1][-1]["messages"][-1]["content"][0]["text"]

        old_grant = prepare_vision_capture(model)
        image_only = observe(0, grant=old_grant)
        assert image_only.status == "ready"
        assert wire_text() == "Observe this frame and return the requested structured fields."

        upgraded = settings.set_vision_signals_permission(True, expected=migrated)
        assert upgraded.permission and upgraded.verified and upgraded.vision_signals_permission
        sent = len(seen)
        assert observe(1, grant=old_grant).status == "failed" and len(seen) == sent
        first = observe(2, previous=image_only, grant=prepare_vision_capture(model))
        assert first.status == "ready"
        assert json.loads(wire_text())["system_signals"]["recent_os_input"] is True
        assert "previous_scene" not in json.loads(wire_text())

        behavior["vision_result"].update(scene_elements=["page canvas", "comments panel"],
                                         visible_changes=["visible panel changed"])
        same_recipient = observe(3, previous=first, grant=prepare_vision_capture(model))
        assert same_recipient.visible_changes == ("visible panel changed",)
        assert json.loads(wire_text())["previous_scene"]["scene_elements"] == ["page canvas"]

        config = settings.configure(VISION, _route(base + "/replacement/v1"), expected=settings.snapshot(VISION))
        assert not config.permission and not config.vision_signals_permission
        with pytest.raises(CloudRouteError, match="Allow cloud window images"):
            settings.set_vision_signals_permission(True, expected=config)
        enabled = settings.set_permission(VISION, True, expected=config)
        settings.set_vision_signals_permission(True, expected=enabled)
        verify_cloud_route(settings, VISION)
        changed_recipient = observe(4, previous=first, grant=prepare_vision_capture(model))
        assert changed_recipient.status == "ready" and changed_recipient.visible_changes == ()
        assert "previous_scene" not in json.loads(wire_text())
        disabled = settings.set_permission(VISION, False, expected=settings.snapshot(VISION))
        assert not disabled.permission and not disabled.vision_signals_permission


def test_revoking_during_serialization_cleans_next_send_and_disables_retries_and_redirects(tmp_path, monkeypatch):
    with _server(monkeypatch) as (base, seen, behavior, _, _):
        settings = ObservationCloudSettings(tmp_path / "settings.db")
        configured = settings.configure(STATE, _route(base + "/state", credential_ref="fixture-key"))
        settings.set_permission(STATE, True, expected=configured)
        verify_cloud_route(settings, STATE, credential=lambda _route: "synthetic-test-key")
        window = {"current_screen": {"status": "ready", "screen_kind": "ide",
                                     "screen_summary": "A code editor is open."}}
        def revoke_then_resolve(_route):
            live = settings.snapshot(STATE)
            if live.permission:
                settings.set_permission(STATE, False, expected=live)
            return "synthetic-test-key"

        model = RoutedObservationModel(STATE, settings, LocalModel(), credential=revoke_then_resolve)
        with pytest.raises(CloudRouteError, match="authorization changed"):
            _state_request(model, window)
        assert len(seen) == 1
        behavior["status"] = 503
        with pytest.raises(CloudRouteError, match="HTTP 503"):
            _state_request(model, window)
        assert len(seen) == 2
        assert "A code editor is open." not in json.dumps(seen[-1][-1])
        assert seen[-1][1] == "Bearer synthetic-test-key"
        behavior["status"] = 307
        with pytest.raises(CloudRouteError, match="HTTP 307"):
            _state_request(model, window)
        assert [path for path, _, _ in seen] == ["/state/chat/completions"] * 3


def test_revoke_commits_while_response_pending_and_discards_old_result(tmp_path, monkeypatch):
    with _server(monkeypatch) as (base, seen, behavior, responded, release_response):
        settings = ObservationCloudSettings(tmp_path / "settings.db")
        route = settings.configure(STATE, _route(base + "/state"))
        settings.set_permission(STATE, True, expected=route)
        verify_cloud_route(settings, STATE)
        model = RoutedObservationModel(STATE, settings, LocalModel())
        behavior["hold"] = True
        with ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(_state_request, model, {})
            assert responded.wait(timeout=3)
            assert not settings.set_permission(STATE, False, expected=settings.snapshot(STATE)).permission
            release_response.set()
            with pytest.raises(CloudRouteError, match="authorization changed"):
                result.result(timeout=3)
        assert len(seen) == 2


def test_independent_loopback_ollama_state_route(tmp_path, monkeypatch):
    with _server(monkeypatch) as (base, seen, _, _, _):
        settings = ObservationCloudSettings(tmp_path / "settings.db")
        local = CloudRoute("local_ollama", base, "custom-local-state", "Ollama fixture", timeout_s=2)
        settings.configure(STATE, local)
        model = RoutedObservationModel(STATE, settings, LocalModel())
        assert verify_cloud_route(settings, STATE).verified
        from deskpet.observation_v2.state import _STATE_SCHEMA

        model.complete(system="local fixture", prompt='{"custom_local_metadata": "allowed on localhost"}',
                       schema=_STATE_SCHEMA)
        assert [path for path, _, _ in seen] == ["/api/chat", "/api/chat"]
        assert "allowed on localhost" in json.dumps(seen[-1][-1])
        assert not settings.snapshot(STATE).permission
        assert settings.snapshot(VISION).route is None
        with pytest.raises(ValueError, match="loopback"):
            CloudRoute("local_ollama", "https://fake.example", "model", "Remote")


def test_explicit_loopback_vision_route_validates_multimodal_fields_independently(tmp_path, monkeypatch):
    with _server(monkeypatch) as (base, seen, behavior, _, _):
        settings = ObservationCloudSettings(tmp_path / "settings.db")
        settings.configure(VISION, CloudRoute("local_ollama", base, "local-vision", "Ollama fixture", timeout_s=2))
        assert verify_cloud_route(settings, VISION).verified
        fallback = LocalModel()
        model = RoutedObservationModel(VISION, settings, fallback)
        assert prepare_vision_capture(model) is LOCAL_CAPTURE
        behavior["vision_result"] = {
            "screen_kind": "document", "activity_kind": "work", "scene_elements": ["pivot table"],
        }
        result = analyze_multimodal(
            model, image=b"synthetic-frame", capture_id="local", activity_id=1, captured_at=datetime.now(UTC),
        )

        assert fallback.images == []
        assert len(seen) == 2 and all(path == "/api/chat" and authorization is None for path, authorization, _ in seen)
        assert result.status == "ready" and result.scene_elements == ("pivot table",)
        assert set(result.filtered_fields) == {"visible_changes", "next_check"}


def test_upload_guard_does_not_lock_business_writes(tmp_path, monkeypatch):
    with _server(monkeypatch, wire_only=True) as (base, _, behavior, responded, release_response):
        database = tmp_path / "business.db"
        settings = ObservationCloudSettings(database)
        configured = settings.configure(STATE, _route(base + "/state"))
        settings.set_permission(STATE, True, expected=configured)
        snapshot = verify_cloud_route(settings, STATE)
        model = RoutedObservationModel(STATE, settings, LocalModel())
        upload_started = Event()
        unblock_upload = Event()
        revoke_started = Event()

        def block_upload():
            upload_started.set()
            assert unblock_upload.wait(timeout=5), "upload release timed out"

        def revoke():
            revoke_started.set()
            return settings.set_permission(STATE, False, expected=snapshot)

        behavior.update(before_upload=block_upload, hold=True)
        with ThreadPoolExecutor(max_workers=2) as executor:
            sending = executor.submit(_state_request, model, {})
            try:
                assert upload_started.wait(timeout=3)
                with sqlite3.connect(database, timeout=0.3) as business:
                    business.execute("BEGIN IMMEDIATE")
                    business.execute("CREATE TABLE reminder_fixture (id INTEGER PRIMARY KEY)")
                    business.execute("INSERT INTO reminder_fixture VALUES (1)")
                revoking = executor.submit(revoke)
                assert revoke_started.wait(timeout=3)
                assert not revoking.done()
                assert settings.snapshot(STATE).permission
                unblock_upload.set()
                assert responded.wait(timeout=3)
                assert not revoking.result(timeout=3).permission
            finally:
                unblock_upload.set()
                release_response.set()
            with pytest.raises(CloudRouteError, match="authorization changed"):
                sending.result(timeout=3)


def test_pause_policy_changes_before_state_send_are_checked_at_the_wire(tmp_path, monkeypatch):
    with _server(monkeypatch) as (base, seen, _, _, _):
        settings = ObservationCloudSettings(tmp_path / "settings.db")
        settings.configure(STATE, _route(base + "/state", credential_ref="fixture-key"))
        verify_cloud_route(settings, STATE, credential=lambda _route: "synthetic-test-key")
        generation = 1

        def pause_after_prepare(_route):
            nonlocal generation
            generation = None
            return "synthetic-test-key"

        model = RoutedObservationModel(
            STATE, settings, LocalModel(), credential=pause_after_prepare, request_policy=lambda: generation,
        )
        with pytest.raises(CloudRouteError, match="policy changed"):
            _state_request(model, {})
        assert len(seen) == 1
        with pytest.raises(CloudRouteError, match="paused"):
            _state_request(model, {})
        assert len(seen) == 1


def test_mac_window_filter_selects_exact_window_without_other_surfaces(monkeypatch):
    import sys
    from types import SimpleNamespace

    from deskpet.observation_v2.media import _mac_single_window_image

    requested = SimpleNamespace(
        windowID=lambda: 8, owningApplication=lambda: SimpleNamespace(processID=lambda: 7),
        frame=lambda: SimpleNamespace(size=SimpleNamespace(width=1200, height=800)),
    )
    other = SimpleNamespace(windowID=lambda: 9)
    filters = []
    captures = []
    content = SimpleNamespace(windows=lambda: [other, requested])
    controls = {
        f"set{field}_": (lambda value: None) for field in ("Width", "Height", "ShowsCursor", "IgnoreShadowsSingleWindow")
    }
    framework = SimpleNamespace(
        SCShareableContent=SimpleNamespace(
            getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_=(
                lambda _exclude, _visible, done: done(content, None)
            ),
        ),
        SCContentFilter=SimpleNamespace(alloc=lambda: SimpleNamespace(
            initWithDesktopIndependentWindow_=lambda window: (filters.append(window), "target filter")[1],
        )),
        SCStreamConfiguration=SimpleNamespace(alloc=lambda: SimpleNamespace(init=lambda: SimpleNamespace(**controls))),
    )
    def capture(filter_, config, done):
        captures.append((filter_, config))
        done("simulated native image", None)

    manager = SimpleNamespace(captureImageWithFilter_configuration_completionHandler_=capture)
    monkeypatch.setitem(sys.modules, "ScreenCaptureKit", framework)
    assert _mac_single_window_image(manager, 8, 7) == "simulated native image"
    assert filters == [requested]
    assert len(captures) == 1 and captures[0][0] == "target filter"
    with pytest.raises(RuntimeError, match="changed"):
        _mac_single_window_image(manager, 8, 6)
    assert len(captures) == 1
