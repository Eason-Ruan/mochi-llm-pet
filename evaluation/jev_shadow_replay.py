"""Replay pre-labeled synthetic ticks through the Jev shadow gate and optional Bailian System 2."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from math import ceil, isfinite
from pathlib import Path

from deskpet.system2.semantic_policy import semantic_background_grants


START = datetime(2026, 11, 25, 12, 0, tzinfo=timezone(timedelta(hours=8)))
MAY_DEFER = "may_defer"
NEEDS_SYSTEM2 = "needs_system2"
BYPASS = "hard_bypass"

REPORT = "编辑实验报告，正在核对结果章节并填入实验图表。"
REPORT_LATER = "继续编辑同一份实验报告，图表旁的结果分析已增加一段。"
CODE = "在代码编辑器为网页布局修改样式，预览页显示导航栏。"
MAIL = "切到邮箱编写与网页布局无关的会议议程。"
BILI_CLIP = "哔哩哔哩正在播放搞笑综艺剪辑，与实验报告无关。"
BILI_LATER = "哔哩哔哩仍在播放同一个搞笑综艺剪辑，播放器进度向前。"
BILI_COURSE = "哔哩哔哩正播放实验课程讲解，内容对应实验报告的结果分析。"
UNCLEAR = "活动分类提示工作，页面内容不足以确认具体目标。"
SCENE_ELEMENTS = {
    REPORT: ("document editor", "document outline", "embedded chart"),
    REPORT_LATER: ("document editor", "document outline", "embedded chart"),
    CODE: ("code editor", "file explorer"),
    MAIL: ("web page", "form", "navigation sidebar"),
    BILI_CLIP: ("video player", "playback timeline"),
    BILI_LATER: ("video player", "playback timeline"),
    BILI_COURSE: ("lecture video", "playback timeline", "subtitles"),
}


@dataclass(frozen=True)
class Case:
    id: str
    scenario: str
    tick: int
    expected: str
    annotation: str
    event: dict
    context: dict
    system2_case: str = "quiet_tick"

    def system2_payload(self) -> dict:
        return dict(origin="background", event=self.event, context=self.context, user_intent=None,
                    semantic_chat=False, semantic_background=True,
                    grants=semantic_background_grants(self.context))


def _visual(as_of, kind, text, *, late=False):
    at = as_of.isoformat()
    return dict(observed_at=at, activity_kind=kind, summary=text, late=late,
                scene_elements=list(SCENE_ELEMENTS.get(text, ())),
                summary_observed_at=at, scene_observed_at=at)


def _segments(frames):
    return [dict(first_observed_at=frame["observed_at"], last_observed_at=frame["observed_at"],
                 activity_kind=frame["activity_kind"], state="working", summary=frame["summary"],
                 scene_observed_at=frame["observed_at"], summary_observed_at=frame["observed_at"],
                 sample_count=1, visual_observations=[frame]) for frame in frames]


def _window(at, duration, frames):
    start = at - timedelta(seconds=duration)
    seconds = dict(work=0, study=0, leisure=0, other=0, unknown=0, unobserved=duration)
    # Each explicit capture represents at most one 30-second sampling interval.
    through = at
    for frame in reversed(frames):
        stamp = datetime.fromisoformat(frame["observed_at"])
        coverage = max(0, int((min(through, stamp + timedelta(seconds=30)) - max(stamp, start)).total_seconds()))
        seconds[frame["activity_kind"]] += coverage
        seconds["unobserved"] -= coverage
        through = min(through, stamp)
    return dict(from_at=start.isoformat(), to_at=at.isoformat(), duration_s=duration, seconds=seconds,
                percent={key: round(value * 100 / duration, 2) for key, value in seconds.items()},
                representative_summaries=[dict(observed_at=frame["observed_at"], activity_kind=frame["activity_kind"],
                                               summary=frame["summary"]) for frame in frames[-3:]])


def _case(id, scenario, tick, expected, annotation, samples, *, focus=False, pending=None,
          gap=None, locked=False, reminder=False, system2_case="quiet_tick"):
    at = START + timedelta(minutes=tick)
    stamp = at.isoformat()
    # The stamps in samples are absolute within a scenario. A later tick can see the same capture again.
    frames = [_visual(START + timedelta(seconds=offset), kind, text) for offset, kind, text in samples]
    last = frames[-1]
    visible = datetime.fromisoformat(last["observed_at"])
    current = dict(observed_at=visible.isoformat(), state="working", activity_kind=last["activity_kind"],
                   summary=last["summary"], reason_codes=[], context_revision=1 + tick,
                   valid_until=(at + timedelta(seconds=20)).isoformat())
    phase = dict(id="synthetic-focus", version=1, kind="focus", state="running",
                 begin_at=(START - timedelta(minutes=5)).isoformat(), title="完成实验报告",
                 goal="写报告", planned_duration_s=1500) if focus else None
    proposals, contacts = [], []
    if pending:
        args = ({"id": phase["id"], "expected_version": phase["version"]} if phase else
                {"kind": "focus", "planned_duration_s": 1500})
        proposals.append(dict(id="synthetic-proposal", version=1, function=pending, args=args, state="pending",
                              subject_refs=[f"phase:{phase['id']}"] if phase else [],
                              expected_versions={f"phase:{phase['id']}": phase["version"]} if phase else {},
                              created_at=(at - timedelta(seconds=58)).isoformat(),
                              valid_until=(at + timedelta(minutes=5)).isoformat()))
        contacts.append(dict(kind="proposal", at=(at - timedelta(seconds=20)).isoformat(),
                             delivery_id="synthetic-proposal", proposal_id="synthetic-proposal",
                             subject_ref="proposal:synthetic-proposal", primary_channel="pet_card", channel="pet_card",
                             exposure="painted", surfaces=[dict(channel="pet_card", active=True, exposure="painted")],
                             response="none"))
    focus_from = at - timedelta(minutes=2)
    gaps = [] if gap is None else [dict(from_at=(START + timedelta(seconds=gap[0])).isoformat(),
                                       to_at=(START + timedelta(seconds=gap[1])).isoformat())]
    context = dict(as_of=stamp, timezone="Asia/Shanghai", current=current, current_status="fresh",
                   last_observed_activity=dict(last, relation="current", scene_observed_at=visible.isoformat()),
                   interruptibility=dict(status="fresh", current=current), current_context=dict(screen_locked=locked),
                   history_1h=dict(from_at=(at - timedelta(hours=1)).isoformat(), to_at=stamp,
                                   segments=_segments(frames), gaps=[]),
                   activity_windows=dict(last_15m=_window(at, 900, frames), last_60m=_window(at, 3600, frames)),
                   focus_evidence_2m=dict(from_at=focus_from.isoformat(), to_at=stamp, complete=True, gaps=gaps,
                                          visual_observations=[frame for frame in frames
                                                               if datetime.fromisoformat(frame["observed_at"]) >= focus_from]),
                   current_phase=phase, items=[], habits=[], recheck_items=[], eligible_items=[],
                   pending_proposals=proposals, enabled_settings=dict(auto_adjust=True, version=1, default_focus_s=1500,
                                                                    default_break_s=300),
                   display_capabilities=dict(pet_card=True, pet_badge=True), recent_decisions=[],
                   recent_contacts=contacts, publication_cursor=1 + tick, opportunity_id=None,
                   jev_activity_scope=scenario)
    wakes = [dict(kind="tick", reason="tick", event_id=f"synthetic-{id}", at=stamp, subject_refs=[])]
    if reminder:
        item_id = "synthetic-reminder"
        item_ref = f"item:{item_id}"
        occurrence = f"{item_id}:1"
        item = dict(id=item_id, version=1, title="取洗好的衣服", occurrence_id=occurrence,
                    rule=dict(target_at=stamp), occurrence=dict(id=occurrence, window_open_at=stamp,
                                                                window_close_at=(at + timedelta(minutes=1)).isoformat()))
        context["eligible_items"].append(item)
        wakes.append(dict(kind="reminder_ready", reason="window_open", event_id=f"synthetic-{id}-reminder", at=stamp,
                          subject_refs=[item_ref], occurrence_id=occurrence))
    event = dict(kind="background", at=stamp, kinds=[wake["kind"] for wake in wakes],
                 trigger_reasons=[wake["reason"] for wake in wakes], trigger_ids=[wake["event_id"] for wake in wakes],
                 subject_refs=[ref for wake in wakes for ref in wake["subject_refs"]],
                 occurrence_ids=[wake["occurrence_id"] for wake in wakes if "occurrence_id" in wake], wakes=wakes)
    context["trigger_reasons"] = event["trigger_reasons"]
    return Case(id, scenario, tick, expected, annotation, event, context, system2_case)


def cases() -> tuple[Case, ...]:
    """Fixed labels come from the scripted evidence and are independent of provider outputs."""
    first_work = [(-5, "work", REPORT)]
    sustained_work = [*first_work, (25, "work", REPORT), (55, "work", REPORT_LATER)]
    first_clip = [(-35, "work", REPORT), (-5, "leisure", BILI_CLIP)]
    sustained_clip = [*first_clip, (25, "leisure", BILI_CLIP), (55, "leisure", BILI_LATER)]
    return (
        _case("work-first-frame", "work-start", 0, MAY_DEFER, "Only one work capture", first_work),
        _case("work-sustained-next-tick", "work-start", 1, NEEDS_SYSTEM2,
              "Same report remains active across independent captures and ticks", sustained_work,
              system2_case="focus_start"),
        _case("work-awaiting-response", "work-start", 2, MAY_DEFER,
              "The matching start proposal was painted and is awaiting a response",
              [*sustained_work, (85, "work", REPORT_LATER), (115, "work", REPORT_LATER)], pending="phase.start"),
        _case("work-ambiguous-first-tick", "work-cap", 0, MAY_DEFER,
              "A first review can wait for stronger work evidence", [(-5, "work", UNCLEAR)]),
        _case("work-ambiguous-cap", "work-cap", 1, NEEDS_SYSTEM2,
              "Unresolved work persists, so a second consecutive deferral would exceed the cap",
              [(-5, "work", UNCLEAR), (55, "work", UNCLEAR)]),
        _case("work-independent-scenes", "work-independent", 0, MAY_DEFER,
              "Two work captures describe unrelated goals", [(-35, "work", CODE), (-5, "work", MAIL)]),
        _case("focus-first-bilibili-frame", "focus-bilibili", 0, MAY_DEFER,
              "One entertainment capture after work cannot justify a pause proposal", first_clip, focus=True),
        _case("focus-sustained-bilibili", "focus-bilibili", 1, NEEDS_SYSTEM2,
              "Current Bilibili entertainment continues across independent captures more than 25 seconds apart",
              sustained_clip, focus=True, system2_case="focus_entertainment"),
        _case("focus-awaiting-response", "focus-bilibili", 2, MAY_DEFER,
              "The matching pause proposal was painted and is awaiting a response",
              [*sustained_clip, (85, "leisure", BILI_LATER), (115, "leisure", BILI_LATER)],
              focus=True, pending="phase.pause"),
        _case("focus-duplicate-bilibili-frame", "focus-duplicate", 0, MAY_DEFER,
              "Duplicate timestamps describe one entertainment capture", [*first_clip, first_clip[-1]], focus=True),
        _case("focus-returned-to-work", "focus-return", 0, MAY_DEFER,
              "Older entertainment ended when the current scene returned to the report",
              [(-65, "leisure", BILI_CLIP), (-35, "leisure", BILI_LATER), (-5, "work", REPORT)], focus=True),
        _case("focus-bilibili-course", "focus-course", 0, MAY_DEFER,
              "The current Bilibili course pertains to the active report",
              [(-65, "leisure", BILI_CLIP), (-35, "study", BILI_COURSE), (-5, "study", BILI_COURSE)], focus=True),
        _case("focus-bilibili-gap", "focus-gap", 0, MAY_DEFER,
              "The captures straddle a coverage gap that breaks evidence of continuity",
              [(-65, "leisure", BILI_CLIP), (-5, "leisure", BILI_LATER)], focus=True, gap=(-55, -15)),
        _case("tick-with-reminder-wake", "hard-wake", 0, BYPASS,
              "A coalesced reminder wake is handled by System 2", first_work, reminder=True,
              system2_case="elapsed_and_reminder"),
        _case("locked-tick", "privacy", 0, BYPASS, "Screen lock prevents shadow processing", first_work, locked=True),
    )


TOKEN_FIELDS = ("input_tokens", "output_tokens", "cached_input_tokens")
BAILIAN_USAGE_FIELDS = dict(input_tokens="prompt_tokens", output_tokens="completion_tokens",
                            cached_input_tokens="cached_tokens")


def _known_count(value):
    return value if type(value) is int and value >= 0 else None


def _totals(rows):
    totals = {}
    request_count = sum(row["requests"] for row in rows)
    for field in TOKEN_FIELDS:
        values = [usage[field] for row in rows for usage in row["usage"] if usage.get(field) is not None]
        totals[field] = dict(tokens=sum(values) if values and len(values) == request_count else None,
                             reported_requests=len(values), total_requests=request_count)
    return totals


def _latency(rows):
    samples = sorted(row["latency_ms"] for row in rows if isinstance(row.get("latency_ms"), (int, float))
                     and isfinite(row["latency_ms"]) and row["latency_ms"] >= 0)
    return dict(samples=len(samples), p50_ms=samples[ceil(len(samples) * .50) - 1] if samples else None,
                p95_ms=samples[ceil(len(samples) * .95) - 1] if samples else None)


def _provider_summary(rows):
    return dict(status_counts=dict(Counter(row["status"] for row in rows)), latency=_latency(rows),
                model_attempts=sum(row["attempts"] for row in rows),
                model_requests=sum(row["requests"] for row in rows), usage=_totals(rows))


def _score(rows, decision):
    counts = dict(true_defer=0, false_defer=0, true_send=0, unnecessary_send=0, unscored=0)
    for row in rows:
        detected = decision(row)
        if detected is None:
            counts["unscored"] += 1
        elif detected:
            counts["true_defer" if row["expected"] == MAY_DEFER else "false_defer"] += 1
        else:
            counts["true_send" if row["expected"] == NEEDS_SYSTEM2 else "unnecessary_send"] += 1
    positive = counts["true_send"] + counts["false_defer"]
    deferred = counts["true_defer"] + counts["false_defer"]
    return {**counts, "needs_system2_scored": positive, "false_negative_rate":
            counts["false_defer"] / positive if positive else None, "defer_precision":
            counts["true_defer"] / deferred if deferred else None}


def _avoided_system2(rows):
    measured = [row["system2"] for row in rows if row["system2"]["status"] == "ok"]
    return dict(could_defer_ticks=len(rows), measured_system2_ticks=len(measured),
                system2_requests=sum(result["requests"] for result in measured) if measured else None,
                system2_usage=_totals(measured))


def summarize(rows, mode):
    eligible = [row for row in rows if row["expected"] != BYPASS]
    local_defer = [row for row in eligible if row["local_baseline_could_defer"]]
    host_defer = [row for row in eligible if row["jev"]["could_defer"] is True]
    combined_defer = [row for row in eligible if row["combined_could_defer"] is True]
    added_defer = [row for row in host_defer if not row["local_baseline_could_defer"]]
    system2 = [row["system2"] for row in rows]
    jev = [row["jev"] for row in rows]
    return dict(
        jev_mode=mode, dataset="synthetic_pre_labeled", action_mode="shadow_only",
        model_evidence={"real": "provider_when_available", "fixture": "fixed_stand_in", "off": "none"}[mode],
        percentile_method="nearest_rank",
        latency_scope=dict(jev="provider_client_wall_time", system2="isolated_admission_wall_time"),
        tick_count=len(rows), label_counts=dict(Counter(row["expected"] for row in rows)),
        raw_model=_score(eligible, lambda row: row["jev"]["raw_defer"]),
        host_policy=_score(eligible, lambda row: row["jev"]["could_defer"]),
        local_baseline=_score(eligible, lambda row: row["local_baseline_could_defer"]),
        combined_policy=_score(eligible, lambda row: row["combined_could_defer"]),
        hard_bypasses=dict(expected=len(rows) - len(eligible),
                           observed=sum(row["jev"]["status"] == "hard_bypass" for row in rows),
                           wrongly_deferred=sum(row["expected"] == BYPASS and row["jev"]["could_defer"] is True
                                                for row in rows)),
        jev=_provider_summary(jev), system2=_provider_summary(system2),
        potential_savings=dict(**_avoided_system2(host_defer),
                               real_jev_could_defer_ticks=sum(row["jev"]["requests"] > 0 for row in host_defer)
                               if mode == "real" else None,
                               local_baseline=_avoided_system2(local_defer), combined=_avoided_system2(combined_defer),
                               added_above_local=_avoided_system2(added_defer), extra_jev_usage=_totals(jev)),
        realized_savings=dict(system2_ticks=0, system2_requests=0, system2_input_tokens=0,
                              system2_output_tokens=0, system2_cached_input_tokens=0),
    )


def _system2_status(status):
    return dict(status=status, model=None, prompt_version=None, latency_ms=None, attempts=0, requests=0,
                usage=[], calls=[])


def _system2_calls(record):
    from deskpet.system2.tool_schema import canonical_tool_name, tool_definitions

    known_tools = {tool.canonical_name for tool in tool_definitions}
    safe = []
    for call in record.get("calls", ()):
        tool = canonical_tool_name(call["tool"])
        if tool in known_tools:
            name = call.get("args", {}).get("function")
            function = canonical_tool_name(name) if isinstance(name, str) else None
            safe.append(dict(tool=tool, status=call["status"], function=function if function in known_tools else None))
    return safe


def run_system2(case, settings_path):
    if settings_path is None:
        return _system2_status("not_requested")

    from sqlite3 import Error as SQLiteError
    from time import monotonic

    from deskpet.observation_v2.routes import CloudRouteError
    from deskpet.system2.dsh import System2RunError
    from evaluation.system2_prompt_ab import run_case

    started = monotonic()
    try:
        result = run_case(settings_path, case.system2_case, True, payload=case.system2_payload())
    except System2RunError as error:
        status = "timeout" if error.metadata.get("timed_out") else "provider_failed"
        result = dict(requests=error.metadata.get("model_requests", 0), latency_ms=round((monotonic() - started) * 1000),
                      usage=error.metadata.get("provider_request_usage", ()), version=error.metadata.get("prompt_version"))
    except (CloudRouteError, OSError, SQLiteError, ValueError):
        # Failure strings can contain a credential reference, a path or a provider payload.
        return _system2_status("unavailable")
    else:
        status = "ok"
        result["latency_ms"] = round((monotonic() - started) * 1000)
    return dict(status=status, model=result.get("model"), prompt_version=result.get("version"),
                latency_ms=result["latency_ms"], attempts=result["requests"], requests=result["requests"],
                calls=_system2_calls(result),
                usage=[{key: _known_count(item.get(field)) for key, field in BAILIAN_USAGE_FIELDS.items()}
                       for item in result["usage"]])


class FixtureAdvisor:
    """Return a fixed response to exercise the host's fail-open paths without a provider."""

    def adjudicate(self, settings, state, *, criteria, send_permitted=None):
        from deskpet.system2.jev_api import JevResult

        return JevResult(status="ok", latency_ms=0, choice="defer", confidence=1.0)


