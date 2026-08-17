import asyncio
import base64
import io
import json
import re
import os
import sys
import time
import threading
import sounddevice as sd
import numpy as np
from pathlib import Path

# cv2 (OpenCV) and mss are heavy (~10s cold import, ~26MB combined) and
# only needed for camera/screenshot capture. They're loaded lazily on first
# use instead of at module import, so importing this module (which happens
# for every tool call via _load_runtime_imports) stays cheap. Call sites use
# the _get_cv2()/_get_mss() accessors below — a PEP 562 module __getattr__
# alone would NOT work here: bare `cv2.`/`mss.` globals inside this module's
# own functions are resolved via LOAD_GLOBAL, which never consults module
# __getattr__ (observed NameError: 'mss' is not defined).
_cv2 = None
_mss = None

def _get_cv2():
    """Return the lazily-imported OpenCV module."""
    global _cv2
    if _cv2 is None:
        import cv2
        _cv2 = cv2
    return _cv2

def _get_mss():
    """Return the lazily-imported mss module (+ mss.tools)."""
    global _mss
    if _mss is None:
        import mss
        import mss.tools  # noqa: F401  (needed for mss.tools.to_png)
        _mss = mss
    return _mss

try:
    import PIL.Image
    _PIL_OK = True
except ImportError:
    _PIL_OK = False

# Gemini Live is optional. When the genai package is unavailable, screen
# capture still works and analysis falls back to the shared brain client
# (Groq vision / GitHub gpt-4.1).
#
# IMPORTANT (perf): the google-genai SDK costs ~14s of cold import time, so
# it is NOT imported at module load — _ensure_genai() loads it lazily the
# first time a live vision session is actually requested.
genai = None
types = None
_GENAI_OK = False
_genai_loaded = False


def _ensure_genai():
    """Lazily import the google-genai SDK (14s cold). Returns _GENAI_OK."""
    global genai, types, _GENAI_OK, _genai_loaded
    if not _genai_loaded:
        _genai_loaded = True
        try:
            from google import genai
            from google.genai import types
            _GENAI_OK = True
        except Exception:
            genai = None
            types = None
            _GENAI_OK = False
    return _GENAI_OK

from core.utils import get_base_dir

BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

LIVE_MODEL          = "models/gemini-2.5-flash-native-audio-preview-12-2025"
CHANNELS            = 1
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE          = 1024

IMG_MAX_W = 640
IMG_MAX_H = 360
JPEG_Q    = 55

# How long to wait for the live session's spoken answer to arrive as a
# transcript before falling back to the still-image analysis (which returns
# text synchronously). The remote WhatsApp dashboard needs the description
# as text, so the transcript is what gets returned.
VISION_TEXT_TIMEOUT = 30

SYSTEM_PROMPT = (
    "You are JEEVES like Jarvis from Iron Man movies. "
    "Analyze images with technical precision and intelligence. "
    "Help the user in a way they can understand — don't be overly complex. "
    "Be concise, smart, and helpful like Tony Stark's AI assistant. "
    "Respond in maximum 2 short sentences. Speed is priority. "
    "Address the user as 'sir' for a tone of respect. "
    "Ask if the user needs any further help with their problem."
)


def _get_api_key() -> str:
    """Load the Gemini API key needed for the live vision session.

    Vision uses Google's genai SDK directly, so it needs the actual
    gemini_api_key (not a Groq/GitHub key).
    """
    try:
        with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
            keys = json.load(f)
        key = keys.get("gemini_api_key", "") or ""
        if not key:
            raise ValueError(
                "gemini_api_key not found in config/api_keys.json. "
                "The vision module requires a valid Gemini API key."
            )
        return key
    except Exception as e:
        raise RuntimeError(f"Could not load API key: {e}")


