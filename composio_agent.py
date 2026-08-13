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
from types import SimpleNamespace

from groq import Groq

from or_client import _load_api_key as _load_groq_key
from composio_shim import ComposioToolSet, App


logger = logging.getLogger("composio_agent")
AGENT_MODEL = "llama-3.3-70b-versatile"

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
        _groq_client = Groq(api_key=_load_groq_key())
    return _groq_client


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


def _sanitize_tools(tools: list) -> list:
    """Strip fields that Groq's API rejects from Composio tool definitions.

    Groq's /v1/chat/completions endpoint rejects tool definitions that
    include a non-null ``strict`` property on ``function``, e.g.
    ``{"strict": False}``.  Composio SDK versions that set this field on
    every tool cause a 400 error, so we remove it here.
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

        func = tool.get("function", {})
        if isinstance(func, dict):
            func = dict(func)
            func.pop("strict", None)
            tool["function"] = func

        sanitized.append(tool)

    return sanitized


def run_agentic_task(user_text: str, system_prompt: str = None, max_turns: int = 6) -> str:
    """
    Sends `user_text` to Groq with Composio tools attached. If the model
    decides to call a tool (e.g. "star this GitHub repo", "check my next
    calendar event", "send an email to X"), Composio executes it for real
    and the result is fed back to the model until it gives a final answer.
    """
    toolset = _get_toolset()
    client = _get_groq_client()
    tools = _sanitize_tools(toolset.get_tools(apps=ENABLED_APPS or None))

    # Merge in tools from configured MCP servers (e.g. Composio's hosted
    # gateway). Optional: if none are configured / reachable this is a no-op.
    mcp = _get_mcp()
    try:
        mcp_tools = mcp.get_tools()
    except Exception as e:
        logger.warning(f"[ComposioAgent] MCP tools unavailable: {e}")
        mcp_tools = []
    tools = tools + mcp_tools

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
        response = client.chat.completions.create(
            model=AGENT_MODEL,
            messages=messages,
            tools=tools,
            tool_choice="auto",
        )
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
            if mcp.has_tool(name):
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
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "name": tool_call.function.name,
                "content": json.dumps(tool_result) if not isinstance(tool_result, str) else tool_result,
            })

    return "I wasn't able to finish that within the allotted steps -- could you narrow the request down?"


if __name__ == "__main__":
    print("JEEVES Composio Agent -- Self-Test")
    try:
        print(run_agentic_task("What GitHub repos do I own?"))
    except Exception as e:
        print("FAIL:", e)
