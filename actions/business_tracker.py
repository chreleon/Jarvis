# actions/business_tracker.py
# Local-first revenue & expense tracking — Jeeves' own answer to cloud
# revenue dashboards. No external accounts: the user tells Jeeves about
# income/expenses (or pastes a CSV) and it is stored in long-term memory
# under "finances". Reports balances, monthly breakdowns, and trends.
#
# NOTE: prints are ASCII-only ($, no emoji) because this module can run
# inside background/daemon threads where Windows' cp1252 console would
# crash on non-ASCII output.

import json
import re
from datetime import datetime

_KINDS = ("income", "expense")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ── Storage (long-term memory, "finances" category) ──────────────────────────

def _load() -> dict:
    from memory.memory_manager import load_memory
    data = load_memory().get("finances", {})
    if not isinstance(data, dict):
        return {"entries": []}
    data.setdefault("entries", [])
    return data


def _save(fin: dict) -> None:
    from memory.memory_manager import load_memory, MEMORY_PATH, _lock
    # Read-modify-write under the shared lock so concurrent writers can't
    # clobber each other (same pattern as actions/background_monitor._save).
    with _lock:
        memory = load_memory()
        memory["finances"] = fin
        MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        MEMORY_PATH.write_text(
            json.dumps(memory, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


# ── Formatting helpers ────────────────────────────────────────────────────────

def _fmt(amount: float) -> str:
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _month_of(date_str: str) -> str:
    return date_str[:7]


def _norm_amount(raw) -> float | None:
    try:
        amount = float(str(raw).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None
    # Amounts are always positive; direction is expressed via kind
    # (income/expense). Rejecting negatives avoids silently flipping a
    # typo like -50 into +50.
    if amount <= 0:
        return None
    return round(amount, 2)


def _norm_date(raw) -> str:
    if isinstance(raw, str):
        raw = raw.strip()
        if _DATE_RE.match(raw):
            return raw
        # "YYYY-MM" → first day of that month
        if re.match(r"^\d{4}-\d{2}$", raw):
            return raw + "-01"
    return _today()


# ── Operations ────────────────────────────────────────────────────────────────

def add_entry(kind: str, amount, label: str = "", date: str = "") -> str:
    kind = (kind or "").strip().lower()
    if kind not in _KINDS:
        return "kind must be 'income' or 'expense'."
    amt = _norm_amount(amount)
    if amt is None:
        return "Please give a positive amount, e.g. amount=50."
    label = (label or "Untitled").strip()[:80]
    date = _norm_date(date)

    fin = _load()
    fin["entries"].append({
        "type": kind, "amount": amt, "label": label, "date": date,
        "source": "manual",
    })
    fin["updated"] = datetime.now().isoformat(timespec="seconds")
    _save(fin)
    return f"Recorded {kind}: {_fmt(amt)} for {label} ({date})."


def remove_entry(index=None, label: str = "") -> str:
    """Remove by 1-based index (newest first, as shown by `list`) or label."""
    fin = _load()
    entries = list(reversed(fin["entries"]))  # newest first
    if not entries:
        return "No entries to remove."

    if index is not None:
        try:
            idx = int(index)
        except (TypeError, ValueError):
            return "index must be a number."
        if not (1 <= idx <= len(entries)):
            return f"No entry at index {idx} (list has {len(entries)})."
        removed = entries.pop(idx - 1)
    else:
        label = (label or "").strip().lower()
        if not label:
            return "Give an index (from `list`) or a label to remove."
        for i, e in enumerate(entries):
            if label in e.get("label", "").lower():
                removed = entries.pop(i)
                break
        else:
            return f"No entry matching label '{label}'."
    fin["entries"] = list(reversed(entries))
    fin["updated"] = datetime.now().isoformat(timespec="seconds")
    _save(fin)
    return (f"Removed {removed['type']}: {_fmt(removed['amount'])} "
            f"for {removed['label']} ({removed['date']}).")


def list_entries(limit: int = 15) -> str:
    fin = _load()
    entries = list(reversed(fin["entries"]))
    if not entries:
        return "No entries recorded yet."
    lines = [f"{len(entries)} entries recorded (newest first):"]
    for i, e in enumerate(entries[:max(1, int(limit))], start=1):
        lines.append(
            f"  {i}. {e['date']}  {e['type']:<7} {_fmt(e['amount']):>10}  {e['label']}"
        )
    return "\n".join(lines)


def _totals(entries: list[dict]) -> tuple[float, float]:
    income = sum(e["amount"] for e in entries if e["type"] == "income")
    expense = sum(e["amount"] for e in entries if e["type"] == "expense")
    return income, expense


def balance() -> str:
    fin = _load()
    entries = fin["entries"]
    if not entries:
        return "No entries yet — say 'track $50 income from freelancing' to start."

    inc, exp = _totals(entries)
    month = _today()[:7]
    m_entries = [e for e in entries if e["date"][:7] == month]
    m_inc, m_exp = _totals(m_entries)

    lines = [
        f"Balance: {_fmt(inc - exp)}  (Income {_fmt(inc)} · Expenses {_fmt(exp)})",
        f"This month ({month}): {_fmt(m_inc - m_exp)} "
        f"(Income {_fmt(m_inc)} · Expenses {_fmt(m_exp)})",
    ]
    return "\n".join(lines)


def monthly_report(month: str = "") -> str:
    month = (month or _today()[:7]).strip()
    if not re.match(r"^\d{4}-\d{2}$", month):
        return "month must look like '2026-08'."
    fin = _load()
    entries = [e for e in fin["entries"] if e["date"][:7] == month]
    if not entries:
        return f"No entries for {month}."
    inc, exp = _totals(entries)
    lines = [f"{month}: Net {_fmt(inc - exp)}  "
             f"(Income {_fmt(inc)} · Expenses {_fmt(exp)})"]
    for e in entries:
        lines.append(f"  {e['date']}  {e['type']:<7} {_fmt(e['amount']):>10}  {e['label']}")
    return "\n".join(lines)


def import_csv(text: str) -> str:
    """Import CSV lines: 'date,type,amount,label' (date optional)."""
    rows = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not rows:
        return "Nothing to import — paste CSV lines: date,type,amount,label"

    fin = _load()
    added = skipped = 0
    for ln in rows:
        parts = [p.strip() for p in ln.split(",")]
        # Accept 4 columns (date,type,amount,label) or 3 (type,amount,label)
        if len(parts) == 4:
            date, kind, amount, label = parts
        elif len(parts) >= 3:
            kind, amount, label = parts[0], parts[1], ",".join(parts[2:])
            date = ""
        else:
            skipped += 1
            continue
        kind = kind.strip().lower()
        amt = _norm_amount(amount)
        if kind not in _KINDS or amt is None:
            skipped += 1
            continue
        fin["entries"].append({
            "type": kind, "amount": amt,
            "label": (label or "Untitled").strip()[:80],
            "date": _norm_date(date), "source": "csv",
        })
        added += 1

    if added:
        fin["updated"] = datetime.now().isoformat(timespec="seconds")
        _save(fin)
    return f"Imported {added} entries" + (f", skipped {skipped}" if skipped else "") + "."


def clear(confirm: str = "") -> str:
    if confirm.strip().lower() != "yes":
        return "This erases ALL entries. Reply with confirm='yes' to proceed."
    _save({"entries": []})
    return "All entries cleared."


# ── Tool entry point ──────────────────────────────────────────────────────────

def business_tracker(parameters: dict, player=None, session_memory=None) -> str:
    """Tool dispatcher: add | remove | list | balance | monthly | import | clear."""
    params = parameters or {}
    action = str(params.get("action", "balance")).strip().lower()
    if action == "add":
        return add_entry(
            params.get("kind", ""), params.get("amount"),
            params.get("label", ""), params.get("date", ""),
        )
    if action == "remove":
        return remove_entry(params.get("index"), params.get("label", ""))
    if action == "list":
        return list_entries(params.get("limit", 15))
    if action == "monthly":
        return monthly_report(params.get("month", ""))
    if action == "import":
        return import_csv(params.get("text", ""))
    if action == "clear":
        return clear(params.get("confirm", ""))
    if action == "balance":
        return balance()
    return ("Unknown action. Use: add, remove, list, balance, monthly, "
            "import, clear.")
