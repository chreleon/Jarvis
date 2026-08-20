#!/usr/bin/env python3
"""
cli.py — Upgraded terminal interface for MARK XXXIX-OR (Jeeves).

This is the primary entry point for the Jeeves CLI. It can be run directly
or through the npm wrapper (`jeeves` via cli.js).

A full-featured CLI with:
  • 5 interaction modes: chat, agent, tools, memory, help
  • ANSI-colored output (zero dependencies)
  • Persistent command history (~/.jeeves_history)
  • Conversation memory integration (loads long-term memory from memory/)
  • Proper system prompt with date/time context (like main.py)
  • Save/load conversation sessions as JSON
  • Tool listing with descriptions
  • Multi-line input (use backslash at end of line to continue)
  • File attachment for processing
  • Auto-retry on API errors with fallback models

Run:
    chmod +x cli.py && ./cli.py
    python cli.py                  # interactive REPL (chat mode)
    python cli.py -c "ask something"   # single-shot
    python cli.py -m agent         # start in agent mode
    python cli.py --tools          # list all available tools and exit
    python cli.py --tool open_app --args '{"app_name": "Notepad"}'  # direct tool call (no LLM)
    python cli.py --daemon         # warm, persistent daemon for fast spawning
    python cli.py --send "open notepad"   # send to daemon (auto-starts it)
    python cli.py --help           # show full help
    jeeves --help                  # via npm wrapper
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import socket
import subprocess
import sys

# Ensure UTF-8 encoding for stdout on Windows (handles emoji in modern terminals)
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

# ── ANSI color helpers (zero dependencies) ──────────────────────────────────

class Style:
    """Terminal styling with ANSI escape codes. Falls back to plain text if
    the output stream doesn't support it."""
    _ENABLED = sys.stdout.isatty()

    # Base colors
    CYAN = "\033[36m" if _ENABLED else ""
    GREEN = "\033[32m" if _ENABLED else ""
    YELLOW = "\033[33m" if _ENABLED else ""
    RED = "\033[31m" if _ENABLED else ""
    MAGENTA = "\033[35m" if _ENABLED else ""
    BLUE = "\033[34m" if _ENABLED else ""
    WHITE = "\033[37m" if _ENABLED else ""
    GRAY = "\033[90m" if _ENABLED else ""
    BRIGHT_CYAN = "\033[96m" if _ENABLED else ""
    BRIGHT_GREEN = "\033[92m" if _ENABLED else ""
    BRIGHT_YELLOW = "\033[93m" if _ENABLED else ""
    BRIGHT_RED = "\033[91m" if _ENABLED else ""
    BRIGHT_MAGENTA = "\033[95m" if _ENABLED else ""

    # Text styles
    BOLD = "\033[1m" if _ENABLED else ""
    DIM = "\033[2m" if _ENABLED else ""
    ITALIC = "\033[3m" if _ENABLED else ""
    UNDERLINE = "\033[4m" if _ENABLED else ""
    RESET = "\033[0m" if _ENABLED else ""


# ── Constants ───────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
MEMORY_PATH = BASE_DIR / "memory" / "long_term.json"
CORE_PROMPT_PATH = BASE_DIR / "core" / "prompt.txt"
HISTORY_FILE = Path.home() / ".jeeves_history"
SESSION_DIR = BASE_DIR / "sessions"

# ── Conversation context budget (mirrors main.py) ─────────────────────────
# Unbounded history + verbatim tool results pushed requests past the
# free-tier per-minute token budget (413 "Payload Too Large" failures).
# These caps keep every brain request small and cheap.
# Context budget constants imported from core.context (canonical source).
# HISTORY_WINDOW_TURNS, MAX_MSG_CHARS, MAX_HISTORY_CHARS, TOOL_RESULT_CHARS
# are now defined in core/context.py and imported at the top of this file.

# ── Agentic loop settings (inspired by Claude Code) ──────────────────────
# Claude Code chains multiple tool calls before responding. We replicate
# this so the LLM can call tools in sequence (e.g. search → read file →
# edit) without requiring the user to prompt between each step.
AGENTIC_MAX_STEPS    = 8     # max tool calls per user turn before forced reply
AGENTIC_STEP_TIMEOUT = 60    # seconds per tool call in the loop
_AUTO_APPROVE        = False  # session-wide toggle (via /approve command)

# ── Permission system (Claude Code-style safety gates) ────────────────────
# Dangerous operations require user confirmation before execution.
# Patterns matched against tool name + args to decide risk level.
_DANGEROUS_TOOL_PATTERNS: list[tuple[str, list[str]]] = [
    # (tool_name, [dangerous_action_values])
    ("cmd_control",     []),  # all cmd_control calls need approval
    ("file_controller", ["delete"]),
    ("computer_settings", ["restart", "shutdown", "sleep", "hibernate"]),
    ("game_updater",    ["install"]),
    ("phone_control",   ["shell", "stop"]),
]

# Tools that are ALWAYS safe (never ask permission)
_ALWAYS_SAFE_TOOLS: set[str] = {
    "open_app", "web_search", "system_status", "manage_monitor",
    "weather_report", "reminder", "youtube_video", "screen_process",
    "browser_control", "file_controller",  # except delete (handled above)
    "computer_control", "computer_settings",  # except restart/shutdown
    "desktop_control", "code_helper",
    "daily_briefing", "anime_watch",
    "send_message", "save_memory", "meta_ai", "flight_finder",
    "file_processor", "phone_control",  # most actions safe
    "secretary",  # except shell/stop (handled above)
}

# ── Cost tracking (approximate, based on Groq free-tier pricing) ────────
_INPUT_COST_PER_1K  = 0.0    # Groq free tier: $0 input
_OUTPUT_COST_PER_1K = 0.0    # Groq free tier: $0 output
_estimated_tokens_in: int = 0
_estimated_tokens_out: int = 0
_total_tool_calls: int = 0

# ── Console Player (adapter for action modules) ─────────────────────────────

class ConsolePlayer:
    """Drop-in adapter that satisfies the 'player' interface expected by
    all Jeeves action modules (open_app, web_search, etc.)."""

    def __init__(self) -> None:
        self._muted = False
        self._state: str = "IDLE"
        self._file_path: str | None = None

    @property
    def muted(self) -> bool:
        return self._muted

    @muted.setter
    def muted(self, v: bool) -> None:
        self._muted = bool(v)

    @property
    def current_file(self) -> str | None:
        return self._file_path

    @current_file.setter
    def current_file(self, path: str | None) -> None:
        self._file_path = path

    def write_log(self, text: str) -> None:
        """Writes a log line — the CLI equivalent of the GUI log panel."""
        print(f"{Style.GRAY}[LOG]{Style.RESET} {text}")

    def set_state(self, state: str) -> None:
        """Called by action modules to indicate state changes."""
        self._state = state
        if state == "LISTENING":
            pass  # In CLI mode we don't flash states constantly
        elif state == "THINKING" and sys.stdout.isatty():
            sys.stdout.write(f"\r{Style.YELLOW}🧠 Thinking...{Style.RESET}")
            sys.stdout.flush()
        elif state == "SPEAKING":
            # TTS is not used in CLI mode; silently ignore
            pass

    def wait_for_api_key(self) -> None:
        """No setup screen in CLI — assume config is present."""
        return


# ── Lazy Runtime Imports ────────────────────────────────────────────────────

_RUNTIME_IMPORTS: dict[str, Any] | None = None


def _load_runtime_imports() -> dict[str, Any]:
    """Lazily import heavy modules and action handlers on first use, matching
    the same pattern used in main.py."""
    global _RUNTIME_IMPORTS
    if _RUNTIME_IMPORTS is None:
        # NOTE: heavy modules are intentionally NOT imported here to keep
        # one-shot / daemon spawns fast:
        #   • actions.screen_processor  (~14s) → lazy in _call_tool
        #   • composio_agent            (~10s) → lazy via _get_composio_agent()
        from actions.file_processor import file_processor
        from actions.flight_finder import flight_finder
        from actions.open_app import open_app
        from actions.weather_report import weather_action
        from actions.send_message import send_message
        from actions.reminder import reminder
        from actions.computer_settings import computer_settings
        from actions.youtube_video import youtube_video
        from actions.desktop import desktop_control
        from actions.browser_control import browser_control
        from actions.file_controller import file_controller
        from actions.code_helper import code_helper
        from actions.dev_agent import dev_agent
        from actions.web_search import web_search as web_search_action
        from actions.computer_control import computer_control
        from actions.cmd_control import cmd_control
        from actions.game_updater import game_updater
        from actions.system_monitor import system_status
        from actions.background_monitor import (
            add_monitor, remove_monitor, list_monitors,
        )
        from actions.business_tracker import business_tracker
        from actions.daily_briefing import daily_briefing
        from actions.anime_watch import anime_watch
        from actions.secretary import secretary
        from actions.meta_ai import meta_ai
        from actions.phone_control import phone_control

        from memory.memory_manager import (
            load_memory, update_memory, format_memory_for_prompt,
            should_extract_memory, extract_memory,
        )

        _RUNTIME_IMPORTS = {
            "file_processor": file_processor,
            "flight_finder": flight_finder,
            "open_app": open_app,
            "weather_action": weather_action,
            "send_message": send_message,
            "reminder": reminder,
            "computer_settings": computer_settings,
            "youtube_video": youtube_video,
            "desktop_control": desktop_control,
            "browser_control": browser_control,
            "file_controller": file_controller,
            "code_helper": code_helper,
            "dev_agent": dev_agent,
            "web_search_action": web_search_action,
            "computer_control": computer_control,
            "cmd_control": cmd_control,
            "game_updater": game_updater,
            "system_status": system_status,
            "add_monitor": add_monitor,
            "remove_monitor": remove_monitor,
            "list_monitors": list_monitors,
            "business_tracker": business_tracker,
            "daily_briefing": daily_briefing,
            "anime_watch": anime_watch,
            "secretary": secretary,
            "meta_ai": meta_ai,
            "phone_control": phone_control,
            "run_agentic_task": None,
            "load_memory": load_memory,
            "update_memory": update_memory,
            "format_memory_for_prompt": format_memory_for_prompt,
            "should_extract_memory": should_extract_memory,
            "extract_memory": extract_memory,
        }
    return _RUNTIME_IMPORTS


def _get_composio_agent():
    """Lazily import the Composio agent.

    Importing composio_agent pulls in the full composio SDK (~10s on this
    machine), so it is deferred until agent-mode work is actually requested.
    Returns the callable, or None if unavailable.
    """
    imports = _load_runtime_imports()
    if imports.get("run_agentic_task") is None:
        try:
            from composio_agent import run_agentic_task
            imports["run_agentic_task"] = run_agentic_task
        except Exception as e:
            print(f"{Style.DIM}[Composio agent unavailable: {e}]{Style.RESET}")
    return imports.get("run_agentic_task")


def _get_brain_client():
    """Import and return the LLM client lazily."""
    try:
        from or_client import client as brain_client
        return brain_client
    except Exception as e:
        print(f"{Style.RED}Failed to import brain client: {e}{Style.RESET}")
        return None


# ── Tool Definitions ────────────────────────────────────────────────────────

from config.tool_definitions import TOOL_REGISTRY, compact_tool_declarations
from config.tool_tips import get_tool_tip, tool_tutorial
from core.utils import parse_tool_call
from core.context import trim_context, truncate_tool_result, HISTORY_WINDOW_TURNS, MAX_MSG_CHARS, MAX_HISTORY_CHARS, TOOL_RESULT_CHARS

# ── Graceful Shutdown ─────────────────────────────────────────────────────
_CLEANUP_REGISTERED = False


def _register_cleanup() -> None:
    """Register the cleanup handler once to ensure resources are freed
    on exit. Mirrors the pattern in main.py."""
    global _CLEANUP_REGISTERED
    if not _CLEANUP_REGISTERED:
        try:
            from memory_cleanup import cleanup as cleanup_jeeves
            import atexit
            atexit.register(cleanup_jeeves)
            _CLEANUP_REGISTERED = True
        except Exception as e:
            print(f"{Style.DIM}[Cleanup] Not registered: {e}{Style.RESET}")


# ── Memory Extraction (auto-learning from conversations) ────────────────────

_last_memory_input: str = ""

# While the daemon is running, background memory learning is suppressed: it
# fires extra LLM calls that collide with the active request on rate-limited
# tiers (observed 429 retry stalls of 7-28s on Groq's free tier). The daemon
# is the fast, spawnable-agent interface -- conversation state is kept in
# memory instead, and learning can be re-enabled by running the interactive
# REPL / main.py.
_DAEMON_MODE = False


def _update_memory_async(user_text: str, jeeves_text: str) -> None:
    """Background thread to extract personal facts and save to long-term memory.

    Mirrors the same pattern used in main.py so the CLI also learns
    from conversations over time.
    """
    global _last_memory_input

    user_text = (user_text or "").strip()
    jeeves_text = (jeeves_text or "").strip()

    if len(user_text) < 5 or user_text == _last_memory_input:
        return
    _last_memory_input = user_text

    try:
        imports = _load_runtime_imports()
        if not imports["should_extract_memory"](user_text, jeeves_text):
            return
        data = imports["extract_memory"](user_text, jeeves_text)
        if data:
            imports["update_memory"](data)
            print(f"[Memory] ✅ CLI remembered: {list(data.keys())}")
    except Exception as e:
        if "429" not in str(e):
            print(f"[Memory] ⚠️ {e}")


def _maybe_learn(user_text: str, jeeves_text: str) -> None:
    """Background memory extraction, skipped in daemon mode.

    Daemon-mode LLM calls collide with the active request on rate-limited
    tiers; conversation state is kept in memory there instead.
    """
    if len(user_text or "") > 5 and not _DAEMON_MODE:
        threading.Thread(
            target=_update_memory_async,
            args=(user_text, jeeves_text),
            daemon=True,
        ).start()


