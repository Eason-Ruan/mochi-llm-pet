"""Persist recipient-specific Observation routes and send authorizations."""

from __future__ import annotations

from contextlib import closing, contextmanager, nullcontext
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import sqlite3
from typing import Literal

import httpx

from .ollama import validate_local_model

VISION = "observation_vision"
STATE = "observation_state"
SYSTEM2 = "system2"
OLLAMA = "local_ollama"
BAILIAN = "bailian"
Role = Literal["observation_vision", "observation_state", "system2"]
OFFICIAL_BASE_URL = "https://api.openai.com/v1"
_BAILIAN_PATH = "/compatible-mode/v1"
BAILIAN_BASE_URL = "https://dashscope.aliyuncs.com" + _BAILIAN_PATH
BAILIAN_MODEL = "qwen4-max"
_BAILIAN_HOSTS = {
    "dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com",
    "dashscope-us.aliyuncs.com", "cn-hongkong.dashscope.aliyuncs.com",
}
_BAILIAN_WORKSPACE_HOST = re.compile(
    r"(?!trial\.|token-plan\.)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\."
    r"(?:cn-beijing|ap-southeast-1|ap-northeast-1|eu-central-1|us-east-1|cn-hongkong)\.maas\.aliyuncs\.com"
)
_CAPABILITY_PROBE_VERSION = "openai-1.109.1-chat-json-schema-v1"
_SYSTEM2_PROBE_VERSION = "dsh-0.2.2rc2-native-tools-v1"
_SAFE_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


def _capability_probe_version(role: Role) -> str:
    return _SYSTEM2_PROBE_VERSION if role == SYSTEM2 else _CAPABILITY_PROBE_VERSION


class CloudRouteError(RuntimeError):
    """A cloud operation failed without disclosing credentials or model content."""


def _loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def _normalize_base_url(value: str) -> str:
    try:
        url = httpx.URL(value)
    except (httpx.InvalidURL, TypeError, ValueError):
        raise ValueError("Invalid model service URL") from None
    if (
        not url.host or url.userinfo or url.query or url.fragment
        or "?" in value or "#" in value or "@" in value or "%" in url.path
        or any(char.isspace() or char == "\\" for char in value)
        or (url.scheme != "https" and (url.scheme != "http" or not _loopback(url.host)))
    ):
        raise ValueError("Use an HTTPS model service URL, or HTTP for a loopback service")
    return str(url.copy_with(path=url.path.rstrip("/"))).rstrip("/")


@dataclass(frozen=True, slots=True)
class CloudRoute:
    provider_kind: Literal["openai", "openai_compatible", "local_ollama", "bailian"]
    base_url: str
    model: str
    display_name: str
    credential_ref: str | None = None
    timeout_s: float = 90.0

    def __post_init__(self) -> None:
        if self.provider_kind not in {"openai", "openai_compatible", OLLAMA, BAILIAN}:
            raise ValueError("Unsupported model provider")
        object.__setattr__(self, "base_url", _normalize_base_url(self.base_url))
        if self.provider_kind == "openai" and self.base_url != OFFICIAL_BASE_URL:
            raise ValueError("The OpenAI provider requires the official OpenAI endpoint")
        if not all(isinstance(value, str) and value.strip() and len(value) <= 128
                   and not any(ord(char) < 32 or ord(char) == 127 for char in value)
                   for value in (self.model, self.display_name)):
            raise ValueError("A model and service display name are required")
        if not math.isfinite(self.timeout_s) or self.timeout_s <= 0:
            raise ValueError("Cloud timeout must be positive and finite")
        if self.credential_ref is not None and not _SAFE_REF.fullmatch(self.credential_ref):
            raise ValueError("Invalid local credential reference")
        if self.credential_ref is None and self.provider_kind == "openai":
            raise ValueError("The OpenAI route requires a local credential reference")
        if self.provider_kind == BAILIAN:
            url = httpx.URL(self.base_url)
            if (url.scheme != "https" or url.port is not None or url.path != _BAILIAN_PATH
                    or (url.host not in _BAILIAN_HOSTS and not _BAILIAN_WORKSPACE_HOST.fullmatch(url.host))):
                raise ValueError("Use an official Alibaba Cloud Model Studio HTTPS Chat Completions base URL")
            if self.model != BAILIAN_MODEL or self.credential_ref is None:
                raise ValueError("The Alibaba Cloud Model Studio route requires qwen4-max and a local credential reference")

        if self.provider_kind == OLLAMA:
            url = httpx.URL(self.base_url)
            if not _loopback(url.host) or url.path != "/" or self.credential_ref is not None:
                raise ValueError("Local Ollama requires a loopback origin and no credential")
            validate_local_model(self.model)

    @property
    def is_cloud(self) -> bool:
        return self.provider_kind != OLLAMA

    @property
    def recipient(self) -> tuple[str, str]:
        return self.provider_kind, self.base_url


