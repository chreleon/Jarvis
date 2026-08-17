# actions/anime_watch.py
# Anime monitor + recommender powered by AniList (free, no API key).
#
#   • new       — currently-airing anime THIS season (the monitor); if
#                 nothing is airing, falls back to the trending list.
#   • trending  — most popular anime overall (MAL/AniList popularity =
#                 "approved on the internet" signal).
#   • check     — details + Netflix availability for one title.
#
# Every entry reports episodes, season/year, genre, and whether it is
# fully released or ongoing. Netflix availability is verified keyless via
# a DDG site: search for netflix.com/title pages; Netflix titles are
# flagged and preferred in the summary, but non-Netflix titles are never
# dropped. ASCII-safe prints only (daemon-thread safe on cp1252 consoles).

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

_API = "https://graphql.anilist.co"
_TIMEOUT = 20
_NETFLIX_CHECK_LIMIT = 5      # only top candidates get a streaming check
_CACHE_TTL = 6 * 3600         # AniList responses cached 6h (seasonal data)
_NETFLIX_TTL = 24 * 3600      # Netflix verdicts cached 24h

_FIELDS = """
  title { romaji english }
  episodes
  status
  season
  seasonYear
  format
  genres
  averageScore
  popularity
  siteUrl
"""

_SEASON_QUERY = """
query ($season: MediaSeason, $year: Int) {
  Page(page: 1, perPage: 8) {
    media(season: $season, seasonYear: $year, status: RELEASING,
          type: ANIME, isAdult: false, sort: POPULARITY_DESC) {
      __typename
      ...fields
    }
  }
}
""" .replace("...fields", _FIELDS)

_TRENDING_QUERY = """
query {
  Page(page: 1, perPage: 8) {
    media(type: ANIME, isAdult: false, sort: POPULARITY_DESC) {
      __typename
      ...fields
    }
  }
}
""" .replace("...fields", _FIELDS)

_SEARCH_QUERY = """
query ($search: String) {
  Page(page: 1, perPage: 5) {
    media(search: $search, type: ANIME, isAdult: false,
          sort: [SEARCH_MATCH, POPULARITY_DESC]) {
      __typename
      ...fields
    }
  }
}
""" .replace("...fields", _FIELDS)


# ── AniList client (with TTL cache) ───────────────────────────────────────────

_cache: dict = {}
_netflix_cache: dict = {}


def _anilist(query: str, variables: dict) -> list[dict]:
    """POST one GraphQL query to AniList; returns media list ([] on failure)."""
    import requests
    try:
        resp = requests.post(
            _API,
            json={"query": query, "variables": variables},
            headers={"Content-Type": "application/json",
                     "Accept": "application/json",
                     "User-Agent": "jeeves-anime/1.0"},
            timeout=_TIMEOUT,
        )
        if resp.status_code == 429:          # rate limited — one short retry
            time.sleep(1.5)
            resp = requests.post(
                _API,
                json={"query": query, "variables": variables},
                headers={"Content-Type": "application/json",
                         "Accept": "application/json",
                         "User-Agent": "jeeves-anime/1.0"},
                timeout=_TIMEOUT,
            )
        resp.raise_for_status()
        return (resp.json().get("data", {}).get("Page", {}) or {}).get("media", []) or []
    except Exception as e:
        print(f"[AnimeWatch] AniList error: {e}")
        return []


def _anilist_cached(key: str, query: str, variables: dict) -> list[dict]:
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    data = _anilist(query, variables)
    if data:                                  # don't cache failures
        _cache[key] = (now, data)
    return data


def _current_season() -> tuple[str, int]:
    month = datetime.now().month
    season = ("WINTER" if month <= 3 else
              "SPRING" if month <= 6 else
              "SUMMER" if month <= 9 else "FALL")
    return season, datetime.now().year


# ── Netflix availability (keyless) ────────────────────────────────────────────

def _netflix_check(title: str) -> bool | None:
    """True if a netflix.com/title/<id> watch page exists for `title`.

    Keyless verification: Bing site-search for a quoted title restricted
    to the /title path, then decode Bing's redirect URLs and look for a
    real watch-page ID (netflix.com/title/<digits>). Returns None when
    the search itself failed (network/anomaly) so an unknown verdict is
    never presented as a wrong "not on Netflix".
    """
    import re as _re
    cached = _netflix_cache.get(title)
    if cached and time.time() - cached[0] < _NETFLIX_TTL:
        return cached[1]
    try:
        from actions.web_search import _ddg_html
        # Bing ranking varies between queries, so pull more results to make
        # sure the watch page (when present) isn't ranked off the page.
        hits = _ddg_html(f'site:netflix.com/title "{title}"', max_results=10)
    except Exception:
        return None
    verdict: bool | None = None
    if hits:                                   # search worked; check the URLs
        urls = [h.get("url") or "" for h in hits]
        verdict = any(_re.search(r"netflix\.com/title/\d+", u) for u in urls)
    _netflix_cache[title] = (time.time(), verdict)
    return verdict


# ── Formatting ────────────────────────────────────────────────────────────────

_STATUS_TEXT = {
    "FINISHED": "fully released",
    "RELEASING": "ongoing",
    "NOT_YET_RELEASED": "not yet aired",
    "CANCELLED": "cancelled",
}


def _status_text(status: str) -> str:
    return _STATUS_TEXT.get(status, status or "?")


