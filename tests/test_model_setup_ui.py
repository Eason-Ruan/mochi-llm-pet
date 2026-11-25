import pytest

from deskpet import desktop_profile, model_setup
from deskpet.observation_v2.routes import BAILIAN_BASE_URL, BAILIAN_OBSERVATION_MODEL, SYSTEM2, VISION, CloudRoute
from deskpet.settings import SettingsStore


@pytest.fixture
def qt_app(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    widgets = pytest.importorskip("PyQt6.QtWidgets")
    return widgets.QApplication.instance() or widgets.QApplication([])


def _save(dialog):
    from PyQt6.QtWidgets import QDialogButtonBox

    dialog.buttons.button(QDialogButtonBox.StandardButton.Save).click()


def _save_and_wait(dialog):
    from PyQt6.QtCore import QEventLoop, QTimer

    loop = QEventLoop()
    dialog._verification_finished.connect(loop.quit)
    timeout = QTimer()
    timeout.setSingleShot(True)
    timeout.timeout.connect(loop.quit)
    timeout.start(5000)
    _save(dialog)
    assert "请等待验证完成" in dialog.feedback.text()
    loop.exec()
    assert dialog.feedback.isHidden()


def test_gui_saves_distinct_model_connections_and_masks_each_key(qt_app, tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QLineEdit

    from deskpet.c_ui import model_setup as ui

    settings = SettingsStore(tmp_path / "business.sqlite3")
    stored_keys = {}
    verified = []
    monkeypatch.setattr(model_setup, "store_route_credential",
                        lambda route, key: stored_keys.update({route.credential_ref: key}))

    def verify(routes, configured):
        verified.append(configured)
        return {SYSTEM2: "verified", VISION: "permission_required"}

    monkeypatch.setattr(ui, "verify_models", verify)
    dialog = ui.ModelSetupDialog(settings.routes)
    values = {
        SYSTEM2: ("https://dialogue.example/v1", "tool-model", "synthetic-dialogue-key"),
        VISION: ("https://observation.example/api/v1", "vision-model", "synthetic-vision-key"),
    }
    for role, (base_url, model, key) in values.items():
        form = dialog.forms[role]
        assert form.endpoint.text() == BAILIAN_BASE_URL
        assert form.selected.isChecked() and not form.permission.isChecked()
        assert not form.endpoint.isReadOnly() and not form.model.isReadOnly()
        assert form.key.echoMode() == QLineEdit.EchoMode.Password
        form.endpoint.setText(base_url)
        form.model.setText(model)
        form.key.setText(key)
        assert form.key.displayText() != key

    dialog.forms[SYSTEM2].permission.click()
    _save_and_wait(dialog)

    snapshots = {role: settings.routes.snapshot(role) for role in values}
    assert verified == [snapshots]
    for role, (base_url, model, key) in values.items():
        route = snapshots[role].route
        assert (route.base_url, route.model, stored_keys[route.credential_ref]) == (base_url, model, key)
        assert dialog.forms[role].key.text() == ""
        assert key.encode() not in settings.path.read_bytes()
    assert snapshots[SYSTEM2].permission and not snapshots[VISION].permission
    assert not settings.read().observation_screenshots
    assert "已验证" in dialog.forms[SYSTEM2].status.text()
    assert "授权截图后" in dialog.forms[VISION].status.text()


def test_unselected_role_is_preserved_and_endpoint_change_requires_its_own_key(qt_app, tmp_path, monkeypatch):
    from deskpet.c_ui import model_setup as ui

    routes = SettingsStore(tmp_path / "business.sqlite3").routes
    custom = routes.configure(SYSTEM2, CloudRoute("openai_compatible", "https://previous.example/v1", "custom", "Legacy"))
    custom = routes.set_permission(SYSTEM2, True, expected=custom)
    stored_keys = []
    monkeypatch.setattr(model_setup, "store_route_credential", lambda route, key: stored_keys.append(key))
    monkeypatch.setattr(ui, "verify_models", lambda routes, configured: {VISION: "verified"})
    dialog = ui.ModelSetupDialog(routes)
    system2, vision = dialog.forms[SYSTEM2], dialog.forms[VISION]

    system2.selected.click()
    assert not system2.permission.isEnabled()
    vision.key.setText("synthetic-first-key")
    vision.permission.click()
    new_url = "https://new-observation.example/compatible-mode/v1"
    vision.endpoint.setText(new_url)
    assert not vision.key.text() and not vision.permission.isChecked()
    _save(dialog)
    assert "API Key" in dialog.feedback.text()
    assert not stored_keys

    vision.key.setText("synthetic-vision-key")
    vision.permission.click()
    _save_and_wait(dialog)

    assert stored_keys == ["synthetic-vision-key"]
    assert routes.snapshot(SYSTEM2) == custom
    saved = routes.snapshot(VISION)
    assert saved.route.base_url == new_url and saved.permission
    assert not saved.vision_signals_permission
    assert "已验证" in vision.status.text()


def test_settings_opens_model_setup_and_keeps_advanced_controls(qt_app, tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QDialog, QPushButton

    from deskpet.c_ui.model_setup import ModelSetupDialog
    from deskpet.c_ui.settings import SettingsPanel

    store = SettingsStore(tmp_path / "business.sqlite3")
    panel = SettingsPanel(store)
    opened = []

    def cancel(dialog):
        keys = [form.key for form in dialog.forms.values()]
        opened.append((dialog.routes, dialog.parent(), [key.text() for key in keys]))
        for key in keys:
            key.setText("synthetic-discarded-key")
        dialog.reject()
        assert all(key.text() == "" for key in keys)
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(ModelSetupDialog, "exec", cancel)
    panel.findChild(QPushButton, "settings-model-setup").click()
    assert opened == [(store.routes, panel, ["", ""])]
    assert all(store.routes.snapshot(role).route is None for role in (SYSTEM2, VISION))
    for role in (SYSTEM2, VISION, "observation_state"):
        assert panel.findChild(QPushButton, f"settings-route-{role}") is not None


def test_terminal_and_gui_read_each_others_changes_to_the_same_profile(qt_app, tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QDialog

    from deskpet import model_setup_cli
    from deskpet.__main__ import main
    from deskpet.c_ui import model_setup as ui

    profile = tmp_path / "profile"
    routes = SettingsStore(profile / "business.sqlite3").routes
    stored_keys = []
    monkeypatch.setattr(model_setup, "store_route_credential", lambda route, key: stored_keys.append(key))
    monkeypatch.setattr(ui, "verify_models", lambda routes, configured: dict.fromkeys(configured, "verified"))
    keys = iter(("synthetic-tool-key", "synthetic-vision-key", ""))
    monkeypatch.setattr(model_setup_cli, "getpass", lambda _: next(keys))

    def verify(settings, configured):
        assert settings.path == routes.path
        return {role: "verified" for role, snapshot in configured.items()
                if settings.record_result(snapshot, probe=True).verified}

    monkeypatch.setattr(model_setup_cli, "verify_models", verify)
    setup_command = ["--configure-models", "--data-dir", str(profile)]
    assert main([
        *setup_command, "--terminal", "--role", "both",
        "--system2-base-url", "https://dialogue.example/v1", "--system2-model", "tool-v1",
        "--observation-base-url", "https://vision.example/v1", "--observation-model", "vision-v1",
        "--share-activity", "--share-images",
    ]) == 0
    before = model_setup.model_setup_defaults(routes)

    def open_dialog(dialog):
        assert dialog.routes.path == routes.path
        for role, form in dialog.forms.items():
            saved = before[role].route
            assert form.endpoint.text() == saved.base_url and form.model.text() == saved.model
            assert form.key.text() == "" and "留空保留" in form.key.placeholderText()
            assert form.permission.isChecked() and "已验证" in form.status.text()
        dialog.forms[VISION].selected.click()
        system2 = dialog.forms[SYSTEM2]
        system2.model.setText("tool-v2")
        assert not system2.permission.isChecked()
        _save_and_wait(dialog)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ui.ModelSetupDialog, "exec", open_dialog)
    assert main(setup_command) == 0
    assert stored_keys == ["synthetic-tool-key", "synthetic-vision-key"]
    system2 = routes.snapshot(SYSTEM2)
    assert system2.route.model == "tool-v2"
    assert system2.route.credential_ref == before[SYSTEM2].route.credential_ref
    assert not system2.permission
    assert routes.snapshot(VISION) == before[VISION]
    assert not (profile / "observation.sqlite3").exists()

    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "")
    assert main([*setup_command, "--terminal", "--role", "system2"]) == 0
    assert prompts == [
        "System 2 API Base URL [https://dialogue.example/v1]: ",
        "System 2 model [tool-v2]: ",
    ]
    assert stored_keys == ["synthetic-tool-key", "synthetic-vision-key"]


def test_gui_setup_from_fresh_macos_profile_explains_raw_scope_without_granting_it(qt_app, tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QDialog, QLabel

    from deskpet.__main__ import main
    from deskpet.c_ui import model_setup as ui
    from deskpet.c_ui.settings import _RouteEditor

    monkeypatch.setattr(desktop_profile, "platform", "darwin")
    monkeypatch.setattr(desktop_profile, "_all_displays_available", lambda: True)

    def open_dialog(dialog):
        prefs = SettingsStore.read_at(dialog.routes.path)
        notice = dialog.findChild(QLabel, "model-setup-raw-screen-scope")
        assert (notice is not None) == (prefs.observation_screen_scope == "all_displays_unfiltered")
        if notice is not None:
            assert "原始画面" in notice.text() and "无痕窗口" in notice.text()
        assert not dialog.forms[VISION].permission.isChecked()
        assert not dialog.routes.snapshot(VISION).permission
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(ui.ModelSetupDialog, "exec", open_dialog)
    command = ["--configure-models", "--data-dir", str(tmp_path)]
    assert main(command) == 0
    store = SettingsStore(tmp_path / "business.sqlite3")
    prefs = store.read()
    assert prefs.observation_screenshots and prefs.observation_screen_scope == "all_displays_unfiltered"
    store.routes.configure(VISION, CloudRoute(
        "openai_compatible", BAILIAN_BASE_URL, BAILIAN_OBSERVATION_MODEL, "Test provider",
    ))
    editor = _RouteEditor(store.routes, VISION, None)
    prompts = []
    monkeypatch.setattr(editor, "_confirm_permission", lambda title, message: prompts.append(message) or False)
    editor.permission.click()
    assert "原始画面" in prompts[-1] and not store.routes.snapshot(VISION).permission

    store.update(expected_version=prefs.version, observation_screen_scope="foreground_window")
    assert main(command) == 0
    editor.permission.click()
    assert "原始画面" not in prompts[-1] and not store.routes.snapshot(VISION).permission
