"""actions/cmd_control.py — System command execution from natural language.

The planner defines cmd_control as:
  task: string (required) — natural language description of what to do
  visible: boolean (optional) — show command window

This module interprets the natural-language task as a shell command,
executes it safely, and returns the result. Supports Windows, macOS,
and Linux with appropriate OS-specific command generation.

Designed as a proper replacement for the old misrouting of cmd_control
to computer_control in executor.py.
"""

import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from core.utils import get_api_config, subprocess_no_window_kwargs

# ── Helpers ─────────────────────────────────────────────────────────────────


def _detect_os() -> str:
    """Return 'windows', 'macos', or 'linux'.

    Prefers the config file's os_system key (allows overriding), then
    falls back to sys.platform for auto-detection.
    """
    cfg_os = get_api_config().get("os_system", "").lower()
    if cfg_os in ("windows", "macos", "linux"):
        return cfg_os
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _shell_args(command: str, visible: bool = False) -> list[str]:
    """Build the subprocess argument list for the current OS.

    On Windows:
      - visible=False  -> powershell -NoProfile -NonInteractive -Command <cmd>
      - visible=True   -> cmd /c start "" <cmd>  (opens a new window)
    On macOS/Linux:
      - visible=False  -> [sh, -c, <cmd>]
      - visible=True   -> [sh, -c, <cmd>]       (terminal already visible)
    """
    os_name = _detect_os()
    if os_name == "windows":
        if visible:
            # Start a new visible cmd window
            # NOTE: shlex.join would quote the whole command as one arg,
            # breaking start's argument parsing — pass the raw string.
            return ["cmd", "/c", "start", "", command]
        return ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
    # macOS / Linux
    return ["sh", "-c", command]


def _interpret_task(task: str) -> str:
    """Use an LLM to turn a natural language task into a concrete shell command.

    Falls back to heuristic matching when the LLM is unavailable.
    """
    from or_client import ClaudeModelShim, GROQ_LITE_MODEL

    os_name = _detect_os()

    model = ClaudeModelShim(
        model_name=GROQ_LITE_MODEL,
        system_instruction=(
            f"You are a {os_name} shell command generator. "
            f"Given a natural language task description, reply with ONLY the "
            f"single shell command to accomplish it. "
            f"Do not include explanation, markdown, or backticks. "
            f"Use the correct syntax for {os_name.upper()}. "
            f"Keep commands safe — avoid destructive operations (rm -rf, del /f, etc.)."
        ),
    )

    try:
        response = model.generate_content(
            f"Generate a {os_name} shell command for: {task}"
        )
        cmd = response.text.strip()
        cmd = re.sub(r"```(?:bash|sh|powershell|cmd)?", "", cmd).strip().rstrip("`").strip()
        if cmd:
            return cmd
    except Exception as exc:
        print(f"[CmdControl] ⚠️ LLM interpretation failed: {exc}")

    # ── Fallback: heuristic command generation ──
    return _heuristic_command(task, os_name)


