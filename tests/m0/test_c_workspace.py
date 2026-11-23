"""P0 must mount C's own factory and send actions through the real host queue."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def test_c_page_mounts_and_shares_sessions_and_reminders(tmp_path):
    code = r'''
import json, sys
from pathlib import Path
from types import SimpleNamespace
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QPushButton
from deskpet.c_ui.surface import CWorkspace
from deskpet.m0.gui import Workbench

app = QApplication([])
w = Workbench(SimpleNamespace(profile='scenario', scenario='c-p0', output=sys.argv[1],
    auto_smoke=False, adapters='examples/m0/c-product.adapters.json'))
p = w.ui_surface
assert isinstance(p, CWorkspace)
assert not p.start_button.isEnabled(), 'constructor must wait for a service snapshot'
w.show()
progress = {'step': 0, 'ticks': 0}
timer = QTimer()
def step():
    progress['ticks'] += 1
    assert progress['ticks'] < 200, (progress, w.errors, w.latest)
    n = progress['step']
    if n == 0 and w.ready_for_actions and p.snapshot:
        assert w.tabs.widget(2) is p
        w.tabs.setCurrentWidget(p)
        assert p.isVisible()
        p.goal.setText('   ')
        assert not p.start_button.isEnabled()
        p.goal.setText('C 页面共享工作段验证')
        p.work_panel.duration.setValue(60)
        p.start_button.click()
        assert not p.start_button.isEnabled(), 'in-flight command must prevent duplicate clicks'
        progress['step'] = 1
    elif n == 1 and p.snapshot['sessions'] and not p.work_panel.pending:
        assert len(w.session_views) == 1
        view = p.snapshot['sessions'][0]
        assert view == w.session_views[view['session_id']]
        assert view['remaining_s'] == 60 and view['goal'] == 'C 页面共享工作段验证'
        assert '已创建' in p.result.text()
        assert p.start_button.isEnabled()
        assert w.session_list.count() == 1
        assert 'C 页面共享工作段验证' in w.session_list.item(0).text()
        p.goal.setText('尚未提交的新草稿')
        w.session_buttons['pause'].click()
        progress['step'] = 2
    elif n == 2 and p.snapshot['sessions'][0]['state'] == 'paused':
        assert '已暂停' in p.session_summary.text()
        assert p.goal.text() == '尚未提交的新草稿', 'snapshot erased a draft'
        assert w.session_views[p.snapshot['sessions'][0]['session_id']]['state'] == 'paused'
        w.mode.setCurrentText('hard')
        w.delay.setValue(0)
        w.create_button.click()
        progress['step'] = 3
    elif n == 3 and 'gui-1' in w.views:
        w.confirm_button.click()
        progress['step'] = 4
    elif n == 4 and w.views['gui-1']['displayed_at']:
        assert len(p.cards) == 1 and not w.cards
        assert w.tabs.currentWidget() is p
        card = next(iter(p.cards.values()))
        assert w.receipts[card.command.delivery_id].source == 'offscreen'
        p.show_reminder(card.command)
        assert len(p.cards) == 1
        done = next(b for b in card.findChildren(QPushButton) if b.text() == '已完成')
        done.click()
        progress['step'] = 5
    elif n == 5 and w.views['gui-1']['status'] == 'completed' and not p.cards:
        assert not w.errors, w.errors
        assert p.empty_reminders.isVisible()
        w._grab_evidence(Path(sys.argv[1]) / 'c-p0.png')
        Path(sys.argv[1], 'c-p0.json').write_text(json.dumps({
            'status': 'passed', 'surface': type(p).__module__ + ':' + type(p).__name__,
            'session': p.snapshot['sessions'][0], 'task': w.views['gui-1'],
            'receipt_count': len(w.receipts), 'checks': progress,
        }, ensure_ascii=False))
        timer.stop()
        w.close()
timer.timeout.connect(step)
timer.start(30)
app.exec()
'''
    process = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)], cwd=ROOT,
        env=dict(os.environ, QT_QPA_PLATFORM="offscreen"),
        capture_output=True, text=True, timeout=20,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    report = json.loads((tmp_path / "c-p0.json").read_text())
    assert report["status"] == "passed"
    assert report["session"]["state"] == "paused"
    assert report["task"]["status"] == "completed"
    assert report["receipt_count"] == 1
    events = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    assert any(e["kind"] == "ui.action" and e["payload"]["action_id"] == "session.start" for e in events)
    assert (tmp_path / "c-p0.png").stat().st_size > 1000


def test_c_config_stays_headless_for_replay():
    process = subprocess.run([sys.executable, "-c", '''
import sys
from deskpet.m0.replay import run_suite
report = run_suite(adapters="examples/m0/c-product.adapters.json")
assert report["status"] == "passed"
assert not any(name.startswith("PyQt6") for name in sys.modules)
assert "deskpet.c_ui.surface" not in sys.modules
'''], cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert process.returncode == 0, process.stdout + process.stderr


def test_full_c_work_session_flow(tmp_path):
    process = subprocess.run(
        [sys.executable, "scripts/smoke_c_work_sessions.py", "--offscreen", "--output", str(tmp_path)],
        cwd=ROOT, capture_output=True, text=True, timeout=25,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    report = json.loads((tmp_path / "c-work-sessions-report.json").read_text())
    assert report["status"] == "passed" and len(report["checks"]) == 18
    assert not report["native_gui_verified"] and report["source"] == "offscreen"
    assert all(report["checks"].values())
    assert report["task"]["status"] == "registered"
    assert report["sessions"][0]["remaining_s"] == 30
    assert report["sessions"][1]["remaining_s"] == 0
    events = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    assert len([e for e in events if e["kind"] == "session.break_due"]) == 1
    assert len([e for e in events if e["kind"] == "session.applied" and e["payload"]["kind"] == "end"]) == 2
    from deskpet.c_ui.review_evidence import summarize_reviews
    evidence = summarize_reviews(events)
    assert evidence["status"] == "passed" and evidence["linked_end_records"] == 2
    assert evidence["review_statuses"] == {"confirmed": 1, "confirmed_none": 1}
    assert evidence["real_user_confirmations"] == 0


def test_review_conflicts_keep_drafts_and_refresh_expected_version(tmp_path):
    code = r'''
from PyQt6.QtWidgets import QApplication
from deskpet.c_ui.work_panel import WorkPanel
app = QApplication([])
calls = []
def submit(action, **kwargs):
    calls.append((action, kwargs))
    return str(len(calls))
p = WorkPanel(submit=submit)
p.show()
def view(key, version=1, state='focusing'):
    return dict(session_id=key, version=version, state=state, goal=key,
        remaining_s=60, task_id=None, confirmed_outcomes=[], next_steps=[])
def snapshot(first):
    return {'sessions':[first, view('other')], 'tasks':[]}
assert not any(b.isEnabled() for b in p.buttons.values())
p.render_snapshot(snapshot(view('first')))
p.sessions.setCurrentRow(0)
p.buttons['end'].click()
p.outcomes.setPlainText('草稿成果')
p.next_steps.setPlainText('草稿下一步')
p.confirm_none.setChecked(True)
p.confirm_end.click()
assert not p.pending and not calls, 'contradictory review was submitted'
p.confirm_none.setChecked(False)
p.confirm_end.click()
assert p.pending and p.outcomes.isReadOnly()
p.receive_result({'command_id':'unrelated', 'status':'applied'})
assert p.pending, 'unrelated result cleared in-flight state'
p.receive_result({'command_id':'1', 'status':'rejected', 'error_code':'STALE_TASK'})
assert not p.pending and p.review_editor.isVisible()
assert p.outcomes.toPlainText() == '草稿成果' and '已保留' in p.result.text()
p.render_snapshot(snapshot(view('first', 2, 'paused')))
p.confirm_end.click()
assert calls[-1][1]['payload']['expected_session_version'] == 2
assert calls[-1][1]['payload']['confirmed_outcomes'] == ['草稿成果']
p.receive_result({'command_id':'2', 'status':'rejected', 'error_code':'INVALID_ARGUMENT'})
p.sessions.setCurrentRow(1)
p.buttons['end'].click()
assert not p.outcomes.toPlainText() and not p.next_steps.toPlainText()
p.outcomes.setPlainText('另一段草稿')
p.cancel_end.click()
p.sessions.setCurrentRow(0)
p.buttons['end'].click()
assert p.outcomes.toPlainText() == '草稿成果'
p.render_snapshot(snapshot(view('first', 3, 'ended')))
assert not p.confirm_end.isEnabled()
assert p.outcomes.toPlainText() == '草稿成果' and '其他入口结束' in p.review_notice.text()
p.cancel_end.click()
p.sessions.setCurrentRow(1)
p.buttons['end'].click()
assert p.outcomes.toPlainText() == '另一段草稿'
def closed(*args, **kwargs):
    raise RuntimeError('closed')
p.submit = closed
p.confirm_end.click()
assert p.pending is None and p.outcomes.toPlainText() == '另一段草稿'
assert '已保留' in p.result.text()
p.close()
'''
    process = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
        env=dict(os.environ, QT_QPA_PLATFORM="offscreen"), capture_output=True, text=True, timeout=15)
    assert process.returncode == 0, process.stdout + process.stderr
