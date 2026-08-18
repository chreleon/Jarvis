# config/tool_tips.py
# Per-tool tips + text tutorials for every Jeeves tool.
#
# Two consumers:
#   • get_tool_tip(name)  — one-liner shown the first time a tool is used
#   • tool_tutorial(name) — full text shown by `/tools <name>` / `/tutorial <name>`
#
# Keep entries short, concrete, and example-first — a user should be able
# to copy the example and get a result.

TUTORIALS: dict[str, dict] = {
    "open_app": {
        "what": "Opens any application on your computer (Windows search).",
        "when": "When you say 'open <app>' — e.g. 'open notepad', 'open visual studio code'.",
        "example": "tool open_app app_name='Spotify'",
        "note": "App names are fuzzy — 'open the terminal' works. If nothing opens, check the app is installed and the name is recognizable.",
    },
    "web_search": {
        "what": "Searches the web (search / news / research / price / compare modes).",
        "when": "For facts, news, prices, or research. 'search X', 'google X', 'look up X' all work.",
        "example": "tool web_search mode='news' query='latest AI news'",
        "note": "No API keys — DuckDuckGo/Bing under the hood, with the LLM racing for a faster answer.",
    },
    "meta_ai": {
        "what": "Asks Meta AI — the AI assistant built into WhatsApp — a question and returns its answer.",
        "when": "When you want a second AI's take, research help, a brainstorm, or an image ('imagine a ...'). Also used automatically when the main brain is unreachable.",
        "example": "ask meta ai what is the capital of Kenya",
        "note": "Runs through the same background WhatsApp browser as sends/monitoring — no new login, no screen needed. If the Meta AI chat isn't on your account (region-flagged), it reports that clearly. The secretary never auto-replies to Meta AI's messages.",
    },
    "phone_control": {
        "what": "Wireless control of your Android phone via ADB — see its screen, tap/type, launch apps, move files, ring it when misplaced, and keep a local copy of its PIN for when you forget it.",
        "when": "'phone status' · 'phone connect' (one-time USB step, then cable-free) · 'phone screen' (LIVE mirror + control via scrcpy — Phantom Droid-style remote view) · 'phone screenshot' / 'what's on my phone' (Jeeves describes the screen) · 'phone ring' / 'find my phone' (max-volume ring) · 'phone unlock <answer>' (PIN vault) · 'phone macro <name>' (fires a MacroDroid macro; 'phone macro list/start') · 'phone termux <cmd>' (real Linux shell into the phone via Termux SSH — 'phone termux status/setup') · 'phone notify <text>' (push a notification) · 'phone battery' · 'phone dev [on|off]' (Developer Options tuning) · 'phone gps' (Termux:API GPS) · 'phone devices' (all connected phones) · 'phone logcat 200' (recent logs) · 'phone wifi' / 'phone network' / 'phone report' (health report) / 'phone top' / 'phone storage' · 'phone apps' · 'phone launch com.whatsapp' · 'phone tap 540 1200' · 'phone shell dumpsys battery'.",
        "example": "phone screenshot and describe it",
        "note": "Full action set: status | connect | info | screenshot (analyze=true for vision) | ring [seconds] (ring stop silences) | unlock [save <pin> <answer> / <answer> / clear <answer> / search <answer>] | macro [list / start / <name> [value]] | tap x y | swipe x1 y1 x2 y2 [ms] | text '...' | key home/back/volume... | apps [query] | launch <pkg> | stop <pkg> | files [path] | pull <remote> [local] | push <local> <remote> | shell '<cmd>'. MacroDroid macros (phone-side automation adb can't do — sensors, toggles, on-phone events): map a friendly name in 'phone_macros' (config/api_keys.json) to an intent action ('com.jeeves.macro.X' fired via am broadcast) or an HTTP path ('/flash' fired via the local HTTP server, port from 'phone_macrodroid_port', default 8080); 'phone macro start' launches MacroDroid so its receivers are live.  Requires the matching MacroDroid macro ('Intent Received' or 'HTTP Server Request' trigger) to actually exist on the phone. The 'Jeeves' starter set (flash / flashoff / ping — HTTP Server Request triggers on /flash, /flashoff, /ping, port 8080) is installed and verified on the phone; map more names in 'phone_macros'. Needs USB debugging enabled; the first 'phone connect' switches ADB to Wi-Fi (port 5555). 'phone dev' manages Developer Options for reliable control (all safe settings put/delete, reversible with 'phone dev off'): status shows current values; 'on' sets stay-awake-on-any-power, UI animation scales to 0 (snappier taps/screenshots), adb_wifi_timeout_ms=0 (the wireless adb session otherwise silently expires after the 10-minute default — the cause of 'phone keeps dropping'), and adb_authorization_timeout=0 (never re-ask for the debug authorization). Never touches OEM/bootloader unlock — that wipes the phone. Termux: the Play build of Termux has no RUN_COMMAND service, so Jeeves runs a real shell into the phone via openssh INSIDE Termux (port 8022, key-only auth, key pair kept only in config/termux_keys/ — gitignored). 'phone termux setup' bootstraps it once by driving the Termux terminal via adb (installs openssh, drops the PC key, starts sshd; idempotent); 'phone termux status/start/stop' manage it; 'phone termux <cmd>' runs any safe command inside Termux, and friendly names map to the Termux:API commands that need the (separately installed) Termux:API app: battery, gps/location, clipboard get/set, sensors, camera, torch, volume, vibrate, wifi. 'phone notify <text>' pushes a real notification to the phone's shade (native cmd notification, no Termux). 'phone battery' is a formatted live battery report (native). The phone is identified by its STABLE serial (not the IP, which changes): a local profile (config/phone_profile.json, gitignored) remembers it, and when the IP changes, re-running 'phone connect' re-finds the phone by scanning the local subnet for adb's port and verifying the serial — no cable needed. Screenshots land in phone_shots/. Destructive shell commands (uninstall/reboot/wipe/rm/...) are refused. 'phone unlock' is a security-question-gated LOCAL PIN vault (answer checked in constant time; escalating lockout 5 min → 30 min → 2 h → 24 h that survives restarts): your phone's real lock code can never be read from the device (Android hashes it in a root-only file, iPhones keep it in the Secure Enclave), so it only shows what you saved yourself — save it with 'phone unlock save <pin> <answer>' while you know it, and it's stored ENCRYPTED at rest (AES-GCM keyed by your answer via scrypt; tampering is detected). Forgot it before saving? 'phone unlock search <answer>' hunts your Documents/Desktop/Downloads and the phone's /sdcard for PIN-like codes near 'pin'/'password'/'code' keywords — read-only, bounded, never touches the lock screen. The unlock tool, its vault, and its lockout state are gitignored and never committed.",
    },
    "system_status": {
        "what": "Live system health: CPU, RAM, GPU, temperature, uptime.",
        "when": "'system status', 'cpu usage', 'how is my computer'.",
        "example": "tool system_status",
        "note": "Instant and free — one of the smart shortcuts, no AI needed.",
    },
    "manage_monitor": {
        "what": "Background news monitoring: watch a topic, get daily headline alerts.",
        "when": "'monitor <topic>', 'keep an eye on <topic>', 'stop monitoring <topic>'.",
        "example": "tool manage_monitor action='add' topic='PS5 restock'",
        "note": "Checks once per day per topic and alerts on NEW headlines only. Use 'briefing' to see active topics.",
    },
    "weather_report": {
        "what": "Weather for a city (opens the forecast in your browser).",
        "when": "'weather', 'weather in paris'.",
        "example": "tool weather_report city='London'",
        "note": "Opens a browser tab — it's a live forecast page, not a text summary.",
    },
    "send_message": {
        "what": "Sends WhatsApp/Telegram/Instagram messages (background WhatsApp Web bridge first, desktop app / browser fallback).",
        "when": "'msg <name>: <text>', 'text mom hi', 'tell bob yo'. Try 'secretary mode on' to have messages handled for you.",
        "example": "tool send_message receiver='Mom' message_text='hi there' platform='whatsapp'",
        "note": "WhatsApp sends use the same background browser as the secretary monitor — nothing needs to be on screen or focused, and it reuses the monitor's window when one is running (one browser, no collisions). Connect once with 'link whatsapp': a window opens with the QR, you scan it, and the session is saved forever — every future send/monitor reuses the same window, no re-login. If the background browser isn't linked yet it falls back to the foreground flow automatically. Force a path with method='bridge' | 'desktop' | 'shortcut' | 'vision'.",
    },
    "reminder": {
        "what": "Schedules a timed reminder via Windows Task Scheduler.",
        "when": "'remind me at 18:00 to call mom' or a specific date/time.",
        "example": "tool reminder date='2026-08-20' time='09:00' message='Standup'",
        "note": "Times are 24h (HH:MM). The reminder pops a toast + plays a beep at the scheduled moment.",
    },
    "youtube_video": {
        "what": "Plays YouTube videos, summarizes content, gets info, or lists trending.",
        "when": "'play despacito', 'summarize this video', 'trending on youtube'.",
        "example": "tool youtube_video action='play' query='lofi beats'",
        "note": "'play' opens YouTube in your browser; 'summarize' fetches the transcript.",
    },
    "screen_process": {
        "what": "Vision: captures and analyzes your screen or webcam.",
        "when": "'what's on my screen', 'screenshot', 'take a picture'.",
        "example": "tool screen_process text='Describe the screen' angle='screen'",
        "note": "Screenshot is analyzed by the vision model — privacy note: the image is sent to the model provider.",
    },
    "computer_settings": {
        "what": "System controls: volume, brightness, WiFi, window management, shutdown.",
        "when": "'volume up', 'mute', 'brightness 50', 'close this window', 'show desktop'.",
        "example": "tool computer_settings action='volume' value='50'",
        "note": "Covers both media keys and OS-level commands depending on your platform.",
    },
    "browser_control": {
        "what": "Drives a real browser (Chrome/Edge/Firefox): navigate, click, fill forms, extract content.",
        "when": "'go to example.com', 'search google for X in the browser', 'click the login button'.",
        "example": "tool browser_control action='go_to' url='https://example.com'",
        "note": "Uses Playwright — heavier than web_search, but it actually operates the page.",
    },
    "file_controller": {
        "what": "File/folder management: list, create, read, write, move, copy, delete, find.",
        "when": "'list my desktop', 'create a folder called projects', 'find files named report'.",
        "example": "tool file_controller action='list' path='desktop'",
        "note": "Paths accept shortcuts: desktop, downloads, documents, home.",
    },
    "desktop_control": {
        "what": "Desktop visual control: wallpaper, organize files, clean up, stats.",
        "when": "'set wallpaper to X', 'organize my desktop'.",
        "example": "tool desktop_control action='wallpaper' path='C:/images/art.jpg'",
        "note": "Can run LLM-generated sandboxed Python for advanced desktop tasks.",
    },
    "code_helper": {
        "what": "Writes, edits, explains, runs, and debugs code files.",
        "when": "'write a python script that does X', 'explain this file', 'fix this error'.",
        "example": "tool code_helper action='write' description='hello world in python' language='python'",
        "note": "Runs code locally — files are saved to your chosen path.",
    },
    "dev_agent": {
        "what": "Builds complete multi-file projects from a description.",
        "when": "'build me a flask web app', 'create a project that parses CSV files'.",
        "example": "tool dev_agent description='a to-do list app in python'",
        "note": "Plans, writes files, installs deps, and tries to run the project. Heavier — give it a clear spec.",
    },
    "computer_control": {
        "what": "Direct mouse/keyboard control: type, click, hotkeys, drag, screenshot.",
        "when": "'type hello', 'press ctrl+s', 'click at 500,400', 'take a screenshot'.",
        "example": "tool computer_control action='type' text='hello world'",
        "note": "Controls your real mouse/keyboard — make sure the right window is focused first.",
    },
    "cmd_control": {
        "what": "Runs system commands and opens files.",
        "when": "'run ipconfig', 'open the readme'.",
        "example": "tool cmd_control task='run ipconfig'",
        "note": "Shell access — be mindful of what you ask it to run.",
    },
    "game_updater": {
        "what": "Updates/installs Steam and Epic Games titles.",
        "when": "'update my steam games', 'install cyberpunk'.",
        "example": "tool game_updater action='list' platform='steam'",
        "note": "Drives the game launchers via UI automation.",
    },
    "flight_finder": {
        "what": "Searches Google Flights for the best route/price.",
        "when": "'find flights from NYC to London on June 15'.",
        "example": "tool flight_finder origin='NYC' destination='London' date='2026-06-15'",
        "note": "Opens Google Flights in your browser with the parsed search.",
    },
    "composio_action": {
        "what": "Runs the Composio agent (Gmail, GitHub, Google Calendar) — connected accounts only.",
        "when": "'read my email', 'create a github repo', 'what's on my calendar'.",
        "example": "tool composio_action request='Summarize my unread email'",
        "note": "Requires a connected Composio account and Groq keys. '/agent <task>' does the same in the CLI.",
    },
    "file_processor": {
        "what": "Processes files by type: images, PDFs, text, data, code, audio, video, archives, PPTX.",
        "when": "'compress this image', 'convert that pdf to text', 'summarize the meeting notes'.",
        "example": "tool file_processor action='process' path='C:/report.pdf'",
        "note": "Auto-detects the file type and picks the right pipeline.",
    },
    "shutdown_jeeves": {
        "what": "Gracefully shuts down Jeeves (saves state, closes cleanly).",
        "when": "You're done for the day.",
        "example": "tool shutdown_jeeves",
        "note": "Use this instead of killing the window — it saves your session.",
    },
    "save_memory": {
        "what": "Stores a fact in long-term memory.",
        "when": "'remember that my birthday is June 3', 'save that I prefer dark mode'.",
        "example": "tool save_memory category='identity' key='birthday' value='June 3'",
        "note": "Jeeves also auto-extracts memory from your conversations.",
    },
    "business_tracker": {
        "what": "Local income/expense tracking: add, balance, monthly reports, CSV import.",
        "when": "'track $50 income from freelancing', 'my balance', 'how much did I spend in July'.",
        "example": "tool business_tracker action='add' kind='income' amount=50 label='freelance'",
        "note": "Data stays in long-term memory — no external accounts.",
    },
    "daily_briefing": {
        "what": "One-command day summary: finances, monitored topics, upcoming reminders (+ optional email).",
        "when": "'good morning', 'briefing', 'what's my day like'.",
        "example": "tool daily_briefing include_email=false",
        "note": "Local and instant. include_email=true adds a Composio email summary (slower).",
    },
    "anime_watch": {
        "what": "Anime monitor + recommender: new airings this season, trending picks, Netflix availability.",
        "when": "'new anime', 'trending anime', 'is demon slayer on netflix'.",
        "example": "tool anime_watch action='check' title='Demon Slayer'",
        "note": "AniList popularity = the 'internet-approved' signal; Netflix flags come from a keyless web check.",
    },
    "secretary": {
        "what": "Secretary mode: holds conversations, priority inbox, follow-up tracking, contact CRM, and proactive alerts.",
        "when": "'secretary on' to start; 'secretary off' for session report. First time opens a WhatsApp QR — scan once, stays connected. 'briefing' for morning summary; 'followups' to see promises tracked; 'alerts' for overdue items; 'contact' for CRM.",
        "example": "secretary action='briefing'",
        "note": "Full action set: on/off/status | link/link close | handle (process one message) | inbox (priority-sorted: 🔴urgent → 🟡today → 🔵week → ⚪fyi) | snooze <n> (demote to weekly) | done <n> (remove handled) | reply (personal reply) | inbox_clear | followups (promises the boss made, tracked automatically) | followup_done <n> | briefing (morning summary: inbox + follow-ups + stats) | alerts (proactive: overdue follow-ups, stale conversations, inbox overflow) | contact [name] (CRM: list all, or show/update relationship/notes) | scan (pet-name discovery) | report (session summary). The priority inbox classifies escalations: 🔴 URGENT (money/legal/emergency, 5+ unanswered) → 🟡 TODAY (decisions, calls, time-sensitive) → 🔵 WEEK (scheduling, planning) → ⚪ FYI (informational). Follow-up tracking automatically detects promises in messages (e.g. 'I'll call tomorrow') and logs them with deadlines — 'briefing' shows overdue ones. Contact CRM tracks who each person is, their relationship to you, and notes — auto-populated from conversations.",
    },
    "agent_task": {
        "what": "Hands a goal to the Composio agent (same as composio_action).",
        "when": "'/agent check my email and reply to anything urgent'.",
        "example": "tool agent_task goal='Summarize unread email'",
        "note": "Requires connected Composio accounts + Groq keys.",
    },
}


