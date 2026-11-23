"""Wait for an actual ten-second reminder through the shared desktop services."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--offscreen", action="store_true")
    parser.add_argument("--adapters", default="examples/m0/cd-desktop.adapters.json")
    args = parser.parse_args()
    if args.offscreen:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PyQt6.QtCore import QTimer
    from PyQt6.QtGui import QGuiApplication
    from PyQt6.QtWidgets import QApplication
    from deskpet.c_ui.desktop import DesktopSession
    from deskpet.m0.composition import provenance

    output = args.output
    output.mkdir(parents=True, exist_ok=False)
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    desktop = DesktopSession(app, SimpleNamespace(profile="realtime", scenario="c-realtime",
        output=str(output), auto_smoke=False, adapters=args.adapters))
    w = desktop.window
    p = w.ui_surface
    reminders, work = p.reminder_panel, p.work_panel
    source = "offscreen" if QGuiApplication.platformName() in {"offscreen", "minimal"} else "native"
    state = {"stage": 0, "since": time.monotonic()}
    started_monotonic = time.monotonic()
    started_wall = datetime.now(timezone.utc)
    checks, report = {}, {}
    timer = QTimer()

    def stage(n):
        state.update(stage=n, since=time.monotonic())

    def finish(error=None):
        timer.stop()
        w._grab_evidence(output / "workspace.png")
        report.update(provenance(), status="failed" if error or w.errors else "passed",
            error=error, host_errors=w.errors, checks=checks, source=source, clock="wall",
            automation=True, native_gui_verified=source == "native" and not error and not w.errors,
            reminder_elapsed_s=state.get("reminder_elapsed_s"), snooze_elapsed_s=state.get("snooze_elapsed_s"),
            manual_review="pending")
        desktop.quit()
        (output / "realtime-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    def tick():
        try:
            clock_gap = ((datetime.now(timezone.utc) - started_wall).total_seconds()
                         - (time.monotonic() - started_monotonic))
            if abs(clock_gap) > 2:
                raise RuntimeError(f"Environment interrupted by suspend or clock adjustment ({clock_gap:.1f}s); rerun while awake")
            elapsed = time.monotonic() - state["since"]
            if elapsed > 25:
                raise TimeoutError(f"stage {state['stage']}; visible={w.isVisible()}, active={w.isActiveWindow()}, errors={w.errors}")
            n = state["stage"]
            view = reminders.selected()
            session = work.current_session()
            if n == 0 and w.ready_for_actions and p.snapshot and (source == "offscreen" or w.isActiveWindow()):
                w.args.auto_smoke = True  # Tag harness actions after startup, without host auto-smoke.
                assert p.snapshot["clock"] == "wall"
                assert abs((datetime.fromisoformat(p.snapshot["now"]) - datetime.now(timezone.utc)).total_seconds()) < 2
                assert not w.advance_button.isEnabled() and not w.observation_advance_button.isEnabled()
                assert not hasattr(w, "_live_timer"), "realtime must not start OS sampling"
                checks["real_system_clock_no_fast_forward"] = True
                desktop.open("reminders")
                reminders.content.setText("实际等待 10 秒后提醒")
                reminders.mode.setCurrentIndex(reminders.mode.findData("hard"))
                reminders.delay.setValue(10)
                state["created_at"] = time.monotonic()
                reminders.create_button.click()
                stage(1)
            elif n == 1 and view and view["status"] == "draft" and not reminders.pending:
                reminders.buttons["confirm"].click()
                stage(2)
            elif n == 2 and view and view["status"] == "registered" and not reminders.pending:
                if view["displayed_at"]:
                    waited = time.monotonic() - state["created_at"]
                    assert 9.5 <= waited < 22
                    assert datetime.fromisoformat(view["displayed_at"]) >= datetime.fromisoformat(view["draft"]["earliest_at"])
                    assert p.cards and next(iter(p.cards.values()))._reported
                    state["reminder_elapsed_s"] = waited
                    state["first_delivery"] = next(iter(p.cards))
                    checks["ten_second_actual_wait_and_display_receipt"] = True
                    w._grab_evidence(output / "ten-second-reminder.png")
                    card = next(iter(p.cards.values()))
                    card.snooze_seconds.setValue(2)
                    state["snoozed_at"] = time.monotonic()
                    card.snooze.click()
                    stage(3)
                else:
                    assert not p.cards or time.monotonic() - state["created_at"] >= 9.5
            elif n == 3 and view and view["occurrence"] == 2 and view["displayed_at"] and not reminders.pending:
                waited = time.monotonic() - state["snoozed_at"]
                assert waited >= 1.8
                assert p.cards and state["first_delivery"] not in p.cards
                state["snooze_elapsed_s"] = waited
                checks["snooze_waits_actual_time"] = True
                next(iter(p.cards.values())).done.click()
                stage(4)
            elif n == 4 and view and view["status"] == "completed" and not p.cards:
                checks["completion_dismisses_reminder"] = True
                desktop.open("work")
                work.goal.setText("验证实际倒计时、暂停与继续")
                work.duration.setValue(3)
                work.start_button.click()
                stage(5)
            elif n == 5 and session and session["remaining_s"] < 2.4 and not work.pending:
                assert session["state"] == "focusing"
                work.buttons["pause"].click()
                stage(6)
            elif n == 6 and session and session["state"] == "paused" and not work.pending:
                state["paused_remaining"] = session["remaining_s"]
                stage(7)
            elif n == 7 and elapsed >= 1.1:
                assert session["remaining_s"] == state["paused_remaining"]
                checks["pause_freezes_remaining_in_real_time"] = True
                work.buttons["resume"].click()
                stage(8)
            elif n == 8 and session and session["state"] == "break_due" and not work.pending:
                checks["resume_expires_without_advancing"] = True
                work.buttons["end"].click()
                work.confirm_none.setChecked(True)
                work.confirm_end.click()
                stage(9)
            elif n == 9 and session and session["state"] == "ended" and not work.pending:
                checks["accepted_review_after_real_timer"] = True
                finish()
        except Exception:
            import traceback
            finish(traceback.format_exc())

    timer.timeout.connect(tick)
    timer.start(50)
    app.exec()
    print(json.dumps({k: v for k, v in report.items() if k not in {"file_hashes", "fixture_sha256"}}, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
