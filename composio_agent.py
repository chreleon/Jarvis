"""
composio_agent.py -- Gives Jeeves real "hands" via Composio: GitHub, Gmail,
Google Calendar, and any other app you've connected in your Composio account.

Runs on Groq (free) using Composio's OpenAI-compatible toolset -- Groq's API
is OpenAI-compatible, so composio_openai works with it directly.

Setup (one-time, in your terminal):
    pip install composio-core composio-openai
    composio login
    composio add github
    composio add gmail
    composio add googlecalendar
    # (add any other app you want Jeeves to control the same way)

Each `composio add <app>` walks you through an OAuth flow in your browser --
same kind of "connect your account" step used elsewhere in this project.
"""

import json
import logging
import time
from types import SimpleNamespace
from typing import Any

from groq import Groq

from or_client import GROQ_DEFAULT_MODEL, GROQ_LITE_MODEL, _groq_pool, client as brain_client
from composio_shim import ComposioToolSet, App


logger = logging.getLogger("composio_agent")
AGENT_MODEL = GROQ_DEFAULT_MODEL
# Groq hard limit on the tools array: any request with more tools is rejected
# with 400 "'tools' : maximum number of items is 128". Connected accounts can
# expose far more (github alone fills a 200-tool page), so every agent call
# must stay under this budget.
MAX_TOOLS = 128
# Cap on each tool-result message fed back to the model: gmail output can be
# huge, and every turn re-sends the whole tools payload against the same
# per-minute token budget, so unbounded results would push multi-turn agent
# requests over it (413 "Request too large").
TOOL_RESULT_CHARS = 2000

# Which Composio-connected apps Jarvis is allowed to use.
# Leave this as an empty list to expose the full connected Composio toolset
# for the current user instead of hard-coding a small subset.
ENABLED_APPS = []

_toolset = None
_groq_client = None
_mcp = None


def _get_toolset() -> ComposioToolSet:
    global _toolset
    if _toolset is None:
        _toolset = ComposioToolSet()
    return _toolset


def _get_groq_client() -> Groq:
    global _groq_client
    if _groq_client is None:
        # Use the shared key pool so agent calls rotate across all configured
        # keys — each free-tier key has its own per-minute TPM budget.
        _groq_client = Groq(api_key=_groq_pool.current())
    return _groq_client


_QUOTA_MARKERS = (
    "429",
    "rate limit",
    "rate_limit_exceeded",
    "tokens per minute",
    "request too large",
    "quota",
)


def _is_quota_error(exc: Exception) -> bool:
    """True for rate-limit / token-budget rejections (429, 413 "Request too
    large", TPM exhaustion) that rotating to another key can fix."""
    text = str(exc).lower()
    return any(marker in text for marker in _QUOTA_MARKERS)


class _ModelExhausted(Exception):
    """Raised when a model is rejected by quota errors on every configured
    key — the caller should retry with a different model (each model has its
    own token-per-day budget on Groq's free tier)."""

    def __init__(self, last_error: Exception):
        super().__init__(str(last_error))
        self.last_error = last_error


def _create_with_key_rotation(messages: list, tools: list,
                              model: str = AGENT_MODEL) -> Any:
    """Call Groq with the shared key pool; rotate keys on quota errors and
    fall back to the lite model when the primary one is exhausted.

    A free-tier key has a 12k TPM budget, and a multi-turn agent loop burns
    the whole tools payload on every turn — one exhausted key must not fail
    a turn when 2+ more keys are configured. Worse, daily token caps are
    per-MODEL: when every key is TPD-capped on the primary model (the
    observed "tokens per day (TPD)" 429s), the lite model still answers, so
    this retries the whole call on it before giving up.
    """
    models = [model]
    if model != GROQ_LITE_MODEL:
        models.append(GROQ_LITE_MODEL)
    last_error: Exception | None = None
    for candidate in models:
        try:
            return _create_with_key_rotation_model(messages, tools, candidate)
        except _ModelExhausted as exc:
            last_error = exc.last_error
            logger.warning(
                f"[ComposioAgent] {candidate} quota-exhausted on all keys "
                f"({str(exc.last_error)[:120]}); trying lite model"
            )
    raise last_error or RuntimeError(
        "Composio agent: Groq quota exhausted on every model and key")


