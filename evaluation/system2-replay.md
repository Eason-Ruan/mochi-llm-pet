# System 2 semantic replay

Run the following command from the repository root in the project Python environment.

```sh
python scripts/system2_semantic_replay.py --output /tmp/mochi-s2-replay --blind-seed 12 --check-semantic
```

The output directory must be new. Repeat `--scenario ID` to select cases from `evaluation/data/system2-semantic-replay.json`. `--check-semantic` exits nonzero when a scored semantic expectation fails, an action is missed or unexpectedly committed, a reminder is shown twice, or an expected recorded model turn is missing or mismatched. The report is written before this check.

Both arms replay the same synthetic events through the System 2 host, scheduler, SQLite business services, and display permits. `baseline` disables both semantic switches, and `semantic` enables both. Each scenario and arm gets fresh temporary business and Observation databases. The fake clock retains the fixture's original message time, event ordering, timezone, and elapsed delays. SQLite writes and display receipts count within the isolated scenario only. There is no desktop session, microphone, screenshot, network client, real alert, or audio playback. The simulated surface uses a foreground slot per render step and does not implement native notification delivery or Qt exposure and animation timing.

`report.json` pairs engine-proposed tools with host receipts, persisted item, phase, setting and habit changes, and simulated display outcomes. It separates a pending proposal from a counted display or phase start. Expected effects score misses, false actions, appropriate interventions, and unnecessary interruptions. Checks also cover item identity and title, follow-up questions, timestamps, merged wakes, automatic successors, presentation channels and preferences, and local hard deadlines. Denial counters count observations across stages, so a tool denial and a turn ending can both be represented. Replay turn latency is locally measured while pumping the host. Rates per hour reflect fixture elapsed time and the selected mix of cases.

`--blind-seed` writes `blind-review.json` with independently randomized A/B labels, fixture context and effects for each case. Share that packet with reviewers and keep `blind-key.json` and `report.json` private until scoring is complete. The packet omits implementation identities, event IDs, timing of model execution, raw prompts, raw tool arguments from recorded model output, and model reply text. It supports judging the choices and effects. It does not support comparing reply wording.

## Recorded model results

The default engine uses hand-authored fixture behavior. These runs measure runtime effects for bounded examples, including deterministic scope, time and presentation enforcement. Judging when a chat message gives consent to write and when a background action is appropriate requires model evidence. To evaluate actual model choices from a prior synthetic run, use `--recorded-model PATH` with a JSON artifact in the format below.

```json
{
  "schema": 1,
  "synthetic_inputs_only": true,
  "variants": {
    "baseline": {
      "colloquial-original-timestamp": {
        "request": [{"origin": "user", "trigger_reasons": ["user_message"], "reply": "我理解你的请求。"}]
      }
    },
    "semantic": {
      "colloquial-original-timestamp": {
        "request": [{
          "origin": "user",
          "trigger_reasons": ["user_message"],
          "calls": [{"action": "item.create", "args": {"type": "reminder", "title": "去趟洗手间", "reminder_rule": {"remind_by_offset_s": 600}}}],
          "reply": "我已处理。",
          "usage": {"requests": 2, "latency_ms": 1200, "cost_usd": 0.001}
        }]
      }
    }
  }
}
```

Use one list entry per invoked engine turn within each step. Captures identify the source and trigger to prevent replaying a reply for a different kind of event. Native tool names and canonical names both work. A capture can refer to fixture-created objects with `$alias.field` when IDs need rebinding. Optional `delay_s` advances the replay clock. `unavailable: true` and `parse_failure: true` replay those failure outcomes. The capture cannot create trusted synthetic user events. Keep captures limited to synthetic examples.

The tool makes zero provider requests while replaying either kind of input. Historical usage and cost are shown only where capture metadata provides them. An incomplete provider request or cost total stays `null`, and model latency reports its sample count. Fixture results and captured model results are labeled separately in the report.
