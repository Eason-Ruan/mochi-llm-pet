"""C reminder behavior: actual queued flow, visible receipts and stale UI safety."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def test_complete_c_reminder_flow(tmp_path):
    result = subprocess.run([sys.executable, "scripts/smoke_c_reminders.py", "--offscreen", "--output", str(tmp_path)],
                            cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((tmp_path / "c-reminders-report.json").read_text())
    assert report["status"] == "passed" and len(report["checks"]) == 26
    assert not report["native_gui_verified"]
    views = {v["task_id"]: v for v in report["tasks"]}
    hard = views[report["task_ids"]["hard"]]
    assert hard["status"] == "completed" and hard["version"] == 4 and hard["occurrence"] == 2
    assert views[report["task_ids"]["cancel"]]["status"] == "cancelled"
    events = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    displayed = [e for e in events if e["kind"] == "delivery.displayed"]
    assert len(displayed) == 4 and all(e["source"] == "offscreen" for e in displayed)
    requests = {e["delivery_id"] for e in events if e["kind"] == "delivery.requested"}
    assert {e["delivery_id"] for e in displayed} <= requests
    assert any(e["kind"] == "delivery.failed" and e["delivery_id"] == report["failed_delivery"] for e in events)


def test_card_visibility_retirement_and_edit_conflicts(tmp_path):
    code = r'''
from datetime import datetime, timedelta, timezone
from PyQt6.QtCore import QEventLoop, QTimer
from PyQt6.QtWidgets import QApplication, QWidget, QVBoxLayout
from deskpet.contracts import DisplayCommand
from deskpet.c_ui.reminder import ReminderCard, ReminderPanel
from deskpet.m0.ui_surface import SurfaceContext

app = QApplication([])
now = datetime(2026, 1, 1, 17, tzinfo=timezone.utc)
calls, reports = [], []
def submit(action, **kwargs):
    calls.append((action, kwargs))
    return str(len(calls))
panel = ReminderPanel(submit=submit)
assert not panel.create_button.isEnabled()
def snapshot(version=2, content='事项'):
    return dict(now=now.isoformat(), tasks=[dict(task_id='task', version=version,
        status='registered', displayed_at=None, occurrence=1,
        draft=dict(content=content, mode='hard', earliest_at=now.isoformat(), latest_first_delivery_at=now.isoformat()))])
panel.render_snapshot(snapshot())
panel.open_edit('task')
panel.content.setText('保留我的修改')
panel.render_snapshot(snapshot(3, '外部修改'))
assert panel.content.text() == '保留我的修改'
panel.save_edit.click()
assert not calls and not panel.pending, 'stale editor changed a newer task'
panel.open_edit('task')
assert panel.content.text() == '外部修改'
panel.content.setText('新的修改')
panel.save_edit.click()
assert panel.pending and calls[-1][1]['version'] == 3
panel.receive_result(dict(command_id='unrelated', status='applied'))
assert panel.pending
panel.receive_result(dict(event_id='1', status='rejected', error_code='STALE_SURFACE'))
assert not panel.pending and panel.content.text() == '新的修改'
assert '已保留' in panel.result.text()
panel.save_edit.click()
panel.receive_result(dict(command_id='2', status='duplicate', message='replayed original rejected result'))
assert panel._editing and panel.content.text() == '新的修改'
panel.cancel_edit.click()
panel.content.setText('  ')
assert not panel.create_button.isEnabled()

allow = {'visible': False}
context = SurfaceContext(submit=lambda v: None,
    report=lambda delivery_id, outcome, error: reports.append(delivery_id) or True,
    can_report=lambda: allow['visible'])
command = DisplayCommand(delivery_id='visible', task_id='task', task_version=3, occurrence=1,
    content='<b>事项</b>', requested_at=now, expires_at=now+timedelta(seconds=30), reason='hard_due')
root = QWidget()
layout = QVBoxLayout(root)
card = ReminderCard(command, context=context, controller=panel)
layout.addWidget(card)
card._acknowledge()
assert not reports, 'hidden card reported a display'
def pump():
    loop = QEventLoop()
    QTimer.singleShot(80, loop.quit)
    loop.exec()
root.show()
pump()
root.grab()
pump()
assert not reports, 'capture-only paint created a receipt'
allow['visible'] = True
panel.snapshot['now'] = (now-timedelta(seconds=1)).isoformat()
card.update()
pump()
assert not reports, 'card reported before the request clock snapshot arrived'
panel.snapshot['now'] = now.isoformat()
card.refresh()
pump()
assert reports == ['visible'], (reports, card.isVisible(), card.visibleRegion().isEmpty(), card._valid(), card._reported, context.can_report_display)
card.update()
pump()
assert reports == ['visible'], 'second paint reported twice'
card.snooze_seconds.setValue(10)
card.snooze.click()
assert calls[-1][0] == 'reminder.snooze'
assert calls[-1][1]['payload'] == {'seconds':10}
assert not card.controller.busy('missing')
card.retire()
before = len(calls)
card._action('done')
card._acknowledge()
assert len(calls) == before and reports == ['visible']
panel.receive_result(dict(command_id='3', status='applied'))
panel.render_snapshot(snapshot(4))
card._action('done')
assert len(calls) == before
root.close()
panel.close()
'''
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                            env=dict(os.environ, QT_QPA_PLATFORM="offscreen"),
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
