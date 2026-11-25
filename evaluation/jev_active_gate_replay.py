"""Exercise real System 2 deferrals on the 25 frozen synthetic Jev A/B ticks."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
from sqlite3 import Error as SQLiteError
from time import monotonic
from unittest.mock import patch

from deskpet.system2 import jev_api, jev_shadow
from deskpet.system2.jev_projection import project_jev_snapshot
from evaluation.jev_prompt_ab import (
    BOUNDARY, CANDIDATE_DECISIONS, CANDIDATE_INSTRUCTIONS, boundary_cases, reference_cohorts,
)
from evaluation.jev_shadow_replay import (
    BYPASS, MAY_DEFER, NEEDS_SYSTEM2, TOKEN_FIELDS, _advisor, _jev_result, _jev_status,
    _latency, _provider_summary, _system2_status, _totals, run_system2,
)


_LOCAL_DEFERS = {"no_candidate", "pending_offer", "unchanged_snapshot"}
_REQUIRED_WAKES = {NEEDS_SYSTEM2, BYPASS}
_SYSTEM2_MODEL = "qwen4-max"
_SYSTEM2_PROMPT = "system2.zh.v14"


def fixed_timelines():
    timelines = {}
    for cohort, cases in {**reference_cohorts(), BOUNDARY: boundary_cases()}.items():
        for case in cases:
            timelines.setdefault((cohort, case.scenario), []).append(case)
    return [(cohort, sorted(cases, key=lambda case: case.tick)) for (cohort, _), cases in timelines.items()]


def _gate(case, snapshot, settings, advisor, previous_defer, now):
    if not jev_shadow.is_pure_tick(case.event):
        return "send", _jev_status("hard_bypass", reason="non_tick_wake")
    if case.context["current_context"]["screen_locked"] is True:
        return "send", _jev_status("hard_bypass", reason="screen_locked")
    if snapshot.skip_reason:
        return "send", _jev_status("ineligible", reason=snapshot.skip_reason)
    if jev_shadow.jev_cap_reached(snapshot, previous_defer, as_of=now):
        return "send", _jev_status("local_decision", reason="defer_cap")
    local = snapshot.local_defer_reason
    if local in _LOCAL_DEFERS:
        return "local_defer", _jev_status("local_decision", reason=local, could_defer=True)
    if not settings.available:
        return "send", _jev_status("provider_unconfigured", reason="jev_settings_unavailable")

    result, choice, reason = jev_shadow.adjudicate_jev(settings, advisor, snapshot, send_permitted=lambda: True)
    return ("jev_defer" if choice == "defer" else "send"), _jev_result(result, choice, reason, "real")


def replay_timeline(cohort, timeline, arm, settings_path, *, settings=None, advisor=None, progress=None):
    """Advance each arm's review only after a completed System 2 admission."""
    if arm not in {"baseline", "gated"} or settings_path is None:
        raise ValueError("Select an arm and provide isolated System 2 route settings")
    if not timeline or len({case.scenario for case in timeline}) != 1:
        raise ValueError("Replay takes one non-empty scenario at a time")
    if arm == "gated" and settings is None:
        settings, advisor = _advisor("real")

    rows = []
    last_review = None
    last_fixture_review = None
    previous_defer = None
    for case in sorted(timeline, key=lambda case: case.tick):
        started = monotonic()
        context = {**case.context, "jev_exact_feedback_provenance": True}
        if last_fixture_review is not None and context["pending_proposals"]:
            # A painted offer is an observation scripted into the next fixed tick.
            # Its provenance stays fixed even if this arm skipped or failed the prior review.
            context["pending_proposals"] = [
                {**offer, "jev_origin_activity": last_fixture_review.activity,
                 "jev_origin_phase": last_fixture_review.phase, "jev_origin_review_at": last_fixture_review.as_of,
                 "jev_displayed": True}
                for offer in context["pending_proposals"]
            ]
        now = datetime.fromisoformat(context["as_of"])
        snapshot = project_jev_snapshot(context, next_tick_in_s=60.0, share_text=bool(settings and settings.share_text),
                                        last_review=last_review)
        review_before = last_review.as_of if last_review is not None else None
        route = "send"
        jev = _jev_status("not_requested", reason="baseline")
        if arm == "gated":
            with (patch.object(jev_api, "_INSTRUCTIONS", CANDIDATE_INSTRUCTIONS),
                  patch.object(jev_shadow, "_DECISIONS", CANDIDATE_DECISIONS)):
                route, jev = _gate(case, snapshot, settings, advisor, previous_defer, now)

        last_fixture_review = snapshot.review
        if route == "send":
            system2 = run_system2(case, settings_path)
            previous_defer = None
            if system2["status"] == "ok":
                last_review = snapshot.review
        else:
            system2 = _system2_status("skipped_by_gate")
            previous_defer = dict(tick_at=now.isoformat(), cap_key=jev_shadow.jev_cap_key(snapshot), host_choice="defer")

        row = dict(
            cohort=cohort, case=case.id, scenario=case.scenario, tick=case.tick, expected=case.expected,
            annotation=case.annotation, arm=arm, route=route, jev=jev, system2=system2,
            pipeline_latency_ms=round((monotonic() - started) * 1000, 1),
            last_real_review_at=review_before, next_real_review_at=last_review.as_of if last_review else None,
            scripted_pending_offer=bool(context["pending_proposals"]),
            missed_required_wake=case.expected in _REQUIRED_WAKES and route != "send",
        )
        rows.append(row)
        if progress is not None:
            progress(row)
    return rows


