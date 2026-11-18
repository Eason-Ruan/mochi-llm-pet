"""Exercise latency, cost, interruption, and semantics calculators."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from d_check_metric_report import load_report, validate_report
from d_calculate_latency import calculate_latency_report, write_analysis as write_latency
from d_calculate_cost import calculate_cost_report, write_analysis as write_cost
from d_calculate_interruption import calculate_interruption_report, write_analysis as write_interruption
from d_calculate_semantics import calculate_semantics_report, write_analysis as write_semantics
from d_calculate_delivery_violations import calculate_violations_report, write_analysis as write_violations
from d_replay import run_recorded_replay


DATASET = ROOT / "evaluation" / "data" / "reminder-dev-pilot.json"


def event(at, kind, payload=None):
    return {"at_s": at, "kind": kind, "payload": payload or {}}


def command(at, action, version, payload=None, command_id=None):
    return event(at, "command", {"command_id": command_id or action, "kind": action,
                                "task_id": "water", "expected_version": version, "payload": payload or {}})


def draft(first=10, last=30, mode="soft"):
    return {"draft": {"content": "Drink water", "mode": mode, "timezone": "UTC",
                      "earliest_at": f"2026-01-01T17:00:{first:02d}Z",
                      "latest_first_delivery_at": f"2026-01-01T17:00:{last:02d}Z"}}


class MetricCalculatorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="d-metric-test-", dir=ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def full_suite(self):
        suite = self.base / "full-suite"
        run_recorded_replay(output=suite)
        return suite

    def test_latency_report_validates(self):
        report, audit = calculate_latency_report(self.full_suite())
        self.assertEqual(validate_report(report)["status"], "passed")
        metric = report["results"][0]
        self.assertEqual(metric["metric_id"], "latency.hard_due_p95")
        self.assertEqual(metric["metric_type"], "duration")
        self.assertEqual(metric["quantile"], 0.95)
        self.assertEqual(metric["quantile_method"], "nearest_rank")
        self.assertIn(metric["status"], {"computed", "insufficient_evidence"})
        self.assertIsNotNone(metric["counts"]["eligible_timing_targets"])

    def test_latency_p95_is_nonnegative_when_computed(self):
        report, _ = calculate_latency_report(self.full_suite())
        metric = report["results"][0]
        if metric["status"] == "computed":
            self.assertIsNotNone(metric["value"])
            self.assertGreaterEqual(metric["value"], 0)
            self.assertEqual(metric["sample_count"], metric["counts"]["measured"])

    def test_cost_report_is_not_measured_for_synthetic(self):
        report, audit = calculate_cost_report(self.full_suite())
        self.assertEqual(validate_report(report)["status"], "passed")
        metric = report["results"][0]
        self.assertEqual(metric["metric_id"], "cost.per_verified_displayed_occurrence")
        self.assertEqual(metric["status"], "not_measured")
        self.assertIsNone(metric["value"])
        self.assertEqual(metric["currency"], "USD")
        self.assertIsNone(metric["numerator"])
        self.assertIsNotNone(metric["denominator"])

    def test_cost_denominator_matches_verified_displays(self):
        report, _ = calculate_cost_report(self.full_suite())
        metric = report["results"][0]
        self.assertEqual(metric["denominator"], metric["counts"]["verified_displayed_occurrences"])

    def test_interruption_report_is_not_measured_for_synthetic(self):
        report, audit = calculate_interruption_report(self.full_suite())
        self.assertEqual(validate_report(report)["status"], "passed")
        metric = report["results"][0]
        self.assertEqual(metric["metric_id"], "interruption.user_reported_rate")
        self.assertEqual(metric["status"], "not_measured")
        self.assertIsNone(metric["value"])
        self.assertIsNone(metric["denominator"])

    def test_interruption_has_required_coverage_keys(self):
        report, _ = calculate_interruption_report(self.full_suite())
        metric = report["results"][0]
        for key in ("invitation", "rated_response", "overall_rated"):
            self.assertIn(key, metric["coverage"])

    def test_latency_cli_produces_report(self):
        suite = self.full_suite()
        output = self.base / "latency-out"
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "d_calculate_latency.py"),
             str(suite), "--output", str(output)],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((output / "metric-report.json").exists())

    def test_cost_cli_produces_report(self):
        suite = self.full_suite()
        output = self.base / "cost-out"
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "d_calculate_cost.py"),
             str(suite), "--output", str(output)],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((output / "metric-report.json").exists())

    def test_interruption_cli_produces_report(self):
        suite = self.full_suite()
        output = self.base / "int-out"
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "d_calculate_interruption.py"),
             str(suite), "--output", str(output)],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((output / "metric-report.json").exists())

    def test_semantics_report_validates(self):
        report, audit = calculate_semantics_report(DATASET)
        self.assertEqual(validate_report(report)["status"], "passed")
        self.assertEqual(len(report["results"]), 3)
        ids = {r["metric_id"] for r in report["results"]}
        self.assertEqual(ids, {
            "semantics.strict_interpretation_accuracy",
            "semantics.field_accuracy",
            "semantics.clarification_behavior_accuracy",
        })

    def test_semantics_not_measured_without_predictions(self):
        report, _ = calculate_semantics_report(DATASET)
        for result in report["results"]:
            self.assertEqual(result["status"], "not_measured")
            self.assertIsNone(result["value"])
            self.assertEqual(result["assessment"], "not_evaluated")
            self.assertIsNone(result["numerator"])
            self.assertIsNone(result["denominator"])

    def test_semantics_eligible_count_matches_dataset(self):
        report, audit = calculate_semantics_report(DATASET)
        self.assertEqual(audit["eligible_request_count"], 6)
        self.assertEqual(audit["double_annotated_samples"], 6)
        for result in report["results"]:
            self.assertEqual(result["counts"]["eligible_requests"], 6)
            self.assertEqual(result["coverage"]["prediction_records"]["denominator"], 6)

    def test_semantics_cli_produces_report(self):
        output = self.base / "sem-out"
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "d_calculate_semantics.py"),
             str(DATASET), "--output", str(output)],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((output / "metric-report.json").exists())
        report = load_report(output / "metric-report.json")
        self.assertEqual(validate_report(report)["status"], "passed")

    def test_delivery_violations_report_validates(self):
        report, audit = calculate_violations_report(self.full_suite())
        self.assertEqual(validate_report(report)["status"], "passed")
        ids = {r["metric_id"] for r in report["results"]}
        self.assertEqual(ids, {
            "delivery.post_cancellation_display_violations",
            "delivery.duplicate_render_violations",
        })

    def test_post_cancellation_violations_is_computed(self):
        report, _ = calculate_violations_report(self.full_suite())
        metric = next(r for r in report["results"]
                      if r["metric_id"] == "delivery.post_cancellation_display_violations")
        self.assertEqual(metric["status"], "computed")
        self.assertEqual(metric["metric_type"], "count")
        self.assertEqual(metric["unit"], "violation")
        self.assertEqual(metric["value"], metric["counts"]["violations"])
        self.assertGreaterEqual(metric["counts"]["violations"], 0)

    def test_duplicate_render_violations_not_measured(self):
        report, _ = calculate_violations_report(self.full_suite())
        metric = next(r for r in report["results"]
                      if r["metric_id"] == "delivery.duplicate_render_violations")
        self.assertEqual(metric["status"], "not_measured")
        self.assertIsNone(metric["value"])
        self.assertIsNone(metric["numerator"])

    def test_delivery_violations_cli_produces_report(self):
        suite = self.full_suite()
        output = self.base / "viol-out"
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "d_calculate_delivery_violations.py"),
             str(suite), "--output", str(output)],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((output / "metric-report.json").exists())


if __name__ == "__main__":
    unittest.main()
