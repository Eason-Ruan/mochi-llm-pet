from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json

from deskpet.observation_v2.context import ObservationContextReader, project_observation_context
from deskpet.observation_v2.pipeline import ObservationStore
from deskpet.observation_v2.state import Observation


def test_history_keeps_first_and_last_distinct_summary_with_original_legacy_value(tmp_path):
    now = datetime(2026, 11, 23, tzinfo=UTC)
    path = tmp_path / "live.sqlite"
    rows = (" Papers  marker ", "  ", "Papers marker", "Code marker", "Write marker", " Papers   marker ", None)
    with ObservationStore(path, live=True) as store:
        for index, summary in enumerate(rows):
            at = now + timedelta(seconds=index * 20)
            store.save_observation(
                Observation("work", "working", summary, (), at, at + timedelta(seconds=15), 1),
                published_at=at,
            )

    context = ObservationContextReader(path).read_observation_context(now + timedelta(minutes=2, seconds=1))
    segment, = context.history.segments
    assert segment.summary == " Papers   marker "
    assert segment.sample_count == 7
    assert [(sample.observed_at, sample.summary) for sample in segment.summary_samples] == [
        (now, "Papers marker"), (now + timedelta(seconds=80), "Write marker"),
    ]

    visible = context.to_dict()
    assert visible["history"]["segments"][0]["summary_samples"][0] == {
        "observed_at": now.isoformat(), "summary": "Papers marker",
    }
    observed = replace(context.current, summary="Current marker", reason_codes=("sustained_activity", "hidden marker"))
    payload_context = replace(context, current=observed)
    redacted = project_observation_context(payload_context)
    assert "marker" not in json.dumps(redacted)
    assert redacted["current"]["reason_codes"] == ["sustained_activity"]
    assert "summary" not in redacted["current"]
    assert "summary" not in redacted["history"]["recent"][-1]
    assert "summary" not in redacted["history"]["segments"][0]
    assert "summary_samples" not in redacted["history"]["segments"][0]
    assert redacted["history"]["stats"]["total_samples"] == 7
    assert project_observation_context(payload_context, share_summary=True)["current"]["summary"] == "Current marker"
