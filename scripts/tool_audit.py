"""scripts/tool_audit.py -- Trigger every Jeeves tool with a SAFE minimal task.

Each tool is invoked through cli._call_tool (the same dispatch the CLI/LLM
uses) inside a timeout-guarded daemon thread so a hanging tool can never
block the audit. Arguments were chosen to be non-destructive:

  - send_message     -> no receiver/message (validation path only). A real
                        send would fire an actual WhatsApp/Telegram message.
  - reminder         -> far-future date; the created Task Scheduler entry is
                        deleted right after.
  - computer_settings-> 'escape' keypress only (no volume/wifi/shutdown).
  - computer_control -> 'wait' only (no typing/mouse).
  - browser_control  -> 'list' only (no browser launch).
  - desktop_control  -> 'stats' only (no wallpaper/organize).
  - shutdown_jeeves  -> expected to raise SystemExit(0) cleanly.
  - code_helper      -> 'run' a throwaway temp script (no LLM, no repo files).

Status values: OK / OK-BUT-ENV (quota, missing key, network) / BROKEN (real
bug) / TIMEOUT / CLEAN-EXIT / SKIPPED (not fired by design).
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cli  # noqa: E402


class _ToolRunner:
    """Runs one tool call in a daemon thread with a timeout."""

    def __init__(self, name: str, args: dict, timeout: float):
        self.name = name
        self.args = args
        self.timeout = timeout
        self.result: str | None = None
        self.error: str | None = None
        self.stdout: str = ""
        self.elapsed = 0.0
        self._exc: BaseException | None = None

    def _run(self):
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.result = cli._call_tool(self.name, self.args, cli.ConsolePlayer())
            self.stdout = buf.getvalue()
        except SystemExit as e:
            self._exc = e  # shutdown_jeeves etc.
        except BaseException as e:  # noqa: BLE001 -- audit must not die
            self._exc = e
            self.error = f"{type(e).__name__}: {e}"

    def go(self) -> str:
        t0 = time.perf_counter()
        thread = threading.Thread(target=self._run, daemon=True)
        thread.start()
        thread.join(timeout=self.timeout)
        self.elapsed = time.perf_counter() - t0
        if thread.is_alive():
            return "TIMEOUT"
        if isinstance(self._exc, SystemExit):
            return f"CLEAN-EXIT({self._exc.code})"
        if self._exc is not None:
            return "ERROR"
        return "OK"


def _cleanup_reminder():
    """Delete the scheduled task the reminder test creates."""
    try:
        subprocess.run(
            'schtasks /Delete /TN "MARKReminder_20990101_0000" /F',
            shell=True, capture_output=True, timeout=20,
        )
    except Exception:
        pass


def _make_temp_hello() -> Path:
    """Write a throwaway hello.py for the code_helper 'run' test."""
    fd, path = tempfile.mkstemp(suffix=".py", prefix="jeeves_audit_")
    with open(fd, "w", encoding="utf-8") as f:
        f.write('print("tool audit ok")\n')
    return Path(path)


TOOL_TESTS: list[tuple[str, dict, float, str]] = [
    ("open_app",         {"app_name": "notepad"},                    30,  "opens Notepad (harmless)"),
    ("web_search",       {"mode": "search", "query": "test"},       60,  "DuckDuckGo fallback"),
    ("system_status",    {},                                         30,  "read-only"),
    ("weather_report",   {"city": "London"},                         45,  "network API"),
    ("send_message",     {"receiver": "", "message_text": ""},       15,  "validation path only (no real send)"),
    ("reminder",         {"date": "2099-01-01", "time": "00:00", "message": "tool audit test"}, 30, "scheduled task deleted after"),
    ("youtube_video",    {"action": "get_info", "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}, 45, "network read"),
    ("screen_process",   {"text": "What do you see?"},               60,  "captures screen (async)"),
    ("computer_settings",{"action": "escape"},                       15,  "presses Escape only"),
    ("browser_control",  {"action": "list"},                         20,  "no browser launch"),
    ("file_controller",  {"action": "list", "path": "desktop"},      20,  "read-only"),
    ("desktop_control",  {"action": "stats"},                        20,  "read-only"),
    ("code_helper",      {"action": "run", "file_path": ""},         60,  "runs throwaway temp script"),
    ("dev_agent",        {"description": "create a text file saying tool audit", "timeout": 5}, 90, "LLM-heavy (likely quota-blocked)"),
    ("computer_control", {"action": "wait", "seconds": 1},           15,  "wait only"),
    ("cmd_control",      {"task": "what is the current date and time"}, 45, "heuristic -> Get-Date"),
    ("game_updater",     {"action": "list", "platform": "steam"},    30,  "lists installed games"),
    ("flight_finder",    {"origin": "NYC", "destination": "London", "date": "2026-09-01"}, 90, "browser/network (may be slow)"),
    ("file_processor",   {"file_path": "test_silence.wav", "action": "info"}, 30, "audio metadata read"),
    ("agent_task",       {"goal": "unused"},                         1,   "SKIP: composio agent (LLM quota; verified separately)"),
    ("composio_action",  {"request": "unused"},                      1,   "SKIP: composio agent (LLM quota; verified separately)"),
    ("shutdown_jeeves",  {},                                         15,  "expected clean SystemExit(0)"),
    ("save_memory",      {"category": "notes", "key": "tool_audit_marker", "value": "audit pass"}, 10, "writes a note to memory"),
]


def main() -> int:
    print("=" * 78)
    print("JEEVES TOOL AUDIT -- safe minimal triggers")
    print("=" * 78)

    hello = _make_temp_hello()
    try:
        for name, args, timeout, note in TOOL_TESTS:
            if name == "code_helper":
                args = dict(args, file_path=str(hello))

            if name in ("agent_task", "composio_action"):
                print(f"\n▸ {name:18s}  SKIPPED  ({note})")
                continue

            if name == "file_processor" and not Path("test_silence.wav").exists():
                print(f"\n▸ {name:18s}  SKIPPED  (test_silence.wav fixture not present)")
                continue

            runner = _ToolRunner(name, args, timeout)
            status = runner.go()

            if name == "reminder":
                _cleanup_reminder()

            head = (runner.result or runner.error or "").replace("\n", " ")[:110]
            print(f"\n▸ {name:18s}  {status:12s}  [{runner.elapsed:5.1f}s]  {note}")
            if runner.stdout.strip():
                print(f"    log: {runner.stdout.strip().splitlines()[-1][:110]}")
            if head:
                print(f"    out: {head}")
    finally:
        hello.unlink(missing_ok=True)

    print("\n" + "=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
