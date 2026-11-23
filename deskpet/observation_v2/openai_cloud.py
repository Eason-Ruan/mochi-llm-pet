"""OpenAI SDK transport for independently routed Observation models."""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
import json
import logging
import math
import secrets
import sqlite3
import sys
import time

import httpcore
import httpx

from .ollama import OllamaClient
from .routes import (
    STATE, VISION, CloudRoute, CloudRouteError, CloudRouteSnapshot, ObservationCloudSettings, Role,
)
from .vision import ScreenModel, cloud_screen_text

_LOG = logging.getLogger(__name__)
LOCAL_CAPTURE = object()
_PROBE_COLORS = {"blue": (0, 0, 255), "green": (0, 170, 0), "red": (255, 0, 0), "yellow": (255, 255, 0)}
_NUMERIC_FIELDS = (
    "activity_coverage_s", "unobserved_s", "foreground_coverage_s", "input_coverage_s",
    "lock_coverage_s", "locked_s", "idle_s", "current_foreground_run_s",
    "foreground_switches", "screen_scene_switches",
)
_BOOLEAN_FIELDS = (
    "screen_locked", "sustained_activity", "recent_os_input", "recently_switched",
    "input_recovered", "recently_switched_screen_scene",
)
_SCREEN_KINDS = ("document", "ide", "meeting", "video", "game", "browser", "unknown")
_SCREEN_STATUSES = ("ready", "failed", "redacted_out")


def _keyring():
    # Use only a native encrypted storage backend, independent of plugin selection.
    if sys.platform == "darwin":
        from keyring.backends.macOS import Keyring
    elif sys.platform == "win32":
        from keyring.backends.Windows import WinVaultKeyring as Keyring
    elif sys.platform == "linux":
        from keyring.backends.SecretService import Keyring
    else:
        raise CloudRouteError("OS credential storage is unavailable on this platform")
    return Keyring()


def _credential_service(route: CloudRoute) -> str:
    recipient = "\n".join(route.recipient).encode()
    return "Mochi Observation " + sha256(recipient).hexdigest()


def store_route_credential(route: CloudRoute, key: str) -> None:
    if route.credential_ref is None or not key or any(not 33 <= ord(char) <= 126 for char in key):
        raise ValueError("A credential reference and an ASCII API key are required")
    try:
        _keyring().set_password(_credential_service(route), route.credential_ref, key)
    except Exception:
        raise CloudRouteError("Could not store the API key in OS credential storage") from None


def delete_route_credential(route: CloudRoute) -> None:
    """Remove a newly created credential after a failed settings commit."""
    if route.credential_ref:
        try:
            _keyring().delete_password(_credential_service(route), route.credential_ref)
        except Exception:
            pass


def _resolve_credential(route: CloudRoute) -> str:
    assert route.credential_ref is not None
    try:
        key = _keyring().get_password(_credential_service(route), route.credential_ref)
    except Exception:
        raise CloudRouteError("Could not read the API key from OS credential storage") from None
    if not key:
        raise CloudRouteError("No API key is saved for this recipient")
    return key


def _number(value: object) -> float | int | None:
    if type(value) is int and 0 <= value <= 1_000_000:
        return value
    if type(value) is float and math.isfinite(value) and 0 <= value <= 1_000_000:
        return value
    return None


def _screen(value: object, *, share_text: bool) -> dict | None:
    if not isinstance(value, dict) or value.get("status") not in _SCREEN_STATUSES:
        return None
    kind = value.get("screen_kind")
    status = value["status"]
    projected = {"status": status, "screen_kind": kind if kind in _SCREEN_KINDS else "unknown"}
    try:
        at = datetime.fromisoformat(value.get("captured_at", ""))
        if at.utcoffset() is not None:
            projected["captured_at"] = at.astimezone(UTC).isoformat()
    except (ValueError, TypeError, OverflowError):
        pass
    if share_text and status == "ready":
        projected["screen_summary"] = cloud_screen_text(value.get("screen_summary"), summary=True)
        projected["visible_text_redacted"] = cloud_screen_text(value.get("visible_text_redacted"))
    return projected


