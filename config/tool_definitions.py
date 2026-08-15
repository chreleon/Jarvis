"""
config/tool_definitions.py -- Shared tool declarations for Jeeves.

Canonical source of truth for all tool definitions used across the
application. Both main.py and cli.py import from here so that adding
or modifying a tool is a single change rather than two (or more).

TOOL_DECLARATIONS -- Full LLM-compatible format used in system prompts
                     (name, description, parameters with types).
TOOL_REGISTRY     -- Lighter display format for the CLI /tools listing
                     (name, description, usage example).
"""

# ── Full LLM-compatible declarations ──────────────────────────────────────
# These are sent to the LLM in the system prompt so it knows which tools
# are available and how to call them.

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
        "description": (
            "Searches the web for any information. Modes: search (default), "
            "news (latest headlines), research (deep dive with sources), "
            "price (pricing info), compare (items vs aspect)."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query":  {"type": "STRING", "description": "Search query"},
                "mode":   {"type": "STRING", "description": "search (default) | news | research | price | compare"},
                "items":  {"type": "ARRAY", "items": {"type": "STRING"}, "description": "Items to compare (compare mode)"},
                "aspect": {"type": "STRING", "description": "Comparison aspect: price | specs | reviews | battery | etc."}
            },
            "required": ["query"]
        }
    },
    {
        "name": "system_status",
        "description": (
            "Reports live system health: CPU %, RAM %, GPU %, CPU temperature, "
            "uptime, and process count. Use when the user asks how the "
            "computer is doing, performance, specs, or system status."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
            "required": []
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
            "clicking elements, filling forms, scrolling, any web-based task. "
            "Supports multiple browsers simultaneously via the 'browser' parameter "
            "(chrome, edge, firefox, brave, opera, vivaldi)."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "go_to | search | click | type | scroll | fill_form | smart_click | smart_type | get_text | press | close | list | close_all"},
                "browser":     {"type": "STRING", "description": "chrome | edge | firefox | brave | opera | vivaldi | safari (default: system default)"},
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
        "name": "cmd_control",
        "description": "Runs system commands or opens files from natural language instructions.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "task": {"type": "STRING", "description": "What to do in plain language"},
                "visible": {"type": "BOOLEAN", "description": "Whether the window should stay visible"}
            },
            "required": ["task"]
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
        "name": "composio_action",
        "description": (
            "Performs a real action on the user's connected GitHub, Gmail, or Google "
            "Calendar account -- e.g. check repos, star a repo, read/send email, "
            "check or create calendar events. Use this whenever the user asks about "
            "or wants to act on GitHub, Gmail, or their calendar."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "request": {
                    "type": "STRING",
                    "description": "Plain-language description of what to check or do, e.g. 'star my Jarvis repo' or 'what's on my calendar tomorrow'"
                }
            },
            "required": ["request"]
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

# ── Compact LLM rendering ──────────────────────────────────────────────────
# The full TOOL_DECLARATIONS JSON above is ~24KB (~6k tokens) per request.
# Most of that is per-parameter prose the model rarely needs. This compact
# rendering keeps the tool name, one-line description, and each parameter's
# name/type/required flag — cutting the tools section to roughly a quarter
# of the size so requests stay inside free-tier token budgets (the 413
# "Payload Too Large" failures in the logs came from shipping the full
# schema on every call).

_COMPACT_CACHE: str | None = None


def compact_tool_declarations() -> str:
    """Compact, token-cheap rendering of TOOL_DECLARATIONS for system prompts."""
    global _COMPACT_CACHE
    if _COMPACT_CACHE is not None:
        return _COMPACT_CACHE
    lines = []
    for t in TOOL_DECLARATIONS:
        name = str(t.get("name", "?"))
        desc = (t.get("description") or "").strip().splitlines()[0].strip()
        params = t.get("parameters") or {}
        props = params.get("properties") or {}
        required = set(params.get("required") or [])
        if props:
            sig = ", ".join(
                f"{k}:{(v.get('type') or 'string').lower()}{'' if k in required else '?'}"
                for k, v in props.items()
            )
        else:
            sig = ""
        lines.append(f"- {name}({sig}): {desc}")
    _COMPACT_CACHE = "\n".join(lines)
    return _COMPACT_CACHE