def _create_with_key_rotation_model(messages: list, tools: list,
                                    model: str) -> Any:
    """One model: round-robin the shared key pool with error classification.

    * payload errors (413 / request too large) raise immediately — no key
      can shrink a request;
    * daily-cap errors (TPD) park the key until tomorrow instead of a 60s
      cooldown, so it isn't re-hit every lap;
    * per-minute rate limits get the short cooldown and the next key.
    When every key is parked long-term, the rest of the lap is skipped
    (retrying is pointless) and _ModelExhausted lets the caller try another
    model. Fresh clients are built per attempt (the pool hands out whichever
    key is current).
    """
    attempts = max(2, _groq_pool.size())
    last_error: Exception | None = None
    for _ in range(attempts):
        key = _groq_pool.current()
        try:
            return Groq(api_key=key).chat.completions.create(
                model=model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
            )
        except Exception as exc:
            last_error = exc
            if brain_client._is_payload_error(exc):
                raise  # too big for the model — rotating keys can't fix it
            if not _is_quota_error(exc):
                raise
            if brain_client._is_daily_cap_error(exc):
                _groq_pool.mark_daily_capped(key)
            else:
                _groq_pool.mark_rate_limited(key)
            _groq_pool.advance()
            # All keys parked for the long term (daily caps) won't recover
            # within the per-minute window — don't burn the rest of the lap.
            recovery_at = _groq_pool.earliest_recovery()
            if (recovery_at is not None
                    and recovery_at - time.time() > _groq_pool.MAX_RECOVERY_WAIT_S):
                break
    raise _ModelExhausted(last_error)


def _get_mcp():
    """Lazily import the MCP client manager (optional feature)."""
    global _mcp
    if _mcp is None:
        from mcp_client import get_mcp_manager
        _mcp = get_mcp_manager()
    return _mcp


