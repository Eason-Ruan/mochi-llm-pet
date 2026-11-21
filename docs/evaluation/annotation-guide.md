# D Dataset and Annotation Guide v0.4

Status: pilot draft. Date: 2026-11-16. Document language: English.
The tables define D's evaluation data, not new public TraceEvent/Scenario fields. Do not insert them into strict business models.
Metric definitions: [metrics-spec.md](metrics-spec.md). Public interfaces: [contracts](../contracts/README.md).
Revision 0.4 links record-level invitation reasons to explicit report-level reason/gap counts. Earlier revisions added typed report and proposed experience/work-outcome protocols. These remain D-side annotations, not implemented GUI forms or new public business fields. The reminder dataset stays `pilot-en-v0.1` and its checker is unchanged.

## 1. Formal data plan and current pilot

| Data | Formal plan | Initial pilot |
| --- | --- | --- |
| Reminder requests | 120: development 80 / holdout 40 | Six synthetic development records: hard, soft, timezone, ambiguous, edit, cancel |
| Computer-use records | 48 independent sessions: development 16 / holdout 32 | Design two development-session records first; demonstrate format until real collection is verified |
| Fault scenarios | 30 classes, possibly several instances per class | Three development classes: early cancellation, missing receipt, failure/recovery |

The pilot column is a starting scale, not a claim that formal data has been collected. Disclose reductions to the formal plan, reasons, and consequences.
Formal quantities and double annotation come from project architecture section A8. Proposed fields and pilot procedures require agreement before formal experiments.
Copies, repeated reads, and reruns do not create independent requests/sessions. Report fault-class counts and instance counts separately.

## 2. Identity and provenance

Every sample needs stable identity, a dataset version, and evidence references. Use letters, digits, hyphens, or underscores for example IDs, not colons, separators, or drive names.

| Suggested field | Meaning |
| --- | --- |
| sample_id / record_id | Evaluation identity; preserve A's original observation sample_id across reads |
| source_group_id | Group an original request with its variants/derived cases |
| source_session_id | Computer-use session, distinct from business WorkSession.session_id |
| dataset_version / split | Dataset revision and dev/holdout; standalone teaching examples use demo |
| origin | Explicit source such as human_request, independent_human_label, synthetic_fixture, scripted, or automation |
| evidence_refs | Original materials, runs, trace lines/seq, samples, and task/request IDs |
| annotator_id / annotated_at | Anonymous annotator identity and timestamp with UTC offset |
| label_version | Label revision; preserve prior labels and reasons |

Model output, scripted state, and automated GUI actions are not independent human truth. Preserve actual source, clock, and simulated flags.
Collect only authorized necessary coarse signals. Do not add key text, clipboard content, screenshots, or complete window titles just for this evaluation. Use de-identified, authorized reminder materials.

## 3. Annotation tables

### 3.1 Reminder requests

| Required information | Annotation rule |
| --- | --- |
| request_sample_id, source_group_id, origin | Distinguish human requests, authored cases, and synthetic inputs |
| utterance, reference_time, input_timezone | Preserve wording and fixed context for relative dates; include UTC offset and IANA timezone |
| intent | create / edit / cancel; editing/cancelling is not creating a new reminder |
| expected_behavior | parse or clarify; do not guess a gold timestamp for ambiguity |
| gold | TaskDraft for create; task_id/expected_version/draft for edit; task_id/expected_version for cancel; null for clarify |
| context, clarification_fields | Existing target tasks or missing fields that require a question |
| content_judgement_rule | Predefined equivalent wording or required meaning |
| annotator_a, annotator_b, agreement, adjudication | Original independent labels, agreement, and final decision/reason |

Store the first model prediction, user corrections, and final confirmed task separately. Never overwrite the first prediction with corrected values.
Hard-reminder endpoints must match. Soft latest time must not precede earliest time. Ask for unclear timezone/location information instead of assuming the machine's default.

