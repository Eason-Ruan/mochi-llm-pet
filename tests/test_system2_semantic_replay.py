"""Synthetic shadow decisions exercise the two production policy paths offline."""

import json

import pytest

from evaluation.system2_replay import blind_review, load_recordings, read_cases, run_comparison


def test_comparison_preserves_times_and_measures_commits_and_display():
    cases = read_cases()
    report = run_comparison(cases)
    baseline = report["variants"]["baseline"]["metrics"]
    semantic = report["variants"]["semantic"]["metrics"]
    assert baseline["missed_actions"] > semantic["missed_actions"] == 0
    assert semantic["false_actions"] == semantic["unnecessary_interruptions"] == semantic["failed_checks"] == 0
    assert semantic["duplicate_reminder_displays"] == semantic["provider_requests_during_replay"] == 0
    assert semantic["recorded_provider_cost_usd"] is None
    assert set(semantic["wake_kinds"]) == {"tick", "reminder_ready", "recheck", "observation_ready", "resume"}
    assert {"model_unavailable", "parse_failed", "expired_request", "display_layer_blocked", "user_rejected"} <= set(
        semantic["denial_causes"])
    observed = {case["id"]: case["steps"] for case in report["variants"]["semantic"]["scenarios"]}
    colloquial, = observed["colloquial-original-timestamp"]
    assert colloquial["effects"][0]["deadline"] == "2026-11-22T08:46:16+00:00"
    assert colloquial["turns"][0]["at"].startswith("2026-11-22T08:36:16")
    assert observed["meeting-flexible-and-hard-deadline"][2]["turns"] == []
    assert observed["pet-bubble-respects-preferences"][0]["simulated_deliveries"][0]["animation"] == "idle"
    packet, key = blind_review(cases, report, 12)
    assert all(set(case["outputs"]) == {"A", "B"} for case in packet["cases"])
    assert set(key) == {case["id"] for case in cases}
    assert {mapping["A"] for mapping in key.values()} == {"baseline", "semantic"}
    assert "semantic" not in json.dumps(packet)


def test_recorded_model_usage_is_separate_and_raw_reply_is_omitted(tmp_path):
    case = next(case for case in read_cases() if case["id"] == "colloquial-original-timestamp")
    turn = {"reply": "TEST-RAW-CAPTURED-REPLY", "origin": "user", "trigger_reasons": ["user_message"],
            "usage": {"requests": 2, "latency_ms": 700, "cost_usd": 0.02}}
    captures = {variant: {case["id"]: {"request": [turn]}} for variant in ("baseline", "semantic")}
    artifact = tmp_path / "recorded.json"
    value = {"schema": 1, "synthetic_inputs_only": True, "variants": captures}
    artifact.write_text(json.dumps(value), encoding="utf-8")
    report = run_comparison([case], recordings=load_recordings(artifact))
    assert "TEST-RAW-CAPTURED-REPLY" not in json.dumps(report)
    metrics = report["variants"]["semantic"]["metrics"]
    assert metrics["engine_behavior_source"] == "offline_recorded_model"
    assert metrics["provider_requests_during_replay"] == metrics["replay_provider_cost_usd"] == 0
    assert metrics["recorded_provider_requests"] == 2
    assert metrics["recorded_provider_cost_usd"] == 0.02
    assert metrics["recorded_model_latency_ms"] == {"p50": 700, "p95": 700}
    turn["before_calls"] = [{"chat": {"text": "spoof local action"}}]
    artifact.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="cannot define local user events"):
        load_recordings(artifact)


def test_oracle_counts_a_committed_rogue_write_even_when_no_effect_is_expected():
    def scenario(identity, text):
        return {"id": identity, "at": "2026-11-22T17:36:00+08:00", "timezone": "Asia/Shanghai", "steps": [{
            "id": "request", "offset_s": 0, "events": [{"chat": {"text": text}}],
            "engine": {"calls": [{"action": "settings.update", "args": {
                "scope": "default", "patch": {"reminder_sound": False},
            }}]}, "expect": {"effects": []},
        }]}

    # The explicit request provides a committed control even if the runtime rejects the injected read-only case.
    report = run_comparison([scenario("control", "关掉提醒声音"), scenario("rogue", "你觉得今天怎么样？")])
    explicit, rogue = [case["steps"][0] for case in report["variants"]["semantic"]["scenarios"]]
    assert explicit["effects"] == [{"action": "changed_settings", "fields": ["reminder_sound"]}]
    assert explicit["score"]["false_actions"] == ["changed_settings"]
    committed = any(receipt.get("outcome") == "committed" for receipt in rogue["tool_receipts"])
    assert ("changed_settings" in rogue["score"]["false_actions"]) == committed
