"""
stt_engine.py -- Local, free speech-to-text using faster-whisper.

Runs entirely on-device: no API key, no internet, no billing, no
"policy violation" walls. Sized for lower-RAM machines by default
(the "tiny" model), configurable via STT_MODEL_SIZE below.
"""

import numpy as np
from faster_whisper import WhisperModel

# "tiny" ~75MB RAM, "base" ~150MB RAM. Bump to "small"/"medium" only on
# machines with more free RAM (small ~500MB, medium ~1.5GB+).
STT_MODEL_SIZE = "tiny"
STT_DEVICE     = "cpu"          # "cuda" if an NVIDIA GPU is available
STT_COMPUTE    = "int8"         # int8 is fastest/lowest-memory on CPU

_model = None


def _get_model() -> WhisperModel:
    global _model
    if _model is None:
        print(f"[STT] Loading faster-whisper ({STT_MODEL_SIZE}, {STT_DEVICE}/{STT_COMPUTE})...")
        _model = WhisperModel(STT_MODEL_SIZE, device=STT_DEVICE, compute_type=STT_COMPUTE)
        print("[STT] Model ready.")
    return _model


def transcribe_pcm16(pcm_bytes: bytes, sample_rate: int = 16000) -> str:
    """
    Transcribe raw 16-bit PCM mono audio bytes (the same format the mic
    capture in main.py already produces) into text.
    """
    if not pcm_bytes:
        return ""

    audio_np = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0

    model = _get_model()
    segments, _info = model.transcribe(
        audio_np,
        language="en",
        beam_size=1,
        vad_filter=True,
    )
    text = " ".join(seg.text.strip() for seg in segments).strip()
    return text


def transcribe_wav_file(path: str) -> str:
    """Convenience helper for transcribing a saved .wav file."""
    import wave
    with wave.open(path, "rb") as wf:
        pcm_bytes = wf.readframes(wf.getnframes())
        sample_rate = wf.getframerate()
    return transcribe_pcm16(pcm_bytes, sample_rate)
