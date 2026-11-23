"""Durability/fault tests in addition to the unchanged shared behavior suite."""
from datetime import timedelta
import json
import sqlite3
import subprocess
import sys

import pytest

from deskpet.contracts import DisplayReceipt, ErrorCode, ProposedPlan, TaskCommand
from deskpet.m0.trace import TraceRecorder
from deskpet.mocks.clock import FakeClock
from deskpet.task_service import SQLiteTaskService, factory
from tests.m0.test_task_service import DisplayProbe, command, envelope, observe, register


@pytest.fixture
def system(tmp_path):
    clock = FakeClock()
    display = DisplayProbe(clock)
    trace = TraceRecorder(clock)
    service = SQLiteTaskService(clock, display, trace.emit, path=tmp_path/"tasks.sqlite3")
    yield service, clock, display, trace
    service.close()


def reopen(service, clock, display, trace):
    path = service.path
    service.close()
    return SQLiteTaskService(clock, display, trace.emit, path=path)


def test_state_commands_and_original_results_survive_close(system):
    service, clock, display, trace = system
    register(service, clock)
    cancel = TaskCommand(command_id="cancel", task_id="milk", expected_version=2, kind="cancel")
    first = service.apply(cancel)
    with reopen(service, clock, display, trace) as restored:
        assert restored.get("milk") == first.view
        result = restored.apply(cancel)
        assert result.status == "duplicate" and result.view == first.view
        assert restored.apply(cancel.model_copy(update={"kind": "complete"})).error_code == ErrorCode.INVALID_ARGUMENT
        assert len(restored.list_tasks()) == 1
        assert restored._db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_rejected_command_is_durable_even_after_task_changes(system):
    service, clock, display, trace = system
    register(service, clock)
    rejected = TaskCommand(command_id="stale", task_id="milk", expected_version=1, kind="cancel")
    assert service.apply(rejected).error_code == ErrorCode.STALE_TASK
    command(service, "snooze", seconds=30)
    with reopen(service, clock, display, trace) as restored:
        result = restored.apply(rejected)
        assert result.status == "duplicate" and result.actual_version == 2
        assert result.view.version == 2 and restored.get("milk").version == 3


def test_observation_decision_and_recheck_are_durable(system):
    service, clock, display, trace = system
    register(service, clock)
    observe(service, clock, "working", revision=2, valid_until=clock.wall_now()+timedelta(seconds=5))
    request = envelope(service, clock, context_revision=2)
    plan = ProposedPlan(envelope=request)
    assert service.accept_decision(plan) is None
    assert service.schedule_recheck(request, clock.wall_now()+timedelta(seconds=15)) is None
    with reopen(service, clock, display, trace) as restored:
        assert restored.validate(request) == ErrorCode.STALE_CONTEXT
        observe(restored, clock, "working", revision=2, valid_until=clock.wall_now()+timedelta(seconds=5))
        assert restored.accept_decision(plan) == ErrorCode.DUPLICATE
        clock.advance(15)
        assert restored.validate(request) == ErrorCode.STALE_CONTEXT
        restored.pump()
        assert any(e.kind == "recheck.fired" for e in trace.events)
        assert not display.commands


def test_restart_accepts_fresh_pipeline_revision_and_rejects_old_boot_decision(system):
    service, clock, display, trace = system
    register(service,clock,due=0,width=100)
    observe(service,clock,"available",revision=10)
    old=envelope(service,clock,context_revision=10)
    clock.advance(1)
    with reopen(service,clock,display,trace) as restored:
        restored.pump()
        assert not display.commands  # Prior-boot available cannot cause early delivery.
        observe(restored,clock,"available",revision=1)
        fresh=envelope(restored,clock,context_revision=1,request_id="new-boot")
        assert restored.validate(fresh) is None
        assert restored.validate(old) == ErrorCode.STALE_CONTEXT
        forged_old=old.model_copy(update={"context_revision":1})
        assert restored.validate(forged_old) == ErrorCode.STALE_CONTEXT
        restored.pump()
        assert len(display.commands) == 1


