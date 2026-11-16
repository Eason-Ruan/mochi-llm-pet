# D Metric Report Validator

Status: implemented starter validator for four metric definitions in report schema v0.4.
This is contract validation, not a completed experiment or a general metric calculator.
All files in this change use English and ASCII characters only.

## 1. Files and placement

Place these files under the existing repository root:

| File | Purpose |
| --- | --- |
| `scripts/d_check_metric_report.py` | Standard-library-only JSON contract checker and command-line entry point |
| `evaluation/data/metric-report-example.json` | The arithmetic-only example from section 5.4 of `metrics-spec.md`, extracted without changing its content |
| `tests/test_d_metric_report.py` | Positive, negative, boundary, and command-line regression tests |
| `docs/evaluation/report-validator.md` | This guide |

No existing replay runner, event contract, dataset, human annotation, or model configuration is changed.
The six reminder pilot samples are not inputs to this example report.

## 2. Run from the repository root

PowerShell:

```powershell
Set-Location "C:\Users\s11447\Desktop\mochi-llm-pet"
uv run --offline python -m unittest discover -s tests -p test_d_metric_report.py -v
uv run --offline python scripts/d_check_metric_report.py evaluation/data/metric-report-example.json
```

The validator and its tests require only the Python standard library. No model, credentials,
API calls, new packages, or network access are used. `uv --offline` still requires an existing
project environment and locally available dependencies; it does not install missing packages
from the network. In an already activated Python environment, `python` can be used directly.

Expected example summary:

```json
{
  "status": "passed",
  "validation_type": "metric_report_contract",
  "validator_profile": "four_starter_metrics",
  "result_schema_version": "0.4",
  "metric_count": 4,
  "metric_status_counts": {"computed": 2, "preliminary": 2},
  "example_only": true,
  "source_evidence_verification": "not_performed",
  "raw_percentile_recomputation": "not_performed",
  "models_called_by_validator": false
}
```

The actual output also includes `checked_report_sha256`, the SHA-256 of the exact checked
report bytes. This identifies the checked input; it does not fingerprint or authenticate
the experiment, source logs, labels, implementation, or configuration.

A valid contract returns exit code 0, even if a metric is preliminary, unavailable, or has
an acceptance assessment of failed. The summary's `passed` refers only to contract checking.
An invalid report returns exit code 1 with a JSON error naming the failed field or rule.
The command writes no files and does not modify the checked report.

## 3. Supported definitions

| Metric ID | Checks specific to this starter profile |
| --- | --- |
| `delivery.soft_window_first_display_rate` | Occurrence denominator, exclusions, evidence coverage, null point estimate with missing evidence, and optional success-rate bound arithmetic |
| `interruption.user_reported_rate` | Rated-response denominator, invitation and response accounting, non-invitation reasons, and preliminary respondent-only reporting |
| `cost.per_verified_displayed_occurrence` | Currency, verified native display denominator, call/outcome coverage, and null point estimate with incomplete accounting |
| `latency.hard_due_p95` | Millisecond units, P95/nearest-rank metadata, measured sample count, missing timing evidence, and preliminary subset reporting |

All definitions use `definition_version="0.4"`. Unsupported metric IDs and schema versions
are rejected explicitly. This profile does not implement every metric type or all six D
evaluation categories. Add an agreed definition and tests before extending the whitelist.

The checker rejects missing or undeclared report/result fields, duplicate metric/cohort/version
records, duplicate JSON keys, non-finite numbers, boolean counts, invalid units, wrong rate/cost
arithmetic, inconsistent coverage, and invalid gap-reason totals. Known zero gaps require `{}`;
unknown inventories remain null with warnings. With `not_measured`, evidence counts and reasons
must remain null. A zero denominator does not produce a numeric zero rate or unit cost.

The example deliberately contains two preliminary metrics. Its interruption ratings cover
only three of six experiences, and its latency measurements cover only three of four targets.
Those statuses must remain preliminary; changing them to computed does not repair coverage.

## 4. Evidence boundary

The checker validates only the declarations and internal arithmetic in one report. It does
not open `cohort_manifest_ref`, locate source events, correlate receipts, review human labels,
validate questionnaire timing, verify native rendering, audit invoices, or recompute a P95
from raw latency observations. A well-formed but invented report can pass these checks.
The guide's examples and tests are synthetic fixtures, not collected measurements.

An explicitly marked format example must have `evidence_type="synthetic_example"`,
`example_only=true`, and `assessment="not_evaluated"` for every result. Other reports need
nonempty dataset and cohort references, but their existence and authenticity are not verified.
A `synthetic_replay` report cannot present measured native display cost, real latency, or
user-reported experience under these real-world metric IDs. Simulated diagnostics need their
own definitions. The window-delivery metric can describe explicitly simulated evidence.

Success-rate bounds are checked only for the window metric with a known positive eligible
population, known confirmed successes, and an exact undetermined-occurrence count. Their
source classifications are still outside this validator. Unavailable latency/cost results
cannot use that success-rate bounds rule.

## 5. Next integration gate

Before producing a measured report, implement evidence-backed metric calculations with
frozen cohorts and explicit source references. Recompute values from those records, retain
the required source/configuration/label fingerprints, and then run this contract checker.
Human review of the reminder pilot, native instrumentation, user questionnaires, billing
collection, and model comparisons remain separate unfinished work.

Reference: [metrics specification](metrics-spec.md), [annotation guide](annotation-guide.md).
