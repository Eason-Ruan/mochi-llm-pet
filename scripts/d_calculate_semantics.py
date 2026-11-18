"""Calculate request-semantics metrics from annotated reminder samples.

Metrics:
  - semantics.strict_interpretation_accuracy
  - semantics.field_accuracy
  - semantics.clarification_behavior_accuracy

These compare the first model prediction against the frozen adjudicated gold
label. Without a recorded first-prediction manifest the metrics are not_measured;
the calculator still audits the annotated dataset and reports the eligible
request inventory.
"""

import argparse
import json
from hashlib import sha256
from pathlib import Path

from d_check_metric_report import load_report, reject_constant, unique_object, validate_report
from d_check_reminder_dataset import validate_dataset


ROOT = Path(__file__).resolve().parents[1]
PROFILE = "m0-request-semantics-v1"
METRIC_IDS = (
    "semantics.strict_interpretation_accuracy",
    "semantics.field_accuracy",
    "semantics.clarification_behavior_accuracy",
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def field_list(gold):
    """Return the ordered list of assessable gold fields for a parse sample."""
    if gold is None:
        return []
    fields = ["intent", "expected_behavior"]
    if isinstance(gold, dict):
        if "content" in gold:
            fields.append("gold.content")
        if "mode" in gold:
            fields.append("gold.mode")
        if "timezone" in gold:
            fields.append("gold.timezone")
        if "earliest_at" in gold:
            fields.append("gold.earliest_at")
        if "latest_first_delivery_at" in gold:
            fields.append("gold.latest_first_delivery_at")
        if "task_id" in gold:
            fields.append("gold.task_id")
        if "expected_version" in gold:
            fields.append("gold.expected_version")
    return fields


def not_measured_result(metric_id, cohort_id, eligible_requests):
    return {
        "metric_id": metric_id, "definition_version": "0.4",
        "cohort_id": cohort_id, "metric_type": "ratio", "status": "not_measured",
        "assessment": "not_evaluated", "value_scope": "not_available",
        "value": None, "unit": "fraction", "currency": None,
        "numerator": None, "denominator": None,
        "numerator_unit": "request", "denominator_unit": "request",
        "statistic": "scalar", "quantile": None, "quantile_method": None, "sample_count": None,
        "counts": {"eligible_requests": eligible_requests, "assessed_requests": None,
                   "correct_requests": None, "incorrect_requests": None},
        "excluded": {},
        "evidence_missing": {
            "interpretation_evidence": {"unit": "request", "count": None, "reason_counts": None}},
        "not_invited_reason_counts": None,
        "coverage": {
            "prediction_records": {"unit": "request", "numerator": None, "denominator": eligible_requests,
                "value": None, "status": "insufficient_evidence"}},
        "warnings": [
            {"code": "FIRST_PREDICTIONS_NOT_MEASURED",
             "message": "No recorded first-prediction manifest was supplied; semantics accuracy is not_measured."},
        ],
    }


def calculate_semantics_report(dataset_path, predictions_path=None):
    dataset_path = Path(dataset_path).resolve()
    data = json.loads(dataset_path.read_text(encoding="utf-8-sig"),
                      parse_constant=reject_constant, object_pairs_hook=unique_object)
    summary = validate_dataset(data)
    require(summary["status"] == "passed", "Dataset structure check failed")
    samples = data["samples"]
    eligible = [s for s in samples if s["annotation_status"] == "double_annotated"]
    cohort_id = "request-semantics:" + sha256(
        dataset_path.read_bytes()).hexdigest()[:16]
    results = [not_measured_result(mid, cohort_id, len(eligible)) for mid in METRIC_IDS]
    report = {
        "result_schema_version": "0.4",
        "experiment_id": "reminder-semantics-pilot",
        "configuration_id": "annotated-dataset:" + data["dataset_version"],
        "dataset_version": data["dataset_version"],
        "cohort_manifest_ref": str(dataset_path.name),
        "evidence_type": "synthetic_replay", "example_only": False,
        "as_of": max(((s.get("annotator_a") or {}).get("annotated_at", "1970-01-01T00:00:00+00:00")
                      for s in samples), default="1970-01-01T00:00:00+00:00"),
        "results": results,
    }
    validate_report(report)
    audit = {
        "schema_version": 1, "analysis_profile": PROFILE, "definition_version": "0.4",
        "cohort_id": cohort_id, "source_dataset": str(dataset_path),
        "dataset_version": data["dataset_version"], "sample_count": len(samples),
        "eligible_request_count": len(eligible),
        "annotation_status": summary["annotation_status"],
        "double_annotated_samples": summary["double_annotated_samples"],
        "predictions_path": str(predictions_path) if predictions_path else None,
        "predictions_supplied": predictions_path is not None,
        "real_model_predictions": "not_measured",
        "rows": [
            {"request_sample_id": s["request_sample_id"], "intent": s["intent"],
             "expected_behavior": s["expected_behavior"],
             "annotation_status": s["annotation_status"],
             "assessable_fields": field_list((s.get("annotator_a") or {}).get("gold") or s.get("gold"))}
            for s in eligible
        ],
    }
    return report, audit


def write_analysis(dataset, output, predictions=None):
    dataset, output = Path(dataset).resolve(), Path(output).resolve()
    predictions = Path(predictions).resolve() if predictions else None
    require(not output.exists(), "Use a new analysis output directory")
    names = ["scripts/d_calculate_semantics.py", "scripts/d_check_metric_report.py",
             "scripts/d_check_reminder_dataset.py", "docs/evaluation/metrics-spec.md"]
    inputs = {name: (ROOT / name).read_bytes() for name in names}
    report, audit = calculate_semantics_report(dataset, predictions)
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
    artifacts = {name: sha256((output / name).read_bytes()).hexdigest() for name in ("cohort.json", "metric-report.json")}
    (output / "analysis-manifest.json").write_text(json.dumps({
        "schema_version": 1, "analysis_profile": PROFILE,
        "source_dataset_sha256": sha256(dataset.read_bytes()).hexdigest(),
        "analysis_input_sha256": audit["analysis_code_sha256"], "artifacts": artifacts,
    }, indent=2) + "\n", encoding="ascii")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--predictions", type=Path, default=None,
                        help="Optional JSON manifest of first model predictions keyed by request_sample_id")
    args = parser.parse_args()
    try:
        report = write_analysis(args.dataset, args.output, args.predictions)
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as error:
        print(json.dumps({"status": "failed", "analysis_profile": PROFILE, "error": str(error)}, ensure_ascii=True, indent=2))
        raise SystemExit(1)
    print(json.dumps({"status": "completed", "analysis_profile": PROFILE,
                     "evidence_type": report["evidence_type"],
                     "metrics": [r["metric_id"] for r in report["results"]],
                     "report": str(args.output / "metric-report.json")}, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
