"""Shared timer, clock boundaries, and cross-process checkpoint regressions."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import sqlite3
import subprocess
import sys
from threading import Barrier

import pytest

from deskpet.contracts import ErrorCode, SessionCommand
from deskpet.mocks.clock import FakeClock
from deskpet.settings import HabitBehavior, SettingsStore
from deskpet.work_sessions import PhaseCommand, PersistentWorkSessionService


def write(service, verb, phase_id="phase", *, version=0, origin="manual", key=None, **payload):
    return service.apply_phase(PhaseCommand(
        command_id=key or f"{verb}-{phase_id}-{version}", phase_id=phase_id,
        kind=verb, expected_version=version, source=origin, payload=payload,
    ))


def test_concurrent_legacy_and_modern_starts_use_one_timer(tmp_path):
    path = tmp_path / "business.sqlite3"
    clock = FakeClock()
    a = PersistentWorkSessionService(path=path, clock=clock, default_focus_s=900)
    b = PersistentWorkSessionService(path=path, clock=clock)
    barrier = Barrier(2)

    def legacy_start():
        barrier.wait()
        return a.apply(SessionCommand(command_id="old", kind="start", session_id="old", payload={"goal": " "}))

    def new_start():
        barrier.wait()
        return write(b, "start", "new", origin="proposal")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(legacy_start), executor.submit(new_start)]
        results = [future.result() for future in futures]
    assert sorted(result.status for result in results) == ["applied", "rejected"]
    assert a.current_phase() == b.current_phase()
    assert a.current_phase().title == "专注"
    assert a.current_phase().remaining_s == 900
    assert len(b.list_phases()) == 1
    assert b.command_result("old").status == results[0].status
    assert b.command_result("start-new-0").status == results[1].status

    a.close()
    b.close()
    recovered = PersistentWorkSessionService(path=path, clock=clock)
    assert recovered.current_phase().state == "paused"
    assert recovered.command_result("start-new-0").status == results[1].status
    recovered.close()


def test_adjust_pause_end_and_skip_break(tmp_path):
    clock = FakeClock()
    service = PersistentWorkSessionService(path=tmp_path / "db.sqlite3", clock=clock)
    assert write(service, "start", title=" 项目汇报 ").view.title == "项目汇报"
    clock.advance(600)
    total = write(service, "adjust", version=1, origin="chat", total_s=2400)
    assert (total.view.planned_duration_s, total.view.remaining_s) == (2400, 1800)
    added = write(service, "adjust", version=2, origin="chat", add_s=600)
    assert (added.view.planned_duration_s, added.view.remaining_s) == (3000, 2400)
    assert write(service, "adjust", version=2, origin="chat", add_s=600).original_status == "applied"
    assert write(service, "adjust", version=2, origin="chat", add_s=1200).error_code == ErrorCode.INVALID_ARGUMENT
    assert write(service, "pause", version=2).error_code == ErrorCode.STALE_TASK

    pause = write(service, "pause", version=3).view
    clock.advance(3600)
    assert service.current_phase() == pause
    assert write(service, "start", "other").error_code == ErrorCode.INVALID_TRANSITION
    assert write(service, "resume", version=4).view.remaining_s == 2400
    clock.advance(2400)
    due = service.current_phase()
    assert due.state == "elapsed"
    assert due.actual_focus_s == 3000
    assert write(service, "start", "break", kind="short_break", previous_phase_id=due.id,
                 expected_previous_version=due.version).view.state == "running"
    assert sum(interval.seconds for interval in service.list_focus_intervals()) == 3000
    assert write(service, "skip_break", "break", version=1).view.state == "ended"
    assert service.current_phase() is None
    review = write(service, "review", version=due.version, confirmed_outcomes=["汇报草稿"]).view
    assert review.confirmed_outcomes == ("汇报草稿",)
    service.close()


def test_sleep_focus_pauses_credit_is_idempotent_and_break_expires(tmp_path):
    path = tmp_path / "business.sqlite3"
    clock = FakeClock()
    service = PersistentWorkSessionService(path=path, clock=clock)
    write(service, "start")
    clock.advance(600)
    service.record_sleep()
    clock.advance(1800)
    service.record_wake()
    paused = service.current_phase()
    assert (paused.state, paused.actual_focus_s, paused.remaining_s) == ("paused", 600, 900)
    interval = service.snapshot_fields()["phase_recovery"][0]
    assert interval["observed_until_at"] == interval["trusted_end_at"]
    credit = dict(interval_id=interval["id"], seconds=120)
    assert write(service, "credit_sleep", version=paused.version, **credit).view.remaining_s == 780
    assert write(service, "credit_sleep", version=paused.version, **credit).status == "duplicate"
    assert write(service, "credit_sleep", version=paused.version + 1, **credit).status == "rejected"
    assert write(service, "end", version=paused.version + 1).view.actual_focus_s == 720
    write(service, "start", "break", kind="short_break")
    service.record_sleep()
    clock.advance(1800)
    service.record_wake()
    assert service.current_phase() is None
    assert service.get_phase("break").state == "elapsed"
    service.close()


def test_force_exit_recovers_only_committed_work_even_with_wall_clock_jump(tmp_path):
    path = tmp_path / "business.sqlite3"
    script = """
