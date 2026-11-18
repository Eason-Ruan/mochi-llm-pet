"""Calculate delivery violation counts from a recorded M0 replay.

Metrics:
  - delivery.post_cancellation_display_violations: successful displays that
    arrive after the owning task version was cancelled. Computed from replay.
  - delivery.duplicate_render_violations: multiple official native cards for one
    delivery. not_measured in synthetic replay because C's card/render evidence
    is not available.
"""

import argparse
import json
from hashlib import sha256
from pathlib import Path

from d_check_metric_report import load_report, reject_constant, unique_object, validate_report
from d_run_evidence import MANIFEST_NAME, check_run_manifest, file_sha256
from d_summarize_suite import safe_scenario_path, summarize_suite


ROOT = Path(__file__).resolve().parents[1]
PROFILE = "m0-delivery-violations-v1"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def detect_post_cancellation_violations(suite):
    """Return (violation_count, cancelled_count, violation_rows) from a recorded suite."""
    violations = []
    cancelled_count = 0
    for case in suite["cases"]:
        name = case["scenario_id"]
        trace = safe_scenario_path(suite["dir"], name, "trace.jsonl")
        events = [json.loads(line, parse_constant=reject_constant, object_pairs_hook=unique_object)
                  for line in trace.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
        cancellations = {}
        for e in events:
            if e["kind"] == "task.cancelled":
                key = (e["task_id"], e["task_version"])
                cancellations[key] = e["seq"]
                cancelled_count += 1
        for e in events:
            if e["kind"] != "delivery.displayed":
                continue
            task_id = e.get("task_id")
            version = e.get("task_version")
            if task_id is None or version is None:
                continue
            for (c_task, c_version), c_seq in cancellations.items():
                if c_task == task_id and version <= c_version and e["seq"] > c_seq:
                    violations.append({
                        "scenario_id": name, "task_id": task_id, "task_version": version,
                        "cancelled_version": c_version, "display_seq": e["seq"],
                        "cancel_seq": c_seq, "delivery_id": e.get("delivery_id"),
                    })
    return len(violations), cancelled_count, violations


def post_cancellation_result(cohort_id, violation_count, cancelled_count, violations):
    return {
        "metric_id": "delivery.post_cancellation_display_violations", "definition_version": "0.4",
        "cohort_id": cohort_id, "metric_type": "count", "status": "computed",
        "assessment": "not_evaluated", "value_scope": "eligible_cohort",
        "value": violation_count, "unit": "violation", "currency": None,
        "numerator": violation_count, "denominator": None,
        "numerator_unit": "violation", "denominator_unit": None,
        "statistic": "scalar", "quantile": None, "quantile_method": None, "sample_count": None,
        "counts": {"violations": violation_count, "cancelled_tasks": cancelled_count,
                   "post_cancellation_displays": violation_count},
        "excluded": {},
        "evidence_missing": {
            "delivery_evidence": {"unit": "occurrence", "count": 0, "reason_counts": {}}},
        "not_invited_reason_counts": None,
        "coverage": {
            "cancelled_tasks": {"unit": "cancelled_task", "numerator": cancelled_count, "denominator": cancelled_count,
                "value": 1.0 if cancelled_count > 0 else None,
                "status": "computed" if cancelled_count > 0 else "not_applicable"}},
        "warnings": [
            {"code": "SYNTHETIC_REPLAY_SCOPE",
             "message": "Violations are detected from simulated M0 replay events only; native render evidence is not verified."},
        ] if violation_count == 0 else [
            {"code": "POST_CANCELLATION_DISPLAY_DETECTED",
             "message": f"{violation_count} post-cancellation display(s) detected in synthetic replay."},
        ],
    }


def duplicate_render_result(cohort_id):
    return {
        "metric_id": "delivery.duplicate_render_violations", "definition_version": "0.4",
        "cohort_id": cohort_id, "metric_type": "count", "status": "not_measured",
        "assessment": "not_evaluated", "value_scope": "not_available",
        "value": None, "unit": "violation", "currency": None,
        "numerator": None, "denominator": None,
        "numerator_unit": "violation", "denominator_unit": None,
        "statistic": "scalar", "quantile": None, "quantile_method": None, "sample_count": None,
        "counts": {"violations": None},
        "excluded": {},
        "evidence_missing": {
            "delivery_evidence": {"unit": "occurrence", "count": None, "reason_counts": None}},
        "not_invited_reason_counts": None,
        "coverage": {
            "delivery_evidence": {"unit": "occurrence", "numerator": None, "denominator": None,
                "value": None, "status": "insufficient_evidence"}},
        "warnings": [
            {"code": "CARD_RENDER_EVIDENCE_NOT_MEASURED",
             "message": "Synthetic replay has no C-side native card/render evidence; duplicate-render violations are not_measured."},
        ],
    }


def calculate_violations_report(suite_path):
    suite_path = Path(suite_path).resolve()
    manifest_path = suite_path / MANIFEST_NAME
    before = file_sha256(manifest_path)
    summary = summarize_suite(suite_path)
    require(summary["status"] == "passed", "Replay/evidence validation failed; no metric was published")
    require(summary["run_provenance"]["status"] == "verified", "A verified recorded-run manifest is required")
    manifest = load_report(manifest_path)
    suite = load_report(suite_path / "report.json")
    suite["dir"] = suite_path
    violation_count, cancelled_count, violations = detect_post_cancellation_violations(suite)
    verification = check_run_manifest(suite_path, set(manifest["scenarios"]))
    require(verification["status"] == "verified" and file_sha256(manifest_path) == before,
            "Saved replay evidence changed during analysis")
    cohort_id = "delivery-violations:" + before[:16]
    report = {
        "result_schema_version": "0.4", "experiment_id": manifest["run_id"],
        "configuration_id": "recorded-m0:" + manifest["input_sha256"],
        "dataset_version": "saved-scenarios:" + manifest["input_sha256"],
        "cohort_manifest_ref": "cohort.json", "evidence_type": "synthetic_replay", "example_only": False,
        "as_of": manifest["finished_at"],
        "results": [post_cancellation_result(cohort_id, violation_count, cancelled_count, violations),
                    duplicate_render_result(cohort_id)],
    }
    validate_report(report)
    audit = {
        "schema_version": 1, "analysis_profile": PROFILE, "definition_version": "0.4",
        "cohort_id": cohort_id, "source_run_directory": str(suite_path),
        "source_manifest_sha256": before, "source_run_provenance": verification,
        "source_artifact_sha256": manifest["artifacts"], "scenario_count": len(suite["cases"]),
        "evidence_type": "synthetic_replay", "clock": "virtual", "receipt_source": "simulated",
        "post_cancellation_violations": violations,
        "duplicate_render_evidence": "not_measured",
    }
    return report, audit


def write_analysis(suite, output):
    suite, output = Path(suite).resolve(), Path(output).resolve()
    require(not output.exists(), "Use a new analysis output directory")
    require(not output.is_relative_to(suite) and not suite.is_relative_to(output),
            "Analysis output and source run must be separate sibling locations")
    names = ["scripts/d_calculate_delivery_violations.py", "scripts/d_check_metric_report.py",
             "scripts/d_count_deliveries.py", "scripts/d_summarize_suite.py", "scripts/d_run_evidence.py",
             "docs/evaluation/metrics-spec.md"]
    inputs = {name: (ROOT / name).read_bytes() for name in names}
    report, audit = calculate_violations_report(suite)
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
