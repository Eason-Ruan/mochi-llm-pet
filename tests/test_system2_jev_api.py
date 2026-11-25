import asyncio
import json

import httpx
import pytest

from deskpet.system2 import jev_api
from deskpet.system2.jev_api import JevClient, JevSettings


SETTINGS = JevSettings(enabled=True, api_key="test-only-bearer")
CRITERIA = {"stay_quiet": "Avoid interrupting", "open_system2": "Evaluate new evidence"}
STATE = {
    "activity": {"kind": "working", "summary": None},
    "phase": {"kind": "focus", "goal_summary": None},
}


def _response(request):
    question = next(iter(json.loads(request.content)["questions"]))
    return {
        "model": "jev-1.14.0",
        "answers": {
            question: {
                "type": "choice",
                "choice": "stay_quiet",
                "confidence": 0.71,
                "probabilities": {"stay_quiet": 0.9, "open_system2": 0.1},
            },
        },
        "usage": {"input_tokens": 250, "output_tokens": 14},
    }


def test_jev_uses_one_fixed_recipient_and_parses_choice_and_usage(monkeypatch, caplog):
    monkeypatch.setenv("HTTPS_PROXY", "https://untrusted.invalid")
    monkeypatch.setenv("MOCHI_JEV_BASE_URL", "http://untrusted.invalid")
    requests = []

    async def respond(request):
        await request.extensions["trace"]("http11.send_request_headers.started", {})
        requests.append(request)
        return httpx.Response(200, json=_response(request))

    result = JevClient(transport=httpx.MockTransport(respond)).adjudicate(
        SETTINGS, STATE, criteria=CRITERIA,
    )
    (request,) = requests
    assert request.method == "POST"
    assert str(request.url) == "https://api.typesafe.ai/v1/systemone"
    assert request.headers["Authorization"] == "Bearer test-only-bearer"
    assert request.headers["Accept-Encoding"] == "identity"
    assert "TypeSafe-Danger-Zone-Long-Context" not in request.headers
    assert "TypeSafe-Danger-Zone-Images" not in request.headers
    data = json.loads(request.content)
    assert set(data) == {"model", "state", "questions"}
    assert data["model"] == "jev-1.14.0"
    assert data["state"] == STATE
    (question,) = data["questions"].values()
    assert question["type"] == "choice"
    assert question["criteria"] == CRITERIA
    assert result.status == "ok"
    assert result.latency_ms >= 0
    assert result.choice == "stay_quiet"
    assert result.confidence == 0.71
    assert result.probabilities == {"stay_quiet": 0.9, "open_system2": 0.1}
    assert result.input_tokens == 250
    assert result.output_tokens == 14
    assert result.model == "jev-1.14.0"
    assert result.send_started_at is not None
    assert "test-only-bearer" not in repr(SETTINGS) + caplog.text


def test_jev_settings_require_separate_opt_in_and_dedicated_key(monkeypatch):
    for name in ("MOCHI_JEV_SHADOW", "MOCHI_JEV_SHARE_TEXT", "MOCHI_JEV_MODEL", "JEV_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TYPESAFE_API_KEY", "unused-other-provider-key")
    keychain_reads = []

    def credential():
        keychain_reads.append(True)
        return "test-keychain-key"

    monkeypatch.setattr(jev_api, "_mac_keychain_key", credential)
    monkeypatch.setenv("MOCHI_JEV_SHARE_TEXT", "1")
    assert jev_api.load_jev_settings() == JevSettings()
    assert not keychain_reads

    monkeypatch.setenv("MOCHI_JEV_SHADOW", "1")
    monkeypatch.delenv("MOCHI_JEV_SHARE_TEXT")
    structured = jev_api.load_jev_settings()
    assert structured.available
    assert structured.share_text is False
    assert structured.api_key == "test-keychain-key"

    monkeypatch.setenv("MOCHI_JEV_SHARE_TEXT", "1")
    monkeypatch.setenv("JEV_API_KEY", "test-env-key")
    monkeypatch.setenv("MOCHI_JEV_MODEL", "jev-1.13.0")
    sharing = jev_api.load_jev_settings()
    assert sharing == JevSettings(True, True, "jev-1.13.0", "test-env-key")
    assert keychain_reads == [True]
    assert JevSettings(enabled=True, api_key="test-key-with-a\nnewline").available is False
    monkeypatch.setenv("MOCHI_JEV_MODEL", "https://untrusted.invalid/fake-secret")
    invalid = jev_api.load_jev_settings()
    assert not invalid.available
    assert not invalid.model


