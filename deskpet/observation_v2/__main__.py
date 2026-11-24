"""Run Observation v2 without starting Mochi's UI."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import asdict, replace
from datetime import UTC, datetime
from getpass import getpass
import json
import math
from pathlib import Path
import sqlite3
from sys import platform
from threading import Event
from uuid import uuid4

from .context import ObservationContextReader
from .dsh import ObservationDSH
from .ollama import DEFAULT_ENDPOINT, OllamaClient
from .openai_cloud import (
    RoutedObservationModel, delete_route_credential, store_route_credential, verify_cloud_route,
)
from .pipeline import (
    LIVE_UPDATE_INTERVAL_S, run_live_multimodal, run_replay, run_replay_multimodal,
)
from .routes import (
    OFFICIAL_BASE_URL, OLLAMA, STATE, VISION, CloudRoute, ObservationCloudSettings, _normalize_base_url,
)
from .state import StateObserver


def _positive(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be positive and finite")
    return number


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _cloud_commands(commands) -> None:
    cloud = commands.add_parser("cloud", help="configure and verify cloud Observation recipients")
    cloud.add_argument("--cloud-settings", type=Path, required=True, help="local model settings SQLite path")
    cloud.add_argument("--user-scope", default="default")
    changes = cloud.add_subparsers(dest="cloud_command", required=True)
    configure = changes.add_parser("configure", help="select a role's receiving model and service")
    configure.add_argument("role", choices=(VISION, STATE))
    configure.add_argument("--provider", choices=("openai", "openai_compatible", OLLAMA), required=True)
    configure.add_argument("--base-url")
    configure.add_argument("--model", required=True)
    configure.add_argument("--display-name", required=True)
    configure.add_argument("--credential-ref", help="reference to an API key in the OS credential store")
    configure.add_argument("--timeout-s", type=_positive, default=90.0)
    verify = changes.add_parser("verify", help="test the model with synthetic inputs")
    verify.add_argument("role", choices=(VISION, STATE))
    consent = changes.add_parser("consent", help="grant or revoke the selected role's extra data access")
    consent.add_argument("role", choices=(VISION, STATE))
    consent.add_argument("permission", choices=("allow", "revoke"))
    consent.add_argument("--scope", choices=("default", "vision-signals"), default="default",
                         help="select the role's default data or structured vision inputs")
    consent.add_argument("--recipient", help="exact service base URL being authorized (required for allow)")
    clear = changes.add_parser("clear", help="restore a role to local Ollama")
    clear.add_argument("role", choices=(VISION, STATE))
    credential = changes.add_parser("credential", help="save an API key for the current service using a private prompt")
    credential.add_argument("role", choices=(VISION, STATE))
    changes.add_parser("status", help="show configured recipients, permissions, and capability check status")


def _manage_cloud(arguments: argparse.Namespace) -> None:
    settings = ObservationCloudSettings(arguments.cloud_settings, user_scope=arguments.user_scope)
    command = arguments.cloud_command
    if command == "status":
        _print_json([asdict(settings.snapshot(role)) for role in (VISION, STATE)])
        return
    snapshot = settings.snapshot(arguments.role)
    route = snapshot.route
    if command == "configure":
        base_url = arguments.base_url
        if not base_url:
            base_url = {"openai": OFFICIAL_BASE_URL, OLLAMA: DEFAULT_ENDPOINT}.get(arguments.provider)
        if not base_url:
            raise ValueError("An OpenAI-compatible endpoint needs an explicit base URL")
        credential_ref = arguments.credential_ref
        if not credential_ref and arguments.provider == "openai":
            credential_ref = uuid4().hex
        snapshot = settings.configure(arguments.role, CloudRoute(
            arguments.provider, base_url, arguments.model, arguments.display_name,
            credential_ref, arguments.timeout_s,
        ), expected=snapshot)
    elif command == "clear":
        snapshot = settings.clear(arguments.role, expected=snapshot)
    elif command == "verify":
        snapshot = verify_cloud_route(settings, arguments.role, expected=snapshot)
    elif command == "credential":
        if route is None or not route.is_cloud:
            raise ValueError("Configure a cloud recipient before entering a credential")
        route = replace(route, credential_ref=uuid4().hex)
        key = getpass(f"API key for {route.display_name} ({route.base_url}): ")
        store_route_credential(route, key)
        try:
            snapshot = settings.configure(arguments.role, route, expected=snapshot)
        except Exception:
            delete_route_credential(route)
            raise
    elif command == "consent":
        allow = arguments.permission == "allow"
        if route is None:
            raise ValueError("Configure the recipient before changing its permission")
        if allow and not arguments.recipient:
            raise ValueError("Provide --recipient with the service's base URL before granting permission")
        if arguments.recipient and _normalize_base_url(arguments.recipient) != route.base_url:
            raise ValueError("The configured recipient changed")
        if arguments.scope == "vision-signals":
            if arguments.role != VISION:
                raise ValueError("The vision-signals permission is available only for vision")
            snapshot = settings.set_vision_signals_permission(allow, expected=snapshot)
        else:
            snapshot = settings.set_permission(arguments.role, allow, expected=snapshot)
    _print_json(asdict(snapshot))


def _desktop_policies(arguments: argparse.Namespace, stop_event: Event | None):
    from deskpet.settings import OBSERVATION_POLICY_READ_TIMEOUT_S, SettingsStore

    def preferences():
        if stop_event is not None and stop_event.is_set():
            return None
        try:
            settings = SettingsStore.read_at(
                arguments.cloud_settings, user_scope=arguments.user_scope, timeout=OBSERVATION_POLICY_READ_TIMEOUT_S,
            )
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            return None
        if (
            (stop_event is not None and stop_event.is_set())
            or settings.observation_paused
            or (arguments.observation_generation is not None
                and settings.observation_generation != arguments.observation_generation)
            or settings.observation_browser_metadata != arguments.browser_metadata
            or settings.observation_excluded_apps != tuple(arguments.exclude_app)
            or settings.observation_excluded_domains != tuple(arguments.exclude_domain)
        ):
            return None
        return settings

    def request_policy() -> int | None:
        settings = preferences()
        return None if settings is None else settings.observation_generation

    def capture_policy() -> int | None:
        settings = preferences()
        if settings is None or not settings.observation_screenshots:
            return None
        return settings.observation_generation

    preferences_at_start = preferences()
    if preferences_at_start is None:
        raise ValueError("Could not confirm the desktop Observation policy")
    if arguments.observation_generation is None:
        arguments.observation_generation = preferences_at_start.observation_generation
    return (
        request_policy, capture_policy, preferences_at_start.document_local_sources,
        preferences_at_start.observation_screen_scope,
    )


def main(*, stop_event: Event | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--database", type=Path, required=True, help="SQLite path (a new file for replay)")
    common.add_argument("--cloud-settings", type=Path, help="optional local SQLite path for model routes and consent")
    common.add_argument("--user-scope", default="default")
    common.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="default loopback Ollama origin")
    common.add_argument("--vision-endpoint", help="independent loopback Ollama vision origin")
    common.add_argument("--vision-model", default="qwen3-vl:2b-instruct")
    common.add_argument("--timeout-s", type=_positive, default=90.0, help="model request timeout ceiling")
    common.add_argument("--window-s", type=_positive, default=60.0)
    common.add_argument("--ttl-s", type=_positive, default=15.0)
    commands = parser.add_subparsers(dest="mode", required=True)
    _cloud_commands(commands)
    replay = commands.add_parser("replay", parents=[common], help="replay a local screen recording")
    replay.add_argument("recording", type=Path)
    replay.add_argument("--state-endpoint", help="independent loopback Ollama state origin")
    replay.add_argument("--state-model", default="qwen2.5:1.5b")
    replay.add_argument("--interval-s", type=_positive, default=4.0)
    replay.add_argument("--max-frames", type=int, default=32)
    replay.add_argument("--multimodal", action="store_true", help="use adaptive sampling and a local Ollama model")
    replay.add_argument("--require-models", action="store_true", help="require results for every model sample")
    live = commands.add_parser("live", parents=[common], help="observe native desktop activity with DSH vision")
    duration = live.add_mutually_exclusive_group()
    duration.add_argument("--duration-s", type=_positive, default=30.0)
    duration.add_argument("--until-stopped", action="store_const", const=None, dest="duration_s",
                          help="collect until stopped by the desktop or interrupted")
    live.add_argument(
        "--model-interval-s", type=_positive, default=LIVE_UPDATE_INTERVAL_S,
        help="interval between periodic local updates (default: %(default)s seconds)",
    )
    live.add_argument("--split-models", action="store_true", help=argparse.SUPPRESS)
    live.add_argument("--vision-wait-s", type=_positive, help=argparse.SUPPRESS)
    live.add_argument("--observation-generation", type=int, help=argparse.SUPPRESS)
    live.add_argument("--browser-metadata", action="store_true", help="enable supported macOS browser page detection")
    live.add_argument("--exclude-app", action="append", default=[],
                      help="exclude an application name or available bundle ID substring")
    live.add_argument("--exclude-domain", action="append", default=[], help="exclude a detected domain and its subdomains")
    capture = live.add_mutually_exclusive_group()
    capture.add_argument("--capture-screen", action="store_true", help="opt into screen image capture")
    capture.add_argument("--capture-screen-from-settings", action="store_true",
                         help="use the live desktop's stored screenshot permission and scope")
    live.add_argument("--screen-scope", choices=("foreground-window", "all-displays"),
                      help="multimodal scope for --capture-screen (foreground-window by default, all-displays needs macOS 14.2+)")
    context = commands.add_parser("context", help="read the System 2 Observation snapshot")
    context.add_argument("--database", type=Path, required=True)
    context.add_argument("--as-of", type=datetime.fromisoformat, help="ISO timestamp with UTC offset")
    context.add_argument("--cloud-settings", type=Path, help="desktop settings path for a managed live database")
    arguments = parser.parse_args()
    if arguments.mode == "cloud":
        try:
            _manage_cloud(arguments)
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            parser.exit(2, f"Observation settings could not be applied ({exc}).\n")
        return
    if arguments.mode == "context":
        try:
            reader = ObservationContextReader(arguments.database)
            if arguments.as_of is not None:
                result = reader.read_observation_context(arguments.as_of)
            elif arguments.cloud_settings is not None:
                from deskpet.m0.observation_input import DesktopObservationInput

                result = DesktopObservationInput(
                    arguments.database, settings_path=arguments.cloud_settings,
                ).read_observation_context(datetime.now(UTC))
            else:
                result = reader.read_realtime_context()
            _print_json(result.to_dict())
        except (OSError, ValueError, sqlite3.Error) as exc:
            parser.exit(2, f"Could not read Observation context ({exc}).\n")
        return
    request_policy = capture_policy = None
    document_local_sources = ()
    screen_scope = "foreground_window"
    split_models = arguments.mode == "replay" and not arguments.multimodal
    if arguments.mode == "live":
        if arguments.split_models or arguments.vision_wait_s is not None:
            parser.error("Live Observation uses DSH. Use replay without --multimodal for the two-model comparison.")
        if arguments.screen_scope is not None:
            if not arguments.capture_screen:
                parser.error("--screen-scope requires --capture-screen")
            screen_scope = arguments.screen_scope.replace("-", "_")
            if screen_scope == "all_displays" and platform != "darwin":
                parser.error("--screen-scope all-displays requires macOS 14.2 or later")
        if arguments.observation_generation is not None and (
            arguments.observation_generation < 0 or not arguments.capture_screen_from_settings
        ):
            parser.error("--observation-generation requires --capture-screen-from-settings and a nonnegative version")
        if arguments.capture_screen_from_settings:
            if arguments.cloud_settings is None:
                parser.error("--capture-screen-from-settings requires --cloud-settings")
            try:
                request_policy, capture_policy, document_local_sources, screen_scope = _desktop_policies(
                    arguments, stop_event,
                )
            except ValueError as exc:
                parser.exit(2, f"Observation could not start ({exc}).\n")
    if arguments.mode == "replay" and arguments.cloud_settings and arguments.database.resolve() == arguments.cloud_settings.resolve():
        parser.error("replay requires a database separate from model settings")

    vision_endpoint = arguments.vision_endpoint or arguments.endpoint
    if split_models:
        state_endpoint = arguments.state_endpoint or arguments.endpoint
        if (arguments.cloud_settings is None and arguments.vision_model == arguments.state_model
                and vision_endpoint == state_endpoint):
            parser.error("choose distinct vision and state models for the same Ollama endpoint")
    timeout = min(arguments.timeout_s, arguments.ttl_s) if arguments.mode == "live" else arguments.timeout_s
    try:
        with ExitStack() as stack:
            vision = stack.enter_context(OllamaClient(arguments.vision_model, endpoint=vision_endpoint, timeout=timeout))
            text = (stack.enter_context(OllamaClient(arguments.state_model, endpoint=state_endpoint, timeout=timeout))
                    if split_models else None)
            settings = (
                ObservationCloudSettings(arguments.cloud_settings, user_scope=arguments.user_scope)
                if arguments.cloud_settings is not None else None
            )
            vision_runtime = None
            if arguments.mode == "live":
                vision_runtime = ObservationDSH()
                stack.callback(vision_runtime.close)
            if settings is not None or vision_runtime is not None:
                vision = RoutedObservationModel(
                    VISION, settings, vision, timeout_limit=timeout, local_vision_only=arguments.mode == "replay",
                    request_policy=request_policy, vision_runtime=vision_runtime,
                )
            if settings is not None and text is not None:
                text = RoutedObservationModel(STATE, settings, text, timeout_limit=timeout, request_policy=request_policy)
            observer = StateObserver(text, window_seconds=arguments.window_s, ttl_seconds=arguments.ttl_s)
            if arguments.mode == "replay":
                if split_models:
                    report = run_replay(
                        arguments.recording, arguments.database, vision, observer,
                        interval_s=arguments.interval_s, max_frames=arguments.max_frames,
                    )
                else:
                    report = run_replay_multimodal(
                        arguments.recording, arguments.database, vision, observer=observer,
                        interval_s=arguments.interval_s, max_frames=arguments.max_frames,
                    )
            else:
                live_options = dict(
                    duration_s=arguments.duration_s,
                    capture_screen=arguments.capture_screen or arguments.capture_screen_from_settings,
                    browser_metadata=arguments.browser_metadata,
                    model_interval_s=arguments.model_interval_s, excluded_apps=tuple(arguments.exclude_app),
                    excluded_domains=tuple(arguments.exclude_domain),
                    capture_policy=capture_policy, stop_event=stop_event,
                    producer_policy=request_policy, policy_generation=arguments.observation_generation,
                )
                report = run_live_multimodal(
                    arguments.database, vision, observer=observer, screen_scope=screen_scope,
                    document_local_sources=document_local_sources, close_vision=vision_runtime.close,
                    **live_options,
                )
    except FileExistsError:
        parser.exit(2, "Observation database already exists. Choose a new path.\n")
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        parser.exit(2, f"Observation could not complete ({exc}).\n")
    _print_json(report)
    if arguments.mode == "replay" and arguments.require_models:
        expected = report["activities"] if split_models else report["visual_attempts"]
        if (expected == 0 or report["screens"].get("ready", 0) != expected
                or report["state_runs"].get("model", 0) != expected):
            parser.exit(1, "Replay did not produce a valid result for every model sample.\n")


if __name__ == "__main__":
    main()