# ── System Prompt Builder ───────────────────────────────────────────────────

# Cached with an mtime check: _build_system_prompt() runs on EVERY LLM turn,
# so without caching each turn re-reads core/prompt.txt from disk. Editing
# the file bumps mtime → re-read happens automatically.
_core_prompt_cache: str | None = None
_core_prompt_mtime_ns: int = -1
_core_prompt_size: int = -1


def _load_core_prompt() -> str:
    global _core_prompt_cache, _core_prompt_mtime_ns, _core_prompt_size
    try:
        st = CORE_PROMPT_PATH.stat()
        if (_core_prompt_cache is not None
                and st.st_mtime_ns == _core_prompt_mtime_ns
                and st.st_size == _core_prompt_size):
            return _core_prompt_cache
        text = CORE_PROMPT_PATH.read_text(encoding="utf-8")
        _core_prompt_cache, _core_prompt_mtime_ns, _core_prompt_size = text, st.st_mtime_ns, st.st_size
        return text
    except Exception:
        return (
            "You are JEEVES, Tony Stark's AI assistant. "
            "Be concise, direct, and always use the provided tools to complete tasks. "
            "Never simulate or guess results — always call the appropriate tool."
        )


def _build_system_prompt() -> str:
    """Build a system prompt with time context, memory, and tool definitions,
    matching the approach used in main.py."""
    imports = _load_runtime_imports()
    mem_str = imports["format_memory_for_prompt"](imports["load_memory"]())

    now = datetime.now()
    time_str = now.strftime("%A, %B %d, %Y — %I:%M %p")
    time_ctx = (
        f"[CURRENT DATE & TIME]\n"
        f"Right now it is: {time_str}\n"
        f"Use this to calculate exact times for reminders.\n\n"
    )

    sys_prompt = _load_core_prompt()

    parts = [time_ctx]
    if mem_str:
        parts.append(mem_str)
    parts.append(sys_prompt)

    # Add tool declarations for the LLM (compact rendering — the full JSON
    # schema is ~6k tokens per request and pushed free-tier budgets past
    # their per-minute limit, causing 413 failures)
    parts.append(
        "\n[TOOLS]\nYou have tools available. To call one, respond with "
        'ONLY a JSON object of the form {"tool_call": {"name": "<tool_name>", "args": {...}}}. '
        "To just speak to the user, respond with plain text (no JSON). "
        "You may chain multiple tool calls in sequence before responding. "
        "Available tools:\n" + compact_tool_declarations()
    )

    # Vision-agent discipline: classify the task type and apply the right
    # approach (from the vision agent protocol). This helps the LLM choose
    # the right tools and respond with appropriate depth.
    parts.append(
        "\n[DISCIPLINE]\n"
        "Before acting, classify the task:\n"
        "  DEBUG — something broken: diagnose → reproduce → fix → verify\n"
        "  BUILD — new feature: design → implement → test\n"
        "  INFO — research/search: find → present clearly\n"
        "  CONTROL — system/phone action: call the right tool directly\n"
        "Apply the matching discipline. For DEBUG: read the actual files, "
        "identify root cause, propose minimal fix. For BUILD: confirm scope "
        "first if ambiguous. For INFO: search then summarize. For CONTROL: "
        "use the most direct tool, skip the LLM when shortcuts exist."
    )

    return "\n".join(parts)


# ── Tool Call Execution (parsing uses shared parse_tool_call from core/utils) ──

# Tools whose one-liner tip was already shown this session — each tool's
# usage hint prints once per session, then stays quiet so long pipelines
# don't spam.
# ── Permission system ────────────────────────────────────────────────────

def _needs_permission(name: str, args: dict) -> bool:
    """Return True if this tool call requires user approval.

    Claude Code asks before running dangerous operations. We replicate
    that safety gate: any tool matching _DANGEROUS_TOOL_PATTERNS needs
    a 'y' from the user before execution. Read-only / safe tools skip
    the prompt entirely.
    """
    name = (name or "").strip()
    # Check always-safe list first (fast path)
    if name in _ALWAYS_SAFE_TOOLS:
        # Even safe tools can have dangerous sub-actions
        for pattern_name, dangerous_actions in _DANGEROUS_TOOL_PATTERNS:
            if name == pattern_name and dangerous_actions:
                action = str(args.get("action", "")).lower().strip()
                if action in dangerous_actions:
                    return True
        return False
    # Not in the safe list → needs permission (unknown tools are dangerous)
    return True


def _ask_permission(name: str, args: dict) -> bool:
    """Ask the user to approve a dangerous tool call.

    Returns True if approved, False if denied. In non-interactive mode
    (daemon, -c), auto-approves to avoid blocking. Session-wide auto-approve
    can be toggled with the /approve command.
    """
    if _AUTO_APPROVE or not sys.stdout.isatty():
        return True  # auto-approve or non-interactive

    # Build a human-readable description of what will happen
    desc = _tool_description(name, args)
    print(f"\n{Style.BRIGHT_YELLOW}⚠️  Permission required:{Style.RESET} {Style.BOLD}{desc}{Style.RESET}")
    try:
        answer = input(f"{Style.DIM}Allow? [y/N]: {Style.RESET}").strip().lower()
    except (KeyboardInterrupt, EOFError):
        print()
        return False
    return answer in ("y", "yes", "ok")


def _tool_description(name: str, args: dict) -> str:
    """Build a short human-readable description of a tool call."""
    action = str(args.get("action", "")).strip()
    task = str(args.get("task", "")).strip()
    path = str(args.get("path", "")).strip()
    cmd = str(args.get("cmd", "")).strip()
    text = str(args.get("text", "")).strip()
    pkg = str(args.get("pkg", "")).strip()

    if name == "cmd_control":
        return f"Run system command: {task or text or '(unspecified)'}"
    if name == "file_controller" and action == "delete":
        return f"Delete file: {path}"
    if name == "computer_settings":
        return f"Computer setting: {action} {args.get('value', '')}"
    if name == "game_updater" and action == "install":
        return f"Install game: {args.get('game_name', 'unknown')}"
    if name == "phone_control":
        if action == "shell":
            return f"Phone shell: {cmd}"
        if action == "stop":
            return f"Force-stop app: {pkg}"
        return f"Phone: {action}"
    return f"{name}({action or task or path or cmd or text or '...'})"


_SEEN_TOOL_TIPS: set[str] = set()


def _maybe_show_tool_tip(name: str) -> None:
    """Print the one-liner usage tip for a tool the first time it's used.

    Skipped in daemon mode so the daemon's stdout stays clean (clients get
    their reply over the socket, not through console prints).
    """
    if _DAEMON_MODE or name in _SEEN_TOOL_TIPS:
        return
    _SEEN_TOOL_TIPS.add(name)
    tip = get_tool_tip(name)
    if tip:
        print(f"\n{Style.DIM}{tip}{Style.RESET}\n")


def _call_tool(name: str, args: dict, player: ConsolePlayer) -> str:
    """Execute a tool by name with the given arguments."""
    imports = _load_runtime_imports()
    name = name or ""
    _maybe_show_tool_tip(name)

    # ── Permission gate (Claude Code-style safety) ──
    if not _DAEMON_MODE and _needs_permission(name, args):
        if not _ask_permission(name, args):
            return "❌ Permission denied."

    # ── save_memory is handled inline ──
    if name == "save_memory":
        category = args.get("category", "notes")
        key = args.get("key", "")
        value = args.get("value", "")
        if key and value:
            imports["update_memory"]({category: {key: {"value": value}}})
            return f"💾 Saved to memory: {category}/{key} = {value}"
        return "save_memory requires 'key' and 'value'."

    try:
        # ── Dispatch ──
        dispatch = {
            "open_app": lambda: imports["open_app"](parameters=args, player=player),
            "weather_report": lambda: imports["weather_action"](parameters=args, player=player),
            "browser_control": lambda: imports["browser_control"](parameters=args, player=player),
            "file_controller": lambda: imports["file_controller"](parameters=args, player=player),
            "send_message": lambda: imports["send_message"](parameters=args, response=None, player=player, session_memory=None),
            "reminder": lambda: imports["reminder"](parameters=args, response=None, player=player),
            "youtube_video": lambda: imports["youtube_video"](parameters=args, response=None, player=player),
            "computer_settings": lambda: imports["computer_settings"](parameters=args, response=None, player=player),
            "desktop_control": lambda: imports["desktop_control"](parameters=args, player=player),
            "code_helper": lambda: imports["code_helper"](parameters=args, player=player),
            "dev_agent": lambda: imports["dev_agent"](parameters=args, player=player),
            "web_search": lambda: imports["web_search_action"](parameters=args, player=player),
            "system_status": lambda: imports["system_status"](parameters=args, player=player),
            "computer_control": lambda: imports["computer_control"](parameters=args, player=player),
            "cmd_control": lambda: imports["cmd_control"](parameters=args, player=player),
            "game_updater": lambda: imports["game_updater"](parameters=args, player=player),
            "flight_finder": lambda: imports["flight_finder"](parameters=args, player=player),
            "file_processor": lambda: imports["file_processor"](parameters=args, player=player),
            "phone_control": lambda: imports["phone_control"](parameters=args, player=player),
        }

        if name in dispatch:
            result = dispatch[name]()
            return result or "Done."

        if name == "manage_monitor":
            action = str(args.get("action", "")).lower().strip()
            topic  = str(args.get("topic", "")).strip()
            if action == "add":
                return imports["add_monitor"](topic)
            if action == "remove":
                return imports["remove_monitor"](topic)
            topics = imports["list_monitors"]()
            return ("Monitoring: " + ", ".join(topics)) if topics \
                   else "No topics are being monitored."

        if name == "business_tracker":
            return imports["business_tracker"](parameters=args, player=player)

        if name == "daily_briefing":
            return imports["daily_briefing"](parameters=args, player=player)

        if name == "anime_watch":
            return imports["anime_watch"](parameters=args, player=player)

        if name == "secretary":
            return imports["secretary"](parameters=args, player=player)

        if name == "meta_ai":
            return imports["meta_ai"](parameters=args, player=player)

        if name == "screen_process":
            # Lazy import: actions.screen_processor costs ~14s to import.
            # The analysis runs inline so its TEXT result comes back here —
            # the remote WhatsApp dashboard needs the actual description,
            # not an "activated" stub. The live session still speaks the
            # answer out loud and prints progress as it goes.
            try:
                from actions.screen_processor import screen_process
            except Exception as e:
                print(f"{Style.RED}Vision module unavailable: {e}{Style.RESET}")
                return "Vision module unavailable."
            text = screen_process(parameters=args, player=player)
            return str(text).strip() if text else "Vision analysis failed."

        if name in ("composio_action", "agent_task"):
            run_agent = _get_composio_agent()
            if run_agent is None:
                return "Composio agent not available."
            req = args.get("request") or args.get("goal") or ""
            return run_agent(req) or "Done."

        if name == "shutdown_jeeves":
            _register_cleanup()
            raise SystemExit(0)

        return f"Unknown tool: {name}"

    except Exception as e:
        return f"{Style.RED}Tool '{name}' failed: {e}{Style.RESET}"


# ── Smart Shortcuts ────────────────────────────────────────────────────────
# Plain-language inputs that map straight to a tool, skipping the LLM:
# instant, free, deterministic, and they always work. `_try_shortcut`
# returns a reply when a shortcut matches, otherwise None so the normal
# brain flow takes over.
TIPS = [
    "⚡ Type naturally: \"open notepad\", \"search python 3.13\", \"play despacito\" — these run instantly, no AI needed.",
    "📨 Send messages fast: \"msg alixon: hi there\" (or \"text mom hi\", \"whatsapp/telegram/ig <name> <text>\") — no long tool command needed.",
    "💼 Business: \"track $50 income from freelancing\" logs it; \"my balance\" reports it; \"briefing\" / \"good morning\" gives the full day summary.",
    "🍥 Anime: \"new anime\" lists what's airing this season; \"trending anime\" recommends by popularity with Netflix availability flags.",
    "⚡ Vision: \"what's on my screen\" / \"screenshot\" analyzes the screen; \"take a picture\" uses the camera.",
    "🤖 /agent <task> hands work to the Composio agent (Gmail, GitHub, Calendar).",
    "💾 /save [name] keeps the conversation; /load <name> brings it back; /sessions lists them.",
    "📎 /attach <path> feeds a file to Jeeves for processing.",
    "🧠 /memory shows what Jeeves remembers about you; /clear resets the conversation.",
    "⌨️ End a line with \\ to continue typing on the next line.",
    "🚪 Type 'exit' to quit — the session auto-saves when you leave.",
    "⚡ One-shot: python cli.py ask \"<question>\" runs through the warm daemon (fast after the first call); daemon stop / daemon status manage it.",
    "🔧 /tools <name> teaches any tool — e.g. /tools send_message (or /tutorial <name>). First use of a tool also prints a one-line hint.",
    "🤵 Secretary mode: 'secretary on' answers routine messages for you. Feed messages in plain words — \"mom says: dinner at 7?\" or \"message from mom: ...\"; \"any messages for me\" shows what needs YOU; \"reply to mom: yes\" answers personally.",
    "🔗 One-time WhatsApp link: say \"link whatsapp\" (or \"secretary link\") to open the persistent WhatsApp window — scan the QR once and stay connected forever; every future send/monitor reuses the same window.",
]


def _has_any(text: str, phrases: tuple[str, ...]) -> bool:
    return any(p in text for p in phrases)


def _after(text: str, phrase: str) -> str:
    idx = text.find(phrase)
    if idx == -1:
        return ""
    return text[idx + len(phrase):].strip(" ,:;")


def _run_shortcut(tool: str, args: dict, player: ConsolePlayer) -> str:
    """Run a shortcut tool and return a short, styled confirmation."""
    result = _call_tool(tool, args, player) or "Done."
    return f"⚡ {result}"


