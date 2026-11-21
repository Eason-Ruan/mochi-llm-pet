"""Check the structure of D's synthetic reminder pilot, without running a model.

Gold values are draft annotations. A successful check does not prove that the
utterance was interpreted correctly, reviewed by a human, or replayed.
"""

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from deskpet.contracts.models import TaskCommand, TaskDraft, TaskView


def require(condition, message):
    if not condition:
        raise ValueError(message)


def aware_time(value, field):
    require(isinstance(value, str), f"{field} must be an ISO datetime string")
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(instant.utcoffset() is not None, f"{field} must include a UTC offset")
    return instant


def _validate_gold(gold, intent, behavior, fields, context, sample_id, reference):
    """Validate a gold label structure against intent/behavior; return the TaskDraft or None."""
    if behavior == "clarify":
        require(gold is None and bool(fields), f"{sample_id}: clarify needs null gold and missing fields")
        require(context is None and intent == "create",
                f"{sample_id}: this pilot's clarification case is a new request")
        return None
    require(isinstance(gold, dict) and not fields, f"{sample_id}: parse needs gold and no clarification fields")
    if intent == "create":
        require(context is None, f"{sample_id}: create sample must not edit a context task")
        draft = TaskDraft.model_validate(gold)
    else:
        expected_fields = {"task_id", "expected_version"} | ({"draft"} if intent == "edit" else set())
        require(set(gold) == expected_fields, f"{sample_id}: incorrect gold fields for {intent}")
        require(type(gold["expected_version"]) is int, f"{sample_id}: version must be an integer")
        require(isinstance(context, dict) and isinstance(context.get("tasks"), list),
                f"{sample_id}: edit/cancel requires task context")
        tasks = [TaskView.model_validate(task) for task in context["tasks"]]
        require(len({task.task_id for task in tasks}) == len(tasks), f"{sample_id}: duplicate context task IDs")
        matches = [task for task in tasks if task.task_id == gold["task_id"]]
        require(len(matches) == 1, f"{sample_id}: target task not found uniquely")
        task = matches[0]
        require(task.status == "registered" and task.version == gold["expected_version"],
                f"{sample_id}: target is not the registered expected version")
        require(task.confirmed_at is not None and task.confirmed_at <= reference,
                f"{sample_id}: task must already be confirmed at the reference time")
        payload = {"draft": gold["draft"]} if intent == "edit" else {}
        TaskCommand.model_validate(dict(command_id=sample_id, kind=intent,
            task_id=gold["task_id"], expected_version=gold["expected_version"], payload=payload))
        draft = TaskDraft.model_validate(gold["draft"]) if intent == "edit" else None
    if draft is not None:
        require(draft.earliest_at >= reference, f"{sample_id}: draft begins before this pilot's reference time")
    return draft


