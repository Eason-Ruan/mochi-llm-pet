from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
from threading import Event

import pytest

from deskpet.settings import HabitBehavior, Settings, SettingsConflict, SettingsStore


def test_default_privacy_and_settings_compare_and_swap(tmp_path):
    path = tmp_path / "business.sqlite3"
    assert SettingsStore.read_at(path) == Settings()
    assert not path.exists()

    user = SettingsStore(path)
    assert user.read().version == 0
    assert not user.read().observation_screenshots
    assert not user.read().lock_screen_detail
    written = user.update(
        expected_version=0, reminder_sound=False, observation_screenshots=True,
        observation_excluded_apps=(" Mail ", "mail"),
        observation_excluded_domains=("Example.com", ".example.com", "Private.test"),
    )
    assert written.version == 1
    assert written.observation_excluded_apps == ("mail",)
    assert written.observation_excluded_domains == ("example.com", "private.test")
    assert not written.reminder_sound and written.phase_end_sound
    assert SettingsStore.read_at(path) == written
    assert SettingsStore(path, user_scope="different").read() == Settings()
    with pytest.raises(SettingsConflict):
        user.update(expected_version=0, observation_screenshots=False)

    def save(value):
        try:
            user.update(expected_version=1, default_focus_s=value)
        except SettingsConflict:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(save, [1200, 2400])) == 1
    assert SettingsStore(path).read().version == 2


def test_legacy_screenshot_scope_defaults_to_foreground_and_scope_changes_advance_generation(tmp_path):
    path = tmp_path / "business.sqlite3"
    store = SettingsStore(path)
    legacy = Settings(version=3, observation_screenshots=True, observation_generation=2).to_dict()
    del legacy["observation_screen_scope"]
    with sqlite3.connect(path) as db:
        db.execute("UPDATE pet_settings SET settings = ?", (json.dumps(legacy),))

    migrated = store.read()
    assert migrated.observation_screenshots and migrated.observation_screen_scope == "foreground_window"
    widened = store.update(expected_version=3, observation_screen_scope="all_displays")
    assert widened.observation_generation == 3
    assert SettingsStore.read_at(path) == widened
    repeated = store.update(expected_version=widened.version, observation_screen_scope="all_displays")
    assert repeated.observation_generation == widened.observation_generation
    narrowed = store.update(expected_version=repeated.version, observation_screen_scope="foreground_window")
    assert narrowed.observation_generation == widened.observation_generation + 1


def test_document_permissions_migrate_and_apply_only_to_the_exact_allowed_source(tmp_path):
    path = tmp_path / "business.sqlite3"
    store = SettingsStore(path)
    legacy = Settings(version=4, default_focus_s=2100, observation_screenshots=True).to_dict()
    del legacy["document_local_sources"], legacy["document_cloud_sources"]
    with sqlite3.connect(path) as db:
        db.execute("UPDATE pet_settings SET settings = ?", (json.dumps(legacy),))

    migrated = store.read()
    assert migrated.version == 4 and migrated.default_focus_s == 2100 and migrated.observation_screenshots
    assert migrated.document_local_sources == migrated.document_cloud_sources == ()
    assert SettingsStore.read_at(path) == migrated

    granted = store.update(
        expected_version=migrated.version,
        document_local_sources=(" VS Code ", "app:vscode", "Word", "Excel", "docs.google.com"),
        document_cloud_sources=("APP:VSCODE", "Docs.Google.COM", "google.com", "mail.google.com"),
    )
    assert granted.document_local_sources == (
        "app:vscode", "app:microsoft-word", "app:microsoft-excel", "domain:docs.google.com",
    )
    assert granted.document_cloud_sources == ("app:vscode", "domain:docs.google.com")
    assert granted.observation_generation == migrated.observation_generation + 1
    assert SettingsStore.read_at(path) == granted

    revoked = store.update(expected_version=granted.version, document_local_sources=("Word", "docs.google.com"))
    assert revoked.document_cloud_sources == ("domain:docs.google.com",)
    assert revoked.observation_generation == granted.observation_generation + 1
    restored_locally = store.update(expected_version=revoked.version,
                                    document_local_sources=granted.document_local_sources)
    assert restored_locally.document_cloud_sources == revoked.document_cloud_sources

    revoked_cloud = store.update(expected_version=restored_locally.version, document_cloud_sources=())
    assert revoked_cloud.document_local_sources == granted.document_local_sources
    assert revoked_cloud.document_cloud_sources == ()
    assert revoked_cloud.observation_generation == restored_locally.observation_generation + 1
    assert SettingsStore.read_at(path) == revoked_cloud

    with pytest.raises(ValueError):
        store.update(expected_version=revoked_cloud.version,
                     document_local_sources=("https://docs.google.com/document/secret",))
    assert store.read() == revoked_cloud


