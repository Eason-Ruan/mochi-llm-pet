from concurrent.futures import ThreadPoolExecutor
import sqlite3

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
