import sys
from types import SimpleNamespace
import warnings

import pytest

from deskpet import model_setup, model_setup_cli
from deskpet.__main__ import main
from deskpet.observation_v2.routes import STATE, SYSTEM2, VISION, CloudRoute
from deskpet.settings import SettingsStore


SYSTEM2_URL = "https://tools.example.test/v1"
OBSERVATION_URL = "https://vision.example.test/v1"


def test_terminal_setup_uses_separate_hidden_prompts_and_the_native_profile(tmp_path, monkeypatch, capsys):
    profile = tmp_path / "local" / "Mochi"
    identity = {}
    local_data = object()

    def writable_location(location):
        assert location is local_data
        assert identity == {"organization": "Mochi", "application": "Mochi"}
        return str(profile)

    monkeypatch.setitem(sys.modules, "PyQt6.QtCore", SimpleNamespace(
        QCoreApplication=SimpleNamespace(
            setOrganizationName=lambda name: identity.update(organization=name),
            setApplicationName=lambda name: identity.update(application=name),
        ),
        QStandardPaths=SimpleNamespace(
            StandardLocation=SimpleNamespace(AppLocalDataLocation=local_data),
            writableLocation=writable_location,
        ),
    ))
    monkeypatch.setitem(sys.modules, "PyQt6.QtWidgets", None)
    monkeypatch.setitem(sys.modules, "deskpet.c_ui.refine", None)
    entries = iter(("3", SYSTEM2_URL, "tool-model", OBSERVATION_URL, "vision-model"))
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or next(entries))
    key_prompts = []
    system2_key, observation_key = "example-system2-key", "example-observation-key"
    keys = iter((system2_key, observation_key, ""))
    monkeypatch.setattr(model_setup_cli, "getpass", lambda prompt: key_prompts.append(prompt) or next(keys))
    keyring = {}
    monkeypatch.setattr(model_setup, "store_route_credential",
                        lambda route, value: keyring.update({route.credential_ref: value}))

    def verify(routes, configured):
        assert routes.path == profile / "business.sqlite3"
        return {
            role: "permission_required" if role == VISION and not snapshot.permission else "verified"
            for role, snapshot in configured.items()
        }

    monkeypatch.setattr(model_setup_cli, "verify_models", verify)
    command = ["--configure-models", "--terminal"]
    assert main(command) == 0
    assert prompts[0].startswith("Configure [1] System 2")
    assert [prompt.split(" [")[0] for prompt in prompts[1:]] == [
        "System 2 API Base URL", "System 2 model", "Observation API Base URL", "Observation model",
    ]
    assert key_prompts == ["System 2 API Key: ", "Observation API Key: "]
    output = capsys.readouterr().out
    assert "System 2: verified" in output
    assert "Observation: image-sharing permission required" in output
    assert all(key not in output for key in (system2_key, observation_key))

    store = SettingsStore(profile / "business.sqlite3")
    system2 = store.routes.snapshot(SYSTEM2)
    vision = store.routes.snapshot(VISION)
    assert (system2.route.base_url, system2.route.model) == (SYSTEM2_URL, "tool-model")
    assert (vision.route.base_url, vision.route.model) == (OBSERVATION_URL, "vision-model")
    assert keyring == {system2.route.credential_ref: system2_key, vision.route.credential_ref: observation_key}
    assert not system2.permission and not vision.permission
    assert not store.read().observation_screenshots
    assert store.routes.snapshot(STATE).route is None
    assert all(key.encode() not in store.path.read_bytes() for key in (system2_key, observation_key))
    assert not (profile / "observation.sqlite3").exists()

    assert main([
        *command, "--skip-system2", "--observation-base-url", OBSERVATION_URL,
        "--observation-model", "new-vision-model", "--share-images",
    ]) == 0
    assert key_prompts[-1] == "Observation API Key (leave blank to keep the saved key): "
    assert store.routes.snapshot(SYSTEM2) == system2
    current_vision = store.routes.snapshot(VISION)
    assert current_vision.route.model == "new-vision-model"
    assert current_vision.route.credential_ref == vision.route.credential_ref
    assert current_vision.permission and not current_vision.vision_signals_permission
    assert not store.read().observation_screenshots
    assert "System 2" not in capsys.readouterr().out


def test_terminal_setup_refuses_visible_input(tmp_path, monkeypatch, capsys):
    def unavailable(_):
        warnings.warn("cannot hide input", model_setup_cli.GetPassWarning)
        pytest.fail("getpass reached its visible-input fallback")

    monkeypatch.setattr(model_setup_cli, "getpass", unavailable)
    monkeypatch.setattr(model_setup_cli, "configure_models",
                        lambda *args, **kwargs: pytest.fail("configured without a private prompt"))
    assert main([
        "--configure-models", "--terminal", "--data-dir", str(tmp_path), "--role", "system2",
        "--system2-base-url", SYSTEM2_URL, "--system2-model", "tool-model",
    ]) == 1
    assert "can hide API key input" in capsys.readouterr().err