def _advisor(mode):
    from deskpet.system2.jev_api import JevClient, JevSettings, load_jev_settings

    if mode == "real":
        settings = load_jev_settings()
        return settings, JevClient()
    if mode == "fixture":
        return JevSettings(enabled=True, share_text=True, api_key="offline-fixture"), FixtureAdvisor()
    return JevSettings(), None


def _jev_status(status, *, reason=None, could_defer=False):
    return dict(status=status, host_reason=reason, model=None, choice=None, confidence=None, probabilities=None,
                raw_defer=None, could_defer=could_defer, latency_ms=None, attempts=0, requests=0, usage=[])


def _jev_result(result, host_choice, host_reason, mode):
    status = result.status if result else "local_decision"
    row = _jev_status(status, reason=host_reason, could_defer=host_choice == "defer")
    if result is None:
        return row
    row.update(model=result.model, choice=result.choice, confidence=result.confidence, probabilities=result.probabilities,
               raw_defer=result.choice == "defer" if result.status == "ok" else None)
    if result.status == "http_error":
        row["http_status"] = result.http_status
    if mode == "real" and result.status not in {"disabled", "invalid_request"}:
        row.update(latency_ms=round(result.latency_ms, 1), attempts=1)
    if mode == "real" and (result.send_started_at is not None or result.status in {"ok", "http_error", "invalid_response"}):
        row.update(requests=1,
                   usage=[dict(input_tokens=_known_count(result.input_tokens),
                               output_tokens=_known_count(result.output_tokens),
                               cached_input_tokens=_known_count(getattr(result, "cached_input_tokens", None)))])
    return row


