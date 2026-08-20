"""
core/action_protocol.py -- Formal interface for Jeeves action modules.

Every action module in actions/ should conform to this protocol. The
protocol defines the expected function signature, return type, and
the `player` interface that actions receive.

This is a documentation/convention layer, not a runtime-enforced ABC.
Python's duck typing means we document the contract here and verify
it's followed during code review. The type hints serve as the
authoritative contract for IDE support and static analysis.

Usage:
    from core.action_protocol import ActionFunc, PlayerProtocol

    def my_action(parameters: dict, player: PlayerProtocol) -> str:
        ...
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class PlayerProtocol(Protocol):
    """Duck-typed interface that action modules receive as `player`.

    GUI passes JeevesUI; CLI passes ConsolePlayer. Both must implement
    these methods so actions work identically in either mode.
    """

    def write_log(self, text: str) -> None:
        """Write a line to the activity log / HUD."""
        ...

    def set_state(self, state: str) -> None:
        """Set the HUD state (LISTENING, THINKING, SPEAKING, etc.)."""
        ...

    @property
    def muted(self) -> bool:
        """Whether audio output is muted."""
        ...

    @property
    def current_file(self) -> str | None:
        """Path to the currently attached file, if any."""
        ...


# ── Action function signature ──────────────────────────────────────────────
# Every action module exposes a top-level function with this signature:
#
#   def action_name(parameters: dict, player=None, **kwargs) -> str:
#       ...
#
# The function must:
#   - Accept `parameters: dict` (tool arguments from the LLM or CLI)
#   - Accept `player` (optional, duck-typed PlayerProtocol)
#   - Accept `**kwargs` for additional context (speak callback, etc.)
#   - Return a string result (fed back to the LLM as tool output)
#   - Return "__SILENT__" if the action speaks for itself (e.g. screen_process)
#   - Handle missing/invalid parameters gracefully (return error string)
#   - Never raise exceptions — catch and return error messages
#
# Common kwargs passed by main.py and cli.py:
#   - speak: callable for TTS output
#   - player: the UI/CLI adapter
#   - No other kwargs are guaranteed.

# Type alias for action functions (for documentation/static analysis)
ActionFunc = callable  # (parameters: dict, player=None, **kwargs) -> str


# ── Registry of known action modules ───────────────────────────────────────
# This list helps with documentation, tool generation, and validation.
# It's NOT used for runtime dispatch (that's in _load_runtime_imports).

ACTION_MODULES = {
    "open_app":             "actions.open_app",
    "web_search":           "actions.web_search",
    "system_status":        "actions.system_monitor",
    "manage_monitor":       "actions.background_monitor",
    "business_tracker":     "actions.business_tracker",
    "daily_briefing":       "actions.daily_briefing",
    "anime_watch":          "actions.anime_watch",
    "secretary":            "actions.secretary",
    "weather_report":       "actions.weather",
    "send_message":         "actions.send_message",
    "reminder":             "actions.reminder",
    "youtube_video":        "actions.youtube_video",
    "screen_process":       "actions.screen_processor",
    "computer_settings":    "actions.computer_settings",
    "browser_control":      "actions.browser_control",
    "file_controller":      "actions.file_controller",
    "desktop_control":      "actions.desktop",
    "code_helper":          "actions.code_helper",
    "dev_agent":            "actions.dev_agent",
    "computer_control":     "actions.computer_control",
    "cmd_control":          "actions.cmd_control",
    "game_updater":         "actions.game_updater",
    "flight_finder":        "actions.flight_finder",
    "file_processor":       "actions.file_processor",
    "phone_control":        "actions.phone_control",
    "meta_ai":              "actions.meta_ai",
}


def validate_action_signature(func) -> list[str]:
    """Check that an action function follows the protocol.

    Returns a list of issues (empty = compliant).
    """
    issues = []
    import inspect

    sig = inspect.signature(func)
    params = list(sig.parameters.keys())

    if not params:
        issues.append("Missing 'parameters' argument")
    elif params[0] != "parameters":
        issues.append(f"First arg should be 'parameters', got '{params[0]}'")

    if len(params) < 2 or params[1] != "player":
        issues.append("Second arg should be 'player'")

    if sig.return_annotation not in (inspect.Parameter.empty, str):
        issues.append(f"Return annotation should be 'str', got '{sig.return_annotation}'")

    return issues
