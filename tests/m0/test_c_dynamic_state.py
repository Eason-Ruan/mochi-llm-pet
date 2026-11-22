from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[2]


def test_dynamic_state_workflow(tmp_path):
    result=subprocess.run([sys.executable,'scripts/smoke_c_state.py','--offscreen','--output',str(tmp_path)],
        cwd=ROOT,capture_output=True,text=True,timeout=25)
    assert result.returncode==0,result.stdout+result.stderr
    report=json.loads((tmp_path/'c-state-report.json').read_text())
    assert report['status']=='passed' and len(report['checks'])==13
    assert not report['native_gui_verified']
    assert report['frame']['observation']['estimate']['state']=='available'
    assert report['frame']['correction_status']=='cleared'


def test_renderer_revisions_drafts_and_whitelist():
    code=r'''
import json
from pathlib import Path
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication
from deskpet.c_ui.renderer import UiSpecRenderer
app=QApplication([])
calls=[]
r=UiSpecRenderer(submit=lambda action:calls.append(action) or action.event_id)
spec=json.loads(Path('examples/m0/c-state-ui.json').read_text())
assert r.render_spec(spec)
assert len(r.widgets)==6
assert r.widgets['title'].textFormat()==Qt.TextFormat.PlainText
r.widgets['reason'].setText('保留输入')
input_widget=r.widgets['reason']
old_callback=r._slots['apply']
spec['surface_revision']=2
spec['data']['summary']='最新估计'
assert r.render_spec(spec)
assert r.widgets['reason'] is input_widget and input_widget.text()=='保留输入'
old_callback()
assert not calls, 'old surface callback submitted a new action'
r.widgets['apply'].click()
assert len(calls)==1 and calls[0].surface_revision==2
assert calls[0].payload=={'ttl_s':30,'state':'working','reason':'保留输入'}
assert r.pending and not r.widgets['apply'].isEnabled()
r.receive_result({'event_id':'unrelated','status':'correction_applied'})
assert r.pending
r.receive_result({'event_id':calls[0].event_id,'status':'rejected'})
assert not r.pending and input_widget.text()=='保留输入'
bad=json.loads(json.dumps(spec)); bad['surface_revision']=1
assert not r.render_spec(bad) and not r.widgets['apply'].isEnabled()
assert r.render_spec(spec)
for patch in ('unknown_type','unknown_action','duplicate_id','unbound_input'):
    bad=json.loads(json.dumps(spec)); bad['surface_revision']=3
    if patch=='unknown_type': bad['components'][0]['type']='python'
    if patch=='unknown_action': bad['components'][-1]['action_id']='run_shell'
    if patch=='duplicate_id': bad['components'][1]['component_id']='title'
    if patch=='unbound_input': bad['components'][4].pop('data_key')
    assert not r.render_spec(bad),patch
    assert not r.widgets['apply'].isEnabled()
    assert len(calls)==1
    assert r.render_spec(spec)
# Changing layout requires a new revision; compatible inputs survive a rebuild.
spec['surface_revision']=3
spec['components'].insert(1,{'component_id':'extra','type':'text','label':'新增说明'})
assert r.render_spec(spec) and r.widgets['reason'].text()=='保留输入'
restricted=UiSpecRenderer(submit=lambda a:None,allowed_actions={'state.clear_override'})
assert not restricted.render_spec(spec)
def closed(action): raise RuntimeError('界面已关闭')
r.submit=closed
r.widgets['apply'].click()
assert not r.pending and '已关闭' in r.status.text()
r.close(); restricted.close()
'''
    result=subprocess.run([sys.executable,'-c',code],cwd=ROOT,
        env=dict(os.environ,QT_QPA_PLATFORM='offscreen'),capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr
