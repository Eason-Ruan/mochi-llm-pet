# Original desktop pet

This guide covers the original sprite-driven pet available through `--legacy`.
See the [Carrot README](../README.md) for the current focus, reminder, and chat
features.

[Historical demo video](https://github.com/user-attachments/assets/4e0e6054-b2d3-41e2-8e87-6cdb13ce9ff7)
· [Demo file in the repository](../assets/demo.mp4)

## Run and configure

Install the [locked dependencies](../README.md#quick-start-windows-and-macos).
From the checkout root, copy [config.example.toml](../config.example.toml)
to `config.toml`, edit the model settings, and run:

```sh
uv run --locked python -m deskpet --legacy
```

Pass `--config PATH` for a different configuration file. The original
Windows entry point `python run.py --legacy` runs the same mode.

| Provider | Configuration |
| --- | --- |
| Ollama | Select `ollama`. Start Ollama and pull the configured model, `gemma4:12b` in the example. |
| Gemini | Select `gemini` and set `GEMINI_API_KEY`. |
| OpenAI | Select `openai` and set `OPENAI_API_KEY`. |
| Anthropic | Select `anthropic` and set `ANTHROPIC_API_KEY`. |
| OpenAI-compatible | Select `openai_compat` and set the service's `base_url`. |

Use `DESKPET_LLM_PROVIDER`, `DESKPET_LLM_MODEL`, `DESKPET_LLM_BASE_URL`, and
`DESKPET_LLM_API_KEY` to override local settings. For a remote Ollama instance,
use its reachable Base URL and restrict service access to intended clients.

## Interact with the cat

Move the pointer back and forth over the cat to pet it, click to poke it, or
drag and release it to throw it. Local motion and physics run independently of
model calls. On each decision the model can return an animation intent and a
short speech line based on the scene and recalled history.

The screenshot and window-title settings are in `[vision]` and `[perception]`.
With a local Ollama instance the model requests stay on-device. With a cloud
provider the enabled image and text inputs go to that provider. The local
`deskpet.db` stores the pet's remembered facts, episodes, mood, and bond.

## Run three cats on Windows

Double-click [start_mochis.bat](../start_mochis.bat) to launch three legacy cats
with separate profiles. Stop them with [stop_mochis.bat](../stop_mochis.bat).

| Cat | Config | Sprite manifest |
| --- | --- | --- |
| Ginger | [config_ginger.toml](../config_ginger.toml) | [anim_manifest.yaml](../assets/anim_manifest.yaml) |
| Grey | [config_grey.toml](../config_grey.toml) | [anim_manifest_grey.yaml](../assets/anim_manifest_grey.yaml) |
| Grey-white | [config_grey_white.toml](../config_grey_white.toml) | [anim_manifest_grey_white.yaml](../assets/anim_manifest_grey_white.yaml) |

Use `cscript //nologo start_ginger.vbs`, `start_grey.vbs`, or
`start_grey_white.vbs` for an individual launch. Each script sets
`DESKPET_MANIFEST` and passes its own config file.

## Sprite development

The repository includes the three sheets and manifests from
[Bow.Pixel's Cat 85+ pack](https://bowpixel.itch.io/meow-cat-85-animation).
Animations cover movement, emotional resting poses, expressive emotes, and
variants. After editing an Aseprite file and its matching sprite sheet,
regenerate its manifest from the checkout root:

```sh
uv run --locked python -m deskpet.tools.aseprite_to_manifest \
  assets/Cat_85_Animations/Cat_Ginger.aseprite \
  assets/Cat_85_Animations/Cat_Ginger.png \
  --out assets/anim_manifest.yaml \
  --sheet-rel Cat_85_Animations/Cat_Ginger.png
```

Use the matching filenames and output path for the other colours. To run
one development decision with a configured model, use
`uv run --locked python -m deskpet.brain.agent --once`. The legacy log
identifies model and rule-based responses.