def project_state_window(window: dict, *, share_screen_text: bool = False) -> dict:
    """Build the complete cloud state input from typed, explicitly allowed fields."""
    projected = {
        field: _number(window.get(field)) for field in _NUMERIC_FIELDS if field in window
    }
    projected.update({
        field: value if type(value) is bool else None
        for field in _BOOLEAN_FIELDS if field in window
        for value in (window[field],)
    })
    for field in ("current_foreground", "previous_foreground"):
        projected[field] = {"present": True} if isinstance(window.get(field), dict) else None
    current = _screen(window.get("current_screen"), share_text=share_screen_text)
    screens = window.get("recent_screen_results")
    projected["current_screen"] = current
    projected["recent_screen_results"] = [
        redacted for value in screens[-3:]
        if (redacted := _screen(value, share_text=share_screen_text)) is not None
    ] if isinstance(screens, list) else []
    return projected


@dataclass(frozen=True, slots=True)
class VisionImageGrant:
    snapshot: CloudRouteSnapshot
    expires_at: float
    permitted: Callable[[], bool]

    def check(self) -> None:
        if time.monotonic() >= self.expires_at or not self.permitted():
            raise CloudRouteError("Window capture authorization expired")


def prepare_vision_capture(
    model: ScreenModel, *, permitted: Callable[[], bool] = lambda: True,
) -> VisionImageGrant | object | None:
    if not isinstance(model, RoutedObservationModel):
        return LOCAL_CAPTURE
    try:
        return model.prepare_vision_capture(permitted=permitted)
    except (CloudRouteError, sqlite3.Error, ValueError):
        return None


def _require_request_permission(permitted: Callable[[], bool] | None) -> None:
    if permitted is not None and not permitted():
        raise CloudRouteError("Desktop Observation policy changed")


class _AuthorizedTransport(httpx.BaseTransport):
    def __init__(
        self, settings: ObservationCloudSettings, snapshot: CloudRouteSnapshot,
        *, probe: bool, image_grant: VisionImageGrant | None, request_permitted: Callable[[], bool] | None,
    ) -> None:
        self._settings = settings
        self._snapshot = snapshot
        self._probe = probe
        self._image_grant = image_grant
        self._request_permitted = request_permitted
        self._http = httpx.HTTPTransport(retries=0, trust_env=False, http2=False)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        route = self._snapshot.route
        assert route is not None
        path = "/chat/completions" if route.is_cloud else "/api/chat"
        expected_url = httpx.URL(route.base_url + path)
        if request.method != "POST" or request.url != expected_url:
            raise CloudRouteError("Unexpected Observation request recipient")
        if route.credential_ref is None:
            request.headers.pop("Authorization", None)

        gate = None

        def release() -> None:
            nonlocal gate
            if gate is not None:
                admitted, gate = gate, None
                admitted.__exit__(None, None, None)

        def before_network_io(event: str, _info: dict) -> None:
            nonlocal gate
            if event == "http11.send_request_headers.started":
                admission = self._settings.admit(self._snapshot, probe=self._probe)
                admission.__enter__()
                gate = admission
                _require_request_permission(self._request_permitted)
                if self._image_grant is not None:
                    self._image_grant.check()
                _LOG.debug(
                    "Observation send admitted role=%s route_generation=%s consent_generation=%s recipient=%s",
                    self._snapshot.role, self._snapshot.route_generation,
                    self._snapshot.consent_generation, route.base_url,
                )
            elif event == "http11.send_request_body.complete":
                release()

        # The pinned httpcore HTTP/1.1 trace runs before the first header byte,
        # after DNS and TLS. Release only after the body has been written.
        if httpcore.__version__ != "1.0.9":
            raise CloudRouteError("Unsupported HTTP transport version for Observation authorization")
        request.extensions["trace"] = before_network_io
        try:
            response = self._http.handle_request(request)
        finally:
            release()
        try:
            self._settings.check(self._snapshot, probe=self._probe)
            _require_request_permission(self._request_permitted)
            if self._image_grant is not None:
                self._image_grant.check()
            return response
        except Exception:
            response.close()
            raise

    def close(self) -> None:
        self._http.close()