def _token_delta(baseline, gated):
    def complete_tokens(usage, field):
        metric = usage[field]
        return metric["tokens"] if metric["total_requests"] else 0

    deltas = {}
    for field in TOKEN_FIELDS:
        baseline_tokens = complete_tokens(baseline, field)
        gated_tokens = complete_tokens(gated, field)
        system2_delta = None if baseline_tokens is None or gated_tokens is None else baseline_tokens - gated_tokens
        deltas[field] = dict(system2_saved=system2_delta,
                             system2_saved_fraction=None if not baseline_tokens or system2_delta is None
                             else system2_delta / baseline_tokens)
    return deltas


def _arm_summary(rows):
    jev = [row["jev"] for row in rows if "jev" in row]
    system2 = [row["system2"] for row in rows]
    return dict(
        tick_count=len(rows),
        routes=dict(Counter(row.get("route", "send") for row in rows)),
        jev_raw_choices=dict(Counter(row["choice"] for row in jev if row["choice"] is not None)),
        host_reasons=dict(Counter(row["host_reason"] for row in jev)),
        pipeline_latency=_latency([{"latency_ms": row.get("pipeline_latency_ms")} for row in rows]),
        jev=_provider_summary(jev), system2=_provider_summary(system2),
    )


def _paired_summary(pairs):
    baseline = _arm_summary([row["baseline"] for row in pairs])
    gated = _arm_summary([row["gated"] for row in pairs])
    skipped = [row for row in pairs if row["gated"]["route"] != "send"]
    required = [row for row in pairs if row["expected"] == NEEDS_SYSTEM2]
    mandatory_bypasses = [row for row in pairs if row["expected"] == BYPASS]
    missed = [row["case"] for row in skipped if row["expected"] == NEEDS_SYSTEM2]
    missed_due_to_failure = {
        arm: [row["case"] for row in required if row[arm].get("route", "send") == "send"
              and row[arm]["system2"]["status"] != "ok"] for arm in ("baseline", "gated")
    }
    return dict(
        label_counts=dict(Counter(row["expected"] for row in pairs)),
        baseline=baseline, gated=gated,
        required_wakes=len(required), missed_required_wakes=missed, required_wake_send_failures=missed_due_to_failure,
        hard_bypasses=dict(
            expected=len(mandatory_bypasses),
            sent=sum(row["gated"]["route"] == "send" for row in mandatory_bypasses),
            completed=sum(row["gated"]["system2"]["status"] == "ok" for row in mandatory_bypasses),
            wrongly_skipped=[row["case"] for row in mandatory_bypasses if row["gated"]["route"] != "send"],
        ),
        skip_eligible_ticks=sum(row["expected"] == MAY_DEFER for row in pairs),
        system2_ticks_avoided=len(skipped),
        system2_requests_saved=baseline["system2"]["model_requests"] - gated["system2"]["model_requests"],
        skipped_tick_baseline_system2=_provider_summary([row["baseline"]["system2"] for row in skipped]),
        token_deltas=_token_delta(baseline["system2"]["usage"], gated["system2"]["usage"]),
    )


def _index_rows(rows):
    indexed = {}
    for row in rows:
        key = (row["cohort"], row["case"])
        if key in indexed:
            raise ValueError(f"Duplicate replay tick: {key}")
        indexed[key] = row
    return indexed


