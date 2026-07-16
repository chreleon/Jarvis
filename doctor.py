"""
doctor.py -- Jeeves' own health-check command.

Run this any time something feels off, or before a fresh launch, to get a
clear pass/fail report instead of guessing from a stack trace.

Usage:
    python doctor.py
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


BASE_DIR = _base_dir()
API_KEY_PATH = BASE_DIR / "config" / "api_keys.json"
VOICES_DIR = BASE_DIR / "voices"
VOICE_NAME = "en_GB-alan-medium"

CHECK = "\u2705"
CROSS = "\u274c"
WARN = "\u26a0\ufe0f"

_results = []  # (label, ok: bool|None, detail: str)


def _report(label: str, ok, detail: str = ""):
    _results.append((label, ok, detail))
    symbol = CHECK if ok is True else (WARN if ok is None else CROSS)
    line = f"{symbol}  {label}"
    if detail:
        line += f" -- {detail}"
    print(line)


def check_config_file():
    if not API_KEY_PATH.exists():
        _report("config/api_keys.json", False, "not found -- run main.py once to create it via the setup screen")
        return {}
    try:
        data = json.loads(API_KEY_PATH.read_text(encoding="utf-8"))
        _report("config/api_keys.json", True, "found and readable")
        return data
    except Exception as e:
        _report("config/api_keys.json", False, f"exists but invalid JSON: {e}")
        return {}


def check_groq_key(config: dict):
    key = config.get("groq_api_key", "").strip()
    if not key:
        _report("Groq API key", False, "missing from config/api_keys.json")
        return

    try:
        from groq import Groq
        client = Groq(api_key=key)
        client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
        )
        _report("Groq API key", True, "valid, responded successfully")
    except ImportError:
        _report("Groq API key", None, "key present, but 'groq' package not installed -- can't verify")
    except Exception as e:
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
        from composio_openai import ComposioToolSet, App
    except ImportError:
        _report("Composio connections", None, "composio_openai not installed -- skipping")
        return

    try:
        toolset = ComposioToolSet()
        for app_name in ("GITHUB", "GMAIL", "GOOGLECALENDAR"):
            app_enum = getattr(App, app_name)
            try:
                tools = toolset.get_tools(apps=[app_enum])
                ok = bool(tools)
                _report(f"Composio: {app_name.title()}", ok,
                        "tools available" if ok else "no tools returned -- may not be connected")
            except Exception as e:
                _report(f"Composio: {app_name.title()}", False, f"{e}")
    except Exception as e:
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
    print("J.E.E.V.E.S. Doctor -- system health check")
    print("=" * 50)

    config = check_config_file()
    check_groq_key(config)
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
    print("=" * 50)


if __name__ == "__main__":
    run_all_checks()
