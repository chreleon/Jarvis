"""core/utils.py — Shared project utilities for Jeeves (MARK XXXIX-OR).

Canonical source of truth for:
  - get_base_dir()       Path to project root (eliminates 15x duplication)
  - get_api_config()     Load config/api_keys.json safely
  - load_api_key()       Load a named key from config with optional fallback

All modules should import from here instead of defining their own copies.
"""

import json
import sys
import os
from pathlib import Path
from typing import Any


def get_base_dir() -> Path:
    """Return the project root directory.

    Handles both development (__file__) and PyInstaller-frozen builds.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = get_base_dir()
CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"


# mtime-keyed cache: config is read on hot paths (every cmd_control
# command, every MCP request, every key lookup), and re-reading +
# re-parsing the file per call is pure waste. Editing the file bumps
# mtime, so the cache refreshes automatically — the "edit config, pick
# it up immediately, no restart" contract is preserved exactly.
_api_config_cache: dict | None = None
_api_config_mtime_ns: int = -1
_api_config_size: int = -1


def get_api_config() -> dict[str, Any]:
    """Load config/api_keys.json as a dict (cached, mtime-invalidated).
    Returns {} on any error."""
    global _api_config_cache, _api_config_mtime_ns, _api_config_size
    try:
        st = CONFIG_PATH.stat()
        if (_api_config_cache is not None
                and st.st_mtime_ns == _api_config_mtime_ns
                and st.st_size == _api_config_size):
            return _api_config_cache
    except OSError:
        pass
    try:
        if CONFIG_PATH.exists():
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        else:
            cfg = {}
    except Exception:
        cfg = {}
    try:
        st = CONFIG_PATH.stat()
        _api_config_mtime_ns, _api_config_size = st.st_mtime_ns, st.st_size
    except OSError:
        pass
    _api_config_cache = cfg
    return cfg


def normalize_api_key(value) -> str:
    """Return the first non-empty, stripped key from a string or list of keys.

    Config files may store a single key as a string or multiple keys (e.g.
    rotation/fallback) as a list. This helper guarantees every consumer gets
    a usable single key instead of crashing or stringifying the list.

    Args:
        value: A string, a list/tuple of strings, or None.

    Returns:
        The first non-empty stripped key, or empty string if none found.
    """
    if isinstance(value, (list, tuple)):
        for item in value:
            key = normalize_api_key(item)
            if key:
                return key
        return ""
    if value is None:
        return ""
    return str(value).strip()


def normalize_api_key_list(value) -> list[str]:
    """Return every non-empty, de-duplicated key from a string or list of keys.

    Like normalize_api_key but keeps all keys instead of only the first —
    used by the Groq key pool where multiple free-tier keys rotate so the
    assistant never runs out of quota.

    Args:
        value: A string, a list/tuple of strings, or None.

    Returns:
        A list of non-empty stripped keys, preserving order (no duplicates).
    """
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            key = normalize_api_key(item)
            if key and key not in out:
                out.append(key)
        return out
    key = normalize_api_key(value)
    return [key] if key else []


def load_api_key(key_name: str, fallback_keys: list[str] | None = None) -> str:
    """Load an API key from config, trying fallback key names if the primary is empty.

    Args:
        key_name: Primary key to look up (e.g. "groq_api_key").
        fallback_keys: Optional list of fallback key names to try.

    Returns:
        The key value (first entry if stored as a list), or empty string if none found.
    """
    config = get_api_config()
    key = normalize_api_key(config.get(key_name, "") or "")
    if not key and fallback_keys:
        for fallback in fallback_keys:
            key = normalize_api_key(config.get(fallback, "") or "")
            if key:
                break
    return key


def parse_tool_call(reply: str) -> tuple[str | None, dict | None]:
    """Parse a JSON tool call from the LLM response.

    Returns (tool_name, tool_args) or (None, None) if the reply is
    plain text (no tool call).

    This is the canonical implementation — both main.py and cli.py
    should import from here instead of defining their own.
    """
    cleaned = (reply or "").strip()
    if cleaned.startswith("```"):
        parts = cleaned.split("```")
        cleaned = parts[1] if len(parts) > 1 else cleaned
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    cleaned = cleaned.strip()
    if not cleaned.startswith("{"):
        return None, None
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return None, None
    call = data.get("tool_call")
    if not call or not isinstance(call, dict):
        return None, None
    return call.get("name"), call.get("args", {})


def get_provider_api_key(provider: str | None = None) -> str:
    """Resolve an API key for a given provider.

    If `provider` is None, the function reads `brain_provider` from
    `config/api_keys.json` and returns the corresponding key. For
    GitHub Models it will try `github_models_api_key`, `github_token`,
    and the `GITHUB_TOKEN` environment variable. For Groq it returns
    `groq_api_key`.
    """
    cfg = get_api_config()
    prov = (provider or str(cfg.get("brain_provider", "groq"))).strip().lower()
    if prov in {"github", "github_models", "github-models", "copilot"}:
        key = str(cfg.get("github_models_api_key", "") or "").strip()
        if not key:
            key = str(cfg.get("github_token", "") or "").strip()
        if not key:
            key = str(os.environ.get("GITHUB_TOKEN", "") or "").strip()
        return key

    # default to groq
    return normalize_api_key(cfg.get("groq_api_key", "") or "")


def subprocess_no_window_kwargs() -> dict:
    """Extra kwargs so a subprocess never flashes a console window (Windows).

    Console apps (nvidia-smi, powershell, cmd, msg, ffmpeg, ...) briefly pop
    a new console window on Windows when spawned without this flag — over a
    desktop session that blinks and steals focus. Pass as
    ``**subprocess_no_window_kwargs()`` into ``subprocess.run`` / ``Popen``
    for background work. Returns {} on non-Windows, so call sites stay
    cross-platform.
    """
    if sys.platform == "win32":
        return {"creationflags": 0x08000000}  # CREATE_NO_WINDOW
    return {}
