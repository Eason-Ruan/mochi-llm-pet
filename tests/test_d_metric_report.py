"""Regression tests for the limited v0.4 metric-report contract validator."""

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "d_check_metric_report.py"
spec = importlib.util.spec_from_file_location("d_metric_report", SCRIPT)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class MetricReportTests(unittest.TestCase):
    def setUp(self):
        self.report = checker.load_report(ROOT / "evaluation/data/metric-report-example.json")

    def result(self, index):
        self.report["results"] = [self.report["results"][index]]
        return self.report["results"][0]

    def rejected(self):
        with self.assertRaises(ValueError):
            checker.validate_report(self.report)

    def unavailable(self, result, status="insufficient_evidence"):
        result.update(status=status, value=None, value_scope="not_available", assessment="not_evaluated")
        result["warnings"] = [{"code": "TEST_UNAVAILABLE", "message": "Evidence is unavailable in this fixture."}]

    def test_documented_example_passes_without_claiming_evidence_verification(self):
        summary = checker.validate_report(self.report)
        self.assertEqual(summary["metric_status_counts"], {"computed": 2, "preliminary": 2})
        self.assertEqual(summary["metric_count"], 4)
        self.assertTrue(summary["example_only"])
        self.assertEqual(summary["source_evidence_verification"], "not_performed")
        self.assertEqual(summary["raw_percentile_recomputation"], "not_performed")
        self.assertFalse(summary["models_called_by_validator"])

    def test_single_metric_report_passes(self):
        self.result(0)
        self.assertEqual(checker.validate_report(self.report)["metric_count"], 1)

    def test_wrong_ratio_arithmetic_is_rejected(self):
        self.result(0)["value"] = 0.75
        self.rejected()

    def test_invalid_numeric_types_and_nonfinite_values_are_rejected(self):
        original = copy.deepcopy(self.report)
        for value in (True, "0.5", float("nan"), float("inf"), -1, 1.1):
            with self.subTest(value=value):
                self.report = copy.deepcopy(original)
                self.result(0)["value"] = value
                self.rejected()

    def test_boolean_and_negative_counts_are_rejected(self):
        original = copy.deepcopy(self.report)
        for value in (True, -1, 2.0):
            with self.subTest(value=value):
                self.report = copy.deepcopy(original)
                self.result(0)["counts"]["eligible_occurrences"] = value
                self.rejected()

    def test_cost_requires_currency_and_display_basis(self):
        original = copy.deepcopy(self.report)
        for key, value in (("currency", None), ("currency", "usd"), ("unit", "currency"),
                           ("denominator_unit", "completed_task")):
            with self.subTest(key=key, value=value):
                self.report = copy.deepcopy(original)
                self.result(2)[key] = value
                self.rejected()

    def test_cost_cannot_use_completed_task_count_as_denominator(self):
        result = self.result(2)
        result.update(denominator=1, value=1.2)
        self.rejected()

    def test_wrong_coverage_arithmetic_and_units_are_rejected(self):
        original = copy.deepcopy(self.report)
        for key, value in (("value", 0.9), ("unit", "call"), ("numerator", 1)):
            with self.subTest(key=key):
                self.report = copy.deepcopy(original)
                self.result(0)["coverage"]["usable_occurrence_evidence"][key] = value
                self.rejected()

    def test_experience_statuses_must_reconcile(self):
        self.result(1)["counts"]["declined"] = 2
        self.rejected()

    def test_invitation_distribution_requires_valid_reasons_and_sum(self):
        original = copy.deepcopy(self.report)
        for distribution in ({"unknown_reason": 1}, {"no_user_interaction_before_deadline": 2}, None):
            with self.subTest(distribution=distribution):
                self.report = copy.deepcopy(original)
                self.result(1)["not_invited_reason_counts"] = distribution
                self.rejected()

    def test_unknown_invitation_reasons_require_a_matching_evidence_gap(self):
        result = self.result(1)
        result["not_invited_reason_counts"] = None
        result["warnings"].append({"code": "INVITATION_REASON_EVIDENCE_GAP", "message": "One reason is missing."})
        self.rejected()
        result["evidence_missing"]["experience_records"].update(count=1, reason_counts={"invalid_record": 1})
        self.assertEqual(checker.validate_report(self.report)["status"], "passed")

    def test_zero_not_invited_requires_empty_distribution(self):
        result = self.result(1)
        result["counts"].update(eligible_experiences=5, not_invited=0)
        for name in ("invitation", "overall_rated"):
            coverage = result["coverage"][name]
            coverage.update(denominator=5, value=coverage["numerator"] / 5)
        result["not_invited_reason_counts"] = {}
        self.assertEqual(checker.validate_report(self.report)["status"], "passed")
        result["not_invited_reason_counts"] = None
        self.rejected()

    def test_evidence_gap_reason_counts_must_reconcile(self):
        result = self.result(3)
        result["evidence_missing"]["latency_measurements"]["reason_counts"] = {"missing_required_value": 2}
        self.rejected()

    def test_zero_gap_is_not_an_unknown_or_zero_filled_reason_map(self):
        self.result(0)["evidence_missing"]["delivery_evidence"]["reason_counts"] = {"missing_record": 0}
        self.rejected()

    def test_required_evidence_domain_cannot_be_omitted(self):
        del self.result(2)["evidence_missing"]["cost_accounting"]
        self.rejected()

    def test_partial_response_cannot_be_promoted_to_computed(self):
        result = self.result(1)
        result.update(status="computed", value_scope="eligible_cohort", warnings=[])
        self.rejected()

    def test_partial_latency_cannot_be_promoted_to_computed(self):
        result = self.result(3)
        result.update(status="computed", value_scope="eligible_cohort", warnings=[])
        self.rejected()

    def test_preliminary_result_cannot_pass_acceptance(self):
        self.result(1)["assessment"] = "passed"
        self.rejected()

    def test_subset_warnings_are_required(self):
        original = copy.deepcopy(self.report)
        for index in (1, 3):
            with self.subTest(index=index):
                self.report = copy.deepcopy(original)
                self.result(index)["warnings"][0]["code"] = "UNRELATED"
                self.rejected()

    def test_damaged_window_evidence_requires_null_point_and_correct_bounds(self):
        result = self.result(0)
        result["evidence_missing"]["delivery_evidence"].update(count=1, reason_counts={"missing_record": 1})
        result["coverage"]["usable_occurrence_evidence"].update(numerator=1, value=0.5)
        self.rejected()
        self.unavailable(result)
        result["bounds"] = {"lower": 0.5, "upper": 1.0, "unit": "fraction", "value_scope": "eligible_cohort"}
        self.assertEqual(checker.validate_report(self.report)["status"], "passed")
        result["bounds"]["upper"] = 0.75
        self.rejected()

    def test_incomplete_cost_cannot_keep_computed_or_preliminary_value(self):
        result = self.result(2)
        result["evidence_missing"]["cost_accounting"].update(count=1, reason_counts={"missing_required_value": 1})
        result["coverage"]["accounted_calls"].update(numerator=4, value=0.8)
        self.rejected()
        result.update(status="preliminary", value_scope="measured_subset")
        result["warnings"] = [{"code": "INCOMPLETE_COST", "message": "One call is unsettled."}]
        self.rejected()
        self.unavailable(result)
        self.assertEqual(checker.validate_report(self.report)["status"], "passed")

    def test_zero_denominator_requires_null_not_a_fabricated_zero_rate(self):
        result = self.result(0)
        result.update(numerator=0, denominator=0, value=0)
        result["counts"]["eligible_occurrences"] = 0
        result["counts"]["total_occurrences"] = 1
        result["coverage"]["usable_occurrence_evidence"].update(numerator=0, denominator=0, value=None, status="not_applicable")
        self.rejected()
        self.unavailable(result, "not_applicable")
        self.assertEqual(checker.validate_report(self.report)["status"], "passed")

    def test_not_measured_keeps_evidence_unknown(self):
        result = self.result(0)
        self.unavailable(result, "not_measured")
        self.rejected()
        result.update(numerator=None, denominator=None)
        result["counts"] = {"eligible_occurrences": None, "total_occurrences": None}
        result["evidence_missing"]["delivery_evidence"].update(count=None, reason_counts=None)
        result["coverage"]["usable_occurrence_evidence"].update(numerator=None, denominator=None, value=None, status="insufficient_evidence")
        self.assertEqual(checker.validate_report(self.report)["status"], "passed")

    def test_unknown_evidence_inventory_needs_explicit_warning(self):
        result = self.result(0)
        self.unavailable(result)
        result["evidence_missing"]["delivery_evidence"].update(count=None, reason_counts=None)
        result["coverage"]["usable_occurrence_evidence"].update(numerator=None, value=None, status="insufficient_evidence")
        self.rejected()
        result["warnings"].append({"code": "EVIDENCE_INVENTORY_UNKNOWN", "message": "Delivery inventory was not established."})
        self.assertEqual(checker.validate_report(self.report)["status"], "passed")

    def test_latency_requires_p95_nearest_rank_and_matching_sample_count(self):
        original = copy.deepcopy(self.report)
        for key, value in (("quantile", 0.5), ("quantile_method", "linear"), ("sample_count", 4), ("unit", "s")):
            with self.subTest(key=key):
                self.report = copy.deepcopy(original)
                self.result(3)[key] = value
                self.rejected()

    def test_latency_gap_cannot_overlap_valid_measurements(self):
        result = self.result(3)
        result["counts"]["missing_latency"] = 2
        result["evidence_missing"]["latency_measurements"].update(count=2, reason_counts={"missing_required_value": 2})
        self.rejected()

    def test_missing_latency_cannot_be_hidden_by_a_zero_evidence_gap(self):
        result = self.result(3)
        result["evidence_missing"]["latency_measurements"].update(count=0, reason_counts={})
        self.rejected()
        result["counts"]["missing_latency"] = 0
        result["evidence_missing"]["latency_measurements"].update(count=1, reason_counts={"missing_required_value": 1})
        self.rejected()

    def test_latency_missing_count_includes_each_primary_evidence_reason(self):
        result = self.result(3)
        for reason in sorted(checker.GAP_REASONS):
            with self.subTest(reason=reason):
                result["evidence_missing"]["latency_measurements"]["reason_counts"] = {reason: 1}
                self.assertEqual(checker.validate_report(self.report)["status"], "passed")

    def test_unknown_latency_inventory_cannot_claim_an_exact_missing_count(self):
        result = self.result(3)
        result["evidence_missing"]["latency_measurements"].update(count=None, reason_counts=None)
        result["warnings"].append({"code": "EVIDENCE_INVENTORY_UNKNOWN", "message": "Missing timing inventory is unknown."})
        self.rejected()
        result["counts"]["missing_latency"] = None
        self.assertEqual(checker.validate_report(self.report)["status"], "passed")

    def test_nonempty_latency_population_without_samples_is_insufficient(self):
        result = self.result(3)
        self.unavailable(result, "not_applicable")
        result["sample_count"] = 0
        result["counts"].update(measured=0, missing_latency=4)
        result["evidence_missing"]["latency_measurements"].update(count=4, reason_counts={"missing_required_value": 4})
        result["coverage"]["latency_measurements"].update(numerator=0, value=0.0)
        self.rejected()
        self.unavailable(result, "insufficient_evidence")
        self.assertEqual(checker.validate_report(self.report)["status"], "passed")

    def test_empty_latency_population_can_be_not_applicable(self):
        result = self.result(3)
        self.unavailable(result, "not_applicable")
        result["sample_count"] = 0
        result["counts"].update(eligible_timing_targets=0, measured=0, missing_latency=0)
        result["evidence_missing"]["latency_measurements"].update(count=0, reason_counts={})
        result["coverage"]["latency_measurements"].update(numerator=0, denominator=0, value=None, status="not_applicable")
        self.assertEqual(checker.validate_report(self.report)["status"], "passed")

    def test_unknown_latency_population_cannot_be_not_applicable(self):
        result = self.result(3)
        self.unavailable(result, "not_applicable")
        result["warnings"].append({"code": "EVIDENCE_INVENTORY_UNKNOWN", "message": "Timing population was not established."})
        result["sample_count"] = 0
        result["counts"].update(eligible_timing_targets=None, measured=0, missing_latency=None)
        result["evidence_missing"]["latency_measurements"].update(count=None, reason_counts=None)
        result["coverage"]["latency_measurements"].update(numerator=0, denominator=None, value=None, status="insufficient_evidence")
        self.rejected()

    def test_unexecuted_latency_measurement_remains_not_measured(self):
        result = self.result(3)
        self.unavailable(result, "not_measured")
        result["sample_count"] = None
        result["counts"].update(measured=None, missing_latency=None, timed_out=None, early_render_violations=None)
        result["evidence_missing"]["latency_measurements"].update(count=None, reason_counts=None)
        result["coverage"]["latency_measurements"].update(numerator=None, value=None, status="insufficient_evidence")
        self.assertEqual(checker.validate_report(self.report)["status"], "passed")

    def test_duplicate_metric_cohort_and_version_is_rejected(self):
        self.report["results"].append(copy.deepcopy(self.report["results"][0]))
        self.rejected()

    def test_unknown_metric_is_rejected(self):
        self.result(0)["metric_id"] = "delivery.new_undefined_metric"
        self.rejected()

    def test_undeclared_result_fields_are_rejected(self):
        self.result(0)["missing_evidence_total"] = 0
        self.rejected()

    def test_experience_evidence_gaps_cannot_exceed_population(self):
        self.result(1)["evidence_missing"]["experience_records"].update(count=7, reason_counts={"invalid_record": 7})
        self.rejected()

    def test_formal_context_requires_frozen_dataset_and_cohort_reference(self):
        self.report.update(example_only=False, evidence_type="native_scenario")
        self.rejected()
        self.report.update(dataset_version="fixture-v1", cohort_manifest_ref="fixture-manifest.json")
        self.assertEqual(checker.validate_report(self.report)["status"], "passed")

    def test_synthetic_replay_cannot_claim_native_cost(self):
        self.result(2)
        self.report.update(example_only=False, evidence_type="synthetic_replay", dataset_version="fixture-v1", cohort_manifest_ref="fixture.json")
        self.rejected()

    def test_examples_cannot_claim_acceptance(self):
        self.result(0)["assessment"] = "passed"
        self.rejected()

    def test_cutoff_requires_timezone(self):
        self.report["as_of"] = "2026-11-16T05:00:00"
        self.rejected()

    def test_strict_json_rejects_duplicate_keys_and_nonfinite_literals(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            for content in ('{"status":1,"status":2}', '{"value":NaN}', '{"value":Infinity}'):
                with self.subTest(content=content):
                    path.write_text(content, encoding="ascii")
                    with self.assertRaises(ValueError):
                        checker.load_report(path)

    def test_cli_returns_failure_exit_code_and_json_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            self.result(0)["value"] = 0.75
            path.write_text(json.dumps(self.report), encoding="ascii")
            completed = subprocess.run([sys.executable, str(SCRIPT), str(path)], capture_output=True, text=True)
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(json.loads(completed.stdout)["status"], "failed")
            self.assertEqual(completed.stderr, "")

    def test_cli_accepts_valid_contract_with_failed_acceptance_assessment(self):
        self.result(0)["assessment"] = "failed"
        self.report.update(example_only=False, evidence_type="native_scenario",
                           dataset_version="fixture-v1", cohort_manifest_ref="fixture-manifest.json")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "valid-contract-failed-assessment.json"
            path.write_text(json.dumps(self.report), encoding="ascii")
            completed = subprocess.run([sys.executable, str(SCRIPT), str(path)], capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["status"], "passed")
            self.assertEqual(summary["metric_status_counts"], {"computed": 1})
            self.assertEqual(summary["source_evidence_verification"], "not_performed")
            self.assertEqual(checker.load_report(path)["results"][0]["assessment"], "failed")
            self.assertEqual(completed.stderr, "")


if __name__ == "__main__":
    unittest.main()