def _shortcut_memory(player: ConsolePlayer) -> str:
    """Render long-term memory as a string (mirrors _print_memory)."""
    imports = _load_runtime_imports()
    memory = imports["load_memory"]()
    if not memory:
        return "🧠 No long-term memory stored yet."
    lines = ["🧠 Long-Term Memory:"]
    for category, values in memory.items():
        lines.append(f"\n  {category.replace('_', ' ').title()}:")
        if isinstance(values, dict):
            for key, val in values.items():
                if isinstance(val, dict):
                    val = val.get("value", val)
                lines.append(f"    {key}: {val}")
    return "\n".join(lines)


def _shortcut_message(rest: str, platform: str, player: ConsolePlayer) -> str | None:
    """Parse '<receiver>: <message>' or '<receiver> <message>' and send it.

    Returns None (fall through to the brain) when the input can't be split
    into both parts or names a generic recipient like "me" — so "text me
    the weather" stays a normal chat request instead of messaging a
    contact called "me".
    """
    rest = rest.strip()
    if ":" in rest:
        receiver, _, message = rest.partition(":")
    else:
        # Quoted receiver: 'omoke jr' hi / "omoke jr" hi — the name may
        # contain spaces, so the first token after the opening quote is the
        # whole quoted phrase, not just the first word.
        if rest[:1] in ("\"", "'", "“", "”"):
            quote = rest[0]
            end = rest.find(quote, 1)
            if end > 1 and len(rest) > end + 1:
                receiver = rest[1:end]
                message  = rest[end + 1:].strip()
            else:
                return None
        else:
            parts = rest.split(None, 1)
            if len(parts) < 2:
                return None
            receiver, message = parts
    receiver = receiver.strip().strip("\"'“”")
    message  = message.strip().strip("\"'“”")
    if not receiver or not message or receiver.lower() in ("me", "us"):
        return None
    # "from <name>: ..." means relaying an incoming message — that's the
    # secretary's job, not an outgoing send. Without this, "message from
    # mom: dinner at 7?" would try to message a contact called "from mom".
    if receiver.lower().startswith("from "):
        sender = receiver[5:].strip()
        if not sender or sender.lower() in ("me", "us"):
            return None
        return _run_shortcut("secretary", {
            "action": "handle", "sender": sender, "message": message,
        }, player)
    return _run_shortcut("send_message", {
        "receiver":     receiver,
        "message_text": message,
        "platform":     platform,
    }, player)


def _phone_shortcut_args(action: str, rest: str) -> dict:
    """Parse the argument tail of a 'phone ...' shortcut into tool args
    (so 'phone tap 540 1200' reaches the tool as x=540, y=1200 — without
    this the coords/commands would be silently dropped). Malformed numbers
    are left unset so phone_control itself explains what it needs."""
    parts = rest.split()
    base = {"action": action}
    try:
        if action in ("tap",) and len(parts) >= 2:
            base["x"] = int(parts[0])
            base["y"] = int(parts[1])
        elif action == "swipe" and len(parts) >= 4:
            base["x1"] = int(parts[0])
            base["y1"] = int(parts[1])
            base["x2"] = int(parts[2])
            base["y2"] = int(parts[3])
            if len(parts) >= 5:
                base["duration_ms"] = int(parts[4])
        elif action in ("text", "type"):
            base["text"] = rest
        elif action in ("ring", "locate", "find"):
            base["action"] = "ring"   # locate/find are just ring
            if parts and parts[0].lower() == "stop":
                base["stop"] = True
            elif parts:
                try:
                    base["seconds"] = int(parts[0])
                except (TypeError, ValueError):
                    pass
        elif action in ("macro", "macrodroid"):
            base["action"] = "macro"
            if parts and parts[0].lower() in ("list", "status"):
                base["list"] = True
            elif parts and parts[0].lower() == "start":
                base["start"] = True
            elif parts:
                base["name"] = parts[0]
                base["value"] = " ".join(parts[1:])
        elif action in ("unlock", "pin", "password"):
            base["action"] = "unlock"
            if parts and parts[0].lower() == "save" and len(parts) >= 2:
                base["save"] = True
                base["pin"] = parts[1]
                base["answer"] = " ".join(parts[2:]) if len(parts) > 2 else ""
            elif parts and parts[0].lower() == "clear":
                base["clear"] = True
                base["answer"] = " ".join(parts[1:])
            else:
                base["answer"] = rest
        elif action in ("dev", "developer", "devopts"):
            base["action"] = "dev"
            base["mode"] = parts[0].lower() if parts else "status"
        elif action in ("termux", "term"):
            base["action"] = "termux"
            if parts and parts[0].lower() in ("status", "check", "setup",
                                              "start", "stop"):
                base["mode"] = parts[0].lower()
            else:
                base["cmd"] = rest          # generic: run inside Termux
        elif action in ("notify", "notification"):
            base["action"] = "notify"
            base["text"] = rest
        elif action in ("battery", "batt"):
            base["action"] = "battery"
        elif action in ("gps", "location"):
            base["action"] = "termux"
            base["cmd"] = "gps"
        elif action in ("clipboard", "clip"):
            base["action"] = "termux"
            base["cmd"] = "clipboard" + (" " + rest if rest else "")
        elif action in ("key", "keyevent"):
            base["key"] = parts[0] if parts else ""
        elif action in ("launch", "stop"):
            base["pkg"] = parts[0] if parts else ""
        elif action == "files":
            base["path"] = rest or "/sdcard"
        elif action == "pull":
            base["remote"] = parts[0] if parts else ""
            if len(parts) >= 2:
                base["local"] = parts[1]
        elif action == "push":
            base["local"] = parts[0] if parts else ""
            base["remote"] = parts[1] if len(parts) >= 2 else ""
        elif action == "shell":
            base["cmd"] = rest
    except (TypeError, ValueError):
        pass  # leave bare action; phone_control validates and explains
    return base


