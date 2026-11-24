"""The daily Refine entry exercises persistent services through its Qt widgets."""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[2]
CODE = r'''
from datetime import datetime, timedelta
from pathlib import Path
import sys
from time import monotonic
from types import SimpleNamespace

from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QLabel, QLineEdit, QPushButton

from deskpet.c_ui.refine import RefineSession
from deskpet.c_ui.time_format import ZONE


app = QApplication([])
data_dir = Path(sys.argv[1])
options = SimpleNamespace(profile="realtime", scenario="desktop", output=None, data_dir=data_dir)
left = Qt.MouseButton.LeftButton


def wait_for(predicate, description):
    until = monotonic() + 4
    while not predicate() and monotonic() < until:
        QTest.qWait(25)
    assert predicate(), (description, session.window.errors)


def section(name):
    QTest.mouseClick(session.pet.buttons[name], left)
    wait_for(lambda: session.section == name and session.panels[name].isVisible(), name)
    if name in session.overviews:
        overview = session.overviews[name]
        assert session.pet._panel is overview and overview.width() == 292
        QTest.mouseClick(overview.findChild(QPushButton, "refine-manage"), left)
        wait_for(session.detail_panels[name].isVisible, "open editor")


def active():
    return session.snapshot.get("active_phase")


def editor():
    return next((widget for widget in session.chat.findChildren(QLineEdit, "refine-chat-editor")
                 if widget.isVisible()), None)

def chat_status(text):
    return any(label.isVisible() and label.text() == text and label.parentWidget().property("source") == "status"
               for label in session.chat.findChildren(QLabel, "refine-chat-message-text"))


def cat_position():
    return session.pet.mapToGlobal(session.pet._cat_area.topLeft())


def drag_cat(dx, dy):
    pet = session.pet
    start = pet._cat_area.center()
    origin = pet.mapToGlobal(start)
    target = origin + QPoint(dx, dy)
    for kind, global_point, button, buttons in (
        (QEvent.Type.MouseButtonPress, origin, left, left),
        (QEvent.Type.MouseMove, target, Qt.MouseButton.NoButton, left),
        (QEvent.Type.MouseButtonRelease, target, left, Qt.MouseButton.NoButton),
    ):
        QApplication.sendEvent(pet, QMouseEvent(
            kind, QPointF(pet.mapFromGlobal(global_point)), QPointF(global_point),
            button, buttons, Qt.KeyboardModifier.NoModifier,
        ))


session = RefineSession(app, options)
try:
    wait_for(lambda: session.window.ready_for_actions, "production worker startup")
    assert session.snapshot["clock"] == "wall"
    assert not session.snapshot["tasks"] and not session.snapshot["phases"]
    assert session.pet.isVisible() and not session.window.isVisible()

    drag_cat(-71, -92)
    original_home = cat_position()
    session.pet.apply_motion_intent("screen_edge", "wave")
    assert cat_position() != original_home
    session.pet.end_temporary_motion()
    assert cat_position() == original_home
    section("settings")
    QTest.mouseClick(session.pet.buttons["settings"], left)
    wait_for(lambda: not session.settings_panel.isVisible(), "settings closed")
    assert cat_position() == original_home

    section("settings")
    session.pet.apply_motion_intent("screen_edge", "wave")
    drag_cat(-71, -30)
    moved_home = session.pet._home_anchor
    session.pet.end_temporary_motion()
    assert cat_position() == moved_home and moved_home != original_home
    default = session.settings_panel._durations["default_focus_s"]
    default.setValue(13)
    default.editingFinished.emit()
    wait_for(lambda: session.snapshot["settings"]["default_focus_s"] == 780, "focus default saved")
    proactive = session.settings_panel._checkboxes["allow_proactive_movement"]
    proactive.setChecked(False)
    wait_for(lambda: not session.snapshot["settings"]["allow_proactive_movement"], "movement preference saved")
    session.pet.apply_motion_intent("screen_edge", "wave")
    assert cat_position() == moved_home

    section("work")
    start = session.work.buttons["start"]
    wait_for(start.isEnabled, "one-click focus")
    assert session.work.duration.value() == 13
    QTest.mouseClick(start, left)
    QTest.mouseClick(start, left)
    wait_for(active, "focus start")
    phase_id = active()["id"]
    assert active()["title"] == "专注" and active()["planned_duration_s"] == 780
    assert len(session.snapshot["phases"]) == 1
    phase_version = active()["version"]

    section("input")
    wait_for(editor, "typing input")
    editor().setText("再加 2 分钟")
    QTest.keyClick(editor(), Qt.Key.Key_Return)
    wait_for(lambda: chat_status("这次对话模型没能完成回复。"), "offline model status")
    assert session.snapshot["system2_route_status"] == "not_configured"
    assert (active()["id"], active()["version"], active()["planned_duration_s"]) == (phase_id, phase_version, 780)
    user, reply = session.chat_store.projected_messages(include_parts=True)
    assert user[1] == "再加 2 分钟" and not user[3]
    assert reply[4] == user[2]
    assert reply[6] == ({"source": "status", "text": "这次对话模型没能完成回复。"},)
    wait_for(editor, "draft editor")
    editor().setText("留待明天输入")

    section("reminders")
    session.reminders.content.setText("提交实验记录")
    QTest.mouseClick(session.reminders.create_button, left)
    wait_for(lambda: session.snapshot["tasks"], "reminder draft")
    QTest.mouseClick(session.reminders.buttons["confirm"], left)
    wait_for(lambda: session.snapshot["tasks"][0]["status"] == "registered", "reminder confirmation")
    assert not session.window.errors
finally:
    session.quit()
    wait_for(lambda: not session.window.thread.isRunning(), "worker shutdown")

session = RefineSession(app, options)
try:
    wait_for(lambda: session.window.ready_for_actions, "persistent restart")
    assert cat_position() == moved_home
    assert (active()["id"], active()["planned_duration_s"]) == (phase_id, 780)
    assert session.snapshot["settings"]["default_focus_s"] == 780
    assert not session.snapshot["settings"]["allow_proactive_movement"]
    assert session.snapshot["tasks"][0]["draft"]["content"] == "提交实验记录"
    section("input")
    wait_for(editor, "reloaded chat")
    assert editor().text() == "留待明天输入"
    assert chat_status("这次对话模型没能完成回复。")
    assert session.chat_store.projected_messages(include_parts=True) == [user, reply]

    # Estimated timing stays unchanged during a content edit. Changing to an
    # exact reminder uses the selected absolute target and starts a new occurrence.
    section("reminders")
    panel = session.reminders
    now = datetime.fromisoformat(session.snapshot["now"])
    target = (now.astimezone(ZONE) + timedelta(days=1)).replace(hour=15, minute=0, second=0, microsecond=0)
    panel.mode.setCurrentIndex(panel.mode.findData("soft"))
    panel.times["target_at"].load(target, target)
    panel.content.setText("准备耳机")
    QTest.mouseClick(panel.create_button, left)

    def get_task():
        return next((task for task in session.snapshot["tasks"]
                     if task["draft"]["content"].startswith("准备耳机")), None)

    wait_for(get_task, "flexible reminder draft")
    QTest.mouseClick(panel.buttons["confirm"], left)
    wait_for(lambda: get_task()["status"] == "registered", "flexible reminder confirmed")
    flexible = get_task()
    assert datetime.fromisoformat(flexible["draft"]["target_at"]) == target
    assert flexible["draft"]["remind_after"] is None
    assert flexible["draft"]["remind_by"] is None and flexible["draft"]["latest_first_delivery_at"] is None
    QTest.mouseClick(panel.buttons["edit"], left)
    wait_for(panel.save_edit.isVisible, "absolute time editor")
    assert not panel.times["remind_after"].enabled.isChecked()
    entered_at = datetime.fromisoformat(session.snapshot["now"])
    panel.content.setText("准备耳机和充电线")
    wait_for(lambda: datetime.fromisoformat(session.snapshot["now"]) > entered_at + timedelta(seconds=1),
             "editor remains open across wall-clock ticks")
    QTest.mouseClick(panel.save_edit, left)
    wait_for(lambda: get_task()["version"] == flexible["version"] + 1, "content saved")
    retitled = get_task()
    assert retitled["occurrence"] == flexible["occurrence"]
    assert retitled["draft"] == flexible["draft"] | {"content": "准备耳机和充电线"}
    QTest.mouseClick(panel.buttons["edit"], left)
    panel.mode.setCurrentIndex(panel.mode.findData("hard"))
    assert datetime.fromisoformat(panel.times["exact"].value()) == target
    QTest.mouseClick(panel.save_edit, left)
    wait_for(lambda: get_task()["version"] == retitled["version"] + 1, "exact time saved")
    exact = get_task()
    assert exact["occurrence"] == flexible["occurrence"] + 1
    assert exact["draft"]["mode"] == "hard"
    assert all(datetime.fromisoformat(exact["draft"][key]) == target
               for key in ("remind_after", "target_at", "remind_by"))

    panel.item_type.setCurrentIndex(panel.item_type.findData("issue"))
    panel.mode.setCurrentIndex(panel.mode.findData("none"))
    panel.times["due_at"].load(target + timedelta(days=1), target)
    panel.content.setText("完成实验记录整理")
    QTest.mouseClick(panel.create_button, left)

    def issue():
        return next((task for task in session.snapshot["tasks"] if task["draft"]["item_type"] == "issue"), None)

    wait_for(issue, "work issue draft")
    QTest.mouseClick(panel.buttons["confirm"], left)
    wait_for(lambda: issue()["status"] == "registered", "work issue confirmed")
    issue_id = issue()["task_id"]
    section("settings")
    session.settings_panel._checkboxes["link_issues"].setChecked(True)
    wait_for(lambda: session.snapshot["settings"]["link_issues"], "link preference saved")
    section("work")
    session.work.task.setCurrentIndex(session.work.task.findData(issue_id))
    QTest.mouseClick(session.work.buttons["set_links"], left)
    wait_for(lambda: active()["primary_issue_id"] == issue_id, "phase linked to issue")
    section("reminders")
    QTest.mouseClick(panel.buttons["done"], left)
    wait_for(lambda: issue()["status"] == "completed" and active()["primary_issue_id"] is None,
             "issue completion unlinks the same running phase")
    assert active()["id"] == phase_id

    section("work")
    assert not session.snapshot["settings"]["auto_adjust"]
    buttons = session.work.buttons
    QTest.mouseClick(buttons["pause"], left)
    wait_for(lambda: active()["state"] == "paused", "manual pause")
    remaining = active()["remaining_s"]
    QTest.qWait(300)
    assert active()["remaining_s"] == remaining
    QTest.mouseClick(buttons["resume"], left)
    wait_for(lambda: active()["state"] == "running", "manual resume")
    session.work.adjust_mode.setCurrentIndex(session.work.adjust_mode.findData("total_s"))
    session.work.adjust_minutes.setValue(40)
    QTest.mouseClick(buttons["adjust"], left)
    wait_for(lambda: active()["planned_duration_s"] == 2400, "manual adjustment")
    QTest.mouseClick(buttons["end"], left)
    wait_for(lambda: active() is None, "manual end")
    QTest.mouseClick(buttons["start_break"], left)
    wait_for(lambda: active() and active()["kind"] == "short_break", "manual break")
    QTest.mouseClick(buttons["skip_break"], left)
    wait_for(lambda: active() is None, "manual skip")

    section("reminders")
    panel = session.reminders
    condition_id = "condition-only-work-item"
    condition = "等我开完会"
    session._action("reminder.create", task_id=condition_id, version=0, payload={"draft": {
        "content": "提交组会记录", "mode": "soft", "item_type": "issue", "timing_context": condition,
    }})
    wait_for(lambda: panel.view(condition_id), "condition-only reminder loaded")
    panel.select_task(condition_id)
    QTest.mouseClick(panel.buttons["confirm"], left)
    wait_for(lambda: panel.view(condition_id)["status"] == "registered", "condition-only reminder confirmed")
    QTest.mouseClick(panel.buttons["edit"], left)
    wait_for(panel.save_edit.isVisible, "condition-only reminder editor")
    assert panel.mode.currentData() == "soft"
    assert condition in panel.preview.text() and condition in panel.details.text()
    assert not any(panel.times[key].enabled.isChecked() for key in ("remind_after", "target_at", "remind_by"))
    latest = target + timedelta(days=1)
    panel.times["remind_by"].load(latest, latest)
    previous = panel.view(condition_id)
    QTest.mouseClick(panel.save_edit, left)
    wait_for(lambda: panel.view(condition_id)["version"] > previous["version"], "condition and latest time saved")
    updated = panel.view(condition_id)
    assert updated["draft"]["timing_context"] == condition
    assert datetime.fromisoformat(updated["draft"]["remind_by"]) == latest
    assert updated["draft"]["target_at"] is None and updated["draft"]["remind_after"] is None
    wait_for(lambda: not panel.save_edit.isVisible(), "condition and latest save acknowledged")

    QTest.mouseClick(panel.buttons["edit"], left)
    wait_for(panel.save_edit.isVisible, "reopen reminder editor")
    panel.mode.setCurrentIndex(panel.mode.findData("none"))
    QTest.mouseClick(panel.save_edit, left)
    wait_for(lambda: panel.view(condition_id)["version"] > updated["version"], "work reminder disabled")
    disabled = panel.view(condition_id)["draft"]
    assert not disabled["reminder_enabled"] and disabled["timing_context"] is None
    assert disabled["remind_by"] is None
    assert not session.window.errors
finally:
    session.quit()
    wait_for(lambda: not session.window.thread.isRunning(), "restarted worker shutdown")
'''


