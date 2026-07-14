import asyncio
import threading
import json
import sys
import traceback
import numpy as np
from pathlib import Path

import sounddevice as sd
from or_client import client as brain_client
from stt_engine import transcribe_pcm16
from tts_engine import synthesize_to_pcm, OUTPUT_SAMPLE_RATE
from ui import JeevesUI
from memory.memory_manager import (
    load_memory, update_memory, format_memory_for_prompt,
    should_extract_memory, extract_memory
)

from actions.file_processor import file_processor
from actions.flight_finder     import flight_finder
from actions.open_app          import open_app
from actions.weather_report    import weather_action
from actions.send_message      import send_message
from actions.reminder          import reminder
from actions.computer_settings import computer_settings
from actions.screen_processor  import screen_process
from actions.youtube_video     import youtube_video
from actions.desktop           import desktop_control
from actions.browser_control   import browser_control
from actions.file_controller   import file_controller
from actions.code_helper       import code_helper
from actions.dev_agent         import dev_agent
from actions.web_search        import web_search as web_search_action
from actions.computer_control  import computer_control
from actions.game_updater      import game_updater


def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
PROMPT_PATH     = BASE_DIR / "core" / "prompt.txt"
CHANNELS            = 1
SEND_SAMPLE_RATE    = 16000
RECEIVE_SAMPLE_RATE = OUTPUT_SAMPLE_RATE
CHUNK_SIZE          = 1024

SILENCE_RMS_THRESHOLD = 400
SILENCE_HANG_MS        = 900
MIN_UTTERANCE_MS       = 300


def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
        return data.get("groq_api_key", data.get("gemini_api_key", ""))


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

