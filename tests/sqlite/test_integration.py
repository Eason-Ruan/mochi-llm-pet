"""B joins the existing A runtime, C queued UI and D trace/report machinery."""
import json

import pytest

from deskpet.m0.replay import fixture_paths, load_scenario, run_scenario
from tests.m0.test_gui import _gui


def test_sqlite_and_d_metrics_preserve_delivery_evidence(tmp_path):
    from scripts.d_count_deliveries import count_deliveries

    for name in ("display-failure-recovery", "cancel-late", "soft-failure-retry"):
        output = tmp_path / name
        report = run_scenario(load_scenario(fixture_paths(name)[0]), output=output,
                              adapters={"task_service": "deskpet.task_service:isolated_factory",
                                        "metrics": "d_metrics:factory"})
        assert report["status"] == "passed", report["failures"]
        counts = count_deliveries(output / "trace.jsonl")
        assert counts["evidence_errors"] == []
        assert counts["unique_deliveries"] == report["outcomes"]["delivery_requests"]
        assert counts["displayed_deliveries"] == report["outcomes"]["displays"]
        assert json.loads((output / "d-sink-info.json").read_text())["implementation"] == "d_metrics.DMetricsSink"


@pytest.mark.parametrize("path",fixture_paths(),ids=lambda p:p.stem)
def test_existing_replay_with_sqlite(path,tmp_path):
    report=run_scenario(load_scenario(path),output=tmp_path,
                        adapters={"task_service":"deskpet.task_service:isolated_factory"})
    assert report["status"] == "passed",report["failures"]
    assert report["native_gui"] == "not_run"


def test_queued_qt_ui_and_metrics_use_sqlite_on_service_thread(tmp_path,monkeypatch):
    monkeypatch.setenv("MOCHI_TASK_DB",str(tmp_path/"gui.sqlite3"))
    process=_gui(tmp_path,adapters={"task_service":"deskpet.task_service:factory"})
    assert process.returncode == 0,process.stdout+process.stderr
    report=json.loads((tmp_path/"native-gui-report.json").read_text())
    assert report["status"] == "passed" and all(report["checks"].values())
    assert report["receipt_source"] == "offscreen" and report["native_gui_verified"] is False
    events=[json.loads(line) for line in (tmp_path/"trace.jsonl").read_text().splitlines()]
    shown=[e for e in events if e["kind"]=="delivery.displayed"]
    assert len(shown)==2 and all(e["source"]=="offscreen" for e in shown)
    assert all("b_event_id" in e["payload"] for e in shown)
