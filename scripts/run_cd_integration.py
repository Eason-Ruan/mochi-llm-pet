"""Reproduce C/D adapter, replay, UI receipt and accepted review evidence checks."""
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def source_hashes():
    files = set()
    for directory in ("deskpet", "scripts", "tests", "examples/m0", "docs/evaluation"):
        files.update(p for p in (ROOT / directory).rglob("*")
                     if p.suffix in {".py", ".json", ".md", ".command", ".ps1"})
    files.update(ROOT / p for p in ("d_metrics.py", "d-adapters.json", "d-cancel-before-due.json", "pyproject.toml", "uv.lock"))
    return {str(p.relative_to(ROOT)): sha256(p.read_bytes()).hexdigest() for p in sorted(files)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="A new output directory")
    parser.add_argument("--offscreen", action="store_true", help="Diagnostics only, no native GUI acceptance")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    baseline = source_hashes()
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    steps = [
        ("d-baseline", ["scripts/d_replay.py", "--output", str(output / "d-baseline")]),
        ("combined-replay", ["-m", "deskpet.m0", "replay", "--suite", "m0", "--adapters",
                            "examples/m0/cd-product.adapters.json", "--output", str(output / "replay")]),
    ]
    for name, script in (("reminders", "smoke_c_reminders.py"), ("work", "smoke_c_work_sessions.py")):
        command = [f"scripts/{script}", "--adapters", "examples/m0/cd-product.adapters.json", "--output", str(output / name)]
        if args.offscreen:
            command.append("--offscreen")
        steps.append((name, command))
    steps += [
        ("delivery-evidence", ["scripts/d_count_deliveries.py", str(output / "reminders/trace.jsonl")]),
        ("review-evidence", ["scripts/check_cd_reviews.py", str(output / "work/trace.jsonl")]),
    ]
    report = {"status": "running", "steps": {}, "source_sha256": sha256(
        json.dumps(baseline, sort_keys=True).encode()).hexdigest(), "source_files": baseline,
        "automation": True, "clock": "virtual", "formal_metrics": "not_measured"}
    try:
        for name, command in steps:
            print(f"Running {name}", flush=True)
            result = subprocess.run([sys.executable, *command], cwd=ROOT, env=env,
                                    capture_output=True, text=True, timeout=180)
            (output / f"{name}.log").write_text(result.stdout + result.stderr)
            report["steps"][name] = result.returncode
            if result.returncode:
                raise RuntimeError(f"{name} failed; inspect {output / (name + '.log')}")
        report["reviews"] = json.loads((output / "work/review-evidence.json").read_text())
        report["deliveries"] = json.loads((output / "reminders/delivery-metrics.json").read_text())
        assert source_hashes() == baseline, "Source changed during integration"
        report["status"] = "passed"
    except (RuntimeError, OSError, AssertionError, subprocess.TimeoutExpired) as exc:
        report.update(status="failed", error=str(exc))
    finally:
        (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "source_files"}, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
