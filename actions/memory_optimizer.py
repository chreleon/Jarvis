"""
memory_optimizer.py -- Find RAM-hungry processes and free memory safely.

Two tools for Jeeves:
  * top_memory_processes -- list what is eating RAM / CPU (no side effects).
  * memory_optimizer      -- close the biggest *background* memory hogs.

The golden rule (user requirement): NEVER close apps that are being used.
"Being used" is detected conservatively:
  - the process in the foreground window,
  - every process that owns a visible, titled top-level window (open apps),
  - critical Windows system processes and Session 0 services,
  - Jeeves itself (this project's python processes).

Only windowless background processes above a memory floor are candidates,
and the kill count is capped. Graceful terminate() first, then kill() if it
does not exit. Everything is defensive: a failed read/kill is reported, never
raised, so the background alert path can never crash the monitor loop.

Windows window detection uses ctypes (user32) -- zero extra dependencies.
On non-Windows, window detection is skipped and the protection list shrinks
to critical system names + self, so the tool still works on Linux/Mac.
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from pathlib import Path

import psutil

BASE_DIR = Path(__file__).resolve().parent.parent

# ── Critical system processes that must never be touched ─────────────────────
_CRITICAL_NAMES = {
    "system", "system idle process", "registry", "smss", "csrss", "wininit",
    "winlogon", "services", "lsass", "lsm", "svchost", "dwm", "conhost",
    "fontdrvhost", "dllhost", "explorer", "taskhostw", "sihost", "ctfmon",
    "spoolsv", "securityhealthservice", "securityhealthsystray", "msmpeng",
    "audiodg", "shellexperiencehost", "startmenuexperiencehost", "searchapp",
    "searchindexer", "runtimebroker", "applicationframehost", "winlogon",
    "winsrv", "sessenv", "csrss", "vmmem", "vmmemwsl",
}

# Minimal memory footprint (MB) a process needs to be worth killing.
_MIN_MEM_MB = 100
# Maximum number of processes closed in a single optimization pass.
_MAX_KILL = 3

# caches for window detection (Windows only)
_user32 = None
_window_pids_cache: set[int] | None = None
_foreground_pid_cache: int | None = None
_window_cache_ts = 0.0
_WINDOW_CACHE_TTL = 5.0


# ── Window detection (Windows) ────────────────────────────────────────────────

def _win_user32():
    global _user32
    if _user32 is None and sys.platform.startswith("win"):
        _user32 = ctypes.windll.user32
    return _user32


def _window_pids() -> set[int]:
    """PIDs of processes owning a visible, titled top-level window (open apps)."""
    global _window_pids_cache, _window_cache_ts
    now = time.monotonic()
    if _window_pids_cache is not None and now - _window_cache_ts < _WINDOW_CACHE_TTL:
        return _window_pids_cache

    pids: set[int] = set()
    user32 = _win_user32()
    if user32 is not None:
        from ctypes import wintypes

        def _enum_cb(hwnd, _lparam):
            if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value:
                    pids.add(int(pid.value))
            return True  # keep enumerating

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        try:
            user32.EnumWindows(WNDENUMPROC(_enum_cb), 0)
        except Exception:
            pass

    _window_pids_cache = pids
    _window_cache_ts = now
    return pids


def _foreground_pid() -> int | None:
    """PID of the process in the foreground window (Windows only)."""
    global _foreground_pid_cache
    user32 = _win_user32()
    if user32 is None:
        return None
    from ctypes import wintypes
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        _foreground_pid_cache = int(pid.value) or None
    except Exception:
        _foreground_pid_cache = None
    return _foreground_pid_cache


# ── Protection ────────────────────────────────────────────────────────────────

def _is_self_process(proc: psutil.Process) -> bool:
    """True if the process is part of this Jeeves project (never kill it)."""
    try:
        exe = (proc.exe() or "").lower()
        if "python" in exe or "jeeves" in exe:
            try:
                cmd = " ".join(proc.cmdline() or []).lower()
                return str(BASE_DIR).lower() in cmd
            except Exception:
                return True  # can't read cmdline; treat python as self to be safe
    except Exception:
        pass
    return False


def _protected_pids(force: bool = False) -> tuple[set[int], dict[int, str]]:
    """PIDs that must not be killed -> (pid_set, {pid: reason}).

    When force=True we still protect critical system processes and Jeeves
    itself, but NOT window-owning apps (the user explicitly asked to force).
    """
    protected: set[int] = set()
    reasons: dict[int, str] = {}

    # Self: the current process and anything running from this project dir.
    protected.add(os.getpid())
    reasons[os.getpid()] = "Jeeves itself"

    for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
        try:
            pid = proc.info["pid"]
            name = (proc.info["name"] or "").lower()
            if _is_self_process(proc):
                protected.add(pid)
                reasons[pid] = "Jeeves itself"
                continue
            if name in _CRITICAL_NAMES:
                protected.add(pid)
                reasons[pid] = "critical system process"
                continue
            if not force:
                # Session 0 = services/background system sessions.
                try:
                    if proc.username() in ("SYSTEM", "NT AUTHORITY\\SYSTEM", "root") \
                            and pid not in (os.getpid(),):
                        protected.add(pid)
                        reasons[pid] = "system session process"
                        continue
                except Exception:
                    pass
        except Exception:
            continue

    if not force:
        # Apps "being used": the foreground window + any visible window owner.
        fg = _foreground_pid()
        if fg:
            protected.add(fg)
            reasons[fg] = "foreground app (in use)"
        for pid in _window_pids():
            if pid not in protected:
                protected.add(pid)
                reasons[pid] = "open app window (in use)"

    return protected, reasons


# ── Top-memory listing ────────────────────────────────────────────────────────

def _top_memory_rows(limit: int = 10) -> list[dict]:
    """Top RAM processes: [{pid, name, mem_mb, cpu_percent, in_use}]."""
    rows: list[dict] = []
    protected, _ = _protected_pids()
    for proc in psutil.process_iter(["pid", "name", "memory_info", "cpu_percent"]):
        try:
            info = proc.info
            mem = info.get("memory_info")
            mem_mb = (mem.rss if mem else 0) / (1024 ** 2)
            if mem_mb < 1:
                continue
            rows.append({
                "pid": info["pid"],
                "name": info["name"] or "?",
                "mem_mb": round(mem_mb, 1),
                "cpu_percent": round(float(info.get("cpu_percent") or 0.0), 1),
                "in_use": info["pid"] in protected,
            })
        except Exception:
            continue
    rows.sort(key=lambda r: r["mem_mb"], reverse=True)
    return rows[:max(1, int(limit))]


def format_top_memory(rows: list[dict]) -> str:
    """Render the top-process list as readable text for speech/logs."""
    if not rows:
        return "No process memory data available."
    lines = ["Top memory consumers:"]
    for r in rows:
        tag = " (in use - skipped)" if r["in_use"] else ""
        lines.append(
            f"  {r['name']} - {r['mem_mb']} MB, CPU {r['cpu_percent']}%, PID {r['pid']}{tag}"
        )
    return "\n".join(lines)


# ── Optimization ──────────────────────────────────────────────────────────────

def optimize_ram_now(max_kill: int = _MAX_KILL, min_mem_mb: int = _MIN_MEM_MB,
                     force: bool = False) -> dict:
    """Close the biggest SAFE (background) memory hogs.

    Returns a dict:
      {freed_mb, killed: [{name, pid, mem_mb}], skipped: [str], message}
    Never raises. Kills are graceful (terminate), escalated to kill() if the
    process does not exit within a short grace period.
    """
    protected, reasons = _protected_pids(force=force)
    killed: list[dict] = []
    skipped: list[str] = []
    freed_mb = 0.0

    candidates: list[tuple[float, psutil.Process, str]] = []
    for proc in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            pid = proc.info["pid"]
            name = (proc.info["name"] or "?").lower()
            mem = proc.info.get("memory_info")
            mem_mb = (mem.rss if mem else 0) / (1024 ** 2)
            if pid in protected or pid == os.getpid():
                if pid in reasons and not _is_self_process(proc):
                    skipped.append(f"{name} ({mem_mb:.0f} MB): {reasons[pid]}")
                continue
            if mem_mb < min_mem_mb:
                continue
            candidates.append((mem_mb, proc, name))
        except Exception:
            continue

    candidates.sort(key=lambda c: c[0], reverse=True)
    for mem_mb, proc, name in candidates[:max_kill]:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except psutil.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2.0)
            killed.append({"name": name, "pid": proc.pid, "mem_mb": round(mem_mb, 1)})
            freed_mb += mem_mb
        except Exception as e:
            skipped.append(f"{name}: {e}")

    # Friendly message for the caller / user.
    if killed:
        message = (
            f"Freed ~{freed_mb:.0f} MB of RAM by closing "
            + ", ".join(f"{k['name']} ({k['mem_mb']:.0f} MB)" for k in killed)
            + ". Open apps were left untouched."
        )
    else:
        message = (
            "No background processes were safely closeable - the memory hogs "
            "are apps currently in use. Recommend closing them manually."
        )
    return {
        "freed_mb": round(freed_mb, 1),
        "killed": killed,
        "skipped": skipped[:8],
        "message": message,
    }


# ── Action entry points (tool-compatible signatures) ─────────────────────────

def top_memory_processes(parameters: dict | None = None, response=None,
                         player=None, session_memory=None, speak=None) -> str:
    """Tool: list what is eating RAM right now (read-only)."""
    params = parameters or {}
    try:
        limit = int(params.get("limit", 10) or 10)
    except (TypeError, ValueError):
        limit = 10
    rows = _top_memory_rows(limit=limit)
    text = format_top_memory(rows)
    if player:
        player.write_log("[sys] " + text.replace("\n", " | "))
    return text


def memory_optimizer(parameters: dict | None = None, response=None,
                     player=None, session_memory=None, speak=None) -> str:
    """Tool: close background RAM hogs without touching apps in use."""
    params = parameters or {}
    try:
        force = bool(params.get("force", False))
        max_kill = int(params.get("max_kill", _MAX_KILL) or _MAX_KILL)
        min_mem_mb = int(params.get("min_mem_mb", _MIN_MEM_MB) or _MIN_MEM_MB)
    except (TypeError, ValueError):
        force, max_kill, min_mem_mb = False, _MAX_KILL, _MIN_MEM_MB

    result = optimize_ram_now(max_kill=max_kill, min_mem_mb=min_mem_mb, force=force)

    lines = [result["message"]]
    if result["killed"]:
        lines.append("Closed:")
        for k in result["killed"]:
            lines.append(f"  - {k['name']} (PID {k['pid']}, {k['mem_mb']} MB)")
    if result["skipped"]:
        lines.append("Left alone (in use / system):")
        for s in result["skipped"]:
            lines.append(f"  - {s}")

    text = "\n".join(lines)
    if player:
        player.write_log("[sys] " + text.replace("\n", " | "))
    if speak:
        speak(result["message"])
    return text


if __name__ == "__main__":
    print(format_top_memory(_top_memory_rows(10)))
    print()
    print(optimize_ram_now()["message"])
