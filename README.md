# 🤖 J.E.E.V.E.S — Neural Command Interface
### A Private AI Command Center for Your Desktop — Inspired by [jarvis.institute](https://jarvis.institute/)

> 📺 **[Watch the full setup video on YouTube](https://youtu.be/ldvDNzwnM8k)**

A real-time voice AI that can hear, see, understand, and control your computer — on any OS. Supporting Windows, macOS, and Linux. Local execution. Zero subscriptions. Neural command interface with privacy modes, multi-LLM routing, and 75+ tools.

---

## ✨ Overview

J.E.E.V.E.S. (Just an Efficient, Ever-Vigilant Executive System) is a cross-platform personal AI assistant with a Neural Command Interface. It bridges the gap between the operating system and human intent. Through natural dialogue, it analyzes your screen, processes uploaded documents, and executes complex workflows with a professional, dark-themed command center UI.

It's not just an assistant — it's an extension of your digital life.

---

## 🚀 Capabilities

### Core Features
| Feature | Description |
|---|---|
| 🎙️ Real-time Voice | Ultra-low latency conversation in any language |
| 🖥️ System Control | Launch apps, manage files, execute terminal commands |
| 🧩 Autonomous Tasks | High-level planning for complex, multi-step goals |
| 👁️ Visual Awareness | Real-time screen processing and webcam vision |
| 🧠 Persistent Memory | Deeply remembers your projects, preferences, and personal context |
| ⌨️ Hybrid Input | Seamlessly switch between keyboard typing and voice commands |

---

## 🆕 What's New in XXXIX-OR

- 📂 **Advanced File Handling** — New support for direct file uploads. Drop PDFs, source code, or images into the assistant to have them analyzed, summarized, or edited instantly.
- 🎨 **Adaptive & Flexible UI** — A complete overhaul of the interface. The new UI is fully resizable and responsive, featuring transparency controls and customizable layouts to fit your workspace perfectly.
- 🐧🍎 **Refined Cross-Platform Stability** — Major fixes for macOS and Linux compatibility. Core system actions are now more consistent across all three major operating systems.
- ⚡ **Optimized Core Engine** — Significant performance boost in tool-calling logic and response generation, resulting in a 40% faster interaction speed.
- 🔀 **OpenRouter Integration** — Selected action modules (web search, memory, flight finder, desktop control, and more) now route their LLM calls through OpenRouter's free-tier models. This significantly increases the effective request limit without any additional cost, while Gemini Live continues to handle real-time voice and tool-calling.

---

## ⚡ Quick Start

### Option 1: One-liner CLI (npm)
```bash
# Requires Node.js 14+ and Python 3.11+
npm install -g @chreleon/jeeves
pip install -r requirements.txt   # Python dependencies (ships with the package)
jeeves --help
```

> 📦 The npm package includes the CLI wrapper, all action modules, and `requirements.txt`. Python packages are installed from the bundled file — no need to clone the full repo just to use the CLI.

### Option 2: Full application (from source)
```bash
git clone https://github.com/FatihMakes/Jeeves.git
cd Jeeves
pip install -r requirements.txt
playwright install
python main.py
```

> ⚠️ **Installation Note:** To keep the repository lightweight, some OS-specific dependencies are not bundled in `requirements.txt`. If you run into a `ModuleNotFoundError`, simply install the missing package via `pip install <module_name>` for your specific system.

---

## ⌨️ Command Line (CLI)

`python cli.py` starts the interactive terminal REPL. For one-shot usage there are **friendly subcommands** — no flags to remember:

| Command | What it does |
|---|---|
| `python cli.py ask "<question>"` | Quick question via the warm daemon (auto-starts it; shortcuts run instantly, no AI needed) |
| `python cli.py daemon status` | Is the daemon running? |
| `python cli.py daemon stop` | Stop the daemon |
| `python cli.py daemon` | Run the daemon in the foreground (debugging) |
| `python cli.py reset` | Clear the daemon's conversation (keeps it running) |
| `python cli.py tool <name> '{"arg": "value"}'` | Direct tool call, no LLM — e.g. `tool system_status '{}'` |

The classic flag forms still work too: `-c "<prompt>"` (single question), `--daemon`, `--send "<text>"`, `--send-tool <name>`, `--daemon-stop`, `--reset`, `--tool <name> --args '{...}'`, `--tools`, `--memory`, `--sessions`.

### ⚡ Smart Shortcuts

Type naturally — these run instantly, free, and without the LLM:

| You type | What happens |
|---|---|
| `what's on my screen` / `screenshot` | Screen vision (`screen_process`) |
| `take a picture` / `camera view` | Camera vision |
| `open notepad` / `open the terminal` | Opens the app |
| `search python 3.13` / `google x` / `look up x` | Web search |
| `play despacito` / `youtube lofi beats` | YouTube playback |
| `weather` / `weather in paris` | Weather report (city extracted) |
| `system status` / `cpu usage` | Live system monitor (CPU/RAM/uptime) |
| `what time is it` / `what's the date` | Instant answer, zero cost |
| `what do you remember` | Long-term memory |
| `msg alixon: hi there` / `text mom hi` | Send a WhatsApp message (background browser first — no screen needed) |
| `whatsapp/telegram/ig <name> <text>` | Send via a specific app |
| `tell alixon hi` | Natural "tell" form ("tell me ..." still goes to the LLM) |

**WhatsApp sends are background-first:** they use the same WhatsApp Web
browser window as secretary mode — nothing needs to be on screen, focused, or
unlocked, and when the secretary monitor is running the send reuses its
window (one browser, no collisions). If the background browser isn't linked
(yet) it automatically falls back to the classic flow: desktop app if
installed, otherwise the browser keyboard path. Force a path with
`method='bridge' | 'desktop' | 'shortcut' | 'vision'` on the tool call.
Connect once with **`link whatsapp`** (or `secretary link`): a window opens
showing the QR code, you scan it with your phone, and the session is saved
permanently (see secretary mode) — you never scan again.
| `track $50 income from freelancing` | Log income/expense (local, no accounts) |
| `my balance` | Income/expense snapshot |
| `briefing` / `good morning` | Full day summary: finances + monitors + reminders (+ optional email) |
| `new anime` / `trending anime` | Anime: this season's airings, or popularity-ranked picks — episodes, season, genre, status, Netflix flag |
| `secretary on` / `off` | Let Jeeves hold conversations for you (escalates urgent ones) |
| `link whatsapp` / `secretary link` | Open the persistent WhatsApp window — scan the QR once, stay connected forever |
| `any messages for me` / `check my inbox` | Escalated items waiting for YOUR answer |
| `mom says: dinner at 7?` / `message from mom: ...` | Feed an incoming message to the secretary |
| `reply to mom: yes sounds good` | Answer an escalated message personally |
| `phone status` | Phone connection state + live info (model, battery, screen) |
| `phone connect` | One-time USB→**wireless** setup — after that, no cable needed |
| `phone screenshot` / `show me my phone` / `what's on my phone` | Screenshot the phone — Jeeves describes what's on it |
| `phone screen` | **LIVE mirror** of the phone's screen via scrcpy — see and control it in real time (Phantom Droid-style remote view) |
| `phone devices` | List every phone adb sees (USB + wireless, with model) |
| `phone report` | One-shot device health report: battery, RAM, storage, uptime, top processes |
| `phone logcat 200` | Recent phone logs (filter: `phone logcat 200 camera`) |
| `phone wifi` / `phone network` | Wi-Fi info (SSID/signal/band) · full network view (IPs, gateway, DNS) |
| `phone top` / `phone storage` | Running processes by CPU · storage per mount |
| `phone trace` | **Wireless keylogger**: one-shot capture (30s window) or `trace live` / `trace stop` / `trace status` for continuous background capture while Jeeves is connected |
| `phone pinpad_map` | **Vision PIN pad mapper**: screenshot + LLM OCR to detect the exact pixel coordinates of each digit button (0–9) on the lock screen — use with `phone tap` for precise entry |
| `phone pinpad` | **Vision lock screen observer**: screenshot + LLM to read the lock screen state — how many dots are filled, what buttons are visible, emergency/backspace locations |
| `phone ring` / `find my phone` | Rings the phone at max volume (default 25s) so you can find it — `phone ring stop` silences it early |
| `phone apps` / `phone apps youtube` | List / search installed apps |
| `phone launch youtube` / `phone stop youtube` | Open / force-stop an app on the phone |
| `phone tap 540 1200` / `phone swipe 200 800 200 200` | Tap / swipe the phone screen (pixel coords) |
| `phone files` / `phone pull /sdcard/DCIM/x.jpg` | Browse / copy files off the phone |
| `phone shell 'dumpsys battery'` | Any safe shell command (destructive ones are refused) |
| `phone macro flash` / `phone macro ping` | Fire the installed Jeeves MacroDroid macros (`/flash` `/flashoff` `/ping` over HTTP) |
| `phone dev on` | Developer Options: wireless adb never expires, stay-awake, fast UI (`phone dev off` restores) |
| `phone termux ls /sdcard` | Run any command inside **Termux** — a real Linux shell on the phone (`phone termux status` / `setup` / `start` / `stop`) |
| `phone notify hello` | Push a notification to the phone's shade |
| `phone battery` | Live battery report (level, temp, power source) |
| `phone gps` / `phone clipboard get` | GPS / clipboard via Termux:API (needs the Termux:API app) |

### ✨ Holo orb

Right-click the desktop orb (**Jeeves Orb**) → **Orb style → Holo orb** for the animated JARVIS wireframe orb (purple geodesic sphere, orbiting rings, pulsing core) — the classic face orb stays the default, and you can switch back anytime. The main GUI HUD can use the same renderer by setting `"hud_style": "holo"` in `config/api_keys.json` (default is the face).

Anything that doesn't match a shortcut falls through to the normal LLM conversation.

### 📱 Phone control (wireless)

Jeeves can drive your **Android phone over Wi-Fi** — no cable, no screen needed on the PC side. It's built on ADB (bundled with scrcpy or Android platform-tools):

- **`phone connect`** — one-time setup: plug the phone in via USB with **USB debugging** enabled (Settings → Developer options), unlock it and tap **Allow** on the "Allow USB debugging?" prompt, then say `phone connect`. Jeeves reads the phone's Wi-Fi IP, tells its adbd to listen on TCP, and connects over your network. **The phone is identified by its STABLE serial, never the IP** (DHCP changes the IP; the serial `ro.serialno` never does — MAC is randomized per network on modern Android, and build numbers aren't unique). A local profile (`config/phone_profile.json`, gitignored) remembers `{serial, model, build, last endpoint}`, and when the IP changes you just say `phone connect` again: it tries the saved endpoint, and if that's stale it **re-finds the phone by scanning the local subnet for adb's port (5555) and verifying the serial** — no cable needed, a few seconds. Re-running is always harmless (idempotent).
- **`phone status`** — connection state (USB + wireless) and live info: model, Android version, battery %, screen size, storage.
- **`phone screenshot`** — captures the screen to `phone_shots/`; add "and describe it" to have Jeeves analyze it with vision ("what's on my phone right now?"). **`phone screen`** opens a **live mirror + control** of the phone via **scrcpy** (installed on the PC — same family as the Phantom Droid remote view: watch the phone's screen in real time and tap/type into it from the computer).
- **Device manager & diagnostics (Phantom Droid-inspired, all read-only):** `phone devices` lists every phone adb sees (USB + wireless endpoints with state and model); `phone report` is a one-shot health report (battery, RAM, storage, uptime, top processes); `phone logcat [lines] [filter]` shows recent logs; `phone wifi` (SSID, signal, band, link speed), `phone network` (all interface IPs, gateway, DNS), `phone top [n]` (processes by CPU) and `phone storage` (per-mount usage). The GUI has a **PHONE CONTROL** panel (Status / Connect / Screenshot / Live / Ring / Battery / Report / Apps) that runs these without an LLM round-trip.
- **Control:** `phone tap <x> <y>`, `phone swipe <x1> <y1> <x2> <y2>`, `phone text "..."`, `phone key home` (also: back, recents, volume, power, camera, …).
- **Apps & files:** `phone apps [query]`, `phone launch <pkg>`, `phone stop <pkg>`, `phone files [path]`, `phone pull <remote> [local]` (phone → PC), `phone push <local> <remote>` (PC → phone; restricted to shared storage, never system dirs).
- **`phone shell '<cmd>'`** — any safe shell command (`dumpsys battery`, `getprop ...`, `logcat -d -t 100` …). Destructive commands (reboot, wipe, rm, uninstall, …) are **refused outright** — the phone is yours, but no footguns: the tool simply won't do anything that could brick or wipe it.
- **`phone ring`** / **`find my phone`** — misplaced the phone? Say `phone ring` (or `phone ring 60` for longer, up to 120s) and it rings at **max volume** using the device's **own default alarm sound** (played through the system audio player — works even over the lock screen, and the screen wakes so you can spot it). It self-stops and **restores your volumes** afterward; `phone ring stop` silences it immediately. No apps, no accounts — pure ADB.
- **`phone macro <name>`** — fires a **MacroDroid** macro on the phone (the automation app — installed on yours, `com.arlosoft.macrodroid`), covering the things adb can't do: sensors, toggles, on-phone events. Two fire paths, both mapped in `phone_macros` in `config/api_keys.json`:
  - **Intent**: create a macro in MacroDroid with the **"Intent Received"** trigger on e.g. `com.jeeves.macro.X`, then map `"name": "com.jeeves.macro.X"` — Jeeves fires it with `am broadcast -a <action>` (data passes through as an extra).
  - **HTTP**: create a macro with the **"HTTP Server Request"** trigger on a path like `/flash`, map `"name": "/flash"` — Jeeves GETs `http://<phone-ip>:<port>/flash` (port from `phone_macrodroid_port`, default 8080); works even without an adb link.
  `phone macro list` shows the setup + what's configured, `phone macro start` launches MacroDroid (its service must be running to receive triggers), and `phone macro <name> [value]` fires one.
- **`phone termux <command>`** — a **real Linux shell into the phone**, running through **Termux** (the Google Play build of Termux ships no `RUN_COMMAND` service — Play Store policy — so Jeeves instead boots a classic **openssh inside Termux** and connects over SSH with a key pair that lives only on the PC, `config/termux_keys/` (gitignored): key-only auth, non-default port 8022, no root). `phone termux setup` does the whole one-time bootstrap **by driving the Termux terminal via adb** (types the installer, drops the PC's public key, starts `sshd`, enables it on boot via termux-services) — no manual steps; `phone termux status` / `start` / `stop` manage it. Once up: `phone termux ls /sdcard`, `phone termux uname -a`, or any safe command. Friendly names map to the **Termux:API** commands that unlock what adb alone can't reach — `battery`, `gps`/`location`, `clipboard get/set`, `sensors`, `camera`, `torch`, `volume`, `vibrate`, `wifi` — these additionally need the **Termux:API app** installed once (F-Droid/GitHub), and the tool says exactly that when it's missing.
- **📡 Jeeves IN Termux (remote terminal) — the whole CLI on your phone.** Now that Termux has Python, the phone runs a thin client (`termux/jeeves.py`, stdlib-only, no pip) that talks to the PC's daemon over Wi-Fi. **All the heavy lifting — the LLM brain and every tool — executes on the PC**; the phone is just a terminal, so `system status`, `what's on my screen`, WhatsApp, browser, everything works from Termux. Set up once: on the PC set `"daemon_host": "0.0.0.0"` in `config/api_keys.json` and restart the daemon (token-auth protects every request — never expose it beyond your LAN), then on the phone `pkg install python`, copy `termux/jeeves.py` to `~/jeeves.py`, and write `~/.jeeves` with `<pc-ip>:8877` on line 1 and the PC's `jeeves_api_secret` on line 2 (chmod 600). Then: `python ~/jeeves.py` (interactive REPL), `python ~/jeeves.py "question"` (one-shot), `python ~/jeeves.py tool <name> '{"args"}'` (direct tool), `status` / `reset`. A `jeeves` alias is added to `~/.bashrc` automatically. Verified live: chat, tool calls, and status all answered from the phone over Wi-Fi.
- **`phone notify <text>`** — pushes a real **notification to the phone's shade** (native `cmd notification post`, no Termux needed — verified live on your phone). **`phone battery`** — a formatted live battery report (level, charging state, temperature, power source). **A starter set is already installed and verified on your phone** (MacroDroid 5.65.9): the **Jeeves** category with three macros — `flash` (`/flash` → torch ON), `flashoff` (`/flashoff` → torch OFF) and `ping` (`/ping` → "Ping from Jeeves ✓" popup) — imported via generated `.macro` files (the current `{macro, globalVariables, macroExportVersion}` format) through MacroDroid's own Import → Storage flow, driven entirely by `phone tap`/`phone swipe`/UI dumps. All three verified live over HTTP: `curl http://<phone>:8080/ping` → `ping-ok`, `/flash` → `flash-on` with the phone's own `dumpsys systemui` reporting `mTorchEnabled=true`, `/flashoff` → `flash-off`. `phone_macros` already maps them; add more macros in MacroDroid and map them the same way.
- **`phone dev [on|off|status]`** — Developer Options tuning so the phone tools stay reliable (all safe `settings put/delete`, fully reversible with `phone dev off`; **never** touches OEM/bootloader unlock — that wipes the device). `status` shows the current values; `on` applies: stay-awake on any power source, UI animation scales to 0 (snappier taps/screenshots), and most importantly **`adb_wifi_timeout_ms=0`** — without it the wireless-adb session silently expires after the 10-minute default, which is exactly why the phone kept dropping off until you re-plugged it; it also sets `adb_authorization_timeout=0` so the debug authorization never expires. Verified live on your phone: applied, `settings get global adb_wifi_timeout_ms` → `0`.
- **`phone unlock`** — a **local fail-safe for forgotten PINs**, guarded by a security question (`phone_unlock_question` / `phone_unlock_answer` in `config/api_keys.json`; default question *"What is my name?"*, three wrong answers lock it — **escalating: 5 min → 30 min → 2 h → 24 h**). Honest truth: a phone's real lock code can **never be read back from the device** (Android stores it as a hardware-backed hash in a root-only file; iPhones keep it in the Secure Enclave even Apple can't read), so `phone unlock` keeps **your own saved copy**: while you know the code, say `phone unlock save <pin> <answer>` and it's stored in a **local vault encrypted at rest** (`config/phone_vault.json` — AES-256-GCM, key derived from your security answer via scrypt, never plaintext), then `phone unlock <answer>` shows it back whenever you forget. Hardened the way the mobile-security research prescribes: the answer is compared in **constant time** (`hmac.compare_digest` — no timing side channel), the 3-strike lockout **persists across daemon restarts** (restarting can't reset the strikes) and escalates, and a **tampered or corrupted vault is detected and reported**, never silently read as empty. **Forgot it before you saved anything?** `phone unlock search <answer>` hunts your PC (Documents/Desktop/Downloads) and the phone's `/sdcard` for PIN-like codes sitting near "pin"/"password"/"code" keywords — read-only and bounded; it never tries anything on the lock screen. With nothing saved, it explains the real recovery paths (Xiaomi/Google/Samsung/iPhone account unlock — which erase the device). **No trial-and-error is ever attempted** — that locks the phone. The tool, its vault, and its lockout state are gitignored on purpose and never committed.
- **`phone trace`** — **wireless keylogger with smart timeout**: captures your PIN via `adb shell getevent` while you enter it on the lock screen. Android's lock screen sends `KEYCODE_0`–`KEYCODE_9` raw input events that `getevent` can see — the screen shows dots, but the keycodes ARE the digits. **Smart timeout**: instead of waiting the full 30 seconds, it stops **2 seconds after the last digit** is entered — so a 4-digit PIN captured in 3 seconds returns in ~5s, not 30s. **Biometric detection**: if the phone unlocks via fingerprint/face (no digits typed), it detects the keyguard dismissal and reports it. Two modes: **one-shot** (`phone trace`) and **live** (`phone trace live` — background daemon; `trace status` to check; `trace stop` to stop). `phone trace save=true answer=<answer>` auto-saves to the vault. Works wirelessly over ADB — no USB cable needed.
- **`phone pinpad_map`** — **Vision PIN pad mapper**: takes a screenshot of the lock screen, sends it to the vision model (Groq/GitHub LLM), and extracts the **exact pixel coordinates of each digit button** (0–9) on the PIN pad. The LLM analyzes the image and returns a coordinate map like `1: (270, 800), 2: (540, 800), ...` that you can use with `phone tap X Y` for precise automated PIN entry. Caveat: Android's `FLAG_SECURE` prevents `screencap` from capturing the actual lock screen on many phones (returns black) — works best on stock Android, Samsung, and older MIUI. On phones with `FLAG_SECURE`, use `phone screen` (scrcpy) which can mirror locked screens.
- **`phone pinpad`** — **Vision lock screen observer**: takes a screenshot and asks the vision model to read the lock screen state — how many dots are filled in the PIN indicator, whether the keyboard is visible, where the emergency/backspace buttons are, and a description of what's on screen. Useful for verifying PIN entry progress ("I tapped 3 digits — does the screen show 3 dots?"). Same `FLAG_SECURE` caveat as `pinpad_map`.
- Everything is bounded (no hung adb) and runs on the same single browser/device discipline as the rest of Jeeves — lightweight in the background.

