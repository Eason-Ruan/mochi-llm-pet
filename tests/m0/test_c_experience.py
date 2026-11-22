from __future__ import annotations
import json
from pathlib import Path
import subprocess
import sys
import pytest
from deskpet.c_ui.time_format import format_time

ROOT=Path(__file__).resolve().parents[2]


def test_chinese_times_preserve_timezone_and_day_boundaries():
    reference='2026-11-19T15:00:00+00:00'
    assert format_time('2026-11-19T15:30:00Z',reference)=='今天 23:30（北京时间 UTC+08:00）'
    assert format_time('2026-11-19T16:00:05Z',reference)=='明天 00:00:05（北京时间 UTC+08:00）'
    assert format_time('2026-11-18T15:00:00Z',reference).startswith('昨天')
    assert format_time('2027-01-01T00:00:00Z',reference).startswith('2027年1月1日')
    with pytest.raises(ValueError): format_time('2026-11-19T15:30:00')


def test_desktop_entry_focus_and_reminder_queue(tmp_path):
    result=subprocess.run([sys.executable,'scripts/smoke_c_experience.py','--offscreen','--output',str(tmp_path)],
        cwd=ROOT,capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
    report=json.loads((tmp_path/'c-experience-report.json').read_text())
    assert report['status']=='passed' and len(report['checks'])==17
    assert not report['native_gui_verified']
    assert report['sprite_animations']>0
    assert len([v for v in report['tasks'] if v['status']=='completed'])==2
    assert len([v for v in report['tasks'] if v['status']=='cancelled' and v['displayed_at'] is None])==1


def test_desktop_config_replays_without_importing_gui():
    result=subprocess.run([sys.executable,'-c','''
import sys
from deskpet.m0.replay import run_suite
assert run_suite(adapters="examples/m0/c-desktop.adapters.json")["status"]=="passed"
assert "deskpet.c_ui.desktop" not in sys.modules
assert not any(k.startswith("PyQt6") for k in sys.modules)
'''],cwd=ROOT,capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