def _get_camera_index() -> int:
    try:
        with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if "camera_index" in cfg:
            return int(cfg["camera_index"])
    except Exception:
        pass

    print("[Camera] 🔍 No camera index in config. Auto-detecting...")
    best_index = 0

    cv2 = _get_cv2()
    for idx in range(6):
        cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap.release()
            continue
        for _ in range(5):
            cap.read()
        ret, frame = cap.read()
        cap.release()
        if ret and frame is not None and frame.mean() > 5:
            best_index = idx
            print(f"[Camera] ✅ Camera found at index {idx} — saving to config.")
            break
        else:
            print(f"[Camera] ⚠️  Index {idx}: no valid frame.")

    try:
        cfg = {}
        if API_CONFIG_PATH.exists():
            with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        cfg["camera_index"] = best_index
        with open(API_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4)
        print(f"[Camera] 💾 Camera index {best_index} saved to config.")
    except Exception as e:
        print(f"[Camera] ⚠️  Could not save camera index: {e}")

    return best_index


def _to_jpeg(img_bytes: bytes) -> bytes:
    if not _PIL_OK:
        return img_bytes
    img = PIL.Image.open(io.BytesIO(img_bytes)).convert("RGB")
    img.thumbnail([IMG_MAX_W, IMG_MAX_H], PIL.Image.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_Q, optimize=False)
    return buf.getvalue()


def _capture_screenshot() -> bytes:
    mss = _get_mss()
    with mss.mss() as sct:
        shot      = sct.grab(sct.monitors[1])
        png_bytes = mss.tools.to_png(shot.rgb, shot.size)
    return _to_jpeg(png_bytes)


def _capture_camera() -> bytes:
    cv2 = _get_cv2()
    camera_index = _get_camera_index()
    cap = cv2.VideoCapture(camera_index, cv2.CAP_DSHOW)
    if not cap.isOpened():
        raise RuntimeError(f"Camera could not be opened: index {camera_index}")
    for _ in range(10):
        cap.read()
    ret, frame = cap.read()
    cap.release()
    if not ret or frame is None:
        raise RuntimeError("Could not capture camera frame.")
    if _PIL_OK:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = PIL.Image.fromarray(rgb)
        img.thumbnail([IMG_MAX_W, IMG_MAX_H], PIL.Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_Q, optimize=False)
        return buf.getvalue()
    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_Q])
    return buf.tobytes()