This standalone demo explains fields; it is not a public Scenario or a counted real request:

```json
{
  "request_sample_id": "demo-request-001",
  "source_group_id": "demo-request-group-001",
  "dataset_version": "pilot-en-v0.1",
  "split": "demo",
  "origin": "synthetic_fixture",
  "utterance": "Between 10 and 30 seconds from now, remind me to drink water when I am available, no later than 30 seconds from now.",
  "reference_time": "2026-11-16T12:00:00+08:00",
  "input_timezone": "Asia/Shanghai",
  "intent": "create",
  "expected_behavior": "parse",
  "gold": {
    "content": "Drink water",
    "mode": "soft",
    "timezone": "Asia/Shanghai",
    "earliest_at": "2026-11-16T04:00:10Z",
    "latest_first_delivery_at": "2026-11-16T04:00:30Z"
  },
  "content_judgement_rule": "The reminder concerns drinking water. Equivalent wording is acceptable; unrelated tasks are not."
}
```

Keep task_id, occurrence, accepted commands, versions, and display evidence in linked result records. Do not derive gold from the model under test.

### 3.1.1 Six current development records

[reminder-dev-pilot.json](../../evaluation/data/reminder-dev-pilot.json) is D's dataset. Top-level dataset_version, split, origin, label_origin, and language apply to all samples. It cannot be passed directly to M0 replay as a Scenario.
The English version is `pilot-en-v0.1`; do not mix its records with the earlier draft under an unchanged dataset identity.
All six are synthetic_fixture with ai_draft labels and pending_human_review status. They are not independent real requests or completed double annotations. judgement_note combines content criteria and time interpretation.

| ID suffix | Category | Check |
| --- | --- | --- |
| 001 | Hard | Reference +30 seconds; both hard endpoints match |
| 002 | Soft | Opportunity interval from +10 to +30 seconds, including final deadline |
| 003 | Timezone | Tokyo 15:00 = UTC 06:00 = Beijing 14:00 on 2026-11-16 |
| 004 | Ambiguous | Missing time window requires clarify and gold=null |
| 005 | Edit | Edit the unique registered v2 task rather than creating another task |
| 006 | Cancel | Cancel the unique v2 task, rather than completing it |

001/005/006 share a source_group_id. Cases 005 and 006 each start independently from 001's confirmed-task context; the array is not a sequential execution script. Six records therefore have four source groups. Keep each group on one side of any dev/holdout split.
Random command_id values are not semantic scoring criteria. Evaluate action, target, expected version, and applicable draft instead.
Review after seeing draft gold is assisted review, not blinded independent annotation. Provide separate gold-hidden materials for independent annotators and preserve both original answers.

Structure check:

```powershell
uv run --offline python scripts/d_check_reminder_dataset.py evaluation/data/reminder-dev-pilot.json
```

The checker currently supports only this unreviewed synthetic English dev format. It checks JSON, IDs, required fields, public task models, target versions, and source-group links. It does not judge natural-language meaning, consult the IANA timezone database, call a model, or execute reminders.
Expected counts: sample_count=6, source_group_count=4, parse=5, clarify=1; model_evaluation remains not_run. Importing formal human labels needs a separately supported format and review evidence, not a manual status change to claim completion.

### 3.2 Computer-use sessions and state labels

Record each independent collection session's source_session_id, anonymous participant, start/end, platform/device, capabilities, source, and errors. Add labels with validity intervals within that session.
A collection session is not a pomodoro WorkSession; it may contain zero or several business work sessions.

| State | Independent human basis | Not sufficient by itself |
| --- | --- | --- |
| working | Independent evidence confirms activity that should not be interrupted early | Editor in foreground or mouse movement |
| available | Independent evidence confirms availability for an early reminder | An idle threshold or an available prediction |
| away | Independent evidence confirms absence from the computer | Idle time alone |
| unknown | Evidence cannot determine the other states | Hiding a known incorrect label |

