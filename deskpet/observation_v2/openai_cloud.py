"""OpenAI SDK transport for independently routed Observation models."""

from __future__ import annotations

import base64
from collections.abc import Callable
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
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
from typing import TYPE_CHECKING

import httpcore
import httpx

from .http_cancellation import CancellableRequest
from .ollama import OllamaClient
from .routes import (
    BAILIAN_OBSERVATION_MODEL, STATE, VISION, CloudRoute, CloudRouteError, CloudRouteSnapshot,
    ObservationCloudSettings, Role, is_bailian_endpoint, observation_privacy_gate,
)
from .usage import ObservationUsage, report_usage
from .vision import ScreenModel, VisionSignals, cloud_screen_text

if TYPE_CHECKING:
    from .dsh import ObservationDSH

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
    allow_late_result: bool = False
    send_permitted: Callable[[], bool] | None = None
    _settings_scope: str | None = field(default=None, repr=False)

    @property
    def comparison_scope(self) -> str:
        snapshot = self.snapshot
        route = snapshot.route
        assert route is not None
        material = json.dumps((
            self._settings_scope, snapshot.user_scope, route.recipient, snapshot.route_generation, snapshot.consent_generation,
        ))
        return sha256(material.encode()).hexdigest()

    def check(self) -> None:
        if time.monotonic() >= self.expires_at or not self.permitted():
            raise CloudRouteError("Window capture authorization expired")
        if self.send_permitted is not None and not self.send_permitted():
            raise CloudRouteError("The captured window is no longer eligible for upload")

    def check_response(self) -> None:
        if not self.allow_late_result:
            self.check()
        elif not self.permitted():
            raise CloudRouteError("Window capture authorization changed")


