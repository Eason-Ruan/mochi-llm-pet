import pytest

from deskpet import i18n, model_setup
from deskpet.model_setup import ModelInput
from deskpet.observation_v2 import openai_cloud
from deskpet.observation_v2.routes import (
    BAILIAN, BAILIAN_BASE_URL, STATE, SYSTEM2, VISION, CloudRoute, CloudRouteError,
)
from deskpet.settings import SettingsStore
from deskpet.system2 import dsh


SYSTEM2_URL = "https://chat.example.test/v1"
VISION_URL = "https://vision.example.test/compat/v1"
SYSTEM2_KEY = "synthetic-system2-key"
VISION_KEY = "synthetic-vision-key"


def initial_inputs(*, share_activity=None, share_images=None):
    return {
        SYSTEM2: ModelInput(SYSTEM2_URL, "chat-model", SYSTEM2_KEY, share_activity),
        VISION: ModelInput(VISION_URL, "vision-model", VISION_KEY, share_images),
    }


@pytest.fixture
def setup(tmp_path, monkeypatch):
    entries = {}

    class Keyring:
        def set_password(self, service, reference, key):
            entries[service, reference] = key

        def get_password(self, service, reference):
            return entries.get((service, reference))

        def delete_password(self, service, reference):
            del entries[service, reference]

    monkeypatch.setattr(openai_cloud, "_keyring", Keyring)
    routes = SettingsStore(tmp_path / "business.sqlite3").routes
    return routes, entries


def test_both_routes_have_independent_fields_in_the_shared_profile(setup):
    routes, entries = setup
    local = CloudRoute("local_ollama", "http://localhost:11434", "gemma", "Local")
    legacy = routes.configure(STATE, local)
    saved = model_setup.configure_models(routes, initial_inputs(share_activity=True))
    reloaded = model_setup.model_setup_defaults(SettingsStore(routes.path).routes)
    assert reloaded == saved
    for role, value in initial_inputs().items():
        assert model_setup.model_input_defaults(reloaded[role]) == (value.base_url, value.model)
        assert openai_cloud._resolve_credential(reloaded[role].route) == value.api_key
        assert value.api_key.encode() not in routes.path.read_bytes()
        assert value.api_key not in repr(value)
    assert len(entries) == 2 and saved[SYSTEM2].permission and not saved[VISION].permission
    assert not saved[VISION].vision_signals_permission
    assert routes.snapshot(STATE) == legacy


def test_model_edit_can_reuse_its_key_and_preserve_the_other_route(setup):
    routes, entries = setup
    first = model_setup.configure_models(routes, initial_inputs(share_images=True))
    unchanged = model_setup.configure_models(routes, {VISION: ModelInput(VISION_URL, "vision-model")})
    assert unchanged == {VISION: first[VISION]}
    edited = model_setup.configure_models(
        routes, {VISION: ModelInput(VISION_URL + "/", "another-vision")}, expected=first,
    )[VISION]
    assert edited.route.model == "another-vision" and not edited.permission
    assert edited.route.credential_ref == first[VISION].route.credential_ref
    assert openai_cloud._resolve_credential(edited.route) == VISION_KEY
    assert routes.snapshot(SYSTEM2) == first[SYSTEM2]
    assert len(entries) == 2


def test_model_switch_can_rebind_bailian_key_at_the_same_recipient(setup):
    routes, _ = setup
    defaults = model_setup.model_input_defaults(routes.snapshot(SYSTEM2))
    assert defaults == (BAILIAN_BASE_URL, "qwen4-max")
    first = model_setup.configure_models(
        routes, {SYSTEM2: ModelInput(*defaults, SYSTEM2_KEY, True)},
    )[SYSTEM2]
    assert first.route.provider_kind == BAILIAN
    changed = model_setup.configure_models(
        routes, {SYSTEM2: ModelInput(BAILIAN_BASE_URL, "qwen3.7-flash")},
    )[SYSTEM2]
    assert changed.route.provider_kind == "openai_compatible"
    assert not changed.permission and not changed.verified
    assert openai_cloud._resolve_credential(changed.route) == SYSTEM2_KEY


