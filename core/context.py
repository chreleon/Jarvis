"""
core/context.py -- Shared conversation context management.

Consolidates the duplicate _trim_context() that existed in both main.py
and cli.py. Both entry points now import from here.

The trimming strategy:
  1. Keep the newest message (current user utterance) always.
  2. Cap each message at MAX_MSG_CHARS.
  3. Cap total context at MAX_HISTORY_CHARS.
  4. Never touch the system message (tool declarations live there).
"""

from __future__ import annotations

# ── Context budget constants ─────────────────────────────────────────────────
# These caps prevent the 413 "Payload Too Large" errors on free-tier
# token budgets. Tuned from observed Groq free-tier limits.
HISTORY_WINDOW_TURNS = 10       # max conversation turns sent to brain
MAX_MSG_CHARS        = 2500     # cap per-message content
MAX_HISTORY_CHARS    = 4000     # cap total conversation history
TOOL_RESULT_CHARS    = 1200     # cap tool output fed back to LLM


def trim_context(messages: list[dict],
                 history_window: int = HISTORY_WINDOW_TURNS,
                 max_msg_chars: int = MAX_MSG_CHARS,
                 max_history_chars: int = MAX_HISTORY_CHARS) -> list[dict]:
    """Trim conversation messages to fit within token budget.

    The newest message is always kept (it's the current utterance).
    Older messages are capped and dropped from oldest-first when the
    total budget is exceeded.

    Args:
        messages: List of {"role": ..., "content": ...} dicts.
        history_window: Maximum number of recent turns to consider.
        max_msg_chars: Per-message character cap.
        max_history_chars: Total character budget for all messages.

    Returns:
        Trimmed list of messages, oldest first, within budget.
    """
    trimmed: list[dict] = []
    budget = max_history_chars

    for m in reversed(messages):
        content = m.get("content") or ""
        if isinstance(content, str) and len(content) > max_msg_chars:
            content = content[:max_msg_chars - 1] + "…"
        cost = len(content) + 64  # small overhead for role/structure
        if trimmed and budget - cost < 0:
            break
        budget -= cost
        trimmed.append({**m, "content": content})

    return list(reversed(trimmed))


def truncate_tool_result(result: str, max_chars: int = TOOL_RESULT_CHARS) -> str:
    """Truncate a tool execution result before feeding back to the LLM.

    Tool outputs (web search, file analysis, phone status, etc.) can be
    huge, and every turn re-sends the whole context. Unbounded results
    push multi-turn requests over the token budget (413).
    """
    if not result or len(result) <= max_chars:
        return result
    return result[:max_chars - 20] + f"\n…[{len(result)} chars total]"