Labels include label_id, source_session_id, sample/interval linkage, state, valid_from, valid_until, independent_evidence_ref, two annotators, and adjudication.
Label validity is `[valid_from, valid_until)`. Do not invent a long interval from an uncertain retrospective guess or missing end time.

Prediction records separately preserve estimator/configuration, based_on_sample_id, base prediction, prediction time, evidence validity, raw_status, capabilities, and errors. Compare only within the validity intersection of raw sample, base estimate, and independent label.
Count one independent sample per configuration/sample_id, even across repeated reads. Predeclare the evaluation instant; test expiry boundaries separately rather than selecting the most favorable read.
Both estimators use matched sample IDs. Report unmatched predictions instead of silently discarding them.

Prevent label leakage:

1. Annotators inspect independent materials without predictions or the other annotator's labels.
2. Evaluate `ObservationFrame.estimated`, not the effective state after user correction.
3. correction.origin=user is a manual-action clue, not sufficient proof of independent annotation. Scripted/automation corrections are not human labels.
4. A correction already used as prediction input cannot validate that prediction.
5. No correction/click, a running work session, or application dwell time is not state truth.

### 3.3 Fault scenarios

Record fault_class_id, instance ID, module, initial state, ordered inputs, expected states/errors, allowed/forbidden effects, evidence, actual outcome, and provenance.
A case name does not replace executable expected_outcomes. Correctly handling an expected failure can pass the case; missing evidence, nonexecution, or unsupported capability must stay explicit.

| Pilot class | Expected behavior | Evidence |
| --- | --- | --- |
| Early cancellation | No new requests/displays after accepted cancellation; old results cannot revive the task | Accepted cancellation, versions, complete later trace and observation horizon |
| Missing display receipt | No counted successful display; preserve timeout/retry | Same-ID requests, timeout, retry, no valid displayed receipt |
| Failure/recovery | Preserve failure, retry, and success; still one unique delivery | Correlated requested -> failed -> retry request -> displayed |

Extend coverage to edit/cancel races, duplicate commands, malformed JSON, service interruptions, stale context, sleep/restart, and permissions. Thirty classes is the formal target; renaming one failure thirty times does not satisfy it.
Recovery-fixture validation is not SQLite process-restart evidence; B supplies real before/after restart state and logs. Simulated GUI receipts do not replace C's native rendering evidence.

### 3.4 Interruption experience records

Purpose: measure a participant's reported experience of one displayed reminder, separately from independent state labels and policy-rule violations. Use this project-defined question: "How much did this reminder interrupt what you were doing?"

| Answer | Meaning | Primary binary treatment |
| --- | --- | --- |
| 0 | No interruption | Not above threshold |
| 1 | Slight interruption | Not above threshold |
| 2 | Moderate interruption | Reported interruption |
| 3 | Severe interruption | Reported interruption |
| Not seen | Participant did not notice/recognize the reminder | Separate non-rating response |
| Prefer not to answer | Participant declines | Separate refusal |

Freeze threshold >=2 before collection. The participant reports their own experience; annotators must not replace it with an inferred state or majority vote.

| Proposed field | Recording rule |
| --- | --- |
| experience_id, protocol_version, experiment_id, configuration_id | Stable record identity and frozen protocol/model condition |
| participant_id, source_session_id | Anonymous participant and computer-use context; not a pomodoro session ID |
| run_id, scenario_id, task_id, occurrence, task_version, delivery_id | Link the specific first validated display and reminder occurrence |
| display_evidence_ref, displayed_at, receipt_source, clock, simulated | Reference actual accepted receipt/rendering evidence; log arrival time is not rendered_at |
| reminder_mode, delivery_reason, context_refs | Preserve hard/soft, original delivery reason, base-observation/sample and any independent-label references |
| eligible, eligibility_evidence_ref | Enrollment and capability rules fixed before observation; retain eligibility evidence |
| invitation_status, invited_at, invitation_deadline_at, response_deadline_at | pending / not_invited / invited / not_applicable, with actual/planned timestamps |
| not_invited_reason, not_invited_detail | Explicit terminal non-invitation reason; null outside not_invited. Detail is required for other |
| exclusion_reason | Eligibility exclusion only; never substitute it for not_invited_reason on an eligible experience |
| response_status, rating, submitted_at, response_origin, response_evidence_ref | pending / answered / declined / not_seen / timed_out / technical_error; rating is integer 0..3 only for answered |
| revision, supersedes_record_id, recorded_by | Append-only corrections and who recorded them; recorded_by does not become the respondent |

