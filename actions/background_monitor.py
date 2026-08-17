"""
BackgroundMonitor — user-configured topic watching.

Checks DDG news once per day per topic; alerts JEEVES when a new headline
appears.

NOTE: prints are ASCII-only because this module runs inside background
threads where Windows' cp1252 console encoding would crash on emoji.
"""
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path


# ── Slug / hash helpers ───────────────────────────────────────────────────────

def _slug(topic: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", topic.lower().strip())[:40].strip("_")

def _title_hash(title: str) -> str:
    return hashlib.md5(title.encode("utf-8", errors="ignore")).hexdigest()[:12]


# ── Memory I/O ─────────────────────────────────────────────────────────────────

def _load() -> dict:
    from memory.memory_manager import load_memory
    data = load_memory().get("monitors", {})
    return data if isinstance(data, dict) else {}

def _save(monitors: dict) -> None:
    from memory.memory_manager import load_memory, MEMORY_PATH, _lock
    # Read and write atomically under the same lock: loading outside it
    # would let another writer's change between our read and write get
    # silently overwritten by this stale snapshot.
    with _lock:
        memory = load_memory()
        memory["monitors"] = monitors
        MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        MEMORY_PATH.write_text(
            json.dumps(memory, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


# ── Public API ─────────────────────────────────────────────────────────────────

def add_monitor(topic: str) -> str:
    topic = topic.strip()
    if not topic:
        return "Please specify a topic to monitor."
    monitors = _load()
    slug = _slug(topic)
    if slug in monitors:
        return f"Already monitoring: {monitors[slug]['topic']}"
    monitors[slug] = {
        "topic":      topic,
        "added":      datetime.now().strftime("%Y-%m-%d"),
        "last_check": "",
        "last_hash":  "",
    }
    _save(monitors)
    print(f"[Monitor] Added: {topic}")
    return f"Now monitoring: {topic}"


def remove_monitor(topic: str) -> str:
    topic = topic.strip().lower()
    monitors = _load()
    # exact slug match first
    slug = _slug(topic)
    if slug in monitors:
        label = monitors.pop(slug)["topic"]
        _save(monitors)
        return f"Stopped monitoring: {label}"
    # partial match fallback
    for key, val in list(monitors.items()):
        if topic in val.get("topic", "").lower():
            label = monitors.pop(key)["topic"]
            _save(monitors)
            return f"Stopped monitoring: {label}"
    return f"Not found in monitored topics: {topic}"


def list_monitors() -> list[str]:
    return [v.get("topic", k) for k, v in _load().items()]


def check_all() -> list[str]:
    """
    Run all pending topic checks (once per day per topic).
    Returns a list of [MONITOR_ALERT] strings — empty if nothing new.

    Each topic is network-bound (a DDG news call), so pending topics run
    in a small thread pool instead of serially — several monitored topics
    finish in roughly the time of one. Workers only read the shared
    monitors dict; updates are merged back on the caller thread.
    """
    from concurrent.futures import ThreadPoolExecutor
    from actions.web_search import _ddg_news

    monitors = _load()
    if not monitors:
        return []

    today   = datetime.now().strftime("%Y-%m-%d")
    pending = {slug: data for slug, data in monitors.items()
               if data.get("last_check") != today}
    if not pending:
        return []                        # everything checked today already

    def _check(slug: str, data: dict) -> tuple[str, dict, str | None]:
        """Check one topic; returns (slug, updated_data, alert_or_None)."""
        topic   = data.get("topic", slug)
        updated = dict(data)
        try:
            results = _ddg_news(topic, max_results=5)
            updated["last_check"] = today
            if not results:
                return slug, updated, None

            top   = results[0]
            title = top.get("title", "").strip()
            if not title:
                return slug, updated, None

            h = _title_hash(title)
            if h != data.get("last_hash"):      # new headline since last check
                updated["last_hash"] = h
                snippet = top.get("snippet", "")[:150]
                source  = top.get("source", "")
                parts   = [f"[MONITOR_ALERT] {topic}", f"Headline: {title}"]
                if snippet:
                    parts.append(snippet)
                if source:
                    parts.append(f"Source: {source}")
                print(f"[Monitor] New headline for '{topic}': {title[:60]}")
                return slug, updated, "\n".join(parts)
            return slug, updated, None
        except Exception as e:
            print(f"[Monitor] Check failed for '{topic}': {e}")
            return slug, data, None

    alerts: list[str] = []
    changed = False
    with ThreadPoolExecutor(max_workers=min(4, len(pending))) as pool:
        for slug, updated, alert in pool.map(
            lambda item: _check(item[0], item[1]), list(pending.items())
        ):
            monitors[slug] = updated
            changed = changed or updated.get("last_check") == today
            if alert:
                alerts.append(alert)

    if changed:
        _save(monitors)

    return alerts
