"""Exercise C's real widgets and queued gateway with deterministic scenario time.

Run from the repository root after uv sync. Native is the default; --offscreen
is for integration tests and never counts as native display evidence.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import traceback
from types import SimpleNamespace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="output/m0/c-p1/native")
    parser.add_argument("--offscreen", action="store_true")
    args = parser.parse_args()
    if args.offscreen:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    # Set the platform before importing Qt; no application is created on import.
    from PyQt6.QtCore import QTimer
    from PyQt6.QtGui import QGuiApplication
    from PyQt6.QtWidgets import QApplication
    from deskpet.c_ui.surface import CWorkspace
    from deskpet.m0.composition import provenance
    from deskpet.m0.gui import Workbench

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    options = SimpleNamespace(profile="scenario", scenario="c-work-session", output=str(output),
                              auto_smoke=False, adapters="examples/m0/c-product.adapters.json")
    w = Workbench(options)
    p = w.ui_surface
    if not isinstance(p, CWorkspace):
        w.close()
        raise RuntimeError(f"C page failed to load: {w.errors}")
    panel = p.work_panel
    assert not panel.start_button.isEnabled()
    state = {"index": 0, "sent": False, "waits": 0}
    checks = {}
    report = {}
    timer = QTimer()
    source = "offscreen" if QGuiApplication.platformName() in ("offscreen", "minimal") else "native"

    def current():
        return panel.current_session() or {}

    def synced():
        view = current()
        return bool(view and w.session_views.get(view["session_id"]) == view and not panel.pending)

    def create_task():
        # Mark subsequent actions as automation without starting the host's own smoke.
        options.auto_smoke = True
        w.tabs.setCurrentIndex(0)
        w.content.setText("论文阅读事项")
        w.delay.setValue(3600)
        w.mode.setCurrentText("hard")
        w.create_button.click()

    def start():
        w.tabs.setCurrentWidget(p)
        p.open_section('work')
        panel.goal.setText("   ")
        assert not panel.start_button.isEnabled()
        panel.goal.setText("阅读论文的方法部分")
        panel.duration.setValue(60)
        panel.task.setCurrentIndex(panel.task.findData("gui-1"))
        panel.start_button.click()
        assert panel.pending and not panel.start_button.isEnabled()

    def advance(seconds):
        state["expected_time"] = w.latest["now"]
        w.action.emit({"kind": "advance", "seconds": seconds})

    def advanced_to(remaining):
        return w.latest["now"] != state["expected_time"] and synced() and current()["remaining_s"] == remaining

    def verify_started():
        if not (synced() and current()["state"] == "focusing"):
            return False
        state["first_id"] = current()["session_id"]
        assert current()["task_id"] == "gui-1" and current()["remaining_s"] == 60
        assert current()["version"] == 1
        return True

    def resume_from_diagnostics():
        w.tabs.setCurrentIndex(0)
        w.session_buttons["resume"].click()

    def open_review():
        w.tabs.setCurrentWidget(p)
        p.open_section('work')
        panel.buttons["end"].click()

    def fill_and_return():
        panel.outcomes.setPlainText("读完方法章节\n整理实验设置")
        panel.next_steps.setPlainText("复现实验")
        panel.cancel_end.click()
        assert current()["state"] == "focusing" and current()["version"] == 3

    def confirm_review():
        assert panel.outcomes.toPlainText() == "读完方法章节\n整理实验设置"
        assert panel.next_steps.toPlainText() == "复现实验"
        w._grab_evidence(output / "review-before-confirm.png")
        panel.confirm_end.click()

    def review_saved():
        if not (synced() and current()["state"] == "ended"):
            return False
        assert current()["version"] == 4 and current()["remaining_s"] == 30
        assert current()["confirmed_outcomes"] == ["读完方法章节", "整理实验设置"]
        assert current()["next_steps"] == ["复现实验"]
        assert "读完方法章节" in panel.review.text() and "读完方法章节" in w.review.text()
        assert w.views["gui-1"]["status"] == "registered", "ending a session completed its task"
        assert not panel.buttons["pause"].isEnabled() and not panel.buttons["end"].isEnabled()
        w._grab_evidence(output / "saved-review.png")
        return True

    def second_start():
        panel.goal.setText("验证到期提示")
        panel.duration.setValue(5)
        panel.start_button.click()

    def second_started():
        return synced() and len(p.snapshot["sessions"]) == 2 and current()["state"] == "focusing" and current()["remaining_s"] == 5

    def expired():
        if not (advanced_to(0) and current()["state"] == "break_due"):
            return False
        assert current()["version"] == 2 and "本轮时间已到" in panel.summary.text()
        assert not panel.buttons["pause"].isEnabled() and not panel.buttons["resume"].isEnabled()
        return True

    def empty_review():
        assert not panel.outcomes.toPlainText() and not panel.next_steps.toPlainText()
        panel.confirm_end.click()

    def empty_saved():
        if not (synced() and current()["state"] == "ended" and len(p.snapshot["sessions"]) == 2):
            return False
        assert current()["version"] == 3
        assert current()["confirmed_outcomes"] == current()["next_steps"] == []
        return True

    def select_first():
        panel.sessions.setCurrentRow(0)
        assert current()["session_id"] == state["first_id"]
        assert "读完方法章节" in panel.review.text()

    def ready():
        if not (w.ready_for_actions and p.snapshot):
            return False
        if source == "native":
            return w.isActiveWindow() and bool(w.windowHandle() and w.windowHandle().isExposed())
        return True

    steps = [
        (lambda: None, ready, "service_and_native_window_ready"),
        (create_task, lambda: "gui-1" in w.views, "task_created"),
        (lambda: w.confirm_button.click(), lambda: w.views["gui-1"]["status"] == "registered" and panel.task.findData("gui-1") >= 0, "task_confirmed"),
        (start, verify_started, "c_start_and_shared_identity"),
        (lambda: advance(20), lambda: advanced_to(40), "focus_consumes_time"),
        (lambda: panel.buttons["pause"].click(), lambda: synced() and current()["state"] == "paused" and current()["version"] == 2, "c_pause"),
        (lambda: advance(120), lambda: advanced_to(40) and current()["state"] == "paused", "pause_preserves_remaining"),
        (resume_from_diagnostics, lambda: synced() and current()["state"] == "focusing" and current()["version"] == 3, "diagnostics_resume_syncs_c"),
        (open_review, lambda: panel.review_editor.isVisible(), "review_is_explicit"),
        (fill_and_return, lambda: panel.review_editor.isHidden(), "return_preserves_session"),
        (lambda: advance(10), lambda: advanced_to(30), "resume_consumes_remaining"),
        (open_review, lambda: panel.review_editor.isVisible(), "review_draft_restored"),
        (confirm_review, review_saved, "confirmed_review_and_task_independence"),
        (second_start, second_started, "select_new_session"),
        (lambda: advance(5), expired, "expiry_and_button_states"),
        (open_review, lambda: panel.review_editor.isVisible(), "second_review_is_separate"),
        (empty_review, empty_saved, "empty_user_review_allowed"),
        (select_first, lambda: "读完方法章节" in panel.review.text(), "history_selection"),
    ]

    def finish(error=None):
        timer.stop()
        w._grab_evidence(output / "c-work-sessions.png")
        snapshot = p.snapshot
        w.close()
        events = [json.loads(line) for line in (output / "trace.jsonl").read_text().splitlines()]
        session_events = [e for e in events if e["kind"].startswith("session.")]
        report.update(provenance(), status="failed" if error or w.errors else "passed",
                      checks=checks, error=error, host_errors=w.errors, source=source,
                      native_gui_verified=source == "native" and not error and not w.errors,
                      adapters=options.adapters, sessions=snapshot.get("sessions", []),
                      task=w.views.get("gui-1"), session_event_count=len(session_events),
                      manual_review="pending")
        (output / "c-work-sessions-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        app.quit()

    def tick():
        try:
            if state["index"] == len(steps):
                finish()
                return
            action, condition, name = steps[state["index"]]
            if not state["sent"]:
                action()
                state["sent"] = True
            if condition():
                checks[name] = True
                state.update(index=state["index"] + 1, sent=False, waits=0)
            else:
                state["waits"] += 1
                if state["waits"] >= 160:
                    raise TimeoutError(f"waiting for {name}; errors={w.errors}")
        except Exception:
            finish(traceback.format_exc())

    w.show()
    QTimer.singleShot(0, w.activate_for_launch)
    timer.timeout.connect(tick)
    timer.start(50)
    app.exec()
    if not report:
        return 1
    print(json.dumps({"status": report["status"], "checks": len(checks), "source": source,
                      "report": str(output / "c-work-sessions-report.json"), "error": report["error"]}, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