def test_terminal_setup_confirms_changing_an_existing_endpoint(tmp_path, monkeypatch, capsys):
    routes = SettingsStore(tmp_path / "business.sqlite3").routes
    custom = CloudRoute("openai_compatible", "https://old.example.test/v1", "custom-vision", "Custom vision")
    previous = routes.configure(VISION, custom)
    confirmations = iter(("", "yes"))
    monkeypatch.setattr("builtins.input", lambda _: next(confirmations))
    keys = []
    monkeypatch.setattr(model_setup_cli, "getpass", lambda prompt: keys.append(prompt) or "example-key")
    monkeypatch.setattr(model_setup, "store_route_credential", lambda route, value: None)
    monkeypatch.setattr(model_setup_cli, "verify_models", lambda routes, configured: {VISION: "permission_required"})
    command = [
        "--configure-models", "--terminal", "--role", "observation", "--data-dir", str(tmp_path),
        "--observation-base-url", OBSERVATION_URL, "--observation-model", "new-vision-model",
    ]

    assert main(command) == 1
    assert "custom-vision at https://old.example.test/v1" in capsys.readouterr().out
    assert not keys and routes.snapshot(VISION) == previous

    assert main(command) == 0
    assert keys == ["Observation API Key: "]
    assert routes.snapshot(VISION).route.base_url == OBSERVATION_URL
    assert routes.snapshot(SYSTEM2).route is None


def test_terminal_setup_offers_an_optional_key_for_existing_ollama(tmp_path, monkeypatch):
    routes = SettingsStore(tmp_path / "business.sqlite3").routes
    endpoint, model = "http://localhost:11434", "qwen3-vl"
    previous = routes.configure(VISION, CloudRoute("local_ollama", endpoint, model, "Ollama"))
    keys = iter(("", "example-local-key"))
    prompts = []
    stored = []
    monkeypatch.setattr(model_setup_cli, "getpass", lambda prompt: prompts.append(prompt) or next(keys))
    monkeypatch.setattr(model_setup, "store_route_credential", lambda route, value: stored.append(value))
    monkeypatch.setattr(model_setup_cli, "verify_models", lambda routes, configured: {VISION: "verified"})
    command = [
        "--configure-models", "--terminal", "--role", "observation", "--data-dir", str(tmp_path),
        "--observation-base-url", endpoint, "--observation-model", model,
    ]

    assert main(command) == 0
    assert routes.snapshot(VISION) == previous
    assert main(command) == 0
    assert routes.snapshot(VISION).route.credential_ref
    assert stored == ["example-local-key"]
    assert prompts == ["Observation API Key (optional): "] * 2


def test_terminal_setup_redacts_unexpected_credential_errors(tmp_path, monkeypatch, capsys):
    key = "example-key-that-must-stay-private"
    monkeypatch.setattr(model_setup_cli, "getpass", lambda _: key)

    def storage_error(*args, **kwargs):
        raise RuntimeError(key)

    monkeypatch.setattr(model_setup, "store_route_credential", storage_error)
    monkeypatch.setattr(model_setup, "delete_route_credential", lambda route: None)
    assert main([
        "--configure-models", "--terminal", "--data-dir", str(tmp_path), "--role", "system2",
        "--system2-base-url", SYSTEM2_URL, "--system2-model", "tool-model",
    ]) == 1
    output = capsys.readouterr()
    assert "OS credential storage" in output.err
    assert key not in output.out + output.err


@pytest.mark.parametrize("arguments", [
    ["--configure-models", "--role", "system2"],
    ["--configure-models", "--terminal", "--role", "system2", "--observation-model", "vision-model"],
    ["--configure-models", "--terminal", "--skip-observation-vision", "--share-images"],
])
def test_terminal_options_require_terminal_mode_and_their_role(arguments):
    with pytest.raises(SystemExit) as error:
        main(arguments)
    assert error.value.code == 2


def test_standalone_setup_flags_dispatch_to_their_gui(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "deskpet.c_ui.refine", SimpleNamespace(
        configure_models=lambda path: calls.append(("unified", path)) or 3,
        configure_system2=lambda path: calls.append(("system2", path)) or 4,
    ))
    assert main(["--configure-models", "--data-dir", str(tmp_path)]) == 3
    assert main(["--configure-system2", "--data-dir", str(tmp_path)]) == 4
    assert calls == [("unified", tmp_path), ("system2", tmp_path)]
