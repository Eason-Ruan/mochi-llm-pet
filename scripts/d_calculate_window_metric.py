"""Calculate a conservative soft-window metric from a recorded M0 replay.

Reads saved command results, task lifecycle events, validated delivery links,
and owner TaskView timestamps. Never treats event arrival time as render time.
This profile accepts only simulated M0 scenario evidence, not native runs.
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
PROFILE = "m0-soft-window-v1"
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


def window(view):
    draft = view["draft"]
    require(draft["mode"] in {"soft", "hard"}, "Unknown reminder mode")
    earliest, latest = (instant(draft[key]) for key in ("earliest_at", "latest_first_delivery_at"))
    require(earliest <= latest, "Invalid task window")
    return earliest, latest


def calculate_case(case, events):
    """Return auditable soft-occurrence rows for one intact M0 case."""
    require(case["profile"] == "scenario" and case["clock"] == "virtual"
            and case["receipt_source"] == "simulated", "Only simulated M0 scenario cases are supported")
    require(events, "Empty trace")
    require(all(e["clock"] == "virtual" and e["simulated"] is True for e in events),
            "Mixed/native evidence is outside this calculator profile")
    times = [instant(e["ts"]) for e in events]
    require(times == sorted(times), "Trace wall time is not ordered")
    cutoff = times[-1]
    applied, result_locations = {}, {}
    for index, result in enumerate(case["outcomes"]["command_results"]):
        if result["status"] == "applied":
            key = result["command_id"]
            require(key not in applied, "Command was applied more than once")
            applied[key] = result
            result_locations[key] = f"/outcomes/command_results/{index}/view/displayed_at"

    rows, states, snapshots, versions, used = {}, {}, [], {}, set()
    pending_command = None
    for event in events:
        seq, kind = event["seq"], event["kind"]
        if kind == "scenario.input":
            payload = event["payload"]
            pending_command = payload["payload"] if payload["kind"] == "command" else None
        if kind not in KINDS:
            continue
        command_id = event["payload"]["command_id"]
        require(pending_command is not None and pending_command["command_id"] == command_id,
                "Task event has no matching command input")
        require(kind == "task." + {"create": "created", "confirm": "confirmed", "edit": "edited",
                "snooze": "snoozed", "cancel": "cancelled", "complete": "completed"}[pending_command["kind"]],
                "Task event does not match the input action")
        require(command_id in applied and command_id not in used, "Task event has no unique applied result")
        used.add(command_id)
        result = applied[command_id]
        view = result["view"]
        key = identity(view)
        window(view)
        task, occurrence = key
        require((event["task_id"], event["task_version"], event["payload"]["occurrence"], event["payload"]["status"])
                == (task, view["version"], occurrence, view["status"]), "Task event and result view disagree")
        require(result["actual_version"] == view["version"] and pending_command["task_id"] == task,
                "Applied result identity mismatch")
        previous = states.get(task)
        expected_status = {"task.created": "draft", "task.confirmed": "registered", "task.snoozed": "registered",
                           "task.cancelled": "cancelled", "task.completed": "completed"}.get(kind)
        if kind == "task.edited":
            expected_status = previous["status"] if previous else None
        require(view["status"] == expected_status, "Task lifecycle status does not match its action")
        if previous:
            require(previous["status"] not in {"cancelled", "completed"}, "Applied command follows a terminal task")
            require(occurrence >= previous["occurrence"], "Owner occurrence history went backwards")
            if kind in {"task.confirmed", "task.cancelled", "task.completed"}:
                require(occurrence == previous["occurrence"], "Lifecycle action unexpectedly changed occurrence")
            if kind == "task.confirmed":
                require(previous["status"] == "draft", "Confirmation requires a draft")
            if kind in {"task.snoozed", "task.completed"}:
                require(previous["status"] == "registered", "Snooze/completion requires a registered task")
        require(view["version"] == (previous["version"] + 1 if previous else 1), "Incomplete task version history")
        require(pending_command["expected_version"] == (previous["version"] if previous else 0),
                "Applied command CAS history mismatch")
        if previous:
            old_key = identity(previous)
            version = versions[(task, previous["version"])]
            version["end_seq"] = seq
            if old_key in rows and (old_key != key or view["status"] != "registered"):
                rows[old_key]["closure"] = {"kind": kind, "seq": seq, "ts": event["ts"]}
        else:
            require(kind == "task.created" and view["status"] == "draft", "Missing task creation history")
        states[task] = view
        snapshots.append((seq, view, result_locations[command_id]))
        versions[(task, view["version"])] = {"view": view, "start_seq": seq, "end_seq": len(events) + 1}
        if view["status"] == "registered" and view["draft"]["mode"] == "soft":
            if key not in rows:
                rows[key] = {
                    "run_id": events[0]["run_id"], "scenario_id": case["scenario_id"],
                    "task_id": task, "occurrence": occurrence, "opened_seq": seq,
                    "opening_command_id": command_id, "versions": [], "closure": None,
                    "requests": [], "receipts": [],
                }
            rows[key]["versions"].append(view["version"])
    require(used == set(applied), "An applied command has no lifecycle event")
    require(set(states) == set(case["outcomes"]["tasks"]), "Final task inventory differs from command history")
    for task, final in case["outcomes"]["tasks"].items():
        identity(final)
        window(final)
        # Only displayed_at can change outside accepted user commands in this profile.
        expected = states[task]
        require(all(final[k] == v for k, v in expected.items() if k != "displayed_at"),
                "Final task state differs from accepted command history")
        token = task.replace("~", "~0").replace("/", "~1")
        snapshots.append((len(events) + 1, final, f"/outcomes/tasks/{token}/displayed_at"))

    requests = {}
    for event in events:
        if event["kind"] not in {"delivery.requested", "delivery.displayed"}:
            continue
        version = versions.get((event["task_id"], event["task_version"]))
        require(version is not None and version["start_seq"] < event["seq"] < version["end_seq"]
                and version["view"]["status"] == "registered", "Delivery uses an inactive task version")
        key = identity(version["view"])
        if key in rows:
            require(event["task_version"] in rows[key]["versions"],
                    "Changing mode within one occurrence is outside this M0 soft-window profile")
        if event["kind"] == "delivery.requested":
            require(event["payload"]["occurrence"] == key[1], "Request occurrence differs from owner state")
            require(event["delivery_id"] not in requests or requests[event["delivery_id"]][0] == key,
                    "Delivery ID was reused for another occurrence")
            requests[event["delivery_id"]] = (key, event)
            if key in rows:
                rows[key]["requests"].append(event)
        else:
            require(event["source"] == "simulated", "Native/offscreen receipt is outside this profile")
            require(event["delivery_id"] in requests and requests[event["delivery_id"]][0] == key,
                    "Displayed receipt has no earlier occurrence request")
            if key in rows:
                rows[key]["receipts"].append(event)

    for key, row in rows.items():
        latest_version = versions[(key[0], row["versions"][-1])]
        earliest, latest = window(latest_version["view"])
        row.update(earliest_at=earliest.isoformat(), latest_first_delivery_at=latest.isoformat(),
                   observation_cutoff=cutoff.isoformat(), status=None, reason=None, gap_reason=None,
                   first_rendered_at=None, render_evidence=None)
        receipts, requests_for_row = row.pop("receipts"), row.pop("requests")
        row["request_seqs"] = [e["seq"] for e in requests_for_row]
        row["receipt_seqs"] = [e["seq"] for e in receipts]
        row["delivery_ids"] = sorted({e["delivery_id"] for e in requests_for_row})
        views = [(seq, view, pointer) for seq, view, pointer in snapshots if identity(view) == key]
        if receipts:
            # TaskView has one mutable timestamp, not a per-receipt history.
            # Multiple accepted receipts cannot be dated from that snapshot alone.
            if len(receipts) != 1:
                row.update(status="undetermined", reason="multiple_receipts_without_first_render_timestamp",
                           gap_reason="missing_required_value")
                continue
            receipt = receipts[0]
            candidates = [(seq, view["displayed_at"], pointer) for seq, view, pointer in views
                          if seq > receipt["seq"] and view["displayed_at"] is not None]
            values = {instant(value) for _, value, _ in candidates}
            if len(values) != 1:
                row.update(status="undetermined", reason="missing_or_conflicting_owner_render_timestamp",
                           gap_reason="missing_required_value")
                continue
            rendered = values.pop()
            version = versions[(key[0], receipt["task_version"])]
            first, last = window(version["view"])
            preceding = [e for e in requests_for_row if e["delivery_id"] == receipt["delivery_id"]
                         and e["seq"] < receipt["seq"]]
            request_time = min(instant(e["ts"]) for e in preceding)
            late = receipt["payload"].get("late_by_s")
            if (not request_time <= rendered <= instant(receipt["ts"])
                    or type(late) not in (int, float) or not math.isfinite(late)
                    or not math.isclose(late, max(0.0, (rendered - last).total_seconds()), abs_tol=1e-9)):
                row.update(status="undetermined", reason="owner_timestamp_contradicts_receipt",
                           gap_reason="invalid_record")
                continue
            row.update(first_rendered_at=rendered.isoformat(),
                       render_evidence={"receipt_seq": receipt["seq"], "task_version": receipt["task_version"],
                                        "snapshot_seq": candidates[0][0], "owner_report_pointer": candidates[0][2],
                                        "field": "TaskView.displayed_at"},
                       status="success" if first <= rendered <= last else "miss",
                       reason="first_display_in_window" if first <= rendered <= last else "first_display_outside_window")
        elif any(view["displayed_at"] is not None for _, view, _ in views):
            row.update(status="undetermined", reason="owner_display_has_no_accepted_receipt", gap_reason="missing_linkage")
        else:
            closure = row["closure"]
            adjudicated = any(instant(e["ts"]) >= latest for e in requests_for_row)
            withdrawn = (closure and closure["kind"] in {"task.cancelled", "task.edited", "task.snoozed"}
                         and instant(closure["ts"]) <= latest and not adjudicated)
            if withdrawn:
                row.update(status="excluded", reason={"task.cancelled": "cancelled_before_first_display_and_deadline",
                           "task.edited": "edited_before_first_display_and_deadline",
                           "task.snoozed": "snoozed_before_first_display_and_deadline"}[closure["kind"]])
            elif closure and closure["kind"] == "task.completed" and instant(closure["ts"]) < latest:
                row.update(status="undetermined", reason="predeadline_completion_requires_an_agreed_obligation_rule",
                           gap_reason="missing_linkage")
            elif cutoff < latest:
                row.update(status="undetermined", reason="observation_ended_before_deadline",
                           gap_reason="missing_required_value")
            else:
                row.update(status="miss", reason="closed_window_without_verifiable_display")
    return list(rows.values()), cutoff


def metric_record(rows, cohort_id):
    states = Counter(row["status"] for row in rows)
    n = len(rows) - states["excluded"]
    successes, unknown = states["success"], states["undetermined"]
    status = "not_applicable" if n == 0 else "insufficient_evidence" if unknown else "computed"
    warnings = [] if status == "computed" else [{"code": "EMPTY_SOFT_COHORT" if n == 0 else "WINDOW_EVIDENCE_GAP",
                "message": "No eligible soft occurrences." if n == 0 else "Primary value is null; keep undetermined occurrences and bounds."}]
    result = {
        "metric_id": "delivery.soft_window_first_display_rate", "definition_version": "0.4",
        "cohort_id": cohort_id, "metric_type": "ratio", "status": status, "assessment": "not_evaluated",
        "value_scope": "eligible_cohort" if status == "computed" else "not_available",
        "value": successes / n if status == "computed" else None,
        "unit": "fraction", "currency": None, "numerator": successes, "denominator": n,
        "numerator_unit": "occurrence", "denominator_unit": "occurrence",
        "statistic": "scalar", "quantile": None, "quantile_method": None, "sample_count": None,
        "counts": {"total_occurrences": len(rows), "eligible_occurrences": n,
                   "confirmed_successes": successes, "confirmed_misses": states["miss"], "undetermined_occurrences": unknown},
        "excluded": dict(Counter(row["reason"] for row in rows if row["status"] == "excluded")),
        "evidence_missing": {"delivery_evidence": {"unit": "occurrence", "count": unknown,
            "reason_counts": dict(Counter(row["gap_reason"] for row in rows if row["status"] == "undetermined"))}},
        "not_invited_reason_counts": None,
        "coverage": {"usable_occurrence_evidence": {"unit": "occurrence", "numerator": n - unknown,
            "denominator": n, "value": (n - unknown) / n if n else None,
            "status": "computed" if n else "not_applicable"}}, "warnings": warnings,
    }
    if unknown and n:
        result["bounds"] = {"lower": successes / n, "upper": (successes + unknown) / n,
                            "unit": "fraction", "value_scope": "eligible_cohort"}
    return result


def calculate_window_report(suite_path):
    """Read and recheck a recorded suite; return a typed report and its audit."""
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
    cohort_id = "confirmed-soft:" + before[:16]
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
        "cohort_rule": "All accepted registered soft occurrences in saved command history; retain unknown outcomes.",
        "outside_cohort": "Hard occurrences and unconfirmed drafts are outside the soft metric, not successes.",
        "real_latency": "not_measured", "real_model_cost": "not_measured", "human_ratings": "not_measured",
        "rows": rows,
    }
    return report, audit


def write_analysis(suite, output):
    suite, output = Path(suite).resolve(), Path(output).resolve()
    require(not output.exists(), "Use a new analysis output directory")
    require(not output.is_relative_to(suite) and not suite.is_relative_to(output),
            "Analysis output and source run must be separate sibling locations")
    names = ["scripts/d_calculate_window_metric.py", "scripts/d_check_metric_report.py",
             "scripts/d_count_deliveries.py", "scripts/d_summarize_suite.py", "scripts/d_run_evidence.py",
             "docs/evaluation/metrics-spec.md", "docs/evaluation/window-metric.md"]
    inputs = {name: (ROOT / name).read_bytes() for name in names}
    report, audit = calculate_window_report(suite)
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
