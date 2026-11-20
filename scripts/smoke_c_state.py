"""Verify C's dynamic state UI using real widgets, observation frames and TTLs."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import traceback
from types import SimpleNamespace


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',default='output/m0/c-p3/native')
    parser.add_argument('--offscreen',action='store_true')
    args=parser.parse_args()
    if args.offscreen:
        os.environ['QT_QPA_PLATFORM']='offscreen'
    from PyQt6.QtCore import QTimer
    from PyQt6.QtGui import QGuiApplication
    from PyQt6.QtWidgets import QApplication
    from deskpet.contracts import SessionResult, UiAction
    from deskpet.m0.composition import provenance
    from deskpet.m0.gui import Workbench
    output=Path(args.output); output.mkdir(parents=True,exist_ok=True)
    app=QApplication.instance() or QApplication([])
    options=SimpleNamespace(profile='scenario',scenario='c-state',output=str(output),auto_smoke=False,adapters='examples/m0/c-product.adapters.json')
    w=Workbench(options); p=w.ui_surface; panel=p.state_panel; renderer=panel.renderer
    source='offscreen' if QGuiApplication.platformName() in ('offscreen','minimal') else 'native'
    checks={}; report={}; state=dict(index=0,sent=False,waits=0)
    timer=QTimer()
    def frame(): return w.latest.get('observation_frame',{})
    def effective(): return frame().get('observation',{}).get('estimate',{}).get('state')
    def ready():
        return bool(w.ready_for_actions and p.snapshot and (source=='offscreen' or
            (w.isActiveWindow() and w.windowHandle() and w.windowHandle().isExposed())))
    def sample(idle):
        options.auto_smoke=True
        w.raw_application.setText('Editor'); w.raw_idle.setValue(idle); w.raw_ttl.setValue(30)
        w.raw_submit.click()
    def expand():
        w.tabs.setCurrentWidget(p)
        p.open_section('state')
        panel.toggle.setChecked(True)
        assert {c.type for c in renderer.spec.components}=={'text','card','list','select','text_input','button'}
        assert not renderer.widgets['clear'].isEnabled()
        renderer.widgets['reason'].setText('   ')
        renderer.widgets['apply'].click()
        assert renderer.pending is None and '请填写' in renderer.status.text()
    def apply(title,ttl):
        renderer.widgets['state'].setCurrentText(title)
        renderer.widgets['reason'].setText('我正在阅读材料')
        renderer.widgets['ttl'].setCurrentText(ttl)
        p.scroll.ensureWidgetVisible(renderer.widgets['apply'])
        renderer.widgets['apply'].click()
        assert renderer.pending and not renderer.widgets['apply'].isEnabled()
    def applied(value):
        return effective()==value and frame().get('correction_status')=='active' and renderer.pending is None
    def raw_changed():
        if not (applied('working') and frame()['estimated']['state']=='away'):
            return False
        assert frame()['correction']['origin']=='automation'
        assert renderer.widgets['reason'].text()=='我正在阅读材料'
        assert '基础估计：离开' in renderer.widgets['effective'].content_label.text()
        assert '自动测试输入' in renderer.widgets['correction'].text()
        w._grab_evidence(output/'active-correction.png')
        return True
    def advance(seconds): w.action.emit({'kind':'advance','seconds':seconds})
    def clear(): renderer.widgets['clear'].click()
    def invalid():
        p.context.submit_action(UiAction(event_id='c-state-invalid',action_id='state.override',surface_revision=1,
            payload=dict(state='working',reason='非法有效期',ttl_s=0)))
    def rejected():
        return any(r.get('event_id')=='c-state-invalid' and r.get('error_code')=='INVALID_ARGUMENT' for r in w.results)
    def start_session():
        p.goal.setText('验证工作段不产生用户标签'); p.work_panel.duration.setValue(60)
        p.start_button.click()
    def session_ready():
        if not (p.snapshot['sessions'] and not p.work_panel.pending): return False
        assert frame()['correction_status']=='cleared'
        assert effective()=='available' and frame()['observation']['estimate']['source']!='user'
        return True
    steps=[
        (lambda:None,ready,'ready'),
        (lambda:sample(15),lambda:frame().get('estimated',{}).get('state')=='available','raw_to_estimate'),
        (expand,lambda:renderer.isVisible(),'six_components_and_required_reason'),
        (lambda:apply('正在专注','5 秒'),lambda:applied('working'),'c_override_with_receipt_id'),
        (lambda:sample(130),raw_changed,'raw_updates_without_overwriting_correction'),
        (lambda:advance(4),lambda:frame().get('raw',{}).get('idle_s')==130 and effective()=='working' and w.latest['now'].endswith('04+00:00'),'correction_before_expiry'),
        (lambda:advance(1),lambda:effective()=='away' and frame()['correction_status']=='expired','correction_expiry_restores_base'),
        (lambda:apply('可以打扰','30 秒'),lambda:applied('available'),'second_correction'),
        (clear,lambda:effective()=='away' and frame()['correction_status']=='cleared' and not renderer.pending,'c_clear_and_sync'),
        (lambda:advance(30),lambda:frame().get('raw_status')=='expired' and effective()=='unknown','expired_evidence_becomes_unknown'),
        (lambda:sample(15),lambda:effective()=='available' and frame()['raw_status']=='fresh','fresh_sample_recovers'),
        (invalid,rejected,'invalid_action_returns_correlated_rejection'),
        (start_session,session_ready,'work_session_does_not_label_user'),
    ]
    def finish(error=None):
        timer.stop()
        p.scroll.ensureWidgetVisible(panel.toggle)
        w._grab_evidence(output/'state-panel.png')
        f=frame(); w.close()
        events=[json.loads(s) for s in (output/'trace.jsonl').read_text().splitlines()]
        if not error:
            try:
                for status in ('correction_applied','correction_cleared'):
                    results=[r for r in w.results if r.get('status')==status]
                    assert results and all(r.get('event_id') for r in results)
                assert len([e for e in events if e['kind']=='observation.correction.applied'])==2
                assert all(e['payload']['origin']=='automation' for e in events if e['kind']=='observation.correction.applied')
                for result in w.results:
                    if result.get('command_id'):
                        SessionResult.model_validate(result)
                assert not w.errors
            except Exception: error=traceback.format_exc()
        report.update(provenance(),status='failed' if error else 'passed',checks=checks,error=error,
            host_errors=w.errors,source=source,native_gui_verified=source=='native' and not error,
            frame=f,results=w.results,manual_review='pending')
        (output/'c-state-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
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
                if state['waits']>=160: raise TimeoutError(f'{name}: {w.errors}')
        except Exception: finish(traceback.format_exc())
    w.show(); QTimer.singleShot(0,w.activate_for_launch)
    timer.timeout.connect(tick); timer.start(50); app.exec()
    print(json.dumps(dict(status=report.get('status'),checks=len(checks),source=source,error=report.get('error')),ensure_ascii=False))
    return 0 if report.get('status')=='passed' else 1


if __name__=='__main__': raise SystemExit(main())
