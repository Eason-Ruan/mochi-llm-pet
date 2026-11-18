"""Calculate state-estimation metrics from independent state labels.

Metrics:
  - interruption.soft_opportunity_conflict_rate: opportunity displays whose
    independent labels are working/away, divided by such displays with known
    working/available/away labels.
  - estimation.base_estimate_accuracy: correct base estimates divided by
    deduplicated samples with independent working/available/away labels.

Both require independent human state labels and base-estimate predictions that
are not produced by synthetic M0 replay, so they are not_measured here.
"""

import argparse
import json
from hashlib import sha256
from pathlib import Path

from d_check_metric_report import load_report, reject_constant, unique_object, validate_report
from d_run_evidence import MANIFEST_NAME, check_run_manifest, file_sha256
from d_summarize_suite import safe_scenario_path, summarize_suite


ROOT = Path(__file__).resolve().parents[1]
PROFILE = "m0-state-estimation-v1"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def not_measured_ratio(metric_id, cohort_id, domain_unit, counts, coverage_key):
    return {
        "metric_id": metric_id, "definition_version": "0.4",
        "cohort_id": cohort_id, "metric_type": "ratio", "status": "not_measured",
        "assessment": "not_evaluated", "value_scope": "not_available",
        "value": None, "unit": "fraction", "currency": None,
        "numerator": None, "denominator": None,
        "numerator_unit": domain_unit, "denominator_unit": domain_unit,
        "statistic": "scalar", "quantile": None, "quantile_method": None, "sample_count": None,
        "counts": counts,
        "excluded": {},
        "evidence_missing": {
            "state_evidence": {"unit": domain_unit, "count": None, "reason_counts": None}},
        "not_invited_reason_counts": None,
        "coverage": {
            coverage_key: {"unit": domain_unit, "numerator": None, "denominator": None,
                "value": None, "status": "insufficient_evidence"}},
        "warnings": [
            {"code": "STATE_LABELS_NOT_MEASURED",
             "message": f"No independent state labels or base-estimate predictions in synthetic replay; {metric_id} is not_measured."},
        ],
    }


def calculate_state_estimation_report(suite_path):
    suite_path = Path(suite_path).resolve()
    manifest_path = suite_path / MANIFEST_NAME
    before = file_sha256(manifest_path)
    summary = summarize_suite(suite_path)
    require(summary["status"] == "passed", "Replay/evidence validation failed; no metric was published")
    require(summary["run_provenance"]["status"] == "verified", "A verified recorded-run manifest is required")
    manifest = load_report(manifest_path)
    suite = load_report(suite_path / "report.json")
    verification = check_run_manifest(suite_path, set(manifest["scenarios"]))
    require(verification["status"] == "verified" and file_sha256(manifest_path) == before,
            "Saved replay evidence changed during analysis")
    cohort_id = "state-estimation:" + before[:16]
    results = [
        not_measured_ratio("interruption.soft_opportunity_conflict_rate", cohort_id, "display",
                           {"eligible_displays": None, "conflict_displays": None}, "labeled_displays"),
        not_measured_ratio("estimation.base_estimate_accuracy", cohort_id, "sample",
                           {"eligible_samples": None, "correct_samples": None}, "labeled_samples"),
    ]
    report = {
        "result_schema_version": "0.4", "experiment_id": manifest["run_id"],
        "configuration_id": "recorded-m0:" + manifest["input_sha256"],
        "dataset_version": "saved-scenarios:" + manifest["input_sha256"],
        "cohort_manifest_ref": "cohort.json", "evidence_type": "synthetic_replay", "example_only": False,
        "as_of": manifest["finished_at"], "results": results,
    }
    validate_report(report)
    audit = {
        "schema_version": 1, "analysis_profile": PROFILE, "definition_version": "0.4",
        "cohort_id": cohort_id, "source_run_directory": str(suite_path),
        "source_manifest_sha256": before, "source_run_provenance": verification,
        "source_artifact_sha256": manifest["artifacts"], "scenario_count": len(suite["cases"]),
        "evidence_type": "synthetic_replay", "clock": "virtual", "receipt_source": "simulated",
        "state_labels": "not_measured", "base_estimates": "not_measured",
    }
    return report, audit


def write_analysis(suite, output):
    suite, output = Path(suite).resolve(), Path(output).resolve()
    require(not output.exists(), "Use a new analysis output directory")
    require(not output.is_relative_to(suite) and not suite.is_relative_to(output),
            "Analysis output and source run must be separate sibling locations")
    names = ["scripts/d_calculate_state_estimation.py", "scripts/d_check_metric_report.py",
             "scripts/d_count_deliveries.py", "scripts/d_summarize_suite.py", "scripts/d_run_evidence.py",
             "docs/evaluation/metrics-spec.md"]
    inputs = {name: (ROOT / name).read_bytes() for name in names}
    report, audit = calculate_state_estimation_report(suite)
    require(all((ROOT / name).read_bytes() == raw for name, raw in inputs.items()),
            "Analysis implementation or metric definition changed during calculation")
    audit["analysis_code_sha256"] = {name: sha256(raw).hexdigest() for name, raw in inputs.items()}
    output.mkdir(parents=True, exist_ok=False)
    for name, raw in inputs.items():
        path = output / "analysis-inputs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    for name, obj in (("cohort.json", audit), ("metric-report.json", report)):
        (output / name).write_text(json.dumps(obj, ensure_ascii=True, allow_nan=False, indent=2) + "\n", encoding="ascii")
    artifacts = {name: file_sha256(output / name) for name in ("cohort.json", "metric-report.json")}
    (output / "analysis-manifest.json").write_text(json.dumps({
        "schema_version": 1, "analysis_profile": PROFILE, "source_manifest_sha256": audit["source_manifest_sha256"],
        "analysis_input_sha256": audit["analysis_code_sha256"], "artifacts": artifacts,
    }, indent=2) + "\n", encoding="ascii")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = write_analysis(args.suite, args.output)
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as error:
        print(json.dumps({"status": "failed", "analysis_profile": PROFILE, "error": str(error)}, ensure_ascii=True, indent=2))
        raise SystemExit(1)
    print(json.dumps({"status": "completed", "analysis_profile": PROFILE,
                     "evidence_type": report["evidence_type"],
                     "metrics": [r["metric_id"] for r in report["results"]],
                     "report": str(args.output / "metric-report.json")}, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
