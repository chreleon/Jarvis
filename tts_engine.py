"""
tts_engine.py -- Local, free text-to-speech (Piper, with an EdgeTTS fallback).

Primary engine: Piper runs entirely on-device (no API key, no internet,
no billing) and produces 16-bit PCM audio that main.py streams straight
to the speakers via sounddevice.

Fallback engine: if the Piper voice model files are missing, the engine
factory automatically switches to Microsoft Edge's free online TTS
(edge-tts) so Jeeves keeps talking. It is decoded to the same 16-bit
mono PCM stream, so nothing downstream changes.

Public PCM streaming API (unchanged):
    warm_up()
    synthesize_to_pcm(text) -> bytes
    synthesize_to_pcm_chunks(text) -> yields PCM chunks + b"" sentinel
    synthesize_to_wav_bytes(text) -> bytes
    synthesize_to_wav_file(text, path) -> Path
"""

import argparse
import io
import sys
import wave
import threading
import subprocess
from pathlib import Path

# Path to the downloaded Piper voice model (.onnx) and its .onnx.json config.
# Download once via: https://github.com/rhasspy/piper/releases
# British voice for a proper "Jeeves" accent -- calm and clear.
PIPER_VOICE_MODEL = Path(__file__).resolve().parent / "voices" / "en_GB-jenny_dioco-medium.onnx"

OUTPUT_SAMPLE_RATE = 22050    # Piper's default output rate
PCM_STREAM_CHUNK_SIZE = 4096
PCM_SENTINEL = b""           # empty bytes signals end-of-utterance


class PiperEngine:
    """Persistent Piper TTS engine that keeps the ONNX model loaded in memory.

    Lazily loads the model on first use (takes ~6s once) and reuses it
    for all subsequent synthesis calls. Thread-safe via a lock.
    """

    def __init__(self):
        self._voice = None
        self._lock = threading.Lock()

    def _ensure_loaded(self):
        if self._voice is not None:
            return
        with self._lock:
            if self._voice is not None:
                return
            if not PIPER_VOICE_MODEL.exists():
                raise RuntimeError(
                    f"Piper voice model not found at {PIPER_VOICE_MODEL}. "
                    "Download a voice from https://github.com/rhasspy/piper/releases "
                    "and place the .onnx + .onnx.json files in the 'voices' folder."
                )
            from piper.voice import PiperVoice
            self._voice = PiperVoice.load(str(PIPER_VOICE_MODEL))

    def warm_up(self):
        """Pre-load the model into memory. Safe to call multiple times."""
        self._ensure_loaded()

    @property
    def voice(self):
        self._ensure_loaded()
        return self._voice

    def synthesize_stream(self, text: str):
        """Yield raw 16-bit PCM mono bytes, one sentence at a time.

        Each yielded chunk corresponds to one sentence of input text,
        enabling true streaming playback — the first sentence plays
        while subsequent sentences are still being synthesized.

        After all sentences are done, yields PCM_SENTINEL (b"").
        """
        if not text or not text.strip():
            yield PCM_SENTINEL
            return

        for chunk in self.voice.synthesize(text):
            raw = chunk.audio_int16_bytes
            if raw:
                # Break each sentence's audio into smaller chunks for the
                # audio output stream to avoid blocking for too long.
                for i in range(0, len(raw), PCM_STREAM_CHUNK_SIZE):
                    yield raw[i:i + PCM_STREAM_CHUNK_SIZE]

        yield PCM_SENTINEL


class EdgeTTSEngine:
    """Fallback TTS engine using Microsoft Edge's free online voices.

    Only used when the Piper voice model is missing. Synthesizes via
    edge-tts, decodes the MP3 to the same 16-bit mono PCM stream at
    OUTPUT_SAMPLE_RATE, and yields it in the same chunk format, so the
    rest of the app is completely unaware which engine is speaking.

    Decoding order: PyAV (av) → pydub → ffmpeg binary. Requires at least
    one of those, plus `edge-tts`.
    """

    VOICE = "en-GB-RyanNeural"  # British male — closest to a proper "Jeeves"

    def __init__(self):
        self._lock = threading.Lock()

    def warm_up(self):
        """Verify edge-tts is importable. Raises RuntimeError if not."""
        import edge_tts  # noqa: F401

    def _synthesize_mp3(self, text: str) -> bytes:
        import asyncio
        import edge_tts

        async def _run():
            communicate = edge_tts.Communicate(text, self.VOICE)
            parts = []
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    parts.append(chunk["data"])
            return b"".join(parts)

        return asyncio.run(_run())

    def _decode_mp3(self, mp3: bytes) -> bytes:
        """Decode MP3 bytes → raw s16le mono PCM at OUTPUT_SAMPLE_RATE."""
        try:
            import av  # PyAV — bundled ffmpeg, pip-installable

            container = av.open(io.BytesIO(mp3))
            resampler = av.AudioResampler(
                format="s16", layout="mono", rate=OUTPUT_SAMPLE_RATE
            )
            out = bytearray()
            for frame in container.decode(audio=0):
                for rframe in resampler.resample(frame):
                    out += rframe.to_ndarray().tobytes()
            for rframe in resampler.resample(None):
                out += rframe.to_ndarray().tobytes()
            if out:
                return bytes(out)
        except ImportError:
            pass
        except Exception:
            pass

        # Fallback 1: pydub (needs ffmpeg on PATH)
        try:
            from pydub import AudioSegment

            seg = AudioSegment.from_file(io.BytesIO(mp3), format="mp3")
            seg = seg.set_frame_rate(OUTPUT_SAMPLE_RATE).set_channels(1).set_sample_width(2)
            return seg.raw_data
        except Exception:
            pass

        # Fallback 2: ffmpeg binary on PATH
        try:
            proc = subprocess.run(
                ["ffmpeg", "-i", "pipe:0", "-f", "s16le", "-ac", "1",
                 "-ar", str(OUTPUT_SAMPLE_RATE), "pipe:1"],
                input=mp3, capture_output=True, check=True,
                # A wedged ffmpeg must never hang the TTS hot path forever —
                # on timeout we fall through to the MP3-decoder error below.
                timeout=30,
            )
            if proc.stdout:
                return proc.stdout
        except Exception:
            pass

        raise RuntimeError(
            "EdgeTTS fallback needs an MP3 decoder: pip install av "
            "(or pydub + ffmpeg)."
        )

    def synthesize_stream(self, text: str):
        """Yield raw 16-bit PCM mono bytes (whole utterance chunked)."""
        if not text or not text.strip():
            yield PCM_SENTINEL
            return

        mp3 = self._synthesize_mp3(text)
        pcm = self._decode_mp3(mp3)
        for i in range(0, len(pcm), PCM_STREAM_CHUNK_SIZE):
            yield pcm[i:i + PCM_STREAM_CHUNK_SIZE]

        yield PCM_SENTINEL


