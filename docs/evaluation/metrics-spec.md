# D Evaluation Metrics Specification v0.4

Status: pilot draft. A/B/C/D must agree on event boundaries, time windows, and billing evidence before a formal experiment.
Date: 2026-11-16. This document defines measurement rules; it does not claim that every metric or real experiment has been implemented.
Document language: English. The English pilot dataset is versioned separately as `pilot-en-v0.1`.
Revision 0.4 makes missing-evidence counts/reasons and non-invitation reason distributions explicit in every metric record. Revision 0.3 added typed results and preliminary/coverage handling; revision 0.2 added collection protocols and the cost denominator. These remain proposed contracts, not newly completed experiments.

## 1. Scope and ownership

D owns collection, event correlation, evaluation, comparisons, and reporting. Each module owner emits its own facts.
A provides sampling, base estimates, model calls, and tool results. B provides task versions, scheduling, and delivery facts. C provides visible-render receipts, user actions, and work-session outcomes.
Experimental logs do not replace B's persistent business records. A coordinates public schema changes; this document does not add public event fields.

References: [D handoff](../handoff/d-evaluation.md), [events](../contracts/events-replay.md), [task semantics](../contracts/task-semantics.md), [observation](../contracts/observation.md), and [GUI/work sessions](../contracts/gui-work.md).

## 2. Common measurement rules

1. Declare `synthetic_replay / native_scenario / live`, clock type, receipt source, actual model use, and platform capabilities. A native window driven by virtual time does not establish real latency.
2. Preserve complete JSONL, reports, input snapshots, source/configuration/scenario fingerprints, experiment configuration, and dataset version. Keep separate runs separate.
3. Group events by `run_id + scenario_id`; use `seq` for order within a run. Correlate task/version, request_id, call_id, delivery_id, and session_id as appropriate.
4. Display, failure, and timeout outcomes must match earlier valid requests, including task, version, and order. Record evidence errors before calculating usable metrics.
5. `delivery.displayed` establishes rendering evidence, not user attention, acknowledgement, or task completion. Completion requires an accepted completion command or an appropriate confirmed fact.
6. Every rate includes its numerator, denominator, exclusions with reasons, and missing-evidence count. Separate hard/soft reminders, platforms, configurations, and evidence sources.
7. Measurement status is separate from acceptance assessment. Use `not_measured`, `unsupported`, `not_applicable`, or `insufficient_evidence` as defined in section 5. A failed computation/format check has status=failed and value=null. A correctly measured threshold violation instead has status=computed and assessment=failed. Do not replace unavailable measurements with 0 or 100 percent.
8. Keep eligible samples with damaged evidence visible. Do not silently remove them to improve a rate. Where they prevent a complete point estimate, report bounds.
9. For N eligible samples, S confirmed successes, and U undetermined samples, the success-rate bounds are S/N and (S+U)/N. For N=0, both are null. A complete trace without a valid receipt means no verifiable display, not proof that no pixels ever appeared.

Metric measurement status and replay report status are separate. A fault scenario can pass because it handles a failure correctly; a run can fail because evidence validation fails. Neither is a substitute for a business success rate.

## 3. Reminder evaluation unit and cancellation

The delivery unit is one confirmed reminder occurrence: `run_id + task_id + occurrence`. Repeated attempts with the same delivery_id do not create new business samples. A version increment alone does not create a new occurrence.
Use B's accepted commands and order to interpret edits, snoozes, cancellations, effective windows, and task versions.

| Situation | Window-delivery treatment | Additional check |
| --- | --- | --- |
| Unconfirmed draft | Exclude from confirmed-reminder denominator | No scheduling before confirmation |
| Cancellation accepted before deadline adjudication, with no earlier valid display | Report as a withdrawn obligation and exclude explicitly | No new display after cancellation |
| Cancellation after display | Preserve the historical display | No reopening after cancellation |
| Cancellation/edit after a missed deadline | Preserve the original miss | Report the later user action separately |
| Snooze creates a new occurrence | Evaluate the new occurrence and retain the old closure reason | Old jobs/versions cannot deliver early |
| Missing occurrence or change history | Do not infer history from the final TaskView | Report insufficient correlation evidence |

