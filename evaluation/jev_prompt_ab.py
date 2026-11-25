"""Compare two Jev prompts on frozen synthetic inputs in shadow replay."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
from unittest.mock import patch

from deskpet.system2 import jev_api, jev_shadow
from evaluation.jev_savings_replay import CLIP, COURSE_FOCUS, EXERCISE, GAME, LECTURE, NOTES, _fixture
from evaluation.jev_savings_replay import cases as savings_cases
from evaluation.jev_shadow_replay import BYPASS, MAY_DEFER, NEEDS_SYSTEM2, cases as smoke_cases, replay, summarize


CANDIDATE_INSTRUCTIONS = (
    "Does any candidate need review before the next tick? Set aside a candidate with pending_offer=true, "
    "same_opportunity=true and last_feedback=unanswered. For focus_pause, match the latest capture to "
    "activity.scene_observed_at in fresh, current activity. Use each capture's activity_kind and summary. "
    "A player alone does not establish purpose."
)
CANDIDATE_DECISIONS = {
    "wake_system2": (
        "Review new actionable evidence or material uncertainty. For focus_start, sustained independent "
        "work/study can qualify. For focus_pause, two non-late captures at least 25s apart must each "
        "support leisure, with no gap or contrary capture through now. Review this run when its purpose "
        "conflicts with the focus goal or its relevance remains uncertain."
    ),
    "defer": (
        "Wait if there is no candidate or all are covered. With no other reason to review, wait on a first "
        "work or leisure capture, or on gap-free work/study that fits the running focus. Reviewing an "
        "artifact alongside its source, tests, or course material can still serve the same goal."
    ),
}

BASELINE = "baseline"
CANDIDATE = "candidate"
SMOKE = "synthetic_smoke_sequences"
SAVINGS = "synthetic_savings_v1"
BOUNDARY = "synthetic_prompt_boundary_v1"


def reference_cohorts():
    return {
        SMOKE: tuple(case for case in smoke_cases() if case.scenario in {"work-start", "focus-bilibili"}),
        SAVINGS: savings_cases(),
    }


def boundary_cases():
    """Keep boundary labels and identifiers outside the model's projected state."""
    case_24s = _fixture("boundary-01", "b01", 0, MAY_DEFER,
                        "The two current leisure captures are 24 seconds apart.",
                        [(-58, NOTES), (-29, CLIP), (-5, CLIP)], focus=COURSE_FOCUS)
    case_25s = _fixture("boundary-02", "b02", 0, NEEDS_SYSTEM2,
                        "The two current leisure captures are 25 seconds apart.",
                        [(-58, NOTES), (-30, CLIP), (-5, CLIP)], focus=COURSE_FOCUS)
    returned = _fixture("boundary-03", "b03", 0, MAY_DEFER,
                        "The current scene has returned to the course exercise after leisure.",
                        [(-85, GAME), (-55, GAME), (-25, EXERCISE), (-5, EXERCISE)], focus=COURSE_FOCUS)
    interrupted = _fixture("boundary-04", "b04", 0, MAY_DEFER,
                            "A relevant lecture separates two leisure captures.",
                            [(-65, CLIP), (-35, LECTURE), (-5, CLIP)], focus=COURSE_FOCUS)
    incomplete = _fixture("boundary-05", "b05", 0, NEEDS_SYSTEM2,
                           "The capture window is incomplete, so the host must send the tick.",
                           [(-35, CLIP), (-5, CLIP)], focus=COURSE_FOCUS)
    incomplete.context["focus_evidence_2m"]["complete"] = False
    game = [(-65, NOTES), (-35, GAME), (-5, GAME)]
    before_offer = _fixture("boundary-06", "b06", 0, NEEDS_SYSTEM2,
                             "A game interrupts the active course timer.",
                             game, focus=COURSE_FOCUS)
    changed = _fixture("boundary-07", "b06", 1, NEEDS_SYSTEM2,
                       "The still-pending pause offer came from the earlier game, while a new unrelated video plays.",
                       [*game, (25, CLIP), (55, CLIP)], focus=COURSE_FOCUS, pending="phase.pause")
    return case_24s, case_25s, returned, interrupted, incomplete, before_offer, changed


def _paired_score(pairs, decision):
    counts = dict(both_correct=0, candidate_corrected=0, candidate_regressed=0,
                  both_wrong=0, unscored=0)
    for pair in pairs:
        baseline, candidate = (decision(pair[arm]) for arm in (BASELINE, CANDIDATE))
        if pair["expected"] == BYPASS or baseline is None or candidate is None:
            counts["unscored"] += 1
            continue
        should_defer = pair["expected"] == MAY_DEFER
        if baseline == should_defer:
            key = "both_correct" if candidate == should_defer else "candidate_regressed"
        else:
            key = "candidate_corrected" if candidate == should_defer else "both_wrong"
        counts[key] += 1
    return counts


