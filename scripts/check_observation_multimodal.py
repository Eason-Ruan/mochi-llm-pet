"""Replay public screen recordings and a controlled desktop signal trace for Observation."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import Future
from contextlib import ExitStack, closing
from dataclasses import dataclass, is_dataclass
from datetime import UTC, datetime, timedelta
import hashlib
from io import BytesIO
import json
from pathlib import Path
import sqlite3
from time import monotonic
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageDraw

from deskpet.observation_v2 import media, pipeline
from deskpet.observation_v2.activity import ActivitySample
from deskpet.observation_v2.context import ObservationContextReader, project_observation_context
from deskpet.observation_v2.ollama import OllamaClient
from deskpet.observation_v2.state import StateObserver, TTL_SECONDS
from deskpet.system2.dsh_projection import project_context


EPOCH = datetime(2000, 1, 1, tzinfo=UTC)
MODES = ("split", "fixed", "event")
PUBLIC_CASES = ("S01-01", "S01-02", "S05-03")
PUBLIC_SCREEN_KINDS = {"S01-01": {"ide"}, "S01-02": {"video"}, "S05-03": {"browser"}}
SOURCE = "app:vscode"
SIMULATED_BOOT = "observation-acceptance-boot"
ANCHOR_LOOKBACK_S = 4.55


def _at(seconds):
    return EPOCH + timedelta(seconds=seconds)


def _seconds(value):
    return (datetime.fromisoformat(value) - EPOCH).total_seconds() if value else None


def _digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _rows(database, table):
    if table not in {"screen_observations", "effective_observations", "activities"}:
        raise ValueError("Unexpected evidence table")
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        if connection.execute("PRAGMA integrity_check").fetchall()[0][0] != "ok":
            raise AssertionError("Observation database failed integrity_check")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise AssertionError("Observation database failed foreign_key_check")
        return [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]


class CountedModel:
    def __init__(self, backend, *, clock=monotonic):
        self.backend = backend
        self.model = backend.model
        self.clock = clock
        self.attempts = []

    def complete(self, *, image=None, **kwargs):
        return self._call(self.backend.complete, image=image, **kwargs)

    def _call(self, operation, *args, **kwargs):
        start = self.clock()
        row = {"kind": "image" if kwargs.get("image") is not None else "text", "at_s": round(start, 6)}
        self.attempts.append(row)
        try:
            result = operation(*args, **kwargs)
            row["result"] = "returned"
            return result
        except Exception as exc:
            row["result"] = type(exc).__name__
            raise
        finally:
            row["latency_s"] = round(self.clock() - start, 6)


def _usage(records):
    def field(record, name):
        return getattr(record, name) if is_dataclass(record) else record.get(name)

    return {
        "responses": len(records),
        "prompt_tokens": sum(field(r, "prompt_tokens") or 0 for r in records),
        "completion_tokens": sum(field(r, "completion_tokens") or 0 for r in records),
        "complete_responses": sum(
            field(r, "prompt_tokens") is not None and field(r, "completion_tokens") is not None for r in records
        ),
    }


def _ollama(stack, args, records):
    try:
        client = OllamaClient(args.model, endpoint=args.ollama, timeout=args.model_timeout,
                              usage_observer=records.append)
    except TypeError:
        client = OllamaClient(args.model, endpoint=args.ollama, timeout=args.model_timeout)
        def record(response):
            if response.is_success:
                response.read()
                data = response.json()
                records.append({"prompt_tokens": data.get("prompt_eval_count"),
                                "completion_tokens": data.get("eval_count")})

        client._http.event_hooks["response"].append(record)
    return stack.enter_context(client)


def _run_public_multimodal(path, database, model, *, mode, interval_s, max_frames):
    from deskpet.observation_v2.sampling import ACTIVITY_POLL_S, CaptureSchedule

    if mode == "event":
        return pipeline.run_replay_multimodal(path, database, model, interval_s=ACTIVITY_POLL_S, max_frames=64)

    class FixedSchedule(CaptureSchedule):
        """The decoder already selects fixed-interval candidates."""

        def due(self, monotonic_s, *, interactive):
            return True

        def inspect(self, thumbnail, monotonic_s):
            return True

    with patch.object(pipeline, "CaptureSchedule", FixedSchedule):
        return pipeline.run_replay_multimodal(path, database, model, interval_s=interval_s, max_frames=max_frames)


def public_replay(args, mode, case, root):
    path = root / case["source_path"]
    if _digest(path) != case["media_sha256"]:
        raise ValueError(f"Frozen media digest mismatch for {case['case_id']}")
    database = args.output / f"public-{mode}-{case['case_id']}.sqlite"
    usage = []
    with ExitStack() as stack:
        model = CountedModel(_ollama(stack, args, usage))
        options = {"interval_s": args.public_interval, "max_frames": args.max_frames}
        if mode == "split":
            report = pipeline.run_replay(path, database, model, StateObserver(model), **options)
        else:
            report = _run_public_multimodal(path, database, model, mode=mode, **options)
    observations = _rows(database, "effective_observations")
    screens = _rows(database, "screen_observations")
    reader = ObservationContextReader(database)
    current = []
    gold_kinds = PUBLIC_SCREEN_KINDS.get(case["case_id"])
    for anchor in case["anchors"]:
        second = anchor["annotated"]
        projection = reader.read_current(_at(second))
        eligible = [row for row in screens if _seconds(row["captured_at"]) <= second
                    and _seconds(row.get("processed_at") or row["captured_at"]) <= second]
        recent = eligible[-1] if eligible else None
        captured = _seconds(recent["captured_at"]) if recent else None
        # The question asks about visible scene features at this anchor. It never asserts
        # what the user was doing or which app had native input focus.
        rateable = bool(gold_kinds and anchor["within_0_55"])
        close = captured is not None and second - captured <= ANCHOR_LOOKBACK_S
        previous_correct = any(row["status"] == "ready" and row["screen_kind"] in gold_kinds for row in eligible) if rateable else None
        current.append({"at_s": second, "source_s": captured, "rateable": rateable,
                        "state": projection[0].state if projection[0] else "unknown",
                        "state_status": projection[1],
                        "screen_kind": recent["screen_kind"] if recent and close else None,
                        "answerable": bool(close and recent["status"] == "ready"
                                           and recent["screen_kind"] in gold_kinds) if rateable else None,
                        "previous_scene_answerable": previous_correct})
    return {
        "evidence": "actual local model on public GUI-World video; no native system signals",
        "case_id": case["case_id"], "mode": mode, "source_url": case["url"], "source_sha256": case["media_sha256"],
        "model": args.model, "model_attempts": model.attempts, "usage": _usage(usage),
        "duration_s": case["duration_s"], "fixed_interval_s": args.public_interval if mode != "event" else None,
        "event_probe_interval_s": pipeline.ACTIVITY_POLL_S if mode == "event" else None,
        "anchor_lookback_s": ANCHOR_LOOKBACK_S,
        "answerable_anchors": sum(anchor["answerable"] is True for anchor in current),
        "previous_scene_answerable_anchors": sum(anchor["previous_scene_answerable"] is True for anchor in current),
        "rateable_anchors": sum(anchor["rateable"] for anchor in current),
        "pipeline_counts": {key: report.get(key) for key in ("activities", "screens", "observations", "state_runs")},
        "published_states": dict(Counter(row["state"] for row in observations)),
        "safety_violations": sum(row["state"] != "unknown" for row in observations),
        "anchors": current,
    }


@dataclass(frozen=True)
class DesktopPhase:
    start: float
    app: str | None
    window: int | None
    title: str | None
    document: str | None = None
    locked: bool | None = False
    idle: float | None = 0.0
    session: str = "test-desktop-a"


DESKTOP = (
    DesktopPhase(0, "Visual Studio Code", 1, "Alpha.md - demo - Visual Studio Code", "Alpha.md"),
    DesktopPhase(18, "Visual Studio Code", 2, "Beta.md - demo - Visual Studio Code", "Beta.md"),
    DesktopPhase(25.5, "Mochi", 3, "Mochi"),
    DesktopPhase(37.5, None, None, None, locked=True, idle=None),
    DesktopPhase(46.5, "Visual Studio Code", 2, "Beta.md - demo - Visual Studio Code", "Beta.md"),
    DesktopPhase(55.5, None, None, None, locked=None, idle=None),
    DesktopPhase(64.5, "Visual Studio Code", 4, "Gamma.md - demo - Visual Studio Code", "Gamma.md", session="test-desktop-b"),
)
READ_AT = (1.5, 7.5, 13.5, 17.5, 18.5, 22.5, 24.5, 26.5, 30.5, 36.5, 39.5, 46.0,
           48.5, 54.5, 57.5, 63.5, 65.5, 70.5, 77.5, 86.5)
DURATION_S = 90.0
COLORS = {1: (205, 50, 50), 2: (50, 205, 50), 3: (90, 90, 90), 4: (50, 50, 205)}


def _phase(seconds):
    return next(phase for phase in reversed(DESKTOP) if phase.start <= seconds)


def _screen(phase):
    image = Image.new("RGB", (640, 360), COLORS[phase.window])
    draw = ImageDraw.Draw(image)
    draw.rectangle((22, 40, 618, 140), fill="white")
    draw.text((35, 51), phase.title or "Unknown", fill="black")
    if phase.document:
        draw.text((35, 90), "Synthetic editor - document body", fill="black")
    return media._jpeg(image)


class ScriptedModel:
    model = "synthetic-desktop-responder"

    def __init__(self):
        self.documents_by_color = {COLORS[p.window]: p.document for p in DESKTOP if p.window in COLORS}

    def complete(self, *, prompt, schema, image=None, **_kwargs):
        if image is None:
            signals = json.loads(prompt)
            state = "away" if signals["screen_locked"] is True else "unknown"
            if signals["sustained_activity"]:
                state = "working"
            elif signals["recent_os_input"] and (signals["recently_switched"] or signals["input_recovered"]):
                state = "available"
            return {"activity_kind": "work" if signals["current_screen"] else "unknown", "state": state,
                    "summary": (signals["current_screen"] or {}).get("screen_summary"), "reason_codes": []}
        with Image.open(BytesIO(image)) as decoded:
            pixel = decoded.getpixel((0, 0))
        nearest = min(self.documents_by_color, key=lambda color: sum((a - b) ** 2 for a, b in zip(color, pixel)))
        label = self.documents_by_color[nearest]
        fields = schema["properties"]
        if "scene_elements" not in fields:
            return {"screen_kind": "ide" if label else "unknown", "visible_text_redacted": None,
                    "screen_summary": "A code editor is visible." if label else None}
        result = {"screen_kind": "ide" if label else "unknown", "activity_kind": "work" if label else "unknown",
                  "scene_elements": ["code editor"] if label else [], "visible_changes": [], "next_check": None}
        if "document_title" in fields:
            result["document_title"] = {"area": "active_editor_title", "name": label} if label else None
        return result


def _desktop_projection(reader, seconds):
    context = reader.read_realtime_context(_at(seconds), monotonic_s=seconds)
    if not hasattr(context, "current_context"):
        legacy = project_observation_context(context, share_summary=True)
        return legacy, project_context(legacy, share=True), project_context(legacy, share=False)
    view = project_observation_context(context, share_summary=True, document_sources=(SOURCE,),
                                       include_capture_health=True, include_document_source=True)
    cloud = project_context(view, share=True, document_sources=(SOURCE,))
    private_cloud = project_context(view, share=True, document_sources=())
    return view, cloud, private_cloud


def _unsaved_changes(view, *, history):
    if history:
        entries = (view.get("history_1h") or {}).get("segments", ())
    else:
        entries = (view.get("last_observed_activity") or {},)
    return [{"document": entry.get("document_name"), "at_s": _seconds(change.get("observed_at"))}
            for entry in entries if (change := entry.get("document_change"))
            and change.get("type") == "unsaved_changes_appeared"]


def _desktop_checks(snapshots):
    checks = []
    names = {phase.document for phase in DESKTOP if phase.document}
    for second, (view, cloud, private_cloud) in snapshots:
        phase = _phase(second)
        external = view.get("last_observed_activity") or {}
        new = "last_observed_activity" in view
        opened_mochi = phase.app == "Mochi"
        expected_doc = "Beta.md" if opened_mochi else phase.document
        document = external.get("document_name")
        relation = external.get("relation")
        observed = _seconds(external.get("observed_at"))
        projected_interruptibility = view.get("interruptibility")
        interruptibility = ((projected_interruptibility.get("current") or {}) if projected_interruptibility
                            else (view.get("current") or {}))
        state = interruptibility.get("state", "unknown")
        dsh_document = (cloud.get("last_observed_activity") or {}).get("document_name")
        stale_current = bool(relation == "current" and (phase.document is None or observed is None
                             or observed < phase.start or observed > second or (document and document != expected_doc)))
        aged_current_scene = bool(relation == "current" and observed is not None
                                  and external.get("scene_elements") and second - observed >= TTL_SECONDS)
        history = view.get("history_1h") or {}
        history_scenes = [
            {"document": segment.get("document_name"), "source_s": _seconds(segment["first_observed_at"])}
            for segment in history.get("segments", []) if segment.get("scene_elements")
        ]
        checks.append({
            "at_s": second, "expected_document": expected_doc, "document": document, "relation": relation,
            "source_s": observed, "current_status": view.get("current_status"),
            "interruptibility": state,
            "missing_metadata_state_violation": phase.locked is None and state != "unknown",
            "recipient_document": dsh_document,
            "ungranted_document_occurrences": sum(name in json.dumps(private_cloud) for name in names),
            "local_latest_unsaved_markers": _unsaved_changes(view, history=False),
            "local_history_unsaved_markers": _unsaved_changes(view, history=True),
            "recipient_latest_unsaved_markers": _unsaved_changes(cloud, history=False),
            "recipient_history_unsaved_markers": _unsaved_changes(cloud, history=True),
            "ungranted_unsaved_markers": len(_unsaved_changes(private_cloud, history=False))
            + len(_unsaved_changes(private_cloud, history=True)),
            "stale_current": stale_current if new else None,
            "aged_scene_in_current_activity": aged_current_scene if new else None,
            "history_scenes": history_scenes,
            "answerable": bool(document == expected_doc and observed is not None and observed <= second
                               and (relation == "previous" if opened_mochi else observed >= phase.start))
            if expected_doc is not None else None,
        })
    return checks


def _run_desktop_multimodal(database, model, mode, options):
    from deskpet.observation_v2.sampling import CaptureSchedule, UPLOAD_INTERVAL_S

    if mode == "event":
        return pipeline.run_live_multimodal(database, model, document_local_sources=(SOURCE,), **options)

    class FixedSchedule(CaptureSchedule):
        """Counterfactual with one eligible image at each 30-second clock slot."""

        def due(self, monotonic_s, *, interactive):
            return (self.identity is not None and monotonic_s % UPLOAD_INTERVAL_S == 0
                    and monotonic_s > self.last_upload_s)

        def inspect(self, thumbnail, monotonic_s):
            return True

        def recommend(self, next_check, monotonic_s, *, video=False):
            pass

    with patch.object(pipeline, "CaptureSchedule", FixedSchedule):
        return pipeline.run_live_multimodal(database, model, document_local_sources=(SOURCE,), **options)


def desktop_replay(args, mode):
    database = args.output / f"desktop-{mode}.sqlite"
    reader = ObservationContextReader(database, boot_id=SIMULATED_BOOT)
    elapsed = 0.0
    snapshots = []
    checkpoints = iter(READ_AT)
    next_read = next(checkpoints, None)
    captures = []

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return _at(elapsed)

    def sleep(seconds):
        nonlocal elapsed, next_read
        end = elapsed + seconds
        while next_read is not None and next_read < end:
            snapshots.append((next_read, _desktop_projection(reader, next_read)))
            next_read = next(checkpoints, None)
        elapsed = end

    class Desktop:
        platform = "darwin"
        errors = {}

        def __init__(self, **_kwargs):
            pass

        def sample(self):
            phase = _phase(elapsed)
            identity = {}
            if "app_id" in ActivitySample.__dataclass_fields__:
                identity["app_id"] = {"Visual Studio Code": "com.microsoft.VSCode", "Mochi": "io.github.mochi.deskpet"}.get(phase.app)
            title = phase.title
            if args.dirty_at is not None and elapsed >= args.dirty_at and phase.document == "Alpha.md":
                title = f"● {title}"
            return ActivitySample(_at(elapsed), elapsed, phase.app, phase.window, title,
                                  idle_s=phase.idle, screen_locked=phase.locked, session_id=phase.session,
                                  private_window=False, **identity)

    class VirtualExecutor:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc_info):
            pass

        def submit(self, operation, *parameters, **kwargs):
            delay = args.late_vision_s if mode != "split" and not model.attempts else 0.0
            ready_at = elapsed + delay

            class ResponseFuture(Future):
                def done(self):
                    return elapsed >= ready_at and super().done()

            future = ResponseFuture()
            try:
                future.set_result(operation(*parameters, **kwargs))
            except Exception as exc:
                future.set_exception(exc)
            return future

    def capture(sample, *, platform):
        phase = _phase(elapsed)
        assert platform == "darwin" and sample.window_id == phase.window and sample.screen_locked is False
        captures.append({"at_s": elapsed, "window": phase.window})
        return _at(elapsed), _screen(phase)

    model = CountedModel(ScriptedModel(), clock=lambda: elapsed)

    def policy():
        return None if args.revoke_at is not None and elapsed >= args.revoke_at else 0

    options = {"duration_s": DURATION_S, "capture_screen": True, "capture_policy": policy}
    with (patch.object(pipeline, "datetime", Clock),
          patch.object(pipeline, "time", SimpleNamespace(monotonic=lambda: elapsed, sleep=sleep)),
          patch.object(pipeline, "system_boot_id", lambda: SIMULATED_BOOT),
          patch.object(pipeline, "NativeActivitySampler", Desktop),
          patch.object(pipeline, "ThreadPoolExecutor", VirtualExecutor),
          patch.object(media, "capture_window", capture)):
        if mode == "split":
            report = pipeline.run_live(database, model, StateObserver(model), **options)
        else:
            report = _run_desktop_multimodal(database, model, mode, options)
    observations = _rows(database, "effective_observations")
    screens = _rows(database, "screen_observations")
    activities = _rows(database, "activities")
    checks = _desktop_checks(snapshots)
    return {
        "evidence": "actual producer and reader; simulated OS clock, windows, pixels and model replies", "mode": mode,
        "duration_s": DURATION_S, "captures": captures, "model_attempts": model.attempts,
        "fixed_slots_s": [0, 30, 60] if mode == "fixed" else None,
        "first_merged_result_delay_s": args.late_vision_s if mode != "split" else None,
        "revoke_capture_at_s": args.revoke_at,
        "native_dirty_marker_at_s": args.dirty_at,
        "document_changes": [{key: row.get(key) for key in ("activity_id", "document_name", "document_change",
                             "document_change_at")} for row in activities if row.get("document_change")],
        "pipeline_counts": {key: report.get(key) for key in ("activities", "screens", "observations", "state_runs")},
        "published_states": dict(Counter(row["state"] for row in observations)),
        "screens": [{key: row.get(key) for key in ("activity_id", "captured_at", "processed_at", "status",
                    "screen_kind", "document_name", "realtime_eligible")} for row in screens],
        "answerable_checkpoints": sum(point["answerable"] is True for point in checks),
        "rateable_checkpoints": sum(point["answerable"] is not None for point in checks),
        "stale_current_checkpoints": sum(point["stale_current"] is True for point in checks),
        "current_activity_claims": sum(point["relation"] == "current" for point in checks),
        "aged_scenes_in_current_activity": sum(point["aged_scene_in_current_activity"] is True for point in checks),
        "missing_metadata_state_violations": sum(point["missing_metadata_state_violation"] for point in checks),
        "ungranted_document_occurrences": sum(point["ungranted_document_occurrences"] for point in checks),
        "ungranted_unsaved_markers": sum(point["ungranted_unsaved_markers"] for point in checks),
        "checkpoints": checks,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New output directory")
    parser.add_argument("--suite", choices=("desktop", "public"), required=True)
    parser.add_argument("--arm", action="append", choices=MODES)
    parser.add_argument("--gui-world", type=Path, help="Frozen GUI-World evaluation bundle with case-manifest.json")
    parser.add_argument("--case", action="append", help="Case IDs from the frozen manifest")
    parser.add_argument("--ollama", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default="qwen3.5:2b")
    parser.add_argument("--model-timeout", default=60.0, type=float)
    parser.add_argument("--public-interval", default=4.0, type=float)
    parser.add_argument("--max-frames", default=16, type=int)
    parser.add_argument("--late-vision-s", default=0.0, type=float, help="Desktop: virtual delay for the first merged result")
    parser.add_argument("--revoke-at", type=float, help="Desktop: revoke image capture at this virtual second")
    parser.add_argument("--dirty-at", type=float, help="Desktop: switch Alpha's native window to an unsaved marker")
    args = parser.parse_args()
    modes = args.arm or MODES
    if args.suite == "public":
        if args.gui_world is None:
            parser.error("--gui-world is required for the public suite")
        manifest = json.loads((args.gui_world / "case-manifest.json").read_text())
        cases = {case["case_id"]: case for case in manifest["cases"]}
        case_ids = args.case or PUBLIC_CASES
        if any(case_id not in cases for case_id in case_ids):
            parser.error("Unknown frozen case ID")
    args.output.mkdir()
    results = []
    for mode in modes:
        if args.suite == "desktop":
            results.append(desktop_replay(args, mode))
        else:
            for case_id in case_ids:
                results.append(public_replay(args, mode, cases[case_id], args.gui_world))
                (args.output / "report.json").write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n")
    (args.output / "report.json").write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
