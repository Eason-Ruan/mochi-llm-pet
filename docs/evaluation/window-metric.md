# Evidence-backed M0 Soft-window Metric

Status: implemented, limited to the recorded M0 reference profile.
Profile: `m0-soft-window-v1`. Report/metric definition: `0.4`.

This tool calculates `delivery.soft_window_first_display_rate` from saved replay
evidence. It is not a native rendering benchmark, a model evaluation, or an extension
to A/B/C public event contracts. All new files use English and ASCII only.

## 1. Files

| File | Purpose |
| --- | --- |
| `scripts/d_calculate_window_metric.py` | Replay validation, occurrence classification, metric calculation, and audit export |
| `tests/test_d_window_metric.py` | Recorded replay and boundary tests |
| `docs/evaluation/window-metric.md` | Frozen calculator profile, evidence rules, and commands |

Existing replay, collection, business, and report-contract code stays unchanged.
The calculator uses the existing validators; it does not trust an old summary file.

## 2. Run from the repository root

Use new source-run and analysis directories. The analysis directory must be separate
from the recorded run, with neither directory containing the other.

```powershell
Set-Location "C:\Users\s11447\Desktop\mochi-llm-pet"
uv run --offline python -m unittest discover -s tests -p "test_d_*.py" -v

$dWindowSuffix = Get-Date -Format "yyyyMMdd-HHmmss"
$dWindowRun = "output/m0/d-window-run-$dWindowSuffix"
$dWindowAnalysis = "output/m0/d-window-analysis-$dWindowSuffix"

uv run --offline python scripts/d_replay.py --output $dWindowRun
if ($LASTEXITCODE -ne 0) { throw "Replay failed; stopped." }

uv run --offline python scripts/d_calculate_window_metric.py $dWindowRun --output $dWindowAnalysis
if ($LASTEXITCODE -ne 0) { throw "Window calculation failed; stopped." }

uv run --offline python scripts/d_check_metric_report.py "$dWindowAnalysis/metric-report.json"
```

The existing project environment with Pydantic is required; no additional packages,
credentials, model calls, GUI, or network access are needed by the commands.
`uv --offline` cannot fetch packages missing from the local environment/cache.

For one existing fixture, use `--scenario soft-opportunity` on the replay command.
Keep the selected source run and its complete `inputs/` snapshots for later audit.
No source file, trace, run report, or saved manifest is modified by analysis.

## 3. Frozen cohort and lifecycle rules

Use all saved cases in the recorded run; there is no post-outcome case filter.
Within those cases, include every accepted registered soft occurrence identifiable
from owner command results and lifecycle events. Identity is run/scenario/task/occurrence;
the occurrence number comes from owner state, never from parsing a delivery ID.
Hard occurrences and unconfirmed drafts are outside this metric, not successful cases.

Applied commands must match input actions, lifecycle identities, CAS history, and
owner versions. Duplicate/rejected commands do not create occurrences. An edit or
snooze creates a new row only when owner state explicitly establishes a new occurrence.
A task version increase alone does not create a business sample. A mode change within
one existing occurrence is outside this reference profile and stops analysis.

Freeze these classifications before interpreting results:

| Situation | Classification |
| --- | --- |
| First verifiable render in the closed confirmed window | success |
| First verifiable render outside that window | miss |
| Window closed with complete evidence but no valid display receipt | miss |
| Cancellation, edit, or snooze closes an undisplayed obligation before deadline adjudication | excluded, with its distinct closure reason |
| Cancellation/edit after a missed deadline | preserve the old miss |
| Cancellation after a valid display | preserve the historical display |
| Observation ends before the deadline without a display or qualifying withdrawal | undetermined; keep it in the eligible denominator |
| Accepted receipt has no unique verifiable owner render timestamp | undetermined |
| Completion before the deadline without a display | undetermined until an obligation rule is agreed |

At the deadline, input/commit order matters. A cancellation before that input's pump
can withdraw the obligation. A prior deadline delivery request prevents a later
same-time cancellation from removing the obligation. The calculator uses trace order
and owner transitions; timestamp equality alone does not decide the race.

Exclusion reason names are `cancelled_before_first_display_and_deadline`,
`edited_before_first_display_and_deadline`, and `snoozed_before_first_display_and_deadline`.
The latter two are this limited reference profile's explicit superseded-obligation
rule, not a claim about every future B implementation. Integrating another task
adapter requires agreeing its lifecycle rules before measurement.

