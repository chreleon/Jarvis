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
from core.utils import parse_tool_call

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


# ── System Prompt Builder ───────────────────────────────────────────────────

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

    # Try loading core/prompt.txt, fall back to a default
    sys_prompt = ""
    try:
        sys_prompt = CORE_PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        sys_prompt = (
            "You are JEEVES, Tony Stark's AI assistant. "
            "Be concise, direct, and always use the provided tools to complete tasks. "
            "Never simulate or guess results — always call the appropriate tool."
        )

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
        "Available tools:\n" + compact_tool_declarations()
    )

    return "\n".join(parts)


# ── Tool Call Execution (parsing uses shared parse_tool_call from core/utils) ──


def _call_tool(name: str, args: dict, player: ConsolePlayer) -> str:
    """Execute a tool by name with the given arguments."""
    imports = _load_runtime_imports()
    name = name or ""

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
        }

        if name in dispatch:
            result = dispatch[name]()
            return result or "Done."

        if name == "screen_process":
            # Lazy import: actions.screen_processor costs ~14s to import.
            def _run_vision():
                try:
                    from actions.screen_processor import screen_process
                except Exception as e:
                    print(f"{Style.RED}Vision module unavailable: {e}{Style.RESET}")
                    return
                screen_process(parameters=args, player=player)

            threading.Thread(target=_run_vision, daemon=True).start()
            return "📷 Vision module activated. Check output above."

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


# ── Conversation Handling ──────────────────────────────────────────────────

def _process_turn(
    text: str,
    player: ConsolePlayer,
    conversation: list[dict],
    brain_client: Any,
) -> dict:
    """Run one full turn: LLM reply → optional tool execution → follow-up.

    Returns a dict: {"reply": str, "tool": str | None, "result": str | None}
    so callers (REPL, daemon) can render or inspect the outcome.
    """
    conversation.append({"role": "user", "content": text})

    try:
        system_prompt = _build_system_prompt()
        reply = brain_client.multi_turn(
            [{"role": "system", "content": system_prompt}] + conversation[-20:]
        )
    except Exception as e:
        error_msg = f"Brain error: {e}"
        print(f"\n{Style.RED}❌ {error_msg}{Style.RESET}")
        return {"reply": error_msg, "tool": None, "result": None}

    conversation.append({"role": "assistant", "content": reply})
    tool_name, tool_args = parse_tool_call(reply)

    if tool_name:
        # ── Tool call detected ──
        print(f"\n{Style.MAGENTA}🔧 Calling: {Style.BOLD}{tool_name}{Style.RESET}")
        if tool_args:
            for k, v in tool_args.items():
                print(f"  {Style.DIM}{k}: {v}{Style.RESET}")

        result = _call_tool(tool_name, tool_args or {}, player)

        # If the tool returns __SILENT__ (e.g. save_memory), don't do follow-up
        if result == "__SILENT__":
            # Replace the raw tool-call assistant message with a cleaner version
            conversation[-1] = {"role": "assistant", "content": "(memory saved silently)"}
            return {"reply": "", "tool": tool_name, "result": result}

        print(f"\n{Style.GREEN}📋 Result:{Style.RESET} {result[:500]}")

        # Follow-up: feed result back to the LLM for a natural response
        conversation.append({
            "role": "user",
            "content": f"[TOOL RESULT for {tool_name}]: {result}\nNow reply to the user naturally.",
        })
        try:
            system_prompt = _build_system_prompt()
            followup = brain_client.multi_turn(
                [{"role": "system", "content": system_prompt}] + conversation[-20:]
            )
            conversation.append({"role": "assistant", "content": followup})

            # ── Stark: memory extraction in background thread (skipped in daemon mode) ──
            if len(text) > 5 and not _DAEMON_MODE:
                threading.Thread(
                    target=_update_memory_async,
                    args=(text, followup),
                    daemon=True,
                ).start()

            return {"reply": followup, "tool": tool_name, "result": result}
        except Exception as e:
            return {"reply": str(result)[:200], "tool": tool_name, "result": result}

    else:
        # ── Plain text reply ──
        # ── Stark: memory extraction in background thread (skipped in daemon mode) ──
        if len(text) > 5 and not _DAEMON_MODE:
            threading.Thread(
                target=_update_memory_async,
                args=(text, reply),
                daemon=True,
            ).start()
        return {"reply": reply, "tool": None, "result": None}


def handle_text(
    text: str,
    player: ConsolePlayer,
    conversation: list[dict],
    brain_client: Any,
) -> str:
    """Send `text` to the LLM reasoning engine, handle tool calls, and
    return the final response string (compat wrapper for the REPL)."""
    return _process_turn(text, player, conversation, brain_client)["reply"]


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
    print(f"{Style.BRIGHT_CYAN}{'=' * 60}{Style.RESET}")
    print()


