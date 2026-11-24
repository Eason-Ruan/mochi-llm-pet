"""Exercise reminder interruptions through the production Qt and task-service boundary."""

from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Thread
from time import monotonic
from types import SimpleNamespace

import pytest

from deskpet.c_ui.native_notifications import notification_id
from deskpet.contracts import TaskCommand
from deskpet.mocks.clock import RealClock
from deskpet.mocks.delivery import FakeDeliveryPort
from deskpet.settings import SettingsStore
from deskpet.task_service.presentation import PresentationLedger
from deskpet.task_service.sqlite import SQLiteTaskService


ROOT = Path(__file__).resolve().parents[2]


def _native_id(data_dir, delivery_id):
    return notification_id(delivery_id, scope=(Path(data_dir) / "business.sqlite3").resolve().as_uri())


def _seed_native_reminders(data_dir, *, state="delivered", rename=True, same_deadline_sounds=None, reminder_sound=True):
    path = data_dir / "business.sqlite3"
    clock = RealClock()
    now = clock.wall_now()
    ledger = PresentationLedger(path, source="native")

    with SQLiteTaskService(clock, FakeDeliveryPort(clock, outcome="missing"),
                           lambda *_args, **_fields: None, path=path) as service:
        for offset, (task_id, title) in enumerate((("transfer", "汇丰转账"), ("laundry", "收衣服"))):
            deadline = now - timedelta(minutes=2, seconds=offset if same_deadline_sounds is None else 0)
            sound = same_deadline_sounds[offset] if same_deadline_sounds is not None else None
            result = service.apply(TaskCommand(
                command_id=f"create:{task_id}", kind="create_registered", task_id=task_id,
                payload={"draft": {"content": title, "mode": "hard", "target_at": deadline,
                                   "remind_after": deadline, "remind_by": deadline, "sound": sound}},
            ))
            assert result.status == "applied"
            task = result.view
            delivery_id = f"delivery:{task_id}:{task.occurrence}:v{task.version}"
            assert ledger.prepare_os(task, delivery_id, _native_id(data_dir, delivery_id),
                                     deadline - timedelta(minutes=1), native_actions=True, requested_for=deadline)
            assert ledger.update(delivery_id, {"submitting"}, state, deadline)

        if rename:
            draft = service.get("transfer").draft.model_copy(update={"content": "汇丰转账（已更新）"})
            edited = service.apply(TaskCommand(
                command_id="rename:transfer", kind="edit", task_id="transfer", expected_version=1,
                payload={"draft": draft.model_dump(mode="json")},
            ))
            assert edited.status == "applied"
            assert (edited.view.version, edited.view.occurrence) == (2, 1)

    settings = SettingsStore(path)
    settings.update(expected_version=settings.read().version, allow_system_notifications=True, reminder_sound=reminder_sound)


def test_native_actions_open_and_change_only_the_original_occurrence(tmp_path):
    _seed_native_reminders(tmp_path)
    _run_desktop(tmp_path, "actions")


def test_phase_end_and_due_reminder_request_only_one_sound(tmp_path):
    _run_desktop(tmp_path, "sound")


def test_reserved_reminders_use_one_surface_and_resume_as_pending(tmp_path):
    _seed_native_reminders(tmp_path, state="scheduled", rename=False)
    _run_desktop(tmp_path, "ownership")


@pytest.mark.parametrize("audible_first", [False, True])
@pytest.mark.parametrize("reminder_sound", [True, False])
def test_native_foreground_batch_preserves_explicit_sound_without_a_second_card(tmp_path, audible_first, reminder_sound):
    _seed_native_reminders(tmp_path, state="scheduled", rename=False,
                           same_deadline_sounds=(audible_first, not audible_first), reminder_sound=reminder_sound)
    _run_desktop(tmp_path, "grouped-sound")


def test_background_os_and_local_deadlines_share_one_silent_primary(tmp_path):
    path = tmp_path / "business.sqlite3"
    settings = SettingsStore(path)
    settings.update(expected_version=settings.read().version, allow_system_notifications=True)
    _run_desktop(tmp_path, "mixed")