def test_bailian_setup_localizes_new_names_and_keeps_saved_route_identity(setup, monkeypatch):
    monkeypatch.setattr(i18n, "_language", "en")
    routes, entries = setup
    defaults = model_setup.model_input_defaults(routes.snapshot(SYSTEM2))
    saved = model_setup.configure_models(routes, {SYSTEM2: ModelInput(*defaults, SYSTEM2_KEY, True)})[SYSTEM2]
    assert saved.route.display_name == "Alibaba Cloud Model Studio"

    i18n.set_language("zh")
    assert model_setup.configure_models(routes, {SYSTEM2: ModelInput(*defaults)})[SYSTEM2] == saved
    assert len(entries) == 1
    assert openai_cloud._resolve_credential(saved.route) == SYSTEM2_KEY


def test_changed_url_cannot_inherit_a_key_or_change_the_other_route(setup):
    routes, entries = setup
    first = model_setup.configure_models(routes, initial_inputs())
    other_url = "https://different.example.test/v1"
    assert not model_setup.can_reuse_api_key(first[VISION], other_url)
    with pytest.raises(ValueError, match="API Key"):
        model_setup.configure_models(routes, {VISION: ModelInput(other_url, "vision-model")})
    assert model_setup.model_setup_defaults(routes) == first
    assert len(entries) == 2


def test_keyless_ollama_preexisting_and_new_loopback_are_supported(setup):
    routes, entries = setup
    base_url = "http://localhost:11434"
    previous = routes.configure(VISION, CloudRoute("local_ollama", base_url, "gemma", "Local"))
    assert model_setup.can_reuse_api_key(previous, base_url)
    loopback = "http://127.0.0.1:8000/v1"
    saved = model_setup.configure_models(routes, {
        SYSTEM2: ModelInput(loopback, "local-tools"),
        VISION: ModelInput(base_url, "gemma"),
    })
    assert saved[VISION] == previous
    assert saved[SYSTEM2].route.provider_kind == "openai_compatible"
    assert saved[SYSTEM2].route.credential_ref is None
    assert not entries


def test_stale_second_role_rolls_back_routes_and_new_credentials(setup):
    routes, entries = setup
    opened = model_setup.model_setup_defaults(routes)
    newer = routes.configure(VISION, CloudRoute("local_ollama", "http://localhost:11434", "gemma", "Newer"))
    with pytest.raises(CloudRouteError, match="authorization changed"):
        model_setup.configure_models(routes, initial_inputs(), expected=opened)
    assert routes.snapshot(SYSTEM2) == opened[SYSTEM2]
    assert routes.snapshot(VISION) == newer
    assert not entries


def test_keyring_failure_leaves_no_partial_setup(setup, monkeypatch):
    routes, entries = setup
    store = model_setup.store_route_credential

    def failure_on_vision(route, key):
        if route.base_url == VISION_URL:
            raise CloudRouteError("Could not store the API key in OS credential storage")
        store(route, key)

    monkeypatch.setattr(model_setup, "store_route_credential", failure_on_vision)
    with pytest.raises(CloudRouteError, match="OS credential storage"):
        model_setup.configure_models(routes, initial_inputs())
    assert all(routes.snapshot(role).route is None for role in model_setup.MODEL_ROLES)
    assert not entries


def test_verification_uses_each_saved_route_and_its_authorization(setup, monkeypatch):
    routes, _ = setup
    issued = []

    def system_probe(db, *, expected):
        issued.append(expected)
        return db.record_result(expected, probe=True)

    def vision_probe(db, role, *, expected):
        issued.append(expected)
        return db.record_result(expected, probe=True)

    monkeypatch.setattr(dsh, "verify_system2_route", system_probe)
    monkeypatch.setattr(model_setup, "verify_cloud_route", vision_probe)
    saved = model_setup.configure_models(routes, initial_inputs())
    assert model_setup.verify_models(routes, saved) == {SYSTEM2: "verified", VISION: "permission_required"}
    assert issued == [saved[SYSTEM2]]
    vision = routes.set_permission(VISION, True, expected=saved[VISION])
    assert model_setup.verify_models(routes, {VISION: vision}) == {VISION: "verified"}
    assert issued == [saved[SYSTEM2], vision]