class _LiveSession:

    def __init__(self):
        self._loop:      asyncio.AbstractEventLoop | None = None
        self._thread:    threading.Thread | None          = None
        self._session                                     = None
        self._out_queue: asyncio.Queue | None             = None
        self._audio_in:  asyncio.Queue | None             = None
        self._ready:     threading.Event                  = threading.Event()
        self._player                                      = None
        self._send_lock: asyncio.Lock | None              = None
        # Last completed spoken answer as text (set on turn_complete in the
        # session's asyncio thread; read cross-thread by screen_process so
        # the analysis can be returned to callers).
        self._last_text: str | None                       = None

    def start(self, player=None):
        if self._thread and self._thread.is_alive():
            return
        self._player = player
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="VisionSessionThread"
        )
        self._thread.start()
        ok = self._ready.wait(timeout=20)
        if not ok:
            raise RuntimeError("Vision session did not start within 20s.")
        print("[ScreenProcess] ✅ Vision session ready (no mic)")

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._main())

    async def _main(self):
        self._out_queue = asyncio.Queue(maxsize=30)
        self._audio_in  = asyncio.Queue()
        self._send_lock = asyncio.Lock()

        _ensure_genai()  # lazy 14s SDK import — only paid for live sessions
        client = genai.Client(
            api_key=_get_api_key(),
            http_options={"api_version": "v1beta"}
        )

        config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            system_instruction=SYSTEM_PROMPT,
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name="Charon"
                    )
                )
            ),
        )

        while True:
            try:
                print("[ScreenProcess] 🔌 Vision session connecting...")
                async with client.aio.live.connect(model=LIVE_MODEL, config=config) as session:
                    self._session = session
                    self._ready.set()
                    print("[ScreenProcess] ✅ Vision session connected")
                    async with asyncio.TaskGroup() as tg:
                        tg.create_task(self._send_loop())
                        tg.create_task(self._recv_loop())
                        tg.create_task(self._play_loop())
            except Exception as e:
                print(f"[ScreenProcess] ⚠️ Disconnected: {e} — reconnecting...")
                self._session = None
                self._ready.clear()
                await asyncio.sleep(2)
                self._ready.set()

    async def _send_loop(self):
        while True:
            item = await self._out_queue.get()
            if self._session:
                image_bytes, mime_type, user_text = item
                try:
                    b64 = base64.b64encode(image_bytes).decode("utf-8")
                    await self._session.send_client_content(
                        turns={
                            "parts": [
                                {"inline_data": {"mime_type": mime_type, "data": b64}},
                                {"text": user_text}
                            ]
                        },
                        turn_complete=True
                    )
                    print("[ScreenProcess] ✅ Image sent")
                except Exception as e:
                    print(f"[ScreenProcess] ⚠️ Send error: {e}")

    async def _recv_loop(self):
        transcript_buf: list[str] = []
        try:
            async for response in self._session.receive():
                if response.data:
                    await self._audio_in.put(response.data)
                sc = response.server_content
                if not sc:
                    continue
                if sc.output_transcription and sc.output_transcription.text:
                    chunk = sc.output_transcription.text.strip()
                    if chunk:
                        transcript_buf.append(chunk)
                if sc.turn_complete:
                    if transcript_buf:
                        full = re.sub(r'\s+', ' ', " ".join(transcript_buf)).strip()
                        if full:
                            self._last_text = full   # readable by callers
                            if self._player:
                                self._player.write_log(f"Jeeves: {full}")
                            print(f"[ScreenProcess] 💬 {full}")
                    transcript_buf = []
        except Exception as e:
            print(f"[ScreenProcess] ⚠️ Recv error: {e}")
            transcript_buf = []
            await asyncio.sleep(0.3)

    async def _play_loop(self):
        stream = sd.RawOutputStream(
            samplerate=RECEIVE_SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=CHUNK_SIZE,
        )
        stream.start()
        try:
            while True:
                chunk = await self._audio_in.get()
                await asyncio.to_thread(stream.write, chunk)
        except Exception as e:
            print(f"[ScreenProcess] ❌ Play error: {e}")
            raise
        finally:
            stream.stop()
            stream.close()

    @property
    def last_text(self) -> str | None:
        """The last completed spoken answer as text (None before any reply).

        Written in the session's asyncio thread on turn_complete, read
        cross-thread by screen_process so the analysis can be returned to
        callers."""
        return self._last_text

    def analyze(self, image_bytes: bytes, mime_type: str, user_text: str):
        if not self._loop:
            return
        asyncio.run_coroutine_threadsafe(
            self._out_queue.put((image_bytes, mime_type, user_text)),
            self._loop
        )

    def is_ready(self) -> bool:
        return self._session is not None


_live       = _LiveSession()
_started    = False
_start_lock = threading.Lock()


def _ensure_started(player=None):
    global _started
    with _start_lock:
        if not _started:
            _live.start(player=player)
            _started = True
        elif player is not None:
            _live._player = player


def _clean_vision_reply(reply: str) -> str:
    """Strip a model's <think>…</think> reasoning block, leaving the answer.

    Some vision models return their chain-of-thought wrapped in think tags;
    that internal reasoning is noise for the user (and for the WhatsApp
    remote dashboard the reply is shown verbatim)."""
    reply = (reply or "").strip()
    if "<think" in reply.lower():
        reply = re.sub(r"<think[^>]*>.*?</think>", "", reply,
                       flags=re.DOTALL | re.IGNORECASE).strip()
    return reply


