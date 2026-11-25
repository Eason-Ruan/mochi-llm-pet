from dataclasses import replace

import pytest

from deskpet.observation_v2 import openai_cloud
from deskpet.observation_v2.routes import BAILIAN, BAILIAN_BASE_URL, BAILIAN_MODEL, STATE, SYSTEM2, VISION, CloudRoute
from deskpet.settings import SettingsStore


WORKSPACE_URL = "https://llm-mochi-fixture.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"


class _Keyring:
    def __init__(self):
        self.entries = {}

    def set_password(self, service, reference, key):
        self.entries[service, reference] = key

    def get_password(self, service, reference):
        return self.entries.get((service, reference))

    def delete_password(self, service, reference):
        del self.entries[service, reference]


@pytest.fixture
def qt_app(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def keyring(monkeypatch):
    backend = _Keyring()
    monkeypatch.setattr(openai_cloud, "_keyring", lambda: backend)
    return backend


def _save(editor):
    from PyQt6.QtWidgets import QDialogButtonBox

    editor.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save).click()


def test_system2_defaults_to_masked_bailian_key(qt_app, tmp_path, keyring):
    from PyQt6.QtWidgets import QDialog, QLineEdit

    from deskpet.c_ui.settings import _RouteEditor

    path = tmp_path / "business.sqlite3"
    routes = SettingsStore(path).routes
    editor = _RouteEditor(routes, SYSTEM2, None)
    assert editor.provider.currentData() == BAILIAN
    assert editor.provider.currentText() == editor.recipient.text() == "阿里云百炼"
    assert editor.endpoint.text() == BAILIAN_BASE_URL
    assert editor.model.text() == BAILIAN_MODEL
    assert editor.key.accessibleName() == "百炼 API Key"
    assert editor.key.echoMode() == QLineEdit.EchoMode.Password
    _save(editor)
    assert routes.snapshot(SYSTEM2).route is None
    assert "百炼 API Key" in editor.error.text()

    key = "synthetic-bailian-key"
    editor.key.setText(key)
    assert editor.key.displayText() != key
    _save(editor)
    snapshot = routes.snapshot(SYSTEM2)
    assert editor.result() == QDialog.DialogCode.Accepted
    assert editor.key.text() == ""
    assert snapshot.route.recipient == (BAILIAN, BAILIAN_BASE_URL)
    assert snapshot.route.model == BAILIAN_MODEL
    assert openai_cloud._resolve_credential(snapshot.route) == key
    assert not snapshot.permission and not snapshot.verified
    assert key.encode() not in path.read_bytes()

    for role in (STATE, VISION):
        observation = _RouteEditor(routes, role, None)
        assert observation.provider.currentText() == "本地默认"
        assert observation.provider.findData(BAILIAN) == -1


def test_bailian_switch_demands_new_key_even_at_the_same_address(qt_app, tmp_path, keyring):
    from deskpet.c_ui.settings import _RouteEditor

    routes = SettingsStore(tmp_path / "business.sqlite3").routes
    previous = CloudRoute("openai_compatible", BAILIAN_BASE_URL, BAILIAN_MODEL, "Legacy", "legacy-reference")
    openai_cloud.store_route_credential(previous, "synthetic-legacy-key")
    original = routes.configure(SYSTEM2, previous)
    original = routes.set_permission(SYSTEM2, True, expected=original)
    original = routes.record_result(original, probe=True)

    editor = _RouteEditor(routes, SYSTEM2, None)
    assert editor.provider.currentData() == previous.provider_kind
    assert editor.endpoint.text() == previous.base_url
    assert editor.permission.isEnabled()
    editor.key.setText("synthetic-legacy-draft")
    editor.provider.setCurrentIndex(editor.provider.findData(BAILIAN))
    assert editor.key.text() == "" and not editor.permission.isEnabled()
    _save(editor)
    assert routes.snapshot(SYSTEM2) == original
    assert len(keyring.entries) == 1

    editor.key.setText("synthetic-bailian-key")
    _save(editor)
    current = routes.snapshot(SYSTEM2)
    assert current.route.recipient == (BAILIAN, previous.base_url)
    assert current.route.credential_ref != previous.credential_ref
    assert openai_cloud._resolve_credential(current.route) == "synthetic-bailian-key"
    assert not current.permission and not current.verified
    with pytest.raises(RuntimeError, match="No API key is saved"):
        openai_cloud._resolve_credential(replace(current.route, credential_ref=previous.credential_ref))


