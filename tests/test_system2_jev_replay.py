import json

from deskpet.system2.jev_api import JevResult
from deskpet.system2.jev_projection import project_jev_snapshot
from evaluation.jev_shadow_replay import BYPASS, MAY_DEFER, NEEDS_SYSTEM2, REPORT, _case, _jev_result, cases, replay


def test_offline_replay_exercises_bypasses_and_cap_without_usage():
    results = replay(cases(), "fixture")
    rows = {row["case"]: row for row in results["rows"]}
    cap = rows["work-ambiguous-cap"]

    assert cap["expected"] == NEEDS_SYSTEM2
    assert cap["jev"]["raw_defer"] is True
    assert cap["jev"]["could_defer"] is False
    assert cap["jev"]["host_reason"] == "defer_cap"
    for key in ("work-awaiting-response", "focus-awaiting-response"):
        assert rows[key]["expected"] == MAY_DEFER
        assert rows[key]["jev"]["could_defer"] is True
        assert rows[key]["jev"]["host_reason"] == "pending_offer"
        assert rows[key]["local_baseline_could_defer"] is True
    for key in ("tick-with-reminder-wake", "locked-tick"):
        assert rows[key]["expected"] == BYPASS
        assert rows[key]["jev"]["status"] == "hard_bypass"
        assert rows[key]["jev"]["raw_defer"] is None

    summary = results["summary"]
    assert summary["model_evidence"] == "fixed_stand_in"
    assert summary["jev"]["model_requests"] == 0
    assert summary["jev"]["latency"]["samples"] == 0
    assert summary["jev"]["usage"]["input_tokens"]["tokens"] is None
    assert summary["realized_savings"]["system2_requests"] == 0
    assert summary["potential_savings"]["local_baseline"]["could_defer_ticks"] == 2
    assert summary["potential_savings"]["added_above_local"]["could_defer_ticks"] == 8
    assert "编辑实验报告" not in json.dumps(results, ensure_ascii=False)


def test_unconfigured_provider_is_distinct_from_hard_bypasses(monkeypatch):
    monkeypatch.delenv("MOCHI_JEV_SHADOW", raising=False)
    results = replay(cases(), "real")

    assert results["summary"]["jev"]["status_counts"] == {"provider_unconfigured": 13, "hard_bypass": 2}
    assert results["summary"]["raw_model"]["unscored"] == 13
    assert results["summary"]["host_policy"]["unscored"] == 13
    for key in ("raw_model", "host_policy"):
        assert results["summary"][key]["false_negative_rate"] is None
    assert results["summary"]["local_baseline"]["true_defer"] == 2
    assert results["summary"]["combined_policy"]["unscored"] == 11
    assert results["summary"]["potential_savings"]["system2_requests"] is None


def test_local_baseline_defers_one_unchanged_tick_with_jev_off():
    selected = []
    for tick, expected in enumerate((NEEDS_SYSTEM2, MAY_DEFER, NEEDS_SYSTEM2, MAY_DEFER)):
        samples = [(tick * 60 - offset, "work", REPORT) for offset in (35, 5)]
        selected.append(_case(f"same-report-{tick}", "same-report", tick, expected, "Repeated report", samples))

    results = replay(selected, "off")
    rows = results["rows"]

    assert [row["local_baseline_could_defer"] for row in rows] == [False, True, False, True]
    assert [row["local_baseline_reason"] for row in rows] == [None, "unchanged_snapshot", "defer_cap",
                                                              "unchanged_snapshot"]
    assert all(row["jev"]["could_defer"] is None for row in rows)
    assert results["summary"]["local_baseline"]["false_defer"] == 0
    assert results["summary"]["host_policy"]["unscored"] == 4


def test_shadow_wire_distinguishes_video_course_and_report():
    fixtures = {case.id: case for case in cases()}

    def state(case_id):
        return project_jev_snapshot(fixtures[case_id].context, next_tick_in_s=60, share_text=True).state

    entertainment = state("focus-sustained-bilibili")
    course = state("focus-bilibili-course")
    returned = state("focus-returned-to-work")

    assert entertainment["activity"]["kind"] == "leisure"
    assert entertainment["phase"]["goal_summary"] == "Write a report."
    assert "video player" in entertainment["focus_2m"]["captures"][-1]["summary"]
    assert "lecture video" in course["focus_2m"]["captures"][-1]["summary"]
    assert "document editor" in returned["focus_2m"]["captures"][-1]["summary"]
    assert "哔哩哔哩" not in json.dumps(entertainment, ensure_ascii=False)
    assert state("work-sustained-next-tick")["work_evidence"]["continuous"] is True
    assert state("work-independent-scenes")["work_evidence"]["continuous"] is False


def test_presend_timeout_is_an_attempt_without_a_transmitted_request():
    row = _jev_result(JevResult(status="timeout", latency_ms=1500), "wake_system2", "timeout", "real")

    assert row["attempts"] == 1
    assert row["requests"] == 0
    assert row["usage"] == []
    assert row["latency_ms"] == 1500
    assert row["raw_defer"] is None
    assert row["could_defer"] is False