def test_restarted_service_requires_an_observation_for_new_revision_one_requests(system):
    service, clock, display, trace = system
    register(service, clock, due=0, width=10)
    clock.advance(1)
    with reopen(service, clock, display, trace) as restored:
        request = envelope(restored, clock, context_revision=1, request_id="new-before-observe")
        assert restored.request_delivery(request).error_code == ErrorCode.STALE_CONTEXT
        assert not display.commands

        clock.advance(9)
        restored.pump()
        assert [command.reason for command in display.commands] == ["deadline_fallback"]


def test_write_failure_rolls_back_state_dedup_events_and_effects(system, monkeypatch):
    service, clock, display, trace = system
    register(service, clock)
    before_events = len(trace.events)
    before = service.get("milk")
    cancel = TaskCommand(command_id="cancel", task_id="milk", expected_version=2, kind="cancel")
    original = service._save
    def fail_after_sql(*args):
        original(*args)
        raise sqlite3.OperationalError("injected disk write failure")
    monkeypatch.setattr(service, "_save", fail_after_sql)
    with pytest.raises(sqlite3.OperationalError):
        service.apply(cancel)
    assert service.get("milk") == before
    assert len(trace.events) == before_events
    assert not display.dismissed
    assert service._db.execute("SELECT COUNT(*) FROM commands WHERE command_id='cancel'").fetchone()[0] == 0
    monkeypatch.setattr(service, "_save", original)
    assert service.apply(cancel).status == "applied"


def test_two_connections_read_current_state_and_cas(system):
    service, clock, display, trace = system
    register(service, clock)
    with SQLiteTaskService(clock, display, trace.emit, path=service.path) as other:
        assert command(other, "snooze", seconds=10).actual_version == 3
        assert service.get("milk").version == 3
        result = service.apply(TaskCommand(command_id="stale", kind="cancel", task_id="milk", expected_version=2))
        assert result.error_code == ErrorCode.STALE_TASK


