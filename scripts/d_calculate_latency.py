"""Calculate hard-reminder due latency P95 from a recorded M0 replay.

A hard reminder's business window is a single due instant. Due latency is the
elapsed time from the scheduled due instant to the first valid native render.
Early renders (negative latency) are reported as separate violations and are
not folded into the nonnegative P95 sample.

This profile accepts only simulated M0 scenario evidence. A virtual clock does
not establish real hardware latency; results carry evidence_type=synthetic_replay.
"""

import argparse
import json
import math
from collections import Counter
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from d_check_metric_report import load_report, reject_constant, unique_object, validate_report
from d_run_evidence import MANIFEST_NAME, check_run_manifest, file_sha256
from d_summarize_suite import safe_scenario_path, summarize_suite


ROOT = Path(__file__).resolve().parents[1]
PROFILE = "m0-hard-due-latency-v1"
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


def is_hard(view):
    draft = view["draft"]
    return draft["mode"] == "hard"


def due_instant(view):
    draft = view["draft"]
    earliest, latest = (instant(draft[key]) for key in ("earliest_at", "latest_first_delivery_at"))
    require(earliest == latest, "Hard reminder must have identical earliest and latest times")
    return earliest


def calculate_case(case, events):
    """Return auditable hard-due latency rows for one intact M0 case."""
    require(case["profile"] == "scenario" and case["clock"] == "virtual"
            and case["receipt_source"] == "simulated", "Only simulated M0 scenario cases are supported")
    require(events, "Empty trace")
    require(all(e["clock"] == "virtual" and e["simulated"] is True for e in events),
            "Mixed/native evidence is outside this calculator profile")
    times = [instant(e["ts"]) for e in events]
    require(times == sorted(times), "Trace wall time is not ordered")
    cutoff = times[-1]

    applied = {}
    for index, result in enumerate(case["outcomes"]["command_results"]):
        if result["status"] == "applied":
            key = result["command_id"]
            require(key not in applied, "Command was applied more than once")
            applied[key] = result

    states, versions, used = {}, {}, set()
    pending_command = None
    rows = {}
    for event in events:
        seq, kind = event["seq"], event["kind"]
        if kind == "scenario.input":
            payload = event["payload"]
            pending_command = payload["payload"] if payload["kind"] == "command" else None
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
        if view["status"] == "registered" and is_hard(view):
            if key not in rows:
                rows[key] = {
                    "run_id": events[0]["run_id"], "scenario_id": case["scenario_id"],
                    "task_id": task, "occurrence": occurrence, "due_at": due_instant(view).isoformat(),
                    "first_rendered_at": None, "latency_ms": None, "status": None, "reason": None,
                }
    require(used == set(applied), "An applied command has no lifecycle event")

    snapshots = []
    for event in events:
        if event["kind"] == "task.confirmed":
            view = applied[event["payload"]["command_id"]]["view"]
            snapshots.append((event["seq"], view))
    for task, final in case["outcomes"]["tasks"].items():
        snapshots.append((len(events) + 1, final))

    requests = {}
    for event in events:
        if event["kind"] not in {"delivery.requested", "delivery.displayed"}:
            continue
        version = versions.get((event["task_id"], event["task_version"]))
        require(version is not None and version["view"]["status"] == "registered",
                "Delivery uses an inactive task version")
        key = identity(version["view"])
        if event["kind"] == "delivery.requested":
            requests[event["delivery_id"]] = key
        else:
            require(event["source"] == "simulated", "Native/offscreen receipt is outside this profile")
            if key in rows and rows[key]["first_rendered_at"] is None:
                receipt_seq = event["seq"]
                candidates = [view for seq, view in snapshots if seq > receipt_seq
                              and identity(view) == key and view.get("displayed_at") is not None]
                render_values = {instant(v["displayed_at"]) for v in candidates}
                if len(render_values) == 1:
                    rendered = render_values.pop()
                    due = instant(rows[key]["due_at"])
                    latency_s = (rendered - due).total_seconds()
                    rows[key]["first_rendered_at"] = rendered.isoformat()
                    rows[key]["latency_ms"] = round(latency_s * 1000, 3)
                    rows[key]["status"] = "measured" if latency_s >= 0 else "early_render_violation"
                    rows[key]["reason"] = "nonnegative_due_latency" if latency_s >= 0 else "rendered_before_due"
                else:
                    rows[key]["status"] = "undetermined"
                    rows[key]["reason"] = "missing_required_value"

    for key, row in rows.items():
        if row["status"] is None:
            closure_seq = row.get("closure_seq")
            if closure_seq is not None:
                row["status"] = "excluded"
                row["reason"] = "cancelled_or_superseded_before_display"
            else:
                row["status"] = "missing_display"
                row["reason"] = "missing_linkage"
    return list(rows.values()), cutoff


def nearest_rank_p95(values):
    """P95 using nearest-rank method: ceil(0.95 * n) smallest value."""
    require(values, "Cannot compute P95 of an empty sample")
    ordered = sorted(values)
    rank = math.ceil(0.95 * len(ordered))
    return ordered[rank - 1]


