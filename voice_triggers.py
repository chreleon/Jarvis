"""
voice_triggers.py -- Bridge Jeeves to Google Assistant via TRIGGERcmd.

Three free-to-use trigger paths (IFTTT is now PAID, so it is demoted to an
optional legacy option):

  A) TRIGGERcmd Smart Home Google Assistant action (recommended, free)
      "Hey Google, turn on status report"
      -> TRIGGERcmd cloud -> local agent -> commands.json -> jeeves CLI

  B) TRIGGERcmd MCP server (free, built into Jeeves) - no voice service at all:
      [Jeeves' brain (Groq + tools)] -> MCP client -> triggercmd-mcp binary
      -> TRIGGERcmd cloud -> local agent -> commands.json -> jeeves CLI
      Configured by writing one stdio entry into config/api_keys.json under
      "mcp_servers"; mcp_client.py + composio_agent.py pick it up so the
      brain can run any TRIGGERcmd command directly (voice or typed).

  C) IFTTT webhooks (legacy, requires a paid IFTTT subscription)
      [Google Assistant] -> IFTTT applet -> /api/IFTTT?trigger=<name>... -> cloud

This module:
  * keeps a simple trigger list in config/api_keys.json under "triggercmd"
  * generates the agent's commands.json (with a .bak backup)
  * downloads/configures the official TRIGGERcmd stdio MCP server binary
  * prints plain-English setup steps for all three paths
  * lets the UI edit triggers without touching any code

Trigger schema:
  {
    "phrase":  "good morning",          # the word(s) you say after "OK Google"
    "mode":    "brain" | "tool",        # brain = --send (LLM), tool = --send-tool (deterministic)
    "bridge":  true,                    # true = forwards your spoken words as $1
    "text":    "...",                   # fixed prompt for brain triggers (ignored when bridge)
    "tool":    "system_status",         # tool name for mode=tool
    "args":    {...},                   # JSON args for mode=tool (optional)
    "enabled": true,
  }

NOTE: TRIGGERcmd's placeholder for passed-in parameters is assumed to be $1
(cross-platform). If parameters do not arrive on your OS, change PARAM_TOKEN.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

# TRIGGERcmd commands.json lives in the agent's data dir (per-platform).
DEFAULT_AGENT_DIRS = {
    "Windows": Path.home() / ".TRIGGERcmdData",
    "Darwin":  Path.home() / ".TRIGGERcmdData",
    "Linux":   Path.home() / ".TRIGGERcmdData",
}

# Parameter placeholder for the spoken words (IFTTT {{TextField}} -> params).
PARAM_TOKEN = "$1"

# Friendly presets for the UI dropdown -- plain language, zero code.
PRESETS = [
    {"key": "bridge",      "label": "Ask Jeeves anything (your words go to the brain)", "mode": "brain", "bridge": True},
    {"key": "status",      "label": "Status report (CPU / RAM / GPU)",                  "mode": "tool",  "tool": "system_status"},
    {"key": "lock",        "label": "Lock the computer",                                "mode": "tool",  "tool": "computer_settings", "args": {"action": "lock"}},
    {"key": "sleep",       "label": "Sleep the computer",                               "mode": "tool",  "tool": "computer_settings", "args": {"action": "sleep"}},
    {"key": "shutdown",    "label": "Shut down the computer",                           "mode": "tool",  "tool": "computer_settings", "args": {"action": "shutdown"}},
    {"key": "good_morning", "label": "Good morning briefing (weather + news)",          "mode": "brain", "text": "Good morning, sir. Give me a briefing: today's weather, top headlines, and anything on my schedule."},
    {"key": "diagnostics", "label": "Run diagnostics",                                  "mode": "brain", "text": "Run your health diagnostics and report the results."},
    {"key": "weather",     "label": "Weather report",                                   "mode": "brain", "text": "What is the current weather?"},
    {"key": "music",       "label": "Play some music",                                  "mode": "brain", "text": "Play some music for me."},
    {"key": "vision",      "label": "Look at the screen",                               "mode": "tool",  "tool": "screen_process"},
    {"key": "reminder",    "label": "Set a reminder",                                   "mode": "brain", "text": "Set a reminder for me."},
    {"key": "open_app",    "label": "Open an app (type the app name in Details)",       "mode": "tool",  "tool": "open_app", "args": {"app_name": ""}},
    {"key": "party",       "label": "House party protocol (fun routine)",               "mode": "brain", "text": "Initiate house party protocol. Open something fun and entertain me."},
]

# "Custom" lets a trigger keep its own raw text/tool (editable in Details).
PRESETS = PRESETS + [
    {"key": "custom", "label": "Custom command (type it in Details)", "mode": "brain"},
]

PRESET_LABELS = {p["key"]: p["label"] for p in PRESETS}
PRESET_BY_KEY = {p["key"]: p for p in PRESETS}


# ── JARVIS-inspired trigger set (Iron Man canon -> Jeeves tools) ──────────────
# Seeded into config on first open of the Voice Triggers UI so the user
# never has to remember which phrases exist.
DEFAULT_JARVIS_TRIGGERS = [
    {"phrase": "tell jeeves",        "mode": "brain", "bridge": True,
     "enabled": True},  # free-form: your words go straight to the brain
    {"phrase": "good morning",       "mode": "brain",
     "text": "Good morning, sir. Give me a briefing: today's weather, top headlines, and anything on my schedule.",
     "enabled": True},
    {"phrase": "status report",      "mode": "tool", "tool": "system_status", "enabled": True},
    {"phrase": "run diagnostics",    "mode": "brain",
     "text": "Run your health diagnostics and report the results.", "enabled": True},
    {"phrase": "lock it down",       "mode": "tool", "tool": "computer_settings",
     "args": {"action": "lock"}, "enabled": True},
    {"phrase": "suit up",            "mode": "tool", "tool": "open_app",
     "args": {"app_name": "Code"}, "enabled": True},  # open your dev environment
    {"phrase": "play some music",    "mode": "brain",
     "text": "Play some music for me.", "enabled": True},
    {"phrase": "set a reminder",     "mode": "brain",
     "text": "Set a reminder for me.", "enabled": True},
    {"phrase": "what's the weather", "mode": "brain",
     "text": "What is the current weather?", "enabled": True},
    {"phrase": "take a picture",     "mode": "tool", "tool": "screen_process", "enabled": True},
    {"phrase": "house party protocol", "mode": "brain",
     "text": "Initiate house party protocol. Open something fun and entertain me.", "enabled": True},
    {"phrase": "goodnight",          "mode": "tool", "tool": "computer_settings",
     "args": {"action": "sleep"}, "enabled": True},
    {"phrase": "shutdown",           "mode": "tool", "tool": "computer_settings",
     "args": {"action": "shutdown"}, "enabled": False},  # off by default - destructive
]


def seed_defaults() -> int:
    """Seed the JARVIS trigger set if the user has no triggers configured yet.

    Called when the Voice Triggers UI opens (and by --generate). Returns the
    number of triggers seeded (0 = already configured)."""
    block = get_block()
    if block.get("triggers"):
        return 0
    block["triggers"] = [dict(t) for t in DEFAULT_JARVIS_TRIGGERS]
    save_block(block)
    return len(DEFAULT_JARVIS_TRIGGERS)


# ── Config I/O (config/api_keys.json) ─────────────────────────────────────────

def _load_cfg() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cfg(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def get_block() -> dict:
    """The whole 'triggercmd' config block (never raises)."""
    block = _load_cfg().get("triggercmd", {})
    if not isinstance(block, dict):
        block = {}
    block.setdefault("enabled", False)
    block.setdefault("token", "")
    block.setdefault("computer", "")
    block.setdefault("agent_dir", str(DEFAULT_AGENT_DIRS.get(sys.platform, DEFAULT_AGENT_DIRS["Windows"])))
    block.setdefault("ground", "")  # '' = auto-detect from the agent's debug.log
    block.setdefault("triggers", [])
    return block


def save_block(block: dict) -> None:
    cfg = _load_cfg()
    cfg["triggercmd"] = block
    _save_cfg(cfg)


# ── Trigger CRUD (UI-facing; never raise) ────────────────────────────────────

def get_triggers() -> list[dict]:
    return [t for t in get_block().get("triggers", []) if isinstance(t, dict)]


def set_triggers(triggers: list[dict]) -> None:
    block = get_block()
    block["triggers"] = triggers
    save_block(block)


def add_trigger(trigger: dict) -> tuple[bool, str]:
    phrase = str(trigger.get("phrase", "")).strip()
    if not phrase:
        return False, "Give the trigger a phrase first (the words after 'OK Google')."
    triggers = get_triggers()
    if any(t.get("phrase", "").strip().lower() == phrase.lower() for t in triggers):
        return False, f"A trigger named '{phrase}' already exists."
    triggers.append(trigger)
    set_triggers(triggers)
    return True, f"Added trigger: {phrase}"


def remove_trigger(phrase: str) -> tuple[bool, str]:
    triggers = [t for t in get_triggers()
                if t.get("phrase", "").strip().lower() != str(phrase).strip().lower()]
    set_triggers(triggers)
    return True, f"Removed trigger: {phrase}"


def toggle_trigger(phrase: str, enabled: bool) -> None:
    triggers = get_triggers()
    for t in triggers:
        if t.get("phrase", "").strip().lower() == str(phrase).strip().lower():
            t["enabled"] = bool(enabled)
    set_triggers(triggers)


# ── commands.json generation ──────────────────────────────────────────────────

def _python() -> str:
    return shutil.which("python") or sys.executable


def _cli_path() -> str:
    return str(BASE_DIR / "cli.py")


def _clean_text(text: str) -> str:
    """Strip characters that would break cmd.exe argument parsing."""
    return "".join(c for c in (text or "") if c not in "\"\r\n")


def build_command(trigger: dict, python: str = "", cli: str = "") -> str:
    """The exact command line the TRIGGERcmd agent will run for this trigger."""
    python = python or _python()
    cli = cli or _cli_path()
    if trigger.get("mode") == "tool":
        cmd = f'"{python}" "{cli}" --send-tool {trigger.get("tool", "")}'.strip()
        args = trigger.get("args") or {}
        if args:
            cmd += f' --send-args "{json.dumps(args)}"'
        return cmd
    # brain
    if trigger.get("bridge"):
        return f'"{python}" "{cli}" --send "{PARAM_TOKEN}"'
    text = _clean_text(trigger.get("text", "")) or _clean_text(trigger.get("phrase", ""))
    return f'"{python}" "{cli}" --send "{text}"'


def detect_agent_ground(agent_dir: str = "") -> str:
    """Detect the running agent's mode from its debug.log.

    The agent only syncs commands whose ``ground`` field matches its own run
    mode (agent.js: ``localcmds[l].ground == ground``). Foreground = launched
    interactively (desktop app); background = daemon/service. Defaults to
    "foreground" when the log can't be read or the mode can't be determined.
    """
    log = Path(agent_dir or get_block().get("agent_dir", "")) / "debug.log"
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return "foreground"
    if "run background tasks" in text:
        return "background"
    if "run foreground tasks" in text:
        return "foreground"
    return "foreground"


def resolve_ground() -> tuple[str, str]:
    """(ground, source) - explicit config wins, else auto-detect from the agent."""
    explicit = str(get_block().get("ground", "") or "").strip().lower()
    if explicit in ("foreground", "background"):
        return explicit, "config"
    return detect_agent_ground(), "agent debug.log"


# ── MCP Tool Description (dynamic MCP tools) ─────────────────────────────────
# The TRIGGERcmd MCP server (rvmey/triggercmd-mcp-stdio) turns every command
# that has an mcpToolDescription into a dedicated tool named
# run_<computer>_<command> whose description IS this text - so the AI brain
# (Jeeves' own MCP client) sees exactly what the command does and how to pass
# parameters. Keep these short, concrete, LLM-friendly sentences.

_MCP_DESC_BY_PHRASE = {
    "tell jeeves": (
        "Ask the Jeeves AI assistant anything. Accepts an optional parameter: "
        "the words spoken after the trigger phrase (e.g. 'tell jeeves what is "
        "the weather outside')."
    ),
    "good morning": "Run the Jeeves morning briefing: today's weather, top headlines, and anything on the schedule.",
    "status report": "Report the computer's system status: CPU, RAM, uptime, and running processes.",
    "run diagnostics": "Run Jeeves' health diagnostics and report the results.",
    "lock it down": "Lock the computer.",
    "suit up": "Open the Code development environment (the Jeeves 'suit up' routine).",
    "play some music": "Tell Jeeves to play some music.",
    "set a reminder": "Tell Jeeves to set a reminder.",
    "what's the weather": "Tell Jeeves to report the current weather.",
    "take a picture": "Look at the screen and describe what is currently displayed (vision).",
    "house party protocol": "Run the Jeeves house party protocol: open something fun and entertain the user.",
    "goodnight": "Put the computer to sleep (the Jeeves goodnight routine).",
    "shutdown": "Shut down the computer. CAUTION: this is destructive and ends the session.",
}

_MCP_DESC_BY_TOOL = {
    "system_status": "Report the computer's system status: CPU, RAM, uptime, and running processes.",
    "computer_settings": "Change computer settings. Accepts a parameter object with an action: 'lock', 'sleep', or 'shutdown'.",
    "screen_process": "Look at the screen and describe what is currently displayed (vision).",
    "open_app": "Open an application on the computer. Accepts an optional parameter: the app name to open.",
}


def mcp_tool_description(trigger: dict) -> str:
    """A clean, LLM-friendly description for a trigger's dynamic MCP tool.

    Order: manual override (``mcp_desc`` on the trigger) > curated map by
    phrase > tool map > generated fallback. A bridge trigger always documents
    its optional parameter so the brain knows it can pass spoken words.
    """
    if not isinstance(trigger, dict):
        return ""
    manual = str(trigger.get("mcp_desc") or "").strip()
    if manual:
        return manual
    phrase = str(trigger.get("phrase", "")).strip()
    curated = _MCP_DESC_BY_PHRASE.get(phrase.lower())
    if curated:
        return curated
    if trigger.get("bridge"):
        return (
            "Ask the Jeeves AI assistant anything. Accepts an optional parameter: "
            "the words spoken after the trigger phrase."
        )
    if trigger.get("mode") == "tool":
        tool = str(trigger.get("tool", "")).strip()
        base = _MCP_DESC_BY_TOOL.get(tool, f"Run the Jeeves tool '{tool}'.")
        args = trigger.get("args") or {}
        if args:
            return f"{base} Parameters: {json.dumps(args)}."
        return base
    text = str(trigger.get("text", "")).strip() or phrase
    short = text if len(text) <= 140 else text[:137] + "..."
    return f"Ask the Jeeves AI assistant to: {short}"


def generate_commands_json(triggers: list[dict] | None = None, computer: str = "") -> list[dict]:
    """The commands.json contents for the TRIGGERcmd agent (enabled triggers only).

    Every entry carries a ``ground`` field. The agent only syncs commands whose
    ``ground`` matches its own run mode (agent.js: ``localcmds[l].ground == ground``),
    and a missing or mismatched field silently never appears on triggercmd.com.
    """
    block = get_block()
    triggers = triggers if triggers is not None else block.get("triggers", [])
    computer = computer or block.get("computer", "")
    ground, _ = resolve_ground()
    commands = []
    for t in triggers:
        if not isinstance(t, dict) or not t.get("enabled", True):
            continue
        entry = {
            "trigger": str(t.get("phrase", "")).strip(),
            "command": build_command(t),
            "computer": computer,
            "ground": ground,
        }
        if t.get("bridge"):
            entry["allowParams"] = True
        desc = mcp_tool_description(t)
        if desc:
            entry["mcpToolDescription"] = desc
        commands.append(entry)
    return commands


def commands_json_path() -> Path:
    return Path(get_block().get("agent_dir", "")) / "commands.json"


def write_commands_json() -> tuple[bool, str, int]:
    """Write commands.json to the agent dir (backing up any existing file)."""
    block = get_block()
    commands = generate_commands_json(block.get("triggers", []), block.get("computer", ""))
    target = commands_json_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            bak = target.with_suffix(".json.bak")
            shutil.copy2(target, bak)
        target.write_text(
            json.dumps(commands, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:
        return False, f"Could not write {target}: {e}", 0
    return True, f"Wrote {len(commands)} command(s) to {target}", len(commands)


# ── IFTTT instructions ────────────────────────────────────────────────────────

def ifttt_webhook_url(trigger: dict, computer: str = "", token: str = "") -> str:
    """The POST URL for the IFTTT Webhooks action of a single trigger."""
    block = get_block()
    computer = computer or block.get("computer", "")
    trigger_name = str(trigger.get("phrase", "")).strip()
    url = f"https://www.triggercmd.com/api/IFTTT?trigger={trigger_name}&computer={computer}"
    if trigger.get("bridge"):
        url += "&params={{TextField}}"
    return url


def instructions() -> str:
    """Plain-English setup guide - free paths first, IFTTT last.

    IFTTT now requires a paid subscription, so the recommended options are
    the TRIGGERcmd Smart Home Google Assistant action (Option A) and the
    built-in TRIGGERcmd MCP server (Option B). IFTTT webhooks (Option C)
    still work for anyone who already pays for IFTTT."""
    block = get_block()
    lines = [
        "HOW TO TRIGGER JEEVES - FREE FIRST (IFTTT is now PAID)",
        "------------------------------------------------------",
        "",
        "OPTION A (recommended, FREE): Google Assistant Smart Home action",
        "  The 'TRIGGERcmd Smart Home' action matches spoken words better",
        "  than the old conversational skill - no IFTTT, no subscription.",
        "",
        *smart_home_instructions().splitlines()[2:],  # skip its own title/divider
        "",
        "OPTION B (FREE, built into Jeeves): TRIGGERcmd MCP server",
        "  Jeeves' brain gets direct TRIGGERcmd tools (list_commands /",
        "  run_command) - say 'run <command> on my computer' or type it.",
        "  Run:  python voice_triggers.py --mcp-status   (check)",
        "        python voice_triggers.py --mcp-install  (download + configure)",
        "  Or press INSTALL MCP SERVER in the Voice Triggers panel.",
        "",
        "OPTION C (PAID, legacy): IFTTT webhooks",
        "  Only if you already have an IFTTT Pro subscription:",
        "  1. Create a free account at https://www.triggercmd.com",
        "  2. Install the TRIGGERcmd Agent on this PC (https://www.triggercmd.com/user/agent/setup)",
        "     and run it once so it registers this computer.",
        "  3. Open https://www.triggercmd.com/user/commands and check the commands are there.",
        "     If you clicked GENERATE, the triggers you enabled are already uploaded.",
        "  4. Go to https://ifttt.com and create an applet:",
        "     IF:  Google Assistant -> 'Say a phrase with a text ingredient'",
        "          Trigger phrase: e.g. 'tell jeeves $'   ($ = your words)",
        "     THEN: Webhooks -> Make a web request",
    ]
    for t in get_triggers():
        if not t.get("enabled", True):
            continue
        lines.append("")
        lines.append(f"     - For '{t.get('phrase')}':")
        lines.append(f"       POST {ifttt_webhook_url(t)}")
        if t.get("bridge"):
            lines.append("       Content-Type: application/x-www-form-urlencoded")
            lines.append(f"       Body: token={block.get('token') or '<your-token>'}")
            lines.append("       (the words after 'tell jeeves' become the command's $1)")
        else:
            lines.append(f"       Body: token={block.get('token') or '<your-token>'}")
    lines.append("")
    lines.append("5. Say: 'Hey Google, tell jeeves good morning' and enjoy.")
    lines.append("")
    lines.append("Free tier note: TRIGGERcmd allows 1 command per minute on free accounts.")
    lines.append("")
    lines.extend([
        "WHY COMMANDS MIGHT NOT SHOW UP ON TRIGGERCMD.COM",
        "------------------------------------------------",
        "The TRIGGERcmd Agent only uploads commands whose 'ground' field",
        "matches its own run mode (foreground = desktop app, background =",
        "daemon/service). GENERATE auto-detects your agent's mode from",
        "~/.TRIGGERcmdData/debug.log ('run foreground tasks' vs 'run",
        "background tasks') and stamps every command with it - so this",
        "should 'just work'. If a command is missing on the site:",
        "  1. Press GENERATE and check the status line - it shows the",
        "     detected mode, e.g. '(ground=foreground from agent debug.log)'.",
        "  2. Confirm the agent is running (look for TRIGGERcmdAgent.exe",
        "     in Task Manager) and that commands.json was written to the",
        "     same data dir the agent uses (default ~/.TRIGGERcmdData).",
        "  3. Force a mode: python voice_triggers.py --generate",
        "     --ground background   (or --ground foreground).",
        "  4. The agent syncs within seconds of the file changing - no",
        "     restart needed. If it still doesn't, check its debug.log",
        "     for 'Added <name>' lines and any 'Failed' errors.",
    ])
    return "\n".join(lines)


# ── Composio linking (use the TRIGGERcmd account you already have in Composio) ─
# The same triggercmd.com account can be reached two ways:
#   * IFTTT -> triggercmd cloud -> local agent  (the commands.json path above)
#   * Composio TRIGGERCMD toolkit -> triggercmd cloud -> local agent
# Linking through Composio reuses an OAuth connection you may already have
# set up, and lets Jeeves run triggers / list computers via the Composio API
# instead of requiring the IFTTT webhook.

def _composio_toolset():
    """Lazy-import the Composio shim; returns None when unavailable."""
    try:
        from composio_shim import ComposioToolSet, _load_composio_credentials
        ts = ComposioToolSet()
        return ts, _load_composio_credentials()
    except Exception:
        return None, ("", "")


def _composio_connected() -> tuple[bool, str]:
    """(connected, account_id_or_reason) for the TRIGGERcmd toolkit.

    Only ACTIVE connections count as linked -- an INITIATED account (OAuth
    started but not finished) would otherwise report "connected" and then
    fail every tool call with 422."""
    ts, (api_key, user_id) = _composio_toolset()
    if ts is None or ts._composio_instance is None:
        return False, "Composio SDK not available"
    try:
        accounts = ts._get_connected_accounts(user_id or "default")
        account_id = accounts.get("triggercmd")
        if not account_id:
            return False, "No TRIGGERcmd connection in Composio"
        # Confirm the connection is ACTIVE, not just initiated.
        listing = ts._composio_instance.connected_accounts.list(
            user_ids=[user_id or "default"]
        )
        for item in getattr(listing, "items", None) or []:
            if str(getattr(item, "id", "") or "") == account_id:
                status = str(getattr(item, "status", "") or "").upper()
                if status == "ACTIVE":
                    return True, account_id
                return False, f"TRIGGERcmd connection is {status} - finish the browser authorization"
        return False, "TRIGGERcmd connection not found"
    except Exception as e:
        return False, f"Could not check Composio: {e}"


def composio_status() -> dict:
    """Human-readable status of the Composio <-> TRIGGERcmd link."""
    connected, account = _composio_connected()
    if connected:
        computers = list_composio_computers()
        return {
            "connected": True,
            "account": account,
            "computers": computers,
            "message": f"Linked via Composio. Computers: {', '.join(computers) or 'none registered'}.",
        }
    return {"connected": False, "message": f"Not linked via Composio ({account})."}


def link_from_composio() -> tuple[bool, str]:
    """Start the Composio OAuth flow for TRIGGERCMD (opens browser).

    Returns (opened, message). Callers should tell the user to authorize in
    the browser, then re-check with composio_status()."""
    ts, (api_key, user_id) = _composio_toolset()
    if ts is None or ts._composio_instance is None:
        return False, "Composio SDK not available - install composio-core / composio-openai first."
    try:
        # Already ACTIVE? Nothing to do.
        connected, _ = _composio_connected()
        if connected:
            return True, "TRIGGERcmd is already linked via Composio."

        # A pending (INITIATED) authorization already exists -> point the
        # user at it instead of creating yet another duplicate link.
        pending_url = _pending_composio_link_url()
        if pending_url:
            import webbrowser
            webbrowser.open(pending_url)
            return True, ("A TRIGGERcmd authorization is already waiting - "
                          "browser opened. Finish it, then press LINK FROM "
                          "COMPOSIO again to confirm.")

        req = ts.initiate_connection("triggercmd")
        if isinstance(req, dict):
            url = req.get("redirectUrl") or req.get("redirect_url")
        else:
            url = getattr(req, "redirectUrl", None) or getattr(req, "redirect_url", None)
        if not url:
            return False, "Composio did not return an authorization URL."
        import webbrowser
        webbrowser.open(str(url))
        return True, ("Browser opened - authorize TRIGGERcmd in Composio, "
                      "then press LINK FROM COMPOSIO again to confirm.")
    except Exception as e:
        return False, f"Could not start Composio link: {e}"


def _pending_composio_link_url() -> str:
    """A reusable authorization URL for an existing INITIATED connection.

    Returns '' when there is no pending TRIGGERCMD authorization (or the
    SDK can't provide one). Never raises."""
    ts, (api_key, user_id) = _composio_toolset()
    if ts is None or ts._composio_instance is None:
        return ""
    try:
        listing = ts._composio_instance.connected_accounts.list(
            user_ids=[user_id or "default"]
        )
        for item in getattr(listing, "items", None) or []:
            tk = getattr(getattr(item, "toolkit", None), "slug", "") or ""
            if tk.lower() != "triggercmd":
                continue
            if str(getattr(item, "status", "") or "").upper() != "INITIATED":
                continue
            link = getattr(item, "link", None) or getattr(item, "connection_link", None)
            url = None
            if isinstance(link, dict):
                url = link.get("url") or link.get("link")
            elif link:
                url = str(link)
            if url:
                return url
    except Exception:
        pass
    return ""


def _composio_execute(slug: str, arguments: dict) -> dict:
    """Execute a Composio tool for the TRIGGERcmd account (never raises)."""
    ts, (api_key, user_id) = _composio_toolset()
    if ts is None or ts._composio_instance is None:
        return {"error": "Composio SDK not available"}
    connected, account_id = _composio_connected()
    if not connected:
        return {"error": "TRIGGERcmd not connected via Composio yet"}
    try:
        result = ts._composio_instance.tools.execute(
            slug=slug,
            arguments=arguments,
            user_id=user_id or "default",
            connected_account_id=account_id,
            dangerously_skip_version_check=True,
        )
        if hasattr(result, "model_dump"):
            result = result.model_dump()
        return result if isinstance(result, dict) else {"data": result}
    except Exception as e:
        return {"error": str(e)}


def list_composio_computers() -> list[str]:
    """Computer names registered in the Composio-linked TRIGGERcmd account."""
    result = _composio_execute("TRIGGERCMD_LIST_COMPUTERS", {})
    if result.get("error"):
        return []
    data = result.get("data", result)
    names: list[str] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                name = item.get("name") or item.get("computer") or item.get("computerName")
                if name:
                    names.append(str(name))
            elif item:
                names.append(str(item))
    return names


def save_composio_computer(name: str) -> None:
    """Save a Composio-listed computer name into the triggercmd block."""
    block = get_block()
    if name and name.strip():
        block["computer"] = name.strip()
    block["composio_linked"] = True
    save_block(block)


def run_via_composio(phrase: str, params: str = "") -> str:
    """Trigger a command through the Composio TRIGGERCMD toolkit.

    This is the Composio-cloud path (no IFTTT needed). Returns the tool
    result text. Requires a connected TRIGGERcmd account in Composio AND a
    matching command name registered at triggercmd.com."""
    block = get_block()
    computer = block.get("computer", "")
    arguments = {"command": phrase, "computer": computer}
    if params:
        arguments["params"] = params
    result = _composio_execute("TRIGGERCMD_TRIGGER_COMMAND", arguments)
    if result.get("error"):
        return f"Composio run failed: {result['error']}"
    return json.dumps(result, default=str)[:800]


# ── TRIGGERcmd MCP server (free AI control - no IFTTT) ──────────────────────
# Official stdio MCP server: https://github.com/rvmey/triggercmd-mcp-stdio
# The server exposes list_commands + run_command (and one dynamic tool per
# command that has an mcpToolDescription set). Jeeves' own MCP client
# (mcp_client.py) speaks stdio, so one entry in config "mcp_servers" gives
# the brain direct TRIGGERcmd control - including by voice.

MCP_REPO = "rvmey/triggercmd-mcp-stdio"
MCP_README_URL = f"https://github.com/{MCP_REPO}"
MCP_BINARY_DIR = BASE_DIR / "bin"
MCP_ENTRY_NAME = "triggercmd"


def _platform_mcp_name() -> str:
    """Binary filename for this OS/arch (matches the repo's Downloads list)."""
    sys_ = sys.platform.lower()
    if sys_.startswith("win"):
        arch = (os.environ.get("PROCESSOR_ARCHITECTURE", "") + os.environ.get("PROCESSOR_ARCHITEW6432", "")).lower()
        if "arm64" in arch:
            return "windows-arm64.exe"
        if "x86" in arch and "64" not in arch:
            return "windows-386.exe"
        return "windows-amd64.exe"
    if sys_.startswith("darwin"):
        return "darwin-arm64" if platform.machine().lower() in ("arm64", "aarch64") else "darwin-amd64"
    arch = platform.machine().lower()
    return {
        "x86_64": "linux-amd64", "amd64": "linux-amd64",
        "i386": "linux-386", "i686": "linux-386",
        "aarch64": "linux-arm64", "arm64": "linux-arm64",
        "armv7l": "linux-arm", "armv6l": "linux-arm",
    }.get(arch, "linux-amd64")


def mcp_binary_path() -> Path:
    """Path of the platform MCP binary in bin/.

    Accepts both naming styles used by the repo: 'triggercmd-mcp-windows-amd64.exe'
    (README examples) and 'windows-amd64.exe' (Downloads list)."""
    name = _platform_mcp_name()
    prefixed = MCP_BINARY_DIR / f"triggercmd-mcp-{name}"
    try:
        if prefixed.exists() and prefixed.stat().st_size > 0:
            return prefixed
    except Exception:
        pass
    return MCP_BINARY_DIR / name


def mcp_binary_installed() -> bool:
    p = mcp_binary_path()
    try:
        return p.exists() and p.stat().st_size > 0
    except Exception:
        return False


def mcp_download_urls() -> list[str]:
    """Candidate raw URLs for the binary (repo has no tagged releases)."""
    name = _platform_mcp_name()
    base = f"https://raw.githubusercontent.com/{MCP_REPO}/main"
    return [f"{base}/{name}", f"{base}/bin/{name}", f"{base}/mcp/{name}"]


def download_mcp_binary(timeout: int = 30) -> tuple[bool, str]:
    """Download the TRIGGERcmd stdio MCP binary into bin/.

    Tries the raw GitHub paths for this platform with a short per-URL timeout
    so a slow network can never freeze the UI. If every candidate fails (the
    repo hosts the files without releases), return the README URL so the user
    can grab the file manually and drop it in bin/ - retry afterwards.
    """
    if mcp_binary_installed():
        return True, f"MCP binary already present: {mcp_binary_path()}"
    import urllib.request

    target = mcp_binary_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    for url in mcp_download_urls():
        tmp = target.with_suffix(target.suffix + ".part")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Jeeves/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as fh:
                shutil.copyfileobj(resp, fh)
            if tmp.stat().st_size > 0:
                tmp.replace(target)
                if not sys.platform.startswith("win"):
                    target.chmod(0o755)
                return True, f"Downloaded MCP server -> {target}"
        except Exception as e:  # noqa: BLE001 - try the next candidate
            errors.append(f"{url}: {type(e).__name__}")
        finally:
            tmp.unlink(missing_ok=True)
    return False, (
        "Could not auto-download the TRIGGERcmd MCP server (the repo hosts the "
        f"binaries as raw files and they are unreachable right now).\nOpen "
        f"{MCP_README_URL}, download '{_platform_mcp_name()}' and drop it into "
        f"{MCP_BINARY_DIR}, then run install again.\n\n" + "\n".join(errors[:4])
    )


def mcp_server_spec() -> dict:
    """The config/api_keys.json -> mcp_servers entry (stdio transport).

    The binary reads TRIGGERCMD_TOKEN from the environment; when no token is
    configured it falls back to ~/.TRIGGERcmdData/token.tkn on its own."""
    spec = {
        "name": MCP_ENTRY_NAME,
        "transport": "stdio",
        "command": str(mcp_binary_path()),
    }
    token = str(get_block().get("token", "")).strip()
    if token:
        spec["env"] = {"TRIGGERCMD_TOKEN": token}
    return spec


def _config_mcp_servers() -> list[dict]:
    cfg = _load_cfg()
    servers = cfg.get("mcp_servers") or []
    if isinstance(servers, dict):
        servers = list(servers.values())
    return cfg, [s for s in servers if isinstance(s, dict)]


def ensure_mcp_configured() -> tuple[bool, str]:
    """Download the binary (if needed) and (re)write the mcp_servers entry.

    Returns (ok, message). The entry is picked up by mcp_client.py on the
    next Jeeves start - or immediately by MCPClientManager.refresh()."""
    if not mcp_binary_installed():
        ok, msg = download_mcp_binary()
        if not ok:
            return False, msg
    cfg, servers = _config_mcp_servers()
    spec = mcp_server_spec()
    replaced = False
    for i, s in enumerate(servers):
        if str(s.get("name", "")).lower() == MCP_ENTRY_NAME:
            servers[i] = spec
            replaced = True
            break
    if not replaced:
        servers.append(spec)
    cfg["mcp_servers"] = servers
    _save_cfg(cfg)
    msg = f"MCP server configured -> {spec['command']}"
    if not spec.get("env"):
        msg += " (no token in config - the binary will use ~/.TRIGGERcmdData/token.tkn)"
    msg += "\nRestart Jeeves (or refresh MCP) so the brain picks up the new server."
    return True, msg


def remove_mcp_config() -> tuple[bool, str]:
    """Remove the triggercmd entry from mcp_servers (binary stays in bin/)."""
    cfg, servers = _config_mcp_servers()
    kept = [s for s in servers if str(s.get("name", "")).lower() != MCP_ENTRY_NAME]
    cfg["mcp_servers"] = kept
    _save_cfg(cfg)
    return True, "Removed 'triggercmd' from mcp_servers (binary kept in bin/)."


def mcp_status() -> dict:
    """Human-readable status of the TRIGGERcmd MCP setup. Never raises."""
    installed = mcp_binary_installed()
    _, servers = _config_mcp_servers()
    configured = any(
        isinstance(s, dict) and str(s.get("name", "")).lower() == MCP_ENTRY_NAME
        for s in servers
    )
    token = str(get_block().get("token", "")).strip()
    mcp_pkg = False
    try:
        import mcp  # noqa: F401
        mcp_pkg = True
    except Exception:
        pass
    problems: list[str] = []
    if not mcp_pkg:
        problems.append("'mcp' python package missing (pip install mcp)")
    if not installed:
        problems.append(f"binary missing: {mcp_binary_path()}")
    if not configured:
        problems.append("no 'triggercmd' entry in config mcp_servers (run install)")
    if not token:
        problems.append("no token in config (binary will fall back to ~/.TRIGGERcmdData/token.tkn)")
    ok = mcp_pkg and installed and configured
    return {
        "ok": ok,
        "mcp_package": mcp_pkg,
        "binary_installed": installed,
        "configured": configured,
        "token_set": bool(token),
        "binary_path": str(mcp_binary_path()),
        "problems": problems,
        "message": "OK - Jeeves' brain can run TRIGGERcmd commands" if ok
                   else "; ".join(problems) or "unknown",
    }


# ── Free setup guide (Google Assistant without IFTTT) ────────────────────────

def smart_home_instructions() -> str:
    """The FREE recommended path: TRIGGERcmd Smart Home Google Assistant action.

    No IFTTT, no subscription. The smart-home action matches spoken words
    better than the old conversational skill ("Hey Google, turn on X")."""
    lines = [
        "CONTROL JEEVES FROM GOOGLE ASSISTANT - FREE (no IFTTT)",
        "------------------------------------------------------",
        "1. Create a free account at https://www.triggercmd.com",
        "2. Install the TRIGGERcmd Agent on this PC:",
        "   https://www.triggercmd.com/user/agent/setup",
        "   Run it once so it registers this computer.",
        "3. In Jeeves: press GENERATE commands.json (above) so the enabled",
        "   triggers are uploaded to your account.",
        "4. Link the Smart Home action (official steps):",
        "   - Open the Google Home app",
        "   - Tap the Settings tab -> Google Assistant",
        "   - Tap 'Manage all Assistant settings' -> Home control",
        "   - Search for TRIGGERcmd -> select 'TRIGGERcmd Smart Home'",
        "   - Continue -> log in to your TRIGGERcmd account -> Allow",
        "5. Say it: 'Hey Google, turn on <trigger>'.",
        "   Example: 'Hey Google, turn on status report' or",
        "            'Hey Google, turn on good morning'.",
        "",
        "Tips:",
        "  - Each command appears as a smart-home switch, so the phrase is",
        "    always 'turn on <name>' / 'turn off <name>'.",
        "  - The Smart Home action matches your words to command names",
        "    (and aliases) - name triggers so they read naturally.",
        "  - Avoid awkward device names: apostrophes or symbols in a trigger",
        "    name can confuse device discovery - prefer plain words.",
        "  - After linking, give it a minute for devices to appear; if they",
        "    don't show up, unlink and link again.",
        "  - Free tier: TRIGGERcmd allows 1 command per minute.",
    ]
    return "\n".join(lines)


# ── Account lookups (never raise) ────────────────────────────────────────────

def fetch_computers() -> list[dict]:
    """Query the TRIGGERcmd API for computers on this account.

    Returns [{"name", "id", "connected"}, ...]. The registered computer NAME
    (not the Windows hostname) is what the cloud trigger API requires - e.g.
    /api/run/triggerSave with computer=<registered name>. Empty on failure.
    """
    token = str(get_block().get("token", "")).strip()
    if not token:
        return []
    import ssl
    import urllib.request

    def _call(ctx=None):
        req = urllib.request.Request(
            "https://www.triggercmd.com/api/computer/list",
            headers={"Authorization": f"Bearer {token}", "User-Agent": "Jeeves/1.0"},
        )
        with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    try:
        data = _call()
    except Exception:
        # Some Windows builds ship a CA bundle that rejects triggercmd.com's
        # chain ("Basic Constraints of CA cert not marked critical") while
        # browsers/curl work fine - fall back to unverified for this one call.
        try:
            data = _call(ssl._create_unverified_context())
        except Exception:
            return []
    records = data.get("records", []) if isinstance(data, dict) else []
    out: list[dict] = []
    for r in records:
        if isinstance(r, dict):
            out.append({
                "name": str(r.get("name", "") or ""),
                "id": str(r.get("id", "") or ""),
                "connected": bool(r.get("connected", False)),
            })
    return out


# ── Local test (proves the exact command the agent will run) ─────────────────

def test_run(phrase: str, timeout: int = 120) -> str:
    """Run the command for a trigger locally (calls the jeeves CLI)."""
    trigger = next((t for t in get_triggers()
                    if t.get("phrase", "").strip().lower() == phrase.strip().lower()), None)
    if not trigger:
        return f"No trigger named '{phrase}'."
    command = build_command(trigger)
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout}s:\n{command}"
    out = (result.stdout or "").strip()[-800:] or (result.stderr or "").strip()[-800:]
    return f"Ran: {command}\n\n{out}" if out else f"Ran: {command} (no output)"


