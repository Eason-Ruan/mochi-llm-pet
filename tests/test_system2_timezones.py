"""User-zone calendar dates survive model and Qt writes across DST transitions."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from deskpet.c_ui.desktop_system2 import DesktopSystem2
from deskpet.system2.host import System2Host
from tests.test_system2_host import FixtureEngine
from tests.test_system2_tools import LocalBusiness


def test_desktop_model_and_rule_share_the_system_iana_timezone(tmp_path, monkeypatch):
    reference = datetime(2026, 3, 8, 4, 30, tzinfo=UTC)
    monkeypatch.setenv("TZ", "America/New_York")
    monkeypatch.setattr(System2Host, "start", lambda self: None)
    business = LocalBusiness(tmp_path)
    business.clock.advance((reference - business.clock.wall_now()).total_seconds())
    writes = []

    def model(request, payload, call):
        context = payload["context"]
        assert context["timezone"] == "America/New_York"
        assert "context.timezone" in request.system_prompt
        zone = ZoneInfo(context["timezone"])
        now = datetime.fromisoformat(context["as_of"]).astimezone(zone)
        assert now.isoformat() == "2026-03-07T23:30:00-05:00"
        target = (now + timedelta(days=1)).replace(hour=15, minute=0)
        assert target.isoformat() == "2026-03-08T15:00:00-04:00"
        writes.append(call("item_create", "plan", {
            "type": "reminder", "title": "交作业",
            "reminder_rule": {"remind_after": target.isoformat(), "remind_by": target.isoformat()},
        }, step_id="dsh:1:1"))
        return "已创建提醒。"

    args = SimpleNamespace(data_dir=tmp_path, business_database=business.path, desktop_lock_probe=lambda: False,
                           desktop_display_capabilities={}, system2_engine=FixtureEngine(model))
    env = SimpleNamespace(tasks=business.tasks, sessions=business.phases, clock=business.clock,
                          observation_context_reader=business.scheduler.observation)
    desktop = DesktopSystem2(args, env, business.settings)
    try:
        desktop.host.submit_user("明天下午三点准点提醒我交作业", event_id="dst-calendar")
        assert desktop.host.pump()
        write, = writes
        assert write["status"] == "applied"
        item = business.tasks.get(write["view"]["id"])
        assert item.draft.timezone == "America/New_York"
        assert item.draft.remind_by == datetime(2026, 3, 8, 19, tzinfo=UTC)

        explicit = desktop.gateway.execute_local("reminder.configure", {
            "item_id": item.task_id, "rule": {"timezone": "Europe/Berlin", "target_at": "2026-03-09T12:00:00+01:00"},
            "expected_version": item.version,
        }, event_id="explicit-zone")
        edited = desktop.gateway.execute_local("reminder.configure", {
            "item_id": item.task_id, "rule": {"target_at": "2026-03-10T12:00:00+01:00"},
            "expected_version": explicit.view["version"],
        }, event_id="keep-zone")
        assert explicit.status == edited.status == "applied"
        assert edited.view["rule"]["timezone"] == "Europe/Berlin"
    finally:
        desktop.close()
        business.close()


def test_qt_reminder_preserves_the_second_hour_when_dst_ends(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    from deskpet.c_ui.reminder import ReminderPanel, display_time

    app = QApplication.instance() or QApplication([])
    assert app is not None
    writes = []

    def submit(action, **fields):
        writes.append((action, fields))
        return "qt-time"

    zone = "America/New_York"
    panel = ReminderPanel(submit=submit, advanced=True, timezone=zone)
    try:
        reference = "2026-11-01T03:30:00+00:00"
        earlier = "2026-11-01T05:30:00+00:00"
        later = "2026-11-01T06:30:00+00:00"
        panel.render_snapshot({"now": reference})
        panel.times["exact"].load(later, reference, required=True)
        panel.content.setText("提交报告")
        assert panel.times["exact"].date.text() == "2026-11-01"
        assert panel.times["exact"].time.text() == "01:30:00"
        assert panel.preview.text() == "明天 01:30 · 准点提醒"
        assert panel.preview_timezone.text() == "America/New_York · UTC-05:00"
        assert display_time(earlier, reference, zone=zone).endswith("UTC-04:00）")
        assert display_time(later, reference).endswith("北京时间 UTC+08:00）")

        panel.create_button.click()
        (action, fields), = writes
        assert action == "reminder.create"
        draft = fields["payload"]["draft"]
        assert draft["timezone"] == zone
        assert draft["remind_by"] == "2026-11-01T01:30:00-05:00"
    finally:
        panel.close()


def test_semantic_relative_reminder_uses_elapsed_time_across_dst(tmp_path):
    reference = datetime(2026, 3, 8, 6, 30, tzinfo=UTC)
    business = LocalBusiness(tmp_path)
    business.clock.advance((reference - business.clock.wall_now()).total_seconds())

    def model(_request, payload, call):
        assert payload["user_intent"]["requested_at"] == "2026-03-08T01:30:00-05:00"
        result = call("item_create", "relative", {"type": "reminder", "title": "浇花", "reminder_rule": {
            "remind_after_offset_s": 3600, "remind_by_offset_s": 3600,
        }})
        assert result["outcome"] == "committed"
        return ""

    host = System2Host(tmp_path, scheduler=business.scheduler, gateway=business.tools,
                       engine=FixtureEngine(model), settings_store=business.settings, clock=business.clock,
                       context_reader=business.scheduler.observation, timezone="America/New_York")
    try:
        host.submit_user("过一小时叫我去浇花", created_at=reference)
        business.clock.advance(5)
        assert host.pump()
        task, = business.tasks.list_tasks()
        assert task.draft.remind_by == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)
        assert host.ui_events()[0].payload["text"] == "已设置提醒「浇花」，03:30:00。"
    finally:
        host.close()
        business.close()