def permits_unfiltered_desktop(grant: object) -> bool:
    if not isinstance(grant, VisionImageGrant):
        return False
    snapshot = grant.snapshot
    route = snapshot.route
    return (snapshot.role == VISION and snapshot.permission and snapshot.verified and route is not None
            and route.model == BAILIAN_OBSERVATION_MODEL and is_bailian_endpoint(route.base_url))


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
        send_permitted: Callable[[], bool] | None, cancellation: CancellableRequest | None,
    ) -> None:
        self._settings = settings
        self._snapshot = snapshot
        self._probe = probe
        self._image_grant = image_grant
        self._request_permitted = request_permitted
        self._send_permitted = send_permitted
        self._cancellation = cancellation
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

        def before_network_io(event: str, info: dict) -> None:
            nonlocal gate
            if self._cancellation is not None:
                self._cancellation.trace(event, info)
            if event == "http11.send_request_headers.started":
                admission = self._settings.admit(self._snapshot, probe=self._probe)
                admission.__enter__()
                gate = admission
                _require_request_permission(self._request_permitted)
                _require_request_permission(self._send_permitted)
                if self._image_grant is not None:
                    self._image_grant.check()
                _LOG.debug(
                    "Observation send admitted role=%s route_generation=%s consent_generation=%s recipient=%s",
                    self._snapshot.role, self._snapshot.route_generation,
                    self._snapshot.consent_generation, route.base_url,
                )
            elif event == "http11.send_request_body.started":
                _require_request_permission(self._request_permitted)
                _require_request_permission(self._send_permitted)
                if self._image_grant is not None:
                    self._image_grant.check()
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
                self._image_grant.check_response()
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
    send_permitted: Callable[[], bool] | None = None,
    cancellation: CancellableRequest | None = None,
    usage_observer: Callable[[ObservationUsage], None] | None = None,
) -> dict:
    _require_request_permission(request_permitted)
    _require_request_permission(send_permitted)
    settings.check(snapshot, probe=probe)
    route = snapshot.route
    assert route is not None
    if image_grant is not None:
        image_grant.check()
    timeout = route.timeout_s if timeout_limit is None else min(route.timeout_s, timeout_limit)
    transport = _AuthorizedTransport(
        settings, snapshot, probe=probe, image_grant=image_grant, request_permitted=request_permitted,
        send_permitted=send_permitted, cancellation=cancellation,
    )
    if not route.is_cloud:
        with OllamaClient(route.model, endpoint=route.base_url, timeout=timeout, transport=transport,
                          usage_observer=usage_observer) as client:
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
    if route.model == BAILIAN_OBSERVATION_MODEL and is_bailian_endpoint(route.base_url):
        options.update(extra_body={"enable_thinking": False}, max_completion_tokens=512)
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
            usage = result.usage
            report_usage(usage_observer, model=route.model, prompt=getattr(usage, "prompt_tokens", None),
                         completion=getattr(usage, "completion_tokens", None), total=getattr(usage, "total_tokens", None))
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
    usage_observer: Callable[[ObservationUsage], None] | None = None,
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
            image=image, credential=credential, timeout_limit=None, probe=True, usage_observer=usage_observer,
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
        self, role: Role, settings: ObservationCloudSettings | None, local: ScreenModel, *,
        timeout_limit: float | None = None, credential: Callable[[CloudRoute], str] = _resolve_credential,
        local_vision_only: bool = False, request_policy: Callable[[], int | None] | None = None,
        usage_observer: Callable[[ObservationUsage], None] | None = None,
        vision_runtime: ObservationDSH | None = None,
    ) -> None:
        self._role = role
        self._settings = settings
        self._local = local
        self._timeout_limit = timeout_limit
        self._credential = credential
        self._local_vision_only = local_vision_only
        self._request_policy = request_policy
        self._usage_observer = usage_observer
        self._vision_runtime = vision_runtime

    def _snapshot(self) -> CloudRouteSnapshot:
        if self._settings is None:
            return CloudRouteSnapshot(self._role, "default")
        return self._settings.snapshot(self._role)

    def _request_permitted(self) -> Callable[[], bool] | None:
        if self._request_policy is None:
            return None
        stamp = self._request_policy()
        if stamp is None:
            raise CloudRouteError("Desktop Observation is paused")
        return lambda: self._request_policy() == stamp

    @property
    def model(self) -> str:
        try:
            route = self._snapshot().route
        except (CloudRouteError, sqlite3.Error, ValueError):
            route = None
        if route is None or (self._local_vision_only and route.is_cloud):
            return self._local.model
        return route.model

    def prepare_vision_capture(self, *, permitted: Callable[[], bool]) -> VisionImageGrant | object | None:
        if self._role != VISION:
            raise ValueError("Only the vision model can acquire a window image")
        snapshot = self._snapshot()
        if snapshot.route is None or (self._local_vision_only and snapshot.route.is_cloud):
            return LOCAL_CAPTURE
        if not snapshot.route.is_cloud:
            return LOCAL_CAPTURE if snapshot.verified else None
        if not (permitted() and snapshot.permission and snapshot.verified):
            return None
        assert self._settings is not None
        return VisionImageGrant(
            snapshot, time.monotonic() + min(self._timeout_limit or 15.0, 15.0), permitted,
            _settings_scope=str(self._settings.path),
        )

    def complete(
        self, *, system: str, prompt: str, schema: dict, image: bytes | None = None,
    ) -> dict:
        if self._role == VISION and image is not None and self._vision_runtime is not None:
            return self._complete_visual(
                None, system=system, prompt=prompt, schema=schema, image=image, allow_partial_fields=False,
            )
        return self._complete(system=system, prompt=prompt, schema=schema, image=image)

    def _complete(
        self, *, system: str, prompt: str, schema: dict, image: bytes | None,
        allow_partial_fields: bool = False,
    ) -> dict:
        request_permitted = self._request_permitted()
        snapshot = self._snapshot()
        route = snapshot.route
        if route is None or (self._local_vision_only and route.is_cloud):
            _require_request_permission(request_permitted)
            result = self._local.complete(system=system, prompt=prompt, schema=schema, image=image)
            _require_request_permission(request_permitted)
            return result
        if not route.is_cloud:
            return self._complete_route(
                snapshot, system=system, prompt=prompt, schema=schema, image=image,
                request_permitted=request_permitted, allow_partial_fields=allow_partial_fields,
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

    def _image_snapshot(self, image_grant: VisionImageGrant) -> CloudRouteSnapshot:
        if (self._role != VISION or self._settings is None or self._local_vision_only or image_grant.snapshot.role != VISION
                or image_grant._settings_scope not in (None, str(self._settings.path))
                or image_grant.snapshot.route is None or not image_grant.snapshot.route.is_cloud):
            raise CloudRouteError("A scoped window capture grant is required")
        return image_grant.snapshot

    def _check_visual_route(self, snapshot: CloudRouteSnapshot) -> None:
        settings = self._settings
        route = snapshot.route
        user_scope = settings.user_scope if settings is not None else "default"
        if self._role != VISION or snapshot.role != VISION or snapshot.user_scope != user_scope:
            raise CloudRouteError("A scoped window capture grant is required")
        if route is not None and not (self._local_vision_only and route.is_cloud):
            assert settings is not None
            settings.check(snapshot)
        elif settings is not None:
            current = self._snapshot()
            if current.route != route or current.route_generation != snapshot.route_generation:
                raise CloudRouteError("Observation service authorization changed")

    def capture_result_scope(self, grant: object) -> CloudRouteSnapshot:
        """Pin the recipient at acquisition for the eventual result commit."""
        if self._role != VISION:
            raise CloudRouteError("A scoped window capture grant is required")
        if isinstance(grant, VisionImageGrant):
            return self._image_snapshot(grant)
        if grant is LOCAL_CAPTURE:
            snapshot = self._snapshot()
            if snapshot.route is None or self._local_vision_only or not snapshot.route.is_cloud:
                self._check_visual_route(snapshot)
                return snapshot
        raise CloudRouteError("A scoped window capture grant is required")

    @contextmanager
    def admit_vision_result(self, snapshot: CloudRouteSnapshot):
        """Serialize result acceptance with recipient and consent changes."""
        settings = self._settings
        with ExitStack() as stack:
            try:
                if settings is not None:
                    stack.enter_context(observation_privacy_gate(settings.path))
                self._check_visual_route(snapshot)
            except (CloudRouteError, OSError, sqlite3.Error, ValueError):
                stack.close()
                yield False
            else:
                yield True

    def complete_image(self, image_grant: VisionImageGrant, *, image: bytes) -> dict:
        from .vision import _SCREEN_SCHEMA, _SYSTEM, _USER

        if self._vision_runtime is not None:
            return self._complete_visual(
                image_grant, system=_SYSTEM, prompt=_USER, schema=_SCREEN_SCHEMA, image=image,
                allow_partial_fields=False,
            )
        return self._complete_route(
            self._image_snapshot(image_grant), system=_SYSTEM, prompt=_USER, schema=_SCREEN_SCHEMA, image=image,
            image_grant=image_grant,
        )

    def complete_multimodal_image(
        self, image_grant: VisionImageGrant | None, *, image: bytes, signals: VisionSignals | None,
        captured_at: datetime, activity_id: int, allow_document_title: bool, all_displays: bool = False,
        unfiltered_desktop: bool = False,
        local_permitted: Callable[[], bool] | None = None, deadline_s: float | None = None,
        scoped_snapshot: CloudRouteSnapshot | None = None,
    ) -> dict:
        from .vision import _multimodal_request, _project_signals

        if self._role != VISION or not image or captured_at.utcoffset() is None:
            raise CloudRouteError("A timed window capture is required")
        if unfiltered_desktop and not permits_unfiltered_desktop(image_grant):
            raise CloudRouteError("Unfiltered desktop capture requires an authorized Qwen vision route")
        projected = _project_signals(
            signals, captured_at=captured_at, activity_id=activity_id,
            comparison_scope=image_grant.comparison_scope if image_grant is not None else None,
        )
        if projected.get("screen_locked") is True:
            raise CloudRouteError("Window capture is unavailable while locked")
        request = _multimodal_request(
            projected, captured_at=captured_at, allow_document_title=allow_document_title,
            include_signals=image_grant is None or image_grant.snapshot.vision_signals_permission,
            all_displays=all_displays, unfiltered_desktop=unfiltered_desktop,
        )
        if image_grant is not None:
            image_grant = replace(image_grant, allow_late_result=True)
        if self._vision_runtime is not None or scoped_snapshot is not None:
            return self._complete_visual(
                image_grant, image=image, local_permitted=local_permitted, deadline_s=deadline_s,
                scoped_snapshot=scoped_snapshot, allow_partial_fields=True, **request,
            )
        if image_grant is None:
            return self._complete(image=image, allow_partial_fields=True, **request)
        return self._complete_route(
            self._image_snapshot(image_grant), image=image, image_grant=image_grant,
            allow_partial_fields=True, **request,
        )

    def _complete_visual(
        self, image_grant: VisionImageGrant | None, *, system: str, prompt: str, schema: dict, image: bytes,
        allow_partial_fields: bool, local_permitted: Callable[[], bool] | None = None,
        deadline_s: float | None = None,
        scoped_snapshot: CloudRouteSnapshot | None = None,
    ) -> dict:
        runtime = self._vision_runtime
        snapshot = self._image_snapshot(image_grant) if image_grant is not None else scoped_snapshot or self._snapshot()
        if scoped_snapshot is not None and scoped_snapshot != snapshot:
            raise CloudRouteError("Observation service authorization changed")
        route = snapshot.route
        if self._local_vision_only and route is not None and route.is_cloud:
            route = None
        if image_grant is None and route is not None and route.is_cloud:
            raise CloudRouteError("A scoped window capture grant is required")
        request_permitted = self._request_permitted()

        def check_route() -> None:
            _require_request_permission(request_permitted)
            self._check_visual_route(snapshot)

        def send_permitted() -> bool:
            check_route()
            if image_grant is not None:
                image_grant.check()
            return ((deadline_s is None or time.monotonic() < deadline_s)
                    and (image_grant is not None or local_permitted is None or local_permitted()))

        def infer(*, system, prompt, schema, image, cancelled=None):
            def active() -> bool:
                return (cancelled is None or not cancelled()) and (request_permitted is None or request_permitted())

            def may_send() -> bool:
                return active() and send_permitted()

            _require_request_permission(may_send)
            if route is not None:
                return self._complete_route(
                    snapshot, system=system, prompt=prompt, schema=schema, image=image,
                    image_grant=image_grant, request_permitted=active,
                    send_permitted=may_send if image_grant is None else None,
                    allow_partial_fields=allow_partial_fields, cancelled=cancelled,
                )
            kwargs = (
                {"send_permitted": may_send, "cancelled": cancelled} if isinstance(self._local, OllamaClient) else {}
            )
            result = self._local.complete(system=system, prompt=prompt, schema=schema, image=image, **kwargs)
            _require_request_permission(active)
            return result

        timeout = self._timeout_limit or (route.timeout_s if route is not None else 90.0)
        if route is not None:
            timeout = min(timeout, route.timeout_s)
        if image_grant is not None:
            deadline_s = min(deadline_s, image_grant.expires_at) if deadline_s is not None else image_grant.expires_at
        if runtime is None:
            result = infer(system=system, prompt=prompt, schema=schema, image=image)
        else:
            result = runtime.complete(
                model=self._local.model if route is None else route.model, system=system, prompt=prompt,
                schema=schema, image=image, infer=infer, timeout=timeout, send_permitted=send_permitted,
                deadline_s=deadline_s,
            )
        check_route()
        if image_grant is not None:
            image_grant.check_response()
        return result

    def _complete_route(
        self, snapshot: CloudRouteSnapshot, *, system: str, prompt: str, schema: dict,
        image: bytes | None, image_grant: VisionImageGrant | None = None,
        request_permitted: Callable[[], bool] | None = None,
        send_permitted: Callable[[], bool] | None = None,
        allow_partial_fields: bool = False,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict:
        assert self._settings is not None
        try:
            with CancellableRequest(cancelled) as cancellation:
                result = _request(
                    self._settings, snapshot, system=system, prompt=prompt, schema=schema, image=image,
                    image_grant=image_grant, request_permitted=request_permitted,
                    send_permitted=send_permitted, cancellation=cancellation,
                    credential=self._credential, timeout_limit=self._timeout_limit, usage_observer=self._usage_observer,
                )
            _require_request_permission(request_permitted)
            if image_grant is not None:
                image_grant.check_response()
            if not allow_partial_fields and result.keys() != schema["properties"].keys():
                raise CloudRouteError("The provider returned unexpected Observation response fields")
            self._settings.record_result(snapshot)
            return result
        except Exception as error:
            sanitized = _failure(error)
            try:
                if cancelled is None or not cancelled():
                    self._settings.record_result(snapshot, failure=str(sanitized))
            except (CloudRouteError, sqlite3.Error, ValueError):
                pass
            raise sanitized from None