def test_soft_system_opportunity_waits_for_local_card_and_expires(tmp_path):
    settings = SettingsStore(tmp_path / "business.sqlite3")
    settings.update(expected_version=settings.read().version, allow_system_notifications=True)
    _run_desktop(tmp_path, "soft-slot")


@pytest.mark.parametrize("outcome", ["paint", "unverified", "snooze"])
def test_soft_native_fallback_survives_same_occurrence_title_and_sound_edit(tmp_path, outcome):
    settings = SettingsStore(tmp_path / "business.sqlite3")
    settings.update(expected_version=settings.read().version, allow_system_notifications=True)
    _run_desktop(tmp_path, f"edited-fallback-{outcome}")


def _run_desktop(data_dir, scenario):
    result = subprocess.run(
        [sys.executable, "-m", "tests.m0.test_interruption_flow", str(data_dir), scenario], cwd=ROOT,
        env=dict(os.environ, QT_QPA_PLATFORM="offscreen", MOCHI_SYSTEM2_SEMANTIC_BACKGROUND="1"),
        capture_output=True, text=True, timeout=24,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@contextmanager
def _desktop(data_dir, scenario, native=None):
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication

    from deskpet.c_ui.refine import RefineSession

    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    session = RefineSession(app, SimpleNamespace(
        profile="realtime", scenario=scenario, data_dir=data_dir, output=data_dir,
        synthetic_delivery=True, adapters=None, native_notifications=native,
    ))

    def wait(predicate):
        until = monotonic() + 6
        while not (value := predicate()) and monotonic() < until:
            QTest.qWait(20)
        assert value, (session.window.views, session.window.errors, session.prompt, session.snapshot.get("active_phase"))
        return value

    try:
        yield session, wait
    finally:
        session.quit()
        wait(lambda: not session.window.thread.isRunning())


def _on_platform_thread(callback, *args):
    platform = Thread(target=callback, args=args)
    platform.start()
    platform.join()


class _SimulatedNotifications:
    available = True
    status = "可用"
    can_respond = True
    can_verify_removal = False

    def __init__(self, *, can_schedule, pending=()):
        self.can_schedule = can_schedule
        self.pending = set(pending)
        self.decisions = {}
        self.submissions = []

    def set_response_handler(self, handler):
        self.response = handler

    def set_foreground_handler(self, handler):
        self.foreground = handler

    def refresh(self, *, request=False):
        pass

    def inventory(self, complete):
        complete(self.pending, {identifier for identifier, visible in self.decisions.items() if visible})

    def submit(self, identifier, due_at, *, sound, complete, snooze):
        self.submissions.append((identifier, due_at, sound))
        if due_at is not None:
            self.pending.add(identifier)
        complete("scheduled" if due_at is not None else "submitted", None)

    def fire_foreground(self, identifier):
        from deskpet.c_ui.native_notifications import _foreground_decision

        self.pending.remove(identifier)
        _on_platform_thread(_foreground_decision,
            identifier, 1, lambda options: self.decisions.update({identifier: options}), self.foreground,
        )

    def fire_background(self, identifier):
        self.pending.remove(identifier)
        self.decisions[identifier] = 1


def _exercise_native_actions(data_dir):
    from PyQt6.QtTest import QTest

    transfer_id = _native_id(data_dir, "delivery:transfer:1:v1")
    laundry_id = _native_id(data_dir, "delivery:laundry:1:v1")

    def response(identifier, action):
        _on_platform_thread(session.presentation.notification_response.emit, identifier, action)

    def task(task_id):
        return session.window.views[task_id]

    def selected_task():
        selected = session.reminders.selected()
        return selected["task_id"] if selected else None

    with _desktop(data_dir, "native-interruption-flow") as (session, wait):
        locked = False
        session.presentation.lock_probe = lambda: locked
        assert not session.window.ready_for_actions
        response(transfer_id, "open")
        wait(lambda: session.section == "reminders" and selected_task() == "transfer")
        assert task("transfer")["draft"]["content"] == "汇丰转账（已更新）"
        assert task("transfer")["acknowledged_at"] is None

        session._show_section(None)
        locked = True
        response(transfer_id, "open")
        response(laundry_id, "ack")
        QTest.qWait(250)
        assert session.section != "reminders"
        assert task("laundry")["acknowledged_at"] is None

        locked = False
        QTest.qWait(250)
        assert session.prompt is None
        assert session.section != "reminders"
        assert task("laundry")["acknowledged_at"] is None

        response(transfer_id, "snooze")
        snoozed = wait(lambda: task("transfer")["occurrence"] == 2 and task("transfer"))
        assert snoozed["version"] == 3 and snoozed["acknowledged_at"] is None
        target = datetime.fromisoformat(snoozed["draft"]["target_at"])
        assert target - datetime.fromisoformat(snoozed["last_snoozed_at"]) == timedelta(minutes=5)

        response(laundry_id, "open")
        wait(lambda: selected_task() == "laundry")
        response(transfer_id, "open")
        response(transfer_id, "ack")
        response(transfer_id, "snooze")
        QTest.qWait(250)
        assert selected_task() == "laundry"
        assert task("transfer")["version"] == 3 and task("transfer")["acknowledged_at"] is None

        response(laundry_id, "ack")
        wait(lambda: task("laundry")["acknowledged_at"])
        assert task("laundry")["version"] == 2
        assert all(view["displayed_at"] is None for view in session.window.views.values())
        assert session.prompt is None and not session.window.errors

        with closing(sqlite3.connect(session.business_db)) as db:
            assert db.execute("SELECT count(*) FROM native_followup_paints").fetchone() == (0,)


def _exercise_same_batch_sound(data_dir):
    from PyQt6.QtWidgets import QApplication

    with _desktop(data_dir, "interruptions-same-batch") as (session, wait):
        session.presentation.native = True
        beeps = []
        QApplication.beep = lambda: beeps.append(monotonic())
        wait(lambda: session.window.ready_for_actions)
        session.window.action.emit({"kind": "session", "command": "start", "session_id": "short-focus",
                                    "payload": {"kind": "focus", "planned_duration_s": 2.5}})
        phase = wait(lambda: session.snapshot.get("active_phase"))
        deadline = phase["phase_due_at"]
        session._action("reminder.create", task_id="hard-deadline", version=0, payload={"draft": {
            "content": "汇丰转账", "mode": "hard", "target_at": deadline,
            "remind_after": deadline, "remind_by": deadline, "sound": True,
        }})
        wait(lambda: session.window.views.get("hard-deadline"))
        session._action("reminder.confirm", task_id="hard-deadline", version=1)
        wait(lambda: session.snapshot["active_phase"]["state"] == "elapsed")
        wait(lambda: session.window.views["hard-deadline"]["displayed_at"])

        assert session.window.views["hard-deadline"]["acknowledged_at"] is None
        assert session.prompt and session.prompt.painted and session.prompt.command.sound == "reminder"
        assert len(beeps) == 1
        with closing(sqlite3.connect(session.business_db)) as db:
            assert db.execute("SELECT count(*) FROM events WHERE kind='sound.requested'").fetchone() == (1,)
        assert not session.window.errors


def _exercise_reserved_ownership(data_dir):
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QPushButton

    transfer_id = _native_id(data_dir, "delivery:transfer:1:v1")
    laundry_id = _native_id(data_dir, "delivery:laundry:1:v1")
    native = _SimulatedNotifications(can_schedule=False, pending=(transfer_id, laundry_id))

    def owners():
        return {row["task_id"]: row for row in session.presentation.ledger.rows()}

    with _desktop(data_dir, "native-reserved-ownership", native) as (session, wait):
        locked = False
        session.presentation.lock_probe = lambda: locked
        wait(lambda: session.window.ready_for_actions and session.presentation.rows)
        native.fire_foreground(transfer_id)
        wait(lambda: session.prompt and session.prompt.painted)
        assert native.decisions[transfer_id] == 0
        assert session.prompt.text == "提醒：汇丰转账"
        assert session.prompt.answer_labels == ("确认收到", "5 分钟后")
        assert owners()["transfer"]["channel"] == "pet_card"
        assert session.window.views["transfer"]["acknowledged_at"] is None

        locked = True
        acknowledge = session.pet.bubble.findChild(QPushButton, "refine-bubble-yes")
        QTest.mouseClick(acknowledge, Qt.MouseButton.LeftButton)
        session._desktop_power_event({"kind": "system_sleep"})
        native.fire_foreground(laundry_id)
        wait(lambda: native.decisions.get(laundry_id))
        assert session.prompt is None and owners()["laundry"]["channel"] == "os_notification"

        session._desktop_power_event({"kind": "system_wake"})
        locked = False
        wait(lambda: owners()["laundry"]["state"] == "delivered")
        assert session.prompt is None
        assert not native.submissions
        assert all(view["acknowledged_at"] is None for view in session.window.views.values())
        with closing(sqlite3.connect(session.business_db)) as db:
            assert db.execute("SELECT count(*) FROM native_followup_paints").fetchone() == (0,)
        assert not session.window.errors


def _exercise_native_grouped_sound(data_dir):
    from PyQt6.QtWidgets import QApplication

    deliveries = {task_id: f"delivery:{task_id}:1:v1" for task_id in ("transfer", "laundry")}
    identifiers = {task_id: _native_id(data_dir, delivery) for task_id, delivery in deliveries.items()}
    native = _SimulatedNotifications(can_schedule=False, pending=identifiers.values())
    with _desktop(data_dir, "foreground-grouped-sound", native) as (session, wait):
        session.presentation.native = True
        beeps = []
        QApplication.beep = lambda: beeps.append(monotonic())
        wait(lambda: session.window.ready_for_actions and session.presentation.rows)
        expected = [deliveries[task_id] for task_id, view in session.window.views.items()
                    if session.snapshot["settings"]["reminder_sound"] and view["draft"]["sound"] is True]

        native.fire_foreground(identifiers["transfer"])
        primary = wait(lambda: session.prompt and session.prompt.painted and session.prompt)
        assert primary.delivery_id == deliveries["transfer"]
        native.fire_foreground(identifiers["laundry"])
        wait(lambda: identifiers["laundry"] in native.decisions)
        assert session.prompt is primary and len(beeps) == len(expected)
        assert set(native.decisions.values()) == {0}
        with closing(sqlite3.connect(session.business_db)) as db:
            assert db.execute("SELECT task_id,state FROM presentations ORDER BY task_id").fetchall() == [
                ("laundry", "passive"), ("transfer", "painted"),
            ]
            assert [row[0] for row in db.execute("SELECT delivery_id FROM attention_sound_requests")] == expected
            assert db.execute("SELECT count(*) FROM events WHERE kind='card.painted'").fetchone() == (1,)
        assert not session.window.errors


def _exercise_mixed_background_batch(data_dir):
    from PyQt6.QtWidgets import QApplication, QFrame, QPushButton

    native = _SimulatedNotifications(can_schedule=True)
    with _desktop(data_dir, "native-and-local-deadline", native) as (session, wait):
        session.presentation.native = True
        beeps = []
        QApplication.beep = lambda: beeps.append(monotonic())

        wait(lambda: session.window.ready_for_actions and session.snapshot.get("settings"))
        now = datetime.now(timezone.utc)
        deadline = now + timedelta(seconds=4)
        drafts = {
            "reserved-native": {"content": "系统预登记"},
            "local-expiring": {"content": "有失效时间", "expires_at": (deadline + timedelta(minutes=1)).isoformat()},
        }
        for task_id, content in drafts.items():
            session._action("reminder.create", task_id=task_id, version=0, payload={"draft": {
                **content, "mode": "hard", "sound": False, "target_at": deadline.isoformat(),
                "remind_after": deadline.isoformat(), "remind_by": deadline.isoformat(),
            }})
        wait(lambda: len(session.window.views) == len(drafts))
        for task_id in drafts:
            session._action("reminder.confirm", task_id=task_id, version=1)
        wait(lambda: all(view["status"] == "registered" for view in session.window.views.values()))
        wait(lambda: native.submissions)
        wait(lambda: datetime.now(timezone.utc) >= deadline)
        assert len(native.submissions) == 1
        identifier, due_at, sound = native.submissions[0]
        assert due_at == deadline and not sound
        native.fire_background(identifier)

        def owners():
            return {row["task_id"]: row for row in session.presentation.ledger.rows()}

        wait(lambda: len(owners()) == len(drafts) and any(row["state"] == "delivered" for row in owners().values()))
        assert owners()["local-expiring"]["state"] in {"grouped", "passive"}
        assert set(row["channel"] for row in owners().values()) == {"os_notification", "pet_badge"}
        assert session.prompt is None and not beeps
        assert all(view["displayed_at"] is None and view["acknowledged_at"] is None
                   for view in session.window.views.values())

        session.pet._open_toolbar()
        session.open_section("reminders")
        menu = session.overviews["reminders"]

        def actionable_from_bell():
            return {row.property("task_id") for row in menu.findChildren(QFrame, "refine-card")
                    if row.isVisible() and (ack := row.findChild(QPushButton, "refine-reminder-ack"))
                    and ack.isVisible() and ack.isEnabled()}

        wait(lambda: actionable_from_bell() >= drafts.keys())
        assert not session.window.errors


def _exercise_soft_system_slot(data_dir):
    from PyQt6.QtTest import QTest

    native = _SimulatedNotifications(can_schedule=False)
    with _desktop(data_dir, "soft-system-slot", native) as (session, wait):
        wait(lambda: session._system2 and session.window.ready_for_actions and session.snapshot.get("settings"))
        gateway = session._system2.gateway
        scheduler = session._system2.scheduler

        def queue_soft(event_id, lease):
            created = gateway.execute_local("item.create", {"type": "reminder", "title": event_id}, event_id=event_id)
            now = gateway.now()
            result = scheduler.present(occurrence_ids=[created.view["occurrence"]["id"]],
                                       presentation={"channel": "os_notification", "level": "actionable"},
                                       reason_code="system_channel_after_slot", now=now,
                                       authority=SimpleNamespace(expires_at=now + timedelta(seconds=lease)))
            assert result["status"] == "pending", result
            delivery_id = result["delivery_id"]
            wait(lambda: delivery_id in session._reminder_pending)
            return delivery_id

        session.show_message("正在显示卡片")
        wait(lambda: session.pet.bubble and session.pet._bubble_exposed(session.pet.bubble))
        queue_soft("still-live", 3)
        QTest.qWait(200)
        assert not native.submissions
        session._dismiss_popup(session.prompt)
        wait(lambda: len(native.submissions) == 1)

        session.show_message("另一张卡片")
        expired = queue_soft("expires-waiting", 0.8)
        deadline = datetime.now(timezone.utc) + timedelta(seconds=1.1)
        session._action("reminder.create", task_id="fixed", version=0, payload={"draft": {
            "mode": "hard", "content": "准点提醒", "remind_after": deadline.isoformat(),
            "remind_by": deadline.isoformat(), "sound": False,
        }})
        wait(lambda: "fixed" in session.window.views)
        session._action("reminder.confirm", task_id="fixed", version=1)
        wait(lambda: session.prompt and session.prompt.painted and session.prompt.command.task_id == "fixed")
        assert expired not in session._reminder_pending
        session._dismiss_popup(session.prompt)
        QTest.qWait(300)
        assert len(native.submissions) == 1
        assert not session.window.errors


def _exercise_edited_native_fallback(data_dir, outcome):
    from PyQt6.QtWidgets import QApplication

    class Notifications(_SimulatedNotifications):
        can_verify_early_transfer = True

        def __init__(self):
            super().__init__(can_schedule=True)
            self.removals = []

        def remove_pending(self, identifier):
            self.removals.append(identifier)
            return True

    native = Notifications()
    task_id = "edited-fallback"
    with _desktop(data_dir, "edited-soft-fallback", native) as (session, wait):
        session.presentation.native = True
        beeps = []
        QApplication.beep = lambda: beeps.append(True)
        wait(lambda: session._system2 and session.window.ready_for_actions and session.snapshot.get("settings"))
        clock = session._system2.gateway.now
        deadline = clock() + timedelta(seconds=40)
        session._action("reminder.create", task_id=task_id, version=0, payload={"draft": {
            "content": "原提醒", "mode": "soft", "sound": True,
            "remind_after": clock().isoformat(), "remind_by": deadline.isoformat(),
        }})
        wait(lambda: task_id in session.window.views)
        session._action("reminder.confirm", task_id=task_id, version=1)
        wait(lambda: native.submissions)
        original_delivery = f"delivery:{task_id}:1:v2"
        original_os_id = _native_id(data_dir, original_delivery)
        assert native.submissions == [(original_os_id, deadline, False)]

        original = session.window.views[task_id]
        session._action("reminder.edit", task_id=task_id, version=original["version"], payload={"draft": {
            **original["draft"], "content": "更新后的提醒", "sound": False,
        }})
        current = wait(lambda: session.window.views[task_id]["version"] == 3 and session.window.views[task_id])
        assert current["occurrence"] == 1
        scheduler = session._system2.scheduler
        occurrence = f"{task_id}:1"
        now = clock()
        candidates = scheduler.snapshot(now)["eligible_items"]
        assert any(item["occurrence"]["id"] == occurrence for item in candidates)
        result = scheduler.present(occurrence_ids=[occurrence], presentation={"channel": "pet_card", "sound": True},
                                   reason_code="edited_reminder", now=now,
                                   authority=SimpleNamespace(expires_at=now + timedelta(seconds=18)))
        assert result["status"] == "pending" and result["delivery_id"] == f"delivery:{task_id}:1:v3", result
        wait(lambda: native.removals)
        assert native.removals == [original_os_id]
        assert session.prompt is None and not beeps
        assert len(native.submissions) == 1

        def rows():
            return {row["delivery_id"]: row for row in session.presentation.ledger.rows()}

        if outcome == "unverified":
            native.inventory = lambda complete: complete(None, None)
            wait(lambda: rows()[original_delivery]["state"] == "unknown")
            assert session.prompt is None and not beeps
            assert result["delivery_id"] not in rows()
        elif outcome == "snooze":
            session._respond_to_reminder("snooze", task_id, 1, seconds=300)
            wait(lambda: session.window.views[task_id]["occurrence"] == 2)
            native.pending.remove(original_os_id)
            wait(lambda: len(native.submissions) == 2)
            assert native.submissions[1][0] == _native_id(data_dir, f"delivery:{task_id}:2:v4")
            assert rows()[original_delivery]["state"] == "cancelled"
            assert session.prompt is None and not beeps
            assert result["delivery_id"] not in rows()
        else:
            native.pending.remove(original_os_id)
            wait(lambda: session.prompt and session.prompt.painted)
            assert session.prompt.command.delivery_id == result["delivery_id"]
            assert session.prompt.text == "提醒：更新后的提醒" and not beeps
            assert rows()[original_delivery]["state"] == "cancelled"
            assert rows()[result["delivery_id"]]["state"] == "painted"
            assert len(native.submissions) == 1
        assert not session.window.errors


if __name__ == "__main__":
    {
        "actions": _exercise_native_actions,
        "sound": _exercise_same_batch_sound,
        "ownership": _exercise_reserved_ownership,
        "grouped-sound": _exercise_native_grouped_sound,
        "mixed": _exercise_mixed_background_batch,
        "soft-slot": _exercise_soft_system_slot,
        "edited-fallback-paint": lambda path: _exercise_edited_native_fallback(path, "paint"),
        "edited-fallback-unverified": lambda path: _exercise_edited_native_fallback(path, "unverified"),
        "edited-fallback-snooze": lambda path: _exercise_edited_native_fallback(path, "snooze"),
    }[sys.argv[2]](sys.argv[1])