def _try_shortcut(text: str, player: ConsolePlayer) -> str | None:
    """Match plain-language input to a direct tool call.

    Returns the reply string when a shortcut applies, else None so the
    caller falls through to the normal brain flow. Matching normalizes
    punctuation/case ("what's on my screen" == "what s on my screen");
    extracted arguments (queries, app names) come from the original text
    so punctuation like "python 3.13" survives intact.
    """
    orig = (text or "").strip()
    t = re.sub(r"[^\w\s]", " ", orig.lower())
    t = re.sub(r"\s+", " ", t).strip()

    # ── Messaging (checked first: content matchers below, e.g. "weather",
    #    must never steal "msg alixon: check the weather") ──
    m = re.match(r"^(?:msg|text|message)\s+(.+)$", orig, re.IGNORECASE)
    if m:
        reply = _shortcut_message(m.group(1), "whatsapp", player)
        if reply is not None:
            return reply  # else fall through (e.g. "text me ...")

    # ── WhatsApp link window: "link whatsapp" / "whatsapp login" / "connect
    #    my whatsapp" — opens the persistent window to scan the QR once ──
    if (re.match(r"^(?:link|connect)\s+(?:my\s+)?whatsapp\b", orig, re.IGNORECASE)
            or re.match(r"^whatsapp\s+(?:link|login|connect|qr)\b",
                        orig, re.IGNORECASE)
            or _has_any(t, ("link my whatsapp", "connect my whatsapp",
                            "add my whatsapp", "whatsapp login",
                            "whatsapp qr", "scan the whatsapp qr",
                            "scan whatsapp qr", "open the whatsapp login"))):
        close = "close" in t or "off" in t or "hide" in t
        return _run_shortcut("secretary",
                             {"action": "link_close" if close else "link"},
                             player)

    # ── Meta AI: "ask meta ai <q>" / "meta ai <q>" — the AI assistant
    #    built into WhatsApp answers as a free secondary brain (also the
    #    automatic fallback when the main brain is unreachable) ──
    m = re.match(r"^(?:ask\s+)?meta\s+ai\s*[:,\-]?\s+(.+)$",
                 orig, re.IGNORECASE)
    if m:
        q = m.group(1).strip()
        if q:
            return _run_shortcut("meta_ai", {"question": q}, player)

    m = re.match(r"^(?:whatsapp|telegram|instagram|ig)\s+(.+)$", orig, re.IGNORECASE)
    if m:
        head = m.group(0).lower()
        if head.startswith("telegram"):
            platform = "telegram"
        elif head.startswith(("instagram", "ig")):
            platform = "instagram"
        else:
            platform = "whatsapp"
        reply = _shortcut_message(m.group(1), platform, player)
        if reply is not None:
            return reply

    m = re.match(r"^tell\s+(.+)$", orig, re.IGNORECASE)
    if m and not re.match(r"^tell\s+(?:me|us)\b", orig, re.IGNORECASE):
        reply = _shortcut_message(m.group(1), "whatsapp", player)
        if reply is not None:
            return reply

    # ── Phone (ADB over Wi-Fi): "phone status" / "phone screenshot" /
    #    "what's on my phone" — Jeeves sees and controls the Android phone
    #    wirelessly (one-time 'phone connect' USB step first). Checked
    #    BEFORE the vision block so "phone screenshot" can't be stolen by
    #    the PC-screen "screenshot" shortcut (substring match). ──
    m = re.match(r"^phone\s+(\S.*)$", orig, re.IGNORECASE)
    if m:
        sub = m.group(1).strip()
        low = sub.lower()
        if low.startswith("devices"):
            return _run_shortcut("phone_control",
                                 {"action": "devices"}, player)
        if low.startswith(("status", "state", "info", "device")):
            return _run_shortcut("phone_control",
                                 {"action": "status"}, player)
        if low.startswith(("connect", "wireless")):
            return _run_shortcut("phone_control",
                                 {"action": "connect"}, player)
        if low.startswith(("screenshot", "shot")):
            return _run_shortcut("phone_control",
                                 {"action": "screenshot", "analyze": True},
                                 player)
        if low.startswith("screen"):
            # live mirror (Phantom Droid-style remote view) — 'phone
            # screenshot' above stays the static capture + vision.
            return _run_shortcut("phone_control",
                                 {"action": "screen"}, player)
        if low.startswith("logcat"):
            parts = sub.split(maxsplit=1)
            args = {"action": "logcat"}
            if len(parts) > 1:
                tail = parts[1].split()
                try:
                    args["lines"] = int(tail[0])
                    if len(tail) > 1:
                        args["query"] = " ".join(tail[1:])
                except (TypeError, ValueError):
                    args["query"] = parts[1].strip()
            return _run_shortcut("phone_control", args, player)
        if low.startswith("wifi"):
            return _run_shortcut("phone_control",
                                 {"action": "wifi"}, player)
        if low.startswith("network"):
            return _run_shortcut("phone_control",
                                 {"action": "network"}, player)
        if low.startswith("report"):
            return _run_shortcut("phone_control",
                                 {"action": "report"}, player)
        if low.startswith("top"):
            parts = sub.split(maxsplit=1)
            args = {"action": "top"}
            if len(parts) > 1:
                try:
                    args["limit"] = int(parts[1])
                except (TypeError, ValueError):
                    pass
            return _run_shortcut("phone_control", args, player)
        if low.startswith("storage"):
            return _run_shortcut("phone_control",
                                 {"action": "storage"}, player)
        if low.startswith(("apps", "applications")):
            parts = sub.split(maxsplit=1)
            q = parts[1].strip() if len(parts) > 1 else ""
            return _run_shortcut("phone_control",
                                 {"action": "apps", "query": q}, player)
        if low.startswith(("tap", "swipe", "text", "type", "key",
                           "launch", "stop", "files", "pull", "push",
                           "shell", "ring", "locate", "unlock", "pin",
                           "password", "macro", "macrodroid", "dev",
                           "developer", "devopts", "termux", "term",
                           "notify", "battery", "batt", "gps", "location",
                           "clipboard", "clip")):
            action = sub.split()[0].lower()
            return _run_shortcut("phone_control",
                                 _phone_shortcut_args(action,
                                                      sub[len(action):].strip()),
                                 player)
        return _run_shortcut("phone_control", {"action": "status"}, player)
    if _has_any(t, ("forgot my pin", "forgot my password", "forgot my passcode",
                    "what is my pin", "what s my pin", "what is my password",
                    "what s my password", "unlock my phone")):
        return _run_shortcut("phone_control", {"action": "unlock"}, player)
    if _has_any(t, ("find my phone", "ring my phone", "make my phone ring",
                    "locate my phone", "where is my phone")):
        return _run_shortcut("phone_control", {"action": "ring"}, player)
    if _has_any(t, ("see my phone", "what s on my phone", "whats on my phone",
                    "show my phone screen", "look at my phone")):
        return _run_shortcut("phone_control",
                             {"action": "screenshot", "analyze": True},
                             player)

    # ── Vision: screen / camera ──
    if _has_any(t, ("what s on my screen", "whats on my screen", "look at my screen",
                    "see my screen", "screenshot", "show me my screen")):
        return _run_shortcut("screen_process",
                             {"text": "Describe what is on the screen right now.", "angle": "screen"},
                             player)
    if _has_any(t, ("take a picture", "look at my camera", "camera view", "use my camera")):
        return _run_shortcut("screen_process",
                             {"text": "Describe what the camera sees right now.", "angle": "camera"},
                             player)

    # ── System monitor ──
    if _has_any(t, ("system status", "system monitor", "how is my computer", "how s my computer",
                    "pc status", "computer stats", "cpu usage")):
        return _run_shortcut("system_status", {}, player)

    # ── Instant answers (no tool, no cost) ──
    if _has_any(t, ("what time is it", "what s the time", "current time")):
        return "⏰ " + datetime.now().strftime("It's %I:%M %p on %A, %B %d, %Y.")
    if _has_any(t, ("what s the date", "what is the date", "today s date", "what day is it")):
        return "📅 " + datetime.now().strftime("Today is %A, %B %d, %Y.")

    # ── Memory ──
    if _has_any(t, ("what do you remember", "what do you know about me", "my memory",
                    "show memory", "what have you remembered")):
        return _shortcut_memory(player)

    # ── Daily briefing: "briefing" / "good morning" ──
    if _has_any(t, ("daily briefing", "morning briefing", "briefing",
                    "good morning", "what s my day like")):
        return _run_shortcut("daily_briefing", {}, player)

    # ── Finance: "my balance" / "how much money do i have" ──
    if _has_any(t, ("my balance", "what s my balance", "how much money do i have",
                    "money balance", "show balance")):
        return _run_shortcut("business_tracker", {"action": "balance"}, player)

    # ── Anime: "new anime" / "trending anime" ──
    if _has_any(t, ("new anime", "what s new in anime", "whats new in anime",
                    "trending anime", "top anime", "what anime should i watch")):
        action = "trending" if "trending" in t or "top anime" in t else "new"
        return _run_shortcut("anime_watch", {"action": action}, player)

    # ── Secretary mode ──
    if "secretary" in t and "link" in t:
        # checked before on/off so "secretary link close" closes the window
        close = "close" in t or "off" in t or "hide" in t
        return _run_shortcut("secretary",
                             {"action": "link_close" if close else "link"},
                             player)
    if "secretary" in t and ("on" in t or "enable" in t or "start" in t):
        return _run_shortcut("secretary", {"action": "on"}, player)
    if "secretary" in t and ("off" in t or "disable" in t or "stop" in t):
        return _run_shortcut("secretary", {"action": "off"}, player)
    if _has_any(t, ("secretary status", "secretary state", "is the secretary on")):
        return _run_shortcut("secretary", {"action": "status"}, player)
    if _has_any(t, ("secretary report", "secretary summary",
                    "what did the secretary do")):
        return _run_shortcut("secretary", {"action": "report"}, player)
    if "secretary" in t and ("scan" in t or "pet name" in t):
        args = {"action": "scan"}
        if re.search(r"\bdeep\b", t, re.IGNORECASE):
            args["deep"] = True
        return _run_shortcut("secretary", args, player)
    if _has_any(t, ("any messages for me", "check my messages",
                    "any escalations", "check my inbox")):
        return _run_shortcut("secretary", {"action": "inbox"}, player)
    if _has_any(t, ("morning briefing", "briefing", "good morning",
                    "what's my day like", "start my day")):
        return _run_shortcut("secretary", {"action": "briefing"}, player)
    if _has_any(t, ("any alerts", "proactive alerts", "what needs attention",
                    "overdue follow-ups", "stale conversations")):
        return _run_shortcut("secretary", {"action": "alerts"}, player)
    if _has_any(t, ("follow-ups", "follow ups", "pending promises",
                    "what did i promise", "what do i need to follow up")):
        return _run_shortcut("secretary", {"action": "followups"}, player)

    # ── Calendar shortcuts ──
    m = re.match(r"^(?:what'?s? on (?:my )?calendar|calendar|my schedule|today'?s? meetings|meetings today)\b", orig, re.IGNORECASE)
    if m:
        return _run_shortcut("secretary", {"action": "calendar"}, player)
    m = re.match(r"^(?:tomorrow'?s? meetings|meetings tomorrow|calendar tomorrow)\b", orig, re.IGNORECASE)
    if m:
        return _run_shortcut("secretary", {"action": "calendar", "sub": "tomorrow"}, player)
    m = re.match(r"^(?:this week'?s? meetings|weekly schedule|calendar week)\b", orig, re.IGNORECASE)
    if m:
        return _run_shortcut("secretary", {"action": "calendar", "sub": "week"}, player)
    m = re.match(r"^(?:next meeting|what'?s? next|upcoming meetings?)\b", orig, re.IGNORECASE)
    if m:
        return _run_shortcut("secretary", {"action": "calendar", "sub": "next"}, player)
    if _has_any(t, ("am i free", "check availability", "my availability", "free this")):
        return _run_shortcut("secretary", {"action": "calendar", "sub": "free", "message": orig}, player)

    # ── Email shortcuts ──
    m = re.match(r"^(?:check (?:my )?email|email inbox|any emails?|my emails?)\b", orig, re.IGNORECASE)
    if m:
        return _run_shortcut("secretary", {"action": "email"}, player)
    if _has_any(t, ("urgent emails", "important emails", "any urgent email")):
        return _run_shortcut("secretary", {"action": "email", "sub": "urgent"}, player)
    if _has_any(t, ("email triage", "sort my email", "4d email", "triage email")):
        return _run_shortcut("secretary", {"action": "email", "sub": "triage"}, player)
    m = re.match(r"^(?:draft (?:an? )?reply|reply to email|email reply)\b", orig, re.IGNORECASE)
    if m:
        rest = m.end()
        return _run_shortcut("secretary", {"action": "email", "sub": "draft", "message": orig[rest:].strip() or "latest email"}, player)

    # ── Delegation shortcuts ──
    m = re.match(r"^(?:delegation|delegate|who handles|routing)\b", orig, re.IGNORECASE)
    if m:
        return _run_shortcut("secretary", {"action": "delegate_list"}, player)

    # ── Meeting prep shortcuts ──
    if _has_any(t, ("meeting prep", "prepare for meeting", "prep meeting", "brief me on")):
        return _run_shortcut("secretary", {"action": "meeting_prep", "meeting": orig}, player)
    if _has_any(t, ("auto prep", "next meeting prep", "prep my next")):
        return _run_shortcut("secretary", {"action": "meeting_prep", "sub": "auto"}, player)

    # Feed an incoming message without tool syntax:
    #   "handle from mom: dinner at 7?" / "incoming from mom: ..."
    m = re.match(r"^(?:handle|incoming)\s+(?:from\s+)?(.+?)\s*:\s*(.+)$",
                 orig, re.IGNORECASE)
    if m:
        sender, msg = m.group(1).strip(), m.group(2).strip()
        if sender.lower() not in ("me", "us") and msg:
            return _run_shortcut("secretary", {
                "action": "handle", "sender": sender, "message": msg,
            }, player)

    # "mom says: dinner at 7?" / "mom says dinner at 7?"
    m = re.match(r"^(.+?)\s+says\s*:\s*(.+)$", orig, re.IGNORECASE)
    if not m:
        m = re.match(r"^(.+?)\s+says\s+(.+)$", orig, re.IGNORECASE)
    if m:
        sender, msg = m.group(1).strip(), m.group(2).strip()
        if sender.lower() not in ("me", "us") and msg:
            return _run_shortcut("secretary", {
                "action": "handle", "sender": sender, "message": msg,
            }, player)

    # Answer personally: "reply to mom: yes sounds good"
    m = re.match(r"^reply(?:\s+to)?\s+(.+?)\s*:\s*(.+)$", orig, re.IGNORECASE)
    if m:
        sender, msg = m.group(1).strip(), m.group(2).strip()
        if sender.lower() not in ("me", "us") and msg:
            return _run_shortcut("secretary", {
                "action": "reply", "sender": sender, "text": msg,
            }, player)

    # ── Open an app: "open notepad" / "open visual studio code" ──
    m = re.match(r"^open\s+(.+)$", orig, re.IGNORECASE)
    if m:
        app = re.sub(r"^(the|a|an)\s+", "", m.group(1), flags=re.IGNORECASE).strip()
        if len(app) >= 2:
            return _run_shortcut("open_app", {"app_name": app.title()}, player)

    # ── Web search: "search python" / "google x" / "look up x" ──
    m = re.match(r"^(?:search|google|look up|find)\s+(?:for\s+)?(.+)$", orig, re.IGNORECASE)
    if m:
        return _run_shortcut("web_search", {"query": m.group(1).strip(), "mode": "search"}, player)

    # ── YouTube: "play despacito" / "youtube lofi beats" ──
    m = re.match(r"^(?:play|youtube)\s+(?:the\s+)?(.+)$", orig, re.IGNORECASE)
    if m:
        return _run_shortcut("youtube_video",
                             {"action": "play", "query": m.group(1).strip()}, player)

    # ── Weather: "weather" / "weather in paris" ──
    if "weather" in t:
        low = orig.lower()
        city = _after(low, "weather in") or _after(low, "weather at") or ""
        return _run_shortcut("weather_report", {"city": city}, player)

    return None


# NOTE: _trim_context removed — use shared core.context.trim_context()
# All call sites already use _trim_context() which now resolves to the
# imported trim_context from core.context.


# ── Conversation Handling ──────────────────────────────────────────────────

def _meta_ai_fallback(text: str) -> str | None:
    """Ask Meta AI (the AI assistant built into WhatsApp) as an emergency
    brain. Returns the answer text, or None when Meta AI is unavailable
    (not linked, no Meta AI chat on the account, or it didn't reply).
    Used when the configured LLM brain fails so Jeeves never just goes
    "can't reach my brain"."""
    try:
        from actions.meta_ai import _ask_bridge
        print(f"{Style.YELLOW}[Meta AI] main brain unreachable — asking Meta AI instead.{Style.RESET}")
        return _ask_bridge(text)
    except Exception:
        return None


def _process_turn(
    text: str,
    player: ConsolePlayer,
    conversation: list[dict],
    brain_client: Any,
) -> dict:
    """Run one full turn with agentic multi-step tool loop.

    Inspired by Claude Code: the LLM can chain multiple tool calls in
    sequence before giving a final text response. This lets it search
    → read → edit, or search → analyze → summarize, without the user
    prompting between each step.

    Returns a dict: {"reply": str, "tool": str | None, "result": str | None,
                     "steps": int, "tokens_in": int, "tokens_out": int}
    """
    global _estimated_tokens_in, _estimated_tokens_out, _total_tool_calls
    conversation.append({"role": "user", "content": text})
    tools_used: list[str] = []
    last_result: str | None = None
    step = 0

    try:
        system_prompt = _build_system_prompt()
        reply = brain_client.multi_turn(
            [{"role": "system", "content": system_prompt}]
            + _trim_context(conversation[-HISTORY_WINDOW_TURNS:])
        )
    except Exception as e:
        error_msg = f"Brain error: {e}"
        print(f"\n{Style.RED}❌ {error_msg}{Style.RESET}")
        fallback = _meta_ai_fallback(text)
        if fallback:
            conversation.append({
                "role": "assistant",
                "content": f"[Meta AI] {fallback}",
            })
            return {"reply": f"[Meta AI] {fallback}", "tool": None,
                    "result": None, "steps": 0}
        return {"reply": error_msg, "tool": None, "result": None, "steps": 0}

    # ── Agentic loop: chain tool calls until the LLM gives a text reply ──
    while step < AGENTIC_MAX_STEPS:
        tool_name, tool_args = parse_tool_call(reply)

        if not tool_name:
            # Plain text reply — loop ends
            break

        step += 1
        _total_tool_calls += 1
        tools_used.append(tool_name)

        # ── Show what's happening (Claude Code-style progress) ──
        if step == 1:
            print()  # blank line before first tool
        desc = _tool_description(tool_name, tool_args or {})
        print(f"  {Style.MAGENTA}⚡{Style.RESET} {Style.BOLD}{tool_name}{Style.RESET} {Style.DIM}— {desc}{Style.RESET}")
        if tool_args and len(tool_args) > 1:
            for k, v in tool_args.items():
                if k != "action" and k != "text":
                    print(f"    {Style.DIM}{k}: {str(v)[:80]}{Style.RESET}")

        result = _call_tool(tool_name, tool_args or {}, player)
        last_result = str(result or "")
        _estimated_tokens_out += len(last_result) // 4  # rough token estimate

        # __SILENT__ tools act on their own (screen_process, etc.)
        if result == "__SILENT__" or tool_name == "screen_process":
            conversation[-1] = {
                "role": "assistant",
                "content": f"(ran {tool_name} — output shown above)",
            }
            if not _DAEMON_MODE:
                print(f"  {Style.GREEN}✓{Style.RESET} {Style.DIM}(output displayed above){Style.RESET}")
            return {"reply": "", "tool": tool_name, "result": result,
                    "steps": step}

        # Truncate huge results to keep context manageable
        result_truncated = last_result[:TOOL_RESULT_CHARS]
        if len(last_result) > TOOL_RESULT_CHARS:
            result_truncated += f"\n... ({len(last_result) - TOOL_RESULT_CHARS} chars truncated)"

        if not _DAEMON_MODE:
            preview = last_result[:200].replace("\n", " ")
            print(f"  {Style.GREEN}✓{Style.RESET} {Style.DIM}{preview}{Style.RESET}")

        # Feed result back to the LLM for the next step
        conversation.append({"role": "assistant", "content": reply})
        conversation.append({
            "role": "user",
            "content": f"[TOOL RESULT for {tool_name}]: {result_truncated}\n"
                       f"Continue working or reply to the user.",
        })

        try:
            system_prompt = _build_system_prompt()
            reply = brain_client.multi_turn(
                [{"role": "system", "content": system_prompt}]
                + _trim_context(conversation[-HISTORY_WINDOW_TURNS:])
            )
            _estimated_tokens_in += len(str(conversation)) // 4
        except Exception as e:
            return {"reply": str(last_result)[:200], "tool": tool_name,
                    "result": last_result, "steps": step}

    # ── Final response ──
    conversation.append({"role": "assistant", "content": reply})
    _estimated_tokens_out += len(reply) // 4
    _maybe_learn(text, reply)

    primary_tool = tools_used[0] if tools_used else None
    if tools_used and not _DAEMON_MODE and len(tools_used) > 1:
        print(f"  {Style.DIM}({len(tools_used)} tool calls: {', '.join(tools_used)}){Style.RESET}")

    return {
        "reply": reply,
        "tool": primary_tool,
        "result": last_result,
        "steps": step,
    }