def _print_help():
    """Print the help screen."""
    print(f"\n{Style.BOLD}{Style.BRIGHT_CYAN}📖 JEEVES CLI — Commands{Style.RESET}")
    print(f"{Style.DIM}{'─' * 50}{Style.RESET}")
    print(f"  {Style.BRIGHT_GREEN}/help{Style.RESET}           Show this help message")
    print(f"  {Style.BRIGHT_GREEN}/tools{Style.RESET}          List all available tools with descriptions")
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
    print(f"  {Style.BRIGHT_GREEN}/exit{Style.RESET}           Exit the CLI")
    print()
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
    print(f"{Style.DIM}The LLM will decide which tool to call based on your request.{Style.RESET}\n")


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


def _print_response(text: str):
    """Print the assistant's response with styled formatting."""
    if not text:
        return
    # Simple markdown-like formatting
    text = re.sub(r"\*\*(.+?)\*\*", f"{Style.BOLD}\\1{Style.RESET}", text)
    text = re.sub(r"\*(.+?)\*", f"{Style.ITALIC}\\1{Style.RESET}", text)
    text = re.sub(r"`(.+?)`", f"{Style.BRIGHT_GREEN}\\1{Style.RESET}", text)

    print(f"\n{Style.BRIGHT_CYAN}🤖 Jeeves:{Style.RESET} {text}\n")


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
                out = run_agent(text)
                print(f"\n{Style.BRIGHT_CYAN}Result:{Style.RESET} {out}")
            else:
                print(f"{Style.RED}Composio agent not available.{Style.RESET}")
            return
        else:
            result = handle_text(initial, player, conversation, brain_client)
            _print_response(result)
            return

    # Register cleanup handler (safe to call multiple times)
    _register_cleanup()

    # Show startup banner
    _print_banner()

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

            elif cmd == "tools":
                _print_tools()

            elif cmd == "memory":
                _print_memory()

            elif cmd == "clear":
                conversation.clear()
                print(f"{Style.GREEN}🧹 Conversation cleared.{Style.RESET}")

            elif cmd == "stats":
                _print_stats(conversation)

            elif cmd == "mode":
                print(f"{Style.CYAN}Current mode: {Style.BOLD}{current_mode}{Style.RESET}")

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
                        out = run_agent(cmd_arg)
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
                out = run_agent(text)
                print(f"\n{Style.BRIGHT_CYAN}Result:{Style.RESET} {out}\n")
            else:
                print(f"{Style.RED}Composio agent not available. Switch back with /chat{Style.RESET}")
            continue

        # ── Normal chat mode ──
        result = handle_text(text, player, conversation, brain_client)
        _print_response(result)

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


def _daemon_token() -> str:
    """Return the shared daemon auth token.

    Reuses `jeeves_api_secret` from config/api_keys.json (same trust domain
    as the local web server). If missing, generates and persists one.
    """
    cfg_path = None
    try:
        cfg_path = API_CONFIG_PATH
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    token = str(cfg.get("jeeves_api_secret", "") or "").strip()
    if not token:
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
        with lock:
            result = _call_tool(name, args, player)
        return {"ok": True, "tool": name, "result": result, "reply": result}

    if rtype == "agent":
        text = str(req.get("text", "") or "").strip()
        if not text:
            return {"ok": False, "error": "agent request requires 'text'"}
        run_agent = _get_composio_agent()
        if run_agent is None:
            return {"ok": False, "error": "Composio agent not available."}
        with lock:
            out = run_agent(text) or "Done."
        return {"ok": True, "reply": str(out)}

    # chat (default)
    text = str(req.get("text", "") or "").strip()
    if not text:
        return {"ok": False, "error": "chat request requires 'text'"}
    with lock:
        if brain_holder["client"] is None:
            brain_holder["client"] = _get_brain_client()
        brain_client = brain_holder["client"]
        if brain_client is None:
            return {"ok": False, "error": "brain unavailable; check config/api_keys.json"}
        out = _process_turn(text, player, conversation, brain_client)
    out["ok"] = True
    return out


def _daemon_run(port: int = DAEMON_DEFAULT_PORT, host: str = "127.0.0.1") -> int:
    """Run the Jeeves daemon: a warm, persistent process serving JSON-lines
    requests over a localhost TCP socket until a shutdown request arrives."""
    global _DAEMON_MODE
    _DAEMON_MODE = True  # suppress background memory-learning LLM calls

    token = _daemon_token()
    player = ConsolePlayer()
    brain_holder: dict = {"client": None}  # loaded lazily on first chat
    conversation: list[dict] = []
    lock = threading.Lock()
    stop = threading.Event()

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

            if str(req.get("token", "")) != token:
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

    while not stop.is_set():
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            continue
        except OSError:
            break
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
    args = _build_parser().parse_args()

    # Daemon server mode
    if args.daemon:
        sys.exit(_daemon_run(args.port))

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
