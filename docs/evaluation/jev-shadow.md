# Jev shadow evaluation

See [Carrot setup](../../README.md) for installation and model configuration.
Jev is an optional experiment for eligible background ticks. Desktop shadow
mode records what Jev would recommend while System 2 handles scheduled turns.

## Offline replay

From the checkout root, run the synthetic cases with a fixed stand-in.
Choose a new output path for each run.

```sh
uv run --locked python -m evaluation.jev_shadow_replay --jev fixture --output report.json
```

Use `--jev off` for the no-Jev baseline. Use `--case` with a case ID to run
its complete scenario and preserve earlier tick history. The JSON records
sanitized per-tick results.

## Real Jev requests

Set `MOCHI_JEV_SHADOW=1` and supply a TypeSafe key through `JEV_API_KEY`.
On macOS the app can also read it from Keychain service `Mochi Jev`, account
`default`. The default model is `jev-1.14.0`. Set `MOCHI_JEV_SHARE_TEXT=1` to
permit the supported sanitized activity descriptions to be shared.

Use `--jev real` to call Jev. Add `--system2-settings PATH` to measure actual
System 2 requests made through a verified Bailian route in that SQLite
settings file. These replays use synthetic observations. The real modes
can incur provider charges.
