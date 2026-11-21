"""Calculate work-session outcome-confirmation coverage.

Metric: work.outcome_confirmation_coverage
  Unique ended work sessions with a valid explicit user outcome confirmation
  divided by unique ended work sessions predeclared to require and support the
  review protocol.

Real outcome-confirmation evidence requires C's accepted end-state views and
explicit user review records. Synthetic M0 replay does not produce these, so
the metric is not_measured here.
"""

import argparse
import json
from hashlib import sha256
from pathlib import Path

from d_check_metric_report import load_report, reject_constant, unique_object, validate_report
from d_run_evidence import MANIFEST_NAME, check_run_manifest, file_sha256
from d_summarize_suite import safe_scenario_path, summarize_suite


ROOT = Path(__file__).resolve().parents[1]
PROFILE = "m0-work-outcomes-v1"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def calculate_work_outcomes_report(suite_path):
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
    cohort_id = "work-outcomes:" + before[:16]
    result = {
        "metric_id": "work.outcome_confirmation_coverage", "definition_version": "0.4",
        "cohort_id": cohort_id, "metric_type": "ratio", "status": "not_measured",
        "assessment": "not_evaluated", "value_scope": "not_available",
        "value": None, "unit": "fraction", "currency": None,
        "numerator": None, "denominator": None,
        "numerator_unit": "session", "denominator_unit": "session",
        "statistic": "scalar", "quantile": None, "quantile_method": None, "sample_count": None,
        "counts": {"eligible_sessions": None, "confirmed_sessions": None},
        "excluded": {},
        "evidence_missing": {
            "outcome_evidence": {"unit": "session", "count": None, "reason_counts": None}},
        "not_invited_reason_counts": None,
        "coverage": {
            "review_records": {"unit": "session", "numerator": None, "denominator": None,
                "value": None, "status": "insufficient_evidence"}},
        "warnings": [
            {"code": "OUTCOME_RECORDS_NOT_MEASURED",
             "message": "Synthetic replay has no accepted end-state views or explicit user review records; outcome coverage is not_measured."},
        ],
    }
    report = {
        "result_schema_version": "0.4", "experiment_id": manifest["run_id"],
        "configuration_id": "recorded-m0:" + manifest["input_sha256"],
        "dataset_version": "saved-scenarios:" + manifest["input_sha256"],
        "cohort_manifest_ref": "cohort.json", "evidence_type": "synthetic_replay", "example_only": False,
        "as_of": manifest["finished_at"], "results": [result],
    }
    validate_report(report)
    audit = {
        "schema_version": 1, "analysis_profile": PROFILE, "definition_version": "0.4",
        "cohort_id": cohort_id, "source_run_directory": str(suite_path),
        "source_manifest_sha256": before, "source_run_provenance": verification,
        "source_artifact_sha256": manifest["artifacts"], "scenario_count": len(suite["cases"]),
        "evidence_type": "synthetic_replay", "clock": "virtual", "receipt_source": "simulated",
        "outcome_review_records": "not_measured",
    }
    return report, audit


def write_analysis(suite, output):
    suite, output = Path(suite).resolve(), Path(output).resolve()
    require(not output.exists(), "Use a new analysis output directory")
    require(not output.is_relative_to(suite) and not suite.is_relative_to(output),
            "Analysis output and source run must be separate sibling locations")
    names = ["scripts/d_calculate_work_outcomes.py", "scripts/d_check_metric_report.py",
             "scripts/d_count_deliveries.py", "scripts/d_summarize_suite.py", "scripts/d_run_evidence.py",
             "docs/evaluation/metrics-spec.md"]
    inputs = {name: (ROOT / name).read_bytes() for name in names}
    report, audit = calculate_work_outcomes_report(suite)
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
