"""Structured requests to a local Ollama endpoint."""

from __future__ import annotations

import base64
from collections.abc import Callable
import ipaddress
import json
import math
from urllib.parse import urlsplit

import httpx

from .http_cancellation import CancellableRequest
from .usage import ObservationUsage, report_usage

DEFAULT_ENDPOINT = "http://127.0.0.1:11434"


class OllamaError(RuntimeError):
    """Model request failed without including request or response contents."""


def validate_local_model(model: str) -> None:
    if not model.strip():
        raise ValueError("An Ollama model is required")
    model_parts = model.casefold().split("/")[-1].split(":", 1)
    if any(part == "cloud" or part.endswith("-cloud") for part in model_parts):
        raise ValueError("Hosted Ollama models are unsupported")


class OllamaClient:
    def __init__(
        self, model: str, *, endpoint: str = DEFAULT_ENDPOINT, timeout: float = 90.0,
        transport: httpx.BaseTransport | None = None,
        usage_observer: Callable[[ObservationUsage], None] | None = None,
    ):
        parts = urlsplit(endpoint)
        host = parts.hostname
        allowed_host = host == "localhost"
        if host and not allowed_host:
            try:
                allowed_host = ipaddress.ip_address(host).is_loopback
            except ValueError:
                pass
        if (
            not allowed_host
            or parts.scheme not in {"http", "https"}
            or parts.path not in {"", "/"}
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
        ):
            raise ValueError("Ollama endpoint must be a loopback HTTP or HTTPS origin")
        validate_local_model(model)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("A positive finite model timeout is required")
        self.model = model
        self._usage_observer = usage_observer
        self._http = httpx.Client(
            base_url=endpoint, timeout=timeout, trust_env=False, follow_redirects=False, transport=transport,
        )

    def __enter__(self) -> OllamaClient:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self._http.close()

    def complete(
        self, *, system: str, prompt: str, schema: dict, image: bytes | None = None,
        send_permitted: Callable[[], bool] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict:
        message = {"role": "user", "content": prompt}
        if image is not None:
            message["images"] = [base64.b64encode(image).decode("ascii")]

        try:
            with CancellableRequest(cancelled) as cancellation:
                def trace(event: str, info: dict) -> None:
                    cancellation.trace(event, info)
                    if (event in {"http11.send_request_headers.started", "http11.send_request_body.started"}
                            and send_permitted is not None and not send_permitted()):
                        raise OllamaError("Local Observation capture is no longer eligible for inference")

                response = self._http.post("/api/chat", json={
                    "model": self.model,
                    "messages": [{"role": "system", "content": system}, message],
                    "format": schema,
                    "stream": False,
                    "think": False,
                    "options": {"temperature": 0, "num_predict": 512},
                }, extensions={"trace": trace} if send_permitted is not None or cancelled is not None else None,
                    headers={"Connection": "close"} if cancelled is not None else None)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict):
                report_usage(self._usage_observer, model=self.model,
                             prompt=data.get("prompt_eval_count"), completion=data.get("eval_count"))
            content = json.loads(data["message"]["content"])
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise OllamaError(f"Local model request failed ({type(exc).__name__})") from None
        if not isinstance(content, dict):
            raise OllamaError("Local model returned a non-object response")
        return content
