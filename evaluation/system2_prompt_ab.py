"""Compare complete, isolated DSH admissions on the configured Bailian route using synthetic text."""

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from time import monotonic
from unittest.mock import patch

from deskpet.mocks.clock import FakeClock
from deskpet.mocks.delivery import FakeDeliveryPort
from deskpet.observation_v2.routes import BAILIAN, SYSTEM2, CloudRoute, _capability_probe_version
from deskpet.settings import SettingsStore
from deskpet.system2.dsh import DSHEngine
from deskpet.system2.dsh_projection import window_within_range
from deskpet.system2.intent import chat_grants, materialize_chat_times
from deskpet.system2.items import ItemService
from deskpet.system2.prompt import select_prompt
from deskpet.system2.protocol import TurnRequest
from deskpet.system2.semantic_policy import semantic_background_grants
from deskpet.system2.tool_schema import READ_TOOLS, canonical_tool_name, tool_definitions, validate_tool_args
from deskpet.task_service.sqlite import SQLiteTaskService


AS_OF = datetime(2026, 11, 25, 12, 0, tzinfo=timezone(timedelta(hours=8)))
CASES = ("quiet_tick", "focus_returned_to_work", "focus_entertainment", "elapsed_and_reminder", "chat_hour", "chat_reminder")


def ago(seconds):
    return (AS_OF - timedelta(seconds=seconds)).isoformat()


def window(duration, work, leisure):
    seconds = dict(work=work, study=0, leisure=leisure, other=0, unknown=0,
                   unobserved=duration - work - leisure)
    return dict(from_at=ago(duration), to_at=ago(0), duration_s=duration, seconds=seconds,
                percent={k: round(v * 100 / duration, 2) for k, v in seconds.items()},
                representative_summaries=[dict(observed_at=ago(5), activity_kind="work", summary="编辑课程报告。")])


def fixture(case):
    current = dict(observed_at=ago(5), state="working", activity_kind="work", context_revision=3)
    frames = [dict(observed_at=ago(s), activity_kind=kind, summary=text, scene_elements=[], late=False)
              for s, kind, text in [(90, "leisure", "浏览娱乐视频。"), (50, "leisure", "观看娱乐视频。"),
                                    (5, "work", "已回到课程报告编辑器。")]]
    if case == "focus_entertainment":
        current["activity_kind"] = "leisure"
        frames[-1].update(activity_kind="leisure", summary="继续观看搞笑综艺片段，与课程报告无关。")
    segments = [dict(first_observed_at=f["observed_at"], last_observed_at=f["observed_at"],
                     activity_kind=f["activity_kind"], summary=f["summary"], sample_count=1,
                     visual_observations=[f]) for f in frames]
    focus = case.startswith("focus_")
    context = dict(as_of=ago(0), timezone="Asia/Shanghai", current=current, current_status="fresh",
                   last_observed_activity=dict(frames[-1], relation="current", scene_observed_at=ago(5)),
                   interruptibility=dict(status="fresh", current=current), current_context=dict(screen_locked=False),
                   history_1h=dict(from_at=ago(3600), to_at=ago(0), segments=segments, gaps=[]),
                   activity_windows=dict(last_15m=window(900, 300, 480), last_60m=window(3600, 2160, 900)),
                   focus_evidence_2m=dict(from_at=ago(120), to_at=ago(0), complete=True, gaps=[],
                                          visual_observations=frames),
                   current_phase=dict(id="example-phase", version=1, kind="focus", state="running",
                                      begin_at=ago(500), title="写课程报告") if focus else None,
                   items=[], habits=[], recheck_items=[], eligible_items=[], pending_proposals=[],
                   enabled_settings=dict(auto_adjust=focus, default_focus_s=1500, default_break_s=300),
                   display_capabilities=dict(pet_card=True, pet_badge=True), recent_decisions=[], recent_contacts=[])
    if case.startswith("chat_"):
        source = "user"
        event = dict(kind="user_message", at=ago(0), trigger_ids=["synthetic-chat"], trigger_reasons=["user_message"])
        intent = dict(utterance=("过去一小时我主要在做什么？告诉我工作、娱乐和缺测各占多少。"
                                if case == "chat_hour" else "五分钟后准点提醒我拿快递。"),
                      message_id="example-message", requested_at=ago(0), timezone="Asia/Shanghai")
        grants = chat_grants(intent, context)
    else:
        source, intent = "background", None
        event = dict(kind="background", at=ago(0), kinds=["tick"], trigger_reasons=["tick"],
                     trigger_ids=["example-tick"], subject_refs=[], occurrence_ids=[],
                     wakes=[dict(kind="tick", reason="tick", event_id="example-tick", at=ago(0), subject_refs=[])])
        if case == "elapsed_and_reminder":
            successor = dict(id="example-break", version=1, kind="break", state="running", begin_at=ago(5))
            elapsed = dict(phase_id="example-phase", phase_version=2, kind="focus", successor=successor)
            occurrence = "example-laundry:1"
            item = dict(id="example-laundry", version=1, title="取洗好的衣服", occurrence_id=occurrence,
                        rule=dict(target_at=ago(0)), occurrence=dict(id=occurrence, window_open_at=ago(60),
                                                                     window_close_at=ago(-60)))
            context.update(current_phase=successor, phase_elapsed=[elapsed], eligible_items=[item])
            event["wakes"] += [
                dict(kind="recheck", reason="phase_elapsed", event_id="example-elapsed", at=ago(5),
                     subject_refs=["phase:example-phase"], phase_elapsed=elapsed),
                dict(kind="reminder_ready", reason="window_open", event_id="example-open", at=ago(0),
                     occurrence_id=occurrence, subject_refs=["item:example-laundry"]),
            ]
            event.update(kinds=[w["kind"] for w in event["wakes"]], trigger_reasons=[w["reason"] for w in event["wakes"]],
                         trigger_ids=[w["event_id"] for w in event["wakes"]], occurrence_ids=[occurrence],
                         subject_refs=["phase:example-phase", "item:example-laundry"], phase_elapsed=[elapsed])
        grants = semantic_background_grants(context)
    return dict(origin=source, event=event, semantic_chat=source == "user", semantic_background=source == "background",
                context=context, user_intent=intent, grants=grants)