def handle_text(
    text: str,
    player: ConsolePlayer,
    conversation: list[dict],
    brain_client: Any,
) -> dict:
    """Send `text` to the LLM reasoning engine, handle tool calls, and
    return the full turn result dict (reply, tool, result, steps).

    Plain-language shortcuts are matched first, so common asks ("open
    notepad", "what's on my screen") run instantly without the LLM.
    The "reply" key always contains the final text response.
    """
    shortcut = _try_shortcut(text, player)
    if shortcut is not None:
        return {"reply": shortcut, "tool": None, "result": None, "steps": 0}
    return _process_turn(text, player, conversation, brain_client)


# ── History (command history persistence) ──────────────────────────────────

def _load_history() -> list[str]:
    """Load command history from disk."""
    try:
        if HISTORY_FILE.exists():
            data = HISTORY_FILE.read_text(encoding="utf-8").strip().split("\n")
            return [line for line in data if line.strip()]
    except Exception:
        pass
    return []


def _save_history(history: list[str], max_items: int = 500):
    """Save command history to disk."""
    try:
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        HISTORY_FILE.write_text(
            "\n".join(history[-max_items:]), encoding="utf-8"
        )
    except Exception:
        pass


# ── Session save/load ──────────────────────────────────────────────────────

def _sanitize_session_name(name: str) -> str:
    """Sanitize a session name to prevent path traversal.

    Stark: guards against '../' or absolute paths in session names,
    which could write files outside the intended SESSION_DIR.
    """
    # Remove any path separators and directory traversal patterns
    sanitized = re.sub(r"[\\/.: ]+", "_", name)
    # Remove leading/trailing underscores from sanitization
    sanitized = sanitized.strip("_")
    # Limit length
    return sanitized[:100] or "session"