import os
import sys
from deskpet.mocks.clock import FakeClock
from deskpet.work_sessions import PersistentWorkSessionService, PhaseCommand
clock = FakeClock()
service = PersistentWorkSessionService(path=sys.argv[1], clock=clock)
service.apply_phase(PhaseCommand(command_id='start', phase_id='phase', kind='start'))
clock.advance(600)
service.pump()
clock.advance(2)
service.current_phase()
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", script, str(path)], check=True)
    clock = FakeClock()
    clock.advance(86400)
    service = PersistentWorkSessionService(path=path, clock=clock)
    paused = service.current_phase()
    assert (paused.state, paused.actual_focus_s, paused.remaining_s) == ("paused", 600, 900)
    interval = service.snapshot_fields()["phase_recovery"][0]
    assert interval["trusted_end_at"] is None
    service.close()

    clock._wall -= timedelta(days=2)
    service = PersistentWorkSessionService(path=path, clock=clock)
    assert service.current_phase() == paused
    assert service.snapshot_fields()["phase_recovery"] == [interval]
    clock._wall += timedelta(days=2)
    start = FakeClock().wall_now() + timedelta(seconds=600)
    args = dict(recovery_interval_id=interval["id"], expected_recovery_version=interval["version"],
                started_at=start.isoformat(), ended_at=(start + timedelta(seconds=100)).isoformat())
    applied = write(service, "credit_recovery", version=paused.version, **args)
    assert applied.view.actual_focus_s == 700
    service.close()

    service = PersistentWorkSessionService(path=path, clock=clock)
    assert service.current_phase().actual_focus_s == 700
    assert write(service, "credit_recovery", version=paused.version, **args).status == "duplicate"
    assert sum(interval.seconds for interval in service.list_focus_intervals()) == 700
    service.close()


def test_issue_unlink_is_atomic_and_historical_reassignment_preserves_time(tmp_path):
    path = tmp_path / "business.sqlite3"
    clock = FakeClock()
    service = PersistentWorkSessionService(path=path, clock=clock)
    phase = write(service, "start", issue_links=["paper"], primary_issue_id="paper").view
    clock.advance(23)
    db = sqlite3.connect(path)
    db.execute("BEGIN IMMEDIATE")
    assert service.unlink_completed_issue("paper", db=db).primary_issue_id is None
    db.rollback()
    assert service.current_phase().primary_issue_id == "paper"

    db.execute("BEGIN IMMEDIATE")
    unlinked = service.unlink_completed_issue("paper", db=db)
    db.commit()
    clock.advance(7)
    old, current = service.list_focus_intervals()
    assert (old.primary_issue_id, old.seconds, old.ended_at is not None) == ("paper", 23, True)
    assert (current.primary_issue_id, current.seconds) == (None, 7)
    fix = write(service, "reassign_interval", version=unlinked.version, interval_id=old.id,
                expected_interval_version=old.version, target_issue_id="report")
    assert fix.view.actual_focus_s == 30 and phase.created_at == fix.view.created_at
    old, current = service.list_focus_intervals()
    assert (old.primary_issue_id, old.seconds) == ("report", 23)
    assert current.primary_issue_id is None
    db.close()
    service.close()


@pytest.mark.parametrize(("basis", "threshold"), [("focus_seconds", 11), ("completed_phases", 1)])
def test_long_break_waits_for_end_and_edit_only_changes_future_phase(tmp_path, basis, threshold):
    path = tmp_path / "business.sqlite3"
    clock = FakeClock()
    settings = SettingsStore(path)
    habit = settings.save_habit(title="论文", expected_version=0, behavior=HabitBehavior(
        focus_s=20, short_break_s=10, long_break_s=30, long_break_enabled=True,
        long_break_basis=basis, threshold=threshold, auto_start_break=True, auto_start_focus=True,
    ))
    service = PersistentWorkSessionService(path=path, clock=clock, settings_store=settings)
    phase = write(service, "start", habit_id=habit.id, expected_habit_version=habit.version).view
    changed = settings.save_habit(title=habit.title, expected_version=habit.version, habit_id=habit.id,
                                  behavior=habit.behavior.model_copy(update={"focus_s": 40, "long_break_s": 60}))
    clock.advance(12)
    assert service.current_phase().kind == "focus"
    assert service.current_phase().planned_duration_s == 20
    clock.advance(8)
    rest = service.current_phase()
    assert (rest.kind, rest.planned_duration_s, rest.applied_habit_version) == ("long_break", 60, changed.version)
    run = service.get_habit_run(phase.habit_run_id)
    assert run.focus_s_since_long_break == run.completed_phases_since_long_break == 0

    settings.save_habit(title=habit.title, expected_version=changed.version, habit_id=habit.id,
                        behavior=changed.behavior, status="archived")
    clock.advance(60)
    assert service.current_phase() is None
    assert len(service.list_phases()) == 2
    service.close()


