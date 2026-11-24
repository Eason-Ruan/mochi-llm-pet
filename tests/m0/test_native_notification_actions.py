"""Exercise macOS notification actions without submitting desktop notifications."""
from contextlib import closing
from datetime import timedelta
import sqlite3
from threading import Event, Lock
from types import SimpleNamespace

import pytest

from deskpet.c_ui.native_notifications import (
    ACK_ACTION,
    GENERIC_BODY,
    GENERIC_TITLE,
    REMINDER_CATEGORY,
    SNOOZE_ACTION,
    SNOOZE_CATEGORY,
    MacNotifications,
    _foreground_decision,
    notification_id,
)
from deskpet.contracts import TaskCommand
from deskpet.mocks.clock import FakeClock
from deskpet.mocks.delivery import FakeDeliveryPort
from deskpet.system2.scheduler import Scheduler
from deskpet.task_service.presentation import PresentationLedger
from deskpet.task_service.sqlite import SQLiteTaskService


def test_a_foreground_deadline_falls_back_and_rejects_a_late_paint():
    offered, completed = [], []
    ready = Event()

    def complete(options):
        completed.append(options)
        ready.set()

    _foreground_decision("deadline", 9, complete, lambda _, decide: offered.append(decide))
    assert ready.wait(2)
    unexpected_paints = []
    assert not offered[0](True, permit=lambda: unexpected_paints.append(True))
    assert completed == [9]
    assert not unexpected_paints


def test_native_actions_are_authenticated_generic_and_buffered_until_ready():
    native = pytest.importorskip("UserNotifications")
    actions = MacNotifications.__new__(MacNotifications)
    actions._response_lock = Lock()
    actions._response_handler = None
    actions._queued_responses = []
    actions.available = actions.sound_available = True
    actions.delegate = MacNotifications._delegate_class().alloc().init()
    actions.delegate.response_handler = actions._receive_response
    actions.delegate.foreground_handler = None
    registered, requests, submitted = {}, [], []

    def submit(request, complete):
        requests.append(request)
        complete(None)

    actions.center = SimpleNamespace(
        setNotificationCategories_=lambda categories: registered.update(
            (str(category.identifier()), category) for category in categories),
        addNotificationRequest_withCompletionHandler_=submit,
    )
    actions._register_categories()
    assert {name: [str(action.identifier()) for action in category.actions()]
            for name, category in registered.items()} == {
        REMINDER_CATEGORY: [ACK_ACTION],
        SNOOZE_CATEGORY: [ACK_ACTION, SNOOZE_ACTION],
    }
    assert all(action.options() == native.UNNotificationActionOptionAuthenticationRequired
               for category in registered.values() for action in category.actions())

    identifier = notification_id("private reminder", scope="profile A")
    assert identifier != notification_id("private reminder", scope="profile B")
    for snooze in (False, True):
        actions.submit(identifier, None, sound=False, snooze=snooze,
                       complete=lambda *result: submitted.append(result))
    assert submitted == [("submitted", None)] * 2
    assert [str(request.content().categoryIdentifier()) for request in requests] == [REMINDER_CATEGORY, SNOOZE_CATEGORY]
    for request in requests:
        assert request.identifier() == identifier
        assert request.content().title() == GENERIC_TITLE
        assert request.content().body() == GENERIC_BODY
        assert request.content().sound() is None
        assert not request.content().userInfo()

    completed, received = [], []
    callbacks = (
        (requests[0], ACK_ACTION),
        (requests[0], SNOOZE_ACTION),
        (requests[1], SNOOZE_ACTION),
        (requests[1], native.UNNotificationDefaultActionIdentifier),
        (requests[1], native.UNNotificationDismissActionIdentifier),
    )
    handler = actions.delegate.userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_
    for request, action in callbacks:
        response = SimpleNamespace(
            notification=lambda: SimpleNamespace(request=lambda: request),
            actionIdentifier=lambda: action,
        )
        handler(None, response, lambda: completed.append(True))
    assert len(completed) == len(callbacks)
    actions.set_response_handler(lambda *response: received.append(response))
    assert received == [(identifier, action) for action in ("ack", "snooze", "open")]