def _pick_best_match(media: list[dict], query: str) -> dict | None:
    """Pick the entry that actually matches `query`.

    AniList's SEARCH_MATCH ranking can surface unrelated titles first
    (e.g. 'Demon Slayer' returns 'Onigiri' before the Kimetsu entries), so
    the best match is chosen by local title relevance (exact match, then
    substring) with popularity as the tiebreaker.
    """
    q = query.lower().strip()
    best = None
    best_key = (-1, -1)
    for m in media:
        t = m.get("title") or {}
        names = [n or "" for n in (t.get("romaji"), t.get("english"))]
        if any(n.lower() == q for n in names):
            key = (100, m.get("popularity") or 0)
        elif any(q in n.lower() for n in names):
            key = (50, m.get("popularity") or 0)
        else:
            key = (0, m.get("popularity") or 0)
        if key > best_key:
            best, best_key = m, key
    return best


def _media_name(m: dict) -> str:
    t = m.get("title") or {}
    name = t.get("romaji") or t.get("english") or "?"
    eng = t.get("english")
    if eng and eng.lower() != name.lower():
        name = f"{name} ({eng})"
    return name


def _format_media(m: dict, on_netflix: bool | None) -> str:
    season = m.get("season")
    year = m.get("seasonYear")
    aired = f"{season.title()} {year}" if season and year else "air date TBA"
    eps = m.get("episodes")
    ep_txt = f"{eps} episodes" if eps else "episodes TBA"
    genres = ", ".join((m.get("genres") or [])[:3]) or "genre ?"
    score = m.get("averageScore")
    score_txt = f"{score}/100" if score else "no score"
    pop = m.get("popularity") or 0
    net = {True: " [ON NETFLIX]", False: "", None: ""}[on_netflix]
    return (
        f"  • {_media_name(m)}{net}\n"
        f"      {_status_text(m.get('status'))} · {ep_txt} · aired {aired} · {m.get('format')}\n"
        f"      Genre: {genres} · Score {score_txt} · Popularity {pop:,} · {m.get('siteUrl')}"
    )


def _attach_netflix(media: list[dict]) -> list[dict]:
    """Check Netflix for the top candidates in parallel; annotate each."""
    checked = {m.get("siteUrl"): m for m in media[: _NETFLIX_CHECK_LIMIT]}
    verdicts: dict = {}
    with ThreadPoolExecutor(max_workers=min(5, len(checked))) as pool:
        future_map = {
            pool.submit(_netflix_check, _media_name(m)): m
            for m in checked.values()
        }
        for fut in future_map:
            verdicts[fut.result(timeout=_TIMEOUT + 5)] = future_map[fut]
    for m in media:
        m["_netflix"] = None
    for verdict, m in verdicts.items():
        m["_netflix"] = verdict
    return media


def _render(media: list[dict], heading: str) -> str:
    if not media:
        return heading + "\n  (no data right now — try again in a bit)"
    lines = [heading]
    for m in media:
        lines.append(_format_media(m, m.get("_netflix")))
    on_netflix = [m for m in media if m.get("_netflix") is True]
    if on_netflix:
        names = ", ".join(_media_name(m) for m in on_netflix)
        lines.append(f"\n  On Netflix: {names}")
    elif any(m.get("_netflix") is False for m in media):
        lines.append("\n  (Netflix checked; none of the top picks are on Netflix — "
                     "all are still worth a look)")
    else:
        lines.append("\n  (Netflix availability could not be verified right now)")
    return "\n".join(lines)


# ── Actions ───────────────────────────────────────────────────────────────────

def _new_releases() -> str:
    season, year = _current_season()
    media = _anilist_cached(
        f"new:{season}:{year}", _SEASON_QUERY,
        {"season": season, "year": year},
    )
    if not media:
        # Between seasons / nothing airing → recommend trending instead.
        return _trending(fallback_note=True)
    _attach_netflix(media)
    return _render(
        media,
        f"New anime airing this {season.title()} {year} (by popularity):",
    )


def _trending(fallback_note: bool = False) -> str:
    media = _anilist_cached("trending", _TRENDING_QUERY, {})
    _attach_netflix(media)
    heading = ("No new anime airing right now — here's the most trending "
               "instead:\n") if fallback_note else \
              "Most trending anime right now (validated by community popularity):"
    return _render(media, heading)


def _check_title(title: str) -> str:
    if not title.strip():
        return "Give a title to check, e.g. action='check' title='Demon Slayer'."
    media = _anilist_cached(f"search:{title.lower()}", _SEARCH_QUERY, {"search": title})
    if not media:
        return f"No anime found for '{title}'."
    best = _pick_best_match(media, title)
    if best is None:
        return f"No anime found for '{title}'."
    best["_netflix"] = _netflix_check(_media_name(best))
    net = best["_netflix"]
    note = ("✅ Available on Netflix" if net is True else
            "Not found on Netflix" if net is False else
            "Netflix availability could not be verified")
    return _render([best], f"'{_media_name(best)}'") + f"\n  {note}"


# ── Tool entry point ──────────────────────────────────────────────────────────

def anime_watch(parameters: dict, player=None, session_memory=None) -> str:
    """Tool dispatcher: new | trending | check."""
    params = parameters or {}
    action = str(params.get("action", "new")).strip().lower()
    if action == "new":
        return _new_releases()
    if action == "trending":
        return _trending()
    if action == "check":
        return _check_title(str(params.get("title", "")))
    return "Unknown action. Use: new, trending, check."