def _analyze_still(image_bytes: bytes, mime_type: str, user_text: str) -> str:
    """Analyze a captured frame through the shared brain client (no Gemini).

    Used when the Gemini Live package or gemini_api_key is unavailable so
    screen analysis still produces an answer instead of failing silently.
    """
    try:
        from or_client import client

        b64 = base64.b64encode(image_bytes).decode("utf-8")
        reply = _clean_vision_reply(client.vision(user_text, b64, mime=mime_type))
        print(f"[ScreenProcess] 💬 {reply}")
        return reply
    except Exception as e:
        print(f"[ScreenProcess] ❌ Vision fallback failed: {e}")
        return ""


def screen_process(
    parameters:     dict,
    response:       str | None = None,
    player=None,
    session_memory=None,
) -> str | bool:
    """Capture and analyze the screen or camera.

    Returns the analysis TEXT (str) on success so callers can display it —
    the remote WhatsApp dashboard shows the actual description instead of an
    "activated" stub. The Gemini Live session also speaks the answer out
    loud; its transcript becomes the returned text, and if it doesn't arrive
    in time the still-image analysis (shared brain) produces the text
    instead. Returns False on failure.
    """
    user_text = (parameters or {}).get("text") or (parameters or {}).get("user_text", "")
    user_text = (user_text or "").strip()
    if not user_text:
        print("[ScreenProcess] ⚠️ No user_text provided.")
        return False

    angle = (parameters or {}).get("angle", "screen").lower().strip()
    print(f"[ScreenProcess] angle={angle!r}  text={user_text!r}")

    try:
        if angle == "camera":
            image_bytes = _capture_camera()
            mime_type   = "image/jpeg"
            print("[ScreenProcess] 📷 Camera captured")
        else:
            image_bytes = _capture_screenshot()
            mime_type   = "image/jpeg" if _PIL_OK else "image/png"
            print("[ScreenProcess] 🖥️ Screen captured")
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[ScreenProcess] ❌ Capture error: {e}")
        return False

    print(f"[ScreenProcess] 📦 {len(image_bytes)} bytes → analyzing")

    # Prefer the Gemini Live session when both the package and a valid API
    # key exist; otherwise analyze the still image through the shared brain.
    # The live session is only started once its prerequisites are confirmed:
    # starting it unconditionally (as before) made the still-image fallback
    # unreachable — a missing key/SDK crashed the session thread and the
    # 20s startup timeout propagated instead of degrading gracefully.
    use_live = _ensure_genai()
    if use_live:
        try:
            _get_api_key()
        except Exception:
            use_live = False

    if use_live:
        try:
            _ensure_started(player=player)
        except Exception as e:
            print(f"[ScreenProcess] ⚠️ Live session unavailable ({e}) — using still-image analysis")
            use_live = False

    if use_live:
        before = _live.last_text
        _live.analyze(image_bytes, mime_type, user_text)
        # The live session speaks the answer out loud; wait for its
        # transcript so the TEXT is returned too (the remote WhatsApp
        # dashboard needs the description).
        deadline = time.time() + VISION_TEXT_TIMEOUT
        while time.time() < deadline:
            if _live.last_text != before:
                return _live.last_text
            time.sleep(0.5)
        print("[ScreenProcess] ⚠️ Live transcript timed out — analyzing still image")
        return _analyze_still(image_bytes, mime_type, user_text)

    return _analyze_still(image_bytes, mime_type, user_text)


def warmup_session(player=None):
    try:
        _ensure_started(player=player)
    except Exception as e:
        print(f"[ScreenProcess] ⚠️ Warmup error: {e}")


if __name__ == "__main__":
    print("[TEST] screen_processor.py v8 — image-only session")
    print("=" * 50)
    mode    = input("screen / camera (default: screen): ").strip().lower() or "screen"
    request = input("Question (Enter for default): ").strip() or "What do you see? Be brief."

    t0 = time.perf_counter()
    warmup_session()
    print(f"Session ready — {time.perf_counter()-t0:.2f}s\n")

    t1     = time.perf_counter()
    result = screen_process({"angle": mode, "text": request}, player=None)
    print(f"Sent — {time.perf_counter()-t1:.3f}s | audio incoming...")
    time.sleep(8)
    print(f"\n{'✅' if result else '❌'}")
