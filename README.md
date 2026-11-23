# Mochi 🐱 — an LLM-powered virtual desktop pet

https://github.com/user-attachments/assets/4e0e6054-b2d3-41e2-8e87-6cdb13ce9ff7

Mochi is a desktop cat for focus sessions, work items, and reminders. Start a
timer from the cat, or ask it to find a suitable moment for a reminder. The
desktop stores timers and explicit deadlines locally. Observation describes
recent activity. System 2 uses DeepSeek Harness (DSH) to interpret requests and
propose actions through the same services that power the buttons.

See the [desktop quick start](#quick-start-windows-and-macos) for setup. The
original sprite pet is also available through `python -m deskpet --legacy`.

---

## M0 collaboration workbench

The new work-assistant collaboration baseline has an independent, offline entry
point. Run `uv run python -m deskpet.m0 gui --profile scenario` for the real Qt
workbench, or `uv run python -m deskpet.m0 replay --suite m0` for deterministic
headless scenarios. Both use scripted decisions and an in-memory task service.
Start with [M0 setup and acceptance](docs/m0.md), [public contracts](docs/contracts/README.md),
[architecture decisions](docs/adr/README.md), and the [B/C/D handoffs](docs/handoff/README.md).
Windows agents can run [the acceptance script](scripts/smoke_windows.ps1).

## What the original LLM pet does

- **Sees your desktop** — a whole-screen screenshot **and** a distilled text scene
  go to the LLM each decision. It genuinely reacts to *what's on screen* (begs at
  cat food, hunts a bird in a video), recognises **any app by name** from its
  title (Photoshop, Discord, a game…), reads what you're doing (coding / watching
  / gaming / away), the time of day, and **what just changed** ("you just switched
  to Brave").
- **Moves with intent** — walks to your cursor, chases/pounces the "little arrow",
  perches on a window edge, naps on the taskbar, hides, follows you around.
- **Talks like a cat** — short, playful, self-centred lines (demands, judgement,
  drama) — never an assistant. Mostly silent, occasionally vocal.
- **Expresses itself** — picks from **27 expressive animations** (nod, sneak,
  knead, dig, dance, play-dead, tilt-head…) to match its mood.
- **Remembers** — learns which apps you live in, recalls being petted / poked /
  thrown, levels up. Energy / mood / bond survive restarts and feed its behaviour.
- **Reacts to touch** — pet it, poke it, grab-and-throw it (see below).
- **Obeys physics** — thrown, it falls, bounces, and lands on a **window top** or
  the taskbar.

The pet's scope is **body-only and safe**: it moves and animates *itself* and
*pretends* to interact. It never injects clicks or keystrokes into your apps.

---

## How it works (three clocks)

```
Sensors (ms, Win32)  ─►  WorldState  ─►  [trigger]  ─►  ONE multimodal LLM
                              ▲                               │ JSON intent
        Memory (SQLite) ──────┘                               ▼
                                          Body + Render (60fps) executes it
```

- **Sensors** read window rects / cursor / foreground app every few ms.
- The **brain** (an LLM) wakes on a heartbeat or a real change, looks at a
  screenshot + a distilled scene + recalled memories + its own recent actions,
  and emits ONE structured *intent*.
- The **body** executes that intent as smooth 60fps motion + animation. It
  re-resolves the target every frame, so the pet tracks a window even while you
  drag it.
- **Reflexes** (pet / poke / grab / throw) bypass the LLM for instant response.

The LLM runs on its own thread — the render loop never blocks on it.

### The LLM has exactly one job: control the pet

To keep a small local model reliable, the model's output is deliberately tiny —
it only decides *behaviour*. Everything mechanical (pixel math, memory,
persistence) is handled by code, not the model.

| field | meaning |
|-------|---------|
| `verb` | the action — `walk_to, follow_cursor, chase, sit_on, watch, nap, nudge, pounce, look_at, hide, idle, say, emotion` |
| `target` | *where*, by name — `cursor`, `active_window`, `window:Chrome`, `taskbar`, … (code resolves to pixels) |
| `edge` | for `sit_on`: which side of the window to perch on |
| `emotion` | its mood — `happy, curious, annoyed, sleepy, mischievous, …` |
| `emote` | optional expressive animation (one of 27), to match the moment |
| `say` | a short spoken line, or null (silence is normal) |
| `thought` | a private one-liner (logged, never shown) |

That's it — no "remember", no confidence, no coordinates. The model just *is* the
cat.

### What it's aware of (each decision)

| Input | What it gives the cat |
|-------|------------------------|
| **Screenshot** (whole desktop) | Real sight — reacts to food, prey, animals, anything visible. |
| **Window titles** | Recognises *any* app by name via the LLM's own knowledge (not a fixed list). Off-switch: `[perception].share_titles`. |
| **Activity** | Coarse read of what you're doing — coding / watching / gaming / browsing / away. |
| **Time & context** | Time of day, "glued to the same app for an hour", low energy — so it gets clingy late at night, bored in long sessions. |
| **"Just happened"** | The event that woke it — app switch, clipboard copy, you went idle — so it *reacts* rather than just describes. |
| **Memories** | Relevant facts + past episodes recalled by current app + recency. |
| **Its own recent actions** | The last few turns, so it doesn't repeat itself. |

Cheap structured signals (active app, idle, cursor) come from Win32 every few ms;
the screenshot is captured fresh per decision.

---

## Quick start (Windows and macOS)

```sh
uv sync --python 3.12 --locked
uv run python -m deskpet
```

After setup, double-click `start_mochi.bat` on Windows or
`start_mochi.command` on macOS. Click the cat's toolbar to open reminders,
focus, chat, or settings. The focus panel can start a timer with the supplied
duration and an optional title. Text chat also accepts local commands such as
「开始 25 分钟」 and 「暂停专注」.

System 2 defaults to Alibaba Cloud Model Studio (阿里云百炼) and `qwen4-max`.
Run `uv run --locked python -m deskpet --configure-system2` to open the local
key form. Enter a Beijing pay-as-you-go **百炼 API Key** and choose **保存并验证**.
Mochi saves the secret in the system credential store and tests the connection
with a synthetic tool call. The form uses the desktop's profile and also accepts
`--data-dir PATH`. A regional workspace can use its matching official endpoint.
[Model details](https://help.aliyun.com/zh/model-studio/qwen4-max) ·
[Regional endpoints](https://help.aliyun.com/zh/model-studio/base-url) ·
[Create a Bailian API key](https://help.aliyun.com/zh/model-studio/get-api-key).

From the cat, open **设置 → 模型与隐私 → 对话与动作 → 配置** to change the
connection and use **验证能力** to test it. Existing profiles can select
**阿里云百炼** there. Manual timers and explicit reminder deadlines also work
offline.

Configure the screen transcriber and Observation state model independently in
the same panel. Image sharing, filtered screen text, and published activity
summaries each have a separate control for their respective recipient.

Mochi stores its reminders, focus history, settings, chat, and draft in the
platform's local application data directory. Use `python -m deskpet --data-dir PATH`
to select another profile directory. Screen capture is off by default and can
be enabled in settings.

Run `python -m deskpet --legacy` to open the original LLM pet, or
`scripts/start_ui_refine.command` on macOS to open the scripted Refine demo.

### System notification setup

Run the quick-start dependency install first. Then choose the setup for your
platform and enable system notifications in Mochi's settings.

- **macOS 13 or later.** With the Xcode Command Line Tools installed, run
  `uv run --locked python scripts/build_mochi_macos.py`. Open
  `.mochi-native/Mochi.app` directly or run `start_mochi.command`. Grant the
  notification permission when macOS asks. Adjust banners and sounds for Mochi
  under System Settings → Notifications. The local ad-hoc signed app links to
  this checkout and its Python environment. If either moves or Python changes,
  remove the old bundle and rerun the builder. Distribution to other Macs requires packaging,
  Developer ID signing, and notarization.
- **Windows 10 or 11.** Run
  `uv run --locked python -m deskpet.c_ui.windows_notification_identity install`
  from the checkout. Open **Mochi** from Start. The installer creates a shortcut
  for the current user with Mochi's application ID. Run the command again if
  the checkout or environment moves, or use `remove` to delete the shortcut.
  Windows can show toasts while Mochi is running. It may take a moment after
  creating the shortcut for Windows to recognize the new identity. Adjust the
  app's notification permissions and sound in Windows Settings.

Notifications display generic text. macOS can register eligible hard deadlines
in advance once authorized. The Windows desktop build submits at the deadline
while running and checks overdue reminders when it resumes. The plain macOS
Python command still runs Mochi with its desktop channels.

### Running multiple cats 🐱🐱🐱

The project ships configs for **three cats** — one per sprite colour (Ginger,
Grey, Grey-White). Each cat has its own memory DB and personality so they
develop independently.

**Just double-click `start_mochis.bat`** — it launches all three as
detached background processes. They survive closing the terminal, and you can
use the same file after a reboot.

| Cat | Config | Sprite |
|-----|--------|--------|
| 🟠 Ginger | `config_ginger.toml` | `Cat_Ginger.png` |
| ⬛ Grey | `config_grey.toml` | `Cat_Grey.png` |
| 🐱 Grey-White | `config_grey_white.toml` | `Cat_Grey_White.png` |

To stop all three at once, double-click **`stop_mochis.bat`**.

Under the hood, each cat is launched via a `.vbs` script that sets the
`DESKPET_MANIFEST` env var (to pick the sprite colour) and the `--config`
flag (to pick the config file). You can also launch cats individually:

```bat
cscript //nologo start_ginger.vbs
cscript //nologo start_grey.vbs
cscript //nologo start_grey_white.vbs
```

---

## Choosing a brain (LLM provider)

All five providers are supported and switchable. Configure via `config.toml`
(copy `config.example.toml`) or environment variables (env always wins).

| Provider | Notes |
|----------|-------|
| **ollama** (default) | Local, free, **private** — screenshots never leave your machine. Model `gemma4:12b` (multimodal). |
| **gemini** | Cloud multimodal. Set `GEMINI_API_KEY`. |
| **openai** | gpt-4o / gpt-5-style. Set `OPENAI_API_KEY`. |
| **anthropic** | Claude. Set `ANTHROPIC_API_KEY`. |
| **openai_compat** | Any OpenAI-compatible endpoint (vLLM / LM Studio / proxy). Set `base_url`. |

Examples:

```bat
:: cloud (Gemini)
set DESKPET_LLM_PROVIDER=gemini
set GEMINI_API_KEY=...your key...
python run.py --legacy
```

### Using a remote Ollama (a GPU box on your network)

If Ollama runs on **another machine**, bind it to all interfaces on that box and
allow the port through its firewall:

```bash
OLLAMA_HOST=0.0.0.0:11434 ollama serve
```

then point the pet at it:

```bat
set DESKPET_LLM_PROVIDER=ollama
set DESKPET_LLM_MODEL=gemma4:12b
set DESKPET_LLM_BASE_URL=http://192.168.1.50:11434   :: <- the box's IP
python run.py --legacy
```

Only expose Ollama on a network you trust. If the pet can't reach the brain, it
prints a friendly note and runs rule-based until the brain is back.

---

## Interacting with it

Three mouse gestures over the cat (clicks elsewhere pass through to your apps as
normal):

- **Pet** — hover over the cat and move the cursor *back and forth* across it. It
  loves it: gets affectionate, kneads, sometimes purrs, and your **bond grows
  fastest**.
- **Poke** — a quick click (tap, no drag). Annoys it — it flinches and grumbles.
- **Grab & throw** — click-drag the cat, then release to fling. It falls, bounces,
  and lands on a **window top** (if it's over one) or the taskbar. Window tops are
  one-way platforms: it lands on them but passes through their sides.

The cat remembers being petted / poked / thrown, and these touches nudge its mood
and bond.

---

## Animations — the whole pack, LLM-driven

Mochi uses the **Bow.Pixel "Cat 85+"** sprite pack — **all ~92 animations** are
wired up, via four routes:

- **Verbs → motion** (walk, run, sit, sleep, pounce, nudge, fall…)
- **Emotions → resting pose** (content, grumpy, sleepy, curious…)
- **27 LLM-pickable emotes** → the expressive/dramatic ones (nod, shake-head,
  tilt-head, scratch, dig, eat, knead, dance, sneak, hiss, flinch, play-dead,
  climb, …)
- **Random variant pools** → idle/walk/run/sit rotate through their variants so it
  never looks looped or identical between runs.

### Adding the real art

The placeholder cat is intentionally simple. To use the real pack:

1. Download **Bow.Pixel "Cat 85+"** (free / pay-what-you-want):
   https://bowpixel.itch.io/meow-cat-85-animation
2. Unzip into `assets/` (it ships `.aseprite` + matching `.png` sheets, 3 colours:
   `Cat_Ginger`, `Cat_Grey`, `Cat_Grey_White`).
3. Auto-generate the manifest from the aseprite tags (maps every animation to its
   exact frames in the sheet, handling the pack's row-aligned export):
   ```bash
   python -m deskpet.tools.aseprite_to_manifest \
     assets/Cat_85_Animations/Cat_Ginger.aseprite \
     assets/Cat_85_Animations/Cat_Ginger.png \
     --out assets/anim_manifest.yaml \
     --sheet-rel Cat_85_Animations/Cat_Ginger.png
   ```
   (Swap `Cat_Ginger` → `Cat_Grey` / `Cat_Grey_White` for a different colour.)
4. Relaunch — it auto-detects the art and replaces the placeholder.

---

## Memory & personality (code-driven, persistent)

State lives in `deskpet.db` (SQLite). The **LLM never decides what to remember** —
code does, so the model stays focused on behaviour. Two stores:

- **`memories`** — two kinds, written automatically:
  - **Facts** about you — which apps you live in, *what* you do (watches a lot of
    videos, codes a lot), being petted / poked / thrown, level-ups. Deduped so the
    same fact doesn't pile up across sessions.
  - **Episodes** — the cat's own recollections of notable moments (what it saw +
    did + felt, e.g. *"those kibble pictures on screen are torture; I must be
    fed"*), captured on emotional turns and throttled so they don't flood.

  Both are retrieved by relevance (current app + recency) back into the prompt, so
  the cat acts on what it knows *and* can call back to what it's lived through
  ("you teased me with food earlier — I haven't forgotten").
- **`pet_state`** — energy, mood, bond, level, xp. Energy drains awake and regens
  while napping; mood eases to neutral; bond/xp climb with positive interaction.
  Saved periodically (survives a hard kill) and reloaded on start — so the
  personality drifts and persists across sessions.

---

## Watching it think (logs)

Run with `INFO` (default) for a readable per-decision block:

```
┌─ wake [heartbeat:changed]  via LLM
│  sees: app=Code.exe[code]  cursor=(500,400) NEAR-pet  idle=8s  energy=0.84(ok) mood=content
│  does: chase → cursor   feeling mischievous  emote=pounce
│  💬 "Tiny arrow, prepare for ambush."
│  🧠 the cursor skitters; ideal practice for my pounce
└─
```

`via LLM` vs `via instinct (rule-based)` tells you whether the brain is actually
connected. Run with **DEBUG** to also see the exact scene sent, the raw LLM JSON,
the screenshot size, and recalled memories:

```bat
:: PowerShell
$env:DESKPET_LOG_LEVEL="DEBUG"; python .\run.py --legacy
:: cmd
set DESKPET_LOG_LEVEL=DEBUG
python run.py --legacy
```

---

## Configuration

See `config.example.toml` for every option. Highlights:

- `[vision]` — `enabled`, `mode` (`monitor` = whole desktop, the default; or
  `active_window`), `max_edge` (downscale for token cost).
- `[llm]` — provider / model / base_url / api_key / temperature.
- `[perception].share_titles` — send raw window titles to the LLM (default on; see
  Privacy).
- `[memory]`, `[triggers]`, `[persona]`, `[render]`.

Key env overrides (env always wins):

```
DESKPET_LLM_PROVIDER  DESKPET_LLM_MODEL  DESKPET_LLM_BASE_URL  DESKPET_LLM_API_KEY
DESKPET_CONFIG (path to config.toml)     DESKPET_LOG_LEVEL (DEBUG|INFO|...)
```

---

## Desktop privacy

The production pet starts with screen capture off. Its settings distinguish
three recipients. The vision model can receive an eligible window image. The
Observation state model can receive structured signals and, with separate
permission, filtered screen text. System 2 receives current and compressed
historical state. Sharing published activity summaries with a cloud System 2
model is a separate choice. User messages and relevant user-entered items are
included when needed for a requested action. Credentials are referenced through
the local credential store. [Observation design](docs/adr/0009-observation-v2.md)

### Privacy in the original sprite pet

With the default local Ollama, **the screenshot and window titles never leave your
machine**. The app never writes screenshots to disk, stores only a *hash* of the
clipboard (to detect change, never the content), and the model's private `thought`
is shown only in *your* local logs.

Cloud providers necessarily receive the screenshot (your whole desktop) — and the
window titles, which add nothing beyond what the screenshot already shows. So
**real privacy = local Ollama**, where everything stays on-device. If you must use
a cloud provider but want to send less, set `[perception].share_titles = false`
(only meaningful in text-only mode, since the screenshot otherwise dominates).

---

## Development

The brain, memory, parsing, triggers, sprite, body, and physics logic are
cross-platform and unit-tested (they run on Linux/Mac); only the perception and
window layers require Windows.

```bash
pip install -r requirements.txt
pytest -q                                   # logic tests
python -m deskpet.brain.agent --once        # one brain decision (needs an LLM)
```

The pet's behaviour scope is **body-only** — it moves and animates *itself* and
*pretends* to interact. It never injects clicks/keystrokes into your other apps.