def test_document_permissions_use_the_observation_privacy_gate(tmp_path):
    from deskpet.observation_v2.routes import observation_privacy_gate

    store = SettingsStore(tmp_path / "business.sqlite3")
    granted = store.update(expected_version=0, document_local_sources=("Word",),
                           document_cloud_sources=("Word",))
    revoking = Event()

    def revoke():
        revoking.set()
        return store.update(expected_version=granted.version, document_cloud_sources=())

    with ThreadPoolExecutor(max_workers=1) as pool:
        with observation_privacy_gate(store.path):
            future = pool.submit(revoke)
            assert revoking.wait(1)
            assert store.read() == granted
            assert not future.done()
        assert future.result(timeout=2).document_cloud_sources == ()


def test_habit_archive_remains_readable_to_phase_transaction(tmp_path):
    path = tmp_path / "business.sqlite3"
    store = SettingsStore(path)
    behavior = HabitBehavior(focus_s=3600, auto_start_break=True)
    first = store.save_habit(title=" 写论文 ", behavior=behavior, expected_version=0)
    assert first.title == "写论文"
    assert first.behavior.auto_start_break and not first.behavior.auto_start_focus
    assert SettingsStore(path, user_scope="other").get_habit(first.id) is None
    with pytest.raises(SettingsConflict):
        store.save_habit(title=first.title, behavior=behavior, habit_id=first.id, expected_version=0)
    archived = store.save_habit(
        title=first.title, behavior=behavior, status="archived", habit_id=first.id, expected_version=first.version,
    )
    with sqlite3.connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        assert store.get_habit(first.id, db=db) == archived
        assert store.read(db=db) == Settings()
    assert SettingsStore(path).list_habits() == (archived,)
    with pytest.raises(SettingsConflict):
        store.save_habit(title=first.title, behavior=behavior, habit_id=first.id, expected_version=archived.version)


def test_preference_and_business_effect_commit_together(tmp_path):
    path = tmp_path / "business.sqlite3"
    store = SettingsStore(path)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE related_state (settings_version INTEGER)")

    def apply_effect(db, current, updated):
        assert store.read(db=db) == updated
        assert current.link_issues and not updated.link_issues
        db.execute("INSERT INTO related_state VALUES (?)", (updated.version,))

    def fail_effect(db, current, updated):
        apply_effect(db, current, updated)
        raise ValueError("Fail after both writes")

    with pytest.raises(ValueError, match="Fail after both writes"):
        store.update(expected_version=0, link_issues=False, before_commit=fail_effect)
    assert store.read().link_issues
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM related_state").fetchall() == []

    updated = store.update(expected_version=0, link_issues=False, before_commit=apply_effect)
    assert not updated.link_issues
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM related_state").fetchall() == [(updated.version,)]