def compare_rows(baseline_rows, gated_rows):
    """Pair completed reports from all shards without calling either provider."""
    baseline = _index_rows(baseline_rows)
    gated = _index_rows(gated_rows)
    fixed = {(cohort, case.id): case for cohort, timeline in fixed_timelines() for case in timeline}
    if baseline.keys() != fixed.keys() or gated.keys() != fixed.keys():
        raise ValueError("Both arms must cover exactly the 25 frozen replay ticks")

    pairs = []
    for key, case in fixed.items():
        base, gate = baseline[key], gated[key]
        for row in (base, gate):
            if (row["scenario"], row["tick"], row["expected"]) != (case.scenario, case.tick, case.expected):
                raise ValueError(f"Replay tick metadata does not match the fixture: {key}")
            system2 = row["system2"]
            if system2["status"] == "ok" and (system2["model"], system2["prompt_version"]) != (
                    _SYSTEM2_MODEL, _SYSTEM2_PROMPT):
                raise ValueError(f"Wrong System 2 model or prompt: {key}")
        skipped = gate["route"] in {"local_defer", "jev_defer"}
        if (base["system2"]["status"] in {"not_requested", "skipped_by_gate"}
                or gate["route"] not in {"send", "local_defer", "jev_defer"}
                or skipped != (gate["system2"]["status"] == "skipped_by_gate")
                or (skipped and gate["system2"]["requests"] != 0)):
            raise ValueError(f"Inconsistent System 2 routing: {key}")
        pairs.append(dict(cohort=key[0], case=case.id, scenario=case.scenario, tick=case.tick, expected=case.expected,
                          annotation=case.annotation, baseline=base, gated=gate,
                          avoided_tick_baseline_tokens=_totals([base["system2"]]) if skipped else None))

    return dict(
        action_mode="isolated_synthetic_replay", system2_model=_SYSTEM2_MODEL, system2_prompt_version=_SYSTEM2_PROMPT,
        dataset="25_fixed_pre_labeled_ticks", fixture_world="scripted_observations_and_pending_display_in_both_arms",
        prior_review="each_arm_advances_only_after_successful_system2_completion",
        required_wake_basis="fixed_fixture_label_and_actual_route", percentile_method="nearest_rank",
        confidence_threshold=jev_shadow._MIN_DEFER_CONFIDENCE,
        jev_prompt=dict(instructions=CANDIDATE_INSTRUCTIONS, criteria=CANDIDATE_DECISIONS),
        summary=_paired_summary(pairs),
        cohorts={cohort: _paired_summary([row for row in pairs if row["cohort"] == cohort])
                 for cohort in dict.fromkeys(row["cohort"] for row in pairs)},
        rows=pairs,
    )


def _print_row(row):
    jev, s2 = row["jev"], row["system2"]
    print(row["cohort"], row["case"], row["expected"], row["route"],
          jev["status"], jev["choice"], jev["confidence"], jev["host_reason"],
          s2["status"], s2["requests"], flush=True)


def _check_route(path):
    from evaluation.system2_prompt_ab import configured_route

    route, _, _ = configured_route(path)
    if route.model != _SYSTEM2_MODEL:
        raise ValueError(f"Use a verified Bailian route configured for {_SYSTEM2_MODEL}")


def _shard(value):
    try:
        index, count = map(int, value.split("/"))
        if 1 <= index <= count <= len(fixed_timelines()):
            return index, count
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("Use a one-based shard such as 1/3")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=("baseline", "gated", "compare"), required=True)
    parser.add_argument("--system2-settings", type=Path,
                        help="Read the verified qwen4-max route from this settings database")
    baseline_source = parser.add_mutually_exclusive_group()
    baseline_source.add_argument("--baseline-jsonl", type=Path,
                                 help="Read independently measured baseline rows for comparison")
    baseline_source.add_argument("--baseline-report", type=Path, action="append", help="Include a baseline report")
    parser.add_argument("--gated-report", type=Path, action="append", help="Include an existing gated report in comparison")
    parser.add_argument("--shard", type=_shard, default=(1, 1), help="Run one subset of whole scenarios, e.g. 1/3")
    parser.add_argument("--output", type=Path, required=True, help="Write a new sanitized JSON report")
    args = parser.parse_args()
    if args.arm == "compare":
        if not (args.baseline_jsonl or args.baseline_report) or not args.gated_report:
            parser.error("Compare requires both baseline and gated reports")
        if args.baseline_jsonl:
            with args.baseline_jsonl.open(encoding="utf-8") as source:
                baseline = [json.loads(line) for line in source]
        else:
            baseline = _read_rows(args.baseline_report, "baseline")
        report = compare_rows(baseline, _read_rows(args.gated_report, "gated"))
        with args.output.open("x", encoding="utf-8") as output:
            _write_report(output, report)
        return
    if args.system2_settings is None:
        parser.error("Both replay arms require --system2-settings")
    try:
        _check_route(args.system2_settings)
    except (OSError, SQLiteError, ValueError):
        parser.error("A verified and permitted Bailian qwen4-max route is required")
    settings, advisor = _advisor("real") if args.arm == "gated" else (None, None)
    if args.arm == "gated" and (not settings.available or not settings.share_text):
        parser.error("Gated replay requires MOCHI_JEV_SHADOW=1, MOCHI_JEV_SHARE_TEXT=1 and a configured Jev key")
    index, count = args.shard
    with args.output.open("x", encoding="utf-8") as output:
        rows = []
        for offset, (cohort, timeline) in enumerate(fixed_timelines()):
            if offset % count == index - 1:
                rows.extend(replay_timeline(cohort, timeline, args.arm, args.system2_settings,
                                            settings=settings, advisor=advisor, progress=_print_row))
        _write_report(output, dict(arm=args.arm, action_mode="isolated_synthetic_replay", rows=rows))


def _read_rows(paths, arm):
    rows = []
    for path in paths:
        with path.open(encoding="utf-8") as source:
            report = json.load(source)
        if report["arm"] != arm:
            raise ValueError(f"Expected a {arm} report")
        rows.extend(report["rows"])
    return rows


def _write_report(output, report):
    json.dump(report, output, ensure_ascii=False, indent=2, allow_nan=False)
    output.write("\n")


if __name__ == "__main__":
    main()
