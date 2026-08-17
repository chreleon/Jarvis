# actions/meta_ai.py
# meta_ai — ask Meta AI (WhatsApp's built-in assistant) through the SAME
# background browser the secretary monitor and sends use.
#
# Meta AI is a full LLM living inside WhatsApp: it answers questions,
# researches, and can generate images ("imagine ..."). There is no public
# API — but we already drive WhatsApp Web's DOM, so the assistant is one
# chat-row click away. Uses:
#   1. A `meta_ai` tool the Jeeves brain can call ("ask Meta AI: ...").
#   2. A brain fallback: when the configured LLM providers fail (key pool
#      exhausted, rate-limited, offline), Jeeves routes the question to
#      Meta AI so it never goes "I can't reach my brain".
#
# Cost discipline (YinYang): no new browser, no new login — the shared
# bridge is reused when the monitor/send path already runs one; otherwise a
# headless browser is launched for the call and released after (exactly the
# send path's pattern). No screenshots, no vision, pure DOM reads. One
# question at a time; the reply wait is capped so a stuck AI can't hang the
# caller.
#
# Importing this module is cheap (no playwright import at module load).


def _bridge_config():
    """Headless/CDP config from config/api_keys.json — one source of truth
    with the send path (secretary_headless / secretary_cdp_url)."""
    from actions.send_message import _bridge_config as _send_config
    return _send_config()


def _ask_bridge(question: str, timeout: int = 90) -> str:
    """Run one Meta AI question through the shared bridge; returns the reply
    text. Raises RuntimeError with a clear message on any failure."""
    from actions.whatsapp_bridge import (
        acquire_shared_bridge, release_shared_bridge, is_profile_linked)
    headless, cdp_url = _bridge_config()
    bridge, created = acquire_shared_bridge(headless=headless, cdp_url=cdp_url)
    if created and not cdp_url and not is_profile_linked():
        # Never linked: don't cold-launch Chromium just to discover the QR
        # is needed — fail fast so the secretary falls back to its instant
        # deterministic draft (YinYang: skip work that is known to fail).
        release_shared_bridge(bridge)
        raise RuntimeError(
            "WhatsApp Web is not linked yet — say 'link whatsapp' (or "
            "'secretary link') once to open the window and scan the QR")
    try:
        bridge.start()   # idempotent — reuses a live session
        # Cold browsers take ~10s to load WhatsApp Web and restore the saved
        # session; wait for it (a QR appearing means it's really not linked).
        if not bridge.wait_logged_in(timeout=45):
            if bridge.needs_qr():
                raise RuntimeError(
                    "WhatsApp Web is not linked in the background browser — "
                    "say 'link whatsapp' (or 'secretary link') once to open "
                    "the window and scan the QR with your phone")
            raise RuntimeError(
                "WhatsApp Web took too long to restore the saved session "
                "(cold start). Try again in a moment.")
        return bridge.meta_ai_ask(question, timeout=timeout)
    finally:
        release_shared_bridge(bridge)


def meta_ai(parameters: dict, player=None) -> str:
    """Tool entry point. parameters: {question: str} (or {prompt: str}).

    Asks Meta AI inside WhatsApp and returns its reply as plain text. The
    question must be something worth asking an AI (Meta AI is not wired for
    system/tool actions — it answers questions, researches, brainstorms,
    and can imagine images)."""
    params = parameters or {}
    question = str(params.get("question") or params.get("prompt") or "").strip()
    if not question:
        return ("Please give me a question to ask Meta AI, sir — e.g. "
                "tool meta_ai question='what is the capital of Kenya?'")
    print(f"[MetaAI] asking Meta AI: {question[:60]}")
    try:
        reply = _ask_bridge(question)
    except RuntimeError as e:
        return f"Meta AI unavailable: {e}"
    except Exception as e:
        return f"Meta AI failed: {type(e).__name__}: {e}"
    if not reply or not reply.strip():
        return "Meta AI replied with nothing."
    return reply.strip()
