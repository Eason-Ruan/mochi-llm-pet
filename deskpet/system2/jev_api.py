"""Send opt-in shadow judgments only to TypeSafe's fixed HTTPS endpoint.

Only typed results and bounded status metadata leave the client. Provider error
bodies, credentials, and request payloads do not enter diagnostics.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import os
import re
import sys
import time
from typing import Literal

import httpx


JEV_SYSTEM_ONE_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_JEV_MODEL = "jev-1.14.0"
JEV_DEADLINE_S = 1.5
JEV_KEYCHAIN_SERVICE = "Mochi Jev"
JEV_KEYCHAIN_ACCOUNT = "default"

_QUESTION = "background_action"
_INSTRUCTIONS = (
    "Based only on the supplied facts and the criteria, choose the single action "
    "that is most appropriate for this background wake."
)
_MAX_STATE_BYTES = 8_192
_MAX_REQUEST_BYTES = 16_384
_MAX_RESPONSE_BYTES = 16_384
_MODEL_NAME = re.compile(r"jev-[A-Za-z0-9._-]{1,60}")
_CHOICE_NAME = re.compile(r"[a-z][a-z0-9_]{0,39}")
_BEARER_KEY = re.compile(r"[!-~]{1,512}")

JevStatus = Literal[
    "ok", "disabled", "cancelled", "timeout", "http_error", "transport_error",
    "invalid_response", "invalid_request",
]


@dataclass(frozen=True, slots=True)
class JevSettings:
    enabled: bool = False
    share_text: bool = False
    model: str = DEFAULT_JEV_MODEL
    api_key: str | None = field(default=None, repr=False)

    @property
    def available(self) -> bool:
        return (
            self.enabled is True
            and isinstance(self.model, str)
            and _MODEL_NAME.fullmatch(self.model) is not None
            and isinstance(self.api_key, str)
            and _BEARER_KEY.fullmatch(self.api_key) is not None
        )


def jev_shadow_requested() -> bool:
    return os.environ.get("MOCHI_JEV_SHADOW") == "1"


def _mac_keychain_key() -> str | None:
    if sys.platform != "darwin":
        return None
    try:
        from keyring.backends.macOS import Keyring

        return Keyring().get_password(JEV_KEYCHAIN_SERVICE, JEV_KEYCHAIN_ACCOUNT)
    except Exception:
        return None


def load_jev_settings() -> JevSettings:
    if not jev_shadow_requested():
        return JevSettings()
    key = os.environ.get("JEV_API_KEY")
    model = os.environ.get("MOCHI_JEV_MODEL", DEFAULT_JEV_MODEL)
    return JevSettings(
        enabled=True,
        share_text=os.environ.get("MOCHI_JEV_SHARE_TEXT") == "1",
        model=model if _MODEL_NAME.fullmatch(model) is not None else "",
        api_key=_mac_keychain_key() if key is None else key,
    )


@dataclass(frozen=True, slots=True)
class JevResult:
    status: JevStatus
    latency_ms: float
    choice: str | None = None
    confidence: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    model: str | None = None
    http_status: int | None = None
    probabilities: dict[str, float] | None = None
    send_started_at: datetime | None = None


class _JevCancelled(Exception):
    pass


class _JevInvalidResponse(Exception):
    pass


class _JevHttpError(Exception):
    def __init__(self, status: int):
        self.status = status


def _request_body(settings: JevSettings, state: dict, criteria: Mapping[str, str]) -> bytes:
    if not isinstance(state, dict) or not 2 <= len(criteria) <= 12:
        raise ValueError
    for name, description in criteria.items():
        if (
            not isinstance(name, str)
            or _CHOICE_NAME.fullmatch(name) is None
            or not isinstance(description, str)
            or not 1 <= len(description) <= 350
        ):
            raise ValueError

    state_json = json.dumps(state, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(state_json.encode()) > _MAX_STATE_BYTES:
        raise ValueError
    if not settings.share_text:
        phase = state.get("phase")
        activity = state.get("activity")
        focus = state.get("focus_2m")
        captures = (focus.get("captures") or ()) if isinstance(focus, dict) else ()
        if (
            (isinstance(phase, dict) and phase.get("goal_summary") is not None)
            or (isinstance(activity, dict) and activity.get("summary") is not None)
            or any(isinstance(capture, dict) and capture.get("summary") is not None for capture in captures)
        ):
            raise ValueError

    payload = {
        "model": settings.model,
        "state": state,
        "questions": {
            _QUESTION: {
                "type": "choice",
                "instructions": _INSTRUCTIONS,
                "criteria": dict(criteria),
            },
        },
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    if len(body) > _MAX_REQUEST_BYTES:
        raise ValueError
    return body


def _probability(value: object) -> bool:
    return type(value) in {float, int} and 0 <= value <= 1


def _parse_response(raw: bytes, criteria: Mapping[str, str]) -> dict:
    try:
        result = json.loads(raw)
        model = result["model"]
        answer = result["answers"][_QUESTION]
        choice = answer["choice"]
        confidence = answer["confidence"]
        probabilities = answer["probabilities"]
        usage = result["usage"]
        input_tokens = usage["input_tokens"]
        output_tokens = usage["output_tokens"]
        if (
            not isinstance(model, str)
            or _MODEL_NAME.fullmatch(model) is None
            or answer["type"] != "choice"
            or choice not in criteria
            or not _probability(confidence)
            or not isinstance(probabilities, dict)
            or probabilities.keys() != criteria.keys()
            or not all(map(_probability, probabilities.values()))
            or type(input_tokens) is not int
            or not 0 <= input_tokens <= 1_000_000
            or type(output_tokens) is not int
            or not 0 <= output_tokens <= 1_000_000
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, RecursionError):
        raise _JevInvalidResponse from None
    return {
        "choice": choice,
        "confidence": float(confidence),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "model": model,
        "probabilities": {name: float(value) for name, value in probabilities.items()},
    }


class JevClient:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None):
        self._transport = transport

    def adjudicate(
        self,
        settings: JevSettings,
        state: dict,
        *,
        criteria: Mapping[str, str],
        send_permitted: Callable[[], bool] | None = None,
    ) -> JevResult:
        """Make one bounded request from the shadow worker."""
        start = time.monotonic()
        send_started_at = None

        def result(status: JevStatus, **fields) -> JevResult:
            return JevResult(status, (time.monotonic() - start) * 1_000, send_started_at=send_started_at, **fields)

        def mark_send_started() -> None:
            nonlocal send_started_at
            send_started_at = datetime.now(UTC)

        if not settings.available:
            return result("disabled")
        try:
            body = _request_body(settings, state, criteria)
        except (TypeError, ValueError, RecursionError):
            return result("invalid_request")

        # DNS can be waiting in the loop's executor when the deadline expires.
        # Close the loop without waiting for the OS resolver to return.
        loop = asyncio.new_event_loop()
        try:
            data = loop.run_until_complete(
                self._ask(settings, body, criteria, send_permitted, mark_send_started, start + JEV_DEADLINE_S),
            )
            return result("ok", **data)
        except (TimeoutError, httpx.TimeoutException):
            return result("timeout")
        except _JevCancelled:
            return result("cancelled")
        except _JevHttpError as error:
            return result("http_error", http_status=error.status)
        except _JevInvalidResponse:
            return result("invalid_response")
        except httpx.HTTPError:
            return result("transport_error")
        finally:
            loop.close()

    async def _ask(
        self,
        settings: JevSettings,
        body: bytes,
        criteria: Mapping[str, str],
        send_permitted: Callable[[], bool] | None,
        mark_send_started: Callable[[], None],
        deadline: float,
    ) -> dict:
        async def check_permission() -> None:
            if send_permitted is None:
                return
            try:
                permitted = await asyncio.to_thread(send_permitted)
            except Exception:
                raise _JevCancelled from None
            if permitted is not True:
                raise _JevCancelled

        async def trace(event: str, _info: dict) -> None:
            if event == "http11.send_request_headers.started":
                await check_permission()
                if time.monotonic() >= deadline:
                    raise TimeoutError
                mark_send_started()

        async with asyncio.timeout(deadline - time.monotonic()):
            await check_permission()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            transport = self._transport
            if transport is None:
                transport = httpx.AsyncHTTPTransport(retries=0, trust_env=False, http2=False)
            async with httpx.AsyncClient(
                transport=transport, timeout=remaining, trust_env=False, follow_redirects=False,
            ) as client:
                async with client.stream(
                    "POST",
                    JEV_SYSTEM_ONE_URL,
                    content=body,
                    headers={
                        "Authorization": f"Bearer {settings.api_key}",
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                        "Content-Type": "application/json",
                    },
                    extensions={"trace": trace},
                ) as response:
                    if response.status_code != 200:
                        raise _JevHttpError(response.status_code)
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise _JevInvalidResponse
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(raw) + len(chunk) > _MAX_RESPONSE_BYTES:
                            raise _JevInvalidResponse
                        raw.extend(chunk)
            data = _parse_response(raw, criteria)
            if time.monotonic() >= deadline:
                raise TimeoutError
            return data