# ── Smart shortcuts (natural-language things the user can say) ────────────────
# (phrase, what it does) pairs — rendered by the HUD's side-panel TIPS section.
# GUI-appropriate only (no slash commands / cli.py subcommands).
SHORTCUT_TIPS: list[tuple[str, str]] = [
    ("what's on my screen", "analyze the screen (vision)"),
    ("take a picture", "use the camera (vision)"),
    ("open <app>", "open an app, e.g. 'open notepad'"),
    ("search <query>", "web search, e.g. 'search python 3.13'"),
    ("play <song>", "play on YouTube, e.g. 'play despacito'"),
    ("weather [in <city>]", "weather report"),
    ("system status", "CPU / RAM / uptime monitor"),
    ("what time is it", "instant answer (no AI)"),
    ("what do you remember", "long-term memory"),
    ("msg <name>: <text>", "send a WhatsApp message"),
    ("whatsapp / telegram / ig <name> <text>", "pick the messaging app"),
    ("my balance", "income/expense snapshot"),
    ("track $50 income from X", "log income/expense"),
    ("briefing / good morning", "full day summary"),
    ("new anime / trending anime", "season airings + popular picks"),
    ("secretary on / off", "hold conversations for you"),
    ("link whatsapp", "open the persistent WhatsApp window — scan once, stay connected"),
    ("any messages for me", "escalated messages needing YOU"),
    ("mom says: dinner at 7?", "feed an incoming message to the secretary"),
    ("reply to mom: yes", "answer an escalated message personally"),
    ("phone status", "phone connection + battery/screen info"),
    ("phone connect", "one-time USB step, then control the phone over Wi-Fi"),
    ("what's on my phone", "screenshot your phone — Jeeves describes the screen"),
    ("phone ring / find my phone", "ring the phone at max volume to find it"),
    ("phone unlock", "security-question PIN vault — save, show, or SEARCH your files/phone for your PIN"),
    ("phone dev on", "Developer Options: wireless adb never expires + stay-awake + fast UI"),
    ("phone macro flash", "fire the Jeeves MacroDroid macros: /flash /flashoff /ping"),
    ("phone termux ls /sdcard", "run any command inside Termux — a real Linux shell on the phone"),
    ("phone notify hello", "push a notification to the phone's shade"),
    ("phone battery", "live battery report (level, temp, power source)"),
    ("phone gps", "real GPS location via Termux:API (when installed)"),
    ("phone apps / phone launch <pkg>", "list apps / open an app on the phone"),
    ("phone tap 540 1200", "tap the phone screen (pixel coords)"),
    ("phone files / phone pull <path>", "browse / copy files off the phone"),
]


