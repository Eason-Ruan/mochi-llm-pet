"""Run Observation v2 without starting Mochi's UI."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import math
from pathlib import Path
import sqlite3

from .context import ObservationContextReader
from .ollama import DEFAULT_ENDPOINT, OllamaClient
from .pipeline import LIVE_UPDATE_INTERVAL_S, run_live, run_replay
from .state import StateObserver


def _positive(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be positive and finite")
    return number


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--database", type=Path, required=True, help="SQLite path (a new file for replay)")
    common.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="loopback Ollama origin")
    common.add_argument("--vision-model", default="qwen3-vl:2b-instruct")
    common.add_argument("--state-model", default="qwen2.5:1.5b")
    common.add_argument("--timeout-s", type=_positive, default=90.0, help="local model request timeout")
    common.add_argument("--window-s", type=_positive, default=60.0)
    common.add_argument("--ttl-s", type=_positive, default=15.0)
    commands = parser.add_subparsers(dest="mode", required=True)
    replay = commands.add_parser("replay", parents=[common], help="replay a local screen recording")
    replay.add_argument("recording", type=Path)
    replay.add_argument("--interval-s", type=_positive, default=4.0)
    replay.add_argument("--max-frames", type=int, default=32)
    replay.add_argument("--require-models", action="store_true", help="fail if any VLM or LLM result was unavailable")
    live = commands.add_parser("live", parents=[common], help="observe native desktop activity")
    live.add_argument("--duration-s", type=_positive, default=30.0)
    live.add_argument(
        "--model-interval-s", type=_positive, default=LIVE_UPDATE_INTERVAL_S,
        help="minimum interval between Observation update rounds (default: %(default)s seconds)",
    )
    live.add_argument("--browser-metadata", action="store_true", help="enable supported macOS browser page detection")
    live.add_argument("--exclude-app", action="append", default=[], help="exclude an application name substring")
    live.add_argument("--exclude-domain", action="append", default=[], help="exclude a detected domain and its subdomains")
    live.add_argument("--capture-screen", action="store_true", help="opt into local primary-monitor capture")
    context = commands.add_parser("context", help="read the System 2 Observation snapshot")
    context.add_argument("--database", type=Path, required=True)
    context.add_argument("--as-of", type=datetime.fromisoformat, help="ISO timestamp with UTC offset")
    arguments = parser.parse_args()
    if arguments.mode == "context":
        try:
            reader = ObservationContextReader(arguments.database)
            result = reader.read_observation_context(arguments.as_of or datetime.now(UTC))
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        except (OSError, ValueError, sqlite3.Error) as exc:
            parser.exit(2, f"Could not read Observation context ({exc}).\n")
        return
    if arguments.vision_model == arguments.state_model:
        parser.error("choose distinct vision and state models")
    timeout = arguments.timeout_s
    if arguments.mode == "live":
        timeout = min(timeout, arguments.ttl_s)
    try:
        with (
            OllamaClient(arguments.vision_model, endpoint=arguments.endpoint, timeout=timeout) as vision,
            OllamaClient(arguments.state_model, endpoint=arguments.endpoint, timeout=timeout) as text,
        ):
            observer = StateObserver(text, window_seconds=arguments.window_s, ttl_seconds=arguments.ttl_s)
            if arguments.mode == "replay":
                report = run_replay(
                    arguments.recording, arguments.database, vision, observer,
                    interval_s=arguments.interval_s, max_frames=arguments.max_frames,
                )
            else:
                report = run_live(
                    arguments.database, vision, observer, duration_s=arguments.duration_s,
                    capture_screen=arguments.capture_screen, browser_metadata=arguments.browser_metadata,
                    model_interval_s=arguments.model_interval_s, excluded_apps=tuple(arguments.exclude_app),
                    excluded_domains=tuple(arguments.exclude_domain),
                )
    except FileExistsError:
        parser.exit(2, "Observation database already exists. Choose a new path.\n")
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        parser.exit(2, f"Observation could not complete ({exc}).\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if arguments.mode == "replay" and arguments.require_models and (
        report["screens"].get("ready", 0) != report["activities"]
        or report["state_runs"].get("model", 0) != report["activities"]
    ):
        parser.exit(1, "Replay did not produce a valid result from both models for every frame.\n")


if __name__ == "__main__":
    main()