Required consistency rules: one primary experience per `(experiment_id, run_id, task_id, occurrence)`; retries do not create invitations. Keep invitation lifecycle and response lifecycle separate:

| Condition | Required encoding |
| --- | --- |
| Eligible, waiting for a user-initiated interaction before invitation deadline | invitation_status=pending; not_invited_reason, exclusion_reason, invited_at, response_status, rating, and submitted_at are null |
| Eligible, invitation will not be offered | invitation_status=not_invited; not_invited_reason is required; exclusion_reason, invited_at, response_status, rating, and submitted_at are null |
| Eligible and actually invited | invitation_status=invited; invited_at/response_deadline_at are present; not_invited_reason/exclusion_reason are null; response_status is explicit |
| Excluded at enrollment | eligible=false; invitation_status=not_applicable; exclusion_reason required; not_invited_reason, invited_at, response_status, rating, and submitted_at are null |

For an invited unanswered record, rating=null and response_status is pending, declined, not_seen, timed_out, or technical_error as appropriate. answered requires an on-time integer rating 0..3 and response evidence. Do not convert null to rating 0.

not_invited_reason is a closed enum in this draft:

- no_user_interaction_before_deadline
- participant_opted_out_after_enrollment
- invitation_capability_unavailable
- invitation_technical_error
- protocol_stop_before_invitation
- other