def test_settings_widget_sends_edits_and_routes_each_sound_preview(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication, QCheckBox, QPushButton

    from deskpet.c_ui.settings import SettingsPanel

    app = QApplication.instance() or QApplication([])
    store = SettingsStore(tmp_path / "business.sqlite3")
    commands = []
    previews = []

    def preview(kind):
        previews.append(kind)
        return "音效已试听"

    panel = SettingsPanel(
        store, sound_preview=preview, notification_status=lambda: "系统通知权限已关闭",
        save_preferences=lambda version, changes: commands.append((version, changes)),
    )
    panel.show()
    app.processEvents()
    try:
        panel.set_height_limit(245)
        panel.adjustSize()
        assert panel.height() <= 245
        reminder_sound = panel.findChild(QCheckBox, "settings-reminder_sound")
        phase_sound = panel.findChild(QCheckBox, "settings-phase_end_sound")
        row_toggles = (
            reminder_sound, phase_sound,
            panel.findChild(QCheckBox, "settings-allow_proactive_movement"),
            panel.findChild(QCheckBox, "settings-reduce_motion"),
        )
        assert len({toggle.mapToGlobal(toggle.rect().topRight()).x() for toggle in row_toggles}) == 1
        reminder_sound.click()
        assert commands == [(0, {"reminder_sound": False})]
        assert store.read().reminder_sound
        version, changes = commands[-1]
        store.update(expected_version=version, **changes)
        panel.finish_preference_save(True)
        assert not reminder_sound.isChecked() and phase_sound.isChecked()
        for button in panel.findChildren(QPushButton):
            if button.text() == "试听":
                button.click()
        assert previews == ["reminder", "phase_end"]
        assert panel.status.text() == "音效已试听"

        notifications = panel.findChild(QCheckBox, "settings-allow_system_notifications")
        notifications.click()
        panel.finish_preference_save(False)
        assert not notifications.isChecked()
        assert "保存设置失败" in panel.status.text()
    finally:
        panel.close()


def test_settings_widget_edits_document_permissions_together(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt6")
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication, QDialogButtonBox, QLineEdit, QPushButton

    from deskpet.c_ui.settings import SettingsPanel

    app = QApplication.instance() or QApplication([])
    store = SettingsStore(tmp_path / "business.sqlite3")
    commands = []
    panel = SettingsPanel(store, save_preferences=lambda version, changes: commands.append((version, changes)))

    def edit_permissions(local_value, cloud_value):
        dialog = app.activeModalWidget()
        local = dialog.findChild(QLineEdit, "settings-document_local_sources")
        cloud = dialog.findChild(QLineEdit, "settings-document_cloud_sources")
        local.setText(local_value)
        cloud.setText(cloud_value)
        dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save).click()
        if dialog.isVisible():
            dialog.reject()

    try:
        assert "本机记录 0 个来源 · 云端共享 0 个来源" == panel._document_permissions_label.text()
        permissions = panel.findChild(QPushButton, "settings-document-permissions")
        QTimer.singleShot(0, lambda: edit_permissions("VS Code，Excel，docs.google.com", "Excel，mail.google.com"))
        permissions.click()
        assert commands == [(0, {
            "document_local_sources": ("app:vscode", "app:microsoft-excel", "domain:docs.google.com"),
            "document_cloud_sources": ("app:microsoft-excel",),
        })]
        version, changes = commands[0]
        store.update(expected_version=version, **changes)
        panel.finish_preference_save(True)
        assert "本机记录 3 个来源 · 云端共享 1 个来源" == panel._document_permissions_label.text()
        assert "仅用于旧版双模型采集与对照回放" in panel._route_labels["observation_state"].text()

        QTimer.singleShot(0, lambda: edit_permissions("", "Excel"))
        permissions.click()
        assert commands[-1] == (1, {"document_local_sources": (), "document_cloud_sources": ()})
        version, changes = commands[-1]
        store.update(expected_version=version, **changes)
        panel.finish_preference_save(True)
        assert "本机记录 0 个来源 · 云端共享 0 个来源" == panel._document_permissions_label.text()
    finally:
        panel.close()


def test_settings_widget_applies_capture_scope_and_app_blacklist(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt6")
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication, QComboBox, QDialogButtonBox, QLabel, QLineEdit, QPushButton

    from deskpet.c_ui import settings as settings_ui

    monkeypatch.setattr(settings_ui, "platform", "darwin")
    app = QApplication.instance() or QApplication([])
    store = SettingsStore(tmp_path / "business.sqlite3")
    panel = settings_ui.SettingsPanel(store)
    scope = panel.findChild(QComboBox, "settings-observation_screen_scope")

    def edit_exclusions():
        dialog = app.activeModalWidget()
        field = dialog.findChild(QLineEdit, "settings-observation_excluded_apps")
        field.setText("Mail， com.example.blocked")
        dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save).click()

    try:
        assert scope.currentData() == "foreground_window"
        assert any("仍可采集" in label.text() and "云端" in label.text() for label in panel.findChildren(QLabel))
        scope.setCurrentIndex(scope.findData("all_displays"))
        scope.activated.emit(scope.currentIndex())
        assert store.read().observation_screen_scope == "all_displays"
        assert not store.read().observation_screenshots
        QTimer.singleShot(0, edit_exclusions)
        panel.findChild(QPushButton, "settings-observation-exclusions").click()
        assert store.read().observation_excluded_apps == ("mail", "com.example.blocked")
    finally:
        panel.close()


def test_settings_widget_requests_macos_screen_recording_only_when_clicked(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication, QPushButton

    from deskpet.c_ui import settings as settings_ui

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(settings_ui.QGuiApplication, "platformName", lambda: "cocoa")
    requests = []

    def permission(*, request):
        requests.append(request)
        return request

    monkeypatch.setattr(settings_ui, "screen_recording_access", permission)
    panel = settings_ui.SettingsPanel(SettingsStore(tmp_path / "business.sqlite3"))
    try:
        assert requests == [False]
        assert "尚未授权" in panel._screen_recording_status.text()
        panel.findChild(QPushButton, "settings-request-screen-recording").click()
        assert requests == [False, True]
        assert "已授权" in panel._screen_recording_status.text()
    finally:
        panel.close()


def test_route_editor_never_reuses_a_credential_or_permission_for_another_recipient(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    from deskpet.c_ui.settings import _RouteEditor
    from deskpet.observation_v2 import openai_cloud
    from deskpet.observation_v2.routes import STATE

    app = QApplication.instance() or QApplication([])
    path = tmp_path / "business.sqlite3"
    routes = SettingsStore(path).routes
    stored = {}
    monkeypatch.setattr(openai_cloud, "store_route_credential", lambda route, key: stored.update({route: key}))

    first = _RouteEditor(routes, STATE, None)
    first.provider.setCurrentIndex(first.provider.findData("openai_compatible"))
    first.recipient.setText("Service A")
    first.endpoint.setText("https://first.example/v1")
    first.model.setText("text-model")
    first.key.setText("synthetic-api-key-A")
    first._save()
    first_snapshot = routes.set_permission(STATE, True, expected=routes.snapshot(STATE))
    assert first_snapshot.route is not None and first_snapshot.permission
    assert stored[first_snapshot.route] == "synthetic-api-key-A"
    assert first.key.text() == ""

    second = _RouteEditor(routes, STATE, None)
    second.endpoint.setText("https://second.example/v1")
    second._save()
    second_snapshot = routes.snapshot(STATE)
    assert second_snapshot.route.base_url == "https://second.example/v1"
    assert second_snapshot.route.credential_ref is None
    assert not second_snapshot.permission and second_snapshot.consent_generation > first_snapshot.consent_generation
    third = _RouteEditor(routes, STATE, None)
    third.key.setText("synthetic-api-key-B")
    third._save()
    third_snapshot = routes.snapshot(STATE)
    assert third_snapshot.route.credential_ref != first_snapshot.route.credential_ref
    assert stored[third_snapshot.route] == "synthetic-api-key-B"
    assert b"synthetic-api-key" not in path.read_bytes()

    local = _RouteEditor(routes, STATE, None)
    local.provider.setCurrentIndex(local.provider.findData("local_ollama"))
    local.endpoint.clear()
    local._save()
    local_snapshot = routes.snapshot(STATE)
    assert local_snapshot.route.provider_kind == "local_ollama"
    assert local_snapshot.route.base_url == "http://127.0.0.1:11434"
    assert local_snapshot.route.credential_ref is None and not local_snapshot.permission
    assert not _RouteEditor(routes, STATE, None).permission.isEnabled()
    app.processEvents()


def test_vision_editor_upgrades_existing_image_authorization_in_place(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication, QMessageBox, QPushButton

    from deskpet.c_ui.settings import _RouteEditor
    from deskpet.observation_v2.routes import VISION, CloudRoute

    app = QApplication.instance() or QApplication([])
    routes = SettingsStore(tmp_path / "business.sqlite3").routes
    configured = routes.configure(VISION, CloudRoute(
        "openai_compatible", "https://first.example/v1", "test-vision", "Service A",
    ))
    permitted = routes.set_permission(VISION, True, expected=configured)
    legacy = routes.record_result(permitted, probe=True)
    editor = _RouteEditor(routes, VISION, None)
    signal_permission = editor.findChild(QPushButton, "settings-route-vision-signals")
    prompts = []

    def confirm(message):
        prompts.append(message.text())
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "exec", confirm)
    try:
        assert legacy.permission and legacy.verified and not legacy.vision_signals_permission
        assert signal_permission.isEnabled() and "升级授权" in signal_permission.text()
        editor.endpoint.setText("https://another.example/v1")
        assert not signal_permission.isEnabled()
        editor.endpoint.setText(legacy.route.base_url)
        signal_permission.click()
        upgraded = routes.snapshot(VISION)
        assert upgraded.permission and upgraded.verified and upgraded.vision_signals_permission
        assert upgraded.route == legacy.route
        assert upgraded.consent_generation == legacy.consent_generation + 1
        assert len(prompts) == 1 and "https://first.example/v1" in prompts[0]

        signal_permission.click()
        downgraded = routes.snapshot(VISION)
        assert downgraded.permission and downgraded.verified and not downgraded.vision_signals_permission
        assert downgraded.consent_generation == upgraded.consent_generation + 1
        assert len(prompts) == 1
    finally:
        editor.close()
        app.processEvents()