def configured_route(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute("SELECT route, permission, verified, route_generation, consent_generation "
                         "FROM observation_cloud_routes WHERE role=? AND user_scope='default'", (SYSTEM2,)).fetchone()
    if not row or not row[1] or row[2] != _capability_probe_version(SYSTEM2):
        raise ValueError("A verified and permitted Bailian System 2 route is required")
    route = CloudRoute(**json.loads(row[0]))
    if route.provider_kind != BAILIAN:
        raise ValueError("The selected System 2 route must use Bailian")
    return route, row[3], row[4]


def run_case(settings_path, case, split):
    route, generation, consent = configured_route(settings_path)

    def marker():
        current, current_generation, current_consent = configured_route(settings_path)
        if (current, current_generation, current_consent) != (route, generation, consent):
            raise ValueError("Bailian route changed during the comparison")
        return ("synthetic", 0, False)

    with TemporaryDirectory(prefix="mochi-prompt-ab-") as temp, patch.dict(
        os.environ, MOCHI_SYSTEM2_SEMANTIC_BACKGROUND="1", MOCHI_SYSTEM2_SPLIT_PROMPT=str(int(split)),
    ):
        settings = SettingsStore(Path(temp) / "business.sqlite3")
        snapshot = settings.routes.configure(SYSTEM2, route)
        snapshot = settings.routes.set_permission(SYSTEM2, True, expected=snapshot)
        settings.routes.record_result(snapshot, probe=True)
        payload = fixture(case)
        ctx, source, grants = payload["context"], payload["origin"], payload["grants"]
        selection = select_prompt(source, payload["event"], ctx, grants, semantic_background=source == "background")
        expected_version = "system2.zh.v14" if split else "system2.zh.v13"
        if selection.version != expected_version:
            raise ValueError(f"Scenario {case} selected {selection.version} instead of {expected_version}")
        turn = TurnRequest("example-admission", "example-step", selection.text,
                           json.dumps(payload, ensure_ascii=False), "default", selection.version, source,
                           allowed_tools=tuple(sorted(READ_TOOLS | grants.keys())), prompt_selection=selection)
        engine = DSHEngine(temp, settings=settings, tools=tool_definitions, observation_guard=marker)
        calls, receipts = [], {}

        def synthetic_result(name, call_id, args):
            if name == "command_get_result":
                receipt = receipts.get(args.get("command_id"))
                if receipt is not None:
                    return dict(status="applied", view=receipt, call_id=call_id)
                return dict(status="rejected", error_code="NOT_AUTHORIZED", call_id=call_id)
            if name == "context_read":
                if window_key := args.get("activity_window"):
                    at = datetime.fromisoformat(args["as_of"]) if args.get("as_of") else AS_OF
                    start = datetime.fromisoformat(args["history_start"]) if args.get("history_start") else None
                    end = datetime.fromisoformat(args["history_end"]) if args.get("history_end") else None
                    if not window_within_range(window_key, at, start, end):
                        return dict(status="rejected", error_code="INVALID_ARGUMENT", call_id=call_id)
                return dict(status="applied", view=ctx, call_id=call_id)
            if name == "item_list":
                return dict(status="applied", view=ctx["eligible_items"], call_id=call_id)
            if name == "item_get":
                matches = [item for item in ctx["eligible_items"] if item["id"] == args.get("id")]
                if matches:
                    return dict(status="applied", view=matches[0], call_id=call_id)
            if name == "settings_get" and args.get("scope", "default") == "default":
                return dict(status="applied", view=ctx["enabled_settings"], call_id=call_id)
            if name == "habit_list":
                return dict(status="applied", view=[], call_id=call_id)
            if name == "phase_get":
                return dict(status="applied", view=ctx["current_phase"], call_id=call_id)
            if name == "item_create" and case == "chat_reminder":
                materialized = materialize_chat_times("item.create", args, AS_OF, "Asia/Shanghai")
                clock = FakeClock(AS_OF)
                with closing(SQLiteTaskService(clock, FakeDeliveryPort(clock), lambda *_, **__: None,
                                               path=Path(temp) / "tasks.sqlite3", auto_opportunity=False)) as tasks:
                    items = ItemService(tasks, timezone="Asia/Shanghai")
                    return {**items.execute("item.create", materialized, command_id=call_id, expected_version=0),
                            "call_id": call_id}
            if name == "proposal_offer" and case == "focus_entertainment" and args.get("function") in ("phase.pause", "phase_pause"):
                return dict(status="pending", outcome="pending_display", call_id=call_id, delivery_id="example-card")
            if name == "reminder_present" and case == "elapsed_and_reminder":
                return dict(status="pending", outcome="pending_display", call_id=call_id, delivery_id="example-reminder")
            if name in {"observation_wait_fresh", "decision_recheck", "reminder_recheck"}:
                return dict(status="applied", outcome="deferred", call_id=call_id)
            return dict(status="rejected", error_code="NOT_AUTHORIZED", call_id=call_id)

        def invoke(name, call_id, args, **_):
            try:
                normalized = validate_tool_args(canonical_tool_name(name), args, relative_time=source == "user")
                receipt = synthetic_result(name, call_id, normalized)
            except (TypeError, ValueError):
                receipt = dict(status="rejected", error_code="INVALID_ARGUMENT", call_id=call_id)
            receipts[call_id] = receipt
            calls.append({"tool": name, "args": args, "status": receipt["status"], "error_code": receipt.get("error_code")})
            return receipt

        start = monotonic()
        try:
            result = engine.run(turn, invoke)
        finally:
            engine.close()
        md = result.metadata
        usage = md["provider_request_usage"]
        if md["prompt_version"] != expected_version or not usage or md["model_requests"] != len(usage) or any(
            "prompt_tokens" not in item for item in usage
        ):
            raise ValueError("Incomplete provider usage; cannot score this admission")
        return dict(case=case, version=md["prompt_version"], model=route.model, elapsed_s=round(monotonic() - start, 2),
                    requests=md["model_requests"], usage=usage, prompt_tokens=sum(u["prompt_tokens"] for u in usage),
                    native_tool_calls=md["tool_calls"], calls=calls, answer=result.text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, required=True, help="Read only the verified System 2 route")
    parser.add_argument("--case", choices=CASES, action="append")
    parser.add_argument("--output", required=True, type=Path, help="New JSONL file with provider usage and synthetic results")
    options = parser.parse_args()
    with options.output.open("x", encoding="utf-8") as report:
        for index, case in enumerate(options.case or CASES):
            for split in ((False, True) if index % 2 == 0 else (True, False)):
                record = run_case(options.settings, case, split)
                report.write(json.dumps(record, ensure_ascii=False) + "\n")
                report.flush()
                print(case, record["version"], record["requests"], record["prompt_tokens"], flush=True)


if __name__ == "__main__":
    main()