Resolve simultaneous cancellation and due work using service commit order; replay uses `(at_s, seq)`. Agree on ties with B before the experiment, rather than changing exclusions after observing outcomes.

## 4. Six metric categories

### 4.1 Request semantics

Evaluate the first model interpretation before user edits. A user-corrected task does not establish correct initial parsing.

| Metric | Definition | Required evidence |
| --- | --- | --- |
| Strict interpretation accuracy | Requests with correct content, mode, timezone, window, target/action where applicable, and clarification behavior / assessable requests | Original request, fixed reference time, independent labels, first prediction |
| Field accuracy | Correct values for a field / applicable labeled requests for that field | Field applicability and predefined matching rules |
| Clarification behavior accuracy | Correct clarify/parse behavior / corresponding assessable requests | Expected behavior fixed before inference |

Compare instants in UTC and also check the requested timezone. Use predefined meaning criteria or independent review for content, not literal string equality as a substitute for semantics.
Apply intent-specific fields to create/edit/cancel requests. A structured GUI or scripted input does not establish natural-language parsing ability.

### 4.2 Delivery

Preserve the existing seven event/delivery counts:

| Field | Meaning |
| --- | --- |
| request_events | Number of delivery request events |
| unique_deliveries | Requested IDs deduplicated by run/scenario/delivery |
| retry_events | request_events - unique_deliveries |
| displayed_deliveries | Successfully validated displayed delivery IDs |
| timeout_events | Validated timeout events associated with requests |
| failed_events | Validated failed events associated with requests |
| without_display_receipt | Requested IDs without a valid successful display receipt |

These counts are not reminder completion rates. Additional metrics to implement:

- **Soft-window first verifiable display rate:** eligible soft-reminder occurrences whose first valid display falls in the confirmed closed interval `[earliest_at, latest_first_delivery_at]` / eligible soft-reminder occurrences. Separate simulated and native evidence. Use actual rendered_at and the effective task version; log arrival time is not rendering time.
- **Post-cancellation new-display violations:** new successful displays from cancelled tasks or their old versions after cancellation acceptance. Historical displays are not new violations.
- **Duplicate-render violations:** multiple official native cards for one delivery. Repeated requests/receipts alone do not establish duplicate rendering; C must provide card/render evidence.

A hard reminder's business window is a single due instant. Do not silently expand it by one second. Measure hard-reminder timeliness using due latency below. Any one-second-budget rate is a performance metric, not a new business permission window.

Example: three confirmed soft occurrences, one withdrawn before display and deadline adjudication, and two fully observed eligible occurrences. One displays within its window; the other has no valid display receipt. The window rate is 1/2=50 percent, with one cancelled exclusion. Request-event count is not the denominator.

### 4.3 Responsiveness and actual due latency

Subtract timestamps only when they share a comparable monotonic clock in the same real run. Threads can share a process clock; independent clocks across processes/restarts cannot simply be subtracted.

| Metric | Start -> end | Report |
| --- | --- | --- |
| GUI action response | User action received -> corresponding visible acknowledgement painted | P50/P95, measured sample count, failures, timeouts |
| Hard-reminder due latency | Scheduled due instant on the monotonic clock -> first valid native render | Raw differences, early-render violations, P50/P95 of nonnegative latency, missing displays |

`delivery.requested -> displayed` omits due-to-request scheduling delay and is not full due latency. Coordinate the missing instrumentation with A/B/C; these are required future measurement points, not claims about existing events.
Use nearest-rank percentiles: the sorted value at rank ceil(p*n), with p=0.50 or 0.95. Report missing/timeouts separately instead of presenting percentiles alone.
Project targets are GUI response <100 ms and hard-reminder P95 <1 s. Current replay evidence does not verify these targets. Review focus-stealing behavior separately.

### 4.4 Interruption and state estimation

- **Soft-opportunity conflict rate:** first opportunity displays before final fallback whose independent labels are working/away / such displays labeled working/available/away. Separate hard reminders, deadline_fallback, and retries with unknown original reasons. Required deadline fallback is not automatically an opportunity-policy error.
- **User-reported interruption rate:** on-time user ratings of 2 or 3 / on-time user ratings of 0, 1, 2, or 3, using the project-defined scale 0=none, 1=slight, 2=moderate, 3=severe. The primary threshold is 2; freeze it before collecting data. This metric concerns reported experience, not the opportunity-policy conflict metric above.
- **Base-estimate accuracy:** correct base estimates / deduplicated samples with independent working/available/away labels. An unknown prediction is undetermined and not correct.
- Report incorrect non-unknown predictions, unknown predictions, state counts/confusion tables, missing or unknown labels, expired/future/partial-capability samples, and sampling/estimation errors. A gold unknown label is not a known-state truth for primary accuracy.

