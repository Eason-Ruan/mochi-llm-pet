# Carrot 🐱 — an LLM-powered virtual desktop pet

https://github.com/user-attachments/assets/4e0e6054-b2d3-41e2-8e87-6cdb13ce9ff7

Carrot is a desktop cat for focus sessions, work items, and reminders. Start a
timer from the cat, or ask it to find a suitable moment for a reminder. The
desktop stores timers and explicit deadlines locally. Observation describes
recent activity. System 2 uses DeepSeek Harness (DSH) to interpret requests and
propose actions through the same services that power the buttons.

The desktop app appears as **Mochi**, including in system permission screens.
The macOS bundle is `Mochi.app` (`io.github.mochi.deskpet`), and the repository
slug is `mochi-llm-pet`.

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

### Install on macOS from a checkout

Mochi.app requires macOS 13 Ventura or later. The two all-display screenshot
modes require macOS 14.2 Sonoma or later. Install [uv](https://docs.astral.sh/uv/getting-started/installation/)
(`brew install uv` if you use Homebrew). If `xcrun --find clang` fails, run
`xcode-select --install` and wait for the installer to finish. An active Xcode
installation already supplies the compiler. [Apple's Command Line Tools guide](https://developer.apple.com/documentation/xcode/installing-the-command-line-tools/)

In Terminal, choose a permanent location for the checkout and run these
commands. If you already have a checkout, start with `cd` into its root and run
the last three commands.

```sh
git clone https://github.com/Eason-Ruan/mochi-llm-pet.git
cd mochi-llm-pet
uv sync --python 3.12 --locked
uv run --locked python scripts/build_mochi_macos.py
open .mochi-native/Mochi.app
```

For future launches, open the same `Mochi.app` or double-click
`start_mochi.command`. The builder ad-hoc signs the app for this Mac and embeds
absolute paths to the checkout and its Python environment. Keep them in place.
Sharing a standalone app with another Mac requires separate packaging, signing
and notarization.

Click the cat's toolbar to open reminders, focus, chat, or settings. Manual
timers and explicit reminder deadlines work offline. To verify the build with
a fresh profile, quit any running copy using the cat's **退出 Mochi** menu,
then run:

```sh
codesign --verify --strict .mochi-native/Mochi.app
mochi_profile="$(mktemp -d "${TMPDIR:-/tmp}/mochi-profile.XXXXXX")"
printf 'Mochi profile: %s\n' "$mochi_profile"
open -n .mochi-native/Mochi.app --args --data-dir "$mochi_profile"
```

Click the focus icon to open **专注与回顾**, then choose **管理计时**. Set a short
duration, move focus away from the field, and check the displayed value before
choosing **开始专注**. Right-click the cat and choose **退出 Mochi**, then run the
same `open` command with the same `--data-dir`. An unfinished session can
reopen as **已暂停** with its title and remaining time. For a notification check,
follow the Notifications step below, then create and confirm an **到点提醒**
reminder a few minutes ahead. An eligible reminder can
show **已登记系统通知**. A visible desktop card may handle delivery while the
desktop is active. For a Lock Screen check, enable Lock Screen delivery for
Mochi in macOS Notifications and lock the Mac before the reminder is due. The
isolated directory contains its own settings and history. macOS manages
permissions for the app identity in the current login. To test first-time
permission prompts, use a macOS login with no prior grants for this app.

### macOS permissions

- **Notifications.** Open **设置 → 桌宠与通知**, turn on **允许系统通知**, click
  **检查系统通知权限**, and accept the macOS prompt. In **System Settings →
  Notifications → Mochi**, enable notifications and select a banner or alert
  style. macOS also controls sound, previews and Lock Screen delivery.
  The app's native alerts use generic reminder text, including when its
  **在锁屏显示通知详情** preference is on. [Apple's notification settings guide](https://support.apple.com/en-ca/guide/mac-help/-mh40583/mac)
- **Screenshots.** First review **截图范围** under **设置 → 模型与隐私**.
  **所有显示器原始画面（实验）** can send visible pixels from every active display
  to verified Alibaba Cloud Bailian `qwen3.7-flash`. This includes background
  windows, excluded apps or domains, and private browser windows. The model
  attempts to redact its output after receiving the image. Choose **当前窗口**
  or **所有显示器（排除黑名单应用）** for a narrower scope, or turn off
  **允许截取屏幕图像**. Live cloud screenshots require both the app's capture switch
  with an explicitly authorized and verified Observation recipient and the
  macOS Screen Recording grant.
- **Grant Screen Recording to the app you launched.** In Mochi's **模型与隐私**,
  click **请求屏幕录制权限**. In **System Settings → Privacy & Security →
  Screen & System Audio Recording** (called **Screen Recording** on older
  releases), enable this checkout's `.mochi-native/Mochi.app`. If the app is
  absent, use **+**, press **Command–Shift–G**, and enter the full path to the
  bundle's `.mochi-native` parent folder. Select **Mochi.app**, quit, and reopen
  Mochi with the same profile. Look for **macOS 屏幕录制权限已授权** in
  **模型与隐私**. To request the same permission from Terminal for the isolated
  profile above, quit Mochi and run:

  ```sh
  open -n .mochi-native/Mochi.app --args --data-dir "$mochi_profile" --request-screen-recording
  ```

  [Apple's screen recording guide](https://support.apple.com/guide/mac-help/control-access-screen-system-audio-recording-mchld6aa7d23/mac)
- **Browser Automation is optional.** The app can request access to a supported
  browser after you enable **读取浏览器域名** or opt into a supported website
  under **编辑文档名权限**. Accept only the browser you intend to use and
  manage it in **System Settings → Privacy & Security → Automation**. Leave
  these options off for an offline setup. [Apple's Automation guide](https://support.apple.com/en-au/guide/mac-help/mchl07817563/mac)

### Start on Windows

```sh
uv sync --python 3.12 --locked
uv run --locked python -m deskpet
```

After setup you can double-click `start_mochi.bat`. The focus panel accepts a
duration and an optional title. Configure and verify System 2 to use text
chat on either platform.

### Configure System 2 and Observation

Each role has its own **API Base URL**, **API Key**, and **model**. You can
configure one role or both, and use different OpenAI-compatible services for
each. New profiles suggest the Beijing Bailian endpoint and `qwen4-max` for
System 2 or `qwen3.7-flash` for Observation. All three fields are editable.
If you use Bailian, choose a pay-as-you-go key and the matching official endpoint.
[Create a Bailian API key](https://help.aliyun.com/zh/model-studio/get-api-key) ·
[Regional endpoints](https://help.aliyun.com/zh/model-studio/base-url).

- **GUI.** In Mochi, open **设置 → 模型与隐私 → 配置模型连接**. Select the
  roles to save, fill in their fields, and choose **保存并验证**. You can also
  open the same dialog from the checkout before launching the app:

  ```sh
  uv run --locked python -m deskpet --configure-models
  ```

- **Terminal.** Choose System 2, Observation, or both. The command prompts for
  each selected role's fields and masks the key input:

  ```sh
  uv run --locked python -m deskpet --configure-models --terminal
  ```

  Use `--role system2`, `--role observation`, or `--role both` to select the
  roles in the command. Provide URLs and model IDs with `--system2-base-url`,
  `--system2-model`, `--observation-base-url`, and `--observation-model`.
  Keys are entered interactively. Add `--share-activity` to authorize sharing
  published activity summaries with System 2. Review the screenshot scope in
  [macOS permissions](#macos-permissions) before adding `--share-images`,
  which authorizes the Observation recipient to receive eligible screenshots.

Both methods read and write the same `business.sqlite3` configuration file
in the local profile. It stores API Base URLs, model IDs, and references
to secrets in macOS Keychain or Windows Credential Manager. On macOS, if a
Keychain prompt appears while saving or verifying a model, allow access for the
Mochi or Python process you just started. Use the masked setup fields to
restore credentials from a private backup. The desktop model setup does not
load API keys from a `.env` file. If you launched the app with `--data-dir PATH`,
pass that same argument during setup. Leave a role's key blank to retain its
current credential at the same Base URL, including when changing only the
model. Enter a key for a new non-loopback URL. Advanced settings are available
in the individual model rows.

Activity-summary sharing with System 2 and screenshot sharing with Observation
are separate choices. Both are off by default when changing the URL, model,
or key unless you explicitly enable sharing for the connection. **保存并验证**
tests each role with synthetic inputs. The cloud vision check requires
screenshot sharing consent. To run that check separately from live capture,
turn off **允许截取屏幕图像** before saving and verifying, then look for
**连接已验证** for each role you selected. The **活动状态判断（旧版／对照）**
row is used for older two-model comparisons.

The desktop app stores reminders, focus history, settings, chat, and draft in the
platform's local application data directory. On macOS, the default directory
is `~/Library/Application Support/Mochi/Mochi`. A new Mac profile starts
with 15-minute focus, 5-minute breaks, automatic focus suggestions, and the
system notifications, Lock Screen detail and screen capture preferences
enabled. It selects the experimental raw all-display scope on supported Macs
and **当前窗口** on older macOS versions. Cloud image sharing still needs explicit
recipient consent and verification. When you reuse a profile, the app uses its
saved preferences.

Run `python -m deskpet --legacy` to open the original LLM pet, or
`scripts/start_ui_refine.command` on macOS to open the scripted Refine demo.

### Troubleshoot or uninstall on macOS

- **App fails to launch.** From the checkout, rerun
  `uv sync --python 3.12 --locked`. If the checkout or Python environment moved,
  Python changed, or you updated the launcher, quit Mochi and move the old
  `.mochi-native/Mochi.app` to Trash before rerunning the builder. If Qt reports
  `Incompatible processor` and `neon` or Launch Services reports `-10827` under
  a restricted automation sandbox, retry from a regular Terminal session in
  the logged-in Mac desktop. [Project environment notes](docs/m0.md)
- **macOS permission stopped working after a rebuild.** The local signature
  can change. Quit Mochi, remove the stale app entry from Screen Recording
  settings, add the current `.mochi-native/Mochi.app`, and reopen it.
  Recheck Notifications and any browser Automation grant you enabled.
  If live screenshots are missing, also check the capture switch, the selected
  scope, and the Observation recipient's consent and verification. Test with
  the desktop unlocked.
- **Remove Mochi.** Quit via **退出 Mochi**, then move the checkout-local
  app to Trash. You can also trash the checkout and its `.venv` when you
  no longer need the source. To erase reminders, history, chat and settings,
  back up your data and remove `~/Library/Application Support/Mochi/Mochi` or
  the profile folder you passed to `--data-dir`. Remove Mochi's grants from
  the macOS settings above if you enabled them. For credential cleanup, open
  **Keychain Access**, find items whose service begins with `Mochi Observation`
  and remove the entries for the profile you are discarding. Delete the
  optional `Mochi Jev` item only if you used it and no longer need it.

### Jev shadow evaluation

Set `MOCHI_JEV_SHADOW=1` when launching the app to send eligible background
ticks to TypeSafe. Supply a TypeSafe key through `JEV_API_KEY`. On macOS,
the app can also read service `Mochi Jev`, account `default`, from Keychain.
Enable `MOCHI_JEV_SHARE_TEXT=1` to also share the allowed, sanitized activity
descriptions. The shadow result is recorded locally and System 2 still handles
each scheduled turn. The default model is `jev-1.14.0`.

Run `uv run --locked python -m evaluation.jev_shadow_replay --jev fixture --output report.json`
for the synthetic replay with an offline stand-in. Use `--jev real` with
shadow mode and a configured key to collect live Jev measurements. The optional
`--system2-settings PATH` reads a verified Bailian route from a settings
database and measures System 2 on those synthetic snapshots.

### System notification setup

For macOS, follow the Notifications step under
[macOS permissions](#macos-permissions).

On Windows 10 or 11, install the dependencies above, then run
`uv run --locked python -m deskpet.c_ui.windows_notification_identity install`
from the checkout. Open **Mochi** from Start and enable system notifications
in Mochi's settings. The installer creates a shortcut for the current user
with Mochi's application ID. Run the command again if the checkout or
environment moves, or use `remove` to delete the shortcut. It may take a
moment for Windows to recognize the new identity. Adjust notification
permissions and sound in Windows Settings.

System notifications display generic text. The Windows desktop build submits
at the deadline while running and checks overdue reminders when it resumes.

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

Carrot uses the **Bow.Pixel "Cat 85+"** sprite pack — **all ~92 animations** are
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

The production pet can collect local activity signals. Use **暂停活动采集**
to pause Observation. For cloud vision, authorize each recipient explicitly
and review the capture scope before granting macOS Screen Recording. The
**当前窗口** scope can send an eligible foreground window image.

On macOS 14.2 or later, **所有显示器（排除黑名单应用）** combines eligible
content across displays into one image per visual request. It omits excluded
apps, windows matching built-in sensitive title rules, and recognized private
windows. With no domain exclusions, browser windows of unknown private
status can be included. A configured domain exclusion requires enough
information to rule it out before including a browser window. The experimental
raw scope can send visible pixels across every active display, including
windows that would be omitted in filtered scopes. See
[macOS permissions](#macos-permissions) for the raw scope's recipient and
authorization requirements.

The current vision model can interpret the screenshot and classify activity in
one call. Recent system signals are a separate opt-in for that recipient.
System 2 can receive current and compressed historical state. Sharing published
activity summaries with a cloud System 2 model requires its own consent. User
messages and relevant user-entered items are included when needed for a requested
action. Credentials are referenced through the local credential store.
[Observation design](docs/adr/0009-observation-v2.md)

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