# ── Engine factory ───────────────────────────────────────────────────────────

def _pick_engine():
    """Pick the best available engine: Piper first, then EdgeTTS."""
    if PIPER_VOICE_MODEL.exists():
        return PiperEngine()
    try:
        import edge_tts  # noqa: F401
        print("[TTS] ⚠️ Piper voice missing — falling back to EdgeTTS.")
        return EdgeTTSEngine()
    except ImportError:
        raise RuntimeError(
            f"Piper voice model not found at {PIPER_VOICE_MODEL} and "
            "edge-tts is not installed. Download a Piper voice or run "
            "`pip install edge-tts`."
        )


# Global singleton engine — loaded once, used forever.
_engine = None
_engine_lock = threading.Lock()


def _get_engine():
    """Return the global engine singleton (lazily created)."""
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = _pick_engine()
    return _engine


def warm_up():
    """Pre-load the TTS engine at startup.

    Call this once at startup (in a background thread) to avoid the
    model-loading delay on the first TTS call. Safe to call multiple
    times — only the first call loads the engine. Never raises.
    """
    try:
        _get_engine().warm_up()
    except Exception as e:
        print(f"[TTS] ⚠️ warm-up failed: {e}")


def synthesize_to_pcm(text: str) -> bytes:
    """Return all raw 16-bit PCM mono audio for *text* as a single blob.

    Convenience helper. For streaming playback prefer
    ``synthesize_to_pcm_chunks()``.
    """
    return b"".join(
        chunk for chunk in _get_engine().synthesize_stream(text)
        if chunk  # skip the sentinel
    )


def synthesize_to_pcm_chunks(text: str, chunk_size: int = PCM_STREAM_CHUNK_SIZE):
    """Yield raw PCM chunks using the persistent Piper engine.

    This is the streaming workhorse: it yields audio chunks as they
    become available from the sentence-by-sentence synthesizer, and
    finishes with a PCM_SENTINEL (b"") to signal utterance end.

    The *chunk_size* parameter is kept for backward compatibility but
    the engine's internal chunking is used instead.
    """
    yield from _get_engine().synthesize_stream(text)


def synthesize_to_wav_bytes(text: str) -> bytes:
    """Convenience helper: wraps the raw PCM in a proper WAV container."""
    engine = _get_engine()
    pcm = b"".join(
        chunk for chunk in engine.synthesize_stream(text)
        if chunk
    )
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(OUTPUT_SAMPLE_RATE)
        wf.writeframes(pcm)
    return buf.getvalue()


def synthesize_to_wav_file(text: str, output_path: Path) -> Path:
    """Write synthesized speech to a .wav file and return the saved path."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(synthesize_to_wav_bytes(text))
    return output_path


def _play_wav_bytes(wav_bytes: bytes) -> None:
    """Play synthesized audio locally if sounddevice is installed."""
    try:
        import numpy as np
        import sounddevice as sd
    except ImportError as exc:
        raise RuntimeError(
            "sounddevice is required for --play. Install it or omit --play."
        ) from exc

    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
        sample_rate = wf.getframerate()

        sd.play(pcm, samplerate=sample_rate)
    sd.wait()


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate local text-to-speech audio with Piper."
    )
    parser.add_argument(
        "text",
        nargs="*",
        help="Text to synthesize. If omitted, read from standard input.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent / "voices" / "custom_tts.wav",
        help="Path to the output WAV file.",
    )
    parser.add_argument(
        "--play",
        action="store_true",
        help="Play the generated audio locally after saving it.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    text = " ".join(args.text).strip()
    if not text:
        text = sys.stdin.read().strip()

    if not text:
        raise SystemExit("No text provided. Pass text as an argument or via stdin.")

    output_path = synthesize_to_wav_file(text, args.output)
    print(f"Saved TTS audio to {output_path}")

    if args.play:
        _play_wav_bytes(output_path.read_bytes())
        print("Playback complete.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
