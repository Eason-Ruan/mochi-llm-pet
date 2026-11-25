from pathlib import Path

import pytest

from deskpet.system2 import jev_api
from evaluation import jev_active_gate_replay as replay
from evaluation.jev_prompt_ab import BOUNDARY, CANDIDATE_DECISIONS, CANDIDATE_INSTRUCTIONS, SAVINGS, SMOKE
from evaluation.jev_shadow_replay import MAY_DEFER, REPORT, _case, _system2_status


SETTINGS_PATH = Path("unused-settings.sqlite3")
JEV_SETTINGS = jev_api.JevSettings(enabled=True, share_text=True, api_key="test-dummy")


def _result(status="ok"):
    return {
        **_system2_status(status),
        "model": "qwen4-max",
        "prompt_version": "system2.zh.v14",
        "requests": 1,
        "attempts": 1,
        "latency_ms": 200,
        "usage": [{"input_tokens": 100, "output_tokens": 10, "cached_input_tokens": 50}],
    }


def _decision(choice="defer", confidence=0.80):
    return jev_api.JevResult(status="ok", latency_ms=20, model="jev-1.14.0", choice=choice,
                             confidence=confidence, input_tokens=20, output_tokens=2)


class _Advisor:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.states = []

    def adjudicate(self, settings, state, *, criteria, send_permitted):
        assert send_permitted()
        assert criteria == CANDIDATE_DECISIONS
        assert jev_api._INSTRUCTIONS == CANDIDATE_INSTRUCTIONS
        self.states.append(state)
        return next(self.responses)


@pytest.mark.parametrize(("middle_offsets", "last_offsets", "second_route"), [
    ((5,), (35, 5), "jev_defer"),
    ((35, 5), (5,), "local_defer"),
])
def test_shared_cap_blocks_consecutive_unresolved_local_and_model_defers(
        monkeypatch, middle_offsets, last_offsets, second_route):
    timeline = [_case(f"repeat-{tick}", "repeat", tick, MAY_DEFER, "Repeated report",
                      [(tick * 60 - gap, "work", REPORT) for gap in offsets])
                for tick, offsets in enumerate(((35, 5), middle_offsets, last_offsets))]
    advisor = _Advisor([_decision("wake_system2"), _decision()])
    sends = []

    def send(case, settings_path):
        sends.append(case.tick)
        return _result("ok" if case.tick == 0 else "provider_failed")

    monkeypatch.setattr(replay, "run_system2", send)
    rows = replay.replay_timeline("test", timeline, "gated", SETTINGS_PATH, settings=JEV_SETTINGS, advisor=advisor)
    first, middle, last = rows

    assert sends == [0, 2]
    assert [row["route"] for row in rows] == ["send", second_route, "send"]
    assert middle["system2"]["requests"] == 0
    assert last["jev"]["host_reason"] == "defer_cap"
    assert first["last_real_review_at"] is None
    assert first["next_real_review_at"] is not None
    assert middle["last_real_review_at"] == first["next_real_review_at"] == middle["next_real_review_at"]
    assert last["next_real_review_at"] == last["last_real_review_at"] == middle["next_real_review_at"]
    assert len(advisor.states) == (2 if second_route == "jev_defer" else 1)


def test_model_failures_and_low_confidence_send_to_system2(monkeypatch):
    cases = {timeline[0].id: (cohort, timeline) for cohort, timeline in replay.fixed_timelines()}
    sends = []

    def send(case, settings_path):
        sends.append(case.id)
        return _result()

    monkeypatch.setattr(replay, "run_system2", send)
    for case_id, response, expected_reason in (
        ("boundary-01", _decision(confidence=0.79), "low_confidence"),
        ("boundary-02", jev_api.JevResult(status="timeout", latency_ms=1500), "timeout"),
    ):
        cohort, timeline = cases[case_id]
        rows = replay.replay_timeline(cohort, timeline, "gated", SETTINGS_PATH,
                                      settings=JEV_SETTINGS, advisor=_Advisor([response]))
        assert rows[0]["route"] == "send"
        assert rows[0]["jev"]["host_reason"] == expected_reason
    for case_id, reason in (("boundary-05", "evidence_incomplete"), ("report-with-reminder", "non_tick_wake")):
        cohort, timeline = cases[case_id]
        advisor = _Advisor([])
        rows = replay.replay_timeline(cohort, timeline, "gated", SETTINGS_PATH,
                                      settings=JEV_SETTINGS, advisor=advisor)
        assert rows[0]["route"] == "send"
        assert rows[0]["jev"]["host_reason"] == reason
        assert advisor.states == []
    assert sends == ["boundary-01", "boundary-02", "boundary-05", "report-with-reminder"]


def test_scripted_offer_origin_does_not_replace_actual_review(monkeypatch):
    cohort, timeline = next(item for item in replay.fixed_timelines() if item[1][0].id == "boundary-06")
    advisor = _Advisor([_decision(), _decision("wake_system2")])
    monkeypatch.setattr(replay, "run_system2", lambda case, settings_path: _result())
    rows = replay.replay_timeline(cohort, timeline, "gated", SETTINGS_PATH, settings=JEV_SETTINGS, advisor=advisor)

    assert rows[0]["route"] == "jev_defer"
    assert rows[0]["missed_required_wake"] is True
    assert rows[1]["scripted_pending_offer"] is True
    assert rows[1]["last_real_review_at"] is None
    assert advisor.states[1]["last_system2_review_at"] is None
    assert advisor.states[1]["pending_offer"] is True
    assert advisor.states[1]["same_opportunity"] is False
    assert rows[1]["next_real_review_at"] is not None


def test_comparison_uses_actual_provider_requests_and_baseline_tokens_for_skipped_ticks(monkeypatch):
    sends = []

    def send(case, settings_path):
        sends.append(case.id)
        return _result()

    monkeypatch.setattr(replay, "run_system2", send)
    baseline, gated = [], []
    advisor = _Advisor([_decision()] * 25)
    for cohort, timeline in replay.fixed_timelines():
        baseline.extend(replay.replay_timeline(cohort, timeline, "baseline", SETTINGS_PATH))
        gated.extend(replay.replay_timeline(cohort, timeline, "gated", SETTINGS_PATH, settings=JEV_SETTINGS,
                                           advisor=advisor))
    report = replay.compare_rows(baseline, gated)
    summary = report["summary"]
    skipped = [row for row in report["rows"] if row["gated"]["route"] != "send"]

    assert [report["cohorts"][cohort]["baseline"]["tick_count"] for cohort in (SMOKE, SAVINGS, BOUNDARY)] == [6, 12, 7]
    assert len(sends) == 50 - len(skipped)
    assert summary["system2_ticks_avoided"] == summary["system2_requests_saved"] == len(skipped) > 0
    assert summary["hard_bypasses"] == {"expected": 1, "sent": 1, "completed": 1, "wrongly_skipped": []}
    assert summary["token_deltas"]["input_tokens"]["system2_saved"] == 100 * len(skipped)
    assert summary["gated"]["jev"]["usage"]["input_tokens"]["tokens"] == 20 * len(advisor.states)
    assert all(row["avoided_tick_baseline_tokens"]["cached_input_tokens"]["tokens"] == 50 for row in skipped)
    assert "boundary-06" in summary["missed_required_wakes"]
    with pytest.raises(ValueError, match="exactly the 25 frozen"):
        replay.compare_rows(baseline, gated[:-1])