def _heuristic_command(task: str, os_name: str) -> str:
    """Generate a safe command from a task using rule-based matching."""
    t = task.lower().strip()

    # Open a file or application
    open_match = re.match(r"open\s+(.+?)(?:\s+with\s+(.+))?$", t, re.IGNORECASE)
    if open_match:
        target = open_match.group(1).strip()
        app = open_match.group(2)
        if app:
            app = app.strip()
            if os_name == "windows":
                return f'start "" "{app}" "{target}"'
            elif os_name == "macos":
                return f'open -a "{app}" "{target}"'
            return f'xdg-open "{target}"'
        # No specific app
        if os_name == "windows":
            return f'start "" "{target}"'
        elif os_name == "macos":
            return f'open "{target}"'
        return f'xdg-open "{target}"'

    # Launch an app
    launch_match = re.match(r"(?:launch|start|run)\s+(.+?)$", t, re.IGNORECASE)
    if launch_match:
        app_name = launch_match.group(1).strip()
        if os_name == "windows":
            return f"start {app_name}"
        elif os_name == "macos":
            return f'open -a "{app_name}"'
        return app_name  # Assume it's on PATH

    # List files in a directory
    list_match = re.match(r"list\s+(?:files?\s+)?(?:in\s+)?(.+)", t, re.IGNORECASE)
    if list_match:
        path = list_match.group(1).strip()
        return f"ls -la {shlex.quote(path)}" if os_name != "windows" else f"dir {shlex.quote(path)}"

    # Create a directory
    mkdir_match = re.match(r"create\s+(?:a\s+)?(?:new\s+)?(?:directory|folder)\s+(?:called\s+)?(.+)", t, re.IGNORECASE)
    if mkdir_match:
        dir_name = mkdir_match.group(1).strip()
        return f"mkdir -p {shlex.quote(dir_name)}" if os_name != "windows" else f'mkdir "{dir_name}"'

    # Check disk usage
    if "disk" in t or "storage" in t or "space" in t:
        if os_name == "windows":
            return "wmic logicaldisk get size,freespace,caption"
        return "df -h"

    # Process info
    if "process" in t or "task" in t or "running" in t:
        if os_name == "windows":
            return "tasklist /FO TABLE"
        return "ps aux"

    # System info
    if "system info" in t or "os" in t or "version" in t:
        if os_name == "windows":
            return "systeminfo | findstr /C:'OS Name' /C:'OS Version'"
        elif os_name == "macos":
            return "sw_vers"
        return "uname -a"

    # Whoami / user info
    if "who" in t or "current user" in t or "logged" in t:
        if os_name == "windows":
            return "whoami"
        return "whoami"

    # Network / IP
    if "ip" in t or "network" in t or "internet" in t:
        if os_name == "windows":
            return "ipconfig"
        elif os_name == "macos":
            return "ifconfig"
        return "ip addr"

    # Date / time
    if "date" in t or "time" in t:
        if os_name == "windows":
            return "Get-Date"
        return "date"

    # Fallback: just echo the task so the user sees we're trying
    return f"echo 'Unrecognized task: {task}'"


# ── Public API ──────────────────────────────────────────────────────────────


def cmd_control(
    parameters: dict,
    player: Callable | None = None,
    speak: Callable | None = None,
) -> str:
    """Execute a natural-language system command.

    Args:
        parameters: dict with keys:
            task (str, required):   Natural language description of what to do.
            visible (bool, opt):    If True, show a command window (Windows only).
        player:  Optional UI player for logging.
        speak:   Optional TTS callback.

    Returns:
        String output of the command, or an error message.
    """
    params = parameters or {}
    task = (params.get("task") or "").strip()
    if not task:
        return "No task specified for cmd_control."

    print(f"[CmdControl] ▶ task: {task[:120]}")

    if speak:
        speak("Interpreting command, sir.")

    # ── Step 1: interpret task → shell command ──
    try:
        command = _interpret_task(task)
    except Exception as exc:
        msg = f"Failed to interpret task: {exc}"
        print(f"[CmdControl] ❌ {msg}")
        return msg

    print(f"[CmdControl] 🖥️ executing: {command}")

    # ── Step 2: check remote execution ──
    try:
        from actions.remote_runner import remote_execution_enabled, remote_run_command

        if remote_execution_enabled():
            print("[CmdControl] 🌐 Running on remote shell")
            try:
                return remote_run_command(command, project_dir=Path.cwd(), timeout=30)
            except Exception as exc:
                return f"Remote execution failed: {exc}"
    except ImportError:
        pass  # Remote runner not available, run locally

    # ── Step 3: execute locally ──
    os_name = _detect_os()
    visible = bool(params.get("visible", False))
    args = _shell_args(command, visible=visible)

    try:
        # Visible mode: fire-and-forget (no output capture)
        if visible and os_name == "windows":
            subprocess.Popen(args, shell=False)
            return f"Launched in new window: {command}"

        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            **subprocess_no_window_kwargs(),
        )

        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        if result.returncode != 0:
            parts = [f"Command exited with code {result.returncode}"]
            if stdout:
                parts.append(stdout)
            if stderr:
                parts.append(stderr)
            return "\n".join(parts)

        # Successful execution
        output = stdout or stderr or "Command completed with no output."
        print(f"[CmdControl] ✅ success ({len(output)} chars)")
        return output

    except subprocess.TimeoutExpired:
        return "Command timed out after 30 seconds."
    except FileNotFoundError:
        return f"Command not found: {command}"
    except Exception as exc:
        return f"Command execution failed: {exc}"


if __name__ == "__main__":
    # Quick smoke test
    test_tasks = [
        "who is the current user",
        "what time is it",
        "list files in this directory",
        "open notepad",
        "check disk space",
    ]
    for t in test_tasks:
        print(f"\n{'='*60}")
        print(f"Task: {t}")
        print(f"Result: {cmd_control({'task': t})}")