TOOL_DECLARATIONS = [
    {
        "name": "open_app",
        "description": (
            "Opens any application on the Windows computer. "
            "Use this whenever the user asks to open, launch, or start any app, "
            "website, or program. Always call this tool — never just say you opened it."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "app_name": {
                    "type": "STRING",
                    "description": "Exact name of the application (e.g. 'WhatsApp', 'Chrome', 'Spotify')"
                }
            },
            "required": ["app_name"]
        }
    },
    {
        "name": "web_search",
        "description": "Searches the web for any information.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query":  {"type": "STRING", "description": "Search query"},
                "mode":   {"type": "STRING", "description": "search (default) or compare"},
                "items":  {"type": "ARRAY", "items": {"type": "STRING"}, "description": "Items to compare"},
                "aspect": {"type": "STRING", "description": "price | specs | reviews"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "weather_report",
        "description": "Gives the weather report to user",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "city": {"type": "STRING", "description": "City name"}
            },
            "required": ["city"]
        }
    },
    {
        "name": "send_message",
        "description": "Sends a text message via WhatsApp, Telegram, or other messaging platform.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "receiver":     {"type": "STRING", "description": "Recipient contact name"},
                "message_text": {"type": "STRING", "description": "The message to send"},
                "platform":     {"type": "STRING", "description": "Platform: WhatsApp, Telegram, etc."}
            },
            "required": ["receiver", "message_text", "platform"]
        }
    },
    {
        "name": "reminder",
        "description": "Sets a timed reminder using Windows Task Scheduler.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "date":    {"type": "STRING", "description": "Date in YYYY-MM-DD format"},
                "time":    {"type": "STRING", "description": "Time in HH:MM format (24h)"},
                "message": {"type": "STRING", "description": "Reminder message text"}
            },
            "required": ["date", "time", "message"]
        }
    },
    {
        "name": "youtube_video",
        "description": (
            "Controls YouTube. Use for: playing videos, summarizing a video's content, "
            "getting video info, or showing trending videos."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "play | summarize | get_info | trending (default: play)"},
                "query":  {"type": "STRING", "description": "Search query for play action"},
                "save":   {"type": "BOOLEAN", "description": "Save summary to Notepad (summarize only)"},
                "region": {"type": "STRING", "description": "Country code for trending e.g. TR, US"},
                "url":    {"type": "STRING", "description": "Video URL for get_info action"},
            },
            "required": []
        }
    },
    {
        "name": "screen_process",
        "description": (
            "Captures and analyzes the screen or webcam image. "
            "MUST be called when user asks what is on screen, what you see, "
            "analyze my screen, look at camera, etc. "
            "You have NO visual ability without this tool. "
            "After calling this tool, stay SILENT — the vision module speaks directly."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "angle": {"type": "STRING", "description": "'screen' to capture display, 'camera' for webcam. Default: 'screen'"},
                "text":  {"type": "STRING", "description": "The question or instruction about the captured image"}
            },
            "required": ["text"]
        }
    },
    {
        "name": "computer_settings",
        "description": (
            "Controls the computer: volume, brightness, window management, keyboard shortcuts, "
            "typing text on screen, closing apps, fullscreen, dark mode, WiFi, restart, shutdown, "
            "scrolling, tab management, zoom, screenshots, lock screen, refresh/reload page. "
            "Use for ANY single computer control command. NEVER route to agent_task."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "The action to perform"},
                "description": {"type": "STRING", "description": "Natural language description of what to do"},
                "value":       {"type": "STRING", "description": "Optional value: volume level, text to type, etc."}
            },
            "required": []
        }
    },
    {
        "name": "browser_control",
        "description": (
            "Controls the web browser. Use for: opening websites, searching the web, "
            "clicking elements, filling forms, scrolling, any web-based task."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "go_to | search | click | type | scroll | fill_form | smart_click | smart_type | get_text | press | close"},
                "url":         {"type": "STRING", "description": "URL for go_to action"},
                "query":       {"type": "STRING", "description": "Search query for search action"},
                "selector":    {"type": "STRING", "description": "CSS selector for click/type"},
                "text":        {"type": "STRING", "description": "Text to click or type"},
                "description": {"type": "STRING", "description": "Element description for smart_click/smart_type"},
                "direction":   {"type": "STRING", "description": "up or down for scroll"},
                "key":         {"type": "STRING", "description": "Key name for press action"},
                "incognito":   {"type": "BOOLEAN", "description": "Open in private/incognito mode"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "file_controller",
        "description": "Manages files and folders: list, create, delete, move, copy, rename, read, write, find, disk usage.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "list | create_file | create_folder | delete | move | copy | rename | read | write | find | largest | disk_usage | organize_desktop | info"},
                "path":        {"type": "STRING", "description": "File/folder path or shortcut: desktop, downloads, documents, home"},
                "destination": {"type": "STRING", "description": "Destination path for move/copy"},
                "new_name":    {"type": "STRING", "description": "New name for rename"},
                "content":     {"type": "STRING", "description": "Content for create_file/write"},
                "name":        {"type": "STRING", "description": "File name to search for"},
                "extension":   {"type": "STRING", "description": "File extension to search (e.g. .pdf)"},
                "count":       {"type": "INTEGER", "description": "Number of results for largest"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "desktop_control",
        "description": "Controls the desktop: wallpaper, organize, clean, list, stats.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "wallpaper | wallpaper_url | organize | clean | list | stats | task"},
                "path":   {"type": "STRING", "description": "Image path for wallpaper"},
                "url":    {"type": "STRING", "description": "Image URL for wallpaper_url"},
                "mode":   {"type": "STRING", "description": "by_type or by_date for organize"},
                "task":   {"type": "STRING", "description": "Natural language desktop task"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "code_helper",
        "description": "Writes, edits, explains, runs, or builds code files.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "write | edit | explain | run | build | auto (default: auto)"},
                "description": {"type": "STRING", "description": "What the code should do or what change to make"},
                "language":    {"type": "STRING", "description": "Programming language (default: python)"},
                "output_path": {"type": "STRING", "description": "Where to save the file"},
                "file_path":   {"type": "STRING", "description": "Path to existing file for edit/explain/run/build"},
                "code":        {"type": "STRING", "description": "Raw code string for explain"},
                "args":        {"type": "STRING", "description": "CLI arguments for run/build"},
                "timeout":     {"type": "INTEGER", "description": "Execution timeout in seconds (default: 30)"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "dev_agent",
        "description": "Builds complete multi-file projects from scratch: plans, writes files, installs deps, opens VSCode, runs and fixes errors.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "description":  {"type": "STRING", "description": "What the project should do"},
                "language":     {"type": "STRING", "description": "Programming language (default: python)"},
                "project_name": {"type": "STRING", "description": "Optional project folder name"},
                "timeout":      {"type": "INTEGER", "description": "Run timeout in seconds (default: 30)"},
            },
            "required": ["description"]
        }
    },
    {
        "name": "agent_task",
        "description": (
            "Executes complex multi-step tasks requiring multiple different tools. "
            "Examples: 'research X and save to file', 'find and organize files'. "
            "DO NOT use for single commands. NEVER use for Steam/Epic — use game_updater."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "goal":     {"type": "STRING", "description": "Complete description of what to accomplish"},
                "priority": {"type": "STRING", "description": "low | normal | high (default: normal)"}
            },
            "required": ["goal"]
        }
    },
    {
        "name": "computer_control",
        "description": "Direct computer control: type, click, hotkeys, scroll, move mouse, screenshots, find elements on screen.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "type | smart_type | click | double_click | right_click | hotkey | press | scroll | move | copy | paste | screenshot | wait | clear_field | focus_window | screen_find | screen_click | random_data | user_data"},
                "text":        {"type": "STRING", "description": "Text to type or paste"},
                "x":           {"type": "INTEGER", "description": "X coordinate"},
                "y":           {"type": "INTEGER", "description": "Y coordinate"},
                "keys":        {"type": "STRING", "description": "Key combination e.g. 'ctrl+c'"},
                "key":         {"type": "STRING", "description": "Single key e.g. 'enter'"},
                "direction":   {"type": "STRING", "description": "up | down | left | right"},
                "amount":      {"type": "INTEGER", "description": "Scroll amount (default: 3)"},
                "seconds":     {"type": "NUMBER",  "description": "Seconds to wait"},
                "title":       {"type": "STRING",  "description": "Window title for focus_window"},
                "description": {"type": "STRING",  "description": "Element description for screen_find/screen_click"},
                "type":        {"type": "STRING",  "description": "Data type for random_data"},
                "field":       {"type": "STRING",  "description": "Field for user_data: name|email|city"},
                "clear_first": {"type": "BOOLEAN", "description": "Clear field before typing (default: true)"},
                "path":        {"type": "STRING",  "description": "Save path for screenshot"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "game_updater",
        "description": (
            "THE ONLY tool for ANY Steam or Epic Games request. "
            "Use for: installing, downloading, updating games, listing installed games, "
            "checking download status, scheduling updates. "
            "ALWAYS call directly for any Steam/Epic/game request. "
            "NEVER use agent_task, browser_control, or web_search for Steam/Epic."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":    {"type": "STRING",  "description": "update | install | list | download_status | schedule | cancel_schedule | schedule_status (default: update)"},
                "platform":  {"type": "STRING",  "description": "steam | epic | both (default: both)"},
                "game_name": {"type": "STRING",  "description": "Game name (partial match supported)"},
                "app_id":    {"type": "STRING",  "description": "Steam AppID for install (optional)"},
                "hour":      {"type": "INTEGER", "description": "Hour for scheduled update 0-23 (default: 3)"},
                "minute":    {"type": "INTEGER", "description": "Minute for scheduled update 0-59 (default: 0)"},
                "shutdown_when_done": {"type": "BOOLEAN", "description": "Shut down PC when download finishes"},
            },
            "required": []
        }
    },
    {
        "name": "flight_finder",
        "description": "Searches Google Flights and speaks the best options.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "origin":      {"type": "STRING",  "description": "Departure city or airport code"},
                "destination": {"type": "STRING",  "description": "Arrival city or airport code"},
                "date":        {"type": "STRING",  "description": "Departure date (any format)"},
                "return_date": {"type": "STRING",  "description": "Return date for round trips"},
                "passengers":  {"type": "INTEGER", "description": "Number of passengers (default: 1)"},
                "cabin":       {"type": "STRING",  "description": "economy | premium | business | first"},
                "save":        {"type": "BOOLEAN", "description": "Save results to Notepad"},
            },
            "required": ["origin", "destination", "date"]
        }
    },
    {
    "name": "file_processor",
    "description": (
        "Processes any file that the user has uploaded or dropped onto the interface. "
        "Use this when the user refers to an uploaded file and wants an action on it. "
        "Supports: images (describe/ocr/resize/compress/convert), "
        "PDFs (summarize/extract_text/to_word), "
        "Word docs & text files (summarize/fix/reformat/translate), "
        "CSV/Excel (analyze/stats/filter/sort/convert), "
        "JSON/XML (validate/format/analyze), "
        "code files (explain/review/fix/optimize/run/document/test), "
        "audio (transcribe/trim/convert/info), "
        "video (trim/extract_audio/extract_frame/compress/transcribe/info), "
        "archives (list/extract), "
        "presentations (summarize/extract_text). "
        "ALWAYS call this tool when a file has been uploaded and the user gives a command about it. "
        "If the user's command is ambiguous, pick the most logical action for that file type."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "file_path": {
                "type": "STRING",
                "description": "Full path to the uploaded file. Leave empty to use the currently uploaded file."
            },
            "action": {
                "type": "STRING",
                "description": (
                    "What to do with the file. Examples by type:\n"
                    "image: describe | ocr | resize | compress | convert | info\n"
                    "pdf: summarize | extract_text | to_word | info\n"
                    "docx/txt: summarize | fix | reformat | translate_hint | word_count | to_bullet\n"
                    "csv/excel: analyze | stats | filter | sort | convert | info\n"
                    "json: validate | format | analyze | to_csv\n"
                    "code: explain | review | fix | optimize | run | document | test\n"
                    "audio: transcribe | trim | convert | info\n"
                    "video: trim | extract_audio | extract_frame | compress | transcribe | info | convert\n"
                    "archive: list | extract\n"
                    "pptx: summarize | extract_text | analyze"
                )
            },
            "instruction": {
                "type": "STRING",
                "description": "Free-form instruction if action doesn't cover it. E.g. 'translate this to Turkish', 'find all email addresses'"
            },
            "format": {
                "type": "STRING",
                "description": "Target format for conversion. E.g. 'mp3', 'pdf', 'csv', 'png'"
            },
            "width":     {"type": "INTEGER", "description": "Target width for image resize"},
            "height":    {"type": "INTEGER", "description": "Target height for image resize"},
            "scale":     {"type": "NUMBER",  "description": "Scale factor for image resize (e.g. 0.5)"},
            "quality":   {"type": "INTEGER", "description": "Quality 1-100 for image/video compress"},
            "start":     {"type": "STRING",  "description": "Start time for trim: seconds or HH:MM:SS"},
            "end":       {"type": "STRING",  "description": "End time for trim: seconds or HH:MM:SS"},
            "timestamp": {"type": "STRING",  "description": "Timestamp for video frame extraction HH:MM:SS"},
            "column":    {"type": "STRING",  "description": "Column name for CSV filter/sort"},
            "value":     {"type": "STRING",  "description": "Filter value for CSV filter"},
            "condition": {"type": "STRING",  "description": "Filter condition: equals|contains|gt|lt"},
            "ascending": {"type": "BOOLEAN", "description": "Sort order for CSV sort (default: true)"},
            "save":      {"type": "BOOLEAN", "description": "Save result to file (default: true)"},
            "destination": {"type": "STRING", "description": "Output folder for archive extract"},
        },
        "required": []
    }
},
    {
    "name": "shutdown_jeeves",
    "description": (
        "Shuts down the assistant completely. "
        "Call this when the user expresses intent to end the conversation, "
        "close the assistant, say goodbye, or stop Jeeves. "
        "The user can say this in ANY language."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {},
    }
    },
    {
        "name": "save_memory",
        "description": (
            "Save an important personal fact about the user to long-term memory. "
            "Call this silently whenever the user reveals something worth remembering: "
            "name, age, city, job, preferences, hobbies, relationships, projects, or future plans. "
            "Do NOT call for: weather, reminders, searches, or one-time commands. "
            "Do NOT announce that you are saving — just call it silently. "
            "Values must be in English regardless of the conversation language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "category": {
                    "type": "STRING",
                    "description": (
                        "identity — name, age, birthday, city, job, language, nationality | "
                        "preferences — favorite food/color/music/film/game/sport, hobbies | "
                        "projects — active projects, goals, things being built | "
                        "relationships — friends, family, partner, colleagues | "
                        "wishes — future plans, things to buy, travel dreams | "
                        "notes — habits, schedule, anything else worth remembering"
                    )
                },
                "key":   {"type": "STRING", "description": "Short snake_case key (e.g. name, favorite_food, sister_name)"},
                "value": {"type": "STRING", "description": "Concise value in English (e.g. Fatih, pizza, older sister)"},
            },
            "required": ["category", "key", "value"]
        }
    },
]


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
        self.ui.on_text_command = self._on_text_command

    def _on_text_command(self, text: str):
        if not self._loop:
            return
        asyncio.run_coroutine_threadsafe(self._handle_utterance(text), self._loop)

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
        try:
            pcm = await asyncio.to_thread(synthesize_to_pcm, text)
            if pcm:
                await self.audio_out_queue.put(pcm)
        except Exception as e:
            print(f"[JEEVES] ❌ TTS: {e}")

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
                r = await loop.run_in_executor(None, lambda: open_app(parameters=args, response=None, player=self.ui))
                result = r or f"Opened {args.get('app_name')}."

            elif name == "weather_report":
                r = await loop.run_in_executor(None, lambda: weather_action(parameters=args, player=self.ui))
                result = r or "Weather delivered."

            elif name == "browser_control":
                r = await loop.run_in_executor(None, lambda: browser_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "file_controller":
                r = await loop.run_in_executor(None, lambda: file_controller(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "send_message":
                r = await loop.run_in_executor(None, lambda: send_message(parameters=args, response=None, player=self.ui, session_memory=None))
                result = r or f"Message sent to {args.get('receiver')}."

            elif name == "reminder":
                r = await loop.run_in_executor(None, lambda: reminder(parameters=args, response=None, player=self.ui))
                result = r or "Reminder set."

            elif name == "youtube_video":
                r = await loop.run_in_executor(None, lambda: youtube_video(parameters=args, response=None, player=self.ui))
                result = r or "Done."
            elif name == "file_processor":
                if not args.get("file_path") and self.ui.current_file:
                    args["file_path"] = self.ui.current_file
                r = await loop.run_in_executor(
                    None,
                    lambda: file_processor(parameters=args, player=self.ui, speak=self.speak)
                )
                result = r or "Done."


            elif name == "screen_process":
                threading.Thread(
                    target=screen_process,
                    kwargs={"parameters": args, "response": None,
                            "player": self.ui, "session_memory": None},
                    daemon=True
                ).start()
                result = "Vision module activated. Stay completely silent — vision module will speak directly."

            elif name == "computer_settings":
                r = await loop.run_in_executor(None, lambda: computer_settings(parameters=args, response=None, player=self.ui))
                result = r or "Done."

            elif name == "desktop_control":
                r = await loop.run_in_executor(None, lambda: desktop_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "code_helper":
                r = await loop.run_in_executor(None, lambda: code_helper(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "dev_agent":
                r = await loop.run_in_executor(None, lambda: dev_agent(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "agent_task":
                from agent.task_queue import get_queue, TaskPriority
                priority_map = {"low": TaskPriority.LOW, "normal": TaskPriority.NORMAL, "high": TaskPriority.HIGH}
                priority = priority_map.get(args.get("priority", "normal").lower(), TaskPriority.NORMAL)
                task_id  = get_queue().submit(goal=args.get("goal", ""), priority=priority, speak=self.speak)
                result   = f"Task started (ID: {task_id})."

            elif name == "web_search":
                r = await loop.run_in_executor(None, lambda: web_search_action(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "computer_control":
                r = await loop.run_in_executor(None, lambda: computer_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "game_updater":
                r = await loop.run_in_executor(None, lambda: game_updater(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "flight_finder":
                r = await loop.run_in_executor(None, lambda: flight_finder(parameters=args, player=self.ui))
                result = r or "Done."
            elif name == "shutdown_jeeves":
                self.ui.write_log("SYS: Shutdown requested.")
                self.speak("Goodbye, sir.")

                def _shutdown():
                    import time, sys, os
                    time.sleep(1)
                    os._exit(0)

                threading.Thread(target=_shutdown, daemon=True).start()
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
            text = await asyncio.to_thread(transcribe_pcm16, pcm_bytes, SEND_SAMPLE_RATE)
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
        self.ui.set_state("THINKING")
        self.conversation.append({"role": "user", "content": text})

        try:
            system_prompt = self._build_system_prompt()
            reply = await asyncio.to_thread(
                brain_client.multi_turn,
                [{"role": "system", "content": system_prompt}] + self.conversation[-20:],
            )
        except Exception as e:
            print(f"[JEEVES] ❌ Brain: {e}")
            self.speak("Sir, I had trouble reaching my reasoning engine just now.")
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
            return

        self.conversation.append({"role": "assistant", "content": reply})

        tool_name, tool_args = self._parse_tool_call(reply)

        if tool_name:
            print(f"[JEEVES] 📞 {tool_name}")
            result = await self._execute_tool(tool_name, tool_args)
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
                    followup = await asyncio.to_thread(
                        brain_client.multi_turn,
                        [{"role": "system", "content": system_prompt}] + self.conversation[-20:],
                    )
                    self.conversation.append({"role": "assistant", "content": followup})
                    self.speak(followup)
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
                self.set_speaking(True)
                await asyncio.to_thread(stream.write, chunk)
                self.set_speaking(False)
        except Exception as e:
            print(f"[JEEVES] ❌ Play: {e}")
            raise
        finally:
            self.set_speaking(False)
            stream.stop()
            stream.close()

    async def run(self):
        while True:
            try:
                print("[JEEVES] Starting local voice pipeline (Whisper + Groq + Piper)...")
                self.ui.set_state("THINKING")
                self._loop           = asyncio.get_event_loop()
                self.audio_out_queue = asyncio.Queue()

                print("[JEEVES] Ready.")
                self.ui.set_state("LISTENING")
                self.ui.write_log("SYS: JEEVES online (local voice pipeline).")

                async with asyncio.TaskGroup() as tg:
                    tg.create_task(self._listen_audio())
                    tg.create_task(self._play_audio())

            except Exception as e:
                print(f"[JEEVES] Error: {e}")
                traceback.print_exc()

            self.set_speaking(False)
            self.ui.set_state("THINKING")
            print("[JEEVES] Restarting voice pipeline in 3s...")
            await asyncio.sleep(3)


def main():
    ui = JeevesUI("face.png")

    def runner():
        ui.wait_for_api_key()
        jeeves = JeevesLive(ui)
        try:
            asyncio.run(jeeves.run())
        except KeyboardInterrupt:
            print("\n🔴 Shutting down...")

    threading.Thread(target=runner, daemon=True).start()
    ui.root.mainloop()


if __name__ == "__main__":
    main()