def get_tool_tip(name: str) -> str:
    """One-line usage tip for a tool ('' when unknown)."""
    t = TUTORIALS.get(name)
    if not t:
        return ""
    return f"💡 {name}: {t['when']}  →  {t['example']}"


def tool_tutorial(name: str) -> str:
    """Full text tutorial for a tool ('' when unknown)."""
    t = TUTORIALS.get(name)
    if not t:
        return ""
    return (
        f"🔧 {name}\n"
        f"  what:  {t['what']}\n"
        f"  when:  {t['when']}\n"
        f"  try:   {t['example']}\n"
        f"  note:  {t['note']}"
    )


def all_tutorial_names() -> list[str]:
    return sorted(TUTORIALS)


def random_tip_entry() -> tuple[str, str]:
    """(tool_name, tip_text) for a random tool.

    The tool name lets UIs (HUD, dashboard) link the tip straight to its
    full tutorial. The tip reads as something the user can actually type
    — the 'what' plus the natural-language 'when' triggers, no CLI syntax.
    """
    import random
    name = random.choice(list(TUTORIALS))
    t = TUTORIALS[name]
    return name, f"💡 {t['what']}  Try: {t['when']}"


def random_tip() -> str:
    """One random natural-language usage tip (for the GUI HUD log)."""
    return random_tip_entry()[1]