def _save_session(conversation: list[dict], name: str | None = None) -> str:
    """Save conversation to a JSON file."""
    if not name:
        name = datetime.now().strftime("session_%Y%m%d_%H%M%S")
    else:
        name = _sanitize_session_name(name)
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    path = SESSION_DIR / f"{name}.json"
    try:
        path.write_text(
            json.dumps(
                {"saved_at": datetime.now().isoformat(), "messages": conversation},
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return name
    except Exception as e:
        print(f"{Style.RED}Failed to save session: {e}{Style.RESET}")
        return ""


def _load_session(name: str) -> list[dict] | None:
    """Load a conversation from a JSON file. Guards against path traversal."""
    try:
        # Resolve and validate path stays within SESSION_DIR
        path = (SESSION_DIR / f"{name}.json").resolve()
        session_root = SESSION_DIR.resolve()
        if not str(path).startswith(str(session_root)):
            print(f"{Style.RED}Invalid session name.{Style.RESET}")
            return None
    except (ValueError, OSError):
        return None
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("messages", [])
    except Exception as e:
        print(f"{Style.RED}Failed to load session: {e}{Style.RESET}")
        return None


def _list_sessions() -> list[str]:
    """List saved session files."""
    if not SESSION_DIR.exists():
        return []
    return sorted(
        [p.stem for p in SESSION_DIR.glob("*.json")],
        reverse=True,
    )


# ── Input Helpers ──────────────────────────────────────────────────────────

MAX_INPUT_LENGTH = 8000


def _get_multiline_input(prompt: str = "You: ") -> str:
    """Read input with multi-line support (use \\ to continue).

    Stark: caps total input at MAX_INPUT_LENGTH to prevent abuse.
    Truncation happens before the running total is updated so the
    line length limit is always accurate.
    """
    lines: list[str] = []
    total_len = 0
    while True:
        try:
            sys.stdout.write(Style.BRIGHT_CYAN + prompt + Style.RESET)
            sys.stdout.flush()
            line = sys.stdin.readline()
        except KeyboardInterrupt:
            print()
            return ""
        except EOFError:
            print()
            return ""

        if not line:  # EOF
            return ""

        line = line.rstrip("\n\r")

        # Stark: enforce maximum input length (truncate BEFORE updating total)
        remaining = MAX_INPUT_LENGTH - total_len
        if remaining <= 0:
            # Already at or over limit — discard this line
            break
        if len(line) > remaining:
            print(f"{Style.YELLOW}Input truncated at {MAX_INPUT_LENGTH} characters.{Style.RESET}")
            line = line[:remaining]

        total_len += len(line)

        # Multi-line continuation: if line ends with backslash
        if line.endswith("\\"):
            lines.append(line[:-1].rstrip())
            prompt = "... "
            continue
        else:
            lines.append(line)
            break

    return "".join(lines)


# ── Display Helpers ─────────────────────────────────────────────────────────

def _print_banner():
    """Show the startup banner."""
    print()
    print(f"{Style.BRIGHT_CYAN}{'=' * 60}{Style.RESET}")
    print(f"{Style.BOLD}{Style.BRIGHT_CYAN}   MARK XXXIX-OR — JEEVES CLI{Style.RESET}")
    print(f"{Style.CYAN}   The Ultimate Cross-Platform Personal AI Assistant{Style.RESET}")
    print(f"{Style.CYAN}   Type '{Style.BRIGHT_YELLOW}/help{Style.CYAN}' for commands, '{Style.BRIGHT_YELLOW}exit{Style.CYAN}' to quit{Style.RESET}")
    print(f"{Style.DIM}   ⚡ Try: 'open notepad' · 'search <q>' · 'play <song>' · \"what's on my screen\" · /tips{Style.RESET}")
    print(f"{Style.DIM}   🧠 Multi-step agentic loop: Jeeves can chain tool calls automatically{Style.RESET}")
    print(f"{Style.DIM}   🔒 Dangerous ops require your approval (shell, delete, restart){Style.RESET}")
    print(f"{Style.BRIGHT_CYAN}{'=' * 60}{Style.RESET}")
    print()


def _print_random_tip() -> None:
    """Show a rotating usage tip after the startup banner."""
    import random
    print(f"\n{Style.DIM}{random.choice(TIPS)}{Style.RESET}\n")


def _print_tips() -> None:
    """Print every tip (the /tips command)."""
    print(f"\n{Style.BOLD}{Style.BRIGHT_CYAN}💡 Tips{Style.RESET}")
    for tip in TIPS:
        print(f"  {tip}")
    print()


def _print_shortcuts() -> None:
    """Print the plain-language shortcut table (part of /help)."""
    print(f"\n{Style.BOLD}{Style.BRIGHT_CYAN}⚡ Smart Shortcuts{Style.RESET}  (type naturally — no AI needed)")
    print(f"{Style.DIM}{'─' * 50}{Style.RESET}")
    print(f"  {Style.BRIGHT_GREEN}\"what's on my screen\"{Style.RESET}   analyze the screen (vision)")
    print(f"  {Style.BRIGHT_GREEN}\"take a picture\"{Style.RESET}       use the camera (vision)")
    print(f"  {Style.BRIGHT_GREEN}\"open <app>\"{Style.RESET}          open an app, e.g. \"open notepad\"")
    print(f"  {Style.BRIGHT_GREEN}\"search <query>\"{Style.RESET}      web search, e.g. \"search python 3.13\"")
    print(f"  {Style.BRIGHT_GREEN}\"play <song>\"{Style.RESET}         play on YouTube, e.g. \"play despacito\"")
    print(f"  {Style.BRIGHT_GREEN}\"weather [in <city>]\"{Style.RESET} weather report")
    print(f"  {Style.BRIGHT_GREEN}\"system status\"{Style.RESET}       CPU / RAM / uptime monitor")
    print(f"  {Style.BRIGHT_GREEN}\"what time is it\"{Style.RESET}     instant answer (no AI)")
    print(f"  {Style.BRIGHT_GREEN}\"what do you remember\"{Style.RESET} long-term memory")
    print(f"  {Style.BRIGHT_GREEN}\"msg <name>: <text>\"{Style.RESET}  send a WhatsApp message (no AI, no long command)")
    print(f"  {Style.BRIGHT_GREEN}\"whatsapp/telegram/ig <name> <text>\"{Style.RESET} pick the app")
    print(f"  {Style.BRIGHT_GREEN}\"link whatsapp\"{Style.RESET}         open the persistent WhatsApp window — scan once, stay connected")
    print(f"  {Style.BRIGHT_GREEN}\"my balance\"{Style.RESET}            income/expense snapshot (say \"track $50 income from X\") ")
    print(f"  {Style.BRIGHT_GREEN}\"briefing\" / \"good morning\"{Style.RESET}  full day summary: finances + monitors + reminders")
    print(f"  {Style.BRIGHT_GREEN}\"new anime\" / \"trending anime\"{Style.RESET}  season airings + popularity-ranked picks (Netflix-flagged)")
    print(f"{Style.DIM}{'─' * 50}{Style.RESET}")


def _print_help():
    """Print the help screen."""
    print(f"\n{Style.BOLD}{Style.BRIGHT_CYAN}📖 JEEVES CLI — Commands{Style.RESET}")
    print(f"{Style.DIM}{'─' * 50}{Style.RESET}")
    print(f"  {Style.BRIGHT_GREEN}/help{Style.RESET}           Show this help message")
    print(f"  {Style.BRIGHT_GREEN}/tips{Style.RESET}           Show usage tips + shortcuts")
    print(f"  {Style.BRIGHT_GREEN}/tools [name]{Style.RESET}   List all tools, or a tutorial for one (e.g. /tools send_message)")
    print(f"  {Style.BRIGHT_GREEN}/tutorial <name>{Style.RESET} Text tutorial for one tool (same as /tools <name>)")
    print(f"  {Style.BRIGHT_GREEN}/memory{Style.RESET}         Show stored long-term memory")
    print(f"  {Style.BRIGHT_GREEN}/agent <text>{Style.RESET}   Send a task to the Composio agent (GitHub/Gmail/Calendar)")
    print(f"  {Style.BRIGHT_GREEN}/save [name]{Style.RESET}    Save current conversation as a session")
    print(f"  {Style.BRIGHT_GREEN}/load <name>{Style.RESET}    Load a saved conversation session")
    print(f"  {Style.BRIGHT_GREEN}/sessions{Style.RESET}       List all saved sessions")
    print(f"  {Style.BRIGHT_GREEN}/clear{Style.RESET}          Clear the current conversation")
    print(f"  {Style.BRIGHT_GREEN}/stats{Style.RESET}          Show conversation stats")
    print(f"  {Style.BRIGHT_GREEN}/attach <path>{Style.RESET}  Attach a file for processing")
    print(f"  {Style.BRIGHT_GREEN}/mode{Style.RESET}           Show the current mode")
    print(f"  {Style.BRIGHT_GREEN}/chat{Style.RESET}           Switch to chat mode (from agent mode)")
    print(f"  {Style.BRIGHT_GREEN}/approve{Style.RESET}        Auto-approve all tool calls for this session")
    print(f"  {Style.BRIGHT_GREEN}/exit{Style.RESET}           Exit the CLI")
    _print_shortcuts()
    print()
    print(f"{Style.DIM}🧠 Agentic: Jeeves chains tool calls automatically (up to {AGENTIC_MAX_STEPS} steps){Style.RESET}")
    print(f"{Style.DIM}🔒 Safety: dangerous ops (shell, delete, restart) ask before running{Style.RESET}")
    print(f"{Style.DIM}Multi-line input: end a line with \\ to continue on the next line{Style.RESET}")
    print(f"{Style.DIM}Tip: Just type naturally — Jeeves will call tools automatically{Style.RESET}")
    print(f"{Style.DIM}{'─' * 50}{Style.RESET}\n")


def _print_tools():
    """Print all available tools with descriptions."""
    print(f"\n{Style.BOLD}{Style.BRIGHT_CYAN}🔧 Available Tools ({len(TOOL_REGISTRY)}){Style.RESET}")
    print(f"{Style.DIM}{'─' * 60}{Style.RESET}")
    for t in TOOL_REGISTRY:
        print(f"  {Style.BRIGHT_GREEN}{t['name']}{Style.RESET}")
        print(f"    {Style.GRAY}{t['description']}{Style.RESET}")
        print(f"    {Style.DIM}Usage: {Style.ITALIC}{t['usage']}{Style.RESET}")
        print()
    print(f"{Style.DIM}To use a tool directly in chat, just ask Jeeves naturally.{Style.RESET}")
    print(f"{Style.DIM}The LLM will decide which tool to call based on your request.{Style.RESET}")
    print(f"{Style.DIM}For a full tutorial on one tool, type /tools <name> — e.g. /tools send_message.{Style.RESET}\n")


def _print_tutorial(name: str) -> None:
    """Print the full text tutorial for one tool (/tools <name>)."""
    text = tool_tutorial(name)
    if not text:
        print(f"\n{Style.YELLOW}No tutorial for '{name}'.{Style.RESET}")
        print(f"{Style.DIM}Try /tools (no name) to list every tool.{Style.RESET}\n")
        return
    print(f"\n{Style.BOLD}{Style.BRIGHT_CYAN}📖 Tutorial: {name}{Style.RESET}")
    print(f"{Style.DIM}{'─' * 50}{Style.RESET}")
    print(f"{Style.GREEN}{text}{Style.RESET}")
    print(f"{Style.DIM}{'─' * 50}{Style.RESET}\n")


def _print_memory():
    """Print stored long-term memory."""
    imports = _load_runtime_imports()
    memory = imports["load_memory"]()
    if not memory:
        print(f"\n{Style.YELLOW}🧠 No long-term memory stored yet.{Style.RESET}")
        print(f"{Style.DIM}Jeeves will automatically remember things as you chat.{Style.RESET}\n")
        return

    print(f"\n{Style.BOLD}{Style.BRIGHT_MAGENTA}🧠 Long-Term Memory{Style.RESET}")
    print(f"{Style.DIM}{'─' * 50}{Style.RESET}")
    for category, values in memory.items():
        cat_label = category.replace("_", " ").title()
        print(f"\n  {Style.BRIGHT_YELLOW}{cat_label}:{Style.RESET}")
        if isinstance(values, dict):
            for key, val in values.items():
                if isinstance(val, dict):
                    val = val.get("value", val)
                print(f"    {Style.CYAN}{key}:{Style.RESET} {val}")
    print(f"{Style.DIM}{'─' * 50}{Style.RESET}\n")


def _print_stats(conversation: list[dict]):
    """Print conversation statistics."""
    user_msgs = sum(1 for m in conversation if m.get("role") == "user")
    asst_msgs = sum(1 for m in conversation if m.get("role") == "assistant")
    total_chars = sum(len(m.get("content", "")) for m in conversation)

    print(f"\n{Style.BOLD}{Style.BRIGHT_CYAN}📊 Conversation Stats{Style.RESET}")
    print(f"{Style.DIM}{'─' * 40}{Style.RESET}")
    print(f"  Total messages:  {Style.BRIGHT_WHITE}{len(conversation)}{Style.RESET}")
    print(f"  User messages:   {Style.BRIGHT_GREEN}{user_msgs}{Style.RESET}")
    print(f"  Assistant msgs:  {Style.BRIGHT_CYAN}{asst_msgs}{Style.RESET}")
    print(f"  Total length:    {Style.BRIGHT_WHITE}{total_chars:,} chars{Style.RESET}")
    print(f"{Style.DIM}{'─' * 40}{Style.RESET}\n")


def _show_thinking(animate: bool = True) -> callable:
    """Start a thinking animation; returns a stop function.

    Shows a simple spinning indicator while the LLM is working.
    Claude Code shows 'Thinking…' — we do the same with a minimal spinner.
    """
    if not animate or not sys.stdout.isatty() or _DAEMON_MODE:
        return lambda: None
    frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    stop_event = threading.Event()
    def _spin():
        i = 0
        while not stop_event.is_set():
            sys.stdout.write(f"\r  {Style.CYAN}{frames[i % len(frames)]}{Style.RESET} {Style.DIM}Thinking...{Style.RESET}")
            sys.stdout.flush()
            i += 1
            stop_event.wait(0.08)
        sys.stdout.write("\r" + " " * 40 + "\r")
        sys.stdout.flush()
    t = threading.Thread(target=_spin, daemon=True)
    t.start()
    def _stop():
        stop_event.set()
        t.join(timeout=2.0)
    return _stop


def _print_response(text: str, meta: dict | None = None):
    """Print the assistant's response with styled formatting.

    When meta is provided (from _process_turn), shows token usage and
    step count — like Claude Code's compact status footer.
    """
    if not text:
        return
    # Simple markdown-like formatting
    text = re.sub(r"\*\*(.+?)\*\*", f"{Style.BOLD}\\1{Style.RESET}", text)
    text = re.sub(r"\*(.+?)\*", f"{Style.ITALIC}\\1{Style.RESET}", text)
    text = re.sub(r"`(.+?)`", f"{Style.BRIGHT_GREEN}\\1{Style.RESET}", text)

    print(f"\n{Style.BRIGHT_CYAN}🤖 Jeeves:{Style.RESET} {text}")

    # Compact status footer (Claude Code-style)
    if meta and not _DAEMON_MODE:
        parts = []
        steps = meta.get("steps", 0)
        if steps > 0:
            tools_used = meta.get("tools_used", [])
            if tools_used:
                parts.append(f"{steps} tool call{'s' if steps > 1 else ''}")
        tokens_in = _estimated_tokens_in
        tokens_out = _estimated_tokens_out
        if tokens_in or tokens_out:
            parts.append(f"~{(tokens_in + tokens_out):,} tokens")
        if parts:
            print(f"{Style.DIM}   {' · '.join(parts)}{Style.RESET}")
    print()  # trailing newline


# ── REPL Loop ───────────────────────────────────────────────────────────────

def repl_loop(initial: str | None = None, start_mode: str = "chat"):
    """Main interactive REPL loop."""
    player = ConsolePlayer()
    brain_client = _get_brain_client()
    if brain_client is None:
        print(f"{Style.RED}Fatal: Could not initialize brain client. Check your API key.{Style.RESET}")
        return

    # Load command history
    history = _load_history()

    # Initialize conversation
    conversation: list[dict] = []
    current_mode = start_mode

    # Handle single-shot command
    if initial:
        if current_mode == "agent" or initial.startswith("agent:"):
            text = initial[len("agent:"):].strip() if initial.startswith("agent:") else initial
            run_agent = _get_composio_agent()
            if run_agent:
                print(f"{Style.YELLOW}🤖 Agent thinking...{Style.RESET}")
                try:
                    out = run_agent(text)
                except Exception as e:
                    print(f"{Style.RED}Agent failed: {type(e).__name__}: {e}{Style.RESET}")
                    return
                print(f"\n{Style.BRIGHT_CYAN}Result:{Style.RESET} {out}")
            else:
                print(f"{Style.RED}Composio agent not available.{Style.RESET}")
            return
        else:
            stop_thinking = _show_thinking()
            result = handle_text(initial, player, conversation, brain_client)
            stop_thinking()
            _print_response(result.get("reply", ""), result)
            return

    # Register cleanup handler (safe to call multiple times)
    _register_cleanup()

    # Show startup banner + a rotating tip
    _print_banner()
    _print_random_tip()

    # Load system info
    try:
        config = json.loads(API_CONFIG_PATH.read_text(encoding="utf-8"))
        provider = config.get("brain_provider", "groq")
        print(f"{Style.DIM}Provider: {provider}  |  Session dir: {SESSION_DIR}{Style.RESET}")
    except Exception:
        pass
    print()

    while True:
        try:
            prompt_prefix = {
                "chat": f"{Style.BRIGHT_GREEN}You{Style.RESET}",
                "agent": f"{Style.BRIGHT_YELLOW}Agent{Style.RESET}",
            }.get(current_mode, f"{Style.BRIGHT_GREEN}You{Style.RESET}")

            raw = _get_multiline_input(f"{prompt_prefix}: ")
        except KeyboardInterrupt:
            print(f"\n{Style.YELLOW}Use 'exit' to quit.{Style.RESET}")
            continue

        if not raw:
            continue

        text = raw.strip()
        history.append(text)
        _save_history(history)

        # ── Handle slash commands ──
        if text.startswith("/"):
            cmd_parts = text[1:].strip().split(maxsplit=1)
            cmd = cmd_parts[0].lower() if cmd_parts else ""
            cmd_arg = cmd_parts[1] if len(cmd_parts) > 1 else ""

            if cmd in ("exit", "quit", "q"):
                # Save conversation on exit if there are messages
                if len(conversation) > 2:
                    name = _save_session(conversation)
                    if name:
                        print(f"{Style.DIM}Session saved: {name}{Style.RESET}")
                print(f"\n{Style.BRIGHT_CYAN}Goodbye, sir.{Style.RESET}\n")
                break

            elif cmd == "help":
                _print_help()

            elif cmd == "tips":
                _print_tips()

            elif cmd == "tools":
                if cmd_arg:
                    _print_tutorial(cmd_arg)
                else:
                    _print_tools()

            elif cmd == "tutorial":
                if cmd_arg:
                    _print_tutorial(cmd_arg)
                else:
                    print(f"{Style.YELLOW}Usage: /tutorial <tool_name>{Style.RESET}")

            elif cmd == "memory":
                _print_memory()

            elif cmd == "clear":
                conversation.clear()
                print(f"{Style.GREEN}🧹 Conversation cleared.{Style.RESET}")

            elif cmd == "stats":
                _print_stats(conversation)

            elif cmd == "mode":
                print(f"{Style.CYAN}Current mode: {Style.BOLD}{current_mode}{Style.RESET}")

            elif cmd == "approve":
                # Toggle auto-approve mode for this session
                global _AUTO_APPROVE
                _AUTO_APPROVE = not getattr(sys.modules[__name__], '_AUTO_APPROVE', False)
                if _AUTO_APPROVE:
                    print(f"{Style.YELLOW}🔓 Auto-approve ON — all tool calls will run without confirmation.{Style.RESET}")
                else:
                    print(f"{Style.GREEN}🔒 Auto-approve OFF — dangerous tools will ask first.{Style.RESET}")

            elif cmd == "attach":
                if cmd_arg:
                    path = Path(cmd_arg).expanduser().resolve()
                    if path.exists():
                        player.current_file = str(path)
                        print(f"{Style.GREEN}📎 Attached: {path}{Style.RESET}")
                    else:
                        print(f"{Style.RED}File not found: {path}{Style.RESET}")
                else:
                    print(f"{Style.YELLOW}Usage: /attach <filepath>{Style.RESET}")

            elif cmd == "save":
                name = _save_session(conversation, cmd_arg or None)
                if name:
                    print(f"{Style.GREEN}💾 Session saved as: {Style.BOLD}{name}{Style.RESET}")
                else:
                    print(f"{Style.YELLOW}No conversation to save.{Style.RESET}")

            elif cmd == "load":
                if not cmd_arg:
                    print(f"{Style.YELLOW}Usage: /load <session_name>{Style.RESET}")
                    sessions = _list_sessions()
                    if sessions:
                        print(f"{Style.DIM}Available sessions: {', '.join(sessions[:10])}{Style.RESET}")
                else:
                    loaded = _load_session(cmd_arg)
                    if loaded is not None:
                        conversation[:] = loaded
                        print(f"{Style.GREEN}📂 Loaded session: {cmd_arg} ({len(loaded)} messages){Style.RESET}")
                        # Print the last exchange
                        for m in loaded[-3:]:
                            role = m.get("role", "?")
                            content = str(m.get("content", ""))[:80]
                            color = Style.BRIGHT_GREEN if role == "user" else Style.BRIGHT_CYAN
                            print(f"  {color}{role}:{Style.RESET} {content}")
                    else:
                        print(f"{Style.RED}Session not found: {cmd_arg}{Style.RESET}")
                        sessions = _list_sessions()
                        if sessions:
                            print(f"{Style.DIM}Available: {', '.join(sessions[:10])}{Style.RESET}")

            elif cmd == "sessions":
                sessions = _list_sessions()
                if sessions:
                    print(f"\n{Style.BRIGHT_CYAN}📂 Saved Sessions{Style.RESET}")
                    for s in sessions[:20]:
                        path = SESSION_DIR / f"{s}.json"
                        size = path.stat().st_size if path.exists() else 0
                        print(f"  {Style.GREEN}{s}{Style.RESET} ({size:,} bytes)")
                    if len(sessions) > 20:
                        print(f"  {Style.DIM}... and {len(sessions) - 20} more{Style.RESET}")
                    print()
                else:
                    print(f"{Style.YELLOW}No saved sessions.{Style.RESET}")

            elif cmd == "agent":
                if cmd_arg:
                    run_agent = _get_composio_agent()
                    if run_agent:
                        print(f"{Style.YELLOW}🤖 Agent thinking...{Style.RESET}")
                        try:
                            out = run_agent(cmd_arg)
                        except Exception as e:
                            print(f"{Style.RED}Agent failed: {type(e).__name__}: {e}{Style.RESET}")
                            continue
                        print(f"\n{Style.BRIGHT_CYAN}Result:{Style.RESET} {out}\n")
                    else:
                        print(f"{Style.RED}Composio agent not available.{Style.RESET}")
                else:
                    current_mode = "agent"
                    print(f"{Style.YELLOW}Switched to agent mode. Send a task description.{Style.RESET}")

            elif cmd == "chat":
                current_mode = "chat"
                print(f"{Style.GREEN}Switched to chat mode.{Style.RESET}")

            else:
                print(f"{Style.YELLOW}Unknown command: /{cmd}. Type /help for available commands.{Style.RESET}")

            continue

        # ── Agent mode shortcut ──
        if current_mode == "agent":
            run_agent = _get_composio_agent()
            if run_agent:
                print(f"{Style.YELLOW}🤖 Agent thinking...{Style.RESET}")
                try:
                    out = run_agent(text)
                except Exception as e:
                    print(f"{Style.RED}Agent failed: {type(e).__name__}: {e}{Style.RESET}")
                    continue
                print(f"\n{Style.BRIGHT_CYAN}Result:{Style.RESET} {out}\n")
            else:
                print(f"{Style.RED}Composio agent not available. Switch back with /chat{Style.RESET}")
            continue

        # ── Normal chat mode ──
        stop_thinking = _show_thinking()
        result = handle_text(text, player, conversation, brain_client)
        stop_thinking()
        _print_response(result.get("reply", ""), result)

        # Auto-save every 10 user messages
        user_count = sum(1 for m in conversation if m.get("role") == "user")
        if user_count > 0 and user_count % 10 == 0:
            name = _save_session(conversation, "autosave")
            if name:
                print(f"{Style.DIM}[Auto-saved: {name}]{Style.RESET}")


# ── Daemon mode (warm, persistent, spawnable) ─────────────────────────────
# A single long-running Jeeves process keeps the brain + conversation warm on
# a local JSON-lines TCP socket. One-shot spawns become fast (~seconds, not
# ~a minute) and can carry conversation state across calls.

DAEMON_DEFAULT_PORT   = 8877
DAEMON_READY_TIMEOUT  = 120.0
DAEMON_REQUEST_TIMEOUT = 300.0
# Auto-shutdown after this long with no requests — the daemon is a warm
# cache, and an idle one just sits in RAM. Set 0 to keep it alive forever.
DAEMON_IDLE_TIMEOUT_S = 600.0


# Guards the read-modify-write in _daemon_token (multiple daemon request
# threads can race to generate + persist the token on first use).
_DAEMON_TOKEN_LOCK = threading.Lock()


# Placeholder secrets shipped as defaults — a known public string is NOT
# a secret. If the config still holds one, _daemon_token() regenerates a
# real random token instead of silently authenticating with a guessable
# value (the MCP servers that bind 0.0.0.0 share this key, so the fix
# hardens them too).
_KNOWN_PLACEHOLDER_SECRETS = {
    "change_me_to_a_strong_secret",
    "change_me_callback_secret",
    "change_me",
    "changeme",
    "secret",
    "password",
}


def _daemon_token() -> str:
    """Return the shared daemon auth token.

    Reuses `jeeves_api_secret` from config/api_keys.json (same trust domain
    as the local web server). If missing OR still a known placeholder,
    generates and persists a real random one.
    """
    with _DAEMON_TOKEN_LOCK:
        cfg_path = None
        try:
            cfg_path = API_CONFIG_PATH
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
        token = str(cfg.get("jeeves_api_secret", "") or "").strip()
        if not token or token.strip().lower() in _KNOWN_PLACEHOLDER_SECRETS:
            token = secrets.token_hex(16)
            cfg["jeeves_api_secret"] = token
            if cfg_path is not None:
                try:
                    cfg_path.write_text(
                        json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8"
                    )
                except Exception as e:
                    print(f"{Style.YELLOW}⚠️ Could not persist daemon token: {e}{Style.RESET}")
        return token


def _daemon_send(conn: socket.socket, obj: dict) -> None:
    try:
        conn.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
    except Exception:
        pass


def _daemon_handle(
    req: dict,
    player: ConsolePlayer,
    brain_holder: dict,
    conversation: list[dict],
    lock: threading.Lock,
) -> dict:
    """Process one daemon request; returns the JSON response dict."""
    rtype = str(req.get("type", "chat")).strip().lower()

    if rtype == "ping":
        return {"ok": True, "pong": True}

    if rtype == "reset":
        with lock:
            conversation.clear()
        return {"ok": True, "reply": "Conversation reset."}

    if rtype == "shutdown":
        return {"ok": True, "reply": "Shutting down.", "shutdown": True}

    if rtype == "tool":
        name = str(req.get("name", "") or "").strip()
        args = req.get("args") or {}
        if not name:
            return {"ok": False, "error": "tool request requires 'name'"}
        if not isinstance(args, dict):
            return {"ok": False, "error": "'args' must be a JSON object"}
        # Tool calls don't touch the conversation — no lock needed, so they
        # never serialize behind a long-running chat turn.
        result = _call_tool(name, args, player)
        return {"ok": True, "tool": name, "result": result, "reply": result}

    if rtype == "agent":
        text = str(req.get("text", "") or "").strip()
        if not text:
            return {"ok": False, "error": "agent request requires 'text'"}
        run_agent = _get_composio_agent()
        if run_agent is None:
            return {"ok": False, "error": "Composio agent not available."}
        # Agent tasks don't touch the conversation — no lock needed.
        try:
            out = run_agent(text) or "Done."
        except Exception as e:
            return {"ok": False, "error": f"Agent failed: {type(e).__name__}: {e}"}
        return {"ok": True, "reply": str(out)}

    if rtype == "incoming":
        # Secretary mode: a message from a third party (WhatsApp, etc.).
        # When enabled, Jeeves replies to routine messages itself and
        # escalates urgent/decision ones to the boss's inbox.
        from actions.secretary import (  # lazy: keeps daemon spawn fast
            is_enabled as secretary_enabled,
            secretary as secretary_fn,
        )
        if not secretary_enabled():
            return {"ok": False, "error": "Secretary mode is OFF — enable it first: 'secretary mode on'"}
        sender = str(req.get("from", "") or "").strip()
        text = str(req.get("text", "") or "").strip()
        if not sender or not text:
            return {"ok": False, "error": "incoming request requires 'from' and 'text'"}
        out = secretary_fn(
            {"action": "handle", "sender": sender, "message": text}, player=player
        )
        return {"ok": True, "reply": out}

    # chat (default)
    text = str(req.get("text", "") or "").strip()
    if not text:
        return {"ok": False, "error": "chat request requires 'text'"}
    # Plain-language shortcuts run instantly without the LLM (same as the
    # REPL): "ask system status" or "ask open notepad" never burn a token.
    shortcut = _try_shortcut(text, player)
    if shortcut is not None:
        return {"ok": True, "reply": shortcut}
    with lock:
        if brain_holder["client"] is None:
            brain_holder["client"] = _get_brain_client()
        brain_client = brain_holder["client"]
        if brain_client is None:
            return {"ok": False, "error": "brain unavailable; check config/api_keys.json"}
        out = _process_turn(text, player, conversation, brain_client)
    out["ok"] = True
    return out


# ── Self-chat file drop (WhatsApp → PC, like /attach) ──────────────────────
# The boss sends a file to their self-chat ("Omoke Jr") from their phone;
# the daemon downloads it via the background WhatsApp bridge, saves it under
# whatsapp_media/, and "attaches" it for the next command — the same flow as
# /attach <path> in the REPL. Follow-up text messages are run through
# file_processor (summarize, extract text, describe, ...) against that file.
WHATSAPP_MEDIA_DIR = BASE_DIR / "whatsapp_media"


def _receive_self_chat_file(sender: str, preview: str,
                            attach_holder: dict) -> str:
    """Download the file the boss sent to their self-chat and remember it.

    Returns the confirmation reply to send back into WhatsApp. attach_holder
    is {"path": str | None} — set to the saved file so the next command acts
    on it."""
    try:
        from actions.whatsapp_bridge import acquire_shared_bridge
        bridge, _ = acquire_shared_bridge(headless=True)
        bridge.start()   # idempotent — reuses the monitor's live browser
        info = bridge.download_last_media(sender, str(WHATSAPP_MEDIA_DIR))
    except Exception as e:
        return (f"⚠️ Couldn't download that file: {type(e).__name__}: {e}\n"
                f"Send a document, photo or video (voice notes and stickers "
                f"aren't supported yet).")
    path = Path(info["path"])
    attach_holder["path"] = str(path)
    try:
        size = path.stat().st_size
        size_s = f"{size/1024:.0f} KB" if size < 1024**2 else f"{size/1024**2:.1f} MB"
    except Exception:
        size_s = ""
    return (f"📎 Got your file: {info['name']} ({size_s}) — saved to "
            f"{WHATSAPP_MEDIA_DIR.name}/{info['name']}.\n"
            f"Now tell me what to do with it — e.g. 'summarize', "
            f"'extract the text', 'what does it say?', 'translate'.")


def _process_attached_file(path: str, instruction: str) -> str:
    """Run an instruction against the attached file (file_processor), like
    /attach + a prompt in the REPL. Returns the result text."""
    try:
        from actions.file_processor import file_processor
        result = file_processor({"file_path": path,
                                 "instruction": instruction})
    except Exception as e:
        return f"⚠️ Could not process the file: {type(e).__name__}: {e}"
    return (result or "Done.")[:1500]


def _daemon_run(port: int = DAEMON_DEFAULT_PORT, host: str = "127.0.0.1",
                 idle_timeout: float = DAEMON_IDLE_TIMEOUT_S) -> int:
    """Run the Jeeves daemon: a warm, persistent process serving JSON-lines
    requests over a localhost TCP socket until a shutdown request arrives
    (or until `idle_timeout` seconds pass without any request)."""
    global _DAEMON_MODE
    _DAEMON_MODE = True  # suppress background memory-learning LLM calls

    token = _daemon_token()
    player = ConsolePlayer()
    brain_holder: dict = {"client": None}  # loaded lazily on first chat
    conversation: list[dict] = []
    lock = threading.Lock()
    stop = threading.Event()

    def _warm_brain() -> None:
        """Pre-import the LLM brain + groq SDK in the background.

        The very first chat request otherwise stalls ~4s on the groq SDK
        import inside the request handler. Warming here overlaps with the
        spawner's ping/readiness polling, so by the time the first real
        request arrives the modules are already in sys.modules and the ask
        goes straight to the network call. The request handler's own
        lazy load is idempotent (import machinery is thread-safe), so a
        request racing this thread is safe — it just waits on the import.
        """
        try:
            if brain_holder["client"] is None:
                brain_holder["client"] = _get_brain_client()
            try:
                # Import the exact symbol the request handler uses, so the
                # whole SDK (incl. the Groq class submodule) is cached —
                # not just the base package.
                from groq import Groq  # noqa: F401
            except Exception:
                pass  # SDK absent — the brain client handles it lazily
            print("[Jeeves daemon] brain pre-warmed", flush=True)
        except Exception as e:
            print(f"[Jeeves daemon] brain pre-warm failed: {e}", flush=True)

    threading.Thread(target=_warm_brain, daemon=True).start()

    def _start_secretary_monitor() -> None:
        """Resume background WhatsApp monitoring inside the daemon so it
        keeps watching across chat calls (and across daemon restarts) without
        the user re-enabling it.

        The remote dashboard is ALWAYS on: messages from the boss's own chat
        (secretary_self_chat, e.g. "Omoke Jr" — texting themselves from
        another number) run through the SAME warm brain and conversation as
        the terminal, so WhatsApp acts as a remote CLI even when secretary
        mode is off — shortcuts first (instant, free), then the full LLM +
        tools. Secretary triage of third-party chats stays gated on
        secretary_mode (checked per sweep inside the listener). Only skip
        the monitor entirely when there is nothing to watch: secretary off
        AND no self-chat configured (a headless browser is ~200MB — YinYang:
        don't pay for it when it serves nobody)."""
        try:
            from actions.secretary import (is_enabled, _load_cfg,
                                           _looks_like_media_preview,
                                           _media_kind_of)
            if not is_enabled() and not _load_cfg().get("secretary_self_chat"):
                return
            from actions.secretary_listener import start_monitor

            # The file the boss most recently sent to the self-chat — acts
            # like /attach: the next command runs against it.
            attach_holder = {"path": None}

            def _self_chat(sender: str, text: str) -> str:
                # The self-chat is the boss talking to Jeeves directly — the
                # reply goes back into WhatsApp, so render it as plain text.
                try:
                    text = (text or "").strip()
                    if not text:
                        return "."
                    # A file dropped into the chat → download it to the PC
                    # (like /attach) and attach it for the next command.
                    if _looks_like_media_preview(text):
                        return _receive_self_chat_file(sender, text,
                                                       attach_holder)
                    # Reactions/recalls are not commands.
                    if _media_kind_of(text) == "skip":
                        return ""
                    # "detach" releases the attached file.
                    if text.lower() in ("/detach", "detach", "clear file",
                                        "clear files", "remove file"):
                        if attach_holder["path"]:
                            attach_holder["path"] = None
                            return ("📎 File detached. "
                                    "What would you like to do?")
                        return "No file is attached."
                    # A file is attached → the next command acts on it
                    # (file_processor), like /attach + a prompt in the REPL.
                    if attach_holder["path"]:
                        return _process_attached_file(attach_holder["path"],
                                                      text)
                    shortcut = _try_shortcut(text, player)
                    if shortcut is not None:
                        return shortcut
                    with lock:
                        if brain_holder["client"] is None:
                            brain_holder["client"] = _get_brain_client()
                        brain_client = brain_holder["client"]
                        if brain_client is None:
                            fb = _meta_ai_fallback(text)
                            if fb:
                                return fb
                            return ("I can't reach my brain right now — "
                                    "check config/api_keys.json.")
                        out = _process_turn(text, player, conversation,
                                            brain_client)
                    reply = str(out.get("reply") or "").strip()
                    result = str(out.get("result") or "").strip()
                    return reply or result or "Done."
                except Exception as e:
                    return f"⚠️ Error running that: {type(e).__name__}: {e}"

            print(f"[Jeeves daemon] {start_monitor(on_self_chat=_self_chat)}",
                  flush=True)
        except Exception as e:
            print(f"[Jeeves daemon] secretary monitor not started: {e}",
                  flush=True)

    threading.Thread(target=_start_secretary_monitor, daemon=True).start()

    def _serve(conn: socket.socket) -> None:
        with conn:
            try:
                data = b""
                while b"\n" not in data:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                line = data.split(b"\n", 1)[0].decode("utf-8", "replace").strip()
                if not line:
                    return
                req = json.loads(line)
            except Exception as e:
                _daemon_send(conn, {"ok": False, "error": f"bad request: {type(e).__name__}: {e}"})
                return

            if not secrets.compare_digest(str(req.get("token", "")), token):
                _daemon_send(conn, {"ok": False, "error": "invalid token"})
                return

            try:
                resp = _daemon_handle(req, player, brain_holder, conversation, lock)
                if resp.get("shutdown"):
                    stop.set()
                _daemon_send(conn, resp)
            except Exception as e:
                # Never let a handler failure kill the connection silently.
                _daemon_send(conn, {"ok": False, "error": f"internal error: {type(e).__name__}: {e}"})

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((host, port))
    except OSError as e:
        print(f"{Style.RED}Daemon bind failed on {host}:{port}: {e}{Style.RESET}", flush=True)
        _DAEMON_MODE = False
        return 1
    srv.listen(8)
    srv.settimeout(1.0)
    print(f"[Jeeves daemon] listening on {host}:{port} (pid {os.getpid()})", flush=True)

    last_request = time.time()
    while not stop.is_set():
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            # No connection this second — check the idle threshold. Every
            # request resets the timer, so this only fires after a quiet
            # period with no traffic.
            if idle_timeout > 0 and time.time() - last_request > idle_timeout:
                # Background secretary monitoring counts as liveness: while
                # it's watching WhatsApp, don't let the idle timer kill the
                # daemon mid-shift. The monitor thread resets nothing itself,
                # so re-arm the timer here on each idle tick.
                try:
                    from actions.secretary_listener import is_monitoring
                    if is_monitoring():
                        last_request = time.time()
                        continue
                except Exception:
                    pass
                print(
                    f"[Jeeves daemon] no requests for {int(idle_timeout)}s — "
                    "auto-shutting down (run 'python cli.py daemon' or "
                    "'python cli.py ask ...' to restart)",
                    flush=True,
                )
                break
            continue
        except OSError:
            break
        last_request = time.time()
        threading.Thread(target=_serve, args=(conn,), daemon=True).start()

    srv.close()
    print("[Jeeves daemon] stopped", flush=True)
    _DAEMON_MODE = False
    return 0


def _daemon_request(req: dict, port: int, timeout: float = DAEMON_REQUEST_TIMEOUT) -> dict:
    """Send one JSON-lines request to a running daemon; returns the response."""
    req = dict(req)
    req.setdefault("token", _daemon_token())
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            s.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
            s.settimeout(timeout)
            data = b""
            while b"\n" not in data:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
    except OSError as e:
        return {"ok": False, "error": f"daemon unreachable on 127.0.0.1:{port}: {e}"}

    line = data.split(b"\n", 1)[0].decode("utf-8", "replace").strip()
    if not line:
        return {"ok": False, "error": "empty response from daemon"}
    try:
        return json.loads(line)
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"bad daemon response: {e}"}


def _daemon_spawn(port: int) -> None:
    """Start a detached background daemon; its logs go to jeeves_daemon.log."""
    log_path = BASE_DIR / "jeeves_daemon.log"
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    else:
        kwargs["start_new_session"] = True
    with open(log_path, "ab") as logf:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--daemon", "--port", str(port)],
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=subprocess.STDOUT,
            cwd=str(BASE_DIR),
            close_fds=True,
            **kwargs,
        )


