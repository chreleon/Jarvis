"""
composio_agent.py -- Gives Jarvis real "hands" via Composio: GitHub, Gmail,
Google Calendar, and any other app you've connected in your Composio account.

Runs on Groq (free) using Composio's OpenAI-compatible toolset -- Groq's API
is OpenAI-compatible, so composio_openai works with it directly.

Setup (one-time, in your terminal):
    pip install composio-core composio-openai
    composio login
    composio add github
    composio add gmail
    composio add googlecalendar
    # (add any other app you want Jarvis to control the same way)

Each `composio add <app>` walks you through an OAuth flow in your browser --
same kind of "connect your account" step used elsewhere in this project.
"""

import json
import logging

from groq import Groq
from composio_openai import ComposioToolSet, App

from or_client import _load_api_key as _load_groq_key

logger = logging.getLogger("composio_agent")

AGENT_MODEL = "llama-3.3-70b-versatile"

# Which Composio-connected apps Jarvis is allowed to use.
# Add/remove App.<NAME> entries here to control what Jarvis can touch.
ENABLED_APPS = [
    App.GITHUB,
    App.GMAIL,
    App.GOOGLECALENDAR,
]

_toolset = None
_groq_client = None


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


def run_agentic_task(user_text: str, system_prompt: str = None, max_turns: int = 6) -> str:
    """
    Sends `user_text` to Groq with Composio tools attached. If the model
    decides to call a tool (e.g. "star this GitHub repo", "check my next
    calendar event", "send an email to X"), Composio executes it for real
    and the result is fed back to the model until it gives a final answer.
    """
    toolset = _get_toolset()
    client = _get_groq_client()
    tools = toolset.get_tools(apps=ENABLED_APPS)

    system_prompt = system_prompt or (
        "You are JARVIS, a personal assistant with real access to the user's "
        "GitHub, Gmail, and Google Calendar via connected tools. Use a tool "
        "whenever the request requires checking or changing something in "
        "those accounts. Be concise in your final reply."
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
        # let Composio execute the whole batch and hand back results.
        messages.append(message.model_dump())

        try:
            tool_results = toolset.handle_tool_calls(response)
        except Exception as e:
            logger.error(f"[ComposioAgent] Tool execution failed: {e}")
            tool_results = [{"error": str(e)}] * len(message.tool_calls)

        for tool_call, tool_result in zip(message.tool_calls, tool_results):
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "name": tool_call.function.name,
                "content": json.dumps(tool_result) if not isinstance(tool_result, str) else tool_result,
            })

    return "I wasn't able to finish that within the allotted steps -- could you narrow the request down?"


if __name__ == "__main__":
    print("JARVIS Composio Agent -- Self-Test")
    try:
        print(run_agentic_task("What GitHub repos do I own?"))
    except Exception as e:
        print("FAIL:", e)