def replay(selected, mode, *, system2_settings=None, progress=None):
    from deskpet.system2.jev_projection import project_jev_snapshot
    from deskpet.system2.jev_shadow import adjudicate_jev, is_pure_tick, jev_cap_key, jev_cap_reached

    settings, advisor = _advisor(mode)
    rows = []
    scenario = None
    for case in selected:
        if case.scenario != scenario:
            last_review, fixture_review, previous = None, None, None
            previous_local = None
            scenario = case.scenario
        context = {**case.context, "jev_exact_feedback_provenance": True}
        if fixture_review is not None and context["pending_proposals"]:
            context["pending_proposals"] = [
                {**offer, "jev_origin_activity": fixture_review.activity,
                 "jev_origin_review_at": fixture_review.as_of, "jev_displayed": True}
                for offer in context["pending_proposals"]
            ]
        now = datetime.fromisoformat(context["as_of"])
        snapshot = project_jev_snapshot(context, next_tick_in_s=60.0, share_text=settings.share_text,
                                        last_review=last_review)
        if not is_pure_tick(case.event):
            jev = _jev_status("hard_bypass", reason="non_tick_wake")
        elif case.context["current_context"]["screen_locked"] is True:
            jev = _jev_status("hard_bypass", reason="screen_locked")
        elif mode == "off":
            jev = _jev_status("not_requested", reason="replay_jev_off", could_defer=None)
        elif not settings.available:
            jev = _jev_status("provider_unconfigured", reason="jev_settings_unavailable", could_defer=None)
        else:
            reached_cap = jev_cap_reached(snapshot, previous, as_of=now)
            result, host_choice, host_reason = adjudicate_jev(settings, advisor, snapshot, cap_reached=reached_cap,
                                                             send_permitted=lambda: True)
            jev = _jev_result(result, host_choice, host_reason, mode)
            previous = dict(tick_at=now.isoformat(), cap_key=jev_cap_key(snapshot), host_choice=host_choice,
                            host_reason=host_reason)
        system2 = run_system2(case, system2_settings)
        fixture_review = snapshot.review
        if system2["status"] in {"ok", "not_requested"}:
            # Shadow always keeps the baseline review. With no network baseline, its completion is scripted.
            last_review = fixture_review
        local_reason = snapshot.local_defer_reason if jev["status"] != "hard_bypass" else None
        if local_reason == "unchanged_snapshot" and jev_cap_reached(snapshot, previous_local, as_of=now):
            local_reason = "defer_cap"
        local_defer = local_reason in {"no_candidate", "pending_offer", "unchanged_snapshot"}
        previous_local = None
        if local_defer:
            previous_local = dict(tick_at=now.isoformat(), cap_key=jev_cap_key(snapshot), host_choice="defer")
        row = dict(case=case.id, scenario=case.scenario, tick=case.tick, expected=case.expected,
                   annotation=case.annotation, jev=jev, system2=system2, local_baseline_could_defer=local_defer,
                   local_baseline_reason=local_reason,
                   combined_could_defer=True if local_defer else jev["could_defer"])
        rows.append(row)
        if progress is not None:
            progress(row)
    summary = summarize(rows, mode)
    summary["jev_text_scope"] = "synthetic_summaries" if settings.share_text else "structured_facts_only"
    summary["prior_review_source"] = "bailian" if system2_settings is not None else "scripted_baseline_completion"
    return dict(summary=summary, rows=rows)