@dataclass(frozen=True, slots=True)
class CloudRouteSnapshot:
    role: Role
    user_scope: str
    route: CloudRoute | None = None
    route_generation: int = 0
    consent_generation: int = 0
    permission: bool = False
    verified: bool = False
    last_success: str | None = None
    last_failure: str | None = None


@contextmanager
def observation_privacy_gate(settings_path: Path | str):
    """Serialize sensitive sends and edits without locking the application's database."""
    path = Path(settings_path).resolve()
    gate_path = path.with_name(path.name + ".observation-privacy-lock")
    descriptor = os.open(gate_path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(descriptor)
    with closing(sqlite3.connect(gate_path, timeout=3.0)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        yield


class ObservationCloudSettings:
    """Independent per-role settings shared by the host and Observation workers."""

    def __init__(self, path: Path | str, *, user_scope: str = "default") -> None:
        if not isinstance(user_scope, str) or not user_scope.strip():
            raise ValueError("A user scope is required")
        self.path = Path(path).resolve()
        self.user_scope = user_scope
        descriptor = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(descriptor)
        with closing(self._connect()) as connection, connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS observation_cloud_routes (
                    user_scope TEXT NOT NULL,
                    role TEXT NOT NULL,
                    route TEXT,
                    route_generation INTEGER NOT NULL,
                    consent_generation INTEGER NOT NULL,
                    permission INTEGER NOT NULL,
                    verified TEXT,
                    last_success TEXT,
                    last_failure TEXT,
                    PRIMARY KEY (user_scope, role)
                )
            """)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=3.0)

    def _get(self, connection: sqlite3.Connection, role: Role) -> CloudRouteSnapshot:
        if role not in {VISION, STATE, SYSTEM2}:
            raise ValueError("Unknown Observation role")
        row = connection.execute("""
            SELECT route, route_generation, consent_generation, permission, verified,
                   last_success, last_failure
            FROM observation_cloud_routes WHERE user_scope = ? AND role = ?
        """, (self.user_scope, role)).fetchone()
        if row is None:
            return CloudRouteSnapshot(role, self.user_scope)
        route, generation, consent, permission, verified, success, failure = row
        return CloudRouteSnapshot(
            role, self.user_scope, CloudRoute(**json.loads(route)) if route else None,
            generation, consent, bool(permission), verified == _capability_probe_version(role),
            success, failure,
        )

    @staticmethod
    def _require_current(current: CloudRouteSnapshot, expected: CloudRouteSnapshot) -> None:
        if (
            current.user_scope != expected.user_scope or current.role != expected.role
            or current.route != expected.route or current.route_generation != expected.route_generation
            or current.consent_generation != expected.consent_generation
            or current.permission != expected.permission
        ):
            raise CloudRouteError("Observation service authorization changed")

    @contextmanager
    def _write(self):
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            yield connection

    def snapshot(self, role: Role) -> CloudRouteSnapshot:
        with closing(self._connect()) as connection:
            return self._get(connection, role)

    def configure(
        self, role: Role, route: CloudRoute | None, *, expected: CloudRouteSnapshot | None = None,
    ) -> CloudRouteSnapshot:
        if route is not None and route.provider_kind == BAILIAN and role != SYSTEM2:
            raise ValueError("Alibaba Cloud Model Studio is available for System 2 only")
        with observation_privacy_gate(self.path), self._write() as connection:
            current = self._get(connection, role)
            if expected is not None:
                self._require_current(current, expected)
            if current.route == route:
                return current
            # Even a change to a model or credential invalidates the queued authorization.
            connection.execute("""
                INSERT OR REPLACE INTO observation_cloud_routes
                VALUES (?, ?, ?, ?, ?, 0, NULL, NULL, NULL)
            """, (
                self.user_scope, role, json.dumps(asdict(route)) if route else None,
                current.route_generation + 1, current.consent_generation + 1,
            ))
            return self._get(connection, role)

    def clear(self, role: Role, *, expected: CloudRouteSnapshot) -> CloudRouteSnapshot:
        return self.configure(role, None, expected=expected)

    def set_permission(
        self, role: Role, allow: bool, *, expected: CloudRouteSnapshot,
    ) -> CloudRouteSnapshot:
        if type(allow) is not bool:
            raise ValueError("Observation permission must be enabled or disabled")
        with observation_privacy_gate(self.path), self._write() as connection:
            current = self._get(connection, role)
            self._require_current(current, expected)
            if current.route is None:
                raise CloudRouteError("Configure the recipient before changing its permission")
            if allow and not current.route.is_cloud:
                raise CloudRouteError("Local models do not require cloud permission")
            if current.permission != allow:
                connection.execute("""
                    UPDATE observation_cloud_routes
                    SET consent_generation = consent_generation + 1, permission = ?
                    WHERE user_scope = ? AND role = ?
                """, (allow, self.user_scope, role))
            return self._get(connection, role)

    @contextmanager
    def admit(self, expected: CloudRouteSnapshot, *, probe: bool = False):
        """Serialize request admission and upload against settings commits."""
        with observation_privacy_gate(self.path):
            self.check(expected, probe=probe)
            yield

    def check(self, expected: CloudRouteSnapshot, *, probe: bool = False) -> None:
        self._require_send(self.snapshot(expected.role), expected, probe=probe)

    def _require_send(
        self, current: CloudRouteSnapshot, expected: CloudRouteSnapshot, *, probe: bool,
    ) -> None:
        self._require_current(current, expected)
        if current.route is None or not (probe or current.verified):
            raise CloudRouteError("Verify the configured Observation model before using it")
        if current.role == VISION and current.route.is_cloud and not current.permission:
            raise CloudRouteError("Window image permission is disabled for this recipient")

    def record_result(
        self, expected: CloudRouteSnapshot, *, probe: bool = False, failure: str | None = None,
    ) -> CloudRouteSnapshot:
        gate = observation_privacy_gate(self.path) if probe else nullcontext()
        with gate, self._write() as connection:
            current = self._get(connection, expected.role)
            self._require_current(current, expected)
            if probe:
                connection.execute("""
                    UPDATE observation_cloud_routes SET verified = ?
                    WHERE user_scope = ? AND role = ?
                """, (None if failure else _capability_probe_version(expected.role), self.user_scope, expected.role))
            if failure:
                connection.execute("""
                    UPDATE observation_cloud_routes SET last_failure = ?
                    WHERE user_scope = ? AND role = ?
                """, (failure, self.user_scope, expected.role))
            else:
                connection.execute("""
                    UPDATE observation_cloud_routes SET last_success = ?, last_failure = NULL
                    WHERE user_scope = ? AND role = ?
                """, (datetime.now(UTC).isoformat(), self.user_scope, expected.role))
            current = self._get(connection, expected.role)
            if not failure:
                self._require_send(current, expected, probe=probe)
            return current
