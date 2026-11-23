"""C/D review evidence, separate from the public WorkSession schema and metrics."""
from datetime import datetime
from collections import Counter


def end_record(*, result, review, submitted_at, origin, action_seq, applied_seq):
    """Only call after an applied end. Caller supplies trusted action provenance."""
    view = result["view"]
    status = "not_offered" if review is None else "unconfirmed"
    submission = review
    malformed = review is not None and not isinstance(review, dict)
    review = review if isinstance(review, dict) else {}
    valid = False
    try:
        offered = datetime.fromisoformat(review["offered_at"])
        submitted = datetime.fromisoformat(submitted_at)
        valid = (offered.utcoffset() is not None and submitted.utcoffset() is not None
                 and offered <= submitted and review["session_id"] == view["session_id"]
                 and isinstance(review["review_id"], str) and bool(review["review_id"])
                 and type(review.get("explicit_no_completed_items")) is bool)
    except (KeyError, TypeError, ValueError):
        pass
    explicit_none = review.get("explicit_no_completed_items") is True
    if malformed or (submission is not None and not valid):
        status = "invalid_evidence"
    elif valid:
        if explicit_none:
            status = "invalid_evidence" if view["confirmed_outcomes"] else "confirmed_none"
        elif view["confirmed_outcomes"]:
            status = "confirmed"
    return {
        "protocol_version": "c-d-review-v1", "command_id": result["command_id"],
        "review_id": review.get("review_id"), "offered_at": review.get("offered_at"),
        "submitted_at": submitted_at, "confirmation_origin": origin,
        "review_status": status, "explicit_no_completed_items": explicit_none,
        "review_submission": submission,
        "action_seq": action_seq, "applied_seq": applied_seq,
        "accepted_result": result,
        # Review support/eligibility was not frozen before enrollment. Do not
        # turn these diagnostic records into a formal completion-rate denominator.
        "eligibility": "not_predeclared",
    }


def summarize_reviews(events):
    """Validate links to accepted service events and UI provenance; no formal rate."""
    by_seq = {(e["run_id"], e["scenario_id"], e["seq"]): e for e in events}
    errors, seen, linked, statuses = [], set(), set(), Counter()
    real_confirmations = 0
    for event in events:
        if event["kind"] != "session.end_accepted":
            continue
        p = event["payload"]
        key = (event["run_id"], event["scenario_id"], event.get("session_id"))
        try:
            assert p["protocol_version"] == "c-d-review-v1", "unknown review protocol"
            assert key not in seen, "duplicate end evidence"
            seen.add(key)
            action = by_seq.get((*key[:2], p["action_seq"]))
            applied = by_seq.get((*key[:2], p["applied_seq"]))
            result = p["accepted_result"]
            view = result["view"]
            assert action and action["kind"] == "ui.action", "missing UI action"
            assert action["payload"]["action_id"] == "session.end", "wrong UI action"
            assert action["payload"]["event_id"] == p["command_id"], "action identity mismatch"
            assert applied and applied["kind"] == "session.applied", "missing accepted command"
            assert applied["session_id"] == key[2] == view["session_id"], "session identity mismatch"
            assert applied["payload"]["command_id"] == p["command_id"] == result["command_id"], "command mismatch"
            assert applied["payload"]["kind"] == "end", "not an end command"
            assert action["seq"] < applied["seq"] < event["seq"], "event order mismatch"
            assert result["status"] == "applied" and result["error_code"] is None, "result not accepted"
            assert view["state"] == applied["payload"]["state"] == "ended", "not an ended view"
            assert view["version"] == applied["payload"]["version"], "terminal version mismatch"
            assert isinstance(view["confirmed_outcomes"], list) and all(
                isinstance(v, str) for v in view["confirmed_outcomes"]), "invalid outcomes"
            assert isinstance(view["next_steps"], list) and all(
                isinstance(v, str) for v in view["next_steps"]), "invalid next steps"
            expected_origin = ("automation" if action["payload"].get("automation")
                               or action["source"] != "native" else "user")
            assert p["confirmation_origin"] == expected_origin, "origin mismatch"
            assert event["source"] == action["source"], "source mismatch"
            assert event["clock"] == action["clock"], "clock mismatch"
            assert event["simulated"] == (expected_origin != "user" or event["clock"] != "wall"), "simulation mismatch"
            reconstructed = end_record(result=result, review=p["review_submission"],
                submitted_at=p["submitted_at"], origin=expected_origin,
                action_seq=p["action_seq"], applied_seq=p["applied_seq"])
            assert reconstructed["review_status"] == p["review_status"], "review status mismatch"
            for field in ("review_id", "offered_at", "explicit_no_completed_items"):
                assert reconstructed[field] == p[field], "review metadata mismatch"
            assert (datetime.fromisoformat(action["ts"]) <= datetime.fromisoformat(p["submitted_at"])
                    <= datetime.fromisoformat(event["ts"])), "submission time mismatch"
            statuses[p["review_status"]] += 1
            linked.add((*key[:2], p["command_id"]))
            if not event["simulated"] and p["review_status"] in {"confirmed", "confirmed_none"}:
                real_confirmations += 1
        except (AssertionError, KeyError, TypeError, ValueError) as exc:
            errors.append(f"seq {event['seq']}: {exc}")
    ended = { (e["run_id"], e["scenario_id"], e["payload"].get("command_id"))
              for e in events if e["kind"] == "session.applied" and e["payload"].get("kind") == "end" }
    return {"status": "failed" if errors else "evidence_gap" if ended - linked else "passed",
            "accepted_end_commands": len(ended), "linked_end_records": len(linked),
            "missing_end_records": len(ended - linked), "review_statuses": dict(statuses),
            "real_user_confirmations": real_confirmations, "errors": errors,
            "formal_outcome_coverage": "not_measured",
            "reason": "Eligibility, participant records and review deadlines are not frozen; automated/virtual runs are diagnostics."}
