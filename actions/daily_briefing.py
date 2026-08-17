# actions/daily_briefing.py
# One command, whole day — Jeeves' local-first "run your business" briefing.
#
# Aggregates everything Jeeves already knows, with no network required:
#   • date / greeting
#   • finance snapshot   (business_tracker, if any entries exist)
#   • background topics  (manage_monitor)
#   • upcoming reminders (Windows Task Scheduler, MARKReminder_* tasks)
#   • email summary      (optional include_email=True → Composio agent)
#
# Everything except the optional email is local and instant. ASCII-safe
# prints only (may run in daemon threads on cp1252 Windows consoles).

import subprocess
import sys
from datetime import datetime

from core.utils import subprocess_no_window_kwargs


def _format_money(amount: float) -> str:
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"


def _finance_snapshot() -> str:
    try:
        from actions.business_tracker import _load, _totals, _today
        entries = _load().get("entries", [])
        if not entries:
            return ""
        inc, exp = _totals(entries)
        month = _today()[:7]
        m_inc, m_exp = _totals([e for e in entries if e["date"][:7] == month])
        lines = [
            "  Finances:",
            f"    Balance {_format_money(inc - exp)}  "
            f"(Income {_format_money(inc)} / Expenses {_format_money(exp)})",
            f"    This month ({month}): {_format_money(m_inc - m_exp)}",
        ]
        return "\n".join(lines)
    except Exception:
        return ""


def _monitors_snapshot() -> str:
    try:
        from actions.background_monitor import list_monitors
        topics = list_monitors()
        if not topics:
            return ""
        return "  Watching: " + ", ".join(topics)
    except Exception:
        return ""


def _reminders_snapshot() -> str:
    """Upcoming MARKReminder_* tasks from Windows Task Scheduler."""
    if sys.platform != "win32":
        return ""
    try:
        proc = subprocess.run(
            ["schtasks", "/Query", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10,
            **subprocess_no_window_kwargs(),
        )
        if proc.returncode != 0:
            return ""
        upcoming = []
        for line in proc.stdout.splitlines():
            if "MARKReminder_" not in line:
                continue
            cols = [c.strip().strip('"') for c in line.split('","')]
            if len(cols) >= 3:
                task, when, status = cols[0], cols[1], cols[2]
                if "MARKReminder_" in task and "Ready" in status:
                    upcoming.append((when, task))
        if not upcoming:
            return ""
        upcoming.sort()
        parts = [f"  Reminders ({len(upcoming)} upcoming):"]
        for when, _task in upcoming[:5]:
            try:
                parsed = datetime.strptime(when, "%m/%d/%Y %I:%M %p")
                parts.append(f"    {parsed.strftime('%b %d, %I:%M %p')}")
            except ValueError:
                parts.append(f"    {when}")
        if len(upcoming) > 5:
            parts.append(f"    ...and {len(upcoming) - 5} more")
        return "\n".join(parts)
    except Exception:
        return ""


def _email_snapshot() -> str:
    """Optional Composio email summary — only when explicitly requested."""
    try:
        from composio_agent import run_agentic_task
        out = run_agentic_task(
            "Summarize my unread email: who wrote, what about, and any "
            "action needed. Keep it under 5 bullet points.",
            max_turns=3,
        ) or ""
        out = str(out).strip()
        if not out or "couldn't finish" in out.lower():
            return "  Email: summary unavailable right now."
        return "  Email:\n    " + "\n    ".join(out.splitlines())[:600]
    except Exception as e:
        return f"  Email: unavailable ({type(e).__name__})"


def daily_briefing(parameters: dict, player=None, session_memory=None) -> str:
    """Produce the daily briefing.

    parameters:
        include_email (bool) — also summarize unread email via the
            Composio agent (slow, uses tokens). Off by default.
    """
    params = parameters or {}
    now = datetime.now()
    lines = [
        f"Good {'morning' if now.hour < 12 else 'afternoon' if now.hour < 18 else 'evening'}."
        f" It's {now.strftime('%A, %B %d, %Y')} — {now.strftime('%I:%M %p')}.",
    ]

    finance = _finance_snapshot()
    if finance:
        lines.append(finance)

    monitors = _monitors_snapshot()
    if monitors:
        lines.append(monitors)

    reminders = _reminders_snapshot()
    if reminders:
        lines.append(reminders)

    if not (finance or monitors or reminders):
        lines.append(
            "  Nothing on the books yet. Try: 'track $50 income from "
            "freelancing', 'monitor AI news', or 'remind me at 18:00 to "
            "call mom'."
        )

    if str(params.get("include_email", "")).lower() in ("1", "true", "yes"):
        email = _email_snapshot()
        if email:
            lines.append(email)

    briefing = "\n".join(lines)
    if player:
        player.write_log("[briefing] generated")
    return briefing
