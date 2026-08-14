import asyncio
import threading
import json
import sys
import time
import traceback
import atexit
from pathlib import Path

from ui import JeevesUI
from dashboard_server import DashboardServer
from memory.memory_manager import (
    load_memory, update_memory, format_memory_for_prompt,
    should_extract_memory, extract_memory
)
from memory_cleanup import cleanup as cleanup_jeeves
from config.tool_definitions import TOOL_DECLARATIONS
from core.utils import get_provider_api_key, normalize_api_key

# NOTE: action modules (file_processor, browser_control, composio_agent, ...)
# are intentionally NOT imported at module level — they're loaded lazily via
# _load_runtime_imports() so startup stays fast. composio_agent additionally
# gets a defensive guard inside _execute_tool so a broken Composio SDK can
# never crash the whole app.


def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
PROMPT_PATH     = BASE_DIR / "core" / "prompt.txt"
CHANNELS            = 1
SEND_SAMPLE_RATE    = 16000
RECEIVE_SAMPLE_RATE = 22050
CHUNK_SIZE          = 1024

SILENCE_RMS_THRESHOLD = 400
SILENCE_HANG_MS        = 900
MIN_UTTERANCE_MS       = 300

VISION_COOLDOWN_S      = 12   # minimum gap between screen_process calls

# Hard ceilings so a slow/hung call can never silence Jeeves for good.
BRAIN_TIMEOUT_S        = 240  # LLM reasoning call (voice turn or tool follow-up)
# 240s (not 90s) so the Groq key pool can exhaust ALL keys through its
# recovery windows (RECOVERY_RETRIES x MAX_RECOVERY_WAIT_S ~= 210s) without
# the app cutting the turn off mid-exhaustion. Still bounded — a genuinely
# hung call surfaces "taking longer than usual" after 4 minutes.
TOOL_TIMEOUT_S         = 120  # any tool run_in_executor call


def _get_api_key() -> str:
    # Resolve API key based on configured brain provider (provider-aware)
    key = get_provider_api_key()
    if key:
        return key
    # Fallback to legacy fields
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
        return normalize_api_key(data.get("groq_api_key", "") or "") \
            or normalize_api_key(data.get("gemini_api_key", "") or "")


def _load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are JEEVES, Tony Stark's AI assistant. "
            "Be concise, direct, and always use the provided tools to complete tasks. "
            "Never simulate or guess results — always call the appropriate tool."
        )
    
_last_memory_input = ""

_RUNTIME_IMPORTS = None
_BRAIN_CLIENT = None


def _load_runtime_imports() -> dict:
    global _RUNTIME_IMPORTS
    if _RUNTIME_IMPORTS is None:
        import numpy as np
        import sounddevice as sd

        from stt_engine import transcribe_pcm16
        from tts_engine import synthesize_to_pcm, synthesize_to_pcm_chunks

        from actions.file_processor import file_processor
        from actions.flight_finder import flight_finder
        from actions.open_app import open_app
        from actions.weather_report import weather_action
        from actions.send_message import send_message
        from actions.reminder import reminder
        from actions.computer_settings import computer_settings
        from actions.screen_processor import screen_process
        from actions.youtube_video import youtube_video
        from actions.desktop import desktop_control
        from actions.browser_control import browser_control
        from actions.file_controller import file_controller
        from actions.code_helper import code_helper
        from actions.dev_agent import dev_agent
        from actions.web_search import web_search as web_search_action
        from actions.computer_control import computer_control
        from actions.game_updater import game_updater
        from actions.system_monitor import system_status, SystemMonitor
        from actions.background_monitor import (
            add_monitor, remove_monitor, list_monitors,
            check_all as monitor_check_all,
        )

        from clap_listen import ClapListener

        _RUNTIME_IMPORTS = {
            "np": np,
            "sd": sd,
            "transcribe_pcm16": transcribe_pcm16,
            "synthesize_to_pcm": synthesize_to_pcm,
            "synthesize_to_pcm_chunks": synthesize_to_pcm_chunks,
            "file_processor": file_processor,
            "flight_finder": flight_finder,
            "open_app": open_app,
            "weather_action": weather_action,
            "send_message": send_message,
            "reminder": reminder,
            "computer_settings": computer_settings,
            "screen_process": screen_process,
            "youtube_video": youtube_video,
            "desktop_control": desktop_control,
            "browser_control": browser_control,
            "file_controller": file_controller,
            "code_helper": code_helper,
            "dev_agent": dev_agent,
            "web_search_action": web_search_action,
            "computer_control": computer_control,
            "game_updater": game_updater,
            "system_status": system_status,
            "SystemMonitor": SystemMonitor,
            "add_monitor": add_monitor,
            "remove_monitor": remove_monitor,
            "list_monitors": list_monitors,
            "monitor_check_all": monitor_check_all,
            # NOTE: run_agentic_task intentionally NOT bundled here — the
            # composio_agent module pulls in the full Composio SDK (~10s /
            # ~100MB cold). It's imported lazily inside _execute_tool only
            # when the composio_action tool is actually invoked.
            "ClapListener": ClapListener,
        }
    return _RUNTIME_IMPORTS


