"""Shared per-role model configuration for the desktop and terminal."""

from collections.abc import Mapping
from dataclasses import dataclass, field
import sqlite3
from uuid import uuid4

import httpx

from .i18n import tr
from .observation_v2.openai_cloud import (
    _resolve_credential, delete_route_credential, store_route_credential, verify_cloud_route,
)
from .observation_v2.routes import (
    BAILIAN, BAILIAN_BASE_URL, BAILIAN_MODEL, BAILIAN_OBSERVATION_MODEL, OFFICIAL_BASE_URL, OLLAMA,
    SYSTEM2, VISION,
    CloudRoute, CloudRouteSnapshot, ObservationCloudSettings, Role,
    _loopback, _normalize_base_url, is_bailian_endpoint,
)

MODEL_ROLES: tuple[Role, ...] = (SYSTEM2, VISION)
_ROLE_MODELS = {SYSTEM2: BAILIAN_MODEL, VISION: BAILIAN_OBSERVATION_MODEL}


@dataclass(frozen=True, slots=True)
class ModelInput:
    base_url: str
    model: str
    api_key: str = field(default="", repr=False)
    permission: bool | None = None


def bailian_display_name() -> str:
    return tr("阿里云百炼", "Alibaba Cloud Model Studio")


def model_setup_defaults(routes: ObservationCloudSettings) -> dict[Role, CloudRouteSnapshot]:
    return {role: routes.snapshot(role) for role in MODEL_ROLES}


def model_input_defaults(snapshot: CloudRouteSnapshot) -> tuple[str, str]:
    if snapshot.route is not None:
        return snapshot.route.base_url, snapshot.route.model
    return BAILIAN_BASE_URL, _ROLE_MODELS[snapshot.role]


def can_reuse_api_key(snapshot: CloudRouteSnapshot, base_url: str) -> bool:
    """A blank input keeps an existing credential, or an existing keyless route."""
    try:
        endpoint = _normalize_base_url(base_url.strip())
    except ValueError:
        return False
    return _same_endpoint(snapshot, endpoint) is not None or _loopback(httpx.URL(endpoint).host)


def _same_endpoint(snapshot: CloudRouteSnapshot, endpoint: str) -> CloudRoute | None:
    route = snapshot.route
    return route if route is not None and route.base_url == endpoint else None


def _provider(role: Role, endpoint: str, model: str, saved: CloudRoute | None, key: str) -> str:
    if saved is not None and saved.provider_kind == OLLAMA and not key:
        return OLLAMA
    if saved is not None and saved.model == model and saved.provider_kind != OLLAMA:
        return saved.provider_kind
    if role == SYSTEM2 and model == BAILIAN_MODEL and is_bailian_endpoint(endpoint):
        return BAILIAN
    if endpoint == OFFICIAL_BASE_URL:
        return "openai"
    return "openai_compatible"


def _prepare_route(snapshot: CloudRouteSnapshot, value: ModelInput) -> tuple[CloudRoute, str]:
    endpoint = _normalize_base_url(value.base_url.strip())
    model, key = value.model.strip(), value.api_key.strip()
    if key and any(not 33 <= ord(char) <= 126 for char in key):
        raise ValueError("密钥含有空格或无效字符，请重新输入。")
    saved = _same_endpoint(snapshot, endpoint)
    if not key and saved is None and not _loopback(httpx.URL(endpoint).host):
        raise ValueError("请为这个服务地址输入自己的 API Key。")
    provider = _provider(snapshot.role, endpoint, model, saved, key)
    reference = saved.credential_ref if saved is not None else None
    if not key and reference is not None and saved.provider_kind != provider:
        # Only rebind an existing key when the normalized recipient URL is identical.
        key = _resolve_credential(saved)
    name = bailian_display_name() if is_bailian_endpoint(endpoint) else httpx.URL(endpoint).host
    route = CloudRoute(
        provider, endpoint, model, saved.display_name if saved is not None else name,
        uuid4().hex if key else reference, saved.timeout_s if saved is not None else 90.0,
    )
    return route, key


def configure_models(
    routes: ObservationCloudSettings,
    updates: Mapping[Role, ModelInput],
    *,
    expected: Mapping[Role, CloudRouteSnapshot] | None = None,
) -> dict[Role, CloudRouteSnapshot]:
    """Save independently configured routes to the profile's single configuration file."""
    if not updates or not set(updates).issubset(MODEL_ROLES):
        raise ValueError("请选择要配置的模型用途。")
    snapshots = {role: routes.snapshot(role) if expected is None else expected[role] for role in updates}
    prepared = {role: _prepare_route(snapshots[role], value) for role, value in updates.items()}
    new_credentials = []
    try:
        for route, key in prepared.values():
            if key:
                new_credentials.append(route)
                store_route_credential(route, key)
        return routes.configure_many(
            {role: (route, updates[role].permission) for role, (route, _) in prepared.items()},
            expected=snapshots,
        )
    except (Exception, KeyboardInterrupt):
        for route in new_credentials:
            delete_route_credential(route)
        raise


def verify_models(
    routes: ObservationCloudSettings, configured: Mapping[Role, CloudRouteSnapshot],
) -> dict[Role, str]:
    """Verify only the saved routes using the existing synthetic probes."""
    results = {}
    for role, snapshot in configured.items():
        if role == VISION and snapshot.route is not None and snapshot.route.is_cloud and not snapshot.permission:
            results[role] = "permission_required"
            continue
        try:
            if role == SYSTEM2:
                from .system2.dsh import verify_system2_route

                result = verify_system2_route(routes, expected=snapshot)
            else:
                result = verify_cloud_route(routes, role, expected=snapshot)
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            results[role] = "failed"
        else:
            results[role] = "verified" if result.verified else "failed"
    return results