def validate_dataset(data):
    """Validate this pilot's format; it intentionally accepts only draft dev data."""
    require(isinstance(data, dict), "Dataset must be an object")
    require(type(data.get("schema_version")) is int and data["schema_version"] == 1,
            "Unsupported schema_version")
    require(data.get("dataset_version") == "pilot-en-v0.1", "Expected pilot-en-v0.1")
    require(data.get("language") == "en", "This dataset must declare language=en")
    require(data.get("split") == "dev", "This pilot is dev data, not holdout")
    require(data.get("origin") == "synthetic_fixture", "Pilot origin must remain synthetic_fixture")
    require(data.get("label_origin") == "ai_draft", "Pilot label_origin must remain ai_draft")
    samples = data.get("samples")
    require(isinstance(samples, list) and bool(samples), "samples must be a nonempty list")
    by_id = {}
    intents = Counter()
    behaviors = Counter()
    groups = set()

    reviewed_count = 0
    double_annotated_count = 0
    for row in samples:
        require(isinstance(row, dict), "Every sample must be an object")
        sample_id = row.get("request_sample_id")
        require(isinstance(sample_id, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", sample_id),
                "Invalid request_sample_id")
        require(sample_id not in by_id, f"Duplicate sample ID: {sample_id}")
        by_id[sample_id] = row
        group = row.get("source_group_id")
        require(isinstance(group, str) and bool(group.strip()), f"{sample_id}: missing source_group_id")
        groups.add(group)
        for field in ("utterance", "input_timezone", "judgement_note", "label_version", "category"):
            require(isinstance(row.get(field), str) and bool(row[field].strip()),
                    f"{sample_id}: missing {field}")
        reference = aware_time(row.get("reference_time"), f"{sample_id}.reference_time")
        status = row.get("annotation_status")
        require(status in {"pending_human_review", "single_annotator_dev", "double_annotated"},
                f"{sample_id}: annotation_status must be pending_human_review, single_annotator_dev, or double_annotated")
        if status in {"single_annotator_dev", "double_annotated"}:
            reviewed_count += 1
            annotator_a = row.get("annotator_a")
            require(isinstance(annotator_a, dict),
                    f"{sample_id}: {status} requires annotator_a review")
            for field in ("annotator_id", "annotated_at", "review_note"):
                require(isinstance(annotator_a.get(field), str) and bool(annotator_a[field].strip()),
                        f"{sample_id}.annotator_a: missing {field}")
            aware_time(annotator_a["annotated_at"], f"{sample_id}.annotator_a.annotated_at")
        if status == "single_annotator_dev":
            require(row.get("annotator_b") is None and row.get("agreement") is None
                    and row.get("adjudication") is None,
                    f"{sample_id}: single_annotator_dev keeps annotator_b/agreement/adjudication null")
        elif status == "double_annotated":
            double_annotated_count += 1
            annotator_b = row.get("annotator_b")
            require(isinstance(annotator_b, dict),
                    f"{sample_id}: double_annotated requires annotator_b review")
            for field in ("annotator_id", "annotated_at", "review_note"):
                require(isinstance(annotator_b.get(field), str) and bool(annotator_b[field].strip()),
                        f"{sample_id}.annotator_b: missing {field}")
            aware_time(annotator_b["annotated_at"], f"{sample_id}.annotator_b.annotated_at")
            require(annotator_b["annotator_id"] != annotator_a["annotator_id"],
                    f"{sample_id}: annotator_b must differ from annotator_a")
            agreement = row.get("agreement")
            require(isinstance(agreement, dict),
                    f"{sample_id}: double_annotated requires agreement record")
            require(isinstance(agreement.get("agreed"), bool),
                    f"{sample_id}.agreement.agreed must be a boolean")
            require(isinstance(agreement.get("agreed_fields"), list),
                    f"{sample_id}.agreement.agreed_fields must be a list")
            require(isinstance(agreement.get("disagreed_fields"), list),
                    f"{sample_id}.agreement.disagreed_fields must be a list")
            adjudication = row.get("adjudication")
            require(isinstance(adjudication, dict),
                    f"{sample_id}: double_annotated requires adjudication record")
            for field in ("adjudicator_id", "adjudicated_at", "adjudication_note"):
                require(isinstance(adjudication.get(field), str) and bool(adjudication[field].strip()),
                        f"{sample_id}.adjudication: missing {field}")
            aware_time(adjudication["adjudicated_at"], f"{sample_id}.adjudication.adjudicated_at")
        else:
            require(all(field in row and row[field] is None for field in (
                "annotator_a", "annotator_b", "agreement", "adjudication"
            )), f"{sample_id}: preserve unreviewed annotation placeholders")
        intent = row.get("intent")
        behavior = row.get("expected_behavior")
        require(intent in {"create", "edit", "cancel"}, f"{sample_id}: invalid intent")
        require(behavior in {"parse", "clarify"}, f"{sample_id}: invalid expected_behavior")
        intents[intent] += 1
        behaviors[behavior] += 1
        gold = row.get("gold")
        fields = row.get("clarification_fields")
        require(isinstance(fields, list) and all(isinstance(x, str) and x.strip() for x in fields),
                f"{sample_id}: invalid clarification_fields")
        context = row.get("context")
        _validate_gold(gold, intent, behavior, fields, context, sample_id, reference)
        annotator_a = row.get("annotator_a")
        if isinstance(annotator_a, dict) and "gold" in annotator_a:
            _validate_gold(annotator_a["gold"], intent, behavior, fields, context,
                           f"{sample_id}.annotator_a", reference)

    for row in samples:
        context = row.get("context")
        if context is None:
            continue
        seed_id = context.get("seed_request_sample_id")
        seed = by_id.get(seed_id) if isinstance(seed_id, str) else None
        require(seed is not None and seed["intent"] == "create" and seed["expected_behavior"] == "parse",
                f"{row['request_sample_id']}: missing parse/create seed")
        require(seed["source_group_id"] == row["source_group_id"],
                f"{row['request_sample_id']}: derived samples must keep the seed source group")
        target = next(task for task in context["tasks"] if task["task_id"] == row["gold"]["task_id"])
        require(TaskDraft.model_validate(target["draft"]) == TaskDraft.model_validate(seed["gold"]),
                f"{row['request_sample_id']}: context does not match the seed draft")

    if double_annotated_count == len(samples):
        overall_status = "double_annotated"
    elif reviewed_count == len(samples):
        overall_status = "single_annotator_dev"
    elif reviewed_count == 0:
        overall_status = "pending_human_review"
    else:
        overall_status = "mixed"
    return {
        "status": "passed",
        "validation_type": "dataset_structure",
        "dataset_version": data["dataset_version"],
        "language": data["language"],
        "sample_count": len(samples),
        "source_group_count": len(groups),
        "intent_counts": dict(intents),
        "expected_behavior_counts": dict(behaviors),
        "origin": data["origin"],
        "split": data["split"],
        "annotation_status": overall_status,
        "human_reviewed_samples": reviewed_count,
        "double_annotated_samples": double_annotated_count,
        "model_evaluation": "not_run",
        "note": (
            "Structure checked only. Single-annotator dev review is not double annotation; "
            "natural-language meaning still requires independent second review."
            if overall_status == "single_annotator_dev"
            else "Structure checked only. Double annotation here means two recorded reviewer labels; "
                 "independence and natural-language meaning still require the declared annotation protocol."
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    args = parser.parse_args()
    try:
        data = json.loads(args.dataset.read_text(encoding="utf-8-sig"))
        result = validate_dataset(data)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False, indent=2))
        raise SystemExit(1)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
