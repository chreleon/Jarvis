"""
voice_downloader.py -- Fetches the Piper voice model files Jeeves needs to
speak, straight from the official Piper voice repository, into the local
voices/ folder. Used by the setup screen so the person doesn't have to do
this by hand.
"""

import sys
import threading
import urllib.request
from pathlib import Path

def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

VOICES_DIR = _base_dir() / "voices"

VOICE_NAME = "en_GB-alan-medium"
_RELEASE_BASE = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alan/medium"
)
FILES = {
    f"{VOICE_NAME}.onnx":      f"{_RELEASE_BASE}/{VOICE_NAME}.onnx",
    f"{VOICE_NAME}.onnx.json": f"{_RELEASE_BASE}/{VOICE_NAME}.onnx.json",
}


def voice_model_present() -> bool:
    return all((VOICES_DIR / name).exists() for name in FILES)


def download_voice_model(status_callback=None) -> bool:
    """
    Downloads both voice files into voices/. Calls status_callback(str) with
    progress updates if provided. Returns True on success, False on failure.
    Safe to call from a background thread.
    """
    def _report(msg: str):
        if status_callback:
            status_callback(msg)
        print(f"[VoiceDownloader] {msg}")

    try:
        VOICES_DIR.mkdir(parents=True, exist_ok=True)

        if voice_model_present():
            _report("Voice files already present.")
            return True

        for i, (filename, url) in enumerate(FILES.items(), 1):
            dest = VOICES_DIR / filename
            _report(f"Downloading voice file {i}/{len(FILES)}: {filename}...")

            def _hook(block_num, block_size, total_size, _filename=filename):
                if total_size > 0:
                    pct = min(100, block_num * block_size * 100 // total_size)
                    _report(f"Downloading {_filename}... {pct}%")

            urllib.request.urlretrieve(url, dest, reporthook=_hook)

        if voice_model_present():
            _report("Voice files downloaded successfully.")
            return True
        else:
            _report("Download finished but files are missing -- something went wrong.")
            return False

    except Exception as e:
        _report(f"Voice download failed: {e}")
        return False


def download_voice_model_async(status_callback=None, done_callback=None):
    """Runs download_voice_model() in a background thread so the UI doesn't freeze."""
    def _run():
        ok = download_voice_model(status_callback)
        if done_callback:
            done_callback(ok)
    threading.Thread(target=_run, daemon=True).start()


if __name__ == "__main__":
    print("Present already?" , voice_model_present())
    download_voice_model(print)
