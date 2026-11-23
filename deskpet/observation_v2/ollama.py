"""Structured requests to a local Ollama endpoint."""

from __future__ import annotations

import base64
import ipaddress
import json
import math
from urllib.parse import urlsplit

import httpx

DEFAULT_ENDPOINT = "http://127.0.0.1:11434"


class OllamaError(RuntimeError):
    """Model request failed without including request or response contents."""


class OllamaClient:
    def __init__(self, model: str, *, endpoint: str = DEFAULT_ENDPOINT, timeout: float = 90.0):
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
        if not model.strip() or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("A model and a positive finite timeout are required")
        model_parts = model.casefold().split("/")[-1].split(":", 1)
        if any(part == "cloud" or part.endswith("-cloud") for part in model_parts):
            raise ValueError("Hosted Ollama models are unsupported")
        self.model = model
        self._http = httpx.Client(
            base_url=endpoint, timeout=timeout, trust_env=False, follow_redirects=False,
        )

    def __enter__(self) -> OllamaClient:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self._http.close()

    def complete(
        self, *, system: str, prompt: str, schema: dict, image: bytes | None = None,
    ) -> dict:
        message = {"role": "user", "content": prompt}
        if image is not None:
            message["images"] = [base64.b64encode(image).decode("ascii")]
        try:
            response = self._http.post("/api/chat", json={
                "model": self.model,
                "messages": [{"role": "system", "content": system}, message],
                "format": schema,
                "stream": False,
                "think": False,
                "options": {"temperature": 0, "num_predict": 512},
            })
            response.raise_for_status()
            content = json.loads(response.json()["message"]["content"])
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise OllamaError(f"Local model request failed ({type(exc).__name__})") from None
        if not isinstance(content, dict):
            raise OllamaError("Local model returned a non-object response")
        return content