def test_refine_desktop_uses_persistent_services_and_restores_window_and_draft(tmp_path):
    process = subprocess.run(
        [sys.executable, "-c", CODE, str(tmp_path)], cwd=ROOT,
        env=dict(os.environ, QT_QPA_PLATFORM="offscreen"),
        capture_output=True, text=True, timeout=25,
    )
    assert process.returncode == 0, process.stdout + process.stderr


def test_public_desktop_entry_initializes_empty_database_and_enforces_profile_lock(tmp_path):
    command = [sys.executable, "-m", "deskpet", "--data-dir", str(tmp_path)]
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        database = tmp_path / "business.sqlite3"
        until = time.monotonic() + 10
        while time.monotonic() < until and process.poll() is None:
            if database.exists():
                with sqlite3.connect(database) as connection:
                    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if {"tasks", "phase_views", "pet_settings"} <= tables:
                        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone() == (0,)
                        assert connection.execute("SELECT COUNT(*) FROM phase_views").fetchone() == (0,)
                        break
            time.sleep(0.03)
        else:
            raise AssertionError("The public launcher did not start a persistent worker")
        second = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=8)
        assert second.returncode == 1
        assert "Mochi 已在使用此本地数据目录。" in second.stderr
        assert process.poll() is None
    finally:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=5)