def _request(
    settings: ObservationCloudSettings, snapshot: CloudRouteSnapshot, *,
    system: str, prompt: str, schema: dict, image: bytes | None,
    credential: Callable[[CloudRoute], str], timeout_limit: float | None,
    probe: bool = False, image_grant: VisionImageGrant | None = None,
    request_permitted: Callable[[], bool] | None = None,
) -> dict:
    _require_request_permission(request_permitted)
    settings.check(snapshot, probe=probe)
    route = snapshot.route
    assert route is not None
    if image_grant is not None:
        image_grant.check()
    timeout = route.timeout_s if timeout_limit is None else min(route.timeout_s, timeout_limit)
    transport = _AuthorizedTransport(
        settings, snapshot, probe=probe, image_grant=image_grant, request_permitted=request_permitted,
    )
    if not route.is_cloud:
        with OllamaClient(route.model, endpoint=route.base_url, timeout=timeout, transport=transport) as client:
            return client.complete(system=system, prompt=prompt, schema=schema, image=image)

    from openai import OpenAI

    # Debug logging in this SDK version can include the full request body.
    logging.getLogger("openai._base_client").disabled = True
    content = prompt
    if image is not None:
        content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {
                "url": "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii"),
            }},
        ]
    options = {"store": False} if route.provider_kind == "openai" else {}
    with httpx.Client(
        transport=transport,
        timeout=httpx.Timeout(timeout, write=min(timeout, 2.0)), trust_env=False, follow_redirects=False,
    ) as http:
        with OpenAI(
            base_url=route.base_url, api_key=credential(route) if route.credential_ref else "mochi-no-key",
            http_client=http, max_retries=0, organization="", project="",
        ) as sdk:
            result = sdk.chat.completions.create(
                model=route.model, stream=False,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": content}],
                response_format={"type": "json_schema", "json_schema": {
                    "name": snapshot.role, "strict": True, "schema": schema,
                }},
                **options,
            )
            choice = result.choices[0]
            if choice.finish_reason != "stop" or choice.message.refusal:
                raise CloudRouteError("The provider did not return a complete structured response")
            data = json.loads(choice.message.content or "")
    if not isinstance(data, dict):
        raise CloudRouteError("The provider returned a non-object response")
    return data


def _failure(error: Exception) -> CloudRouteError:
    if isinstance(error, CloudRouteError):
        return error
    if isinstance(error.__cause__, CloudRouteError):
        return error.__cause__
    status = getattr(error, "status_code", None)
    label = f"HTTP {status}" if type(status) is int else type(error).__name__
    return CloudRouteError(f"Observation cloud request failed ({label})")


def verify_cloud_route(
    settings: ObservationCloudSettings, role: Role, *,
    credential: Callable[[CloudRoute], str] = _resolve_credential,
    expected: CloudRouteSnapshot | None = None,
) -> CloudRouteSnapshot:
    """Verify structured output and, for vision, a synthetic image only."""
    snapshot = expected if expected is not None else settings.snapshot(role)
    if role not in {VISION, STATE}:
        raise ValueError("This capability check is for Observation roles")
    if snapshot.role != role:
        raise ValueError("The expected settings belong to another role")
    image = None
    if role == VISION:
        from PIL import Image

        answer = secrets.choice(tuple(_PROBE_COLORS))
        buffer = BytesIO()
        Image.new("RGB", (128, 128), _PROBE_COLORS[answer]).save(buffer, format="JPEG")
        image = buffer.getvalue()
        prompt = "Identify the dominant color in this synthetic sample: blue, green, red, or yellow."
        value_schema = {"type": "string", "maxLength": 160, "enum": list(_PROBE_COLORS)}
    else:
        answer = secrets.token_hex(4)
        prompt = "Return this test code as the value of answer: " + answer
        value_schema = {"type": "string", "maxLength": 240}
    try:
        settings.check(snapshot, probe=True)
        result = _request(
            settings, snapshot, system="Return only the requested JSON object.", prompt=prompt,
            schema={
                "type": "object", "properties": {"answer": value_schema},
                "required": ["answer"], "additionalProperties": False,
            },
            image=image, credential=credential, timeout_limit=None, probe=True,
        )
        if result != {"answer": answer}:
            raise CloudRouteError("The model failed the structured capability check")
        return settings.record_result(snapshot, probe=True)
    except Exception as error:
        sanitized = _failure(error)
        try:
            settings.record_result(snapshot, probe=True, failure=str(sanitized))
        except (CloudRouteError, sqlite3.Error, ValueError):
            pass
        raise sanitized from None