### 🤵 Secretary mode

Turn it on with `secretary on` and Jeeves holds conversations on your behalf — like a real assistant, it acknowledges fast, shields you from routine chatter, and **never over-commits** (no confirming plans, prices, or decisions for you). The decision engine is deterministic rules, so it's instant and free — no LLM per message. Everything is plain words, no tool syntax:

- **`secretary on` / `secretary off`** — toggle. (`secretary status` or `any messages for me` checks state/inbox.) **Turning it off reports back**: `secretary off` ends with a session report — who it talked to, what it told them, any calls it saw (audio/video, ringing or missed), and anything still waiting for your answer.
- **Automatic monitoring, fully in the background:** `secretary on` starts **monitoring WhatsApp without any window on screen** (after the one-time link). It reads the chat list straight from the page's DOM (Playwright) — no screenshots, no vision, no foreground requirement, and a locked screen doesn't matter. Auto-replies go out through the same background session. The daemon keeps this up even when you're not chatting; `secretary off` stops triaging third parties (the remote dashboard above stays connected), and `daemon stop` stops everything.
- **One-time link, then always connected:** the first time, a **real WhatsApp window opens showing the QR code** — scan it with your phone (WhatsApp → Linked devices → Link a device) and that's it. The session is saved in the persistent profile (`.whatsapp_profile`), so **you never log in again** — every later `secretary on`, daemon restart, and background send reuses the same saved session. Want the window back on screen anytime? Say **`link whatsapp`** (or `secretary link`); the daemon reuses the *same* window — it never spawns a new one per `secretary on` — and `secretary link close` (or `link whatsapp off`) closes it.
- **Two ways to connect:** (1) **Use your own browser (recommended if you're already logged in):** start Chrome/Edge with remote debugging — close it, then run `chrome --remote-debugging-port=9222` — and set `"secretary_cdp_url": "http://127.0.0.1:9222"` in `config/api_keys.json`. The monitor then drives **your logged-in WhatsApp Web tab** — no QR, no separate browser. (2) **Dedicated WhatsApp window** (default): a persistent profile browser — linked once via the on-screen QR (or `qr_login.png`), session saved forever.
- **Scans the Unread section only — nothing else:** instead of walking the whole chat list, the monitor switches the pane to WhatsApp's own **Unread filter** (a real click on the filter button → the Unread tab) so it only ever sees chats with unread messages — a small list instead of every chat. Within that, it tends **recent messages first** (last message from the past 24h, newest first) before older unread, so the catch-up always starts with what's fresh.
- **Monitors everyone, never groups:** every `"secretary_poll_seconds": 15` seconds it reads the unread pane and feeds each new sender + message preview to the triage rules — all contacts are handled (no more "unknown sender" escalation); **group chats are read but never replied to** (two layers: the poll drops rows that look like groups — including emoji-name contacts that merely *look* like group avatars — and the sender double-checks the opened chat's header — "group info" hint or member list — before typing, so even a photo-less group that looks like a contact can't slip a reply out). Handled messages are fingerprinted, so nothing is ever re-handled or double-replied, even across daemon restarts. Config: `secretary_poll_seconds` (min 5), `secretary_headless: false` to keep the dedicated browser window visible instead of headless.
- **Your own chat = a remote dashboard — always on, not secretary-dependent:** set `"secretary_self_chat": "Omoke Jr"` (a string or a list) to the chat you use when **texting yourself from another number** — e.g. `Omoke Jr` next to `Omoke`. Messages that land there are **not** triaged like a third party's: they run through the **full Jeeves CLI brain** — shortcuts first (instant, free: `system status`, `search ...`, `msg wife hi`), then the LLM + tools — and the reply comes back into that same chat. **The dashboard works even when secretary mode is OFF**: as long as the daemon is running, the WhatsApp monitor stays up and routes your self-chat messages to Jeeves — `secretary off` only stops triaging *other people's* chats (and `secretary on` from your phone turns that back on without restarting anything). The monitor also counts as daemon liveness, so the daemon won't idle-shut-down while the dashboard is connected. Vision works remotely too: `what's on my screen` / `take a picture` captures on the PC and the **actual analysis comes back as the WhatsApp reply** (the Gemini Live transcript when the session is running, else the still-image description), so you can see what's on your PC from your phone. **Files work too — send any document, photo or video to the chat and Jeeves downloads it to the PC (`whatsapp_media/`) and "attaches" it like `/attach`**: the next message you send is run against that file ("summarize this", "extract the text", "what does it say?"), and it stays attached until you send `detach` or a new file. Voice notes and stickers can't be pulled down yet. Your phone becomes a remote terminal: `secretary on` from it, `any messages for me`, `briefing`, whatever the CLI can do. **The phone tool works from here too** — `phone status`, `phone screenshot and describe it`, `phone apps`, `phone launch com.whatsapp`, `phone tap 540 1200`, `phone shell dumpsys battery` — Jeeves drives your Android phone wirelessly (ADB over Wi-Fi, see 📱 Phone control below) and the reply comes back into the chat.
- **Feed a message in manually** (still works, e.g. for other apps): `mom says: dinner at 7?`, `message from mom: dinner at 7?`, or `handle from mom: dinner at 7?`
- **Familiar names work for people in memory:** if a relationship is stored in long-term memory (e.g. `relationships.wife` → the real contact 😻もま かて), say **`msg wife hi`** and Jeeves sends to that person — no need for the exact WhatsApp name.
- **Calls are watched too:** while monitoring, the secretary also watches for **incoming audio/video calls** — it can't pick up, so it escalates to you ("Esther is calling you (video)") and logs every call it saw into the session report. Missed calls in the chat list ("Missed voice call") are escalated, never auto-replied to like a text.
- **Routine messages** get an automatic reply (sent over WhatsApp) — and by default the reply is **drafted by Meta AI** (WhatsApp's own AI) in your casual style: sheng/Swahili when the sender writes casually, `lol`/`ty`, emoji — like a real friend texting back. The golden rule never changes: the draft prompt forbids committing to anything, a rejection filter discards any draft that would ("count me in", "see you at…"), and every failure mode (AI down, timeout, a burst of >3 messages in a minute) falls back to the instant built-in template, so a reply is never blocked or delayed by the AI. Turn it off with `"secretary_meta_ai_drafts": false` in `config/api_keys.json`. Note: the sender's message is sent to Meta AI to draft the reply (that's the feature); WhatsApp only lets Meta read what you share with it.
- **Replies use what each contact calls YOU** — not a generic "boss": the secretary scans your existing chats once (at most every 24h, in the background — `secretary scan` re-runs it anytime, `secretary scan deep` reads more chats) and learns the name each person uses for you, e.g. the wife's chat → **"Ziii"**. The reply then says *"I'll make sure Ziii sees it"* instead of *"I'll make sure Boss sees it"*. It's a **static map** — the scan is the only place chat text is read; per-reply lookup is a cached dict, so drafting stays instant and free (nothing is re-extracted per message). Dictionary terms (baby, honey, mzee, bro, mrembo…) are caught by pattern rules; novel nicknames the dictionary can't list are caught by a one-time brain pass over the sampled messages; senders with no found name get the neutral **"My boss"**. The Meta AI chats and your own self-chat are never scanned. Turn the whole feature off with `"secretary_pet_names": false` in `config/api_keys.json`.
- **Media gets a reply that fits the TYPE — and when Meta AI drafts are on, photos/videos/documents are FORWARDED to Meta AI for REAL content analysis.** The chat list only shows a preview label ("Photo", "Voice message", …), so for type-only reactions: a photo → *"Wow, stunning photo! 😍"*, a document → a professional acknowledgment, a voice note → "got it", a sticker/GIF → playful (the built-in template, or Meta AI's draft when enabled). But for forwardable media (photo/video/document/GIF) the secretary now does better than a type template: it sends the safe type-ack **instantly**, then forwards the actual file to Meta AI through WhatsApp's native forward path and sends **Meta AI's real analysis** as a follow-up — e.g. a forwarded CV got back *"Nimeipata CV yako 💪 Unataka ni-edit nini ndani ama ni-check tu?"*. No attachment plumbing, no downloads — WhatsApp's own forward-to-Meta-AI flow, running headless in the background browser. Stickers/voice notes can't be forwarded that way, so they keep the type reply. Reactions ("Reacted to …") and recalls/deletions get **no reply at all** — pure noise is silently skipped, never acknowledged or logged.
- **Urgent / money / legal / decision requests or repeat messages** are **escalated to you**: `any messages for me` shows each one with the suggested reply.
- **Answer personally:** `reply to mom: yes sounds good` — it sends as you and clears that sender's inbox items.

The daemon also exposes an `incoming` request type (`{"type": "incoming", "from": "Mom", "text": "..."}`) so a future WhatsApp/Telegram listener can hand messages straight to the secretary.

### 📖 Per-tool tutorials

Every tool has a one-liner tip shown the **first time you use it** in a session, and a full text tutorial. In the REPL: `/tools <name>` (or `/tutorial <name>`) prints the full tutorial for any tool — e.g. `/tools send_message`. `/tools` with no name lists every tool.

### 🔄 The Daemon

The daemon (`python cli.py --daemon`) is a persistent background server that keeps the AI brain and your conversation **warm** on a local TCP socket (`127.0.0.1:8877`, token-authenticated). Set `"daemon_host": "0.0.0.0"` in `config/api_keys.json` (or pass `--host`) to bind the LAN so the Termux remote terminal (above) can reach it — every request still requires the token. Without it, every one-shot call pays a full cold start; with it, calls after the first are nearly instant. The conversation persists across calls until you `reset`, and the client auto-starts the daemon on first use (logs: `jeeves_daemon.log`). It **auto-shuts down after 10 minutes without requests** — `--idle-timeout <seconds>` changes that (or `0` to keep it alive forever), and any request resets the timer. While the WhatsApp monitor is running (secretary on, or the always-on remote dashboard), the daemon treats that as liveness and won't idle-shut-down.

---

## 🧠 Brains / Providers

Jeeves can use multiple LLM providers as its "brain." The project currently
supports two built-in providers:

- **Groq** — the default provider (free Groq API / LLaMA-family models).
- **GitHub Models** — GitHub-hosted models such as `gpt-4.1`/`gpt-4.1-mini`.

You can pick the active provider from the in-app setup screen or by editing
`config/api_keys.json` and setting `brain_provider` to either `groq` or
`github_models`. Example:

```json
{
	"brain_provider": "github_models",
	"github_models_api_key": "YOUR_GITHUB_MODELS_KEY",
	"os_system": "windows"
}
```

Auto-fallback: if a request fails due to rate limits, quota, or an expired
API token, Jeeves will attempt the other configured provider and adopt it if
the fallback request succeeds. This helps keep the assistant responsive when
one provider becomes temporarily unavailable.

### 🤖 Meta AI — the AI inside WhatsApp

WhatsApp ships a full AI assistant — **Meta AI** — as an ordinary chat. Jeeves
can use it, because the background WhatsApp browser already knows how to open
chats and read replies:

- **`ask meta ai <question>`** (or `meta ai: <question>`, or the `meta_ai`
  tool) sends the question to Meta AI through the same background browser as
  sends/monitoring and returns its answer — research, second opinions,
  brainstorms, or images (`imagine a ...`). No new login, no screen needed.
- **Media goes to Meta AI by FORWARDING, not attaching:** the secretary
  forwards received photos/videos/documents to Meta AI via WhatsApp's native
  forward-to-Meta-AI flow (hover → "Forward media" → pick Meta AI → send),
  and Meta AI genuinely analyzes the file — a forwarded CV came back with
  *"Nimeipata CV yako 💪 Unataka ni-edit nini ndani ama ni-check tu?"*. This
  runs headless in the same background browser; no download, no file
  plumbing, no attach-popup fragility.
- **Emergency brain:** if the configured LLM providers fail (all keys
  exhausted, rate-limited, offline), Jeeves automatically routes the question
  to Meta AI instead of going "can't reach my brain" — the reply is marked
  `[Meta AI]`.
- **The secretary never argues with it:** Meta AI's replies arrive in the
  chat list like any unread chat, but the monitor recognizes them and never
  triages or auto-replies to them (that would create an AI-argues-with-AI
  loop).

If the Meta AI chat isn't on your account (it's region/feature-flagged),
`ask meta ai` reports that clearly instead of failing silently.


## 📋 Requirements

| Requirement | Details |
|---|---|
| **OS** | Windows 10/11, macOS, or Linux |
| **Python** | 3.11 or 3.12 |
| **Microphone** | Required for voice interaction |
| **API Keys** | Free Gemini API key + Free OpenRouter API key |

---

## ⚠️ License

Personal and non-commercial use only.
Licensed under **[Creative Commons BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/)**.

---

## 👤 Connect with the Creator

Engineered by a developer building a real-world JEEVES-style assistant.
⭐ **Star the repository to support the journey to Mark 100.**

| Platform | Link |
|---|---|
| YouTube | [@FatihMakes](https://www.youtube.com/@FatihMakes) |
| Instagram | [@fatihmakes](https://www.instagram.com/fatihmakes) |