def test_reopened_ledger_rejects_foreign_profile_expired_or_legacy_button_responses(tmp_path):
    path = tmp_path / "business.sqlite3"
    copied = tmp_path / "copy.sqlite3"
    clock = FakeClock()
    now = clock.wall_now()
    due = now + timedelta(minutes=1)
    expiry = due + timedelta(minutes=1)
    ledger = PresentationLedger(path)
    identifiers = {}
    with SQLiteTaskService(clock, FakeDeliveryPort(clock), lambda *_args, **_fields: None, path=path) as tasks:
        for name, actionable in (("new", True), ("legacy", False)):
            response = tasks.apply(TaskCommand(
                command_id=f"create-{name}", kind="create_registered", task_id=name,
                payload={"draft": {"content": name, "mode": "hard", "remind_after": due,
                                   "remind_by": due, "target_at": due, "expires_at": expiry}},
            ))
            delivery = f"delivery:{name}:1:v1"
            identifier = identifiers[name] = notification_id(delivery, scope=path.as_uri() if actionable else "")
            assert ledger.prepare_os(response.view, delivery, identifier, now, native_actions=actionable)
            assert ledger.update(delivery, {"submitting"}, "scheduled", now)
    with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(copied)) as target:
        source.backup(target)

    restarted = PresentationLedger(path)
    assert restarted.native_response_target(identifiers["new"], "open", now) is None
    assert restarted.native_response_target(identifiers["new"], "ack", due).task_id == "new"
    assert restarted.native_response_target(identifiers["new"], "ack", now, record_exposure=True) is None
    assert restarted.native_response_target(identifiers["new"], "open", expiry) is None
    assert restarted.native_response_target(identifiers["legacy"], "ack", due) is None
    assert restarted.native_response_target(identifiers["legacy"], "open", due).task_id == "legacy"
    assert PresentationLedger(copied).native_response_target(identifiers["new"], "open", due) is None


@pytest.mark.parametrize(("action", "expected_response"), [("ack", "acknowledged"), ("snooze", "snoozed")])
def test_positive_native_callback_credits_early_delivery_once_before_inventory(tmp_path, action, expected_response):
    path = tmp_path / "business.sqlite3"
    clock = FakeClock()
    now = clock.wall_now()
    ledger = PresentationLedger(path)
    with SQLiteTaskService(clock, FakeDeliveryPort(clock), lambda *_args, **_fields: None, path=path) as tasks:
        task = tasks.apply(TaskCommand(
            command_id="create-early", kind="create_registered", task_id="early",
            payload={"draft": {"content": "Early reminder", "mode": "soft",
                               "remind_after": now, "remind_by": now + timedelta(minutes=10)}},
        )).view
        delivery = "delivery:early:1:v1"
        identifier = notification_id(delivery, scope=path.as_uri())
        assert ledger.prepare_os(task, delivery, identifier, now, native_actions=True)
        # The delegate can receive an action before the submit callback or inventory.
        assert ledger.native_response_target(identifier, "open", now) is None
        assert ledger.native_response_target(identifier, "open", now, record_exposure=True) == task
        assert ledger.native_response_target(identifier, action, now, record_exposure=True) == task
        payload = {"seconds": 300} if action == "snooze" else {}
        result = tasks.apply(TaskCommand(command_id=action, kind=action, task_id="early",
                                         expected_version=1, payload=payload))
        assert result.status == "applied"
        assert ledger.native_response_target(identifier, action, now, record_exposure=True) is None
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("""SELECT json_extract(fields, '$.payload.reason') FROM events
                             WHERE kind='os.delivered'""").fetchall() == [("user_response",)]
    contact, = Scheduler(path, clock=clock).snapshot(now)["recent_contacts"]
    assert contact["occurrence_id"] == task.occurrence_id and contact["response"] == expected_response
    assert contact["primary_channel"] == "os_notification" and contact["exposure"] == "reported_delivered"
    assert contact["surfaces"] == [{"channel": "os_notification", "exposure": "reported_delivered", "active": True}]