def _print_row(row):
    jev, system2 = row["jev"], row["system2"]
    print(row["case"], row["expected"],
          "jev=" + jev["status"], "choice=" + str(jev["choice"] or "-"), "gate=" + str(jev["host_reason"] or "-"),
          "could_defer=" + str(jev["could_defer"]), "system2=" + system2["status"],
          "system2_requests=" + str(system2["requests"]), flush=True)


def main():
    choices = cases()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jev", choices=("off", "fixture", "real"), default="off",
                        help="Use no Jev, a fixed offline stand-in, or the configured real Jev API")
    parser.add_argument("--system2-settings", type=Path,
                        help="Read-only SQLite settings with a verified Bailian route; calls the real API on synthetic data")
    parser.add_argument("--case", action="append", choices=[case.id for case in choices],
                        help="Select a tick's entire scenario to preserve prior tick and cap history")
    parser.add_argument("--output", type=Path, required=True, help="New JSON file for sanitized per-tick results")
    args = parser.parse_args()
    scenarios = {case.scenario for case in choices if case.id in (args.case or ())}
    selected = [case for case in choices if not scenarios or case.scenario in scenarios]
    with args.output.open("x", encoding="utf-8") as report:
        results = replay(selected, args.jev, system2_settings=args.system2_settings, progress=_print_row)
        json.dump(results, report, ensure_ascii=False, indent=2, allow_nan=False)
        report.write("\n")


if __name__ == "__main__":
    main()
