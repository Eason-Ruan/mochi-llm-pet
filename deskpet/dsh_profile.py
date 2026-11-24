"""Explicit SDK-only DeepSeek Harness compositions."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys


def _row(name: str, config: dict | None = None, **fields) -> dict:
    result = {"id": name, "name": "@deepseek-ai/dsh-" + name, **fields}
    if config is not None:
        result["config"] = config
    return result


def _write_profile(home: Path, name: str, entries: list[dict]) -> None:
    profile = home / "profiles" / name
    profile.mkdir(parents=True, mode=0o700)
    manifest = {"name": f"dsh-profile-{name}", "private": True, "dsh": {"profile": {"bundles": []}}}
    (profile / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
    # JSON is an unambiguous subset of the profile patch YAML grammar.
    (profile / "cordis.patch.yml").write_text(
        json.dumps([{"insert": entries}], ensure_ascii=False), encoding="utf-8",
    )


def _base_entries(
    profile: str, *, origin: str, model: str, official: bool, prompt: str,
    context_window: int = 32768, image_policy: dict | None = None,
) -> list[dict]:
    return [
        _row("sdk-app", {"profile": profile}),
        _row("sdk-jsonrpc-server", {"maxTokensAsSuccess": False}, inject=["sdkAppStartup", "loader"]),
        _row("session-projection"),
        {"id": "timer", "name": "@deepseek-ai/cordis-plugin-timer"},
        _row("llm"),
        _row("llm-pi-ai", {"providers": {"mochi": {
            "api": "openai-completions", "apiKeyEnv": "MOCHI_BRIDGE_TOKEN", "baseURL": origin + "/v1",
            "compat": {
                "supportsStore": False, "supportsDeveloperRole": False,
                "supportsReasoningEffort": False, "supportsStrictMode": False,
                "maxTokensField": "max_completion_tokens" if official else "max_tokens",
            },
            "retryPolicy": {"mode": "normal", "maxRetries": 0},
            **(image_policy or {}),
            "models": [{"id": model, "name": model, "contextWindow": context_window,
                        "maxTokens": 4096, "reasoningEfforts": False,
                        **({"input": ["text", "image"]} if image_policy else {})}],
        }}}),
        _row("session"),
        _row("system-prompt", {"includeHarnessIdentity": False, "includeRuntimeContext": False, "personaPrefix": prompt}),
        _row("tools"),
        _row("tool-executor-native"),
        _row("mcp-resources"),
        _row("agent"),
        _row("jobs-local"),
        _row("agent-loop", {"agents": []}),
    ]


def write_system2_profile(
    home: Path, *, origin: str, model: str, official: bool,
    prompt: str, tools: list[dict], port_file: Path, sessions: Path, bailian: bool = False,
) -> None:
    entries = _base_entries(
        "mochi", origin=origin, model=model, official=official, prompt=prompt,
        context_window=1_000_000 if bailian else 32768,
    )
    entries += [
        _row("session-persistence-jsonl", {"root": str(sessions), "compression": "none"}),
        _row("session-checkpoint-policy"),
        {
            "id": "mochi-bridge", "name": str(Path(__file__).with_name("system2") / "dsh_bridge.mjs"),
            "config": {"origin": origin, "portFile": str(port_file), "tools": tools},
        },
    ]
    _write_profile(home, "mochi", entries)


def write_observation_profile(
    home: Path, *, origin: str, model: str, prompt: str, width: int, height: int, max_bytes: int,
) -> None:
    name = "mochi-observation"
    image_policy = {
        "maxRequestImages": 1, "maxRequestImageBytes": ((max_bytes + 2) // 3) * 4,
        "requestImagePixelBudget": width * height, "requestImageMaxBytes": max_bytes,
    }
    entries = _base_entries(
        name, origin=origin, model=model, official=False, prompt=prompt, image_policy=image_policy,
    )
    entries.append({
        "id": "mochi-observation-image",
        "name": str(Path(__file__).with_name("observation_v2") / "dsh_image.mjs"),
        "config": {"width": width, "height": height, "maxBytes": max_bytes},
    })
    _write_profile(home, name, entries)


def isolated_environment(home: Path, token: str) -> dict[str, str]:
    temporary = home / "tmp"
    temporary.mkdir(mode=0o700)
    isolated = {
        "DSH_HOME": str(home), "DSH_TELEMETRY_DISABLED": "1", "MOCHI_BRIDGE_TOKEN": token,
        "HOME": str(home), "TMPDIR": str(temporary),
        "PATH": "/usr/bin:/bin",
    }
    if sys.platform == "win32":
        system = os.environ.get("SystemRoot", r"C:\Windows")
        isolated.update(
            SYSTEMROOT=system, WINDIR=system, PATH=str(Path(system) / "System32"),
            USERPROFILE=str(home), TEMP=str(temporary), TMP=str(temporary),
            APPDATA=str(home), LOCALAPPDATA=str(home),
        )
    return isolated
