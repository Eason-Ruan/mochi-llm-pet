"""Configure the desktop model connections from an interactive terminal."""

from collections.abc import Mapping
from getpass import GetPassWarning, getpass
from pathlib import Path
import sqlite3
import sys
import warnings

from .desktop_profile import desktop_data_dir
from .model_setup import (
    MODEL_ROLES, ModelInput, can_reuse_api_key, configure_models, model_input_defaults, model_setup_defaults,
    verify_models,
)
from .observation_v2.routes import SYSTEM2, VISION, Role, _normalize_base_url
from .settings import SettingsStore


_ROLE_LABELS = {SYSTEM2: "System 2", VISION: "Observation"}
_ROLE_CHOICES = {"system2": (SYSTEM2,), "observation": (VISION,), "both": MODEL_ROLES}
_VERIFICATION_LABELS = {
    "verified": "verified",
    "permission_required": "image-sharing permission required for verification",
    "failed": "verification failed",
}


def _select_roles(role: str | None) -> tuple[Role, ...]:
    if role is not None:
        return _ROLE_CHOICES[role]
    choices = {"1": "system2", "2": "observation", "3": "both"}
    while True:
        choice = input("Configure [1] System 2, [2] Observation, or [3] both: ").strip().casefold()
        selected = _ROLE_CHOICES.get(choices.get(choice, choice))
        if selected:
            return selected
        print("Choose System 2, Observation, or both.")


def _read_api_key(label: str, *, can_reuse: bool, has_saved_key: bool) -> str:
    hint = ""
    if can_reuse:
        hint = " (leave blank to keep the saved key)" if has_saved_key else " (optional)"
    with warnings.catch_warnings():
        # getpass warns before falling back to visible input.
        warnings.simplefilter("error", GetPassWarning)
        return getpass(f"{label} API Key{hint}: ")


def run_terminal_setup(
    data_dir: Path | None, *,
    role: str | None = None,
    overrides: Mapping[Role, tuple[str | None, str | None, bool | None]] | None = None,
) -> int:
    try:
        roles = _select_roles(role)
    except (EOFError, KeyboardInterrupt):
        print("Model setup was cancelled.", file=sys.stderr)
        return 130
    except OSError:
        print("Could not read from the terminal.", file=sys.stderr)
        return 1

    overrides = overrides or {}
    for option_role, options in overrides.items():
        if option_role not in roles and any(value is not None for value in options):
            print(f"Select {_ROLE_LABELS[option_role]} to use its terminal options.", file=sys.stderr)
            return 2

    try:
        routes = SettingsStore(desktop_data_dir(data_dir) / "business.sqlite3").routes
        snapshots = model_setup_defaults(routes)
    except (ImportError, OSError, RuntimeError, ValueError, sqlite3.Error):
        print("Could not open the Mochi profile. Check --data-dir and directory permissions.", file=sys.stderr)
        return 1

    updates = {}
    try:
        for selected in roles:
            label = _ROLE_LABELS[selected]
            snapshot = snapshots[selected]
            current = snapshot.route
            default_url, default_model = model_input_defaults(snapshot)
            base_url, model, permission = overrides.get(selected, (None, None, None))
            if base_url is None:
                base_url = input(f"{label} API Base URL [{default_url}]: ").strip() or default_url
            try:
                base_url = _normalize_base_url(base_url.strip())
            except ValueError:
                print(f"Enter a valid API Base URL for {label}.", file=sys.stderr)
                return 1
            if current is not None and current.base_url != base_url:
                print(f"{label} currently uses {current.model} at {current.base_url}.")
                if input("Replace this model service? [y/N] ").strip().casefold() not in {"y", "yes"}:
                    print("Model setup was cancelled.", file=sys.stderr)
                    return 1
            if model is None:
                model = input(f"{label} model [{default_model}]: ").strip() or default_model
            updates[selected] = ModelInput(
                base_url, model,
                api_key=_read_api_key(
                    label, can_reuse=can_reuse_api_key(snapshot, base_url),
                    has_saved_key=current is not None and current.credential_ref is not None,
                ),
                permission=permission,
            )
    except GetPassWarning:
        print("Open an interactive terminal that can hide API key input.", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("Model setup was cancelled.", file=sys.stderr)
        return 130
    except OSError:
        print("Could not read from the terminal.", file=sys.stderr)
        return 1

    try:
        configured = configure_models(routes, updates, expected=snapshots)
    except KeyboardInterrupt:
        print("Model setup was interrupted.", file=sys.stderr)
        return 130
    except ValueError:
        print("Could not save model setup. Check each API Base URL, model, and API Key.", file=sys.stderr)
        return 1
    except Exception:
        print("Could not save model setup. Check the profile and OS credential storage.", file=sys.stderr)
        return 1
    finally:
        updates.clear()

    print("Model setup saved. Checking capabilities with synthetic inputs.", flush=True)
    try:
        results = verify_models(routes, configured)
    except KeyboardInterrupt:
        print("Verification was cancelled.", file=sys.stderr)
        return 130
    except Exception:
        print("Could not finish model verification.", file=sys.stderr)
        return 1
    for selected in roles:
        print(f"{_ROLE_LABELS[selected]}: {_VERIFICATION_LABELS[results[selected]]}.")
    return int("failed" in results.values())