def metric_record(rows, cohort_id):
    measured = [r for r in rows if r["status"] == "measured"]
    early = [r for r in rows if r["status"] == "early_render_violation"]
    missing = [r for r in rows if r["status"] == "missing_display"]
    excluded = [r for r in rows if r["status"] == "excluded"]
    undetermined = [r for r in rows if r["status"] == "undetermined"]
    eligible = len(rows) - len(excluded)
    latencies = [r["latency_ms"] for r in measured]
    status = "not_applicable" if eligible == 0 else (
        "computed" if measured and not undetermined and not missing else "insufficient_evidence")
    p95 = nearest_rank_p95(latencies) if latencies else None
    warnings = []
    if status == "insufficient_evidence":
        if missing:
            warnings.append({"code": "LATENCY_MISSING_DISPLAY",
                             "message": f"{len(missing)} hard occurrences have no valid display receipt."})
        if undetermined:
            warnings.append({"code": "LATENCY_RENDER_TIMESTAMP_GAP",
                             "message": f"{len(undetermined)} occurrences lack a verifiable render timestamp."})
    result = {
        "metric_id": "latency.hard_due_p95", "definition_version": "0.4",
        "cohort_id": cohort_id, "metric_type": "duration", "status": status, "assessment": "not_evaluated",
        "value_scope": "eligible_cohort" if status == "computed" else "not_available",
        "value": p95 if status == "computed" else None,
        "unit": "ms", "currency": None, "numerator": None, "denominator": None,
        "numerator_unit": None, "denominator_unit": None,
        "statistic": "percentile", "quantile": 0.95, "quantile_method": "nearest_rank",
        "sample_count": len(measured) if measured else None,
        "counts": {
            "eligible_timing_targets": eligible, "measured": len(measured),
            "missing_latency": len(missing) + len(undetermined),
            "timed_out": 0, "early_render_violations": len(early),
        },
        "excluded": dict(Counter(r["reason"] for r in excluded)),
        "evidence_missing": {
            "latency_measurements": {"unit": "timing_target", "count": len(missing) + len(undetermined),
                "reason_counts": dict(Counter(r["reason"] for r in missing + undetermined))}},
        "not_invited_reason_counts": None,
        "coverage": {"latency_measurements": {"unit": "timing_target", "numerator": len(measured),
            "denominator": eligible, "value": len(measured) / eligible if eligible else None,
            "status": "computed" if eligible else "not_applicable"}},
        "warnings": warnings,
    }
    return result


def calculate_latency_report(suite_path):
    suite_path = Path(suite_path).resolve()
    manifest_path = suite_path / MANIFEST_NAME
    before = file_sha256(manifest_path)
    summary = summarize_suite(suite_path)
    require(summary["status"] == "passed", "Replay/evidence validation failed; no metric was published")
    require(summary["run_provenance"]["status"] == "verified", "A verified recorded-run manifest is required")
    manifest = load_report(manifest_path)
    suite = load_report(suite_path / "report.json")
    rows, cutoffs = [], []
    for case in suite["cases"]:
        name = case["scenario_id"]
        saved_case = load_report(safe_scenario_path(suite_path, name, "report.json"))
        require(saved_case == case, "Suite and per-case reports disagree")
        trace = safe_scenario_path(suite_path, name, "trace.jsonl").read_text(encoding="utf-8-sig")
        events = [json.loads(line, parse_constant=reject_constant, object_pairs_hook=unique_object)
                  for line in trace.splitlines() if line.strip()]
        case_rows, cutoff = calculate_case(case, events)
        rows.extend(case_rows)
        cutoffs.append(cutoff)
    verification = check_run_manifest(suite_path, set(manifest["scenarios"]))
    require(verification["status"] == "verified" and file_sha256(manifest_path) == before,
            "Saved replay evidence changed during analysis")
    cohort_id = "hard-due-latency:" + before[:16]
    report = {
        "result_schema_version": "0.4", "experiment_id": manifest["run_id"],
        "configuration_id": "recorded-m0:" + manifest["input_sha256"],
        "dataset_version": "saved-scenarios:" + manifest["input_sha256"],
        "cohort_manifest_ref": "cohort.json", "evidence_type": "synthetic_replay", "example_only": False,
        "as_of": max(cutoffs).isoformat(), "results": [metric_record(rows, cohort_id)],
    }
    validate_report(report)
    audit = {
        "schema_version": 1, "analysis_profile": PROFILE, "definition_version": "0.4",
        "cohort_id": cohort_id, "source_run_directory": str(suite_path),
        "source_manifest_sha256": before, "source_run_provenance": verification,
        "source_artifact_sha256": manifest["artifacts"], "scenario_count": len(suite["cases"]),
        "evidence_type": "synthetic_replay", "clock": "virtual", "receipt_source": "simulated",
        "cohort_rule": "All accepted registered hard occurrences; early renders reported as violations.",
        "outside_cohort": "Soft occurrences and unconfirmed drafts are outside the hard-due latency metric.",
        "real_latency": "not_measured", "real_model_cost": "not_measured", "human_ratings": "not_measured",
        "rows": rows,
    }
    return report, audit


def write_analysis(suite, output):
    suite, output = Path(suite).resolve(), Path(output).resolve()
    require(not output.exists(), "Use a new analysis output directory")
    require(not output.is_relative_to(suite) and not suite.is_relative_to(output),
            "Analysis output and source run must be separate sibling locations")
    names = ["scripts/d_calculate_latency.py", "scripts/d_check_metric_report.py",
             "scripts/d_count_deliveries.py", "scripts/d_summarize_suite.py", "scripts/d_run_evidence.py",
             "docs/evaluation/metrics-spec.md"]
    inputs = {name: (ROOT / name).read_bytes() for name in names}
    report, audit = calculate_latency_report(suite)
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