# ── CLI entry ─────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Jeeves voice triggers (TRIGGERcmd bridge)")
    parser.add_argument("--list", action="store_true", help="list configured triggers")
    parser.add_argument("--generate", action="store_true", help="write commands.json")
    parser.add_argument("--ground", metavar="MODE", default="",
                        help="agent run mode for generated commands: background (default) or foreground")
    parser.add_argument("--instructions", action="store_true",
                        help="print setup guide (free paths first, IFTTT last)")
    parser.add_argument("--test", metavar="PHRASE", help="run a trigger command locally")
    parser.add_argument("--composio-status", action="store_true",
                        help="show Composio <-> TRIGGERcmd link status")
    parser.add_argument("--composio-link", action="store_true",
                        help="start the Composio OAuth flow for TRIGGERcmd")
    parser.add_argument("--composio-run", metavar="PHRASE",
                        help="trigger a command via the Composio TRIGGERcmd toolkit")
    parser.add_argument("--mcp-status", action="store_true",
                        help="show TRIGGERcmd MCP server status")
    parser.add_argument("--mcp-install", action="store_true",
                        help="download + configure the TRIGGERcmd MCP server (free, no IFTTT)")
    parser.add_argument("--mcp-remove", action="store_true",
                        help="remove the triggercmd entry from mcp_servers")
    parser.add_argument("--computers", action="store_true",
                        help="list computers registered on your TRIGGERcmd account")
    args = parser.parse_args()

    if args.list:
        for t in get_triggers():
            state = "ON " if t.get("enabled", True) else "OFF"
            print(f"[{state}] {t.get('phrase')}  ->  {build_command(t)}")
        return 0
    if args.generate:
        if args.ground:
            block = get_block()
            block["ground"] = args.ground.strip().lower()
            save_block(block)
        ground, source = resolve_ground()
        ok, msg, count = write_commands_json()
        print(msg)
        print(f"ground={ground} ({source})")
        return 0 if ok else 1
    if args.instructions:
        print(instructions())
        return 0
    if args.test:
        print(test_run(args.test))
        return 0
    if args.composio_status:
        print(json.dumps(composio_status(), indent=2, default=str))
        return 0
    if args.composio_link:
        ok, msg = link_from_composio()
        print(msg)
        return 0 if ok else 1
    if args.composio_run:
        print(run_via_composio(args.composio_run))
        return 0
    if args.mcp_status:
        print(json.dumps(mcp_status(), indent=2, default=str))
        return 0
    if args.mcp_install:
        ok, msg = ensure_mcp_configured()
        print(msg)
        return 0 if ok else 1
    if args.mcp_remove:
        ok, msg = remove_mcp_config()
        print(msg)
        return 0 if ok else 1
    if args.computers:
        computers = fetch_computers()
        if not computers:
            print("No computers found - is the token correct and the agent installed?")
        for c in computers:
            state = "connected" if c["connected"] else "offline"
            print(f"{c['name']}  ({c['id']})  [{state}]")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
