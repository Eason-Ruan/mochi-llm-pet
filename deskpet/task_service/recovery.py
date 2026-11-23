"""Reproducible B recovery evidence, using separate OS processes and real SQLite.

Run: python -m deskpet.task_service.recovery --output output/m0/b-recovery
Fixture import here is a verification tool, never a production restore endpoint.
"""
from __future__ import annotations

import argparse
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys

from deskpet.contracts import DisplayReceipt, RecoveryFixture
from deskpet.m0.composition import ROOT, provenance
from deskpet.m0.trace import JsonlMetricsSink, TraceRecorder
from deskpet.mocks.clock import FakeClock
from .sqlite import SQLiteTaskService


class Probe:
    def __init__(self):
        self.commands = []
        self.dismissed = []

    def show(self, command):
        self.commands.append(command)
        return None

    def dismiss(self, task_id):
        self.dismissed.append(task_id)


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")


def _snapshot(service):
    return {
        "pid": os.getpid(), "database_id": service.database_id,
        "tasks": [v.model_dump(mode="json") for v in service.list_tasks()],
        "deliveries": [dict(r) for r in service._db.execute("SELECT * FROM deliveries ORDER BY delivery_id")],
        "rechecks": [dict(r) for r in service._db.execute("SELECT * FROM rechecks ORDER BY task_id")],
        "outbox": [dict(r) for r in service._db.execute("SELECT * FROM outbox ORDER BY id")],
    }


def _obsolete_jobs(service, fixture):
    # First-delivery jobs are encoded by the TaskView's confirmed window.
    # Obsolete external jobs map to our versioned recheck rows. Both ultimately
    # revalidate against tasks before any effect, instead of trusting the job.
    for job in fixture.job_examples:
        if job["kind"] == "obsolete_job":
            service._db.execute("INSERT OR REPLACE INTO rechecks VALUES (?,?,?,?)", (
                job["task_id"],job["task_version"],job["run_at"],"fixture-obsolete-job"))


def _phase(phase, output, database):
    fixture=RecoveryFixture.model_validate_json((ROOT/"docs/contracts/recovery-fixture.json").read_text(encoding="utf-8"))
    offset={"write":0,"resume":5,"verify":120}[phase]
    clock=FakeClock(fixture.captured_at+timedelta(seconds=offset))
    probe=Probe()
    trace=TraceRecorder(clock,run_id=f"b-recovery-{phase}",scenario_id="sqlite-process-restart",
                        sink=JsonlMetricsSink(output/f"{phase}-trace.jsonl"))
    with SQLiteTaskService(clock,probe,trace.emit,path=database) as service:
        if phase == "write":
            assert service.list_tasks() == [], "verification must use a fresh DB"
            with service._transaction():
                for view in fixture.tasks:
                    service._db.execute("INSERT INTO tasks VALUES (?,?)",(view.task_id,view.model_dump_json()))
                for issued in fixture.pending_deliveries:
                    service._db.execute("INSERT INTO deliveries VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                        issued.delivery_id,issued.task_id,issued.task_version,issued.occurrence,
                        issued.model_dump_json(),"pending",0,
                        (fixture.captured_at-timedelta(seconds=5)).isoformat(),"fixture-writer",0,None,1))
            # Actually call DeliveryPort in the writer process; no receipt arrives.
            service.pump()
            assert [c.delivery_id for c in probe.commands] == ["persisted-delivery-1"]
            assert service.get("recover-pending").displayed_at is None
            # Leave the obsolete job residue on disk for the NEW process to reject.
            with service._transaction():
                _obsolete_jobs(service,fixture)
            _write(output/"before-restart.json",_snapshot(service))
        elif phase == "resume":
            _write(output/"opened-in-new-process.json",_snapshot(service))
            service.pump()
            assert [c.delivery_id for c in probe.commands] == ["persisted-delivery-1"]
            assert service.get("recover-pending").displayed_at is None
            assert not list(service._db.execute("SELECT * FROM rechecks"))
            _write(output/"before-receipt.json",_snapshot(service))
            issued=probe.commands[0]
            assert service.record_receipt(DisplayReceipt(
                delivery_id=issued.delivery_id,task_id=issued.task_id,task_version=issued.task_version,
                rendered_at=clock.wall_now(),outcome="displayed",source="simulated")) is None
            clock.advance(54)
            service.pump()
            assert len(probe.commands) == 1  # 17:00:59: snoozed task still not due.
            clock.advance(1)
            service.pump()
            assert len(probe.commands) == 2
            snoozed=probe.commands[1]
            assert (snoozed.task_id,snoozed.task_version,snoozed.occurrence) == ("recover-snoozed",3,2)
            service.record_receipt(DisplayReceipt(
                delivery_id=snoozed.delivery_id,task_id=snoozed.task_id,task_version=snoozed.task_version,
                rendered_at=clock.wall_now(),outcome="displayed",source="simulated"))
            _write(output/"after-restart.json",_snapshot(service))
        else:
            service.pump()
            assert probe.commands == []  # Persisted successful receipts stop retries.
            assert service.get("recover-pending").displayed_at is not None
            assert service.get("recover-snoozed").displayed_at is not None
            assert service.get("recover-cancelled").displayed_at is None
            assert service._db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            _write(output/"verified-in-third-process.json",_snapshot(service))
        _write(output/f"{phase}-requests.json",[v.model_dump(mode="json") for v in probe.commands])
        _write(output/f"{phase}-process.json",{"pid":os.getpid(),"database_id":service.database_id,"phase":phase})