def test_bailian_workspace_requires_new_key_and_preserves_saved_fields(qt_app, tmp_path, keyring):
    from deskpet.c_ui.settings import _RouteEditor

    routes = SettingsStore(tmp_path / "business.sqlite3").routes
    general = CloudRoute(BAILIAN, BAILIAN_BASE_URL, BAILIAN_MODEL, "阿里云百炼", "general-reference")
    openai_cloud.store_route_credential(general, "synthetic-general-key")
    original = routes.configure(SYSTEM2, general)
    editor = _RouteEditor(routes, SYSTEM2, None)
    editor.key.setText("synthetic-general-draft")
    editor.endpoint.setText(WORKSPACE_URL)
    assert editor.key.text() == "" and not editor.permission.isEnabled()
    _save(editor)
    assert routes.snapshot(SYSTEM2) == original

    editor.key.setText("synthetic-workspace-key")
    editor.recipient.setText("Mochi 百炼业务空间")
    editor.timeout.setValue(75)
    _save(editor)
    saved = routes.snapshot(SYSTEM2)
    assert saved.route.credential_ref != general.credential_ref
    assert openai_cloud._resolve_credential(saved.route) == "synthetic-workspace-key"

    reopened = _RouteEditor(routes, SYSTEM2, None, preset_provider=BAILIAN)
    assert reopened.endpoint.text() == WORKSPACE_URL
    assert reopened.recipient.text() == saved.route.display_name
    assert reopened.timeout.value() == 75
    assert reopened.key.text() == ""
    _save(reopened)
    assert routes.snapshot(SYSTEM2) == saved
    assert len(keyring.entries) == 2


def test_english_bailian_editor_uses_localized_defaults_and_keeps_custom_name(qt_app, tmp_path, monkeypatch, keyring):
    from deskpet import i18n
    from deskpet.c_ui.settings import _RouteEditor

    monkeypatch.setattr(i18n, "_language", "en")
    routes = SettingsStore(tmp_path / "business.sqlite3").routes
    editor = _RouteEditor(routes, SYSTEM2, None)
    assert editor.provider.currentText() == editor.recipient.text() == "Alibaba Cloud Model Studio"
    editor.recipient.clear()
    editor.key.setText("synthetic-bailian-key")
    _save(editor)
    saved = routes.snapshot(SYSTEM2)
    assert saved.route.display_name == "Alibaba Cloud Model Studio"

    customized = routes.configure(SYSTEM2, replace(saved.route, display_name="我的工作空间"), expected=saved)
    reopened = _RouteEditor(routes, SYSTEM2, None)
    assert reopened.recipient.text() == "我的工作空间"
    _save(reopened)
    assert routes.snapshot(SYSTEM2) == customized
    assert customized.route.credential_ref == saved.route.credential_ref
    assert len(keyring.entries) == 1


def test_bailian_preset_cancel_and_stale_save_preserve_existing_route(qt_app, tmp_path, keyring):
    from deskpet.c_ui.settings import _RouteEditor

    routes = SettingsStore(tmp_path / "business.sqlite3").routes
    previous = CloudRoute("openai_compatible", "https://previous.example/v1", "previous-model", "Legacy")
    original = routes.configure(SYSTEM2, previous)
    editor = _RouteEditor(routes, SYSTEM2, None, preset_provider=BAILIAN)
    assert editor.snapshot == original
    assert editor.endpoint.text() == BAILIAN_BASE_URL
    assert editor.model.text() == BAILIAN_MODEL
    assert editor.permission.isHidden()
    editor.key.setText("synthetic-discarded-key")
    editor.reject()
    assert editor.key.text() == ""
    assert not keyring.entries
    assert routes.snapshot(SYSTEM2) == original

    stale = _RouteEditor(routes, SYSTEM2, None, preset_provider=BAILIAN)
    current = routes.set_permission(SYSTEM2, True, expected=original)
    stale.key.setText("synthetic-stale-key")
    _save(stale)
    assert "保存路由失败" in stale.error.text()
    assert routes.snapshot(SYSTEM2) == current
    assert not keyring.entries
