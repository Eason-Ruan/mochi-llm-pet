"""The explicit, SDK-only DeepSeek Harness composition for System 2."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys


def write_profile(
    home: Path, *, origin: str, model: str, official: bool,
    prompt: str, tools: list[dict], port_file: Path, sessions: Path,
) -> None:
    profile = home / "profiles" / "mochi"
    profile.mkdir(parents=True, mode=0o700)
    manifest = {"name": "dsh-profile-mochi", "private": True, "dsh": {"profile": {"bundles": []}}}
    (profile / "package.json").write_text(json.dumps(manifest), encoding="utf-8")

    def row(name: str, config: dict | None = None, **fields) -> dict:
        result = {"id": name, "name": "@deepseek-ai/dsh-" + name, **fields}
        if config is not None:
            result["config"] = config
        return result

    entries = [
        row("sdk-app", {"profile": "mochi"}),
        row("sdk-jsonrpc-server", {"maxTokensAsSuccess": False}, inject=["sdkAppStartup", "loader"]),
        row("session-projection"),
        {"id": "timer", "name": "@deepseek-ai/cordis-plugin-timer"},
        row("llm"),
        row("llm-pi-ai", {"providers": {"mochi": {
            "api": "openai-completions", "apiKeyEnv": "MOCHI_BRIDGE_TOKEN", "baseURL": origin + "/v1",
            "compat": {
                "supportsStore": False, "supportsDeveloperRole": False,
                "supportsReasoningEffort": False, "supportsStrictMode": False,
                "maxTokensField": "max_completion_tokens" if official else "max_tokens",
            },
            "retryPolicy": {"mode": "normal", "maxRetries": 0},
            "models": [{"id": model, "name": model, "contextWindow": 32768,
                        "maxTokens": 4096, "reasoningEfforts": False}],
        }}}),
        row("session"),
        row("system-prompt", {"includeHarnessIdentity": False, "includeRuntimeContext": False, "personaPrefix": prompt}),
        row("tools"),
        row("tool-executor-native"),
        row("mcp-resources"),
        row("agent"),
        row("jobs-local"),
        row("agent-loop", {"agents": []}),
        row("session-persistence-jsonl", {"root": str(sessions), "compression": "none"}),
        row("session-checkpoint-policy"),
        {
            "id": "mochi-bridge", "name": str(Path(__file__).with_name("dsh_bridge.mjs")),
            "config": {"origin": origin, "portFile": str(port_file), "tools": tools},
        },
    ]
    # JSON is an unambiguous subset of the profile patch YAML grammar.
    (profile / "cordis.patch.yml").write_text(
        json.dumps([{"insert": entries}], ensure_ascii=False), encoding="utf-8",
    )


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