def _failure_counts(rows):
    failures = Counter()
    for row in rows:
        result = row["jev"]
        # A client exception loses its typed provider result at the host gate.
        status = result["host_reason"] if result["host_reason"] == "client_error" else result["status"]
        if status not in {"ok", "local_decision", "hard_bypass"}:
            failures[status] += 1
    return dict(failures)


def _replay_arm(timeline, mode, arm):
    if arm == BASELINE:
        return replay(timeline, mode)
    with (patch.object(jev_api, "_INSTRUCTIONS", CANDIDATE_INSTRUCTIONS),
          patch.object(jev_shadow, "_DECISIONS", CANDIDATE_DECISIONS)):
        return replay(timeline, mode)


def compare(cohorts, mode, *, progress=None):
    """Run each scenario independently in both arms, alternating the first arm."""
    results = {}
    scenario_index = 0
    for name, cases in cohorts.items():
        timelines = {}
        for case in cases:
            timelines.setdefault(case.scenario, []).append(case)

        rows_by_arm = {BASELINE: [], CANDIDATE: []}
        order = {}
        for scenario, timeline in timelines.items():
            timeline.sort(key=lambda case: case.tick)
            ordered = (BASELINE, CANDIDATE) if scenario_index % 2 == 0 else (CANDIDATE, BASELINE)
            order[scenario] = list(ordered)
            scenario_index += 1
            for arm in ordered:
                report = _replay_arm(timeline, mode, arm)
                rows_by_arm[arm].extend(report["rows"])
                if progress is not None:
                    for row in report["rows"]:
                        progress(name, arm, row)

        arms = {}
        for arm, rows in rows_by_arm.items():
            arms[arm] = summarize(rows, mode)
            arms[arm]["dataset"] = name
            arms[arm]["jev"]["failure_counts"] = _failure_counts(rows)
        pairs = [
            dict(case=baseline["case"], scenario=baseline["scenario"], tick=baseline["tick"],
                 expected=baseline["expected"], baseline=baseline, candidate=candidate)
            for baseline, candidate in zip(rows_by_arm[BASELINE], rows_by_arm[CANDIDATE], strict=True)
        ]
        results[name] = dict(
            summary=arms,
            paired=dict(
                raw_model=_paired_score(pairs, lambda row: row["jev"]["raw_defer"]),
                host_policy=_paired_score(pairs, lambda row: row["jev"]["could_defer"]),
                combined_policy=_paired_score(pairs, lambda row: row["combined_could_defer"]),
            ),
            scenario_arm_order=order,
            rows=pairs,
        )
    return dict(
        jev_mode=mode,
        action_mode="shadow_only",
        prompts={
            BASELINE: dict(instructions=jev_api._INSTRUCTIONS, criteria=dict(jev_shadow._DECISIONS)),
            CANDIDATE: dict(instructions=CANDIDATE_INSTRUCTIONS, criteria=CANDIDATE_DECISIONS),
        },
        cohorts=results,
    )


def _print_row(cohort, arm, row):
    result = row["jev"]
    print(cohort, arm, row["case"], row["expected"], result["status"], result["choice"],
          result["confidence"], result["host_reason"], flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jev", choices=("fixture", "real"), default="fixture",
                        help="Use the offline stand-in or the configured real Jev API")
    parser.add_argument("--cohort", choices=("reference", "boundary", "all"), default="reference",
                        help="Select the reference cohorts, the separately frozen boundary cohort, or all cohorts")
    parser.add_argument("--output", type=Path, required=True, help="Write a new sanitized JSON report")
    args = parser.parse_args()
    if args.jev == "real":
        settings = jev_api.load_jev_settings()
        if not settings.available or not settings.share_text:
            parser.error("real A/B requires MOCHI_JEV_SHADOW=1, MOCHI_JEV_SHARE_TEXT=1 and a configured Jev key")
    cohorts = {} if args.cohort == "boundary" else reference_cohorts()
    if args.cohort != "reference":
        cohorts[BOUNDARY] = boundary_cases()
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(compare(cohorts, args.jev, progress=_print_row), output,
                  ensure_ascii=False, indent=2, allow_nan=False)
        output.write("\n")


if __name__ == "__main__":
    main()
