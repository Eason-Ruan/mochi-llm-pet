"""Exercise the new Qt entry through the same actions and services as the workbench."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


CODE = r'''
from datetime import datetime, timedelta
from pathlib import Path
import sys
from time import monotonic
from types import SimpleNamespace

from PyQt6 import sip
from PyQt6.QtCore import QEvent, QPoint, QPointF, QSettings, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QLabel, QLineEdit, QPushButton, QScrollArea, QWidget

from deskpet.c_ui.refine import RefineSession
from deskpet.c_ui.time_format import local_time

app = QApplication([])
app.setQuitOnLastWindowClosed(False)
output = Path(sys.argv[1])
settings = QSettings(str(output / 'settings.ini'), QSettings.Format.IniFormat)
session = RefineSession(app, SimpleNamespace(
    profile='scenario', scenario='ui-refine-regression', output=str(output), auto_smoke=False,
    adapters='examples/m0/ui-refine.adapters.json',
), settings=settings)
pet, window, chat = session.pet, session.window, session.chat


def wait_for(predicate, message):
    deadline = monotonic() + 4
    while not predicate() and monotonic() < deadline:
        QTest.qWait(15)
    assert predicate(), (message, window.errors)


def visible_widget(parent, cls, object_name):
    matches = [widget for widget in parent.findChildren(cls, object_name) if widget.isVisible()]
    return matches[-1] if matches else None


def click(parent, object_name, cls=QPushButton):
    find = lambda: visible_widget(parent, cls, object_name)
    wait_for(find, f'{object_name} did not become visible')
    button = find()
    assert button.isEnabled(), object_name
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    return button


def hold(parent, object_name, changed):
    # Wait until the opening transition and deferred editor focus have settled.
    wait_for(lambda: parent.graphicsEffect() is None, f"{object_name} transition did not finish")
    find = lambda: visible_widget(parent, QPushButton, object_name)
    wait_for(find, f'{object_name} did not become visible')
    button = find()
    QTest.mousePress(button, Qt.MouseButton.LeftButton)
    try:
        wait_for(changed, f"{object_name} did not respond to a held press")
    finally:
        receiver = button if not sip.isdeleted(button) and button.isVisible() else parent
        QTest.mouseRelease(receiver, Qt.MouseButton.LeftButton)


def choose(menu):
    buttons = {
        key: pet.findChild(QPushButton, f'refine-menu-{key}')
        for key in ('reminders', 'work', 'input', 'settings')
    }
    click(pet, f'refine-menu-{menu}')
    assert [key for key, button in buttons.items() if button.property('selected')] == [menu]


def send(text):
    find = lambda: visible_widget(chat, QLineEdit, 'refine-chat-editor')
    wait_for(find, 'typing editor did not become visible')
    find().setText(text)
    click(chat, 'refine-chat-send')


def messages():
    return [label.text() for label in chat.findChildren(QLabel, 'refine-chat-message-text')
            if label.isVisible()]


def current_sessions():
    return session.snapshot.get('sessions', [])


try:
    wait_for(lambda: len(window.views) == 3 and all(
        task['status'] == 'registered' for task in window.views.values()), 'seed tasks did not register')
    assert pet.isVisible() and not window.isVisible()
    clock_start = datetime.fromisoformat(session.snapshot['now'])
    wait_for(lambda: datetime.fromisoformat(session.snapshot['now']) > clock_start + timedelta(seconds=.2),
             'scenario clock did not advance with elapsed time')

    choose('settings')
    click(session.settings_panel, 'refine-settings-typing')
    choose('input')
    milk_before = window.views['demo-milk']
    wait_for(lambda: visible_widget(chat, QLineEdit, 'refine-chat-editor'), 'typing preference ignored')
    hold(chat, 'refine-chat-mode', lambda: visible_widget(chat, QLabel, 'refine-chat-elapsed'))
    click(chat, 'refine-chat-stop')
    assert settings.value('defaultInput') == 'typing'
    wait_for(lambda: any('模拟语音' in label.text() for label in chat.findChildren(QLabel) if label.isVisible()),
             'simulated transcript was not labelled')
    wait_for(lambda: any('确认' in text for text in messages()), 'script did not request confirmation')
    assert window.views['demo-milk']['version'] == milk_before['version']
    assert len(messages()) == 2
    click(chat, 'refine-chat-continue')
    send('确认延后。')
    wait_for(lambda: window.views['demo-milk']['version'] == milk_before['version'] + 1,
             'confirmed edit did not reach the shared task service')
    wait_for(lambda: any('已将' in text for text in messages()), 'script did not show applied result')
    milk_after = window.views['demo-milk']
    new_due = local_time(milk_after['draft']['earliest_at'])
    assert new_due.date() == local_time(clock_start).date() + timedelta(days=1)
    assert (new_due.hour, new_due.minute, new_due.second) == (18, 0, 0)
    assert any(task == milk_after for task in session.snapshot['tasks'])
    assert len(messages()) == 4
    assert not chat.findChild(QPushButton, 'refine-chat-history-up')
    assert not chat.findChild(QPushButton, 'refine-chat-history-down')
    history = chat.findChild(QScrollArea, 'refine-chat-history')
    grip = chat.findChild(QWidget, 'refine-chat-height-grip')
    pet._home_anchor = QPoint(50, 40)
    pet._layout_pet()
    initial_height = chat.height()
    direction = -1 if chat._panel_above else 1
    origin = grip.mapToGlobal(grip.rect().center())
    target = origin + QPoint(0, direction * 48)
    left = Qt.MouseButton.LeftButton
    for kind, point, button, buttons in (
        (QEvent.Type.MouseButtonPress, origin, left, left),
        (QEvent.Type.MouseMove, target, Qt.MouseButton.NoButton, left),
        (QEvent.Type.MouseButtonRelease, target, left, Qt.MouseButton.NoButton),
    ):
        QApplication.sendEvent(grip, QMouseEvent(
            kind, QPointF(grip.mapFromGlobal(point)), QPointF(point),
            button, buttons, Qt.KeyboardModifier.NoModifier,
        ))
    assert chat.height() == initial_height + 48
    assert settings.value('chatHeight', type=int) == chat.height()
    chat.open('typing')
    wait_for(lambda: visible_widget(chat, QLineEdit, 'refine-chat-editor'), 'composer did not appear')
    editor = visible_widget(chat, QLineEdit, 'refine-chat-editor')
    assert editor is not None
    editor.setText('暂存的输入')
    for _ in range(6):
        chat.append_message('我可以帮你做这三件事。\n\n**提醒与事项**：' + '滚动验证。' * 14)
    QTest.qWait(60)
    body = chat._message_cards.itemAt(chat._message_cards.count() - 1).widget().body
    assert body.height() == body.heightForWidth(body.width())
    assert body.document.toPlainText().startswith('我可以帮你做这三件事。')
    assert '**' not in body.document.toPlainText()
    bar = history.verticalScrollBar()
    wait_for(lambda: bar.maximum() > 0, 'scrollbar did not appear')
    QTest.qWait(60)
    bar.setValue(0)
    chat.append_message('浏览历史时收到的新消息。')
    QTest.qWait(60)
    assert bar.value() == 0
    editor = visible_widget(chat, QLineEdit, 'refine-chat-editor')
    assert editor is not None and editor.text() == '暂存的输入'
    bar.setValue(bar.maximum())
    assert bar.value() > 0
    chat.reset_input()

    session.show_message('一条可以关闭的测试消息')
    wait_for(lambda: session.prompt is not None and session.prompt.kind == 'message',
             'plain proactive bubble did not appear')
    session.request_demo_invite()
    wait_for(lambda: any(item.kind == 'pomodoro' for item in session.waiting_prompts),
             'Pomodoro invitation did not queue behind the message')
    click(pet, 'refine-bubble-dismiss', QLabel)
    wait_for(lambda: session.prompt is not None and session.prompt.kind == 'pomodoro',
             'dismissing the message did not advance the queue')
    click(pet, 'refine-bubble-no')
    assert session.prompt is None
    assert not current_sessions()

    session.request_demo_invite()
    wait_for(lambda: session.prompt is not None and session.prompt.kind == 'pomodoro',
             'Pomodoro invitation could not be repeated')
    session.answer_prompt(True)
    session.answer_prompt(True)
    wait_for(current_sessions, 'accepted Pomodoro did not reach the shared session service')
    assert len(current_sessions()) == 1
    work_id = current_sessions()[0]['session_id']
    assert current_sessions()[0]['state'] == 'focusing'
    assert current_sessions()[0]['task_id'] == 'demo-report'
    assert window.session_views[work_id]['remaining_s'] > 1495
    wait_for(lambda: pet.findChild(QPushButton, 'refine-menu-work').property('selected'),
             'accepting did not open the work summary')
    remaining = current_sessions()[0]['remaining_s']
    window.action.emit({'kind': 'advance', 'seconds': 20})
    wait_for(lambda: current_sessions()[0]['remaining_s'] <= remaining - 20,
             'fast-forward did not advance the same timer')

    session.trigger_next_reminder()
    wait_for(lambda: session.prompt is not None and session.prompt.kind == 'reminder',
             'due reminder did not reach the external owner')
    task_id = session.prompt.command.task_id
    assert not window.isVisible(), 'a reminder activated the diagnostic window'
    pet.grab()
    pet.diagnostics_requested.emit()
    wait_for(window.isVisible, 'diagnostic workbench did not reopen from the pet')
    window.tabs.setCurrentWidget(window.ui_surface)
    QTest.qWait(400)
    assert not window.receipts and window.views[task_id]['displayed_at'] is None
    click(pet, 'refine-bubble-no')
    assert window.views[task_id]['status'] == 'registered'
    assert not window.errors
finally:
    session.quit()
    wait_for(lambda: not window.thread.isRunning(), 'shared worker did not stop')
'''


def test_ui_refine_shared_script_clock_and_reminder_owner(tmp_path):
    process = subprocess.run(
        [sys.executable, '-c', CODE, str(tmp_path)], cwd=ROOT,
        env=dict(os.environ, QT_QPA_PLATFORM='offscreen'),
        capture_output=True, text=True, timeout=35,
    )
    assert process.returncode == 0, process.stdout + process.stderr
