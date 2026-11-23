"""OS process exit/restart tests; these do not just validate fixture structure."""
import json
import subprocess
import sys

from deskpet.contracts import DisplayReceipt, TaskCommand
from deskpet.mocks.clock import FakeClock
from deskpet.task_service import SQLiteTaskService
from deskpet.task_service.recovery import Probe, run


def test_portable_recovery_fixture_runs_in_three_distinct_processes(tmp_path):
    report=run(tmp_path/"evidence")
    assert report["status"] == "passed"
    assert report["verification"] == "real_SQLite_process_restart"
    assert len({p["pid"] for p in report["processes"]}) == 3
    before=json.loads((tmp_path/"evidence/before-restart.json").read_text())
    after=json.loads((tmp_path/"evidence/after-restart.json").read_text())
    assert before["database_id"] == after["database_id"]
    assert len(before["rechecks"]) == 2 and not after["rechecks"]
    assert (tmp_path/"evidence/write-trace.jsonl").stat().st_size > 0
    assert (tmp_path/"evidence/resume-trace.jsonl").stat().st_size > 0


def test_abrupt_exit_after_show_before_receipt_commit_reuses_committed_intent(tmp_path):
    database=tmp_path/"crash.sqlite3"
    sent=tmp_path/"actual-show.json"
    code='''
import os,sys
from pathlib import Path
from deskpet.task_service import SQLiteTaskService
from deskpet.mocks.clock import FakeClock
from tests.m0.test_task_service import register
class Crash:
    def show(self,c):
        with open(sys.argv[2],"w",encoding="utf-8") as f:
            f.write(c.model_dump_json())
            f.flush()
            os.fsync(f.fileno())
        os._exit(72)
    def dismiss(self,t): pass
c=FakeClock()
s=SQLiteTaskService(c,Crash(),lambda *a,**k:None,path=sys.argv[1])
register(s,c,mode="hard",due=0)
s.pump()
'''
    result=subprocess.run([sys.executable,"-c",code,str(database),str(sent)],capture_output=True,text=True,timeout=20)
    assert result.returncode == 72,result.stderr
    issued=json.loads(sent.read_text())
    # os._exit bypasses finally/close and leaves a real SQLite crash boundary.
    clock=FakeClock()
    probe=Probe()
    with SQLiteTaskService(clock,probe,lambda *a,**k:None,path=database) as service:
        assert service.get("milk").displayed_at is None
        service.pump()
        assert len(probe.commands) == 1
        retried=probe.commands[0]
        assert retried.delivery_id == issued["delivery_id"]
        assert (retried.task_version,retried.occurrence) == (2,1)
        assert service.record_receipt(DisplayReceipt(
            delivery_id=retried.delivery_id,task_id="milk",task_version=2,
            rendered_at=clock.wall_now(),outcome="displayed",source="simulated")) is None
        assert service._db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with SQLiteTaskService(clock,Probe(),lambda *a,**k:None,path=database) as service:
        assert service.get("milk").displayed_at is not None


def test_cancel_commits_before_recovering_unsent_intent(tmp_path):
    database=tmp_path/"cancel.sqlite3"
    # Crash after the intent transaction, before its side effect is drained.
    code='''
import os,sys
from deskpet.task_service import SQLiteTaskService
from deskpet.mocks.clock import FakeClock
from deskpet.task_service.recovery import Probe
from tests.m0.test_task_service import register
c=FakeClock()
s=SQLiteTaskService(c,Probe(),lambda *a,**k:None,path=sys.argv[1])
register(s,c,mode="hard",due=0)
s._drain=lambda: os._exit(73)
s.pump()
'''
    result=subprocess.run([sys.executable,"-c",code,str(database)],capture_output=True,text=True,timeout=20)
    assert result.returncode == 73,result.stderr
    clock=FakeClock()
    probe=Probe()
    with SQLiteTaskService(clock,probe,lambda *a,**k:None,path=database) as service:
        result=service.apply(TaskCommand(command_id="cancel-after-crash",kind="cancel",task_id="milk",expected_version=2))
        assert result.status == "applied"
        service.pump()
        assert not probe.commands
        assert service.get("milk").status == "cancelled"
        assert service._db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


def test_abrupt_exit_during_command_transaction_rolls_back_all_rows(tmp_path):
    database=tmp_path/"uncommitted.sqlite3"
    code='''
import os,sys
from deskpet.task_service import SQLiteTaskService
from deskpet.mocks.clock import FakeClock
from deskpet.task_service.recovery import Probe
from deskpet.contracts import TaskCommand
from tests.m0.test_task_service import register
c=FakeClock()
s=SQLiteTaskService(c,Probe(),lambda *a,**k:None,path=sys.argv[1])
register(s,c,mode="hard",due=10)
save=s._save
def crash(*args):
    save(*args)
    os._exit(74)
s._save=crash
s.apply(TaskCommand(command_id="interrupted-cancel",task_id="milk",kind="cancel",expected_version=2))
'''
    result=subprocess.run([sys.executable,"-c",code,str(database)],capture_output=True,text=True,timeout=20)
    assert result.returncode == 74,result.stderr
    clock=FakeClock()
    with SQLiteTaskService(clock,Probe(),lambda *a,**k:None,path=database) as service:
        assert service.get("milk").status == "registered" and service.get("milk").version == 2
        assert service._db.execute("SELECT COUNT(*) FROM commands WHERE command_id='interrupted-cancel'").fetchone()[0] == 0
        assert service._db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
        assert service._db.execute("SELECT COUNT(*) FROM events WHERE kind='task.cancelled'").fetchone()[0] == 0
        retry=service.apply(TaskCommand(command_id="interrupted-cancel",task_id="milk",kind="cancel",expected_version=2))
        assert retry.status == "applied" and retry.actual_version == 3
