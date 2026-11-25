from datetime import datetime
import json

import httpx

from deskpet.system2 import jev_api, jev_shadow
from evaluation import jev_shadow_replay
from evaluation.jev_prompt_ab import BOUNDARY, CANDIDATE_INSTRUCTIONS, SAVINGS, SMOKE, boundary_cases, compare
from evaluation.jev_prompt_ab import reference_cohorts


def test_reference_ab_uses_same_wire_state_and_restores_deployed_prompt(monkeypatch):
    deployed = dict(instructions=jev_api._INSTRUCTIONS, criteria=dict(jev_shadow._DECISIONS))
    wire = {"baseline": [], "candidate": []}

    def respond(request):
        payload = json.loads(request.content)
        question = payload["questions"]["background_action"]
        arm = "candidate" if question["instructions"] == CANDIDATE_INSTRUCTIONS else "baseline"
        wire[arm].append(payload)
        if arm == "candidate" and len(wire[arm]) == 1:
            return httpx.Response(503, text="private provider body")
        return httpx.Response(200, json=dict(
            model=payload["model"],
            answers={"background_action": dict(type="choice", choice="defer", confidence=1.0,
                                               probabilities=dict(wake_system2=0.0, defer=1.0))},
            usage=dict(input_tokens=12, output_tokens=2),
        ))

    settings = jev_api.JevSettings(enabled=True, share_text=True, api_key="test-dummy-key")
    client = jev_api.JevClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(jev_shadow_replay, "_advisor", lambda mode: (settings, client))
    report = compare(reference_cohorts(), "real")
    smoke, savings = (report["cohorts"][name] for name in (SMOKE, SAVINGS))
    assert len(smoke["rows"]) == 6
    assert len(savings["rows"]) == 12
    assert smoke["scenario_arm_order"] == {
        "work-start": ["baseline", "candidate"],
        "focus-bilibili": ["candidate", "baseline"],
    }
    assert [payload["state"] for payload in wire["baseline"]] == [payload["state"] for payload in wire["candidate"]]
    for arm in wire:
        assert all(payload["questions"]["background_action"] == {"type": "choice", **report["prompts"][arm]}
                   for payload in wire[arm])
    assert report["prompts"]["baseline"] == deployed
    assert dict(instructions=jev_api._INSTRUCTIONS, criteria=jev_shadow._DECISIONS) == deployed

    candidate_summary = smoke["summary"]["candidate"]
    assert candidate_summary["jev"]["failure_counts"] == {"http_error": 1}
    assert candidate_summary["jev"]["usage"]["input_tokens"] == {
        "tokens": None, "reported_requests": 5, "total_requests": 6,
    }
    failed, following = smoke["rows"][:2]
    assert failed["candidate"]["jev"]["raw_defer"] is None
    assert failed["candidate"]["jev"]["could_defer"] is False
    assert failed["candidate"]["jev"]["http_status"] == 503
    assert following["baseline"]["jev"]["host_reason"] == "defer_cap"
    assert following["candidate"]["jev"]["host_reason"] == "model_defer"
    assert smoke["paired"]["raw_model"]["unscored"] == 1
    assert smoke["paired"]["host_policy"]["candidate_regressed"] == 2
    assert "private provider body" not in json.dumps(report)
    assert "test-dummy-key" not in json.dumps(report)


def test_boundary_inputs_are_blind_coded_and_keep_exact_offer_provenance(monkeypatch):
    states = []

    class Recorder(jev_shadow_replay.FixtureAdvisor):
        def adjudicate(self, settings, state, *, criteria, send_permitted=None):
            states.append(state)
            return super().adjudicate(settings, state, criteria=criteria, send_permitted=send_permitted)

    settings, _ = jev_shadow_replay._advisor("fixture")
    monkeypatch.setattr(jev_shadow_replay, "_advisor", lambda mode: (settings, Recorder()))
    cases = boundary_cases()
    report = compare({BOUNDARY: cases}, "fixture")["cohorts"][BOUNDARY]

    assert len(report["rows"]) == 7
    for case in cases:
        assert case.id not in json.dumps(case.context)
        assert case.id not in json.dumps(case.event)
        assert case.id not in json.dumps(states)
        assert case.expected not in json.dumps(states)
        assert case.annotation not in json.dumps(states)
    for state, spacing in ((states[0], 24), (states[2], 25)):
        earlier, later = state["focus_2m"]["captures"][-2:]
        assert earlier["activity_kind"] == later["activity_kind"] == "leisure"
        assert (datetime.fromisoformat(later["observed_at"])
                - datetime.fromisoformat(earlier["observed_at"])).total_seconds() == spacing
        assert state["activity"]["scene_observed_at"] == later["observed_at"]
    assert states[-1]["pending_offer"] is True
    assert states[-1]["last_feedback"] == "unanswered"
    assert states[-1]["same_opportunity"] is False
    assert report["rows"][4]["candidate"]["jev"]["host_reason"] == "evidence_incomplete"
    assert report["rows"][-1]["candidate"]["local_baseline_reason"] is None
