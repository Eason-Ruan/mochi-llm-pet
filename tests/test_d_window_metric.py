"""Exercise window calculation against real recorded M0 replay evidence."""

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from d_calculate_window_metric import calculate_case, calculate_window_report, write_analysis
from d_check_metric_report import load_report, validate_report
from d_replay import run_recorded_replay
from d_run_evidence import MANIFEST_NAME, file_sha256


def event(at, kind, payload=None):
    return {"at_s": at, "kind": kind, "payload": payload or {}}


def command(at, action, version, payload=None, command_id=None):
    return event(at, "command", {"command_id": command_id or action, "kind": action,
                                "task_id": "water", "expected_version": version, "payload": payload or {}})


def draft(first=10, last=30, mode="soft"):
    return {"draft": {"content": "Drink water", "mode": mode, "timezone": "UTC",
                      "earliest_at": f"2026-01-01T17:00:{first:02d}Z",
                      "latest_first_delivery_at": f"2026-01-01T17:00:{last:02d}Z"}}


class WindowMetricTests(unittest.TestCase):
    def setUp(self):
        # d_replay intentionally accepts only fixture paths inside the repository.
        self.temporary = tempfile.TemporaryDirectory(prefix="d-window-test-", dir=ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.sequence = 0

    def recorded(self, tail, *, state="working", mode="soft", create_only=False):
        self.sequence += 1
        path = self.base / f"scenario-{self.sequence}.json"
        initial = [event(0, "observation", {"state": state}), command(0, "create", 0, draft(last=10 if mode == "hard" else 30, mode=mode))]
        if not create_only:
            initial.append(command(0, "confirm", 1))
        events = initial + tail
        for seq, value in enumerate(events, 1):
            value["seq"] = seq
        path.write_text(json.dumps({"scenario_id": f"window-{self.sequence}", "initial_time": "2026-01-01T17:00:00Z",
                                   "events": events, "expected_outcomes": {"tasks": {}}}), encoding="ascii")
        suite = self.base / f"run-{self.sequence}"
        run_recorded_replay(output=suite, scenario=str(path))
        return suite

    def metric(self, suite):
        report, audit = calculate_window_report(suite)
        self.assertEqual(validate_report(report)["status"], "passed")
        self.assertEqual(report["evidence_type"], "synthetic_replay")
        return report["results"][0], audit

    def test_full_m0_suite_keeps_the_unfinished_window_undetermined(self):
        suite = self.base / "full-suite"
        run_recorded_replay(output=suite)
        metric, audit = self.metric(suite)
        self.assertEqual(audit["scenario_count"], 20)
        self.assertEqual((metric["numerator"], metric["denominator"]), (4, 5))
        self.assertEqual(metric["status"], "insufficient_evidence")
        self.assertIsNone(metric["value"])
        self.assertEqual((metric["bounds"]["lower"], metric["bounds"]["upper"]), (0.8, 1.0))
        pending = [row for row in audit["rows"] if row["status"] == "undetermined"]
        self.assertEqual([r["scenario_id"] for r in pending], ["expired-observation"])

    def test_closed_interval_includes_earliest_opportunity(self):
        metric, audit = self.metric(self.recorded([event(10, "advance")], state="available"))
        self.assertEqual(metric["value"], 1.0)
        self.assertEqual(audit["rows"][0]["first_rendered_at"], "2026-01-01T17:00:10+00:00")

    def test_closed_interval_includes_deadline_fallback(self):
        metric, _ = self.metric(self.recorded([event(30, "advance")]))
        self.assertEqual(metric["value"], 1.0)

    def test_complete_window_without_receipt_is_a_miss_not_missing_evidence(self):
        metric, audit = self.metric(self.recorded([event(0, "fault", {"delivery": "missing"}), event(30, "advance")]))
        self.assertEqual(metric["status"], "computed")
        self.assertEqual(metric["value"], 0.0)
        self.assertEqual(metric["evidence_missing"]["delivery_evidence"]["count"], 0)
        self.assertEqual(audit["rows"][0]["status"], "miss")

    def test_open_window_is_unknown_not_a_miss_or_exclusion(self):
        metric, _ = self.metric(self.recorded([event(10, "advance")]))
        self.assertEqual(metric["counts"]["undetermined_occurrences"], 1)
        self.assertEqual(metric["denominator"], 1)
        self.assertEqual(metric["excluded"], {})
        self.assertEqual(metric["bounds"]["upper"], 1.0)

    def test_predeadline_cancellation_is_explicitly_excluded(self):
        metric, audit = self.metric(self.recorded([command(8, "cancel", 2), event(30, "advance")]))
        self.assertEqual(metric["status"], "not_applicable")
        self.assertEqual((metric["counts"]["total_occurrences"], metric["denominator"]), (1, 0))
        self.assertEqual(metric["excluded"], {"cancelled_before_first_display_and_deadline": 1})
        self.assertEqual(audit["rows"][0]["closure"]["kind"], "task.cancelled")

    def test_cancellation_after_a_miss_preserves_the_obligation(self):
        metric, _ = self.metric(self.recorded([event(0, "fault", {"delivery": "missing"}), event(30, "advance"), command(35, "cancel", 2)]))
        self.assertEqual(metric["denominator"], 1)
        self.assertEqual(metric["value"], 0.0)

    def test_cancellation_after_display_preserves_historical_success(self):
        metric, _ = self.metric(self.recorded([event(10, "advance"), command(20, "cancel", 2)], state="available"))
        self.assertEqual(metric["value"], 1.0)
        self.assertEqual(metric["excluded"], {})

    def test_same_time_cancellation_before_deadline_pump_is_excluded(self):
        metric, _ = self.metric(self.recorded([command(30, "cancel", 2)]))
        self.assertEqual(metric["denominator"], 0)

    def test_same_time_cancellation_after_deadline_request_does_not_erase_miss(self):
        metric, _ = self.metric(self.recorded([event(0, "fault", {"delivery": "missing"}), event(30, "advance"), command(30, "cancel", 2)]))
        self.assertEqual(metric["denominator"], 1)
        self.assertEqual(metric["value"], 0.0)

    def test_failed_attempt_then_retry_counts_one_successful_occurrence(self):
        metric, audit = self.metric(self.recorded([event(0, "fault", {"delivery": "failed"}), event(10, "advance"),
                                                  event(11, "fault", {"delivery": "displayed"})], state="available"))
        self.assertEqual((metric["numerator"], metric["denominator"]), (1, 1))
        self.assertEqual(len(audit["rows"][0]["request_seqs"]), 2)

    def test_late_render_is_not_window_success(self):
        metric, audit = self.metric(self.recorded([event(0, "fault", {"delivery": "missing"}), event(30, "advance"),
                                                  event(35, "receipt", {"delivery_id": "$last"})]))
        self.assertEqual(metric["value"], 0.0)
        self.assertEqual(audit["rows"][0]["reason"], "first_display_outside_window")

    def test_delayed_receipt_uses_owner_render_time_not_event_arrival(self):
        metric, audit = self.metric(self.recorded([event(0, "fault", {"delivery": "missing"}), event(30, "advance"),
                            event(35, "receipt", {"delivery_id": "$last", "rendered_at": "2026-01-01T17:00:30Z"})]))
        self.assertEqual(metric["value"], 1.0)
        self.assertEqual(audit["rows"][0]["first_rendered_at"], "2026-01-01T17:00:30+00:00")

    def test_duplicate_receipt_does_not_change_first_display_or_population(self):
        metric, _ = self.metric(self.recorded([event(0, "fault", {"delivery": "missing"}), event(30, "advance"),
                            event(35, "receipt", {"delivery_id": "$last", "rendered_at": "2026-01-01T17:00:30Z"}),
                            event(36, "receipt", {"delivery_id": "$last", "rendered_at": "2026-01-01T17:00:31Z"})]))
        self.assertEqual(metric["denominator"], 1)
        self.assertEqual(metric["value"], 1.0)

    def test_multiple_accepted_receipts_cannot_be_dated_from_one_snapshot(self):
        suite = self.recorded([event(30, "advance")])
        case = load_report(suite / "report.json")["cases"][0]
        events = [json.loads(line) for line in (suite / case["scenario_id"] / "trace.jsonl").read_text().splitlines()]
        extra = copy.deepcopy(next(e for e in events if e["kind"] == "delivery.displayed"))
        extra["seq"] = len(events) + 1
        events.append(extra)
        rows, _ = calculate_case(case, events)
        self.assertEqual(rows[0]["status"], "undetermined")
        self.assertEqual(rows[0]["reason"], "multiple_receipts_without_first_render_timestamp")

    def test_edit_before_deadline_closes_old_occurrence_and_uses_owner_new_occurrence(self):
        metric, audit = self.metric(self.recorded([command(9, "edit", 2, draft(40, 50)), event(50, "advance")]))
        self.assertEqual((metric["counts"]["total_occurrences"], metric["denominator"]), (2, 1))
        self.assertEqual(metric["value"], 1.0)
        self.assertEqual([r["occurrence"] for r in audit["rows"]], [1, 2])

    def test_edit_after_deadline_keeps_the_previous_miss(self):
        metric, _ = self.metric(self.recorded([event(0, "fault", {"delivery": "missing"}), event(30, "advance"),
                    command(35, "edit", 2, draft(40, 50)), event(40, "fault", {"delivery": "displayed"}), event(50, "advance")]))
        self.assertEqual((metric["numerator"], metric["denominator"]), (1, 2))
        self.assertEqual(metric["value"], 0.5)

    def test_snooze_uses_owner_occurrence_and_records_old_closure(self):
        metric, audit = self.metric(self.recorded([command(9, "snooze", 2, {"seconds": 10}), event(39, "advance")]))
        self.assertEqual((metric["numerator"], metric["denominator"]), (1, 1))
        self.assertEqual(metric["excluded"], {"snoozed_before_first_display_and_deadline": 1})
        self.assertEqual([r["occurrence"] for r in audit["rows"]], [1, 2])

    def test_completion_without_display_does_not_invent_a_withdrawal_rule(self):
        metric, _ = self.metric(self.recorded([command(8, "complete", 2), event(30, "advance")]))
        self.assertEqual(metric["denominator"], 1)
        self.assertEqual(metric["status"], "insufficient_evidence")
        self.assertEqual(metric["excluded"], {})

    def test_duplicate_command_does_not_increase_population(self):
        metric, _ = self.metric(self.recorded([command(0, "confirm", 1), event(30, "advance")]))
        self.assertEqual(metric["denominator"], 1)

    def test_hard_reminders_and_unconfirmed_drafts_are_outside_soft_cohort(self):
        for kwargs in ({"mode": "hard"}, {"create_only": True}):
            with self.subTest(kwargs=kwargs):
                metric, _ = self.metric(self.recorded([event(30, "advance")], **kwargs))
                self.assertEqual(metric["counts"]["total_occurrences"], 0)
                self.assertEqual(metric["status"], "not_applicable")

    def test_missing_owner_timestamp_is_not_replaced_by_event_time(self):
        suite = self.recorded([event(30, "advance")])
        case = load_report(suite / "report.json")["cases"][0]
        events = [json.loads(line) for line in (suite / case["scenario_id"] / "trace.jsonl").read_text().splitlines()]
        case["outcomes"]["tasks"]["water"]["displayed_at"] = None
        rows, _ = calculate_case(case, events)
        self.assertEqual(rows[0]["status"], "undetermined")
        self.assertIsNone(rows[0]["first_rendered_at"])

    def test_tampered_source_report_prevents_publication(self):
        suite = self.recorded([event(30, "advance")])
        path = suite / "report.json"
        path.write_bytes(path.read_bytes() + b"\n")
        output = self.base / "bad-analysis"
        with self.assertRaises(ValueError):
            write_analysis(suite, output)
        self.assertFalse(output.exists())

    def test_legacy_suite_without_runtime_manifest_is_rejected(self):
        suite = self.recorded([event(30, "advance")])
        (suite / MANIFEST_NAME).unlink()
        with self.assertRaises(OSError):
            calculate_window_report(suite)

    def test_output_is_new_separate_and_preserves_source_artifacts(self):
        suite = self.recorded([event(30, "advance")])
        before = file_sha256(suite / "report.json")
        output = self.base / "analysis"
        report = write_analysis(suite, output)
        self.assertEqual(validate_report(report)["status"], "passed")
        manifest = load_report(output / "analysis-manifest.json")
        for name, digest in manifest["artifacts"].items():
            self.assertEqual(file_sha256(output / name), digest)
        for name, digest in manifest["analysis_input_sha256"].items():
            self.assertEqual(file_sha256(output / "analysis-inputs" / name), digest)
        self.assertEqual(file_sha256(suite / "report.json"), before)
        with self.assertRaises(ValueError):
            write_analysis(suite, output)
        with self.assertRaises(ValueError):
            write_analysis(suite, suite / "analysis")

    def test_analysis_source_change_prevents_publication(self):
        suite = self.recorded([event(30, "advance")])
        target = ROOT / "scripts/d_calculate_window_metric.py"
        original = Path.read_bytes
        reads = 0

        def changing_read(path):
            nonlocal reads
            raw = original(path)
            if path == target:
                reads += 1
                if reads > 1:
                    return raw + b"\n"
            return raw

        output = self.base / "changed-source-analysis"
        with mock.patch.object(Path, "read_bytes", changing_read):
            with self.assertRaisesRegex(ValueError, "changed during calculation"):
                write_analysis(suite, output)
        self.assertFalse(output.exists())

    def test_cli_distinguishes_analysis_success_from_incomplete_measurement(self):
        suite = self.recorded([event(10, "advance")])
        output = self.base / "cli-analysis"
        result = subprocess.run([sys.executable, str(ROOT / "scripts/d_calculate_window_metric.py"), str(suite),
                                 "--output", str(output)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["metric"]["status"], "insufficient_evidence")
        self.assertIsNone(summary["metric"]["value"])
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