# ── Lightweight display registry ──────────────────────────────────────────
# Used by the CLI /tools listing for human-readable output.

TOOL_REGISTRY = [
    {"name": "open_app",         "description": "Opens any application on the computer.",                                                  "usage": "open_app app_name='WhatsApp'"},
    {"name": "web_search",       "description": "Multi-mode web search (search, news, research, price, compare).",                        "usage": "web_search mode='news' query='latest AI news'"},
    {"name": "system_status",    "description": "Reports live system health: CPU, RAM, GPU, temperature, uptime.",                    "usage": "system_status"},
    {"name": "weather_report",   "description": "Gets the weather forecast for a city.",                                                  "usage": "weather_report city='London'"},
    {"name": "send_message",     "description": "Sends a message via WhatsApp, Telegram, etc.",                                           "usage": "send_message receiver='John' message_text='Hi' platform='WhatsApp'"},
    {"name": "reminder",         "description": "Sets a timed reminder.",                                                                 "usage": "reminder date='2025-12-25' time='09:00' message='Christmas!'"},
    {"name": "youtube_video",    "description": "Plays, summarizes, or gets YouTube video info.",                                         "usage": "youtube_video action='play' query='music mix'"},
    {"name": "screen_process",   "description": "Captures and analyzes the screen or webcam.",                                            "usage": "screen_process text='What is on my screen?'"},
    {"name": "computer_settings","description": "Controls volume, brightness, WiFi, shutdown, etc.",                                      "usage": "computer_settings action='volume' value='50'"},
    {"name": "browser_control",  "description": "Controls web browsers — navigate, click, fill; supports chrome/edge/firefox etc.",       "usage": "browser_control action='go_to' url='https://example.com' browser='firefox'"},
    {"name": "file_controller",  "description": "Manages files and folders.",                                                             "usage": "file_controller action='list' path='desktop'"},
    {"name": "desktop_control",  "description": "Controls the desktop: wallpaper, organize, stats.",                                      "usage": "desktop_control action='wallpaper' path='C:/image.jpg'"},
    {"name": "code_helper",      "description": "Writes, edits, explains, runs code files.",                                              "usage": "code_helper action='write' description='hello world' language='python'"},
    {"name": "dev_agent",        "description": "Builds complete multi-file projects from scratch.",                                      "usage": "dev_agent description='a flask web app'"},
    {"name": "computer_control", "description": "Direct mouse/keyboard control.",                                                         "usage": "computer_control action='type' text='hello'"},
    {"name": "cmd_control",      "description": "Runs system commands or opens files.",                                                   "usage": "cmd_control task='open notepad'"},
    {"name": "game_updater",     "description": "Updates/installs Steam and Epic Games.",                                                 "usage": "game_updater action='list' platform='steam'"},
    {"name": "flight_finder",    "description": "Searches Google Flights for best options.",                                              "usage": "flight_finder origin='NYC' destination='London' date='2025-06-15'"},
    {"name": "file_processor",   "description": "Processes uploaded files (images, PDFs, code, audio, etc.).",                           "usage": "file_processor file_path='doc.pdf' action='summarize'"},
    {"name": "agent_task",      "description": "Executes complex multi-step tasks requiring multiple tools.",                               "usage": "agent_task goal='research X and save to file'"},
    {"name": "composio_action",  "description": "Actions on GitHub, Gmail, Google Calendar.",                                            "usage": "composio_action request='check my emails'"},
    {"name": "shutdown_jeeves",  "description": "Shuts down the assistant completely.",                                                   "usage": "shutdown_jeeves"},
    {"name": "save_memory",      "description": "Saves personal facts about you (name, preferences, projects) to long-term memory.",     "usage": "save_memory category='identity' key='name' value='John'"},
]
