from copy import deepcopy

import pytest

from deskpet.c_ui.review_evidence import end_record, summarize_reviews


NOW = "2026-01-01T17:00:00+00:00"


def result(outcomes):
    return {"command_id": "end-1", "status": "applied", "error_code": None,
            "view": {"session_id": "work-1", "version": 2, "state": "ended",
                     "confirmed_outcomes": outcomes, "next_steps": ["read methods"]}}


def review(**changes):
    return {"review_id": "review-1", "session_id": "work-1", "offered_at": NOW,
            "explicit_no_completed_items": False, **changes}


@pytest.mark.parametrize("outcomes,metadata,status", [
    (["read introduction"], review(), "confirmed"),
    ([], review(explicit_no_completed_items=True), "confirmed_none"),
    ([], review(), "unconfirmed"),
    (["read introduction"], None, "not_offered"),
    (["read introduction"], review(explicit_no_completed_items=True), "invalid_evidence"),
    (["read introduction"], review(offered_at="2026-01-02T17:00:00+00:00"), "invalid_evidence"),
    (["read introduction"], review(offered_at="2026-01-01T17:00:00"), "invalid_evidence"),
    (["read introduction"], review(session_id="other"), "invalid_evidence"),
])
def test_review_requires_explicit_consistent_evidence(outcomes, metadata, status):
    record = end_record(result=result(outcomes), review=metadata, submitted_at=NOW,
                        origin="automation", action_seq=1, applied_seq=2)
    assert record["review_status"] == status


def events():
    base = {"run_id": "test", "scenario_id": "cd", "ts": NOW,
            "source": "offscreen", "simulated": True, "clock": "virtual"}
    return [
        dict(base, seq=1, kind="ui.action", payload={"event_id": "end-1", "action_id": "session.end", "automation": True}),
        dict(base, seq=2, kind="session.applied", session_id="work-1", payload={
            "command_id": "end-1", "kind": "end", "state": "ended", "version": 2}),
        dict(base, seq=3, kind="session.end_accepted", session_id="work-1", payload=end_record(
            result=result(["read introduction"]), review=review(), submitted_at=NOW,
            origin="automation", action_seq=1, applied_seq=2)),
    ]


def test_automated_review_never_counts_as_real_user_confirmation():
    summary = summarize_reviews(events())
    assert summary["status"] == "passed" and summary["linked_end_records"] == 1
    assert summary["real_user_confirmations"] == 0
    assert summary["formal_outcome_coverage"] == "not_measured"
    manual_virtual = events()
    manual_virtual[0].update(source="native")
    manual_virtual[0]["payload"]["automation"] = False
    manual_virtual[2].update(source="native")
    manual_virtual[2]["payload"]["confirmation_origin"] = "user"
    assert summarize_reviews(manual_virtual)["real_user_confirmations"] == 0


@pytest.mark.parametrize("mutation", ["version", "session", "command", "origin", "duplicate", "missing_action", "order", "empty_claim"])
def test_checker_rejects_broken_links_and_fabricated_confirmations(mutation):
    data = events()
    payload = data[-1]["payload"]
    if mutation == "version":
        payload["accepted_result"]["view"]["version"] = 3
    elif mutation == "session":
        payload["accepted_result"]["view"]["session_id"] = "other"
    elif mutation == "command":
        data[1]["payload"]["command_id"] = "other"
    elif mutation == "origin":
        payload["confirmation_origin"] = "user"
    elif mutation == "duplicate":
        data.append(dict(deepcopy(data[-1]), seq=4))
    elif mutation == "missing_action":
        data.pop(0)
    elif mutation == "order":
        data[0]["seq"] = 5
        payload["action_seq"] = 5
    else:
        payload["accepted_result"]["view"]["confirmed_outcomes"] = []
    summary = summarize_reviews(data)
    assert summary["status"] == "failed" and summary["errors"]


def test_end_event_alone_is_an_evidence_gap():
    summary = summarize_reviews(events()[:2])
    assert summary["status"] == "evidence_gap" and summary["missing_end_records"] == 1
