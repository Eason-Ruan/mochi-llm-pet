import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


def test_real_clock_profile_does_not_import_native_sensors_or_qt():
    code = '''
import sys
from datetime import datetime, timezone
from deskpet.m0.composition import build_environment
from deskpet.m0.__main__ import parser
assert parser().parse_args(["gui", "--profile", "realtime"]).profile == "realtime"
env = build_environment(profile="realtime", adapters="examples/m0/c-product.adapters.json")
assert env.clock.kind == "wall" and not hasattr(env.clock, "advance")
assert abs((env.clock.wall_now() - datetime.now(timezone.utc)).total_seconds()) < 1
assert env.observations.read().estimate.state == "unknown"
assert not any(name.startswith("PyQt6") for name in sys.modules)
assert "deskpet.m0.live_observation" not in sys.modules
'''
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_ten_second_real_reminder_snooze_and_session_pause(tmp_path):
    output = tmp_path / "realtime"
    result = subprocess.run([sys.executable, "scripts/smoke_c_realtime.py", "--offscreen",
                             "--output", str(output)], cwd=ROOT,
                            env=dict(os.environ, QT_QPA_PLATFORM="offscreen"),
                            capture_output=True, text=True, timeout=75)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((output / "realtime-report.json").read_text())
    assert report["status"] == "passed" and len(report["checks"]) == 7
    assert report["clock"] == "wall" and report["source"] == "offscreen"
    assert not report["native_gui_verified"]
    events = [json.loads(line) for line in (output / "trace.jsonl").read_text().splitlines()]
    assert all(e["clock"] == "wall" for e in events)
    assert len([e for e in events if e["kind"] == "delivery.displayed"]) == 2
    from deskpet.c_ui.review_evidence import summarize_reviews
    reviews = summarize_reviews(events)
    assert reviews["status"] == "passed" and reviews["real_user_confirmations"] == 0
