"""Calculate cost per verified displayed occurrence from a recorded M0 replay.

Real model/API cost requires actual provider call records. In synthetic replay
there are no real provider calls, so real_model_cost is not_measured and the
primary metric value is null. The calculator still counts verified displayed
occurrences (the denominator) and audits the receipt linkage.

Any simulated cost diagnostic must use a separately named metric and explicit
evidence source; it cannot claim native cost.
"""

import argparse
import json
from collections import Counter
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from d_check_metric_report import load_report, reject_constant, unique_object, validate_report
from d_run_evidence import MANIFEST_NAME, check_run_manifest, file_sha256
from d_summarize_suite import safe_scenario_path, summarize_suite


ROOT = Path(__file__).resolve().parents[1]
PROFILE = "m0-cost-per-display-v1"
KINDS = {"task.created", "task.confirmed", "task.edited", "task.snoozed", "task.cancelled", "task.completed"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def instant(value):
    require(isinstance(value, str), "Timestamp must be an ISO string")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(result.utcoffset() is not None, "Timestamp requires a UTC offset")
    return result


def identity(view):
    require(isinstance(view["task_id"], str) and bool(view["task_id"]), "Invalid task ID")
    for key in ("occurrence", "version"):
        require(type(view[key]) is int and view[key] > 0, "Invalid task occurrence/version")
    return view["task_id"], view["occurrence"]


def calculate_case(case, events):
    """Count verified displayed occurrences for one intact M0 case."""
    require(case["profile"] == "scenario" and case["clock"] == "virtual"
            and case["receipt_source"] == "simulated", "Only simulated M0 scenario cases are supported")
    require(events, "Empty trace")
    require(all(e["clock"] == "virtual" and e["simulated"] is True for e in events),
            "Mixed/native evidence is outside this calculator profile")
    times = [instant(e["ts"]) for e in events]
    require(times == sorted(times), "Trace wall time is not ordered")

    applied = {}
    for index, result in enumerate(case["outcomes"]["command_results"]):
        if result["status"] == "applied":
            key = result["command_id"]
            require(key not in applied, "Command was applied more than once")
            applied[key] = result

    states, versions, used = {}, {}, set()
    rows = {}
    for event in events:
        seq, kind = event["seq"], event["kind"]
        if kind not in KINDS:
            continue
        command_id = event["payload"]["command_id"]
        require(command_id in applied and command_id not in used, "Task event has no unique applied result")
        used.add(command_id)
        view = applied[command_id]["view"]
        key = identity(view)
        task, occurrence = key
        previous = states.get(task)
        if previous:
            old_key = identity(previous)
            if old_key in rows:
                rows[old_key]["closure_seq"] = seq
        states[task] = view
        versions[(task, view["version"])] = {"view": view, "start_seq": seq, "end_seq": len(events) + 1}
        if view["status"] == "registered":
            if key not in rows:
                rows[key] = {
                    "run_id": events[0]["run_id"], "scenario_id": case["scenario_id"],
                    "task_id": task, "occurrence": occurrence,
                    "mode": view["draft"]["mode"], "displayed": False, "displayed_at": None,
                }
    require(used == set(applied), "An applied command has no lifecycle event")

    snapshots = []
    for event in events:
        if event["kind"] == "task.confirmed":
            view = applied[event["payload"]["command_id"]]["view"]
            snapshots.append((event["seq"], view))
    for task, final in case["outcomes"]["tasks"].items():
        snapshots.append((len(events) + 1, final))

    for event in events:
        if event["kind"] != "delivery.displayed":
            continue
        version = versions.get((event["task_id"], event["task_version"]))
        require(version is not None and version["view"]["status"] == "registered",
                "Delivery uses an inactive task version")
        key = identity(version["view"])
        require(event["source"] == "simulated", "Native/offscreen receipt is outside this profile")
        if key in rows and not rows[key]["displayed"]:
            receipt_seq = event["seq"]
            candidates = [view for seq, view in snapshots if seq > receipt_seq
                          and identity(view) == key and view.get("displayed_at") is not None]
            render_values = {instant(v["displayed_at"]) for v in candidates}
            if len(render_values) == 1:
                rows[key]["displayed"] = True
                rows[key]["displayed_at"] = render_values.pop().isoformat()
    return list(rows.values())


def metric_record(rows, cohort_id):
    displayed = [r for r in rows if r["displayed"]]
    n = len(rows)
    verified = len(displayed)
    result = {
        "metric_id": "cost.per_verified_displayed_occurrence", "definition_version": "0.4",
        "cohort_id": cohort_id, "metric_type": "cost", "status": "not_measured", "assessment": "not_evaluated",
        "value_scope": "not_available",
        "value": None,
        "unit": "currency_per_verified_displayed_occurrence", "currency": "USD",
        "numerator": None, "denominator": verified if verified else None,
        "numerator_unit": "currency", "denominator_unit": "verified_displayed_occurrence",
        "statistic": "scalar", "quantile": None, "quantile_method": None, "sample_count": None,
        "counts": {
            "input_requests": None, "verified_displayed_occurrences": verified if verified else None,
            "late_displays": 0, "user_completed_tasks": 0,
        },
        "excluded": {},
        "evidence_missing": {
            "cost_accounting": {"unit": "call", "count": None, "reason_counts": None},
            "delivery_evidence": {"unit": "occurrence", "count": None, "reason_counts": None}},
        "not_invited_reason_counts": None,
        "coverage": {
            "accounted_calls": {"unit": "call", "numerator": None, "denominator": None,
                "value": None, "status": "insufficient_evidence"},
            "occurrence_outcomes": {"unit": "occurrence", "numerator": n if n else None,
                "denominator": n if n else None,
                "value": 1.0 if n else None, "status": "computed" if n else "not_applicable"},
        },
        "warnings": [{"code": "REAL_MODEL_COST_NOT_MEASURED",
                      "message": "Synthetic replay has no real provider call records; cost is not_measured."}],
    }
    return result


def calculate_cost_report(suite_path):
    suite_path = Path(suite_path).resolve()
    manifest_path = suite_path / MANIFEST_NAME
    before = file_sha256(manifest_path)
    summary = summarize_suite(suite_path)
    require(summary["status"] == "passed", "Replay/evidence validation failed; no metric was published")
    require(summary["run_provenance"]["status"] == "verified", "A verified recorded-run manifest is required")
    manifest = load_report(manifest_path)
    suite = load_report(suite_path / "report.json")
    rows = []
    for case in suite["cases"]:
        name = case["scenario_id"]
        saved_case = load_report(safe_scenario_path(suite_path, name, "report.json"))
        require(saved_case == case, "Suite and per-case reports disagree")
        trace = safe_scenario_path(suite_path, name, "trace.jsonl").read_text(encoding="utf-8-sig")
        events = [json.loads(line, parse_constant=reject_constant, object_pairs_hook=unique_object)
                  for line in trace.splitlines() if line.strip()]
        rows.extend(calculate_case(case, events))
    verification = check_run_manifest(suite_path, set(manifest["scenarios"]))
    require(verification["status"] == "verified" and file_sha256(manifest_path) == before,
            "Saved replay evidence changed during analysis")
    cohort_id = "verified-displayed:" + before[:16]
    report = {
        "result_schema_version": "0.4", "experiment_id": manifest["run_id"],
        "configuration_id": "recorded-m0:" + manifest["input_sha256"],
        "dataset_version": "saved-scenarios:" + manifest["input_sha256"],
        "cohort_manifest_ref": "cohort.json", "evidence_type": "synthetic_replay", "example_only": False,
        "as_of": manifest["finished_at"], "results": [metric_record(rows, cohort_id)],
    }
    validate_report(report)
    audit = {
        "schema_version": 1, "analysis_profile": PROFILE, "definition_version": "0.4",
        "cohort_id": cohort_id, "source_run_directory": str(suite_path),
        "source_manifest_sha256": before, "source_run_provenance": verification,
        "source_artifact_sha256": manifest["artifacts"], "scenario_count": len(suite["cases"]),
        "evidence_type": "synthetic_replay", "clock": "virtual", "receipt_source": "simulated",
        "cohort_rule": "All accepted registered occurrences (hard and soft) with verified display receipts.",
        "outside_cohort": "Unconfirmed drafts are outside the cost denominator.",
        "real_latency": "not_measured", "real_model_cost": "not_measured", "human_ratings": "not_measured",
        "rows": rows,
    }
    return report, audit


def write_analysis(suite, output):
    suite, output = Path(suite).resolve(), Path(output).resolve()
    require(not output.exists(), "Use a new analysis output directory")
    require(not output.is_relative_to(suite) and not suite.is_relative_to(output),
            "Analysis output and source run must be separate sibling locations")
    names = ["scripts/d_calculate_cost.py", "scripts/d_check_metric_report.py",
             "scripts/d_count_deliveries.py", "scripts/d_summarize_suite.py", "scripts/d_run_evidence.py",
             "docs/evaluation/metrics-spec.md"]
    inputs = {name: (ROOT / name).read_bytes() for name in names}
    report, audit = calculate_cost_report(suite)
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
                     "evidence_type": report["evidence_type"], "metric": report["results"][0],
                     "report": str(args.output / "metric-report.json")}, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