Deduplicate by configuration and sample_id. Compare base `frame.estimated`, not the effective state after correction. The comparison instant must lie within the intersection of raw-sample, base-estimate, and independent-label validity intervals. See [annotation rules](annotation-guide.md).

For interruption ratings, the experience unit is one first validated native display of `run_id + task_id + occurrence`. Deduplicate retry requests and display IDs for that occurrence. Link the experience to the particular delivery, receipt, participant, and computer-use context. Hard, soft-opportunity, and deadline-fallback experiences are reported separately; required fallback can still be subjectively disruptive.
Use the [experience protocol](annotation-guide.md#34-interruption-experience-records). Only actual user responses from the declared real collection protocol qualify; automation, synthetic examples, inferred state, lack of a click, and operator guesses do not.

Always report N_eligible_experiences, N_invitation_pending, N_invited, N_rated, N_declined, N_not_seen, N_timed_out, N_response_pending, N_technical_error, and N_not_invited, with reasons and exclusions. Rated, declined, not_seen, timed_out, response_pending, and invited technical_error counts must sum to N_invited. N_invited + N_not_invited + N_invitation_pending must sum to N_eligible_experiences. Keep non-invited/pending-invitation experiences out of invited-response counts; report not_invited_reason counts separately from exclusion_reason counts.
Report invitation coverage=N_invited/N_eligible_experiences, rated response coverage=N_rated/N_invited, and overall rated coverage=N_rated/N_eligible_experiences. A zero denominator is not_applicable. Disruption rate with no rated responses is not_applicable. If ratings cover fewer than all eligible experiences, publish the respondent-only rate as status=preliminary, value_scope=observed_respondents, and assessment=not_evaluated, with coverage and NONRESPONSE_COVERAGE warnings. This remains true when unanswered records have terminal refusal/timeout states. It is not a full-population estimate. See section 5 for the result representation.

Worked example: 6 eligible experiences, 5 invitations, 3 valid ratings [0,2,3], 1 decline, 1 timeout, and 1 not-invited experience. Interruption rate is 2/3; rated response coverage is 3/5; invitation coverage is 5/6. Neither refusal nor silence is a zero rating. The example is arithmetic only, not collected user data.
Until C/D implement the invitation/response records and evidence links, this metric remains not_measured and is not included in formal performance claims.

### 4.5 Cost and model comparisons

Total experiment cost covers Jev, System 2, retries, extra verification, and actually billed failed requests. Preserve model/endpoint version, call IDs, usage, currency, price source/effective date, cached/input/output prices, and failure reasons.
Separate provider-reported billed cost from usage-based estimates. Missing usage means unknown cost, not zero. With no real model calls, real-model cost and latency remain not_measured.

- Report total cost and **cost per predefined input request**, whose denominator is the frozen input-request manifest, including failed, cancelled, and clarification cases as specified in that manifest. Also report the explicitly defined displayed-occurrence metric below; do not use the unqualified phrase "cost per successful occurrence".
- Reduction = 1 - candidate_total_cost / reference_total_cost. Require the same complete input set, accounting scope, and positive baseline cost. Incomplete accounting does not support a complete reduction claim.
- Main comparison: the same Jev + medium System 2 versus the same Jev + stronger System 2. Fix data split, prompts, tools, scheduling, timeout, retry, and budget rules, and preserve actual implementation/configuration versions.
- Original Mochi is a response/call-frequency reference, not a reminder-completion baseline. At least 30 percent cost reduction is an unverified target. Present quality metrics alongside cost; dropping tasks is not evidence of successful optimization.

#### Primary displayed-occurrence cost denominator

`cost.per_verified_displayed_occurrence` = total accounted model/API cost for the entire predefined experiment cohort / N_verified_displayed_occurrences.

A verified displayed occurrence is a unique `(run_id, task_id, occurrence)` in that cohort with at least one valid `native/displayed` receipt matched to an earlier request, the task version valid when rendered, and the permitted task lifecycle. Count it once even if there are retries or repeated receipts. Determine occurrence from B's command/state evidence, not by guessing an ID string format.
This success definition is **validated native display**, not in-window first display and not user-confirmed task completion. A late but otherwise valid native display counts here and is separately flagged in timeliness metrics. A display after accepted cancellation does not qualify; a valid pre-cancellation display remains historical evidence. Late post-run receipt arrival can qualify only if its actual rendering belongs to the frozen observation horizon and the receipt is validated before the accounting result is finalized.

The numerator covers all in-scope cohort calls, including failed attempts, retries, validation, and costs for cases without a qualifying display. Deduplicate call IDs, declare shared/setup-call allocation in advance, and keep unresolved costs visible. Do not sum only calls belonging to successful displays. Use the same input cohort, cost scope, observation horizon, and settlement rule for both model configurations.
If no qualifying occurrence exists, value=null and status=not_applicable; still report the total incurred cost. If cost or occurrence evidence is incomplete, value=null and status=insufficient_evidence, with known cost/counts and missing items. Pending or cancelled calls with unknown eventual billing prevent a complete cost result. No real model calls means not_measured, not a measured free model.
For synthetic/offscreen receipts, real-world displayed-occurrence cost remains not_measured; any simulated diagnostic must use a separately named metric and explicit evidence source. Real API costs incurred in a synthetic-input experiment can still be reported as total/per-input costs with their actual provenance.

Worked example: four frozen input requests incur USD 1.20 in total. Two occurrences have valid native displays, one of them late; another request is cancelled without display and another fails without display. If only one task is user-completed, the displayed-occurrence denominator is still 2. Cost per verified displayed occurrence is USD 0.60, and cost per input request is USD 0.30. Also report one late display; do not relabel this as cost per on-time delivery or per completed task.
Preserve included/excluded occurrence IDs with reasons, their accepted request/receipt references, cost-call IDs, currency/pricing revision, and manifest fingerprints. Alternative on-time or completed-task cost metrics need distinct IDs and definitions.

### 4.6 Work assistance

- **Outcome-confirmation record coverage:** unique ended work sessions with a valid explicit user outcome confirmation / unique ended work sessions predeclared to require and support the review protocol. Count each `(run_id, session_id)` once, not every end/retry/version event. Freeze support and eligibility before the session; a failed form or missed invitation does not remove an eligible session afterward.
- A user explicitly confirming no completed items counts as a completed review, not as a completed item. An empty list, omitted field, timeout, or automatic default is not such confirmation. Preserve excluded sessions with reasons and undetermined sessions with evidence gaps.
- Report user-confirmed completed items, user-confirmed incomplete items, unassessed items, and next-step records separately. Tool-verified items without user confirmation are a separate count, not user-confirmed achievements. Without a frozen target-item list, do not compute a completion fraction; without item IDs, report record counts rather than distinct-item totals.
- Report timer error, consumption while paused, duplicate starts, and restart consistency for assessable cases.

Use user-confirmed facts or permitted trustworthy tool results. Session ending, application dwell time, model thought, and pet mood do not establish completed work. Ending a session does not automatically complete associated tasks.
Use the [work-outcome protocol](annotation-guide.md#35-work-session-outcome-records). Count only a valid, linked, on-time confirmation as reviewed. If confirmation evidence is damaged, apply the common insufficient-evidence/bounds rule; do not silently exclude the session. Report not_offered, pending, declined, timed_out, unavailable, and invalid_evidence separately from confirmed/confirmed_none.
Example: 5 eligible ended sessions, 3 valid reviews with completed items, 1 explicit confirmation of no completed items, and 1 declined review give coverage 4/5. The explicit-none session contributes zero completed items. This is an illustrative example, not an observed result.
Until C/D supply end-command/view evidence plus explicit confirmation records, outcome-confirmation and achievement metrics remain not_measured. The existing reminder pilot does not implement or validate this collection.

## 5. Typed metric report format

This is the proposed D-side format for future metric reports. It is not a public TraceEvent/Scenario or the current reminder structure-checker's output, and no generic report validator/calculator is implemented by this document.

### 5.1 Report context and common result fields

A report contains context plus a results array. A single-metric report still uses a one-element array. Report-level context is inherited by every result.

| Report field | Contract |
| --- | --- |
| result_schema_version | String "0.4" for this report format, independent of dataset/public-event schema versions |
| experiment_id, configuration_id | Stable experiment and actual configuration identities |
| dataset_version, cohort_manifest_ref | Frozen dataset/cohort identity and evidence reference; required for measured reports, nullable only in explicitly marked format examples |
| evidence_type, example_only | Actual evidence category; synthetic_example with example_only=true is reserved for format/arithmetic examples and cannot support performance claims |
| as_of | Report cutoff as an ISO timestamp with UTC offset; use it to resolve invitation/response deadlines |
| results | Nonempty list of metric records; metric_id + cohort_id + definition_version must be unique within a report |

| Per-result field | Contract |
| --- | --- |
| metric_id, definition_version, cohort_id | Exact metric meaning/version and the manifest cohort used for this result |
| metric_type, value, unit, currency | Typed numeric result and its units; see the type table below. value is a finite JSON number or null, never NaN/Infinity or a formatted string |
| status, assessment, value_scope | Measurement availability, acceptance judgment, and population actually described; see section 5.3 |
| numerator, denominator, numerator_unit, denominator_unit | Required for rates, unit costs, and relative changes; otherwise null. Preserve the measured components even when a zero denominator prevents division |
| statistic, quantile, quantile_method, sample_count | statistic=scalar with other statistical fields null, or percentile with the requirements below |
| counts, excluded | Named nonnegative JSON integer counts with definitions/units fixed by metric/cohort; unknown counts are null and require an explanatory warning. Exclusions remain separate from nonresponse/evidence gaps; never mix unlike units |
| evidence_missing | Required nonempty object of declared evidence domains. Every domain has unit, count, and reason_counts; explicit zero is distinct from unknown/unmeasured. See section 5.1.1 |
| not_invited_reason_counts | Required field: interruption reason-count map when established, or null with the specified evidence-gap warning when unestablished; null for unrelated metrics. Keep separate from exclusions and response states; see section 5.1.1 |
| coverage | Named coverage records, each with unit, numerator, denominator, value, and status. Require explicit applicable coverage; unknown inventory is not 100-percent coverage |
| warnings | Array of objects with nonempty code/message. Use [] when none apply; preliminary/unavailable/failed results require an explanatory warning |

Coverage uses counts of the same declared unit: 0 <= numerator <= denominator. For denominator>0, status=computed and value=numerator/denominator; for denominator=0, status=not_applicable and value=null. An unknown numerator/denominator uses null, status=insufficient_evidence, and an explanatory result warning.
Metric-specific named counts/coverage describe their own populations: occurrence evidence, rated experiences, billable-call accounting, or actual latency samples. Do not divide a call count by an occurrence count to claim evidence coverage.
For success-rate evidence bounds in section 2, an optional bounds object contains lower, upper, unit=fraction, and value_scope=eligible_cohort; 0 <= lower <= upper <= 1. The unsupported primary point value remains null. Do not invent cost/latency bounds from this success-rate rule.

### 5.1.1 Missing evidence and invitation-reason accounting

Every result must contain evidence_missing. Each domain is an object with these explicit fields:

| Field path | Contract |
| --- | --- |
| evidence_missing.<domain>.unit | Unit of affected eligible entities, such as occurrence, experience, call, or timing_target |
| evidence_missing.<domain>.count | Number of distinct eligible entities whose required evidence is missing/unusable; a nonnegative JSON integer, or null when the inventory/count is not established |
| evidence_missing.<domain>.reason_counts | Counts by the primary evidence-gap reason. For an exact count, values are nonnegative JSON integers and their sum equals count. A verified zero uses {} |

Freeze required domains in each metric definition; do not omit a domain because its count is zero or unknown:

| Metric in the illustrative report | Required domains and units |
| --- | --- |
| delivery.soft_window_first_display_rate | delivery_evidence: occurrence |
| interruption.user_reported_rate | experience_records: experience |
| cost.per_verified_displayed_occurrence | cost_accounting: call; delivery_evidence: occurrence |
| latency.hard_due_p95 | latency_measurements: timing_target |

Other metric definitions must declare their domains/units before implementation. A comparison may use separate candidate/reference domains with the same unit. Never add call gaps and occurrence gaps into an untyped missing total.
A complete known inventory permits exact counts. An unestablished inventory uses count=null, with an EVIDENCE_INVENTORY_UNKNOWN warning; reason_counts may contain only observed lower-bound counts, or be null when nothing has been assessed. With no measurement attempted, use count=null and reason_counts=null and explain the not_measured status. Do not use count=0 to mean unknown.
For exact counts, assign one primary reason to each affected entity in this fixed order: missing_record, invalid_record, missing_linkage, missing_required_value. Preserve additional errors in source evidence, but do not count one entity repeatedly within a domain. The report's domain/cohort evidence identifies the affected entity IDs and their primary assignments for audit.
Known refusal, not_seen, timeout, or not_invited statuses are not automatically missing evidence: an intact record can conclusively establish nonresponse. Likewise, a fully observed failed delivery is not a missing record. Keep these operational outcomes in counts while evidence_missing reports only missing/unusable evidence. Missing a required latency pair does count as missing_required_value for its timing_target.

For interruption.user_reported_rate, not_invited_reason_counts is a map using only the closed enum in [the experience protocol](annotation-guide.md#34-interruption-experience-records). With a known inventory, require:

- sum(not_invited_reason_counts.values()) = counts.not_invited.
- counts.not_invited=0 requires {}, not null.
- Count each eligible terminal not_invited experience once, under its stored reason; do not aggregate excluded, invitation-pending, or actually invited records here.
- A known not_invited record without a valid required reason is an invalid_record in evidence_missing.experience_records. Set not_invited_reason_counts=null with an INVITATION_REASON_EVIDENCE_GAP warning rather than publishing a trusted distribution. Do not invent other as a missing-reason placeholder; other requires its recorded explanation.
- If counts.not_invited or the complete reason distribution cannot be established, use not_invited_reason_counts=null and an INVITATION_REASON_EVIDENCE_GAP warning. Preserve any known records for later audit rather than manufacturing a zero or a reconciled-looking distribution.

For unrelated metrics, not_invited_reason_counts=null means this distribution is not applicable, not that the metric has zero invitations. It is separate from excluded and from evidence_missing.<domain>.reason_counts; those three maps describe different populations/causes.

### 5.2 Metric types and units

| metric_type | Required units and interpretation | Valid value |
| --- | --- | --- |
| ratio | unit=fraction; numerator/denominator units identify comparable counts | 0..1, not a displayed percent |
| relative_change | unit=fraction; monetary comparisons also declare currency and monetary component units | Signed fraction under the metric formula; cost reduction can be negative when candidate cost increases |
| count | unit names the counted object, such as event, occurrence, session, or item | Nonnegative integer |
| duration | Canonical unit=ms for these latency reports; do not mix seconds and milliseconds | Nonnegative numeric duration; report negative early-render differences as separate violations |
| cost | unit=currency for totals, or currency_per_input_request / currency_per_verified_displayed_occurrence for the defined normalized costs; currency is required | Nonnegative monetary value with the declared currency/basis |

currency is an uppercase ISO-style code such as USD, not a currency symbol. It is null for nonmonetary results; a monetary relative_change also declares the source currency even though its final value is dimensionless. Never add currencies without a separately frozen conversion rule/source/date.
For unit costs, numerator_unit=currency and denominator_unit names the normalization basis. For monetary relative_change, both component units are currency; cost reduction uses 1 - candidate/baseline, not numerator/denominator alone.
For a duration percentile, statistic=percentile, quantile is 0.50 or 0.95, quantile_method=nearest_rank, and sample_count is the positive number of valid latency observations. value is a duration in ms, not the quantile itself or a fraction. Preserve measured values/evidence references and missing/timeout counts. Compute from unrounded observations; round only for presentation.

### 5.3 Availability, preliminary values, and acceptance

| status | Numeric value and scope |
| --- | --- |
| computed | Valid completed calculation for the explicitly declared metric/cohort; any predeclared coverage requirements are satisfied |
| preliminary | Non-null descriptive value from a valid subset only where the metric explicitly permits it; requires subset value_scope, coverage, counts, and warnings |
| not_measured | Instrumentation/experiment not executed; value=null |
| unsupported | Declared platform capability unavailable; value=null |
| not_applicable | Measurement exists but its definition has no applicable denominator/population, including a zero denominator; value=null |
| insufficient_evidence | Required linkage, cost settlement, or primary outcome evidence is incomplete/invalid; value=null and report known components/gaps or valid success-rate bounds |
| failed | Calculation or result validation failed; value=null and include the failure reason |

value_scope is eligible_cohort, observed_respondents, measured_subset, or not_available. Unavailable/null-point results use not_available; bounds can separately describe eligible_cohort.
assessment is passed, failed, or not_evaluated. It evaluates an agreed acceptance criterion, not whether arithmetic completed. Use not_evaluated for preliminary or unavailable results, and for computed results with no predeclared acceptance rule. A computed violation count can legitimately have assessment=failed.

Two permitted subset cases are defined here:

- Interruption: when N_rated>0 but N_rated<N_eligible_experiences, publish the rating-based fraction as preliminary/observed_respondents. Include rated response and overall rated coverage, invitation states, and a NONRESPONSE_COVERAGE warning; never classify missing answers as zero ratings.
- Latency: when valid latency observations exist but do not cover all eligible timing targets, a descriptive P50/P95 may be preliminary/measured_subset. Include LATENCY_COVERAGE_GAP, measured/eligible/timeout/missing counts, and sample_count. It cannot establish the full-cohort latency target.

preliminary is not a general escape hatch: damaged delivery-rate evidence and incomplete displayed-occurrence cost accounting still require a null primary value under the earlier definitions. With no observations use the appropriate null status, not a fabricated subset estimate. An example can illustrate computed/preliminary records only when its report is explicitly example_only.

### 5.4 Illustrative report

The following values are arithmetic examples, not results from the six reminder records or from real users/models. Each cohort_id denotes a separate illustrative population; no measured dataset or cohort manifest is asserted.

```json
{
  "result_schema_version": "0.4",
  "experiment_id": "result-format-demo",
  "configuration_id": "none-demo",
  "dataset_version": null,
  "cohort_manifest_ref": null,
  "evidence_type": "synthetic_example",
  "example_only": true,
  "as_of": "2026-11-16T05:00:00Z",
  "results": [
    {
      "metric_id": "delivery.soft_window_first_display_rate",
      "definition_version": "0.4", "cohort_id": "window-demo",
      "metric_type": "ratio", "status": "computed",
      "assessment": "not_evaluated", "value_scope": "eligible_cohort",
      "value": 0.5, "unit": "fraction", "currency": null,
      "numerator": 1, "denominator": 2,
      "numerator_unit": "occurrence", "denominator_unit": "occurrence",
      "statistic": "scalar", "quantile": null,
      "quantile_method": null, "sample_count": null,
      "counts": {"total_occurrences": 3, "eligible_occurrences": 2},
      "excluded": {"cancelled_before_first_display": 1},
      "evidence_missing": {
        "delivery_evidence": {"unit": "occurrence", "count": 0, "reason_counts": {}}
      },
      "not_invited_reason_counts": null,
      "coverage": {
        "usable_occurrence_evidence": {"unit": "occurrence", "numerator": 2, "denominator": 2, "value": 1.0, "status": "computed"}
      },
      "warnings": []
    },
    {
      "metric_id": "interruption.user_reported_rate",
      "definition_version": "0.4", "cohort_id": "experience-demo",
      "metric_type": "ratio", "status": "preliminary",
      "assessment": "not_evaluated", "value_scope": "observed_respondents",
      "value": 0.6666666666666666, "unit": "fraction", "currency": null,
      "numerator": 2, "denominator": 3,
      "numerator_unit": "rated_experience", "denominator_unit": "rated_experience",
      "statistic": "scalar", "quantile": null,
      "quantile_method": null, "sample_count": null,
      "counts": {"eligible_experiences": 6, "invited": 5, "rated": 3, "declined": 1, "not_seen": 0, "timed_out": 1, "response_pending": 0, "technical_error": 0, "not_invited": 1, "invitation_pending": 0},
      "excluded": {},
      "evidence_missing": {
        "experience_records": {"unit": "experience", "count": 0, "reason_counts": {}}
      },
      "not_invited_reason_counts": {"no_user_interaction_before_deadline": 1},
      "coverage": {
        "invitation": {"unit": "experience", "numerator": 5, "denominator": 6, "value": 0.8333333333333334, "status": "computed"},
        "rated_response": {"unit": "experience", "numerator": 3, "denominator": 5, "value": 0.6, "status": "computed"},
        "overall_rated": {"unit": "experience", "numerator": 3, "denominator": 6, "value": 0.5, "status": "computed"}
      },
      "warnings": [{"code": "NONRESPONSE_COVERAGE", "message": "Only rated respondents contribute; do not generalize to every eligible experience."}]
    },
    {
      "metric_id": "cost.per_verified_displayed_occurrence",
      "definition_version": "0.4", "cohort_id": "cost-demo",
      "metric_type": "cost", "status": "computed",
      "assessment": "not_evaluated", "value_scope": "eligible_cohort",
      "value": 0.6, "unit": "currency_per_verified_displayed_occurrence", "currency": "USD",
      "numerator": 1.2, "denominator": 2,
      "numerator_unit": "currency", "denominator_unit": "verified_displayed_occurrence",
      "statistic": "scalar", "quantile": null,
      "quantile_method": null, "sample_count": null,
      "counts": {"input_requests": 4, "verified_displayed_occurrences": 2, "late_displays": 1, "user_completed_tasks": 1},
      "excluded": {},
      "evidence_missing": {
        "cost_accounting": {"unit": "call", "count": 0, "reason_counts": {}},
        "delivery_evidence": {"unit": "occurrence", "count": 0, "reason_counts": {}}
      },
      "not_invited_reason_counts": null,
      "coverage": {
        "accounted_calls": {"unit": "call", "numerator": 5, "denominator": 5, "value": 1.0, "status": "computed"},
        "occurrence_outcomes": {"unit": "occurrence", "numerator": 4, "denominator": 4, "value": 1.0, "status": "computed"}
      },
      "warnings": []
    },
    {
      "metric_id": "latency.hard_due_p95",
      "definition_version": "0.4", "cohort_id": "latency-demo",
      "metric_type": "duration", "status": "preliminary",
      "assessment": "not_evaluated", "value_scope": "measured_subset",
      "value": 900.0, "unit": "ms", "currency": null,
      "numerator": null, "denominator": null,
      "numerator_unit": null, "denominator_unit": null,
      "statistic": "percentile", "quantile": 0.95,
      "quantile_method": "nearest_rank", "sample_count": 3,
      "counts": {"eligible_timing_targets": 4, "measured": 3, "missing_latency": 1, "timed_out": 0, "early_render_violations": 0},
      "excluded": {},
      "evidence_missing": {
        "latency_measurements": {"unit": "timing_target", "count": 1, "reason_counts": {"missing_required_value": 1}}
      },
      "not_invited_reason_counts": null,
      "coverage": {
        "latency_measurements": {"unit": "timing_target", "numerator": 3, "denominator": 4, "value": 0.75, "status": "computed"}
      },
      "warnings": [{"code": "LATENCY_COVERAGE_GAP", "message": "P95 uses three observed latencies [100, 200, 900] ms; one eligible target is missing."}]
    }
  ]
}
```

## 6. Pilot acceptance and integration checklist

1. Trace every ratio to included/excluded sample IDs, label versions, and events.
2. Reproduce the same inputs/configuration; repeated sample reads do not increase sample size.
3. Handle zero denominators, cancellations, missing labels, damaged traces, and failure/recovery explicitly.
4. Keep real latency, cost, and native display unmeasured without supporting evidence.
5. Agree with A on first interpretations, base estimates, calls/usage; B on occurrences, windows, and cancellation ties; C on visible painting, action timing, and outcome confirmation.
6. Fingerprint every actual adapter/configuration when integration expands. This document does not change the old runner's D-sink-only restriction.
7. Add content fingerprints for metric definitions, labels, split lists, and prices to the formal experiment manifest. The existing runner does not automatically include these Markdown files or future datasets.
8. Freeze questionnaire timing/scale/threshold, work-review eligibility/deadlines, and cost-success definition. Verify their records before including these metrics in a formal report. A structure-checked reminder dataset does not satisfy this gate.