def test_jev_disabled_or_invalid_input_makes_no_request():
    client = JevClient(transport=httpx.MockTransport(lambda _: pytest.fail("Unexpected HTTP request")))
    assert client.adjudicate(JevSettings(), STATE, criteria=CRITERIA).status == "disabled"
    assert client.adjudicate(SETTINGS, {"string": "x" * 8_192}, criteria=CRITERIA).status == "invalid_request"
    assert client.adjudicate(SETTINGS, STATE, criteria={"quiet": "One choice"}).status == "invalid_request"


@pytest.mark.parametrize("state", [
    {"activity": {"summary": "A document is visible."}},
    {"phase": {"goal_summary": "Write code."}},
    {"focus_2m": {"captures": [{"summary": "A document is visible."}]}},
])
def test_jev_projected_text_needs_separate_consent(state):
    sent = []

    def respond(request):
        sent.append(json.loads(request.content)["state"])
        return httpx.Response(200, json=_response(request))

    client = JevClient(transport=httpx.MockTransport(respond))
    assert client.adjudicate(SETTINGS, state, criteria=CRITERIA).status == "invalid_request"
    assert not sent
    sharing = JevSettings(enabled=True, share_text=True, api_key="test-only-bearer")
    assert client.adjudicate(sharing, state, criteria=CRITERIA).status == "ok"
    assert sent == [state]


@pytest.mark.parametrize("status", [302, 401, 429, 500])
def test_jev_never_follows_redirects_or_retries_http_errors(status, caplog):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, headers={"Location": "https://untrusted.invalid"}, text="secret error details")

    result = JevClient(transport=httpx.MockTransport(respond)).adjudicate(SETTINGS, STATE, criteria=CRITERIA)
    assert len(requests) == 1
    assert result.status == "http_error"
    assert result.http_status == status
    assert result.latency_ms >= 0
    assert "secret error details" not in repr(result) + caplog.text


@pytest.mark.parametrize("bad_field", ["choice", "probabilities", "confidence", "usage", "body", "encoded", "large"])
def test_jev_invalid_response_does_not_supply_choice(bad_field):
    def respond(request):
        if bad_field == "large":
            return httpx.Response(200, content=b"x" * 20_000)
        if bad_field == "encoded":
            return httpx.Response(200, headers={"Content-Encoding": "br"})
        if bad_field == "body":
            return httpx.Response(200, content=b"{")
        payload = _response(request)
        answer = next(iter(payload["answers"].values()))
        if bad_field == "usage":
            payload["usage"]["input_tokens"] = True
        else:
            answer[bad_field] = "undeclared_choice"
        return httpx.Response(200, json=payload)

    result = JevClient(transport=httpx.MockTransport(respond)).adjudicate(SETTINGS, STATE, criteria=CRITERIA)
    assert result.status == "invalid_response"
    assert result.choice is None


def test_jev_permission_rechecked_at_headers_before_send():
    permissions = iter([True, False])
    sent = []

    async def respond(request):
        await request.extensions["trace"]("http11.send_request_headers.started", {})
        sent.append(request)
        return httpx.Response(200, json=_response(request))

    result = JevClient(transport=httpx.MockTransport(respond)).adjudicate(
        SETTINGS, STATE, criteria=CRITERIA, send_permitted=lambda: next(permissions),
    )
    assert result.status == "cancelled"
    assert result.send_started_at is None
    assert not sent


def test_jev_total_deadline_stops_trickling_response(monkeypatch):
    monkeypatch.setattr(jev_api, "JEV_DEADLINE_S", 0.08)
    closed = []

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(20):
                await asyncio.sleep(0.01)
                yield b" "

        async def aclose(self):
            closed.append(True)

    result = JevClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=SlowStream()))).adjudicate(
        SETTINGS, STATE, criteria=CRITERIA,
    )
    assert result.status == "timeout"
    assert result.latency_ms >= 75
    assert closed == [True]