class RoutedObservationModel:
    """Re-select a configured cloud route on each new round; default to local Ollama."""

    def __init__(
        self, role: Role, settings: ObservationCloudSettings, local: ScreenModel, *,
        timeout_limit: float | None = None, credential: Callable[[CloudRoute], str] = _resolve_credential,
        local_vision_only: bool = False, request_policy: Callable[[], int | None] | None = None,
    ) -> None:
        self._role = role
        self._settings = settings
        self._local = local
        self._timeout_limit = timeout_limit
        self._credential = credential
        self._local_vision_only = local_vision_only
        self._request_policy = request_policy

    @property
    def model(self) -> str:
        try:
            route = self._settings.snapshot(self._role).route
        except (CloudRouteError, sqlite3.Error, ValueError):
            route = None
        if route is None or (self._local_vision_only and route.is_cloud):
            return self._local.model
        return route.model

    def prepare_vision_capture(self, *, permitted: Callable[[], bool]) -> VisionImageGrant | object | None:
        if self._role != VISION:
            raise ValueError("Only the vision model can acquire a window image")
        snapshot = self._settings.snapshot(VISION)
        if snapshot.route is None or (self._local_vision_only and snapshot.route.is_cloud):
            return LOCAL_CAPTURE
        if not snapshot.route.is_cloud:
            return LOCAL_CAPTURE if snapshot.verified else None
        if not (permitted() and snapshot.permission and snapshot.verified):
            return None
        return VisionImageGrant(snapshot, time.monotonic() + min(self._timeout_limit or 15.0, 15.0), permitted)

    def complete(
        self, *, system: str, prompt: str, schema: dict, image: bytes | None = None,
    ) -> dict:
        request_permitted = None
        if self._request_policy is not None:
            stamp = self._request_policy()
            if stamp is None:
                raise CloudRouteError("Desktop Observation is paused")
            request_permitted = lambda: self._request_policy() == stamp
        snapshot = self._settings.snapshot(self._role)
        route = snapshot.route
        if route is None or (self._local_vision_only and route.is_cloud):
            _require_request_permission(request_permitted)
            result = self._local.complete(system=system, prompt=prompt, schema=schema, image=image)
            _require_request_permission(request_permitted)
            return result
        if not route.is_cloud:
            return self._complete_route(
                snapshot, system=system, prompt=prompt, schema=schema, image=image,
                request_permitted=request_permitted,
            )
        if self._role != STATE or image is not None:
            raise CloudRouteError("A scoped window capture grant is required")
        from .state import _STATE_SCHEMA, _SYSTEM

        window = json.loads(prompt)
        if not isinstance(window, dict):
            raise CloudRouteError("Invalid Observation state inputs")
        projected = project_state_window(window, share_screen_text=snapshot.permission)
        return self._complete_route(
            snapshot, system=_SYSTEM, prompt=json.dumps(projected, ensure_ascii=False, allow_nan=False),
            schema=_STATE_SCHEMA, image=None, request_permitted=request_permitted,
        )

    def complete_image(self, image_grant: VisionImageGrant, *, image: bytes) -> dict:
        if (self._role != VISION or self._local_vision_only or image_grant.snapshot.role != VISION
                or image_grant.snapshot.route is None or not image_grant.snapshot.route.is_cloud):
            raise CloudRouteError("A scoped window capture grant is required")
        from .vision import _SCREEN_SCHEMA, _SYSTEM, _USER

        return self._complete_route(
            image_grant.snapshot, system=_SYSTEM, prompt=_USER, schema=_SCREEN_SCHEMA, image=image,
            image_grant=image_grant,
        )

    def _complete_route(
        self, snapshot: CloudRouteSnapshot, *, system: str, prompt: str, schema: dict,
        image: bytes | None, image_grant: VisionImageGrant | None = None,
        request_permitted: Callable[[], bool] | None = None,
    ) -> dict:
        try:
            result = _request(
                self._settings, snapshot, system=system, prompt=prompt, schema=schema, image=image,
                image_grant=image_grant, request_permitted=request_permitted,
                credential=self._credential, timeout_limit=self._timeout_limit,
            )
            _require_request_permission(request_permitted)
            if image_grant is not None:
                image_grant.check()
            if result.keys() != schema["properties"].keys():
                raise CloudRouteError("The provider returned unexpected Observation response fields")
            self._settings.record_result(snapshot)
            return result
        except Exception as error:
            sanitized = _failure(error)
            try:
                self._settings.record_result(snapshot, failure=str(sanitized))
            except (CloudRouteError, sqlite3.Error, ValueError):
                pass
            raise sanitized from None