def test_manual_habit_continuation_keeps_count_across_break_and_restart(tmp_path):
    path = tmp_path / "business.sqlite3"
    clock = FakeClock()
    settings = SettingsStore(path)
    habit = settings.save_habit(title="论文", expected_version=0, behavior=HabitBehavior(
        focus_s=20, short_break_s=10, long_break_s=30, long_break_enabled=True,
        long_break_basis="completed_phases", threshold=2,
    ))
    service = PersistentWorkSessionService(path=path, clock=clock, settings_store=settings)
    focus = write(service, "start", habit_id=habit.id, expected_habit_version=habit.version).view
    clock.advance(20)
    fields = service.snapshot_fields()
    suggested = fields["phase_suggested_break"]
    assert suggested.pop("duration_s") == 10
    assert write(service, "start", "rest", **suggested).view.kind == "short_break"
    assert write(service, "skip_break", "rest", version=1).status == "applied"
    service.close()

    service = PersistentWorkSessionService(path=path, clock=clock, settings_store=settings)
    continuation = service.snapshot_fields()["phase_habit_continuation"]
    continuation.pop("title")
    continuation.pop("duration_s")
    focus_again = write(service, "start", "next", **continuation).view
    assert focus_again.habit_run_id == focus.habit_run_id
    clock.advance(20)
    fields = service.snapshot_fields()
    assert fields["phase_suggested_break"]["kind"] == "long_break"
    assert fields["phase_habit_run"]["completed_phases_since_long_break"] == 2
    service.close()


