import json
from pathlib import Path

from core.utils import get_base_dir, BASE_DIR, CONFIG_PATH, normalize_api_key

CONFIG_DIR  = BASE_DIR / "config"
CONFIG_FILE = CONFIG_PATH


def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def config_exists() -> bool:
    return CONFIG_FILE.exists()


def save_api_keys(groq_api_key: str | list | tuple = "", github_models_api_key: str = "", gemini_api_key: str = "") -> None:
    """Save one or more API keys to the config file.

    Args:
        groq_api_key: Groq API key(s) (primary LLM provider) — a single
            string or a list of keys. A list is stored as-is so Jeeves can
            rotate across multiple free-tier keys.
        github_models_api_key: GitHub Models API key (alternative LLM provider).
        gemini_api_key: Gemini API key (only needed for vision/screen processing).
    """
    ensure_config_dir()

    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}

    if groq_api_key:
        if isinstance(groq_api_key, (list, tuple)):
            keys = []
            for k in groq_api_key:
                s = str(k).strip()
                if s:
                    keys.append(s)
            if keys:
                data["groq_api_key"] = keys if len(keys) > 1 else keys[0]
        else:
            data["groq_api_key"] = groq_api_key.strip()
    if github_models_api_key:
        data["github_models_api_key"] = github_models_api_key.strip()
    if gemini_api_key:
        data["gemini_api_key"] = gemini_api_key.strip()

    CONFIG_FILE.write_text(
        json.dumps(data, indent=2),
        encoding="utf-8"
    )


def load_api_keys() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"❌ Failed to load api_keys.json: {e}")
        return {}


def get_gemini_key() -> str | None:
    return normalize_api_key(load_api_keys().get("gemini_api_key", "") or "") \
        or normalize_api_key(load_api_keys().get("groq_api_key", "") or "")


def is_configured() -> bool:
    """Check if at least one LLM provider key is configured."""
    keys = load_api_keys()
    groq_key = normalize_api_key(keys.get("groq_api_key", "") or "")
    github_key = normalize_api_key(keys.get("github_models_api_key", "") or "")
    gemini_key = normalize_api_key(keys.get("gemini_api_key", "") or "")
    return bool((groq_key and len(groq_key) > 15) or (github_key and len(github_key) > 15) or (gemini_key and len(gemini_key) > 15))