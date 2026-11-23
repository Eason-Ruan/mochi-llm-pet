"""C reminder workflow through actual widgets, queued commands and display receipts."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import traceback
from types import SimpleNamespace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='output/m0/c-p2/native')
    parser.add_argument('--offscreen', action='store_true')
    parser.add_argument('--adapters', default='examples/m0/c-product.adapters.json')
    args = parser.parse_args()
    if args.offscreen:
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    from PyQt6.QtCore import QTimer
    from PyQt6.QtGui import QGuiApplication
    from PyQt6.QtWidgets import QApplication
    from deskpet.m0.gui import Workbench
    from deskpet.m0.composition import provenance

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    options = SimpleNamespace(profile='scenario', scenario='c-reminders', output=str(output),
        auto_smoke=False, adapters=args.adapters)
    w = Workbench(options)
    p = w.ui_surface
    panel = p.reminder_panel
    source = 'offscreen' if QGuiApplication.platformName() in ('offscreen', 'minimal') else 'native'
    checks, report, ids = {}, {}, {}
    state = dict(index=0, sent=False, waits=0)
    timer = QTimer()

    def view(key):
        return panel.view(ids.get(key)) or {}

    def ready():
        return bool(w.ready_for_actions and p.snapshot and (source == 'offscreen'
            or (w.isActiveWindow() and w.windowHandle() and w.windowHandle().isExposed())))

    def create(title, mode='hard', delay=10, window=20):
        options.auto_smoke = True  # host readiness has completed; tag harness actions
        w.tabs.setCurrentWidget(p)
        panel.content.setText(title)
        panel.mode.setCurrentIndex(panel.mode.findData(mode))
        panel.delay.setValue(delay)
        panel.window.setValue(window)
        p.scroll.ensureWidgetVisible(panel.create_button)
        panel.create_button.click()
        assert not panel.create_button.isEnabled()

    def created(key, title):
        selected = panel.selected()
        if not (selected and selected['draft']['content'] == title and not panel.pending):
            return False
        ids[key] = selected['task_id']
        assert selected['status'] == 'draft' and selected['version'] == 1
        return True

    def confirm():
        panel.buttons['confirm'].click()

    def registered(key, version=2):
        return not panel.pending and view(key).get('status') == 'registered' and view(key).get('version') == version

    def advance(seconds):
        state['before'] = w.latest['now']
        w.action.emit(dict(kind='advance', seconds=seconds))

    def advanced():
        return w.latest['now'] != state['before'] and not panel.pending

    def shown(key):
        v = view(key)
        cards = [c for c in p.cards.values() if c.command.task_id == ids[key]]
        return bool(v.get('displayed_at') and len(cards) == 1 and cards[0]._reported)

    def card(key):
        return next(c for c in p.cards.values() if c.command.task_id == ids[key])

    def duplicate_and_snooze():
        c = card('hard')
        state['old_card'] = c
        state['old_command'] = c.command
        p.show_reminder(c.command)
        assert len(p.cards) == 1 and not w.cards
        assert not p.context.report_display(c.command.delivery_id)
        assert w.receipts[c.command.delivery_id].source == source
        c.snooze_seconds.setValue(10)
        w._grab_evidence(output / 'due-reminder.png')
        c.snooze.click()
        assert panel.busy(ids['hard'])

    def snoozed():
        if not (registered('hard', 3) and view('hard')['occurrence'] == 2 and not p.cards):
            return False
        assert view('hard')['displayed_at'] is None
        # A queued callback from a retired card must not operate on the new version.
        state['old_card']._action('done')
        assert not panel.pending
        assert '已延后' in panel.result.text()
        return True

    def complete():
        c = card('hard')
        assert c.command.delivery_id != state['old_command'].delivery_id
        assert c.command.task_id == ids['hard'] and c.command.task_version == 3
        c.done.click()

    def cancelled(key):
        return not panel.pending and view(key).get('status') == 'cancelled' and not p.cards

    def working():
        w.state.setCurrentText('working')
        w.correction_ttl.setValue(300)
        w.correction_apply.click()

    def edit():
        panel.buttons['edit'].click()
        panel.content.setText('修改后的提醒')
        panel.delay.setValue(20)
        panel.save_edit.click()

    def edit_saved():
        return registered('cancel', 3) and view('cancel')['draft']['content'] == '修改后的提醒'

    def late_old_request():
        # Re-delivery of the old completed occurrence must not reopen a card.
        w._display(state['old_command'])
        assert not p.cards
        assert view('hard')['status'] == 'completed'

    def failure_requested():
        failures = [r for r in w.receipts.values() if r.task_id == ids['retry'] and r.outcome == 'failed']
        if not failures:
            return False
        state['failed_delivery'] = failures[0].delivery_id
        assert not p.cards
        return True

    def recover():
        w.delivery_fault.setCurrentIndex(0)
        advance(1)

    def recovered():
        if not shown('retry'):
            return False
        assert card('retry').command.delivery_id == state['failed_delivery']
        return True

    steps = [
        (lambda: None, ready, 'ready'),
        (lambda: create('提交报告'), lambda: created('hard', '提交报告'), 'c_create_draft'),
        (lambda: advance(5), lambda: advanced() and not p.cards and not w.receipts, 'draft_not_scheduled'),
        (confirm, lambda: registered('hard') and not p.cards, 'c_confirm'),
        (lambda: advance(4), lambda: advanced() and not p.cards, 'not_early'),
        (lambda: advance(1), lambda: shown('hard'), 'hard_due_visible_receipt'),
        (duplicate_and_snooze, snoozed, 'snooze_same_task_and_retire_old_card'),
        (lambda: advance(9), lambda: advanced() and not p.cards, 'snooze_not_early'),
        (lambda: advance(1), lambda: shown('hard'), 'snoozed_occurrence_redisplayed'),
        (complete, lambda: not panel.pending and view('hard').get('status') == 'completed' and not p.cards, 'c_complete'),
        (late_old_request, lambda: not p.cards, 'late_completed_request_rejected'),
        (working, lambda: w.latest['observation']['estimate']['state'] == 'working', 'working_context'),
        (lambda: create('柔性提醒', 'soft', 5, 10), lambda: created('soft', '柔性提醒'), 'soft_draft'),
        (confirm, lambda: registered('soft'), 'soft_confirm'),
        (lambda: advance(5), lambda: advanced() and not p.cards, 'soft_waits_while_working'),
        (lambda: advance(10), lambda: shown('soft'), 'soft_deadline_fallback'),
        (lambda: card('soft').cancel.click(), lambda: cancelled('soft'), 'c_cancel_displayed'),
        (lambda: create('等待修改', delay=30), lambda: created('cancel', '等待修改'), 'editable_draft'),
        (confirm, lambda: registered('cancel'), 'editable_confirm'),
        (edit, edit_saved, 'c_edit_invalidates_old_schedule'),
        (lambda: panel.buttons['cancel'].click(), lambda: cancelled('cancel'), 'cancel_before_due'),
        (lambda: advance(60), lambda: advanced() and not p.cards, 'cancel_does_not_reappear'),
        (lambda: create('失败后重试', delay=0), lambda: created('retry', '失败后重试'), 'retry_draft'),
        (lambda: (w.delivery_fault.setCurrentText('渲染失败'), confirm()), failure_requested, 'render_failure_is_explicit'),
        (recover, recovered, 'retry_reuses_delivery_id'),
        (lambda: card('retry').done.click(), lambda: not panel.pending and view('retry').get('status') == 'completed' and not p.cards, 'recovered_complete'),
    ]

    def finish(error=None):
        timer.stop()
        p.scroll.ensureWidgetVisible(panel.details)
        w._grab_evidence(output / 'reminder-panel.png')
        snapshot = p.snapshot
        w.close()
        events = [json.loads(s) for s in (output / 'trace.jsonl').read_text().splitlines()]
        if not error:
            try:
                displayed = [e for e in events if e['kind'] == 'delivery.displayed']
                assert len(displayed) == 4 and all(e['source'] == source for e in displayed)
                assert not any(e['kind'] == 'delivery.requested' and e['task_id'] == ids['cancel'] for e in events)
                attempts = [e for e in events if e['kind'] == 'delivery.requested' and e['delivery_id'] == state['failed_delivery']]
                assert len(attempts) == 2
            except Exception:
                error = traceback.format_exc()
        report.update(provenance(), status='failed' if error or w.errors else 'passed', error=error,
            checks=checks, host_errors=w.errors, source=source,
            native_gui_verified=source == 'native' and not error and not w.errors,
            tasks=snapshot.get('tasks', []), task_ids=ids, failed_delivery=state.get('failed_delivery'),
            manual_review='pending')
        (output / 'c-reminders-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        app.quit()

    def tick():
        try:
            if state['index'] == len(steps):
                finish()
                return
            action, condition, name = steps[state['index']]
            if not state['sent']:
                action()
                state['sent'] = True
            if condition():
                checks[name] = True
                state.update(index=state['index'] + 1, sent=False, waits=0)
            else:
                state['waits'] += 1
                if state['waits'] >= 160:
                    raise TimeoutError(f'{name}; host errors={w.errors}')
        except Exception:
            finish(traceback.format_exc())

    w.show()
    QTimer.singleShot(0, w.activate_for_launch)
    timer.timeout.connect(tick)
    timer.start(50)
    app.exec()
    print(json.dumps(dict(status=report.get('status'), checks=len(checks), source=source, error=report.get('error')), ensure_ascii=False))
    return 0 if report.get('status') == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
