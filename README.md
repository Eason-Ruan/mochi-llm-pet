# Carrot 🐱

Carrot is a desktop pet for focus sessions, work items, and reminders. It can
suggest a focus session, look for a suitable moment to remind you, or act on a
request in chat. It appears as **Mochi** on the desktop and in system settings.
The macOS bundle is `Mochi.app` (`io.github.mochi.deskpet`).

- [Install and launch](#quick-start-windows-and-macos)
- [Configure chat and Observation](#configure-system-2-and-observation)
- [Grant macOS permissions](#macos-permissions)
- [Verify a fresh install](#verify-a-fresh-install)
- [Troubleshoot or uninstall](#troubleshoot-or-uninstall-on-macos)

## Quick start (Windows and macOS)

### macOS

Use macOS 13 Ventura or later. All-display screenshot modes require macOS 14.2
Sonoma or later. Install [uv](https://docs.astral.sh/uv/getting-started/installation/)
and check that `xcrun --find clang` succeeds. If it fails, run
`xcode-select --install` and wait for installation to finish. An active Xcode
installation also supplies the compiler.

Clone to a directory you intend to keep. From an existing checkout, run the
last three commands from its root.

```sh
git clone https://github.com/Eason-Ruan/mochi-llm-pet.git
cd mochi-llm-pet
uv sync --python 3.12 --locked
uv run --locked python scripts/build_mochi_macos.py
open .mochi-native/Mochi.app
```

Open the same bundle or double-click `start_mochi.command` for future launches.
The builder signs the app locally and embeds absolute paths to the checkout
and Python environment. Rebuild after either path changes. Distributing a
standalone bundle requires separate packaging, signing, and notarization.

### Windows

From the checkout root, install the locked dependencies and start the app.

```powershell
uv sync --python 3.12 --locked
uv run --locked python -m deskpet
```

You can also double-click `start_mochi.bat` after installing dependencies.

## Use the desktop pet

Click the cat's toolbar to open reminders, focus, chat, or settings. Manual
timers and explicit reminder deadlines work offline. Under **专注与回顾 →
管理计时**, set the duration and click **开始专注**. Use **提醒事项** to create an
**到点提醒**. Configure System 2 for chat and Observation for screen
understanding. You can accept or dismiss suggestions in the cat's bubble.
Use **清除聊天记录** in chat to clear the conversation and its recent context.

A fresh Mac profile uses 15-minute focus sessions, 5-minute breaks, and
automatic focus suggestions. Reminders, history, preferences and chat live
in the local profile. The default Mac path is
`~/Library/Application Support/Mochi/Mochi`. Pass `--data-dir PATH` to use a
different profile. Right-click the cat and choose **退出 Mochi** to quit.

## Configure System 2 and Observation

Each role has its own **API Base URL**, **API Key**, and **model**. The
suggested Alibaba Cloud Bailian configuration uses the Beijing endpoint
with `qwen4-max` for System 2 and `qwen3.7-flash` for Observation. Use a key
and endpoint from the same region. [Bailian API keys](https://help.aliyun.com/zh/model-studio/get-api-key) ·
[Regional endpoints](https://help.aliyun.com/zh/model-studio/base-url)

- **Desktop.** Under **设置 → 模型与隐私 → 配置模型连接**, select the roles,
  enter their credentials, and click **保存并验证**. You can open the same
  dialog with `uv run --locked python -m deskpet --configure-models`.
- **Terminal.** Run the command below to enter keys at hidden prompts.
  Use `--role system2`, `--role observation`, or `--role both` to preselect roles.

```sh
uv run --locked python -m deskpet --configure-models --terminal
```

Pass the same `--data-dir` as your app if you use a custom profile. Keys go
to macOS Keychain or Windows Credential Manager. To restore a private backup,
enter the keys in the masked fields. Leaving a key blank at its existing
Base URL keeps the saved key.

**保存并验证** checks the selected models using synthetic inputs. Observation
cloud verification needs image-sharing consent. To verify before using live
screenshots, turn off **允许截取屏幕图像**, run the check, and look for
**连接已验证**. In Terminal, use `--share-images` after reviewing
[screenshot scope](#screen-recording). Use `--share-activity` separately
to share published activity summaries with System 2.

## macOS permissions

### Screen Recording

1. In **设置 → 模型与隐私**, review **截图范围** and enable
   **允许截取屏幕图像** when you want live screenshots.
2. Click **请求屏幕录制权限**. In **System Settings → Privacy & Security →
   Screen & System Audio Recording** (**Screen Recording** on older macOS),
   enable the current `Mochi.app`.
3. If it is absent, click **+**, press **Command–Shift–G**, and enter the full
   path to the checkout's `.mochi-native` folder. Select **Mochi.app**, quit
   and reopen the app, then look for **macOS 屏幕录制权限已授权** in **模型与隐私**.

Fresh Mac profiles enable the capture preference and select
**所有显示器原始画面（实验）** on supported versions.
This scope can send visible pixels from every active display, including
excluded apps and private browser windows, to the explicitly authorized and
verified Bailian `qwen3.7-flash` route. The model attempts to redact its output.
Choose **当前窗口** or **所有显示器（排除黑名单应用）** for a narrower scope.
Live cloud capture requires the app switch, OS permission, and verified
recipient consent. Use **暂停活动采集** to pause Observation.
[Apple's Screen Recording guide](https://support.apple.com/guide/mac-help/control-access-screen-system-audio-recording-mchld6aa7d23/mac)

Browser Automation is optional for **读取浏览器域名** and supported sites under
**编辑文档名权限**. Manage these grants in **Privacy & Security → Automation**.

### System notification setup

- **macOS.** Under **设置 → 桌宠与通知**, turn on **允许系统通知** and click
  **检查系统通知权限**. Accept the macOS prompt if shown. In **System Settings →
  Notifications → Mochi**, enable notifications and select a banner or alert
  style. macOS controls Lock Screen delivery and sound.
- **Windows 10 or 11.** From the checkout, run
  `uv run --locked python -m deskpet.c_ui.windows_notification_identity install`.
  Launch **Mochi** from Start and enable system notifications in the app.
  Rerun the installer if the checkout or environment moves. Use `remove` in
  place of `install` to remove the shortcut.

Native banners use generic reminder text. A desktop card may handle delivery
while the app is visible.

## Verify a fresh install

On macOS, quit the app and run these commands from the checkout root to
verify the signature and launch with an isolated profile.

```sh
codesign --verify --strict .mochi-native/Mochi.app
mochi_profile="$(mktemp -d "${TMPDIR:-/tmp}/mochi-profile.XXXXXX")"
open -n .mochi-native/Mochi.app --args --data-dir "$mochi_profile"
```

1. In **管理计时**, set a short duration, leave the field, and check the displayed
   value before clicking **开始专注**. Quit and run the same `open` command. An
   unfinished session can reopen as **已暂停** with its remaining time.
2. With notifications authorized, create and confirm an **到点提醒** a few
   minutes ahead. To check Lock Screen delivery, enable it in macOS
   Notifications and lock the Mac before the reminder is due.
3. To request Screen Recording from Terminal, quit and append
   `--request-screen-recording` to the same `open` command. macOS permissions
   are tied to the app identity in the current login. Use a login with no
   prior grants to see first-time prompts.

## Troubleshoot or uninstall on macOS

- **Launch or build fails.** Rerun `uv sync --python 3.12 --locked`. Quit and
  move the old `.mochi-native/Mochi.app` to Trash before rebuilding. If Qt
  reports `neon` or Launch Services reports `-10827` in an automation
  sandbox, retry from a regular Terminal on the logged-in Mac.
- **Screen Recording fails after a rebuild.** Quit, remove the stale entry
  in Screen Recording settings, add the current signed bundle, and reopen.
  Check the scope, verification and image-sharing consent with the Mac unlocked.
- **Uninstall.** Quit Mochi and move the checkout-local bundle to Trash. Back
  up data you want to retain before removing the profile. Remove the app's
  grants in System Settings and its saved credentials in Keychain Access.
  Model credential services begin with `Mochi Observation`.

## Further documentation

- [M0 workbench, offline replay, and acceptance](docs/m0.md)
- [Architecture decisions](docs/adr/README.md) and [public contracts](docs/contracts/README.md)
- [Observation capture and sharing](docs/adr/0009-observation-v2.md)
- [Jev shadow evaluation and replay](docs/evaluation/jev-shadow.md)
- [Original desktop pet, historical demo, and sprite development](docs/legacy-pet.md)

## Development

```sh
uv sync --python 3.12 --locked --extra dev
uv run --locked --extra dev pytest -q
```
