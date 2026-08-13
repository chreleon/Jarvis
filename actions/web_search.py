#web_search.py
"""
web_search.py -- Multi-mode web search for Jeeves.

Modes: search (default) | news | research | price | compare.

No Gemini: every LLM call routes through the active brain (Groq / GitHub
Models) via or_client. DuckDuckGo provides a real-web fallback, and news
queries race the LLM against DDG news in parallel, returning the first
valid result -- the Jeeves equivalent of Mark-L's first-result-wins idea
(without any Gemini dependency).
"""

import json  # noqa: F401  (kept for parity with other action modules)
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor

from core.utils import get_base_dir, BASE_DIR, CONFIG_PATH  # noqa: F401


# ── LLM brain routing (Groq / GitHub Models -- no Gemini) ──────────────────

def _llm_query(query: str, system: str) -> str:
    """Route an info query through the active brain (Groq / GitHub Models)."""
    from or_client import client
    text = client.chat(query, system=system).strip()
    if not text:
        raise ValueError("LLM returned an empty response.")
    return text


# ── DuckDuckGo backends ──────────────────────────────────────────────────────

def _ddg_search(query: str, max_results: int = 6) -> list[dict]:
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS

    results = []
    with DDGS() as ddgs:
        for r in ddgs.text(query, max_results=max_results):
            results.append({
                "title":   r.get("title",  ""),
                "snippet": r.get("body",   ""),
                "url":     r.get("href",   ""),
            })
    return results


def _ddg_news(query: str, max_results: int = 8) -> list[dict]:
    """DDG news search -- returns actual articles, not website homepages."""
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS

    results = []
    with DDGS() as ddgs:
        for r in ddgs.news(query, max_results=max_results):
            results.append({
                "title":   r.get("title",  ""),
                "snippet": r.get("body",   ""),
                "url":     r.get("url",    ""),
                "date":    r.get("date",   ""),
                "source":  r.get("source", ""),
            })
    return results


def _format_ddg(query: str, results: list[dict], kind: str = "results") -> str:
    if not results:
        return f"No {kind} found for: {query}"

    lines = [f"{kind.capitalize()} for: {query}\n"]
    for i, r in enumerate(results, 1):
        if r.get("title"):   lines.append(f"{i}. {r['title']}")
        if r.get("source") and r.get("date"):
            lines.append(f"   ({r['source']} -- {r['date']})")
        if r.get("snippet"): lines.append(f"   {r['snippet']}")
        if r.get("url"):     lines.append(f"   {r['url']}")
        lines.append("")
    return "\n".join(lines).strip()


def _format_price(query: str, results: list[dict]) -> str:
    """Price-mode DDG output: pull price-ish snippets first."""
    if not results:
        return f"No price results found for: {query}"

    lines = [f"Price check for: {query}\n"]
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        snippet = r.get("snippet", "")
        url = r.get("url", "")
        lines.append(f"{i}. {title}")
        if snippet:
            lines.append(f"   {snippet}")
        if url:
            lines.append(f"   {url}")
        lines.append("")
    return "\n".join(lines).strip()


# ── Parallel first-result-wins ───────────────────────────────────────────────

def _race_llm_vs_ddg(query: str, system: str, ddg_fn, timeout: int = 30) -> str:
    """Run the LLM and a DDG backend in parallel; return the first valid result.

    Whichever finishes first with real content wins -- Mark-L's
    first-result-wins idea, executed on Jeeves' Groq/GitHub-Models brain.
    """
    def _llm():
        try:
            return _llm_query(query, system)
        except Exception:
            return ""

    def _ddg():
        try:
            return ddg_fn()
        except Exception:
            return ""

    # NOTE: no `with ThreadPoolExecutor` here — exiting the context manager
    # calls shutdown(wait=True), which blocks until the *loser* future finishes
    # and defeats first-result-wins latency. We shutdown(wait=False) instead so
    # the loser keeps running in the background while we return the winner.
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ws-race")
    try:
        llm_fut = pool.submit(_llm)
        ddg_fut = pool.submit(_ddg)
        done, _ = concurrent.futures.wait(
            [llm_fut, ddg_fut],
            return_when=concurrent.futures.FIRST_COMPLETED,
            timeout=timeout,
        )
        for fut in done:
            try:
                result = fut.result(timeout=1)
            except Exception:
                continue
            if result:
                return result
        # First finisher was empty -- wait for the remaining one
        for fut in (llm_fut, ddg_fut):
            if fut in done:
                continue
            try:
                result = fut.result(timeout=timeout)
            except Exception:
                continue
            if result:
                return result
    finally:
        pool.shutdown(wait=False)
    return ""


# ── Mode implementations ─────────────────────────────────────────────────────