## 4. Timestamp evidence and limitations

The current M0 display event omits `rendered_at`; its `ts` is receipt/event arrival
time. The calculator does not substitute that time for rendering time or infer a
window hit from `late_by_s=0` alone.

For an occurrence with exactly one accepted displayed event, require a consistent,
non-null `TaskView.displayed_at` from a later owner command-result snapshot or final
view of the same occurrence. Match the receipt to an earlier valid request and to the
registered task version active at that event. The timestamp must lie between the
original request and receipt arrival and agree with the receipt's lateness field.
Use the effective window of the accepted receipt's task version.

The audit records request/receipt sequence numbers, delivery IDs, task versions, and
an `owner_report_pointer` JSON Pointer into the per-case report. `snapshot_seq` is the
command event's sequence for a command-result view, or trace length plus one for the
final task view. The latter is a snapshot marker, not an invented trace event.

Owner occurrence history is required. A final TaskView alone cannot reconstruct old
occurrences or classify historical delivery. If multiple accepted display events
exist without per-receipt timestamps, do not infer their first render from one mutable
snapshot. Normal M0 duplicate receipts are rejected as duplicates and do not add samples.
Request retries likewise do not add occurrences.

This is a narrow evidence adapter for M0. Native/live integration needs owner-supplied
render timestamp and clock evidence; it must not inherit this profile's assumptions.

## 5. Report and audit outputs

| Output | Contents |
| --- | --- |
| `metric-report.json` | One v0.4 typed window metric with explicit counts, coverage, exclusions, gaps, and optional bounds |
| `cohort.json` | Every soft occurrence and its evidence references, cutoff, classification, and reason |
| `analysis-inputs/` | Exact snapshots of the calculator, used validators, metric definition, and this profile guide |
| `analysis-manifest.json` | Hashes of analysis inputs and both output JSON files, bound to the source run manifest hash |

Source run artifacts and input snapshots are validated before analysis and rechecked
before publication. A missing/incomplete run manifest, altered artifact, failed case,
or inconsistent report prevents publication in this starter workflow. The analysis
implementation/definition files are captured before calculation and checked afterward.
Keep the original run directory alongside the analysis: exported reports do not
duplicate all source logs and do not become independently verified native evidence.

Hashes detect accidental changes relative to saved manifests, not coordinated rewriting
of both files and hashes. Neither the typed report checker nor this calculator
authenticates data against an external trusted source.

`status=completed` and exit code 0 mean analysis/export completed. They do not mean
the metric is fully determined or an acceptance target passed. The metric can have
`status=insufficient_evidence`, `value=null`, and `assessment=not_evaluated`.
Exit code 1 means analysis/evidence validation failed. The typed report must also pass
the existing report-contract checker, which is invoked internally before publication.

## 6. Baseline interpretation

For the 25 built-in M0 fixtures, the expected soft cohort contains twelve occurrences
across the confirmed-soft cohort rule. Seven have verifiable first displays in their
windows. One (`soft-window-miss`) has a complete observation horizon that covers its
deadline but never produces a display, so it is a confirmed miss rather than missing
evidence. Three occurrences are explicitly excluded: `soft-cancel-before-deadline`,
the old occurrence of `soft-edit-before-deadline`, and the old occurrence of
`soft-snooze-before-deadline`. `soft-failure-retry` is a single successful occurrence
even though its first delivery attempt failed.

`expired-observation` ends at 6 seconds, before its 30-second soft deadline, without
a valid display. It remains one undetermined occurrence, not a measured miss. The
original result is kept as-is; it is neither re-judged as a success nor as a failure.

Expected result: numerator 7, denominator 9, confirmed successes 7, confirmed misses 1,
undetermined 1, excluded 3. Usable evidence coverage is 8/9. The primary value is null
with `insufficient_evidence`; the success-rate bounds are 7/9 to 8/9. Do not call either
endpoint the measured rate. The source suite can still pass all 25 scenario expectations.

A single completed `soft-opportunity` or `soft-deadline` fixture produces 1/1 in this
simulated profile. These are programmed replay outcomes, not model or real-user quality.
Real latency, real model cost, and human experience remain `not_measured`.

References: [metric specification](metrics-spec.md), [report validator](report-validator.md).