def test_desktop_panel_clicks_use_the_same_versioned_phase_service(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from uuid import uuid4

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    from deskpet.c_ui.work_panel import PhasePanel
    from deskpet.m0.gui import QtDeliveryBridge, ServiceWorker

    app = QApplication.instance() or QApplication([])
    assert app is not None
    path = tmp_path / "business.sqlite3"
    args = SimpleNamespace(profile="scenario", scenario="persistent-phase-panel", output=None,
                           business_database=path)
    delivery = QtDeliveryBridge()
    worker = ServiceWorker(args, delivery)
    errors = []
    actions = []
    worker.error.connect(errors.append)

    def submit(action_id, *, payload):
        event_id = uuid4().hex
        actions.append({"kind": "ui", "action": {
            "event_id": event_id, "action_id": action_id, "surface_revision": 1, "payload": payload,
        }})
        return event_id

    panel = PhasePanel(submit=submit)
    worker.snapshot_ready.connect(panel.render_snapshot)
    worker.result_ready.connect(panel.receive_result)
    worker.start()
    assert not errors
    assert panel.minimumSizeHint().width() <= 275
    panel.duration.setValue(2)
    panel.buttons["start"].click()
    panel.buttons["start"].click()
    assert len(actions) == 1
    worker.act(actions.pop())
    assert "专注" in panel.summary.text()
    phase = worker.env.sessions.current_phase()
    assert (phase.title, phase.planned_duration_s) == ("专注", 120)

    adjusted = write(worker.env.sessions, "adjust", phase.id, version=phase.version, origin="chat", total_s=180)
    worker.snapshot()
    assert "03:00" in panel.summary.text()
    panel.buttons["pause"].click()
    worker.act(actions.pop())
    assert worker.env.sessions.current_phase().version == adjusted.view.version + 1
    assert "已暂停" in panel.summary.text()
    assert not errors
    worker.timer.stop()
    worker.env.sessions.close()
    worker.env.tasks.close()
    panel.close()


def test_desktop_review_headings_and_controls_fit_in_scroll_viewport(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt6")
    from PyQt6.QtCore import QPoint, QRect
    from PyQt6.QtWidgets import QApplication, QLabel

    from deskpet.c_ui.refine_style import DetailPanel, PANEL_WIDTH
    from deskpet.c_ui.work_panel import PhasePanel

    app = QApplication.instance() or QApplication([])
    clock = FakeClock()
    service = PersistentWorkSessionService(path=tmp_path / "business.sqlite3", clock=clock)
    try:
        focus = write(service, "start").view
        clock.advance(120)
        assert write(service, "end", version=focus.version).status == "applied"
        snapshot = service.snapshot_fields()
    finally:
        service.close()
    panel = PhasePanel(submit=lambda *args, **kwargs: None)
    details = DetailPanel("专注与回顾", panel, lambda: None)
    scroll = details._scroll.verticalScrollBar()
    viewport = details._scroll.viewport()

    def visible_rect(widget):
        return QRect(widget.mapTo(viewport, QPoint()), widget.size())

    try:
        details.show()
        panel.render_snapshot(snapshot)
        for width, height in ((PANEL_WIDTH, 440), (PANEL_WIDTH - 20, 285), (360, 590)):
            details.setFixedWidth(width)
            details.set_height_limit(height)
            details.resize(width, height)
            app.processEvents()
            assert scroll.maximum() > 0
            assert details._scroll.horizontalScrollBar().maximum() == 0

            for editor, title, field in (
                (panel.review_editor, "本轮回顾", panel.outcomes),
                (panel.interval_editor, "修改工时归属", panel.review_intervals),
            ):
                heading = editor.layout().itemAt(0).widget()
                assert isinstance(heading, QLabel) and heading.text() == title
                assert editor.accessibleName() == title
                scroll.setValue(heading.mapTo(panel, QPoint()).y() - 4)
                heading_rect = visible_rect(heading)
                field_rect = visible_rect(field)
                assert heading_rect.bottom() < field_rect.top()
                assert viewport.rect().contains(heading_rect) and viewport.rect().contains(field_rect)
                assert viewport.childAt(heading_rect.center()) is heading

            scroll.setValue(scroll.maximum())
            submit = panel.buttons["reassign_interval"]
            assert submit.isEnabled() and viewport.rect().contains(visible_rect(submit))
    finally:
        details.close()


def test_disabling_issue_linkage_commits_interval_boundary_with_settings(tmp_path):
    path = tmp_path / "business.sqlite3"
    clock = FakeClock()
    settings = SettingsStore(path)
    service = PersistentWorkSessionService(path=path, clock=clock, settings_store=settings)
    write(service, "start", issue_links=["paper"], primary_issue_id="paper")
    clock.advance(23)
    updated = settings.update(expected_version=settings.read().version, link_issues=False,
                              before_commit=service.apply_settings_update)
    assert not updated.link_issues
    clock.advance(7)
    assert service.current_phase().primary_issue_id is None
    assert service.issue_focus_summary("paper") == {
        "first_focus_at": FakeClock().wall_now(), "actual_focus_s": 23, "session_links": ["phase"],
    }
    old, current = service.list_focus_intervals()
    assert (old.seconds, current.seconds, current.primary_issue_id) == (23, 7, None)
    service.close()


def test_expired_break_during_sleep_does_not_start_overdue_habit_focus(tmp_path):
    path = tmp_path / "business.sqlite3"
    clock = FakeClock()
    settings = SettingsStore(path)
    habit = settings.save_habit(title="论文", expected_version=0, behavior=HabitBehavior(
        focus_s=20, short_break_s=10, auto_start_break=True, auto_start_focus=True,
    ))
    service = PersistentWorkSessionService(path=path, clock=clock, settings_store=settings)
    write(service, "start", habit_id=habit.id, expected_habit_version=habit.version)
    clock.advance(20)
    assert service.current_phase().kind == "short_break"
    service.record_sleep()
    clock.advance(30)
    service.record_wake()
    assert service.current_phase() is None
    assert service.snapshot_fields()["phase_habit_continuation"]["habit_id"] == habit.id
    assert len(service.list_phases()) == 2
    service.close()


@pytest.mark.parametrize("wake_before_deadline", [False, True])
def test_habit_focus_after_sleep_depends_on_break_deadline(tmp_path, wake_before_deadline):
    path = tmp_path / "business.sqlite3"
    clock = FakeClock()
    settings = SettingsStore(path)
    habit = settings.save_habit(title="论文", expected_version=0, behavior=HabitBehavior(
        focus_s=20, short_break_s=10, auto_start_break=True, auto_start_focus=True,
    ))
    primary = PersistentWorkSessionService(path=path, clock=clock, settings_store=settings)
    other = PersistentWorkSessionService(path=path, clock=clock, settings_store=settings)
    try:
        write(primary, "start", habit_id=habit.id, expected_habit_version=habit.version)
        clock.advance(20)
        rest = primary.current_phase()
        assert rest.kind == "short_break"
        primary.record_sleep()
        clock.advance(5)
        if wake_before_deadline:
            primary.record_wake()
        clock.advance(5)
        active = other.current_phase()
        assert other.get_phase(rest.id).state == "elapsed"
        if wake_before_deadline:
            assert (active.kind, active.source) == ("focus", "habit")
            assert len(primary.list_phases()) == 3
        else:
            assert active is None
            primary.record_wake()
            assert len(primary.list_phases()) == 2
            assert primary.snapshot_fields()["phase_habit_continuation"]["habit_id"] == habit.id
    finally:
        other.close()
        primary.close()
