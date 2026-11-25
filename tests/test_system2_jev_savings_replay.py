from datetime import datetime
import json

from deskpet.system2.jev_projection import project_jev_snapshot
from deskpet.system2.prompt import select_prompt
from evaluation.jev_savings_replay import cases
from evaluation.jev_shadow_replay import BYPASS, MAY_DEFER, NEEDS_SYSTEM2, replay


def test_savings_corpus_reaches_advisor_beyond_local_controls():
    report = replay(cases(), "fixture")
    rows = {row["case"]: row for row in report["rows"]}

    assert report["summary"]["label_counts"] == {MAY_DEFER: 8, NEEDS_SYSTEM2: 3, BYPASS: 1}
    assert {key: row["local_baseline_reason"] for key, row in rows.items() if row["local_baseline_could_defer"]} == {
        "report-pause-card-painted": "pending_offer",
        "editor-layout-suggestions-disabled": "no_candidate",
    }
    opportunities = {key for key, row in rows.items() if row["expected"] == MAY_DEFER
                     and not row["local_baseline_could_defer"] and row["jev"]["host_reason"] == "model_defer"}
    assert opportunities == {"report-results-chart", "report-source-and-review", "editor-layout-diff",
                             "editor-layout-tests", "course-lecture", "course-notebook-exercise"}
    assert rows["report-with-reminder"]["jev"]["status"] == "hard_bypass"
    assert report["summary"]["jev"]["model_requests"] == 0
    assert report["summary"]["system2"]["model_requests"] == 0


def test_savings_controls_reach_both_projections_without_labels(monkeypatch):
    monkeypatch.setenv("MOCHI_SYSTEM2_SPLIT_PROMPT", "1")
    corpus = {case.id: case for case in cases()}

    def projected(case_id):
        case = corpus[case_id]
        return project_jev_snapshot(case.context, next_tick_in_s=60, share_text=True).state

    for key, kind, element in (("report-racing-game", "leisure", "game scene"),
                               ("course-variety-clip", "leisure", "video player"),
                               ("course-lecture", "study", "lecture video")):
        state = projected(key)
        assert state["focus_2m"]["gaps"] == []
        first, second = state["focus_2m"]["captures"][-2:]
        assert first["activity_kind"] == second["activity_kind"] == kind
        assert element in first["summary"] and element in second["summary"]
        assert (datetime.fromisoformat(second["observed_at"])
                - datetime.fromisoformat(first["observed_at"])).total_seconds() == 30

    assert projected("editor-layout-no-timer")["work_evidence"]["continuous"] is True
    for case in corpus.values():
        payload = case.system2_payload()
        wire = json.dumps(payload, ensure_ascii=False)
        assert case.id not in wire and case.expected not in wire and case.annotation not in wire
        prompt = select_prompt(payload["origin"], payload["event"], payload["context"], payload["grants"],
                               semantic_background=True)
        assert prompt.version == "system2.zh.v14"