def _daemon_send_or_spawn(req: dict, port: int) -> dict:
    """Send a request to the daemon, auto-starting a background daemon first
    if none is running. First call pays startup; later calls are warm."""
    for _ in range(3):
        resp = _daemon_request({"type": "ping"}, port, timeout=5.0)
        if resp.get("pong"):
            return _daemon_request(req, port, timeout=DAEMON_REQUEST_TIMEOUT)
        time.sleep(1.0)

    _daemon_spawn(port)
    deadline = time.time() + DAEMON_READY_TIMEOUT
    resp = None
    while time.time() < deadline:
        time.sleep(1.0)
        resp = _daemon_request({"type": "ping"}, port, timeout=5.0)
        if resp.get("pong"):
            break
    if not (resp and resp.get("pong")):
        return {"ok": False, "error": "daemon did not become ready; check jeeves_daemon.log"}
    return _daemon_request(req, port, timeout=DAEMON_REQUEST_TIMEOUT)


def _print_daemon_response(resp: dict, raw: bool = False) -> None:
    """Render a daemon response for the terminal."""
    if not resp.get("ok"):
        print(f"{Style.RED}Error: {resp.get('error', 'unknown error')}{Style.RESET}")
        return
    if resp.get("tool"):
        text = str(resp.get("result") or "")
        if raw:
            print(text)
        else:
            print(f"🔧 {resp['tool']}: {text}")
    else:
        text = str(resp.get("reply") or "")
        if raw:
            print(text)
        else:
            _print_response(text)