@pytest.mark.parametrize("same_id", [False, True])
def test_real_processes_serialize_cas_and_idempotency(system, same_id):
    service, clock, display, trace = system
    register(service, clock)
    worker = '''
import json,sys
from deskpet.task_service import SQLiteTaskService
from deskpet.mocks.clock import FakeClock
from deskpet.mocks.delivery import FakeDeliveryPort
from deskpet.contracts import TaskCommand
c=FakeClock()
s=SQLiteTaskService(c,FakeDeliveryPort(c),lambda *a,**k:None,path=sys.argv[1])
print("ready",flush=True)
sys.stdin.readline()
r=s.apply(TaskCommand(command_id=sys.argv[2],task_id="milk",kind="cancel",expected_version=2))
print(r.model_dump_json(),flush=True)
s.close()
'''
    processes = [subprocess.Popen([sys.executable,"-c",worker,str(service.path),"same" if same_id else f"worker-{i}"],
                                  stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                 for i in range(2)]
    try:
        for p in processes:
            assert p.stdout.readline().strip() == "ready"
        for p in processes:
            p.stdin.write("go\n")
            p.stdin.flush()
        results=[]
        for p in processes:
            stdout,stderr=p.communicate(timeout=15)
            assert p.returncode == 0,stderr
            results.append(json.loads(stdout))
        assert sorted(r["status"] for r in results) == (["applied","duplicate"] if same_id else ["applied","rejected"])
        assert service.get("milk").version == 3
        if not same_id:
            assert next(r for r in results if r["status"]=="rejected")["error_code"] == "STALE_TASK"
    finally:
        for p in processes:
            if p.poll() is None:
                p.kill()
                p.communicate()


def test_committed_intent_is_visible_from_another_connection_before_show(system):
    service, clock, display, trace = system
    register(service, clock, mode="hard", due=0)
    original=display.show
    def verify_before_show(issued):
        with sqlite3.connect(service.path) as db:
            row=db.execute("SELECT command,status FROM deliveries WHERE delivery_id=?",(issued.delivery_id,)).fetchone()
            assert json.loads(row[0])["delivery_id"] == issued.delivery_id and row[1] == "pending"
            assert db.execute("SELECT COUNT(*) FROM outbox WHERE delivery_id=?",(issued.delivery_id,)).fetchone()[0] == 1
            assert json.loads(db.execute("SELECT view FROM tasks WHERE task_id='milk'").fetchone()[0])["displayed_at"] is None
        return original(issued)
    display.show=verify_before_show
    service.pump()
    assert service.get("milk").displayed_at is not None


def test_receipt_and_first_display_fact_remain_idempotent_after_restart(system):
    service, clock, display, trace = system
    register(service, clock, mode="hard", due=0)
    service.pump()
    receipt=next(iter(display.receipts.values()))
    first=service.get("milk").displayed_at
    with reopen(service,clock,display,trace) as restored:
        assert restored.record_receipt(receipt) == ErrorCode.DUPLICATE
        clock.advance(100)
        restored.pump()
        assert restored.get("milk").displayed_at == first
        assert len(display.commands) == 1


def test_retries_use_wall_deadline_after_new_monotonic_origin(system):
    service, clock, display, trace = system
    display.mode="missing"
    clock.advance(10000)
    register(service,clock,mode="hard",due=0)
    service.pump()
    restarted=FakeClock(clock.wall_now()+timedelta(seconds=4))
    probe=DisplayProbe(restarted)
    with reopen(service,restarted,probe,trace) as restored:
        restored.pump()
        assert probe.commands == []
        restarted.advance(1)
        restored.pump()
        assert probe.commands[0].delivery_id == display.commands[0].delivery_id
        assert restored.get("milk").displayed_at is not None


def test_sink_failure_does_not_lose_events_or_rollback_commands(system):
    service, clock, display, trace = system
    def broken(*args,**kwargs):
        raise OSError("sink unavailable")
    service.emit=broken
    register(service,clock)
    assert service.get("milk").version == 2
    assert service._db.execute("SELECT COUNT(*) FROM events WHERE emitted=0").fetchone()[0] == 2
    service.emit=trace.emit
    service.pump()
    assert [e.kind for e in trace.events] == ["task.created","task.confirmed"]
    assert all(e.payload["b_event_id"].startswith(service.database_id+":") for e in trace.events)


def test_factory_requires_explicit_durable_path(monkeypatch):
    monkeypatch.delenv("MOCHI_TASK_DB",raising=False)
    clock=FakeClock()
    with pytest.raises(ValueError,match="MOCHI_TASK_DB"):
        factory(clock=clock,delivery=DisplayProbe(clock),emit=lambda *a,**k:None)


def test_expired_unsent_lease_is_committed_before_show_without_changing_id(system,monkeypatch):
    service,clock,display,trace=system
    register(service,clock,mode="hard",due=0)
    drain=service._drain
    monkeypatch.setattr(service,"_drain",lambda:None)
    service.pump()
    stored=json.loads(service._db.execute("SELECT command FROM deliveries").fetchone()[0])
    assert not display.commands
    clock.advance(40)
    original=display.show
    def inspect(issued):
        with sqlite3.connect(service.path) as db:
            committed=json.loads(db.execute("SELECT command FROM deliveries").fetchone()[0])
        assert committed["expires_at"] == issued.model_dump(mode="json")["expires_at"]
        assert issued.expires_at > clock.wall_now()
        assert issued.delivery_id == stored["delivery_id"]
        return original(issued)
    display.show=inspect
    monkeypatch.setattr(service,"_drain",drain)
    observe(service,clock,"unknown")  # Drain recovery intent, without another pump creating a lease.
    assert len(display.commands) == 1 and service.get("milk").displayed_at is not None


def test_unknown_schema_fails_without_rewriting_database(tmp_path):
    path=tmp_path/"future.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=99")
    clock=FakeClock()
    with pytest.raises(ValueError,match="schema 99"):
        SQLiteTaskService(clock,DisplayProbe(clock),lambda *a,**k:None,path=path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 99
