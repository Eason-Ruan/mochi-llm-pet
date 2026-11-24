"""Replay current System 2 chat and supported background policies offline."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation.system2_replay import blind_review, load_recordings, read_cases, run_replay


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New output directory")
    parser.add_argument("--scenario", action="append", help="Run selected scenario IDs (repeatable)")
    parser.add_argument("--recorded-model", type=Path, help="Optional offline model outputs captured on synthetic inputs")
    parser.add_argument("--blind-seed", type=int, help="Blind the comparison between two background policies")
    parser.add_argument("--check-semantic", action="store_true", help="Exit nonzero if a check fails with semantic background")
    args = parser.parse_args()
    try:
        cases = read_cases()
        if args.scenario:
            missing = set(args.scenario) - {case["id"] for case in cases}
            if missing:
                raise ValueError(f"Unknown scenario IDs: {', '.join(sorted(missing))}")
            cases = [case for case in cases if case["id"] in args.scenario]
        recordings = load_recordings(args.recorded_model) if args.recorded_model else None
        if args.blind_seed is not None and recordings is not None and len(recordings) != 2:
            raise ValueError("Blind review of recorded turns requires schema 2 captures for both background policies")
        args.output.mkdir(parents=True, exist_ok=False)
        report = run_replay(cases, recordings=recordings)
        write_json(args.output / "report.json", report)
        if args.blind_seed is not None:
            review, key = blind_review(cases, report, args.blind_seed)
            write_json(args.output / "blind-review.json", review)
            write_json(args.output / "blind-key.json", key)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"Replay failed: {type(error).__name__}: {error}\n")
    print(json.dumps({variant: data["metrics"] for variant, data in report["variants"].items()},
                     ensure_ascii=False, indent=2))
    semantic = report["variants"]["semantic_background"]["metrics"]
    interruption = semantic["interruption"]
    failed_steps = any(semantic[key] for key in (
        "false_actions", "missed_actions", "failed_checks", "duplicate_reminder_displays",
        "missing_or_mismatched_recorded_turns",
    ))
    failed_interruption = (
        any(interruption["hard_timing"][key] for key in ("early", "late", "missing"))
        or any(interruption["stale_displays"].values())
        or any(interruption[key] for key in (
            "duplicate_primary_displays", "repeat_sound_requests", "extra_same_batch_sound_requests", "quiet_failures",
        ))
    )
    if args.check_semantic and (failed_steps or failed_interruption):
        parser.exit(1, "Semantic replay failed one or more checks. See report.json.\n")


if __name__ == "__main__":
    main()