# ── Friendly subcommands (ask / daemon / tool / reset) ────────────────────
# Short, memorable one-shot verbs routed before the flag parser, so common
# operations don't require remembering --send/--daemon/--tool flag names.
# The flag forms below still work unchanged.

def _main_subcommands() -> bool:
    """Route friendly subcommands; returns True when one was handled."""
    argv = sys.argv[1:]
    if not argv:
        return False

    # ui|gui → launch the full desktop app (main.py)
    if argv[0] in ("ui", "gui"):
        import main as _main
        _main.main()
        return True

    # ask "<text>" → chat through the warm daemon (auto-starts on first use)
    if argv[0] == "ask":
        text = " ".join(a for a in argv[1:] if a.strip()).strip()
        if not text:
            print(f"{Style.YELLOW}Usage: python cli.py ask \"your question\"{Style.RESET}")
            return True
        resp = _daemon_send_or_spawn({"type": "chat", "text": text}, DAEMON_DEFAULT_PORT)
        _print_daemon_response(resp)
        return True

    # daemon [start|stop|status] [--idle-timeout SECONDS]
    if argv[0] == "daemon":
        rest = argv[1:]
        verb = rest[0].lower() if rest and not rest[0].startswith("-") else "start"
        idle = DAEMON_IDLE_TIMEOUT_S
        try:
            if "--idle-timeout" in rest:
                idle = float(rest[rest.index("--idle-timeout") + 1])
        except (ValueError, IndexError):
            pass
        if verb == "stop":
            resp = _daemon_request({"type": "shutdown"}, DAEMON_DEFAULT_PORT, timeout=10.0)
            if resp.get("ok"):
                print(f"{Style.GREEN}Daemon stopped.{Style.RESET}")
            else:
                print(f"{Style.YELLOW}Daemon not stopped: {resp.get('error', 'no daemon running?')}{Style.RESET}")
            return True
        if verb == "status":
            resp = _daemon_request({"type": "ping"}, DAEMON_DEFAULT_PORT, timeout=5.0)
            if resp.get("pong"):
                print(f"{Style.GREEN}Daemon is running on port {DAEMON_DEFAULT_PORT}.{Style.RESET}")
            else:
                print(f"{Style.YELLOW}Daemon is not running (port {DAEMON_DEFAULT_PORT}).{Style.RESET}")
            return True
        if verb != "start":
            print(f"{Style.YELLOW}Usage: python cli.py daemon [start|stop|status] [--idle-timeout SECONDS]{Style.RESET}")
            return True
        sys.exit(_daemon_run(DAEMON_DEFAULT_PORT, idle_timeout=idle))

    # tool <name> ['{json args}'] → direct tool call in this process, no LLM
    if argv[0] == "tool":
        if len(argv) < 2:
            print(f"{Style.YELLOW}Usage: python cli.py tool open_app '{{\"app_name\": \"Notepad\"}}'{Style.RESET}")
            return True
        name = argv[1]
        try:
            tool_args = json.loads(argv[2]) if len(argv) > 2 else {}
        except json.JSONDecodeError as e:
            print(f"{Style.RED}Invalid args JSON: {e}{Style.RESET}")
            return True
        if not isinstance(tool_args, dict):
            print(f"{Style.RED}Args must be a JSON object.{Style.RESET}")
            return True
        result = _call_tool(name, tool_args, ConsolePlayer())
        print(f"🔧 {name}:\n{result}")
        return True

    # reset → clear the daemon's conversation (keeps it running)
    if argv[0] == "reset":
        resp = _daemon_send_or_spawn({"type": "reset"}, DAEMON_DEFAULT_PORT)
        if resp.get("ok"):
            print(f"{Style.GREEN}Daemon conversation reset.{Style.RESET}")
        else:
            print(f"{Style.RED}Reset failed: {resp.get('error', 'unknown error')}{Style.RESET}")
        return True

    return False


# ── CLI Entry Point ─────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="MARK XXXIX-OR (Jeeves) — Terminal Interface",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python cli.py                  # Start interactive REPL\n"
            "  python cli.py -c 'hello'       # Single question\n"
            "  python cli.py -m agent         # Start in agent mode\n"
            "  python cli.py --tools          # List tools\n"
            "  python cli.py --tool open_app --args '{\"app_name\": \"Notepad\"}'  # direct tool, no LLM\n"
            "  python cli.py --daemon         # Run the warm persistent daemon\n"
            "  python cli.py --send 'open notepad'          # Send to daemon (auto-starts it)\n"
            "  python cli.py --send-tool system_status      # Direct tool via daemon\n"
            "  python cli.py --memory         # Show stored memory\n"
            "\n"
            "Friendly subcommands (no flags to remember):\n"
            "  python cli.py ui                      # Launch the desktop app\n"
            "  python cli.py ask 'open notepad'      # Quick question via the warm daemon\n"
            "  python cli.py daemon status           # Is the daemon running?\n"
            "  python cli.py daemon stop             # Stop the daemon\n"
            "  python cli.py reset                   # Clear the daemon's conversation\n"
            "  python cli.py tool open_app '{\"app_name\": \"Notepad\"}'  # Direct tool, no LLM\n"
            "  python cli.py --sessions       # List saved sessions\n"
        ),
    )
    parser.add_argument(
        "-c", "--command",
        help="Run a single prompt and exit",
    )
    parser.add_argument(
        "-m", "--mode",
        choices=["chat", "agent"],
        default="chat",
        help="Start in a specific mode (default: chat)",
    )
    parser.add_argument(
        "--tools",
        action="store_true",
        help="List all available tools and exit",
    )
    parser.add_argument(
        "--memory",
        action="store_true",
        help="Show stored long-term memory and exit",
    )
    parser.add_argument(
        "--sessions",
        action="store_true",
        help="List saved conversation sessions",
    )
    parser.add_argument(
        "--attach",
        type=str,
        help="Attach a file for processing",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run the warm, persistent Jeeves daemon and keep it alive",
    )
    parser.add_argument(
        "--idle-timeout",
        type=int,
        default=DAEMON_IDLE_TIMEOUT_S,
        metavar="SECONDS",
        help=f"Auto-shutdown after SECONDS without requests (default: {int(DAEMON_IDLE_TIMEOUT_S)}; 0 = keep alive forever)",
    )
    parser.add_argument(
        "--host",
        type=str,
        default=None,
        metavar="IP",
        help="Interface to bind the daemon to (default: 127.0.0.1, or "
             "config 'daemon_host'). Use 0.0.0.0 to allow remote clients "
             "(phone/Termux) — the token still authenticates every request.",
    )
    parser.add_argument(
        "--daemon-stop",
        action="store_true",
        help="Send a shutdown request to a running daemon",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DAEMON_DEFAULT_PORT,
        help=f"Daemon TCP port (default: {DAEMON_DEFAULT_PORT})",
    )
    parser.add_argument(
        "--send",
        type=str,
        metavar="TEXT",
        help="Send a chat prompt to the daemon (auto-starts it if not running)",
    )
    parser.add_argument(
        "--send-tool",
        type=str,
        metavar="NAME",
        help="Send a direct tool call to the daemon (no LLM); combine with --send-args",
    )
    parser.add_argument(
        "--send-args",
        type=str,
        default="{}",
        metavar="JSON",
        help="Tool arguments as JSON for --send-tool",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="With --send/--send-tool: reset the daemon's conversation first",
    )
    parser.add_argument(
        "--tool",
        type=str,
        metavar="NAME",
        help="Call a tool directly in this process and exit (no LLM); combine with --args",
    )
    parser.add_argument(
        "--args",
        type=str,
        default="{}",
        metavar="JSON",
        help="Tool arguments as JSON for --tool",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Print only the raw result (no styling) for --tool/--send/--send-tool",
    )
    return parser


def main() -> None:
    if _main_subcommands():
        return
    args = _build_parser().parse_args()

    # Daemon server mode
    if args.daemon:
        host = args.host
        if not host:
            try:
                host = str(json.loads(API_CONFIG_PATH.read_text(
                    encoding="utf-8")).get("daemon_host") or "127.0.0.1")
            except Exception:
                host = "127.0.0.1"
        sys.exit(_daemon_run(args.port, host=host,
                             idle_timeout=args.idle_timeout))

    # Daemon admin
    if args.daemon_stop:
        resp = _daemon_request({"type": "shutdown"}, args.port, timeout=10.0)
        if resp.get("ok"):
            print(f"{Style.GREEN}Daemon stopped.{Style.RESET}")
        else:
            print(f"{Style.YELLOW}Daemon not stopped: {resp.get('error', 'no daemon running?')}{Style.RESET}")
        return

    # Non-interactive commands
    if args.tools:
        _print_tools()
        return

    if args.memory:
        _print_memory()
        return

    if args.sessions:
        sessions = _list_sessions()
        if sessions:
            print(f"\n{Style.BRIGHT_CYAN}📂 Saved Sessions ({len(sessions)}){Style.RESET}")
            for s in sessions[:30]:
                path = SESSION_DIR / f"{s}.json"
                size = path.stat().st_size if path.exists() else 0
                print(f"  {Style.GREEN}{s}{Style.RESET} ({size:,} bytes)")
            if len(sessions) > 30:
                print(f"  {Style.DIM}... and {len(sessions) - 30} more{Style.RESET}")
            print()
        else:
            print(f"{Style.YELLOW}No saved sessions.{Style.RESET}")
        return

    # Direct tool invocation in this process (no LLM, deterministic, raw output)
    if args.tool:
        try:
            tool_args = json.loads(args.args or "{}")
        except json.JSONDecodeError as e:
            print(f"{Style.RED}Invalid --args JSON: {e}{Style.RESET}")
            return
        if not isinstance(tool_args, dict):
            print(f"{Style.RED}--args must be a JSON object.{Style.RESET}")
            return
        player = ConsolePlayer()
        result = _call_tool(args.tool, tool_args, player)
        if args.raw:
            print(result)
        else:
            print(f"🔧 {args.tool}:\n{result}")
        return

    # Daemon client (auto-starts a warm daemon if none is running)
    if args.send is not None or args.send_tool:
        if args.reset:
            _daemon_send_or_spawn({"type": "reset"}, args.port)
        if args.send_tool:
            try:
                tool_args = json.loads(args.send_args or "{}")
            except json.JSONDecodeError as e:
                print(f"{Style.RED}Invalid --send-args JSON: {e}{Style.RESET}")
                return
            if not isinstance(tool_args, dict):
                print(f"{Style.RED}--send-args must be a JSON object.{Style.RESET}")
                return
            req = {"type": "tool", "name": args.send_tool, "args": tool_args}
        elif args.mode == "agent":
            req = {"type": "agent", "text": args.send or ""}
        else:
            req = {"type": "chat", "text": args.send or ""}
        resp = _daemon_send_or_spawn(req, args.port)
        _print_daemon_response(resp, raw=args.raw)
        return

    # Interactive REPL
    try:
        repl_loop(args.command, start_mode=args.mode)
    except KeyboardInterrupt:
        print(f"\n{Style.BRIGHT_CYAN}Shutting down...{Style.RESET}")
    except BrokenPipeError:
        pass


if __name__ == "__main__":
    main()
