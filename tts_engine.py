"""
tts_engine.py -- Local, free text-to-speech using Piper.

Runs entirely on-device: no API key, no internet, no billing.
Produces 16-bit PCM audio that main.py streams straight to the
speakers via sounddevice, matching the same output pipeline the
old Gemini Live audio used.
"""

import io
import wave
import subprocess
from pathlib import Path

# Path to the downloaded Piper voice model (.onnx) and its .onnx.json config.
# Download once via: https://github.com/rhasspy/piper/releases
# British male voice for a proper "Jarvis" accent -- calm, clear, RP-leaning.
PIPER_VOICE_MODEL = Path(__file__).resolve().parent / "voices" / "en_GB-alan-medium.onnx"
PIPER_EXECUTABLE  = "piper"   # assumes `pip install piper-tts` put this on PATH

OUTPUT_SAMPLE_RATE = 22050    # Piper's default output rate


def synthesize_to_pcm(text: str) -> bytes:
    """
    Runs Piper on `text` and returns raw 16-bit PCM mono audio bytes,
    ready to be written straight to a sounddevice output stream.
    """
    if not text or not text.strip():
        return b""

    if not PIPER_VOICE_MODEL.exists():
        raise RuntimeError(
            f"Piper voice model not found at {PIPER_VOICE_MODEL}. "
            "Download a voice from https://github.com/rhasspy/piper/releases "
            "and place the .onnx + .onnx.json files in the 'voices' folder."
        )

    proc = subprocess.run(
        [
            PIPER_EXECUTABLE,
            "--model", str(PIPER_VOICE_MODEL),
            "--output_raw",
        ],
        input=text.encode("utf-8"),
        capture_output=True,
        check=True,
    )
    return proc.stdout


def synthesize_to_wav_bytes(text: str) -> bytes:
    """Convenience helper: wraps the raw PCM in a proper WAV container."""
    pcm = synthesize_to_pcm(text)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(OUTPUT_SAMPLE_RATE)
        wf.writeframes(pcm)
    return buf.getvalue()