Use no_user_interaction_before_deadline only at or after invitation_deadline_at. Earlier waiting is pending. Use not_invited_detail=null unless explanatory detail is needed; other always requires a nonempty English explanation. Do not put raw personal content in it.
Pre-enrollment lack of consent/support belongs to exclusion_reason with eligible=false. An opt-out, capability loss, or invitation failure after eligible enrollment remains an eligible not_invited experience with the appropriate not_invited_reason. A technical failure after an actual invitation instead has response_status=technical_error. Preserve this distinction in counts.
All eligible records have a planned invitation_deadline_at. pending/not_invited records have response_deadline_at=null; actually invited records have response_deadline_at=invited_at+120 seconds. At report cutoff, keep pending invitations separate from pending invited responses.
Invitation and response windows are half-open: [rendered_at, invitation_deadline_at) and [invited_at, response_deadline_at). At the exact deadline, the corresponding window has expired. Use actual collection timestamps on the declared real clock; an on-time response cannot precede its invitation.
See the [metric report contract](metrics-spec.md#5-typed-metric-report-format) for status=preliminary, population scope, coverage, and warnings.
When aggregating eligible terminal not_invited records, place their reason distribution in not_invited_reason_counts and reconcile its sum with counts.not_invited. Keep eligibility exclusions in excluded and damaged/missing records in evidence_missing. See [report evidence accounting](metrics-spec.md#511-missing-evidence-and-invitation-reason-accounting); complete nonresponse records do not become evidence gaps merely because they contain no rating.
Store receipt source and collection origin independently: an automation or synthetic answer is not a real user report even if a native window exists. A real primary response requires user origin, validated native display, a real clock, an eligible participant, and the declared deadline rules. Keep simulated/offscreen records out of the real metric.

Proposed collection procedure, to be agreed with C before formal use:

1. Obtain participation permission and freeze eligible display cohorts, question, scale, timing, and analysis strata. Never filter invitations based on whether a result looks favorable.
2. After the first validated native display, offer one non-modal question at the next user-initiated interaction within 120 seconds of rendered_at. Do not open an unsolicited foreground popup to collect a disruption label. If there is no such interaction, close the record as not_invited with its reason; capability failures and opt-outs are also retained explicitly.
3. An invitation expires 120 seconds after invited_at. Save the first on-time submitted rating or explicit declined/not_seen response. After expiry, record timed_out; keep late responses for a separately flagged analysis, not the primary numerator/denominator.
4. Retain the original answer, timestamp, and evidence. Later corrections append revisions rather than silently replacing the primary response. Independent auditors may check linkage/status encoding, but do not rewrite the participant's experience.
5. At the frozen report cutoff, reconcile eligible displays, exclusions, invitations, rated responses, refusals, not-seen responses, timeouts, technical errors, pending records, and not-invited reasons. Late/missing records must remain visible.

The 120-second defaults are a proposed pilot protocol, not already collected evidence. Changes require a protocol revision before a new cohort starts. The real metric is not_measured until these records and C's display linkage exist; the current six reminder records do not supply them.

### 3.5 Work-session outcome records

Purpose: separate an ended timer, an explicit outcome review, and actual completed work. Link business session_id and source_session_id separately.

| Proposed field | Recording rule |
| --- | --- |
| outcome_record_id, protocol_version, experiment_id, configuration_id | Stable annotation identity and experimental condition |
| run_id, session_id, session_version_at_end, participant_id, source_session_id | Accepted business-session end and independent collection context |
| task_id, goal, planned_items, plan_evidence_ref | Optional linked task and stable planned work-item IDs fixed at session start; preserve later plan revisions |
| ended_at, end_command_id, end_event_ref, accepted_result_ref, snapshot_ref | Accepted end result/view and evidence at the terminal version, not just a button click |
| review_required, review_supported_at_enrollment, eligibility_evidence_ref, exclusion_reason | Predeclared denominator rules; do not relabel a failed form unsupported after seeing the outcome |
| review_status, offered_at, review_deadline_at, submitted_at, confirmation_origin | not_offered / pending / confirmed / confirmed_none / declined / timed_out / unavailable / invalid_evidence; actual user and time provenance |
| confirmation_evidence_ref, explicit_no_completed_items | Link an explicit user confirmation, including an affirmative statement that nothing was completed |
| items | Stable work_item_id, optional task_id, completed/incomplete/unassessed, source, evidence_ref, confirmation time |
| next_steps | Stable record IDs, text, optional related item IDs, confirmation/evidence references; distinguish missing from explicitly empty |
| revision, supersedes_record_id, adjudication_note | Preserve original records and audited corrections |

Proposed collection and annotation procedure:

1. Before the session, freeze its target items when an item-based completion metric is planned. Declare whether a review is required and supported, participant eligibility, and the review deadline policy.
2. Confirm end acceptance using SessionResult.view or a versioned WorkSession snapshot and its command/event reference. In current M0, session.applied records can contain kind/state/version without outcome details. That event alone cannot prove confirmed outcomes; collect the accepted view or C's linked review evidence.
3. Collect one explicit user review as part of ending, or via the predeclared follow-up process within 10 minutes after accepted end. A later D questionnaire is an external annotation, not an invented business command. Preserve offered_at and the fixed deadline; ending without a review is still a visible eligible case.
4. Record completed, incomplete, and unassessed items separately. A missing item response remains unassessed. A user explicitly confirming no completed items sets confirmed_none and explicit_no_completed_items=true; a blank/default confirmed_outcomes list is not sufficient evidence.
5. For confirmed/confirmed_none, require linked actual user confirmation and its time. confirmed_none must contain no user-confirmed completed items, but may contain incomplete items and next steps. Synthetic/automation records are separate diagnostics, not real user-confirmation evidence.
6. Trusted tool completion requires an agreed, task-relevant observable result and call/effect evidence, not merely an arbitrary tool's succeeded status. Count tool-verified items without user confirmation separately. A model summary or generated next-step suggestion remains unconfirmed until the user adopts it.
7. Deduplicate by `(run_id, session_id)` for review coverage and by stable work_item_id within a session for item counts. Retries, multiple versions, repeated snapshots, and revisions do not add sessions/items. Preserve source records; audit mapping disagreements without inventing missing outcomes.
8. At the frozen cutoff, report confirmed, confirmed_none, not_offered, pending, declined, timed_out, unavailable, invalid_evidence, exclusions, and evidence gaps. A UI/collection failure after enrollment does not remove a session that was eligible at enrollment.

User-confirmed completed/incomplete item counts require explicit user evidence. Tool-verified but unconfirmed items are separate. Without a complete predefined item denominator, publish counts only; omitted goals, arbitrary text strings, and session-ended events cannot create a completion rate.
The 10-minute rule is a proposed pilot default requiring agreement with C. Keep confirmation/achievement metrics not_measured until the end-state and review evidence are available. This does not block the reminder-only structure pilot.

## 4. Independent annotation and disagreements

1. Give both annotators the same anonymous materials/specification, hiding predictions and each other's answers.
2. Preserve original A/B labels. Report agreement before adjudication, jointly labelable count, unknowns, and unlabelable samples.
3. Record disagreement and review original evidence with the agreed adjudicator/process. Keep reasons. Use unknown when unresolved.
4. Preserve both original labels and the final label. Evaluate against frozen final labels; post-adjudication agreement is not independent 100-percent agreement.
5. A one-person pilot is single_annotator_dev, not completed double annotation.

## 5. Development/holdout split and controls

- Group by source_group_id and source_session_id before targeting 80/40 requests and 16/32 sessions. Keep variants and all samples from one session on one side.
- Group adjacent, strongly overlapping recordings together; do not split one usage episode into supposedly independent dev/holdout sessions.
- Respect grouping before counts. Collect additional independent data when necessary; do not split related samples merely to hit a ratio.
- Save explicit split lists, assignment procedure/seed when random, fingerprints, and freeze time. Do not tune prompts, thresholds, or budgets on holdout data.
- Validate on development data, freeze definitions/implementation/prompts/tools/scheduling/budgets, then run holdout comparisons. Disclose tuning after holdout inspection rather than calling it unseen evaluation.
- Count synthetic fixtures, real desktop sessions, manual GUI, and automated GUI separately.

## 6. Minimal data-to-report workflow

Materials -> independent A/B labels -> adjudication -> frozen labels/split -> fixed-configuration run -> event/prediction correlation -> evidence validation -> metrics -> failure/undetermined cases -> versioned report.

| Deliverable | Minimum content |
| --- | --- |
| Data inventory | Stable IDs, source, version, split, evidence references, actual counts |
| Original/final labels | A/B labels, disagreements, decisions, validity, revision history |
| Experience records | Display/context linkage, invitation protocol, actual user ratings/refusals, timing, nonresponses, and revisions |
| Work-outcome records | Accepted end evidence, eligibility, explicit confirmation/none, item/next-step references, and missing-review states |
| Scenarios/configurations | Executable inputs, expectations, estimator/model/budget versions |
| Run evidence | JSONL, reports, manifests, platform capabilities, source and clock types |
| Metrics/report | Numerators/denominators, exclusions, gaps, measurement status, failure cases |

## 7. Two beginner checks

**Reminder:** Three confirmed soft occurrences; one is cancelled before display/deadline adjudication. Of the two eligible fully observed occurrences, one displays in-window and one lacks a valid receipt. The verifiable window rate is 1/2=50 percent, with one cancelled exclusion.

**State:** Independent label working, base rule prediction available, effective state working after correction. Record a base-prediction conflict and a functioning correction. The corrected state does not make the original rule prediction correct.

These are teaching examples, not formal results. Establish the workflow on a small development pilot before scaling data collection.