def _response_with_calls(tool_calls: list) -> SimpleNamespace:
    """Wrap tool calls into a minimal response object the toolset can execute.

    Lets us hand Composio only the tool calls that belong to it when a batch
    mixes Composio and MCP tools.
    """
    message = SimpleNamespace(tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _minify_tool(tool: dict, desc_limit: int = 100) -> dict:
    """Shrink one Composio tool schema to the bare minimum Groq needs.

    Composio tool definitions carry long per-property prose; gmail's 63
    tools alone serialize to ~38k tokens — over 3x Groq's free-tier 12k
    TPM budget, so even a gmail-only request was rejected (413 "Request too
    large"). Keeping the tool name, a short description, and each
    parameter's name/type (plus enum/items where present) cuts that to
    ~4k tokens while leaving the model enough to pick and fill the right
    tool.
    """
    fn = tool.get("function") or {}
    params = fn.get("parameters") or {}
    props = params.get("properties") or {}
    min_props: dict = {}
    for key, val in props.items():
        if not isinstance(val, dict):
            min_props[key] = {"type": "string"}
            continue
        entry = {k: val[k] for k in ("type", "enum", "items") if k in val}
        min_props[key] = entry or {"type": "string"}
    return {
        "type": "function",
        "function": {
            "name": str(fn.get("name", "")),
            "description": str(fn.get("description") or "")[:desc_limit],
            "parameters": {
                "type": "object",
                "properties": min_props,
                "required": list(params.get("required") or []),
            },
        },
    }


def _sanitize_tools(tools: list) -> list:
    """Normalize + minify Composio tool definitions for Groq.

    Two jobs in one pass:
      * drop fields Groq rejects (non-null ``strict`` on ``function``),
      * minify the schema so the combined tools payload fits the free-tier
        token budget (see _minify_tool).
    """
    sanitized: list = []
    for tool in tools:
        # Convert Pydantic / SDK model objects to plain dicts if needed
        if hasattr(tool, "model_dump"):
            tool = tool.model_dump()
        elif hasattr(tool, "model_dump_json"):
            tool = json.loads(tool.model_dump_json())
        elif not isinstance(tool, dict):
            # Leave unknown types alone — they will likely fail at the
            # Groq client layer anyway, but at least we tried.
            sanitized.append(tool)
            continue
        else:
            tool = dict(tool)  # shallow copy

        sanitized.append(_minify_tool(tool))

    return sanitized


def _select_apps(user_text: str) -> list | None:
    """Pick the Composio apps relevant to `user_text`, or None (use all).

    Keeps the tools list small (so a request never exceeds Groq's 128-tool
    cap) and, more importantly, guarantees the toolkit the request actually
    needs is present instead of being truncated away when "all connected
    tools" is too big.
    """
    text = (user_text or "").lower()
    if any(w in text for w in ("email", "mail", "inbox", "gmail")):
        return [App.GMAIL]
    if any(w in text for w in ("calendar", "event", "schedule", "appointment", "meeting")):
        return [App.GOOGLECALENDAR]
    if any(w in text for w in ("github", "repo", "repository", "pull request")):
        return [App.GITHUB]
    if "triggercmd" in text:
        return [App.TRIGGERCMD]
    return None


def _merge_and_cap_tools(composio_tools: list, mcp_tools: list,
                         max_tools: int = MAX_TOOLS) -> list:
    """Merge Composio + MCP tools and enforce Groq's 128-tool cap.

    MCP tools are user-configured and few, so they are kept first; the rest
    of the budget goes to Composio tools (which _select_apps has already
    narrowed to the request's toolkits when possible).
    """
    if len(composio_tools) + len(mcp_tools) <= max_tools:
        return composio_tools + mcp_tools
    if len(mcp_tools) >= max_tools:
        return list(mcp_tools[:max_tools])
    return list(mcp_tools) + list(composio_tools[: max_tools - len(mcp_tools)])


def run_agentic_task(user_text: str, system_prompt: str = None, max_turns: int = 6) -> str:
    """
    Sends `user_text` to Groq with Composio tools attached. If the model
    decides to call a tool (e.g. "star this GitHub repo", "check my next
    calendar event", "send an email to X"), Composio executes it for real
    and the result is fed back to the model until it gives a final answer.
    """
    toolset = _get_toolset()
    # Narrow to the toolkits the request actually needs when ENABLED_APPS
    # doesn't already pin one: connected accounts can expose 200+ tools
    # (github alone), and Groq rejects any request with more than 128 with
    # a 400 "'tools' : maximum number of items is 128" — which previously
    # broke every /agent call, e.g. "read my email".
    apps = ENABLED_APPS or _select_apps(user_text) or None
    tools = _sanitize_tools(toolset.get_tools(apps=apps))

    # Merge in tools from configured MCP servers (e.g. Composio's hosted
    # gateway). Optional: if none are configured / reachable this is a no-op.
    mcp = None
    try:
        mcp = _get_mcp()
        mcp_tools = mcp.get_tools()
    except Exception as e:
        logger.warning(f"[ComposioAgent] MCP tools unavailable: {e}")
        mcp_tools = []
    tools = _merge_and_cap_tools(tools, mcp_tools)

    system_prompt = system_prompt or (
        "You are JEEVES, a personal assistant with real access to the user's "
        "GitHub, Gmail, and Google Calendar via connected tools, plus any tools "
        "exposed by configured MCP servers. Use a tool whenever the request "
        "requires checking or changing something in those accounts. Be concise "
        "in your final reply."
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_text},
    ]

    for _ in range(max_turns):
        response = _create_with_key_rotation(messages, tools)
        message = response.choices[0].message

        if not message.tool_calls:
            return (message.content or "").strip()

        # Model wants to call one or more tools -- append its request, then
        # execute the batch: MCP tools through the MCP client, everything else
        # through Composio.
        messages.append(message.model_dump())

        tool_results: list = [None] * len(message.tool_calls)
        composio_calls: list = []
        composio_indices: list[int] = []

        for i, tool_call in enumerate(message.tool_calls):
            name = tool_call.function.name
            try:
                arguments = json.loads(tool_call.function.arguments or "{}")
            except (json.JSONDecodeError, TypeError):
                arguments = {}

            # MCP tools take precedence over Composio tools on name collision
            # (MCP is checked first); names are otherwise passed through as-is.
            if mcp is not None and mcp.has_tool(name):
                result_text, err = mcp.execute(name, arguments)
                tool_results[i] = {"result": result_text} if err is None else {"error": err}
            else:
                composio_calls.append(tool_call)
                composio_indices.append(i)

        if composio_calls:
            try:
                results = toolset.handle_tool_calls(_response_with_calls(composio_calls))
            except Exception as e:
                logger.error(f"[ComposioAgent] Tool execution failed: {e}")
                results = [{"error": str(e)}] * len(composio_calls)
            for index, result in zip(composio_indices, results):
                tool_results[index] = result

        for tool_call, tool_result in zip(message.tool_calls, tool_results):
            if tool_result is None:
                tool_result = {"error": "No result produced for this tool call"}
            content = json.dumps(tool_result) if not isinstance(tool_result, str) else tool_result
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "name": tool_call.function.name,
                # Truncate so multi-turn requests stay inside the TPM budget.
                "content": content[:TOOL_RESULT_CHARS],
            })

    return "I wasn't able to finish that within the allotted steps -- could you narrow the request down?"


if __name__ == "__main__":
    print("JEEVES Composio Agent -- Self-Test")
    try:
        print(run_agentic_task("What GitHub repos do I own?"))
    except Exception as e:
        print("FAIL:", e)
