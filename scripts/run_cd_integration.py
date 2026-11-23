"""Reproduce C/D adapter, replay, UI receipt and accepted review evidence checks."""
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
import platform
from pathlib import Path
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def source_hashes():
    files = set()
    for directory in ("deskpet", "scripts", "tests", "examples/m0", "docs/evaluation", "docs/contracts"):
        files.update(p for p in (ROOT / directory).rglob("*")
                     if p.suffix in {".py", ".json", ".md", ".command", ".ps1"})
    files.update(ROOT / p for p in ("d_metrics.py", "d-adapters.json", "d-cancel-before-due.json", "pyproject.toml", "uv.lock",
                                    ".python-version", "requirements.txt", "docs/cd-integration.md", "docs/c-realtime.md"))
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
        ("regression", ["-m", "pytest", "-q", "tests/m0", "tests/test_d_evaluation.py",
                        "tests/test_d_metric_calculators.py", "tests/test_d_metric_report.py",
                        "tests/test_d_window_metric.py", f"--junitxml={output / 'tests.xml'}"]),
        ("examples", ["-m", "deskpet.m0", "examples", "--check"]),
        ("d-baseline", ["scripts/d_replay.py", "--output", str(output / "d-baseline")]),
        ("combined-replay", ["-m", "deskpet.m0", "replay", "--suite", "m0", "--adapters",
                            "examples/m0/cd-product.adapters.json", "--output", str(output / "replay")]),
    ]
    for name, script in (("reminders", "smoke_c_reminders.py"), ("work", "smoke_c_work_sessions.py"),
                         ("state", "smoke_c_state.py"), ("experience", "smoke_c_experience.py")):
        adapters = "examples/m0/cd-desktop.adapters.json" if name == "experience" else "examples/m0/cd-product.adapters.json"
        command = [f"scripts/{script}", "--adapters", adapters, "--output", str(output / name)]
        if args.offscreen:
            command.append("--offscreen")
        steps.append((name, command))
    realtime = ["scripts/smoke_c_realtime.py", "--output", str(output / "realtime")]
    if args.offscreen:
        realtime.append("--offscreen")
    steps += [("realtime", realtime),
              ("host", ["-m", "deskpet.m0", "gui", "--profile", "scenario", "--auto-smoke",
                        "--adapters", "examples/m0/cd-product.adapters.json", "--output", str(output / "host")])]
    steps += [
        ("delivery-evidence", ["scripts/d_count_deliveries.py", str(output / "reminders/trace.jsonl")]),
        ("review-evidence", ["scripts/check_cd_reviews.py", str(output / "work/trace.jsonl")]),
        ("realtime-delivery-evidence", ["scripts/d_count_deliveries.py", str(output / "realtime/trace.jsonl")]),
        ("realtime-review-evidence", ["scripts/check_cd_reviews.py", str(output / "realtime/trace.jsonl")]),
        ("multi-delivery-evidence", ["scripts/d_count_deliveries.py", str(output / "experience/trace.jsonl")]),
    ]
    for name in ("window_metric", "delivery_violations", "work_outcomes"):
        steps.append((f"d-{name}", [f"scripts/d_calculate_{name}.py", str(output / "d-baseline"),
                                     "--output", str(output / f"d-{name}")]))
    report = {"status": "running", "steps": {}, "source_sha256": sha256(
        json.dumps(baseline, sort_keys=True).encode()).hexdigest(), "source_files": baseline,
        "automation": True, "clocks": {"scenario": "virtual", "realtime": "wall"},
        "platform": platform.platform(), "started_at": datetime.now(timezone.utc).isoformat(),
        "step_details": {}, "formal_human_metrics": "not_measured",
        "pending_acceptance": ["human_experience", "Windows" if sys.platform != "win32" else "macOS",
                               "A upstream UiSpec integration", "B persistent task service"]}
    def save():
        (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    save()
    try:
        for name, command in steps:
            print(f"Running {name}", flush=True)
            step_env = dict(env)
            if name == "host" and args.offscreen:
                step_env["QT_QPA_PLATFORM"] = "offscreen"
            started = time.monotonic()
            result = subprocess.run([sys.executable, *command], cwd=ROOT, env=step_env,
                                    capture_output=True, text=True, timeout=180)
            (output / f"{name}.log").write_text(result.stdout + result.stderr)
            report["steps"][name] = result.returncode
            report["step_details"][name] = {"command": command, "elapsed_s": round(time.monotonic() - started, 3)}
            save()
            if result.returncode:
                raise RuntimeError(f"{name} failed; inspect {output / (name + '.log')}")
        report["reviews"] = json.loads((output / "work/review-evidence.json").read_text())
        report["deliveries"] = json.loads((output / "reminders/delivery-metrics.json").read_text())
        report["gui"] = {}
        for name, filename in (("reminders", "c-reminders-report.json"), ("work", "c-work-sessions-report.json"),
                               ("state", "c-state-report.json"), ("experience", "c-experience-report.json"),
                               ("realtime", "realtime-report.json"), ("host", "native-gui-report.json")):
            data = json.loads((output / name / filename).read_text())
            if data.get("status") != "passed" or data.get("host_errors"):
                raise RuntimeError(f"{name} has failed checks or host errors")
            if not data.get("checks") or not all(data["checks"].values()):
                raise RuntimeError(f"{name} did not complete its checks")
            if not args.offscreen and not data.get("native_gui_verified"):
                raise RuntimeError(f"{name} is not verified native GUI evidence")
            report["gui"][name] = {"checks_passed": len(data["checks"]),
                "native_gui_verified": data.get("native_gui_verified", False),
                "source": data.get("source", data.get("receipt_source")),
                "clock": "wall" if name == "realtime" else "virtual"}
        suites = ET.parse(output / "tests.xml").getroot().findall("testsuite")
        report["regression"] = {key: sum(int(s.get(key, 0)) for s in suites)
                                for key in ("tests", "failures", "errors", "skipped")}
        if source_hashes() != baseline:
            raise RuntimeError("Source changed during integration")
        report["artifact_sha256"] = {str(p.relative_to(output)): sha256(p.read_bytes()).hexdigest()
                                     for p in sorted(output.rglob("*")) if p.is_file() and p != output / "summary.json"}
        report["status"] = "passed"
    except (RuntimeError, OSError, AssertionError, subprocess.TimeoutExpired) as exc:
        report.update(status="failed", error=str(exc))
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        save()
    print(json.dumps({k: v for k, v in report.items() if k not in {"source_files", "artifact_sha256"}}, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
