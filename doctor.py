"""
doctor.py -- Jeeves' own Doctor Strange diagnostic command.

This script behaves like the workspace's diagnostic specialist: it reproduces
runtime issues, surfaces the likely root cause, and gives the next corrective
step instead of only printing a bare pass/fail list.

Usage:
    python doctor.py
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

from core.utils import BASE_DIR, CONFIG_PATH as API_KEY_PATH, normalize_api_key
from composio_shim import ComposioToolSet, App, _load_composio_credentials

# Force UTF-8 output encoding on Windows
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')


VOICES_DIR = BASE_DIR / "voices"
VOICE_NAME = "en_GB-jenny_dioco-medium"

CHECK = "\u2705"
CROSS = "\u274c"
WARN = "\u26a0\ufe0f"

_results = []  # (label, ok: bool|None, detail: str)
_diagnostic_notes = []


def _report(label: str, ok, detail: str = ""):
    _results.append((label, ok, detail))
    symbol = CHECK if ok is True else (WARN if ok is None else CROSS)
    line = f"{symbol}  {label}"
    if detail:
        line += f" -- {detail}"
    print(line)


def _note(message: str):
    _diagnostic_notes.append(message)


def check_config_file():
    if not API_KEY_PATH.exists():
        _note("Likely root cause: the repo config file is missing, so Jeeves cannot load runtime secrets.")
        _report("config/api_keys.json", False, "not found -- run main.py once to create it via the setup screen")
        return {}
    try:
        data = json.loads(API_KEY_PATH.read_text(encoding="utf-8"))
        _report("config/api_keys.json", True, "found and readable")
        return data
    except Exception as e:
        _note("Likely root cause: config/api_keys.json exists but is malformed JSON.")
        _report("config/api_keys.json", False, f"exists but invalid JSON: {e}")
        return {}


def check_brain_key(config: dict):
    prov = str(config.get("brain_provider", "groq")).strip().lower()
    if prov in {"github", "github_models", "github-models", "copilot"}:
        # Validate GitHub Models key presence and do a light health check if possible
        key = config.get("github_models_api_key", "") or config.get("github_token", "") or ""
        key = str(key).strip()
        if not key:
            _note("Likely root cause: GitHub Models key is missing for the selected provider.")
            _report("GitHub Models API key", False, "missing from config/api_keys.json or GITHUB_TOKEN")
            return

        try:
            import requests
            resp = requests.post(
                "https://models.github.ai/inference/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1},
                timeout=10,
            )
            if resp.status_code >= 400:
                _report("GitHub Models API key", False, f"request failed: {resp.status_code} {resp.text[:200]}")
            else:
                _report("GitHub Models API key", True, "valid or accepted (light check)")
        except ImportError:
            _report("GitHub Models API key", None, "key present, but 'requests' not installed -- can't verify")
        except Exception as e:
            _report("GitHub Models API key", False, f"health check failed: {e}")
        return

    # default: groq
    raw = config.get("groq_api_key", "")
    key = normalize_api_key(raw)
    if not key:
        _note("Likely root cause: the Groq runtime key is absent, so the LLM brain will not initialize.")
        _report("Groq API key", False, "missing from config/api_keys.json")
        return
    suffix = ""
    if isinstance(raw, (list, tuple)) and len(raw) > 1:
        suffix = f" ({len(raw)} keys configured — Jeeves rotates between them on rate limits)"

    try:
        from groq import Groq
        from or_client import GROQ_LITE_MODEL
        client = Groq(api_key=key)
        client.chat.completions.create(
            model=GROQ_LITE_MODEL,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
        )
        _report("Groq API key", True, "valid, responded successfully" + suffix)
    except ImportError:
        _note("Likely root cause: the code expects the Groq package, but the environment is missing it.")
        _report("Groq API key", None, "key present, but 'groq' package not installed -- can't verify")
    except Exception as e:
        _note("Likely root cause: the Groq key exists but is being rejected by the provider or the model endpoint.")
        _report("Groq API key", False, f"key present but rejected: {e}")


def check_voice_files():
    onnx = VOICES_DIR / f"{VOICE_NAME}.onnx"
    cfg = VOICES_DIR / f"{VOICE_NAME}.onnx.json"
    if onnx.exists() and cfg.exists():
        _report("Piper voice files", True, f"{VOICE_NAME} present in voices/")
    else:
        missing = [p.name for p in (onnx, cfg) if not p.exists()]
        _report("Piper voice files", False, f"missing: {', '.join(missing)} -- use the setup screen's download button")


def check_piper_executable():
    path = shutil.which("piper")
    if path:
        _report("Piper executable", True, path)
    else:
        _report("Piper executable", False, "not found on PATH -- pip install piper-tts")


def check_python_packages():
    packages = {
        "groq": "groq",
        "faster_whisper": "faster-whisper",
        "PyQt6": "PyQt6",
        "flask": "flask (only needed for web_server.py)",
        "composio_openai": "composio-openai",
    }
    for module_name, pip_name in packages.items():
        try:
            __import__(module_name)
            _report(f"Package: {pip_name}", True)
        except ImportError:
            _report(f"Package: {pip_name}", False, f"not installed -- pip install {pip_name.split(' ')[0]}")


def check_composio_connections():

    try:
        toolset = ComposioToolSet()
        api_key, user_id = _load_composio_credentials()
        if not api_key:
            _note("Likely root cause: Composio auth key was not found in the environment or config fallback.")
        if user_id:
            _note(f"Diagnostic identity: Composio runtime user_id resolved to '{user_id}'.")

        for app_name in ("GITHUB", "GMAIL", "GOOGLECALENDAR"):
            app_enum = getattr(App, app_name)
            try:
                tools = toolset.get_tools(apps=[app_enum])
                ok = bool(tools)
                _report(
                    f"Composio: {app_name.title()}",
                    ok,
                    "tools available" if ok else "no tools returned -- may not be connected",
                )
                if not ok:
                    _note(
                        f"Likely root cause: the Composio SDK loaded the key, but the tool fetch for {app_name} returned no tools. "
                        "This usually means the account is not connected for that app or the SDK contract changed."
                    )
            except Exception as e:
                _note(f"Likely root cause: the Composio tool query failed for {app_name} with a runtime exception.")
                _report(f"Composio: {app_name.title()}", False, f"{e}")
    except Exception as e:
        _note("Likely root cause: the Composio runtime could not initialize at all.")
        _report("Composio connections", False, f"couldn't initialize toolset: {e}")


def check_microphone():
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        inputs = [d for d in devices if d.get("max_input_channels", 0) > 0]
        if inputs:
            _report("Microphone", True, f"{len(inputs)} input device(s) detected")
        else:
            _report("Microphone", False, "no input devices found")
    except Exception as e:
        _report("Microphone", None, f"couldn't query audio devices: {e}")


def run_all_checks():
    print("=" * 50)
    print("J.E.E.V.E.S. Doctor Strange -- runtime diagnosis")
    print("=" * 50)

    config = check_config_file()
    check_brain_key(config)
    check_voice_files()
    check_piper_executable()
    check_python_packages()
    check_composio_connections()
    check_microphone()

    print("=" * 50)
    failed = [r for r in _results if r[1] is False]
    warned = [r for r in _results if r[1] is None]

    if not failed and not warned:
        print(f"{CHECK}  All checks passed. Jeeves should be ready to run.")
    else:
        if failed:
            print(f"{CROSS}  {len(failed)} check(s) failed -- see above for fixes.")
        if warned:
            print(f"{WARN}  {len(warned)} check(s) couldn't be fully verified.")

        print("\nDoctor Strange analysis:")
        print("- Likely root cause: inspect the first failed check above and follow its hint.")
        if _diagnostic_notes:
            print("- Evidence:")
            for note in _diagnostic_notes[:4]:
                print(f"  * {note}")
        print("- Next action: patch the smallest failing dependency or config path, then rerun python doctor.py.")

    print("=" * 50)


if __name__ == "__main__":
    run_all_checks()
