"""Desktop entry, editing focus and arrival-ordered multi-reminder verification."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import traceback
from types import SimpleNamespace


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',default='output/m0/c-p4/native')
    parser.add_argument('--offscreen',action='store_true')
    args=parser.parse_args()
    if args.offscreen: os.environ['QT_QPA_PLATFORM']='offscreen'
    from PyQt6.QtCore import QPoint, QTimer, Qt
    from PyQt6.QtGui import QGuiApplication
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication, QLineEdit
    from deskpet.c_ui.desktop import DesktopSession
    from deskpet.m0.composition import provenance
    output=Path(args.output); output.mkdir(parents=True,exist_ok=True)
    app=QApplication.instance() or QApplication([]); app.setQuitOnLastWindowClosed(False)
    options=SimpleNamespace(profile='scenario',scenario='c-experience',output=str(output),auto_smoke=False,adapters='examples/m0/c-desktop.adapters.json')
    desktop=DesktopSession(app,options); w=desktop.window; p=w.ui_surface; panel=p.reminder_panel
    source='offscreen' if QGuiApplication.platformName() in ('offscreen','minimal') else 'native'
    timer=QTimer(); checks={}; report={}; state=dict(index=0,sent=False,waits=0)
    worker=w.worker; thread=w.thread
    external=QLineEdit(); external.setWindowTitle('焦点验收输入框')
    def ready():
        return bool(w.ready_for_actions and p.snapshot and desktop.pet.isVisible() and
            (source=='offscreen' or (w.isActiveWindow() and w.windowHandle().isExposed())))
    def create(number):
        options.auto_smoke=True
        # DesktopSession copied args before construction; tag automated UI events.
        w.args.auto_smoke=True
        desktop.open('reminders')
        panel.content.setText(f'同时到期事项 {number}')
        panel.delay.setValue(10); panel.mode.setCurrentIndex(0)
        p.scroll.ensureWidgetVisible(panel.create_button)
        panel.create_button.click()
    def drafted(number):
        v=panel.selected()
        return bool(v and v['draft']['content']==f'同时到期事项 {number}' and v['status']=='draft' and not panel.pending)
    def confirm():
        p.scroll.ensureWidgetVisible(panel.buttons['confirm'])
        panel.buttons['confirm'].click()
    def registered():
        return bool(panel.selected() and panel.selected()['status']=='registered' and not panel.pending)
    def focus_draft():
        desktop.open('work')
        p.goal.setText('保持输入和光标的位置')
        p.goal.setFocus(); p.goal.setSelection(2,3)
    def focused(): return app.focusWidget() is p.goal
    def due(): w.action.emit(dict(kind='advance',seconds=10))
    def queue_ready():
        if len(p.cards)!=3 or desktop.pet.pending_count!=3: return False
        displayed=[v for v in p.snapshot['tasks'] if v['displayed_at']]
        if len(displayed)!=1: return False
        assert p.sections.currentIndex()==p.indices['work']
        assert app.focusWidget() is p.goal and p.goal.selectedText()=='输入和'
        assert p.goal.text()=='保持输入和光标的位置'
        state['keys']=list(p.cards)
        assert p.inbox.selector.currentData()==state['keys'][0]
        assert sum(c._reported for c in p.cards.values())==1
        w._grab_evidence(output/'multi-reminders-and-draft.png')
        desktop.pet.grab().save(str(output/'desktop-pet.png'))
        return True
    def choose_second(): p.inbox.selector.setCurrentIndex(1)
    def second_visible():
        return len([v for v in p.snapshot['tasks'] if v['displayed_at']])==2 and p.cards[state['keys'][1]]._reported
    def cancel_hidden():
        c=p.cards[state['keys'][2]]; state['cancel_card']=c; state['cancel_id']=c.command.task_id
        assert not c._reported
        desktop.open('reminders')
        for i in range(panel.tasks.count()):
            if panel.tasks.item(i).data(Qt.ItemDataRole.UserRole)==state['cancel_id']:
                panel.tasks.setCurrentRow(i); break
        panel.buttons['cancel'].click()
    def hidden_cancelled():
        if len(p.cards)!=2 or panel.view(state['cancel_id'])['status']!='cancelled': return False
        assert panel.view(state['cancel_id'])['displayed_at'] is None
        state['cancel_card']._action('done')
        assert not panel.pending
        assert p.inbox.selector.currentData()==state['keys'][1]
        return True
    def complete_second(): p.cards[state['keys'][1]].done.click()
    def remaining_first():
        return len(p.cards)==1 and p.inbox.selector.currentData()==state['keys'][0]
    def complete_first(): p.cards[state['keys'][0]].done.click()
    def all_processed():
        return not p.cards and not panel.pending and len([v for v in p.snapshot['tasks'] if v['status']=='completed'])==2
    def external_focus():
        external.show(); external.raise_(); external.activateWindow(); external.setFocus()
    def external_ready(): return app.focusWidget() is external and external.isActiveWindow()
    def refresh():
        state['before']=w.latest['now']; w.action.emit(dict(kind='advance',seconds=1))
    def focus_kept():
        if w.latest['now']==state['before']: return False
        assert app.focusWidget() is external and external.isActiveWindow()
        return True
    def hide_panel():
        external.close(); w.close()
    def hidden():
        assert w.worker is worker and w.thread is thread and thread.isRunning()
        return not w.isVisible() and desktop.pet.isVisible()
    def pet_click(): QTest.mouseClick(desktop.pet,Qt.MouseButton.LeftButton,pos=QPoint(70,50))
    def reopened():
        if not w.isVisible(): return False
        assert w.worker is worker and w.thread is thread and thread.isRunning()
        assert len(p.snapshot['tasks'])==3
        assert p.sections.currentIndex()==p.indices['reminders']
        assert 'deskpet.app' not in __import__('sys').modules
        assert 'deskpet.brain.worker' not in __import__('sys').modules
        w._grab_evidence(output/'reopened-workspace.png')
        return True
    steps=[(lambda:None,ready,'desktop_and_single_service_ready')]
    for i in range(1,4):
        steps.extend([(lambda n=i:create(n),lambda n=i:drafted(n),f'draft_{i}'),(confirm,registered,f'confirm_{i}')])
    steps.extend([
        (focus_draft,focused,'focus_and_draft_prepared'),(due,queue_ready,'arrival_queue_preserves_editing_focus'),
        (choose_second,second_visible,'only_selected_reminder_is_displayed'),
        (cancel_hidden,hidden_cancelled,'unseen_cancel_keeps_other_cards'),
        (complete_second,remaining_first,'complete_only_selected_task'),(complete_first,all_processed,'queue_drained'),
        (external_focus,external_ready,'external_input_active'),(refresh,focus_kept,'background_refresh_does_not_activate'),
        (hide_panel,hidden,'close_hides_without_stopping_service'),(pet_click,reopened,'pet_opens_existing_workspace'),
    ])
    def finish(error=None):
        timer.stop(); snapshot=p.snapshot
        desktop.quit()
        events=[json.loads(s) for s in (output/'trace.jsonl').read_text().splitlines()]
        if not error:
            try:
                assert not thread.isRunning() and not desktop.pet.timer.isActive()
                shown=[e for e in events if e['kind']=='delivery.displayed']
                assert len(shown)==2 and all(e['source']==source for e in shown)
                assert not w.errors
            except Exception: error=traceback.format_exc()
        report.update(provenance(),status='failed' if error else 'passed',error=error,checks=checks,source=source,
            native_gui_verified=source=='native' and not error,tasks=snapshot.get('tasks'),host_errors=w.errors,
            sprite_animations=len(desktop.pet.player.clips),manual_review='pending')
        (output/'c-experience-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        app.quit()
    def tick():
        try:
            if state['index']==len(steps): finish(); return
            action,condition,name=steps[state['index']]
            if not state['sent']: action(); state['sent']=True
            if condition():
                checks[name]=True; state.update(index=state['index']+1,sent=False,waits=0)
            else:
                state['waits']+=1
                if state['waits']>=160: raise TimeoutError(f'{name}; host errors={w.errors}')
        except Exception: finish(traceback.format_exc())
    timer.timeout.connect(tick); timer.start(50); app.exec()
    print(json.dumps(dict(status=report.get('status'),checks=len(checks),source=source,error=report.get('error')),ensure_ascii=False))
    return 0 if report.get('status')=='passed' else 1


if __name__=='__main__': raise SystemExit(main())