def run(output):
    output=Path(output).resolve()
    output.mkdir(parents=True,exist_ok=True)
    database=output/"recovery.sqlite3"
    if database.exists():
        raise ValueError("use a new output directory; existing verification DB will not be overwritten")
    report={"status":"running","verification":"real_SQLite_process_restart",
            "native_gui":"not_run","receipt_source":"simulated","processes":[],"provenance":provenance()}
    for phase in ("write","resume","verify"):
        result=subprocess.run([sys.executable,"-m","deskpet.task_service.recovery","--phase",phase,
                               "--output",str(output),"--database",str(database)],
                              capture_output=True,text=True,timeout=30)
        (output/f"{phase}-stdout.txt").write_text(result.stdout,encoding="utf-8")
        (output/f"{phase}-stderr.txt").write_text(result.stderr,encoding="utf-8")
        if result.returncode:
            report.update(status="failed",failed_phase=phase,returncode=result.returncode)
            _write(output/"report.json",report)
            raise RuntimeError(f"recovery {phase} failed: {result.stderr}")
        report["processes"].append(json.loads((output/f"{phase}-process.json").read_text(encoding="utf-8")))
    assert len({p["pid"] for p in report["processes"]}) == 3
    assert len({p["database_id"] for p in report["processes"]}) == 1
    report.update(status="passed",checks={
        "separate_processes_same_database":True,"pending_reuses_persisted_delivery_id":True,
        "displayed_requires_receipt":True,"cancelled_obsolete_job_suppressed":True,
        "snoozed_no_early_delivery":True,"snoozed_version_3_occurrence_2":True,
        "successful_receipts_survive_another_restart":True,"sqlite_integrity":True})
    _write(output/"report.json",report)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",default="output/m0/b-recovery")
    parser.add_argument("--phase",choices=("write","resume","verify"))
    parser.add_argument("--database")
    args=parser.parse_args()
    if args.phase:
        if not args.database:
            parser.error("--phase requires --database")
        _phase(args.phase,Path(args.output),Path(args.database))
    else:
        report=run(args.output)
        print(json.dumps({"status":report["status"],"verification":report["verification"],
                          "processes":report["processes"],"report":str(Path(args.output)/"report.json")},indent=2))


if __name__ == "__main__":
    main()