def _get_brain_client():
    global _BRAIN_CLIENT
    if _BRAIN_CLIENT is None:
        from or_client import client as brain_client
        _BRAIN_CLIENT = brain_client
    return _BRAIN_CLIENT

def _update_memory_async(user_text: str, jeeves_text: str) -> None:
    global _last_memory_input

    user_text   = (user_text   or "").strip()
    jeeves_text = (jeeves_text or "").strip()

    if len(user_text) < 5 or user_text == _last_memory_input:
        return
    _last_memory_input = user_text

    try:
        api_key = _get_api_key()
        if not should_extract_memory(user_text, jeeves_text, api_key):
            return
        data = extract_memory(user_text, jeeves_text, api_key)
        if data:
            update_memory(data)
            print(f"[Memory] ✅ {list(data.keys())}")
    except Exception as e:
        if "429" not in str(e):
            print(f"[Memory] ⚠️ {e}")


class JeevesLive:
    """
    J.E.E.V.E.S.
    Just an Efficient, Ever-Vigilant Executive System

    A calm, capable voice assistant: listens (Whisper), thinks (Groq),
    speaks (Piper), and can act on real accounts (Composio) -- all free,
    all local where it matters, no billing walls in the way.
    """


    def __init__(self, ui: JeevesUI):
        self.ui              = ui
        self.audio_out_queue = None
        self._loop           = None
        self._is_speaking    = False
        self._speaking_lock  = threading.Lock()
        self.conversation    = []
        self._clap_listener = None
        self._dashboard = None
        self._last_vision_ts = 0.0          # cooldown guard for screen_process
        self._last_user_speech = 0.0        # recency guard for background alerts
        self._sys_monitor = None            # lazy: SystemMonitor on first alert loop
        self._alert_lock = None             # lazy: asyncio.Lock to serialize alerts
        self._briefing_done  = False        # morning briefing runs once per launch
        self.ui.on_text_command = self._on_text_command
        self.ui.on_clap_toggle  = self._on_clap_toggle
        self.ui.on_remote_clicked = self._make_remote_key

    def _on_text_command(self, text: str):
        if not self._loop:
            return
        asyncio.run_coroutine_threadsafe(self._handle_utterance(text), self._loop)

    def _on_remote_command(self, text: str):
        if not self._loop:
            return
        asyncio.run_coroutine_threadsafe(self._handle_utterance(text), self._loop)

    def _make_remote_key(self):
        if self._dashboard is None:
            self.ui.write_log("SYS: Remote dashboard unavailable.")
            return None
        key    = self._dashboard.new_key()
        url    = self._dashboard.get_url()
        manual = self._dashboard.get_manual_url()
        self.ui.write_log(f"SYS: Remote dashboard key generated — {url} (key: {key})")
        return url, key, f"{url}/auto-login?key={key}", manual

    def set_speaking(self, value: bool):
        with self._speaking_lock:
            self._is_speaking = value
        if value:
            self.ui.set_state("SPEAKING")
        elif not self.ui.muted:
            self.ui.set_state("LISTENING")

    def speak(self, text: str):
        if not text or not self._loop:
            return
        asyncio.run_coroutine_threadsafe(self._speak_async(text), self._loop)

    async def _speak_async(self, text: str):
        self.ui.write_log(f"Jeeves: {text}")
        loop = asyncio.get_running_loop()
        if self.audio_out_queue is None:
            # Playback pipeline not started yet — drop instead of crashing
            # inside the TTS thread and leaving the user with no reply.
            print("[JEEVES] ⚠️ speak dropped: audio pipeline not ready")
            return
        try:
            imports = _load_runtime_imports()
            chunks = imports["synthesize_to_pcm_chunks"]

            def _emit_chunks():
                for chunk in chunks(text):
                    loop.call_soon_threadsafe(self.audio_out_queue.put_nowait, chunk)

            await asyncio.to_thread(_emit_chunks)
        except Exception as e:
            print(f"[JEEVES] ❌ TTS: {e}")
            # Ensure the play loop doesn't get stuck — send sentinel
            loop.call_soon_threadsafe(self.audio_out_queue.put_nowait, b"")

    def speak_error(self, tool_name: str, error: str):
        short = str(error)[:120]
        self.ui.write_log(f"ERR: {tool_name} — {short}")
        self.speak(f"Sir, {tool_name} encountered an error. {short}")

    def _build_system_prompt(self) -> str:
        from datetime import datetime

        memory     = load_memory()
        mem_str    = format_memory_for_prompt(memory)
        sys_prompt = _load_system_prompt()

        now      = datetime.now()
        time_str = now.strftime("%A, %B %d, %Y — %I:%M %p")
        time_ctx = (
            f"[CURRENT DATE & TIME]\n"
            f"Right now it is: {time_str}\n"
            f"Use this to calculate exact times for reminders.\n\n"
        )

        parts = [time_ctx]
        if mem_str:
            parts.append(mem_str)
        parts.append(sys_prompt)
        parts.append(
            "\n[TOOLS]\nYou have tools available. To call one, respond with "
            "ONLY a JSON object of the form "
            '{"tool_call": {"name": "<tool_name>", "args": {...}}}. '
            "To just speak to the user, respond with plain text (no JSON). "
            "Available tools:\n" + json.dumps(TOOL_DECLARATIONS, indent=2)
        )

        return "\n".join(parts)

    async def _execute_tool(self, name: str, args: dict) -> str:
        args = dict(args or {})
        imports = _load_runtime_imports()

        print(f"[JEEVES] 🔧 {name}  {args}")
        self.ui.set_state("THINKING")
        if name == "save_memory":
            category = args.get("category", "notes")
            key      = args.get("key", "")
            value    = args.get("value", "")
            if key and value:
                update_memory({category: {key: {"value": value}}})
                print(f"[Memory] 💾 save_memory: {category}/{key} = {value}")
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
            return "__SILENT__"

        loop   = asyncio.get_event_loop()
        result = "Done."

        try:
            if name == "open_app":
                r = await loop.run_in_executor(None, lambda: imports["open_app"](parameters=args, response=None, player=self.ui))
                result = r or f"Opened {args.get('app_name')}."

            elif name == "weather_report":
                r = await loop.run_in_executor(None, lambda: imports["weather_action"](parameters=args, player=self.ui))
                result = r or "Weather delivered."

            elif name == "browser_control":
                r = await loop.run_in_executor(None, lambda: imports["browser_control"](parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "file_controller":
                r = await loop.run_in_executor(None, lambda: imports["file_controller"](parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "send_message":
                r = await loop.run_in_executor(None, lambda: imports["send_message"](parameters=args, response=None, player=self.ui, session_memory=None))
                result = r or f"Message sent to {args.get('receiver')}."

            elif name == "reminder":
                r = await loop.run_in_executor(None, lambda: imports["reminder"](parameters=args, response=None, player=self.ui))
                result = r or "Reminder set."

            elif name == "youtube_video":
                r = await loop.run_in_executor(None, lambda: imports["youtube_video"](parameters=args, response=None, player=self.ui))
                result = r or "Done."
            elif name == "file_processor":
                if not args.get("file_path") and self.ui.current_file:
                    args["file_path"] = self.ui.current_file
                r = await loop.run_in_executor(
                    None,
                    lambda: imports["file_processor"](parameters=args, player=self.ui, speak=self.speak)
                )
                result = r or "Done."


            elif name == "screen_process":
                now = time.time()
                if now - self._last_vision_ts < VISION_COOLDOWN_S:
                    result = (
                        f"Vision module is cooling down ({int(VISION_COOLDOWN_S - (now - self._last_vision_ts))}s left). "
                        "Please wait before analyzing the screen again."
                    )
                else:
                    self._last_vision_ts = now
                    threading.Thread(
                        target=imports["screen_process"],
                        kwargs={"parameters": args, "response": None,
                                "player": self.ui, "session_memory": None},
                        daemon=True
                    ).start()
                    result = "Vision module activated. Stay completely silent — vision module will speak directly."

            elif name == "system_status":
                r = await loop.run_in_executor(None, lambda: imports["system_status"](parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "manage_monitor":
                action = str(args.get("action", "")).lower().strip()
                topic  = str(args.get("topic", "")).strip()
                if action == "add":
                    result = await asyncio.to_thread(imports["add_monitor"], topic)
                elif action == "remove":
                    result = await asyncio.to_thread(imports["remove_monitor"], topic)
                else:
                    topics = await asyncio.to_thread(imports["list_monitors"])
                    result = ("Monitoring: " + ", ".join(topics)) if topics \
                             else "No topics are being monitored."

            elif name == "computer_settings":
                r = await loop.run_in_executor(None, lambda: imports["computer_settings"](parameters=args, response=None, player=self.ui))
                result = r or "Done."

            elif name == "desktop_control":
                r = await loop.run_in_executor(None, lambda: imports["desktop_control"](parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "code_helper":
                r = await loop.run_in_executor(None, lambda: imports["code_helper"](parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "dev_agent":
                r = await loop.run_in_executor(None, lambda: imports["dev_agent"](parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "agent_task":
                from agent.task_queue import get_queue, TaskPriority
                priority_map = {"low": TaskPriority.LOW, "normal": TaskPriority.NORMAL, "high": TaskPriority.HIGH}
                priority = priority_map.get(args.get("priority", "normal").lower(), TaskPriority.NORMAL)
                task_id  = get_queue().submit(goal=args.get("goal", ""), priority=priority, speak=self.speak)
                result   = f"Task started (ID: {task_id})."

            elif name == "web_search":
                r = await loop.run_in_executor(None, lambda: imports["web_search_action"](parameters=args, player=self.ui))
                result = r or "Done."
                # Surface rich search results in the dynamic content panel
                try:
                    self.ui.show_content("WEB SEARCH", result)
                except Exception:
                    pass

            elif name == "computer_control":
                r = await loop.run_in_executor(None, lambda: imports["computer_control"](parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "cmd_control":
                from actions.cmd_control import cmd_control
                r = await loop.run_in_executor(None, lambda: cmd_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "game_updater":
                r = await loop.run_in_executor(None, lambda: imports["game_updater"](parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "flight_finder":
                r = await loop.run_in_executor(None, lambda: imports["flight_finder"](parameters=args, player=self.ui))
                result = r or "Done."
            elif name == "composio_action":
                # Lazy: composio_agent pulls the full Composio SDK (~10s /
                # ~100MB cold) — only pay it when this tool is actually used.
                # Defensive: a broken/mismatched Composio SDK must never
                # crash the whole app — degrade to a helpful message.
                try:
                    from composio_agent import run_agentic_task
                except Exception as _composio_import_error:
                    def run_agentic_task(request: str) -> str:
                        return (
                            f"Composio isn't available right now ({_composio_import_error}). "
                            "Check that composio and composio-openai are installed and "
                            "up to date, or run 'python doctor.py' for details."
                        )
                r = await loop.run_in_executor(
                    None, lambda: run_agentic_task(args.get("request", ""))
                )
                result = r or "Done."

            elif name == "shutdown_jeeves":
                self.ui.write_log("SYS: Shutdown requested.")
                self.speak("Goodbye, sir.")

                def _shutdown():
                    import time, os
                    try:
                        cleanup_jeeves()
                    except Exception as e:
                        print(f"Cleanup error: {e}")
                    time.sleep(1)
                    os._exit(0)

                threading.Thread(target=_shutdown, daemon=True).start()
                result = "__SILENT__"
            else:
                result = f"Unknown tool: {name}"

        except Exception as e:
            result = f"Tool '{name}' failed: {e}"
            traceback.print_exc()
            self.speak_error(name, e)

        if not self.ui.muted:
            self.ui.set_state("LISTENING")

        print(f"[JEEVES] 📤 {name} → {str(result)[:80]}")

        return result

    async def _listen_audio(self):
        print("[JEEVES] 🎤 Mic started")
        loop = asyncio.get_event_loop()
        imports = _load_runtime_imports()
        np = imports["np"]
        sd = imports["sd"]

        frame_ms               = int(1000 * CHUNK_SIZE / SEND_SAMPLE_RATE)
        silence_frames_needed  = max(1, SILENCE_HANG_MS // frame_ms)
        min_frames             = max(1, MIN_UTTERANCE_MS // frame_ms)

        buffer          = bytearray()
        silence_run     = 0
        heard_any_voice = False

        def callback(indata, frames, time_info, status):
            nonlocal buffer, silence_run, heard_any_voice

            with self._speaking_lock:
                jeeves_speaking = self._is_speaking
            if jeeves_speaking or self.ui.muted:
                return

            data = indata.tobytes()
            rms  = float(np.sqrt(np.mean(np.square(indata.astype(np.float32)))))

            if rms >= SILENCE_RMS_THRESHOLD:
                buffer.extend(data)
                silence_run     = 0
                heard_any_voice = True
            elif heard_any_voice:
                buffer.extend(data)
                silence_run += 1

            frames_recorded = len(buffer) // 2 // CHANNELS
            turn_finished = (
                heard_any_voice
                and silence_run >= silence_frames_needed
                and frames_recorded >= min_frames
            )
            if turn_finished:
                utterance = bytes(buffer)
                buffer.clear()
                silence_run     = 0
                heard_any_voice = False
                loop.call_soon_threadsafe(
                    lambda u=utterance: asyncio.ensure_future(self._handle_audio_utterance(u))
                )

        try:
            with sd.InputStream(
                samplerate=SEND_SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK_SIZE,
                callback=callback,
            ):
                print("[JEEVES] 🎤 Mic stream open")
                while True:
                    await asyncio.sleep(0.1)
        except Exception as e:
            print(f"[JEEVES] ❌ Mic: {e}")
            raise

    async def _handle_audio_utterance(self, pcm_bytes: bytes):
        try:
            self.ui.set_state("THINKING")
            imports = _load_runtime_imports()
            text = await asyncio.to_thread(imports["transcribe_pcm16"], pcm_bytes, SEND_SAMPLE_RATE)
            text = (text or "").strip()
            if not text:
                if not self.ui.muted:
                    self.ui.set_state("LISTENING")
                return
            self.ui.write_log(f"You: {text}")
            await self._handle_utterance(text)
        except Exception as e:
            print(f"[JEEVES] ❌ STT: {e}")
            traceback.print_exc()
            if not self.ui.muted:
                self.ui.set_state("LISTENING")

    async def _handle_utterance(self, text: str):
        self._last_user_speech = time.monotonic()
        self.ui.set_state("THINKING")
        self.conversation.append({"role": "user", "content": text})

        try:
            system_prompt = self._build_system_prompt()
            reply = await asyncio.wait_for(
                asyncio.to_thread(
                    _get_brain_client().multi_turn,
                    [{"role": "system", "content": system_prompt}] + self.conversation[-20:],
                ),
                timeout=BRAIN_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            print("[JEEVES] ❌ Brain timed out")
            self.speak("Sir, I'm taking longer than usual to think. Please bear with me.")
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
            return
        except Exception as e:
            print(f"[JEEVES] ❌ Brain: {e}")
            self.speak("Sir, I had trouble reaching my reasoning engine just now.")
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
            return

        # Never let an empty/whitespace model reply vanish silently —
        # speak a graceful fallback so the user always gets a response.
        reply = (reply or "").strip()
        if not reply:
            reply = "I'm sorry, sir, I didn't quite catch that. Could you repeat?"

        self.conversation.append({"role": "assistant", "content": reply})

        tool_name, tool_args = self._parse_tool_call(reply)

        if tool_name:
            print(f"[JEEVES] 📞 {tool_name}")
            try:
                result = await asyncio.wait_for(
                    self._execute_tool(tool_name, tool_args),
                    timeout=TOOL_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                # A hung tool must never leave the user without an answer.
                print(f"[JEEVES] ❌ Tool {tool_name} timed out after {TOOL_TIMEOUT_S}s")
                result = f"Tool '{tool_name}' timed out after {TOOL_TIMEOUT_S}s."
                self.speak_error(tool_name, "timed out — I'll stop waiting on it.")
            if result == "__SILENT__":
                pass
            else:
                self.conversation.append({
                    "role": "user",
                    "content": f"[TOOL RESULT for {tool_name}]: {result}\n"
                               f"Now reply to the user naturally in one or two sentences."
                })
                try:
                    system_prompt = self._build_system_prompt()
                    followup = await asyncio.wait_for(
                        asyncio.to_thread(
                            _get_brain_client().multi_turn,
                            [{"role": "system", "content": system_prompt}] + self.conversation[-20:],
                        ),
                        timeout=BRAIN_TIMEOUT_S,
                    )
                    followup = (followup or "").strip()
                    if not followup:
                        followup = str(result)[:150]
                    self.conversation.append({"role": "assistant", "content": followup})
                    self.speak(followup)
                except asyncio.TimeoutError:
                    print("[JEEVES] ❌ Brain (followup) timed out")
                    self.speak(str(result)[:150])
                except Exception as e:
                    print(f"[JEEVES] ❌ Brain (followup): {e}")
                    self.speak(str(result)[:150])

            threading.Thread(
                target=_update_memory_async,
                args=(text, str(tool_name)),
                daemon=True
            ).start()
        else:
            self.speak(reply)
            if len(text) > 5:
                threading.Thread(
                    target=_update_memory_async,
                    args=(text, reply),
                    daemon=True
                ).start()

        if not self.ui.muted:
            self.ui.set_state("LISTENING")

    @staticmethod
    def _parse_tool_call(reply: str):
        cleaned = reply.strip()
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

    async def _play_audio(self):
        print("[JEEVES] 🔊 Play started")
        imports = _load_runtime_imports()
        sd = imports["sd"]

        stream = sd.RawOutputStream(
            samplerate=RECEIVE_SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=CHUNK_SIZE,
        )
        stream.start()
        try:
            while True:
                chunk = await self.audio_out_queue.get()
                # PCM_SENTINEL (b"") marks end of an utterance
                if chunk == b"":
                    self.set_speaking(False)
                    continue
                self.set_speaking(True)
                await asyncio.to_thread(stream.write, chunk)
        except Exception as e:
            print(f"[JEEVES] ❌ Play: {e}")
            raise
        finally:
            self.set_speaking(False)
            stream.stop()
            stream.close()

    def _on_clap_toggle(self, enabled: bool):
        """
        Called both at startup (based on the saved config flag) and live,
        whenever the CLAP WAKE button in the app is clicked. Starts or
        stops the double-clap listener accordingly -- no restart needed.
        """
        if enabled:
            if self._clap_listener is None:
                self._clap_listener = _load_runtime_imports()["ClapListener"](
                    lambda: setattr(self.ui, "muted", not self.ui.muted)
                )
            if not self._clap_listener.is_running():
                self._clap_listener.start()
                print("[JEEVES] Clap wake enabled -- double-clap toggles mute.")
        else:
            if self._clap_listener is not None and self._clap_listener.is_running():
                self._clap_listener.stop()
                print("[JEEVES] Clap wake disabled.")

    def _maybe_start_clap_listener(self):
        """Reads the saved config flag once at startup and starts the
        listener if it was left enabled from a previous session."""
        try:
            with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            self._on_clap_toggle(bool(cfg.get("enable_clap_wake", False)))
        except Exception as e:
            print(f"[JEEVES] Clap listener not started: {e}")

    async def _maybe_start_morning_briefing(self):
        """Speak a morning greeting + fresh news once per launch (5–11am).

        Runs as a background task so it never blocks the voice pipeline;
        any failure is logged and ignored — Jeeves keeps working either way.
        """
        if self._briefing_done or not self._loop or not self.audio_out_queue:
            return
        hour = int(time.localtime().tm_hour)
        if not (5 <= hour < 11):
            self._briefing_done = True
            return

        # Only greet + brief when the mic is actually on so we don't talk over the user
        if getattr(self.ui, "muted", False):
            self._briefing_done = True
            return

        self._briefing_done = True
        try:
            imports = _load_runtime_imports()
            greeting = "Good morning, sir."
            self.speak(greeting)
            self.ui.write_log("SYS: Morning briefing started.")

            loop = asyncio.get_running_loop()
            news = await loop.run_in_executor(
                None,
                lambda: imports["web_search_action"](
                    parameters={"mode": "news", "query": "top headlines today"},
                    player=self.ui,
                ),
            )
            if news and "Could not fetch" not in news and "No news found" not in news:
                lines = [ln for ln in news.splitlines() if ln.strip()]
                spoken = " ".join(lines[:4])[:600]
                await self._speak_async(
                    "Here are the top headlines. " + spoken
                )
        except Exception as e:
            print(f"[JEEVES] Morning briefing skipped: {e}")

    async def _speak_alert(self, alert: str):
        """Voice a background alert, phrased naturally by the brain.

        The [SYSTEM_ALERT]/[MONITOR_ALERT] strings are instructions for the
        model ("warn the user in their language..."), not user-facing text,
        so we run them through one short brain call. On any brain failure we
        fall back to speaking the alert's first line directly — an alert must
        never be swallowed silently.
        """
        if not self._loop or not self.audio_out_queue:
            return
        # Serialize alerts: both monitor loops can fire near-simultaneously,
        # and two concurrent TTS emitters would interleave PCM chunks in the
        # audio queue and garble playback.
        if self._alert_lock is None:
            self._alert_lock = asyncio.Lock()
        async with self._alert_lock:
            await self._do_speak_alert(alert)

    @staticmethod
    def _alert_has_tool_call(reply: str) -> bool:
        """True if the brain replied with a {tool_call:...} JSON instead of prose."""
        cleaned = (reply or "").strip()
        if not cleaned.startswith("{"):
            return False
        try:
            data = json.loads(cleaned)
            return bool(data.get("tool_call"))
        except Exception:
            return False

    @staticmethod
    def _alert_first_line(alert: str) -> str:
        """First line of an alert with the [SYSTEM_ALERT]/[MONITOR_ALERT] tag stripped."""
        line = (alert or "").splitlines()[0][:200] if alert else ""
        for tag in ("[SYSTEM_ALERT]", "[MONITOR_ALERT]"):
            if line.startswith(tag):
                line = line[len(tag):].strip()
                break
        return line

    async def _do_speak_alert(self, alert: str):
        try:
            system_prompt = self._build_system_prompt()
            reply = await asyncio.wait_for(
                asyncio.to_thread(
                    _get_brain_client().multi_turn,
                    [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": alert},
                    ],
                ),
                timeout=BRAIN_TIMEOUT_S,
            )
            reply = (reply or "").strip()
            # Never speak raw tool-call JSON — fall back to the alert text.
            if not reply or self._alert_has_tool_call(reply):
                reply = self._alert_first_line(alert) or \
                        "Sir, I have an alert for you."
            await self._speak_async(reply)
            self.ui.write_log("SYS: Monitor alert spoken.")
        except Exception as e:
            print(f"[JEEVES] Alert: {e}")
            try:
                first = self._alert_first_line(alert)
                if first:
                    await self._speak_async(first)
            except Exception:
                pass

    async def _run_system_monitor(self):
        """Background task: voice alerts when hardware metrics cross thresholds."""
        while True:
            await asyncio.sleep(10)
            try:
                if self._sys_monitor is None:
                    self._sys_monitor = _load_runtime_imports()["SystemMonitor"]()
                alert = await asyncio.to_thread(self._sys_monitor.check)
            except Exception as e:
                print(f"[JEEVES] System monitor: {e}")
                continue
            if not alert:
                continue
            # Never interrupt an active conversation or a just-spoken turn
            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking or (time.monotonic() - self._last_user_speech) < 10:
                continue
            if getattr(self.ui, "muted", False):
                continue
            await self._speak_alert(alert)

    async def _run_background_monitor(self):
        """Check user-configured topics once per day; speak alerts on new headlines."""
        await asyncio.sleep(300)          # wait 5 min after startup before first check
        while True:
            try:
                with self._speaking_lock:
                    speaking = self._is_speaking
                recent_speech = (time.monotonic() - self._last_user_speech) < 30
                if not speaking and not recent_speech \
                        and not getattr(self.ui, "muted", False):
                    alerts = await asyncio.to_thread(
                        _load_runtime_imports()["monitor_check_all"]
                    )
                    for alert in alerts:
                        await self._speak_alert(alert)
                        await asyncio.sleep(6)   # gap between consecutive alerts
            except Exception as e:
                print(f"[JEEVES] Background monitor: {e}")
            await asyncio.sleep(1800)     # re-check every 30 minutes

    async def run(self):
        self._maybe_start_clap_listener()
        try:
            self._dashboard = DashboardServer()
            self._dashboard.set_command_callback(self._on_remote_command)
            self._dashboard.set_wake_callback(lambda: self.ui.write_log("SYS: Remote wake received."))

            def _on_remote_connected():
                self.ui.write_log("SYS: Remote dashboard connected.")
                self.ui.mark_remote_connected()

            self._dashboard.set_connect_callback(_on_remote_connected)
            asyncio.create_task(self._dashboard.serve())
        except Exception as e:
            print(f"[Dashboard] Disabled: {e}")
            self._dashboard = None
        while True:
            try:
                print("[JEEVES] Starting local voice pipeline (Whisper + Groq + Piper)...")
                self.ui.set_state("THINKING")
                self._loop           = asyncio.get_event_loop()
                self.audio_out_queue = asyncio.Queue()

                print("[JEEVES] Ready.")
                self.ui.set_state("LISTENING")
                self.ui.write_log("SYS: JEEVES online (local voice pipeline).")

                # Morning briefing: background task, speaks greeting + fresh news
                asyncio.create_task(self._maybe_start_morning_briefing())

                async with asyncio.TaskGroup() as tg:
                    tg.create_task(self._listen_audio())
                    tg.create_task(self._play_audio())
                    tg.create_task(self._run_system_monitor())
                    tg.create_task(self._run_background_monitor())

            except Exception as e:
                print(f"[JEEVES] Error: {e}")
                traceback.print_exc()

            self.set_speaking(False)
            self.ui.set_state("THINKING")
            print("[JEEVES] Restarting voice pipeline in 3s...")
            await asyncio.sleep(3)


def _warm_up_tts():
    """Pre-load the Piper TTS model in a background thread at startup.

    This avoids the ~7s model-loading delay on the very first TTS call,
    making the first response as fast as all subsequent ones.
    """
    try:
        import tts_engine
        tts_engine.warm_up()
        # ASCII-only print: this runs in a background thread where the
        # Windows cp1252 console encoding would crash on emoji.
        print("[JEEVES] TTS engine warmed up")
    except Exception as e:
        print(f"[JEEVES] TTS warm-up: {e}")


def _prewarm_runtime_imports():
    """Pre-load heavy runtime modules in a background thread.

    The voice pipeline calls _load_runtime_imports() on first use, which
    costs ~6s of cold imports (numpy, sounddevice, faster-whisper, ...) and
    would otherwise stall the "LISTENING" mic stream right after launch.
    Starting it here overlaps with UI construction + setup + TTS warm-up,
    so by the time the pipeline starts the modules are already cached.
    """
    try:
        _load_runtime_imports()
        # ASCII-only print: background threads can hit the cp1252 console
        # encoding on Windows and crash on emoji.
        print("[JEEVES] Runtime modules pre-warmed")
    except Exception as e:
        print(f"[JEEVES] Runtime pre-warm: {e}")


def main():
    # Register cleanup to run on any exit
    atexit.register(cleanup_jeeves)

    # Build the window FIRST (uncontended CPU) so it appears immediately,
    # then warm heavy runtime imports in the background. The prewarm
    # overlaps with wait_for_api_key() + TTS warm-up, so by the time the
    # voice pipeline starts, its modules are already cached and the mic
    # opens instantly.
    ui = JeevesUI("face.png")
    threading.Thread(target=_prewarm_runtime_imports, daemon=True).start()

    def runner():
        ui.wait_for_api_key()

        # Fire-and-forget: warm up TTS in background so the model is
        # ready before the user's first spoken request.
        threading.Thread(target=_warm_up_tts, daemon=True).start()

        jeeves = JeevesLive(ui)
        try:
            asyncio.run(jeeves.run())
        except KeyboardInterrupt:
            print("\n🔴 Shutting down...")
            try:
                cleanup_jeeves()
            except Exception as e:
                print(f"Cleanup error: {e}")

    threading.Thread(target=runner, daemon=True).start()
    ui.root.mainloop()


if __name__ == "__main__":
    main()