def _mode_search(query: str) -> str:
    """Default mode: brain answer with DDG fallback."""
    try:
        return _llm_query(
            query,
            "You are Jeeves' web search assistant. Answer factually and "
            "concisely. If you are unsure, say so instead of inventing facts.",
        )
    except Exception as e:
        print(f"[WebSearch] WARNING LLM failed ({e}) -- falling back to DDG")
        results = _ddg_search(query)
        return _format_ddg(query, results)


def _mode_news(query: str) -> str:
    """News mode: race the brain against DDG news, first valid result wins."""
    result = _race_llm_vs_ddg(
        query,
        "You are a news briefing assistant. Summarize the latest news about "
        "the topic with headlines, dates and sources. Be factual and current.",
        lambda: _format_ddg(query, _ddg_news(query), kind="news"),
        timeout=30,
    )
    if result:
        return result
    # Everything failed -- last resort plain DDG
    return _format_ddg(query, _ddg_news(query), kind="news") or \
        f"Could not fetch news for: {query}"


def _mode_research(query: str) -> str:
    """Research mode: deeper, structured brain answer + DDG sources."""
    try:
        answer = _llm_query(
            query,
            "You are a research assistant. Investigate the topic thoroughly: "
            "give an overview, key facts, pros and cons, and any notable "
            "nuances. Structure the answer with headings. Be accurate.",
        )
        results = _ddg_search(query, max_results=5)
        if results:
            sources = "\n\nSources:\n" + "\n".join(
                f"- {r.get('title','')} ({r.get('url','')})"
                for r in results if r.get("url")
            )
            return answer + sources
        return answer
    except Exception as e:
        print(f"[WebSearch] WARNING Research LLM failed ({e}) -- DDG only")
        return _format_ddg(query, _ddg_search(query, max_results=8))


def _mode_price(query: str) -> str:
    """Price mode: brain price summary raced against DDG price snippets."""
    result = _race_llm_vs_ddg(
        query,
        "You are a shopping assistant. Report typical current prices, price "
        "ranges and where to find them for the requested item. Give concrete "
        "numbers; if unsure, say prices vary and why.",
        lambda: _format_price(query, _ddg_search(query, max_results=8)),
        timeout=30,
    )
    if result:
        return result
    return f"Could not find price info for: {query}"


def _mode_compare(items: list[str], aspect: str) -> str:
    """Compare mode: per-item DDG research + brain synthesis."""
    if not items:
        return "Please provide at least two items to compare, sir."

    try:
        joined = ", ".join(items)
        return _llm_query(
            f"Compare {joined} in terms of {aspect}. Give specific facts, "
            "numbers, and a clear verdict on which is better for the typical "
            "user and why.",
            "You are a comparison assistant. Be factual, balanced and precise.",
        )
    except Exception as e:
        print(f"[WebSearch] WARNING Compare LLM failed ({e}) -- DDG per item")

    per_item: dict[str, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=min(len(items), 4)) as pool:
        futs = {
            pool.submit(_ddg_search, f"{item} {aspect}", 3): item
            for item in items
        }
        for fut in concurrent.futures.as_completed(futs):
            item = futs[fut]
            try:
                per_item[item] = fut.result()
            except Exception:
                per_item[item] = []

    lines = [f"Comparison -- {aspect.upper()}", "-" * 40]
    for item in items:
        lines.append(f"\n* {item}")
        for r in per_item.get(item, [])[:2]:
            if r.get("snippet"):
                lines.append(f"  - {r['snippet']}")
    return "\n".join(lines)


# ── Public API ───────────────────────────────────────────────────────────────

def web_search(
    parameters:     dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    """Multi-mode web search. See module docstring for mode details.

    parameters:
        query  : the search query (required unless items given)
        mode   : search | news | research | price | compare (default: search)
        items  : list of items for compare mode
        aspect : comparison aspect for compare mode
    """
    params = parameters or {}
    query  = str(params.get("query", "") or "").strip()
    mode   = str(params.get("mode",  "search") or "search").lower().strip()
    items  = params.get("items", []) or []
    aspect = str(params.get("aspect", "general") or "general").strip() or "general"

    if not query and not items:
        return "Please provide a search query, sir."

    if items and mode != "compare":
        mode = "compare"

    if player:
        player.write_log(f"[Search] {query or ', '.join(items)}")

    print(f"[WebSearch] Query: {query!r}  Mode: {mode}")

    try:
        if mode == "news":
            return _mode_news(query)
        if mode == "research":
            return _mode_research(query)
        if mode == "price":
            return _mode_price(query)
        if mode == "compare":
            return _mode_compare(items, aspect)
        return _mode_search(query)
    except Exception as e:
        print(f"[WebSearch] Failed: {e}")
        try:
            return _format_ddg(query, _ddg_search(query))
        except Exception:
            return f"Search failed, sir: {e}"
