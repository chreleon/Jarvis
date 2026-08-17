"""
stt_engine.py -- Local, free speech-to-text using faster-whisper.

Runs entirely on-device: no API key, no internet, no billing, no
"policy violation" walls. Auto-tunes model size and compute type
based on available system RAM at import time.

Auto-detected tiers (tested on Windows 11, 4-core):
  < 4 GB  RAM  -> tiny + float32  (only stable option on low-end)
  4-8 GB  RAM  -> base + float32
  8-16 GB RAM  -> small + int8
  16+ GB  RAM  -> medium + int8
"""

import os

# Tame MKL / OpenBLAS thread pool BEFORE importing ctranslate2/faster-whisper.
# On Windows, the int8 compute path can trigger mkl_malloc / OpenBLAS memory
# allocation failures. Limiting threads avoids oversubscribing the 4-core CPU.
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("MKL_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "2")

import threading

import numpy as np
from faster_whisper import WhisperModel

# ── Auto-detect best model size based on total system RAM ──
def _auto_select_config() -> tuple[str, str]:
    """Pick (model_size, compute_type) based on available RAM.

    Tested on this specific machine (3.7 GB RAM, 4 cores, Windows 11):
      - int8        -> mkl_malloc crash on both load and transcribe
      - int8_float16 -> backend doesn't support it on CPU
      - base + f32  -> mkl_malloc crash (model too large for 4 GB)
      - tiny + f32  -> stable and reliable

    On higher-RAM machines, base/int8 unlock better accuracy.
    """
    try:
        import psutil
        total_gb = psutil.virtual_memory().total / (1024 ** 3)
    except ImportError:
        total_gb = 4.0  # conservative fallback

    if total_gb >= 16:
        return ("medium", "int8")
    elif total_gb >= 8:
        return ("small", "int8")
    elif total_gb >= 5:
        return ("base", "float32")
    else:
        return ("tiny", "float32")  # < 5 GB


STT_MODEL_SIZE, STT_COMPUTE = _auto_select_config()
STT_DEVICE = "cpu"   # "cuda" if an NVIDIA GPU is available

print(f"[STT] Auto-selected: {STT_MODEL_SIZE} / {STT_COMPUTE}")

_model = None
_model_lock = threading.Lock()   # guards lazy load (startup prewarm may race first transcribe)


def _get_model() -> WhisperModel:
    global _model
    if _model is not None:
        return _model
    with _model_lock:
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
        best_of=1,
        vad_filter=True,
        vad_parameters=dict(
            threshold=0.5,              # speech threshold (default 0.5)
            min_speech_duration_ms=100,  # ignore pops shorter than 100ms
            min_silence_duration_ms=400, # split segments at 400ms pauses
            speech_pad_ms=300,           # pad segments by 300ms either side
        ),
        condition_on_previous_text=False,  # saves memory; VAD keeps segments short anyway
        no_speech_threshold=0.6,           # skip segments likely to be silence/noise
        compression_ratio_threshold=2.0,   # filter garbled audio (default 2.4)
        log_prob_threshold=-1.0,           # skip very low-confidence segments
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


if __name__ == "__main__":
    _get_model()