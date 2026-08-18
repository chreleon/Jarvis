# actions/secretary.py
# Secretary mode — Jeeves holds conversations on the boss's behalf.
#
# How a real secretary works, applied here:
#   • Acknowledge fast   — every message gets a prompt, polite response.
#   • Shield the boss    — routine/small-talk is handled, never forwarded.
#   • Never over-commit  — the secretary never confirms plans, prices, or
#     decisions for the boss; it collects info and offers options.
#   • Escalate sparingly — only urgency, money/legal, unknowns, repeated
#     contact, or anything needing a boss decision reaches the inbox.
#
# The decision engine is deterministic (rules + templates, no LLM per
# message): fast, free, and consistent. The boss reviews escalated items
# with `secretary inbox` and answers personally with `secretary reply`.

import copy
import json
import re
import time
from datetime import datetime
from pathlib import Path

_CFG_PATH = Path(__file__).resolve().parent.parent / "config" / "api_keys.json"


# ── Group-name heuristic (YinYang: safety net when the JS row filter misses)
# WhatsApp groups often have names like "Family", "Work Group", "Class
# 2025", "Chat" or end with ": N members" / "(5)".  Photo-less groups with
# a single initial avatar pass the JS row filter as individual chats —
# this name-based heuristic catches the most common patterns so the
# secretary never replies to a group by accident.
_GROUP_NAME_RE = re.compile(
    r"(\bgroup\b|\bchat\b|\bteam\b|\bfamily\b|\bclass\b|\bforum\b|"
    r"\bcommittee\b|\bsociety\b|\bclub\b|\bchurch\b|\bmosque\b|"
    r"\bsquad\b|\bcrew\b|\bsupport\b|\bcommunity\b|\bboard\b|"
    r"\bpanel\b|\bstaff\b|\bcouncil\b|\bteam\b|\bdevs\b|"
    r"\bdevelopers\b|\bengineers\b|\bstudents\b|\bparents\b|"
    r"\bneighbors\b|\bneighbours\b|\bassociates\b|\bmembers\b|"
    r"\bassociates\b|\bassociates\b)"
    r"|\(\d+\)"    # trailing (N) — e.g. "Family (5)"
    r"|:\s*\d+"    # trailing ": N" — e.g. "Group: 12"
    , re.IGNORECASE,
)


def _looks_like_group(sender: str) -> bool:
    """True when the sender name looks like a group chat.

    Catches the most common patterns that the JS row-level filter misses
    (photo-less groups with a single initial avatar).  Used as a safety net
    in handle_message — when the bridge send bypasses the subtitle check
    (send_fn path), this catches groups by name before a reply is sent."""
    s = (sender or "").strip()
    if not s:
        return False
    return bool(_GROUP_NAME_RE.search(s))


# ── Promise / follow-up detection ─────────────────────────────────────────────
# A real secretary tracks when the boss promises to do something and
# reminds them. These regexes catch the most common promise patterns in
# English/Swahili/sheng so the secretary can extract and log them.
_PROMISE_PATTERNS = [
    # English promises
    re.compile(r"i'?ll\s+(call|text|message|send|come|be|do|check|look|get|see|meet|reply|respond|follow|confirm|book|order|pay|bring|pick|drop|fix|handle|sort|arrange|plan|organize|reschedule|cancel|let you know|get back)", re.I),
    re.compile(r"(?:can|will|shall|should|might|may|could)\s+(?:i|we)\s+(call|text|message|send|come|be|do|check|look|get|see|meet|reply|respond|follow|confirm|book|order|pay|bring|pick|drop|fix|handle|sort|arrange|plan|organize|reschedule|cancel)", re.I),
    re.compile(r"(?:remind|reminder)\s+(?:me|us)\s+(?:to|about)", re.I),
    re.compile(r"(?:i'?ll|we'?ll|let me)\s+(?:have|get|send|bring)\s+(?:it|them|that|this|one)", re.I),
    re.compile(r"(?:tomorrow|next week|later today|tonight|this evening|monday|tuesday|wednesday|thursday|friday|saturday|sunday)", re.I),
    # Swahili/sheng promises
    re.compile(r"nit(?:a|afuatilie|atuma|leta|nunua|lipa|weka|fanya)", re.I),
    re.compile(r"tuta(?:fanya|onana|piga|tembelea)", re.I),
    re.compile(r"(?:nikumbushe|nikikumbushe)", re.I),
]

# Time expressions that indicate WHEN the promise should be fulfilled
_TIME_EXPR_RE = re.compile(
    r"(?:(?:in\s+)?\d+\s+(?:min(?:ute)?s?|hrs?|hours?|days?|weeks?|months?)"
    r"|tomorrow|tonight|this\s+(?:evening|afternoon|morning|week|month)"
    r"|next\s+(?:week|month|monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"|monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    r"|later\s+(?:today|this\s+week)"
    r"|on\s+\d{1,2}[\/-]\d{1,2}"
    r")", re.I,
)


def _detect_promises(message: str) -> list[dict]:
    """Extract promises/follow-ups from a message.

    Returns a list of {"text": str, "deadline": str|None} dicts.
    A real secretary never misses 'I'll call you tomorrow' — these
    are the boss's commitments that need tracking.
    """
    text = (message or "").strip()
    if not text or len(text) > 500:
        return []
    promises = []
    for pat in _PROMISE_PATTERNS:
        m = pat.search(text)
        if m:
            # Extract the surrounding context as the promise text
            start = max(0, m.start() - 20)
            end = min(len(text), m.end() + 40)
            promise_text = text[start:end].strip()
            # Try to find a time expression
            deadline = None
            tm = _TIME_EXPR_RE.search(text[m.start():])
            if tm:
                deadline = tm.group(0).strip()
            promises.append({"text": promise_text, "deadline": deadline})
            break  # one promise per message is enough
    return promises


def _add_followup(sender: str, promise: dict) -> None:
    """Log a follow-up item for the boss."""
    st = _state()
    followups = st.setdefault("followups", [])
    followups.append({
        "from": sender,
        "promise": promise.get("text", ""),
        "deadline": promise.get("deadline"),
        "created": datetime.now().isoformat(timespec="seconds"),
        "done": False,
    })
    # Keep bounded
    if len(followups) > 100:
        del followups[:-80]
    _save_state(st)


# ── Priority classification ───────────────────────────────────────────────────
# A real secretary doesn't just escalate — they PRIORITIZE. The inbox is
# sorted into tiers so the boss knows what to handle FIRST.

# Priority: urgent (handle now), today (handle today), week (handle this week), fyi (informational)
_URGENT_KEYWORDS = (
    "urgent", "asap", "emergency", "immediately", "right now", "critical",
    "deadline today", "overdue", "past due", "final notice", "last chance",
    "expires today", "cancelled", "canceled", "terminated",
)
_TODAY_KEYWORDS = (
    "today", "tonight", "this evening", "this afternoon", "this morning",
    "in an hour", "in 2 hours", "soon", "now", "right away",
)
_WEEK_KEYWORDS = (
    "this week", "next week", "monday", "tuesday", "wednesday",
    "thursday", "friday", "weekend", "next tuesday",
)


def _classify_priority(sender: str, message: str, escalation_reasons: list[str]) -> str:
    """Classify an escalated message into a priority tier.

    Returns: 'urgent', 'today', 'week', or 'fyi'.
    A real secretary's triage is the difference between a productive day
    and drowning in noise.
    """
    text = (message or "").lower()
    reasons = [r.lower() for r in (escalation_reasons or [])]

    # Urgent: money/legal/emergency keywords, or repeated unanswered contact
    if any(k in text for k in _URGENT_KEYWORDS):
        return "urgent"
    if "urgency keywords" in " ".join(reasons):
        return "urgent"
    if "unanswered messages" in " ".join(reasons):
        # 2+ unanswered → today, 5+ → urgent
        m = re.search(r"(\d+) unanswered", " ".join(reasons))
        if m and int(m.group(1)) >= 5:
            return "urgent"
        return "today"
    if "needs the boss's decision" in " ".join(reasons):
        return "today"
    if "call" in " ".join(reasons):
        return "today"

    # Today: time-sensitive language
    if any(k in text for k in _TODAY_KEYWORDS):
        return "today"

    # Week: scheduling, planning
    if any(k in text for k in _WEEK_KEYWORDS):
        return "week"

    # Default: informational
    return "fyi"


# ── Contact CRM ───────────────────────────────────────────────────────────────
# A real secretary remembers who each person is, their relationship to the
# boss, and the context of recent interactions. This is the lightweight CRM.

def _get_contact(sender: str) -> dict:
    """Get the CRM record for a contact."""
    st = _state()
    contacts = st.setdefault("contacts", {})
    key = (sender or "").strip().lower()
    if key not in contacts:
        contacts[key] = {
            "name": sender,
            "relationship": "",
            "notes": "",
            "last_interaction": "",
            "message_count": 0,
            "created": datetime.now().isoformat(timespec="seconds"),
        }
        _save_state(st)
    return contacts[key]


def _update_contact(sender: str, **kwargs) -> None:
    """Update a contact's CRM record."""
    st = _state()
    contacts = st.setdefault("contacts", {})
    key = (sender or "").strip().lower()
    contact = contacts.get(key, _get_contact(sender))
    for k, v in kwargs.items():
        if v:  # only update non-empty values
            contact[k] = v
    contact["last_interaction"] = datetime.now().isoformat(timespec="seconds")
    contact["message_count"] = contact.get("message_count", 0) + 1
    contacts[key] = contact
    _save_state(st)


def _format_contact(sender: str) -> str:
    """Format a contact's CRM record for display."""
    c = _get_contact(sender)
    lines = [f"📇 {c.get('name', sender)}:"]
    if c.get("relationship"):
        lines.append(f"  Relationship: {c['relationship']}")
    if c.get("notes"):
        lines.append(f"  Notes: {c['notes']}")
    if c.get("last_interaction"):
        lines.append(f"  Last seen: {c['last_interaction'][:16]}")
    lines.append(f"  Messages handled: {c.get('message_count', 0)}")
    return "\n".join(lines)


# ── Persistence ───────────────────────────────────────────────────────────────
# _load_cfg() runs on every sweep and several times per handled message
# (is_enabled, boss name, Meta-AI-drafts flag) — cache it keyed on
# (mtime_ns, size), the same pattern as memory_manager.load_memory (YinYang):
# steady-state polls/triage do one stat() instead of a read + JSON parse each
# call, and any write (secretary on/off) bumps mtime so freshness is kept.
_cfg_cache: dict | None = None
_cfg_cache_mtime_ns: int = -1
_cfg_cache_size: int = -1


def _load_cfg() -> dict:
    """Load config/api_keys.json, cached on (mtime_ns, size).

    Callers may MUTATE the result (secretary on/off does), so a fresh copy
    is returned each time — the cache itself stays pristine and the file
    stays the source of truth."""
    global _cfg_cache, _cfg_cache_mtime_ns, _cfg_cache_size
    try:
        st = _CFG_PATH.stat()
        if (_cfg_cache is not None
                and st.st_mtime_ns == _cfg_cache_mtime_ns
                and st.st_size == _cfg_cache_size):
            return copy.deepcopy(_cfg_cache)
    except OSError:
        pass
    try:
        data = json.loads(_CFG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    _cfg_cache = data
    try:
        st = _CFG_PATH.stat()
        _cfg_cache_mtime_ns = st.st_mtime_ns
        _cfg_cache_size = st.st_size
    except OSError:
        pass
    return copy.deepcopy(data)


def _save_cfg(cfg: dict) -> None:
    """Persist config and invalidate the cache so the next read re-reads the
    file (the write bumps mtime anyway; explicit invalidation is belt-and-
    suspenders for same-size writes)."""
    global _cfg_cache, _cfg_cache_mtime_ns, _cfg_cache_size
    try:
        _CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CFG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False),
                             encoding="utf-8")
        _cfg_cache = None
        _cfg_cache_mtime_ns = -1
        _cfg_cache_size = -1
    except Exception as e:
        print(f"[Secretary] Could not persist config: {e}")


def _state() -> dict:
    from memory.memory_manager import load_memory
    data = load_memory().get("secretary", {})
    return data if isinstance(data, dict) else {"conversations": {}, "inbox": []}


def _save_state(st: dict) -> None:
    from memory.memory_manager import load_memory, MEMORY_PATH, _lock
    with _lock:
        memory = load_memory()
        memory["secretary"] = st
        MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        MEMORY_PATH.write_text(
            json.dumps(memory, indent=2, ensure_ascii=False), encoding="utf-8")


def is_enabled() -> bool:
    return bool(_load_cfg().get("secretary_mode", False))


# ── Processed-message fingerprints (persisted, cross-process dedupe) ────────
# The background WhatsApp monitor (actions/secretary_listener.py) marks each
# handled (sender, message) pair here so a restarted daemon or a second
# process can never re-handle — and double-reply to — the same message.

def _is_processed(fingerprint: str) -> bool:
    return fingerprint in set(_state().get("processed", []))


def _is_processed_many(fingerprints) -> set:
    """Return the subset of fingerprints already processed — in ONE state
    load and ONE set build (YinYang: _is_processed rebuilds the whole set
    per call, so a 10+ chat sweep does ~19x more set-insert work than it
    needs, growing with the processed list)."""
    fps = [f for f in (fingerprints or []) if f]
    if not fps:
        return set()
    processed = set(_state().get("processed", []))
    return {f for f in fps if f in processed}


def _mark_processed(fingerprint: str) -> None:
    st = _state()
    seen = st.setdefault("processed", [])
    if fingerprint not in seen:
        seen.append(fingerprint)
        if len(seen) > 2000:
            del seen[:-1500]
        _save_state(st)


def _mark_processed_many(fingerprints) -> None:
    """Mark several (sender, message) fingerprints processed in ONE write.

    The background monitor's catch-up sweep can hand dozens of unread chats
    at once; marking them one-by-one rewrites the whole memory file per
    message. One batched write per sweep keeps the disk I/O flat (YinYang)."""
    st = _state()
    seen = st.setdefault("processed", [])
    added = False
    for fp in fingerprints or []:
        if fp not in seen:
            seen.append(fp)
            added = True
    if added:
        if len(seen) > 2000:
            del seen[:-1500]
        _save_state(st)


# ── Names ─────────────────────────────────────────────────────────────────────

def _boss_name() -> str:
    cfg = _load_cfg()
    if cfg.get("boss_name"):
        return str(cfg["boss_name"])
    try:
        from memory.memory_manager import load_memory
        identity = load_memory().get("identity", {})
        first = identity.get("first_name", {})
        name = first.get("value", "") if isinstance(first, dict) else first
        if name:
            return str(name)
    except Exception:
        pass
    return "my boss"


def _secretary_sig(sender: str | None = None) -> str:
    """The signature appended to drafted replies. When the sender is known
    it uses what THEY call the boss ("Jeeves on behalf of baby"); otherwise
    the configured boss name."""
    if sender:
        return f"Jeeves (on behalf of {_pet_name_for(sender)})"
    return f"Jeeves (on behalf of {_boss_name()})"


# ── Pet names (what each contact calls the boss) ────────────────────────────
# The secretary replies as the boss's assistant, so the natural reply uses
# what the SENDER calls the boss: the wife gets "I'll make sure baby sees
# it", a friend "I'll pass it to bro". These are discovered ONCE by scanning
# the existing chats (the monitor auto-runs the scan at most once a day, or
# 'secretary scan' does it on demand), persisted as a STATIC map, and looked
# up per reply from an mtime cache — chat text is never re-read per draft
# (YinYang: the scan is the only place messages are analysed). Unknown
# senders get the neutral "My boss".

# Address terms people use for the boss — longest (multi-word) first so the
# matcher prefers "my love" over a bare "love". English, Swahili and sheng.
_PET_TERMS = (
    "my sweetheart", "my darling", "my baby", "my love", "my dear",
    "my heart", "my honey", "sweetheart", "sweetie", "darling", "baby",
    "babe", "bae", "boo", "honey", "hubby", "princess", "cutie",
    "handsome", "gorgeous", "beautiful", "angel", "pumpkin", "lover",
    "dear", "love", "mrembo", "mpenzi", "mpenz", "mwanangu", "mwana",
    "rafiki", "dada", "kaka", "kijana", "mzee", "buda", "manze",
    "champ", "chief", "boss", "bro", "bros", "bruh", "daktari",
    "jamaa", "og", "king", "queen",
)

# Words that sit in the vocative slot but are greetings, not names.
_PET_NAME_NOISE = {
    "hi", "hey", "hello", "yo", "sawa", "asante", "please", "sorry",
    "ok", "okay", "thanks", "thank", "you", "yes", "no", "haha", "lol",
}

# Terms that double as ordinary phrase words — "love you" / "Dear John" are
# NOT vocatives, so these need punctuation right after the term to count.
_PET_BARE_START_EXCLUDE = {"love", "dear"}

# Common words (English + Swahili/sheng) that must never be read as a
# novel nickname, even when they sit at the start of several messages.
_PET_STOPWORDS = {
    "i", "you", "we", "they", "he", "she", "it", "me", "us", "them",
    "my", "your", "our", "the", "a", "an", "and", "or", "but", "so",
    "if", "of", "to", "for", "with", "at", "on", "in", "from", "by",
    "is", "are", "was", "were", "am", "be", "been", "have", "has",
    "had", "do", "does", "did", "can", "could", "will", "would",
    "should", "not", "no", "yes", "ok", "okay", "please", "thanks",
    "thank", "sorry", "hello", "hey", "hi", "yo", "haha", "lol",
    "this", "that", "these", "those", "there", "here", "what", "when",
    "where", "who", "why", "how", "just", "like", "know", "think",
    "want", "need", "come", "going", "go", "get", "see", "say",
    "tell", "make", "let", "good", "great", "fine", "sure", "really",
    "today", "tomorrow", "yesterday", "now", "later", "soon", "well",
    "wait", "listen", "look", "guys", "man", "dear", "love",
    "ni", "na", "si", "kama", "hii", "hivi", "sasa", "vipi", "poa",
    "sawa", "asante", "tafadhali", "niko", "uko", "yuko", "tuko",
    "mko", "kuna", "hapa", "huko", "kwa", "ya", "za", "mimi",
    "wewe", "yeye", "sisi", "nyinyi", "wao", "yangu", "yako", "yake",
    "yetu", "huyu", "huyo", "kabisa", "bado", "tena", "pia", "ila",
    "lakini", "ndio", "ndiyo", "hapana", "sijui", "njoo", "kuja",
    "enda", "nenda", "au", "ikiwa", "weee", "wee", "eee", "mmm",
    "hmm", "eh", "ah", "oh", "aah",
}


def _vocative_terms_in(msg: str) -> set:
    """Address terms used in ONE message, from the vocative slot: message-
    initial ("baby, how are you?", "baby come home", "mzee uko wapi") or
    message-final after a space/punctuation ("miss you baby"). Ambiguous
    phrase words ("love", "dear") need punctuation right after the term, so
    "love you" and "Dear John," never match."""
    m = (msg or "").strip()
    if not m or len(m) > 400:
        return set()
    low = m.lower()
    found: set[str] = set()
    for term in _PET_TERMS:
        if term not in low:
            continue
        if re.match(r"^" + re.escape(term) + r"\b", low):
            if term in _PET_BARE_START_EXCLUDE and not re.match(
                    r"^" + re.escape(term) + r"(?=[,!:;?…—\-–]|$)", low):
                continue
            found.add(term)
            continue
        if re.search(r"(?<=[\s,;:!?…—\-–])" + re.escape(term)
                     + r"[.!?…]*$", low):
            found.add(term)
    return found - _PET_NAME_NOISE


def _extract_vocative(messages, sender: str | None = None) -> str | None:
    """What a sender calls the boss, from their recent messages: the most
    frequent address term across messages (tie → dictionary order). Also
    catches NOVEL nicknames a dictionary can't list ("Ziii niweke ..."):
    a short message-initial word used in ≥3 distinct messages is very likely
    an address term. None when nothing reliable is found; the sender's own
    name is never a candidate."""
    counts: dict[str, int] = {}
    for msg in messages or []:
        for term in _vocative_terms_in(msg):
            counts[term] = counts.get(term, 0) + 1
    # novel nicknames: message-initial token, ≥3 distinct messages
    initial: dict[str, int] = {}
    for msg in messages or []:
        m = (msg or "").strip()
        if not m:
            continue
        tok = re.split(r"[\s,!:;?…—\-–]+", m, maxsplit=1)[0].strip("'\"….!?")
        t = tok.lower()
        if (2 <= len(t) <= 12 and t.isalpha() and t not in _PET_NAME_NOISE
                and t not in _PET_STOPWORDS):
            initial[t] = initial.get(t, 0) + 1
    s = (sender or "").strip().lower()
    first = s.split()[0] if s else ""
    counts = {t: c for t, c in counts.items() if t != first}
    for t, c in initial.items():
        if t != first and c >= 3:
            counts[t] = max(counts.get(t, 0), c)
    if not counts:
        return None
    return max(counts, key=lambda t: (counts[t],
                                      -_PET_TERMS.index(t)
                                      if t in _PET_TERMS else 0))


_pet_names_cache: dict | None = None
_pet_names_mtime_ns = -1
_pet_names_size = -1


def _pet_names_map() -> dict:
    """The static {sender: pet name} map, mtime-cached on the memory file —
    loaded once, never re-read per reply (YinYang)."""
    global _pet_names_cache, _pet_names_mtime_ns, _pet_names_size
    try:
        from memory.memory_manager import MEMORY_PATH
        st = MEMORY_PATH.stat()
        if (st.st_mtime_ns, st.st_size) == (_pet_names_mtime_ns,
                                            _pet_names_size):
            return dict(_pet_names_cache or {})
    except OSError:
        pass
    names: dict = {}
    try:
        names = dict(_state().get("pet_names", {}) or {})
    except Exception:
        pass
    _pet_names_cache = dict(names)
    try:
        from memory.memory_manager import MEMORY_PATH
        st = MEMORY_PATH.stat()
        _pet_names_mtime_ns, _pet_names_size = st.st_mtime_ns, st.st_size
    except OSError:
        pass
    return names


def _pet_name_for(sender: str) -> str:
    """What THIS sender calls the boss (e.g. 'baby' for the wife), from the
    static scan; 'My boss' when unknown. Never extracts from chat text here —
    the scan already did that (YinYang: per-reply is static-map only).
    Known dictionary terms read naturally lowercase in a sentence ("make
    sure baby sees it"); novel nicknames are capitalized ("make sure Ziii
    sees it")."""
    names = _pet_names_map()
    s = (sender or "").strip().lower()
    if not s:
        return "My boss"
    for key, name in names.items():
        k = (key or "").strip().lower()
        if k and (s == k or s in k or k in s):
            n = (name or "").strip()
            if not n:
                return "My boss"
            if n.lower() in _PET_TERMS:
                return n                 # dictionary term — keep as stored
            return n[:1].upper() + n[1:]  # novel nickname — capitalize
    return "My boss"


def _pet_names_static() -> set:
    """Senders whose pet name is user-approved/static — the auto-scan never
    overwrites or re-derives these (e.g. the boss decided the wife is
    'Junior', not whatever the scan finds). Stored as lowercased titles in
    state["pet_names_static"]."""
    try:
        v = _state().get("pet_names_static", []) or []
        return {str(x).strip().lower() for x in v if str(x).strip()}
    except Exception:
        return set()


def _is_excluded_scan_chat(title: str) -> bool:
    """Chats that must never contribute pet names: the Meta AI assistant
    and the boss's own self-chat (its "messages" are commands to Jeeves)."""
    t = (title or "").strip().lower()
    if not t or t == "meta ai" or t.startswith("meta ai"):
        return True
    cfg_self = _load_cfg().get("secretary_self_chat", "")
    self_chats = (cfg_self if isinstance(cfg_self, list)
                  else [cfg_self] if cfg_self else [])
    return any(t == str(x).strip().lower() or t in str(x).strip().lower()
               for x in self_chats if str(x).strip())


def _pet_names_scan_needed() -> bool:
    """True when the pet-name map should be (re)built: enabled in config and
    not scanned in the last 24h. This gate is what keeps the per-reply path
    free of DOM/LLM work — the scan is the ONLY place chat text is read."""
    v = _load_cfg().get("secretary_pet_names", True)
    if v is False:
        return False
    try:
        at = _state().get("pet_names_scanned_at", "")
        if at:
            then = datetime.fromisoformat(str(at))
            if (datetime.now() - then).total_seconds() < 86400:
                return False
    except Exception:
        pass
    return True


def _llm_scan_chat(title: str, msgs: list) -> str | None:
    """One small brain call to read what THIS sender calls the boss from
    their recent messages — catches novel nicknames ("Ziii") that no
    dictionary can list, even from a single use. Returns the term or None.
    Never raises: any failure returns None and the deterministic result
    stands."""
    try:
        from or_client import client as brain
        sample = [str(m).strip().replace("\n", " ")[:120]
                  for m in (msgs or []) if str(m).strip()][:15]
        if len(sample) < 2:
            return None
        body = "\n".join(f"{i + 1}. {m}" for i, m in enumerate(sample))
        prompt = (
            f"These are recent messages FROM {title!r} (the sender) TO their "
            f"partner/boss:\n{body}\n\n"
            f"What single word or short phrase does this sender use to "
            f"ADDRESS the boss in these messages (e.g. 'baby', 'mzee', "
            f"'Ziii', 'honey', 'bro')?\n"
            f"Rules: only terms used to address or NAME the boss; ignore "
            f"greetings, lyrics, filler and the sender's own name; if the "
            f"boss's real name is used, that counts; if there is no clear "
            f"address term, return 'none'.\n"
            f"Reply with ONLY JSON: {{\"name\": \"...\"}}")
        data = brain.chat_json(prompt, max_tokens=40)
        name = str((data or {}).get("name") or "").strip().lower()
        name = name.strip("'\" .")
        if not name or name in ("none", "null", "n/a", "-"):
            return None
        if len(name) > 20:
            return None
        return name
    except Exception:
        return None


def scan_pet_names(bridge=None, limit: int = 15, save: bool = True,
                   llm: bool = True) -> str:
    """Scan the existing chats once and persist what each contact calls the
    boss (wife → 'baby', a friend → 'bro'), used by every reply template via
    the static map. Read-only: opens chats and reads their recent incoming
    message texts. Chats with no address term are left out of the map —
    their replies fall back to 'My boss'. Never runs per-draft: call it with
    'secretary scan', or let the monitor auto-run it at most once a day.

    `llm=True` adds a one-time brain pass over the sampled messages to catch
    novel nicknames no dictionary can list ("Ziii"); every failure falls
    back to the deterministic extraction. Returns a human summary."""
    if bridge is None:
        try:
            from actions.whatsapp_bridge import acquire_shared_bridge
            bridge, _ = acquire_shared_bridge(headless=True)
            bridge.start()
        except Exception as e:
            return f"Could not open the WhatsApp bridge: {e}"
    try:
        titles = bridge.list_chat_titles()
    except Exception as e:
        return f"Could not read the chat list: {e}"
    titles = [t for t in (titles or []) if not _is_excluded_scan_chat(t)]
    static = _pet_names_static()
    # User-approved names are decided — never re-derive or overwrite them.
    titles = [t for t in titles if (t or "").strip().lower() not in static]
    found: dict[str, str] = {}
    unreadable = 0
    samples: dict[str, list] = {}
    for title in titles[:max(1, int(limit))]:
        try:
            msgs = bridge.read_recent_incoming(title, limit=40)
        except Exception:
            unreadable += 1
            continue
        samples[title] = msgs
        name = _extract_vocative(msgs, sender=title)
        if name:
            found[title] = name
    if save:
        # Persist the deterministic findings FIRST so a slow/hung brain pass
        # can never block them (daktari: the scan is once per 24h; the map
        # should land even if refinement stalls).
        try:
            st = _state()
            names = dict(st.get("pet_names", {}) or {})
            names.update({k: v for k, v in found.items()
                          if (k or "").strip().lower() not in static})
            st["pet_names"] = names
            st["pet_names_scanned_at"] = datetime.now().isoformat(
                timespec="seconds")
            _save_state(st)
        except Exception as e:
            return f"Pet-name scan found names but could not save them: {e}\n" \
                   + _pet_scan_summary(found, unreadable)
    if llm:
        # One-time refinement pass (no bridge involved — pure network, so
        # the monitor keeps polling) for chats the dictionary couldn't
        # crack. Bounded: a handful of small calls, once per 24h.
        refined = False
        for title, msgs in samples.items():
            if title in found:
                continue
            name = _llm_scan_chat(title, msgs)
            if name:
                found[title] = name
                refined = True
        if save and refined:
            try:
                st = _state()
                names = dict(st.get("pet_names", {}) or {})
                names.update({k: v for k, v in found.items()
                              if (k or "").strip().lower() not in static})
                st["pet_names"] = names
                _save_state(st)
            except Exception:
                pass   # deterministic names already persisted — best effort
    return _pet_scan_summary(found, unreadable)


def _pet_scan_summary(found: dict, unreadable: int) -> str:
    lines = [f"{t} → '{n}'" for t, n in found.items()]
    head = (f"Pet-name scan complete: {len(found)} chat(s) with an address "
            f"term" + (f" ({unreadable} couldn't be read)" if unreadable
                       else "") + ".")
    if lines:
        return head + "\n" + "\n".join("  " + l for l in lines)
    return head + "\n  No address terms found — replies will use 'My boss'."


# ── Escalation rules ──────────────────────────────────────────────────────────

# Money, deadlines, contracts, emergencies — a real secretary never touches
# these alone.
_ESCALATE_KEYWORDS = (
    "urgent", "asap", "asap!", "emergency", "immediately", "right now",
    "deadline", "today", "tonight", "now", "pay", "payment", "invoice",
    "money", "bank", "refund", "charge", "contract", "legal", "lawsuit",
    "cancel", "cancelled", "fire", "fired", "quit", "complaint", "refund",
    "password", "credit card", "ssn", "social security", "account number",
)

# Things that demand the boss's actual decision — escalate, don't guess.
_DECISION_KEYWORDS = (
    "can you confirm", "confirm", "yes or no", "are you available",
    "are you free", "want to", "would you", "can you do", "how much",
    "what's your price", "quote", "meeting at", "reschedule",
)

def _unanswered_count(sender: str) -> int:
    st = _state()
    conv = st.get("conversations", {}).get(sender, [])
    count = 0
    for entry in reversed(conv):
        if entry.get("role") == "outgoing":
            break
        count += 1
    return count


def triage(sender: str, message: str) -> dict:
    """Decide: reply automatically, or escalate to the boss.

    Returns {"action": "reply"|"escalate", "reasons": [...], "draft": str,
             "priority": str, "promises": [...]}.

    Priority tiers (like a real secretary's triage board):
      urgent  — handle NOW (money/legal/emergency, 5+ unanswered)
      today   — handle today (decisions, calls, time-sensitive)
      week    — handle this week (scheduling, planning)
      fyi     — informational (no action needed)
    """
    text = (message or "").lower()
    reasons: list[str] = []
    draft = _draft_reply(sender, message)

    if any(k in text for k in _ESCALATE_KEYWORDS):
        reasons.append("urgency keywords (money/deadline/emergency)")
    if any(k in text for k in _DECISION_KEYWORDS):
        reasons.append("needs the boss's decision")
    n = _unanswered_count(sender)
    if n >= 2:
        reasons.append(f"{n} unanswered messages from this sender")

    # Detect promises/follow-ups the boss should track
    promises = _detect_promises(message)

    if reasons:
        priority = _classify_priority(sender, message, reasons)
        return {"action": "escalate", "reasons": reasons[:3], "draft": draft,
                "priority": priority, "promises": promises}
    # Track promises even in routine replies — the boss may promise
    # things in casual conversations too.
    if promises:
        for p in promises:
            _add_followup(sender, p)
    # Update contact CRM for every interaction
    _update_contact(sender)
    return {"action": "reply", "reasons": [], "draft": draft,
            "priority": "fyi", "promises": promises}


# ── Reply drafting (secretary voice, never over-commits) ─────────────────────
# The drafts below are the instant, free baseline. The register is casual and
# warm — sheng/Swahili phrases and emoji when natural, like the WhatsApp
# conversations the boss actually has (see _META_STYLE_GUIDE) — but the
# golden rule never changes: the secretary acknowledges and collects, it
# never confirms plans, prices, or decisions for the boss.

# Casual register observed from the boss's own WhatsApp circle + Meta AI's
# matching style (Gojo, live Aug 2026): sheng/Swahili openers and fillers
# (sasa, vipi, poa, sawa), abbreviations (lol, ty), emoji (😄 😅 🙏 💪 😋),
# short warm sentences. Meta AI drafts follow this; the deterministic
# templates below carry a light version of it.
_META_STYLE_GUIDE = (
    "Match a laid-back WhatsApp vibe — like texting a real friend. Sheng/Swahili "
    "is fine when it feels natural (\"sasa\", \"vipi\", \"poa\", \"sawa\"), "
    "abbreviations like \"lol\" / \"ty\" and emoji (😄 😅 🙏 💪 😋) are OK, "
    "and keep it short and warm. Mirror the sender: formal English gets formal "
    "English back; casual/sheng gets casual/sheng back."
)


def _draft_reply(sender: str, message: str) -> str:
    text = (message or "").lower()
    # What THIS sender calls the boss (static map: "baby" for the wife,
    # "My boss" when unknown) — never re-scanned per draft.
    boss = _pet_name_for(sender)
    first = sender.split()[0] if sender else "there"
    if any(w in text for w in ("hi ", "hello", "hey ", "good morning",
                               "good afternoon", "good evening", "how are")):
        return (f"Hi {first}! 😄 Thanks for reaching out — this is "
                f"{_secretary_sig(sender)}. I've noted your message and I'll "
                f"make sure {boss} sees it, asap. 🙏")
    if any(w in text for w in ("free", "available", "meet", "schedule",
                               "call", "when", "what time", "friday",
                               "monday", "tomorrow", "later")):
        return (f"Sawa {first}, {boss} is a bit tied up rn 😅 — I'll check "
                f"availability and get back to you with some options.")
    if any(w in text for w in ("thank", "great", "awesome", "love", "nice")):
        return (f"Poa 😄 I'll make sure {boss} sees it. Have a great day! 🙏")
    return (f"Asante {first} 🙏 I've passed your message to {boss} and they'll "
            f"reply as soon as they're free.")


# ── Meta AI drafts (styled auto-replies via WhatsApp's own AI) ──────────────
# When enabled, routine auto-replies are drafted by Meta AI (the assistant
# inside WhatsApp) in the casual register above, then sent through the same
# background bridge. Every failure mode falls back to the instant
# deterministic draft, so a reply is never blocked or delayed by the AI:
#   • Meta AI unavailable / not linked / no reply in time → deterministic
#   • the draft LOOKS like a commitment ("count me in", "see you at") →
#     rejected, deterministic instead (the secretary never over-commits)
#   • more than 3 drafts in a minute (message burst) → instant drafts while
#     the burst clears, so replies don't queue behind the AI (YinYang)
# Config: "secretary_meta_ai_drafts": true (default) | false.

# Commitment markers that defeat the whole point of the secretary. If a Meta
# AI draft contains any of these, it is discarded for the safe template.
_COMMIT_MARKERS = (
    "count me in", "i'll be there", "i will be there", "see you at",
    "see you there", "let's meet", "lets meet", "i can do", "i'll do it",
    "on my way", "confirmed", "yes, let", "yes let", "sounds good, let",
    "deal", "i'm in", "im in", "reserve", "booking", "booked",
)

_draft_times: list[float] = []   # recent Meta AI draft timestamps (burst cap)


def _meta_drafts_enabled() -> bool:
    """True when auto-replies may be drafted by Meta AI. Defaults to true;
    an explicit `"secretary_meta_ai_drafts": false` turns it off (null/
    missing means unset → on, same rule as secretary_headless)."""
    v = _load_cfg().get("secretary_meta_ai_drafts", True)
    return True if v is None else bool(v)


def _meta_draft_prompt(sender: str, message: str, deterministic: str) -> str:
    """The instruction sent to Meta AI to produce one secretary reply.

    Pure and testable. Enforces the secretary's golden rule in the prompt
    itself AND via _meta_draft_over_commits() on the returned text."""
    boss = _pet_name_for(sender)
    return (
        f"You are {boss}'s WhatsApp assistant. The boss is busy, so reply to "
        f"this message on their behalf.\n"
        f"Sender: {sender}\n"
        f"Their message: {message}\n"
        f"Style: {_META_STYLE_GUIDE}\n"
        f"HARD RULES: NEVER confirm or commit to anything — no plans, prices, "
        f"payments, meetings, decisions, or promises. Never share personal or "
        f"private info. If the message needs the boss (money, legal, urgency, "
        f"a decision), just say you'll pass it on to them.\n"
        f"Safe fallback draft (mirror its safe tone, don't copy it): "
        f"{deterministic}\n"
        f"Keep the reply under 25 words. Reply with ONLY the message text, "
        f"nothing else."
    )


def _meta_draft_over_commits(draft: str) -> bool:
    """True when a drafted reply would commit the boss to something — the
    one thing the secretary must never do. Conservative whitelist of clear
    commitments; anything else passes."""
    d = (draft or "").lower()
    return any(m in d for m in _COMMIT_MARKERS)


def _meta_draft(sender: str, message: str, deterministic: str,
                media_kind: str | None = None) -> str:
    """Draft a reply with Meta AI; the deterministic template is the
    instant, always-safe fallback. Returns the reply text to send.

    media_kind: when the message is media (photo/video/voice note/...), the
    draft prompt is the media-specific one — Meta AI reacts appropriately
    to the TYPE (warm praise for a photo, professional ack for a document)."""
    if not _meta_drafts_enabled():
        return deterministic
    # Burst cap: after 3 drafts in the last 60s, use instant drafts until the
    # queue clears (each Meta AI round-trip takes ~15-20s).
    now = time.time()
    recent = [t for t in _draft_times if now - t < 60.0]
    _draft_times[:] = recent
    if len(recent) >= 3:
        return deterministic
    try:
        from actions.meta_ai import _ask_bridge
        prompt = (_meta_media_prompt(sender, media_kind, deterministic)
                  if media_kind
                  else _meta_draft_prompt(sender, message, deterministic))
        reply = _ask_bridge(prompt, timeout=60).strip()
        _draft_times.append(now)
    except Exception:
        return deterministic
    if not reply or len(reply) > 600 or _meta_draft_over_commits(reply):
        return deterministic
    return reply


# ── Media messages (photos, voice notes, documents, stickers, ...) ───────────
# The poll only ever sees the chat-list PREVIEW for a non-text message
# (verified live Aug 2026): "Photo", "Video", "Sticker", "Voice message",
# "Document", "GIF", "Location", "Contact", "Poll", "View once photo", etc.
# — often prefixed by icon-font glyph text ("wds-ic-readic-videocamVideo").
# The secretary can't see the actual content, but it CAN react correctly to
# the TYPE: Meta AI drafts a media-appropriate reply (wow stunning photo 😍
# for a photo, professional ack for a document). Reactions, recalls and
# system notices get NO reply at all. Groups are already dropped upstream.

_MEDIA_KIND_RE = re.compile(
    r"^(photo|video|sticker|voice(?: message| note)?|document|gif|audio|"
    r"location|contact|poll|image|view once)(?:,? no caption)?[^a-z0-9]*$",
    re.IGNORECASE,
)

# Things that are not "messages" to answer: reactions, recalls/deletions,
# scheduled-message notices, and group/system events.
_SKIP_PREVIEW_RE = re.compile(
    r"^(reacted|recalled|you (?:deleted|recalled|scheduled)|"
    r"deleted this message|scheduled|set the username|joined|left|"
    r"changed the subject|created (?:this )?group|was added|removed)",
    re.IGNORECASE,
)


def _strip_preview_glyphs(preview: str) -> str:
    """Strip the leading icon-font glyph run from a preview, then lowercase.

    Glyph runs are lowercase WITH hyphens ("wds-ic-readic-videocam" in
    "wds-ic-readic-videocamVideo") — so only a leading lowercase run that
    contains a hyphen is stripped. A plain lowercase word (a media label
    like "photo", a filename like "notes.pdf", or text like "you recalled")
    is real content and survives; capitalized labels ("Photo", "Reacted")
    are untouched by the regex anyway."""
    p = (preview or "").strip()
    m = re.match(r"^[a-z0-9\-.]*", p)
    if m and "-" in m.group(0):
        p = p[m.end():]
    return p.strip(" :\xa0").lower()


def _media_kind_of(preview: str) -> str | None:
    """Classify a message preview:
      'photo'|'video'|'sticker'|'voice note'|'document'|'gif'|'audio'|
      'location'|'contact'|'poll'  → media to acknowledge appropriately
      'skip'                        → reaction/recall/system notice, no reply
      None                          → text (or unknown)"""
    p = _strip_preview_glyphs(preview)
    if not p:
        return None
    if _SKIP_PREVIEW_RE.search(p):
        return "skip"
    m = _MEDIA_KIND_RE.match(p)
    if not m:
        return None
    kind = m.group(1).lower()
    if kind in ("voice", "voice message", "voice note"):
        return "voice note"
    if kind in ("image",):
        return "photo"
    if kind == "view once":
        return "media"   # view-once could be photo or video
    return kind


# A chat-list preview that names a file ("CV update English.docx",
# "notes.pdf") instead of a media label — the self-chat dashboard treats
# these as "a file was sent, download it" too.
_FILENAME_PREVIEW_RE = re.compile(
    r"^[\w \-']+\.(pdf|docx?|xlsx?|pptx?|txt|md|csv|zip|rar|7z|jpe?g|png|gif|webp|mp4|mov|mkv|mp3|m4a|wav|json|xml|apk)\b",
    re.IGNORECASE,
)


def _looks_like_media_preview(text: str) -> bool:
    """True when a chat-list preview means 'a file was sent' — a media label
    (Photo/Video/Document/...), a filename with an extension, or a bare
    Document/PDF label. The self-chat dashboard uses this to decide whether
    to download the file (like /attach) instead of treating the text as a
    command."""
    p = _strip_preview_glyphs(text)
    if not p:
        return False
    kind = _media_kind_of(p)
    if kind and kind != "skip":
        return True
    if _FILENAME_PREVIEW_RE.match(p):
        return True
    return p in ("document", "pdf", "file")


def _draft_media_reply(sender: str, kind: str) -> str:
    """Deterministic media acknowledgment — the instant fallback AND the
    safe-tone reference for Meta AI's media draft."""
    boss = _pet_name_for(sender)
    first = sender.split()[0] if sender else "there"
    if kind == "photo":
        return (f"Wow, stunning photo {first}! 😍 I'll make sure "
                f"{boss} sees it.")
    if kind in ("video", "gif"):
        return f"Nice one {first}! 😄 I'll show it to {boss}."
    if kind == "voice note":
        return f"Got your voice note {first} 🙏 I'll pass it on to {boss}."
    if kind == "sticker":
        return f"😄 I'll make sure {boss} sees that sticker!"
    if kind == "document":
        return f"Thanks for the document {first} — I'll pass it to {boss}."
    if kind == "location":
        return f"Got the location {first} — I'll let {boss} know."
    return (f"Asante {first} 🙏 I've received your message for {boss}.")


def _meta_media_prompt(sender: str, kind: str, deterministic: str) -> str:
    """The instruction for Meta AI when the message is media — react to the
    TYPE, never claim to have seen the content, same golden rules."""
    boss = _pet_name_for(sender)
    return (
        f"You are {boss}'s WhatsApp assistant. {sender} sent a {kind} "
        f"(media message, no text). Reply on {boss}'s behalf.\n"
        f"Style: {_META_STYLE_GUIDE}\n"
        f"React appropriately to the type: a photo → warm praise like "
        f"'wow, stunning photo'; a document/invoice → professional "
        f"acknowledgment; a voice note → confirm you got it; a sticker/GIF "
        f"→ playful. NEVER claim to have seen the actual content.\n"
        f"HARD RULES: never confirm or commit to anything — no plans, "
        f"prices, payments, meetings, decisions, or promises; never share "
        f"personal info.\n"
        f"Safe fallback draft (mirror its safe tone, don't copy it): "
        f"{deterministic}\n"
        f"Keep the reply under 20 words. Reply with ONLY the message text, "
        f"nothing else."
    )


# ── Conversation log + inbox ─────────────────────────────────────────────────

def _log(sender: str, role: str, text: str) -> None:
    st = _state()
    conv = st.setdefault("conversations", {}).setdefault(sender, [])
    conv.append({"role": role, "text": text,
                 "at": datetime.now().isoformat(timespec="seconds")})
    if len(conv) > 50:
        del conv[:-50]
    _save_state(st)


def _escalate(sender: str, message: str, reasons: list[str], draft: str,
             priority: str = "fyi", promises: list | None = None) -> str:
    st = _state()
    inbox = st.setdefault("inbox", [])
    entry = {
        "from": sender, "message": message, "reasons": reasons,
        "draft": draft, "priority": priority,
        "at": datetime.now().isoformat(timespec="seconds"),
    }
    inbox.append(entry)
    _save_state(st)

    # Track promises the boss made in this message
    for p in (promises or []):
        _add_followup(sender, p)

    # Update the contact CRM
    _update_contact(sender)

    # Priority emoji for the escalation notice
    prio_emoji = {"urgent": "🔴", "today": "🟡", "week": "🔵", "fyi": "⚪"}.get(priority, "⚪")
    prio_label = priority.upper()

    result = (
        f"{prio_emoji} [{prio_label}] from {sender}:\n"
        f"  \"{message}\"\n"
        f"  why: {', '.join(reasons)}.\n"
        f"  suggested reply: {draft}\n"
        f"  → answer it with: secretary reply \"{sender}\" \"<your words>\""
    )
    if promises:
        p_text = promises[0].get("text", "")
        deadline = promises[0].get("deadline", "")
        result += f"\n  📝 Follow-up tracked: {p_text}"
        if deadline:
            result += f" (by {deadline})"
    return result


def _escalate_call(sender: str, kind: str, ringing: bool = False) -> str:
    """A call (ringing now, or missed) — the secretary can't answer it, so
    it goes straight to the boss's inbox and is recorded in the session's
    call log so `secretary off` can report it."""
    st = _state()
    now = datetime.now().isoformat(timespec="seconds")
    inbox = st.setdefault("inbox", [])
    inbox.append({
        "from": sender, "message": f"{kind} call", "reasons": ["call"],
        "draft": "", "at": now,
    })
    calls = st.setdefault("calls", [])
    calls.append({"from": sender, "kind": kind, "ringing": bool(ringing),
                   "at": now})
    _save_state(st)
    verb = "is calling you" if ringing else "called you"
    return (f"[ESCALATED to {_boss_name()}] {sender} {verb} ({kind} call) — "
            f"the secretary can't pick up, so you were told instead.")


def _session_start_ts() -> str:
    """ISO timestamp the current secretary session began (set on `on`)."""
    st = _state()
    ts = st.get("session_start") or datetime.now().isoformat(timespec="seconds")
    st["session_start"] = ts
    _save_state(st)
    return ts


def _session_overview() -> str:
    """What the secretary did this session — who it talked to, what it told
    them, calls it saw, and anything still waiting for the boss. Built from
    the persisted state so it survives daemon restarts; returned by `off`."""
    st = _state()
    start = st.get("session_start") or ""
    convs = st.get("conversations", {}) or {}
    inbox = st.get("inbox", []) or []
    calls = st.get("calls", []) or []

    # only entries from this session
    def _since(entry: dict) -> bool:
        at = str(entry.get("at") or "")
        return (not start) or (at >= start)

    talked: dict[str, list[str]] = {}     # sender → outgoing texts
    for sender, entries in convs.items():
        out = [e.get("text", "") for e in entries
               if _since(e) and e.get("role") == "outgoing" and e.get("text")]
        if out:
            talked[sender] = out
    session_calls = [c for c in calls if _since(c)]
    urgent = [it for it in inbox if _since(it)]

    lines = [f"🤵 Secretary session report ({_secretary_sig()}):"]
    if talked:
        lines.append(f"  • Talked to {len(talked)} person(s):")
        for sender, outs in talked.items():
            lines.append(f"    - {sender}: replied {len(outs)} time(s)")
            for t in outs[-2:]:
                lines.append(f"        \"{t[:90]}{'…' if len(t) > 90 else ''}\"")
    else:
        lines.append("  • No conversations were handled this session.")
    if session_calls:
        lines.append(f"  • Calls seen ({len(session_calls)}):")
        for c in session_calls:
            ring = " (ringing)" if c.get("ringing") else " (missed)"
            lines.append(f"    - {c.get('from', '?')}: {c.get('kind', '?')} call"
                         f"{ring} at {str(c.get('at'))[11:16]}")
    if urgent:
        lines.append(f"  • ⚠️  Urgent for you ({len(urgent)}):")
        for i, it in enumerate(urgent, 1):
            lines.append(f"    {i}. From {it.get('from', '?')}: "
                         f"\"{it.get('message', '')}\"")
            reasons = ", ".join(it.get("reasons", []))
            if reasons:
                lines.append(f"       why: {reasons}")
        lines.append("  → 'secretary inbox' to review, 'secretary reply \"<name>\" \"<words>\"' to answer.")
    else:
        lines.append("  • No urgent messages waiting for you.")
    # Delegated messages
    forwarded = st.get("forwarded", []) or []
    session_fwd = [f for f in forwarded if _since(f)]
    if session_fwd:
        lines.append(f"  • 🔀 Delegated ({len(session_fwd)}):")
        for f in session_fwd[:5]:
            lines.append(f"    - {f['from']} → {f['to']}: \"{f['message'][:50]}\"")
    return "\n".join(lines)


# Media kinds Meta AI can genuinely analyze via WhatsApp's native
# forward path (verified live Aug 2026 — a forwarded CV docx got "Nimeipata
# CV yako 💪 ..."). Stickers/voice notes/locations/contacts/polls can't be
# forwarded that way, so they keep the type-aware template draft.
_FORWARDABLE_MEDIA = {"photo", "video", "gif", "document", "media"}


def _send_text(sender: str, text: str, send_fn=None) -> str:
    """Send `text` to `sender` via the bridge sender or the foreground
    fallback; returns the send result string."""
    if send_fn is not None:
        return send_fn(sender, text)
    from actions.send_message import send_message
    return send_message({
        "receiver": sender, "message_text": text,
        "platform": "whatsapp",
    }, player=None)


def handle_message(sender: str, message: str, send: bool = True,
                   send_fn=None, forward_fn=None) -> str:
    """Process one incoming message. Returns what happened (and sends when
    the decision is to auto-reply).

    `send_fn(sender, draft)` overrides the default foreground sender — the
    background WhatsApp monitor passes the Playwright bridge so replies go
    out without touching the screen.

    `forward_fn(sender)` — when provided, forwardable media (photo/video/
    document) gets forwarded to Meta AI (WhatsApp's built-in assistant) for
    REAL content analysis: the type-aware ack goes out instantly, then Meta
    AI's actual reply about the file is sent as a follow-up. Without it,
    media gets the type-aware template / Meta AI type-draft as before."""
    if not is_enabled():
        return "Secretary mode is OFF — enable it with 'secretary mode on'."
    sender = (sender or "").strip()
    message = (message or "").strip()
    if not sender or not message:
        return "Need both a sender and a message."

    # daktari: the JS row filter misses photo-less groups (single avatar).
    # The bridge send path via send_fn bypasses the subtitle group guard.
    # This name-based heuristic catches the most common group patterns so
    # the secretary NEVER replies to a group by accident.
    if _looks_like_group(sender):
        return (f"Skipping '{sender}' — looks like a group chat. The "
                f"secretary only replies to individual contacts.")

    _log(sender, "incoming", message)

    # Media (photo/video/voice note/document/...) gets a reply that fits the
    # TYPE — Meta AI reacts appropriately when drafting is on, otherwise the
    # deterministic media acknowledgment. Reactions/recalls/system notices
    # are not messages to answer at all.
    media_kind = _media_kind_of(message)
    if media_kind == "skip":
        return ("No reply needed — that was a reaction/recall/system "
                "notice, not a message to answer.")

    decision = triage(sender, message)

    # ── Delegation: auto-forward to the right handler ──
    # A real secretary doesn't just escalate to the boss — they route
    # to the right person. If a delegation rule matches, forward the
    # message to the handler AND acknowledge to the sender.
    delegation_result = _auto_delegate(sender, message, send_fn=send_fn)
    if delegation_result:
        # Message was forwarded to the handler — still acknowledge to sender
        draft = decision["draft"]
        if send:
            try:
                _send_text(sender, draft, send_fn)
                _log(sender, "outgoing", draft)
            except Exception:
                pass  # acknowledgment is best-effort
        return (f"🔀 Delegated: {delegation_result}\n"
                f"  📨 Acknowledgment sent to {sender}: {draft}")

    if decision["action"] == "escalate":
        return _escalate(sender, message, decision["reasons"], decision["draft"],
                        priority=decision.get("priority", "fyi"),
                        promises=decision.get("promises"))

    draft = decision["draft"]
    if media_kind:
        draft = _draft_media_reply(sender, media_kind)
    if not send:
        _log(sender, "outgoing", draft)
        return f"[DRAFT] to {sender}: {draft}"

    # Forwardable media + drafting on + a forwarder available → forward the
    # actual file to Meta AI so the reply is a real analysis, not a type
    # template. The safe ack goes out FIRST (instant reply for the sender),
    # then Meta AI's analysis follows when it's ready. Any failure falls
    # back to the ack already sent — never a crash, never a missing reply.
    if (media_kind in _FORWARDABLE_MEDIA and _meta_drafts_enabled()
            and forward_fn is not None):
        try:
            result = _send_text(sender, draft, send_fn)
            _log(sender, "outgoing", draft)
        except Exception as e:
            return f"Ack to {sender} failed: {e}\n  ack: {draft}"
        try:
            analysis = forward_fn(sender)
        except Exception as e:
            return (f"Acked {sender} with the type reply; asking Meta AI to "
                    f"analyze it failed: {e}")
        if not analysis or not str(analysis).strip():
            return f"Acked {sender} with the type reply (Meta AI gave nothing back)."
        analysis = str(analysis).strip()
        try:
            _send_text(sender, analysis, send_fn)
            _log(sender, "outgoing", analysis)
        except Exception as e:
            return (f"Acked {sender} and Meta AI analyzed it, but sending the "
                    f"analysis failed: {e}\n  analysis: {analysis}")
        return (f"Replied to {sender} with the type ack, then forwarded the "
                f"media to Meta AI and sent its real analysis:\n"
                f"  1. {draft}\n  2. {analysis}")

    # Routine auto-replies get drafted by Meta AI (WhatsApp's built-in
    # assistant) in the boss's casual register when enabled — the
    # deterministic template stays as the instant fallback for every
    # failure mode (unavailable, timeout, over-commit, burst).
    draft = _meta_draft(sender, message, draft, media_kind=media_kind)
    try:
        result = _send_text(sender, draft, send_fn)
        _log(sender, "outgoing", draft)
        return f"Replied to {sender}: {draft}\n  (send result: {result})"
    except Exception as e:
        return f"Drafted reply but sending failed: {e}\n  draft: {draft}"


# ── Morning briefing ─────────────────────────────────────────────────────────
def _morning_briefing() -> str:
    """Generate a comprehensive morning briefing.

    A real secretary starts the boss's day with everything they need:
    calendar, email, inbox, follow-ups, and recent conversations.
    Calendar and email are pulled from Composio when available;
    the briefing degrades gracefully without them.
    """
    st = _state()
    inbox = st.get("inbox", []) or []
    followups = [f for f in (st.get("followups", []) or []) if not f.get("done")]
    convs = st.get("conversations", {}) or {}
    contacts = st.get("contacts", {}) or {}

    lines = [f"☀️ Morning briefing for {_boss_name()}:"]
    lines.append(f"  {datetime.now().strftime('%A, %B %d — %I:%M %p')}")

    # ── Calendar (Composio Google Calendar) ──
    if _composio_available():
        try:
            cal_result = _composio_task(
                "List my calendar events for today. Show time and title. "
                "If none, say 'No meetings today'. Keep it brief.",
                timeout=20.0,
            )
            if cal_result and "no meetings" not in cal_result.lower():
                lines.append(f"\n  📅 Calendar:")
                for line in cal_result.splitlines()[:6]:
                    lines.append(f"    {line.strip()}")
            else:
                lines.append("\n  📅 Calendar: no meetings today")
        except Exception:
            lines.append("\n  📅 Calendar: unavailable")
    else:
        lines.append("\n  📅 Calendar: not connected (run 'composio add googlecalendar')")

    # ── Email (Composio Gmail) ──
    if _composio_available():
        try:
            email_result = _composio_task(
                "How many unread emails do I have? For each urgent/important "
                "one, show sender and subject in one line. Skip newsletters. "
                "If none urgent, just say the count.",
                timeout=20.0,
            )
            if email_result:
                lines.append(f"\n  📧 Email:")
                for line in email_result.splitlines()[:6]:
                    lines.append(f"    {line.strip()}")
        except Exception:
            lines.append("\n  📧 Email: unavailable")
    else:
        lines.append("\n  📧 Email: not connected (run 'composio add gmail')")

    # ── Secretary inbox (WhatsApp messages) ──
    if inbox:
        urgent = [i for i in inbox if i.get("priority") == "urgent"]
        today = [i for i in inbox if i.get("priority") == "today"]
        week = [i for i in inbox if i.get("priority") == "week"]
        fyi = [i for i in inbox if i.get("priority") == "fyi"]
        lines.append(f"\n  📥 Messages: {len(inbox)} escalated item(s)")
        if urgent:
            lines.append(f"    🔴 {len(urgent)} URGENT — handle NOW:")
            for i in urgent[:3]:
                lines.append(f"       • {i['from']}: \"{i['message'][:60]}\"")
        if today:
            lines.append(f"    🟡 {len(today)} today:")
            for i in today[:3]:
                lines.append(f"       • {i['from']}: \"{i['message'][:60]}\"")
        if week:
            lines.append(f"    🔵 {len(week)} this week")
        if fyi:
            lines.append(f"    ⚪ {len(fyi)} informational")
    else:
        lines.append("\n  📥 Messages: clear 🎉")

    # ── Follow-ups ──
    if followups:
        overdue = [f for f in followups if _is_overdue(f)]
        lines.append(f"\n  📝 Follow-ups: {len(followups)} pending")
        if overdue:
            lines.append(f"    ⚠️ {len(overdue)} OVERDUE:")
            for f in overdue[:3]:
                lines.append(f"       • {f['from']}: \"{f['promise'][:50]}\"")
        upcoming = [f for f in followups if not _is_overdue(f)][:3]
        if upcoming:
            lines.append(f"    Upcoming:")
            for f in upcoming:
                dl = f.get("deadline", "no deadline")
                lines.append(f"       • {f['from']}: \"{f['promise'][:50]}\" (by {dl})")
    else:
        lines.append("\n  📝 Follow-ups: none pending")

    # Recent conversations (last 24h)
    recent_senders = []
    cutoff = datetime.now().isoformat()[:10]  # today
    for sender, entries in convs.items():
        for e in reversed(entries):
            at = str(e.get("at", ""))
            if at[:10] >= cutoff and e.get("role") == "incoming":
                recent_senders.append(sender)
                break
    if recent_senders:
        lines.append(f"\n  💬 Messages from: {', '.join(recent_senders[:5])}")
    else:
        lines.append("\n  💬 No new messages today")

    # Session stats
    total_handled = sum(c.get("message_count", 0) for c in contacts.values())
    delegations = len(st.get("delegation_rules", {}))
    contact_count = len(contacts)
    stats_parts = []
    if total_handled:
        stats_parts.append(f"{total_handled} messages handled")
    if contact_count:
        stats_parts.append(f"{contact_count} contacts tracked")
    if delegations:
        stats_parts.append(f"{delegations} delegation rules")
    if stats_parts:
        lines.append(f"\n  📊 Stats: {', '.join(stats_parts)}")

    # Delegation rules summary
    deleg_rules = st.get("delegation_rules", {})
    if deleg_rules:
        lines.append(f"\n  🔀 Delegation:")
        for cat, handler in list(deleg_rules.items())[:3]:
            lines.append(f"    • {cat} → {handler}")
        if len(deleg_rules) > 3:
            lines.append(f"    ... and {len(deleg_rules) - 3} more")

    return "\n".join(lines)


def _is_overdue(followup: dict) -> bool:
    """Check if a follow-up is overdue based on its deadline."""
    deadline = (followup.get("deadline") or "").lower()
    if not deadline or deadline == "no deadline":
        return False
    # Simple checks
    now = datetime.now()
    if "yesterday" in deadline:
        return True
    if "today" in deadline:
        return True
    if "this morning" in deadline or "this afternoon" in deadline:
        return True
    return False


# ── Proactive alerts ─────────────────────────────────────────────────────────
def _proactive_alerts() -> str:
    """Generate proactive alerts the boss needs to know about.

    A real secretary doesn't wait to be asked — they proactively alert
    the boss about overdue items, stale conversations, and pending
    follow-ups.
    """
    alerts = []
    st = _state()

    # Overdue follow-ups
    followups = [f for f in (st.get("followups", []) or []) if not f.get("done")]
    overdue = [f for f in followups if _is_overdue(f)]
    if overdue:
        for f in overdue:
            alerts.append(
                f"⚠️ OVERDUE follow-up: {f['from']} — \"{f['promise'][:60]}\"\n"
                f"   deadline was: {f.get('deadline', '?')}"
            )

    # Stale conversations (someone messaged 2+ days ago with no reply)
    convs = st.get("conversations", {}) or {}
    from datetime import timedelta
    two_days_ago = (datetime.now() - timedelta(days=2)).isoformat()[:10]
    for sender, entries in convs.items():
        if not entries:
            continue
        last_incoming = None
        last_outgoing = None
        for e in reversed(entries):
            if e.get("role") == "incoming" and not last_incoming:
                last_incoming = e
            if e.get("role") == "outgoing" and not last_outgoing:
                last_outgoing = e
        if (last_incoming and last_incoming.get("at", "")[:10] <= two_days_ago
                and (not last_outgoing or last_outgoing.get("at", "")[:10] <= two_days_ago)):
            alerts.append(
                f"💤 Stale conversation: {sender} — last message "
                f"{last_incoming.get('at', '?')[:10]}, no reply sent"
            )

    # Inbox overflow
    inbox = st.get("inbox", []) or []
    urgent_count = sum(1 for i in inbox if i.get("priority") == "urgent")
    if urgent_count >= 3:
        alerts.append(
            f"🔴 Inbox overflow: {urgent_count} urgent items waiting!"
        )

    if not alerts:
        return "✅ All clear — no proactive alerts. You're on top of things!"

    return f"🔔 {len(alerts)} alert(s):\n\n" + "\n\n".join(alerts)


# ── Calendar integration (via Composio Google Calendar) ────────────────────────
# A real secretary manages the boss's calendar: checks today's meetings,
# sends availability, blocks focus time, and reminds about upcoming events.

def _composio_available() -> bool:
    """True when the Composio agent is importable (Gmail/Calendar tools)."""
    try:
        from composio_agent import run_agentic_task
        return True
    except Exception:
        return False


def _composio_task(text: str, timeout: float = 30.0) -> str:
    """Run a task through the Composio agent (Gmail, Calendar, GitHub).
    Returns the result text, or an error string. Times out after `timeout`
    seconds so the briefing never hangs."""
    import threading as _threading
    result_holder: dict = {"result": None, "error": None}
    def _run():
        try:
            from composio_agent import run_agentic_task
            result_holder["result"] = run_agentic_task(text) or "Done."
        except Exception as e:
            result_holder["error"] = f"Composio unavailable: {type(e).__name__}: {e}"
    t = _threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        return "Composio timed out (try again later)"
    return result_holder["error"] or result_holder["result"] or "Done."


def _calendar_today() -> str:
    """Get today's calendar events via Composio Google Calendar."""
    if not _composio_available():
        return ("Calendar not connected. Connect Google Calendar via Composio: "
                "'composio add googlecalendar' in your terminal, or use "
                "'secretary connect gmail/calendar' to open the setup.")
    return _composio_task(
        "List my calendar events for today. Show the time, title, and "
        "location (if any) for each event. If no events, say so."
    )


def _calendar_tomorrow() -> str:
    """Get tomorrow's calendar events."""
    if not _composio_available():
        return "Calendar not connected. Run 'composio add googlecalendar'."
    return _composio_task(
        "List my calendar events for tomorrow. Show time, title, location."
    )


def _calendar_week() -> str:
    """Get this week's calendar overview."""
    if not _composio_available():
        return "Calendar not connected."
    return _composio_task(
        "List my calendar events for this week (Monday to Friday). "
        "Group by day. Show time and title for each."
    )


def _calendar_next() -> str:
    """Get the next upcoming meeting."""
    if not _composio_available():
        return "Calendar not connected."
    return _composio_task(
        "What is my next upcoming calendar event? Show the time, title, "
        "who invited me, and any notes/description."
    )


def _calendar_free(text: str) -> str:
    """Check availability for a proposed time."""
    if not _composio_available():
        return "Calendar not connected."
    return _composio_task(
        f"Check if I'm free at this time: {text}. "
        "If free, say so. If busy, list what conflicts."
    )


def _calendar_schedule(text: str) -> str:
    """Create a calendar event."""
    if not _composio_available():
        return "Calendar not connected."
    return _composio_task(
        f"Create a calendar event: {text}. "
        "Confirm the event was created with time, title, and any attendees."
    )


# ── Email triage (via Composio Gmail) ────────────────────────────────────────
# A real secretary applies the 4 D's to email: Delete, Do, Delegate, Defer.
# They triage the inbox, surface urgent items, and draft replies.

def _email_inbox() -> str:
    """Get a summary of the inbox (unread emails)."""
    if not _composio_available():
        return ("Gmail not connected. Connect via Composio: "
                "'composio add gmail' in your terminal.")
    return _composio_task(
        "List my unread emails. For each: who sent it, subject, and a "
        "one-line summary. Group by priority: urgent/important first, "
        "then newsletters/automated, then everything else."
    )


def _email_urgent() -> str:
    """Get only urgent/important emails."""
    if not _composio_available():
        return "Gmail not connected."
    return _composio_task(
        "List only my URGENT or IMPORTANT unread emails. For each: "
        "sender, subject, one-line summary, and whether it needs a "
        "reply. Skip newsletters, automated messages, and promotions."
    )


def _email_draft(text: str) -> str:
    """Draft a reply to an email."""
    if not _composio_available():
        return "Gmail not connected."
    return _composio_task(
        f"Draft a reply to this email request: {text}. "
        "Write the draft in a professional but warm tone. "
        "Show the draft and ask if it should be sent."
    )


def _email_summary() -> str:
    """Get a full email summary for the morning briefing."""
    if not _composio_available():
        return "Gmail not connected."
    return _composio_task(
        "Summarize my email inbox: how many unread, who wrote, what about. "
        "Flag any that need a reply today. Skip newsletters and automated."
    )


def _email_triage_report() -> str:
    """Triage emails using the 4D framework."""
    if not _composio_available():
        return "Gmail not connected."
    return _composio_task(
        "Triage my unread emails using the 4D framework:\n"
        "  DELETE: newsletters, spam, automated (list them)\n"
        "  DO: quick replies under 2 minutes (list with suggested reply)\n"
        "  DELEGATE: messages that should go to someone else (who?)\n"
        "  DEFER: messages that need more time (when to follow up)\n"
        "For each category, list the emails with sender, subject, and action."
    )


# ── Delegation system ─────────────────────────────────────────────────────────
# A real secretary routes messages to the right person. This module tracks
# who handles what, so the secretary can say 'forward this to X' or
# 'cc the legal team'.

def _delegation_list() -> str:
    """Show delegation rules (who handles what)."""
    st = _state()
    rules = st.get("delegation_rules", {})
    if not rules:
        return ("No delegation rules set. Add some with:\n"
                "  secretary delegate_add category='legal' handler='Lawyer Bob'\n"
                "  secretary delegate_add category='finance' handler='Accountant Alice'")
    lines = ["📋 Delegation rules:"]
    for cat, handler in rules.items():
        lines.append(f"  • {cat} → {handler}")
    return "\n".join(lines)


def _delegation_add(category: str, handler: str) -> str:
    """Add a delegation rule."""
    st = _state()
    rules = st.setdefault("delegation_rules", {})
    rules[category.lower().strip()] = handler.strip()
    _save_state(st)
    return f"✅ Delegation rule added: {category} → {handler}"


def _delegation_remove(category: str) -> str:
    """Remove a delegation rule."""
    st = _state()
    rules = st.get("delegation_rules", {})
    key = category.lower().strip()
    if key in rules:
        removed = rules.pop(key)
        _save_state(st)
        return f"✅ Removed delegation rule: {category} → {removed}"
    return f"No delegation rule found for '{category}'."


def _delegation_check(message: str) -> str:
    """Check if a message should be delegated based on rules."""
    st = _state()
    rules = st.get("delegation_rules", {})
    if not rules:
        return "No delegation rules configured."
    text = (message or "").lower()
    matches = []
    for category, handler in rules.items():
        if category in text:
            matches.append((category, handler))
    if matches:
        cat, handler = matches[0]
        return (f"🔀 This looks like it should be delegated:\n"
                f"  Category: {cat}\n"
                f"  Handler: {handler}\n"
                f"  → Forward to {handler}? (secretary delegate_forward {handler})")
    return "No delegation rule matched this message."


def _delegation_match(message: str) -> tuple[str, str] | None:
    """Return (category, handler) if the message matches a delegation rule,
    else None. Used by the triage flow to auto-flag delegatable messages."""
    st = _state()
    rules = st.get("delegation_rules", {})
    if not rules:
        return None
    text = (message or "").lower()
    for category, handler in rules.items():
        if category in text:
            return (category, handler)
    return None


def _delegation_forward(handler: str, sender: str, message: str,
                        send_fn=None) -> str:
    """Forward a message to the delegated handler via WhatsApp.

    The handler is a contact name (e.g. 'Lawyer Bob'). The message is
    forwarded with context: who it's from, what it's about, and that it
    was delegated by the boss's secretary.
    """
    handler = (handler or "").strip()
    sender = (sender or "").strip()
    message = (message or "").strip()
    if not handler:
        return "Need a handler name to forward to."
    if not message:
        return "Need a message to forward."

    # Build the forwarded message with context
    forwarded = (
        f"📋 Delegated message from {_boss_name()}'s secretary:\n\n"
        f"From: {sender}\n"
        f"Message: {message}\n\n"
        f"{_boss_name()} asked me to forward this to you for handling."
    )

    # Send via WhatsApp
    try:
        from actions.send_message import send_message
        result = send_message({
            "receiver": handler,
            "message_text": forwarded,
            "platform": "whatsapp",
        }, player=None)
    except Exception as e:
        return f"Forward failed: {e}"

    # Log the delegation
    _log_delegation(sender, handler, message)

    return (f"✅ Forwarded to {handler}:\n"
            f"  From: {sender}\n"
            f"  Message: {message[:80]}{'…' if len(message) > 80 else ''}\n"
            f"  Result: {result}")


def _log_delegation(sender: str, handler: str, message: str) -> None:
    """Log a forwarded delegation for the session report."""
    st = _state()
    forwarded = st.setdefault("forwarded", [])
    forwarded.append({
        "from": sender,
        "to": handler,
        "message": message[:200],
        "at": datetime.now().isoformat(timespec="seconds"),
    })
    # Keep bounded
    if len(forwarded) > 100:
        del forwarded[:-80]
    _save_state(st)


def _delegation_forwarded_list() -> str:
    """Show recently forwarded messages."""
    st = _state()
    forwarded = st.get("forwarded", []) or []
    if not forwarded:
        return "No messages have been forwarded yet."
    lines = [f"📤 {len(forwarded)} forwarded message(s):"]
    for f in forwarded[-10:]:  # show last 10
        lines.append(f"  • {f['from']} → {f['to']} ({f['at'][:16]})")
        lines.append(f"    \"{f['message'][:60]}\"")
    return "\n".join(lines)


def _auto_delegate(sender: str, message: str, send_fn=None) -> str | None:
    """Check if a message should be auto-forwarded to a delegated handler.

    Returns the forwarding result string if auto-forwarded, else None.
    This integrates with the triage flow so delegatable messages are
    automatically routed to the right person.
    """
    match = _delegation_match(message)
    if match is None:
        return None
    category, handler = match
    # Auto-forward the message to the handler
    result = _delegation_forward(handler, sender, message, send_fn=send_fn)
    return result


# ── Meeting prep ──────────────────────────────────────────────────────────────
# A real secretary prepares briefs before meetings: who you're meeting,
# recent context, talking points, action items from previous meetings.

def _meeting_prep(text: str) -> str:
    """Prepare a brief for an upcoming meeting.

    Uses the boss's memory + CRM + recent conversations to build context
    about who they're meeting and what to discuss.
    """
    parts = [f"📋 Meeting prep briefing:"]

    # Who are we meeting?
    from memory.memory_manager import load_memory
    memory = load_memory()
    contacts = _state().get("contacts", {})

    # Check if we know this person
    for key, contact in contacts.items():
        name_lower = key.lower()
        text_lower = (text or "").lower()
        if name_lower in text_lower or name_lower in text_lower.split():
            parts.append(f"\n📇 Contact info: {contact.get('name', key)}")
            if contact.get("relationship"):
                parts.append(f"  Relationship: {contact['relationship']}")
            if contact.get("notes"):
                parts.append(f"  Notes: {contact['notes']}")
            parts.append(f"  Messages handled: {contact.get('message_count', 0)}")
            # Recent conversations
            convs = _state().get("conversations", {})
            recent = convs.get(key, [])[-3:]
            if recent:
                parts.append(f"  Recent context:")
                for e in recent:
                    role = "them" if e.get("role") == "incoming" else "us"
                    parts.append(f"    [{role}] {str(e.get('text', ''))[:80]}")
            break
    else:
        parts.append(f"\nNo contact info found for '{text}'. "
                     "Run 'secretary scan' or add a contact: "
                     "'secretary contact \"Name\" relationship=... notes=...'")

    # Check calendar for context
    parts.append(f"\n📅 Calendar check: run 'secretary calendar' for today's schedule.")

    # Suggested talking points
    parts.append(f"\n💡 Suggested preparation:")
    parts.append(f"  1. Review recent conversations with this person")
    parts.append(f"  2. Check if they have any pending items in your inbox")
    parts.append(f"  3. Prepare any documents they might need")

    return "\n".join(parts)


def _meeting_prep_calendar() -> str:
    """Auto-prep for the next meeting by combining calendar + CRM + memory."""
    if not _composio_available():
        return _meeting_prep("next meeting")
    # Get next meeting from calendar
    cal_result = _composio_task(
        "What is my next calendar event? Show the title, time, and "
        "who invited me. Return ONLY the event info, nothing else."
    )
    # Build prep brief
    brief = [f"📋 Auto meeting prep:", f"\n📅 Next meeting: {cal_result}"]
    # Check contacts for the attendees
    st = _state()
    contacts = st.get("contacts", {})
    for key, contact in contacts.items():
        if key.lower() in cal_result.lower():
            brief.append(f"\n📇 {contact.get('name', key)}:")
            if contact.get("relationship"):
                brief.append(f"  Relationship: {contact['relationship']}")
            if contact.get("notes"):
                brief.append(f"  Notes: {contact['notes']}")
    brief.append(f"\n💡 Prepare: review recent conversations, check pending items")
    return "\n".join(brief)


# ── Tool entry point ──────────────────────────────────────────────────────────

def secretary(parameters: dict, player=None, session_memory=None) -> str:
    """Tool dispatcher.

    Actions:
      on / off / status        — toggle and query secretary mode
      link / link close        — WhatsApp window management
      handle  sender, message  — process one incoming message
      inbox                    — priority-sorted: 🔴 urgent → 🟡 today → 🔵 week → ⚪ fyi
      snooze / done            — manage inbox items
      reply   sender, text     — send a personal reply
      followups / followup_done — track promises the boss made
      briefing                 — morning summary: inbox + follow-ups + stats
      alerts                   — proactive: overdue items, stale convos
      contact [name]           — contact CRM
      scan / report            — pet-name scan / session summary
      calendar                 — today's meetings (via Composio Google Calendar)
      calendar tomorrow/week/next/free/schedule — calendar operations
      email                    — inbox summary (via Composio Gmail)
      email urgent/draft/triage — email management with 4D framework
      delegate_add/remove/list — delegation rules (who handles what)
      meeting_prep [name]      — prepare brief for upcoming meeting
    """
    params = parameters or {}
    action = str(params.get("action", "status")).strip().lower()
    sender = str(params.get("sender", "")).strip()
    text = str(params.get("message") or params.get("text") or "").strip()

    if action in ("link", "connect"):
        try:
            from actions.secretary_listener import link_whatsapp
            return link_whatsapp()
        except Exception as e:
            return f"Could not open the WhatsApp link window: {e}"
    if action in ("link_close", "close_link", "link_off"):
        try:
            from actions.secretary_listener import close_link_window
            return close_link_window()
        except Exception as e:
            return f"Could not close the WhatsApp link window: {e}"
    if action in ("report", "summary"):
        if not is_enabled():
            return ("Secretary mode is OFF — there is no active session to "
                    "report. Say 'secretary mode on' to start one.")
        return _session_overview()
    if action in ("scan", "petnames", "pet_names"):
        # One-time scan of the existing chats for what each contact calls
        # the boss ("baby" from the wife) — stored as a static map, never
        # re-scanned per reply. 'secretary scan deep' re-reads more chats.
        limit = int(params.get("limit", 15) or 15)
        if str(params.get("deep") or "").strip().lower() in ("1", "true",
                                                              "yes"):
            limit = 40
        return scan_pet_names(limit=limit)
    if action in ("on", "enable", "start"):
        cfg = _load_cfg()
        cfg["secretary_mode"] = True
        _save_cfg(cfg)
        _session_start_ts()   # remember when this session began
        # Start the background WhatsApp monitor. Any failure (no WhatsApp,
        # no vision deps) must never break the mode toggle itself.
        try:
            from actions.secretary_listener import start_monitor
            monitor_note = start_monitor()
        except Exception as e:
            monitor_note = f"background WhatsApp monitoring could not start: {e}"
        return (f"Secretary mode ON. {monitor_note.capitalize()}. "
                f"Messages are handled by {_secretary_sig()} — urgent ones "
                f"still reach you. Manual feed still works: "
                f"secretary handle sender='Mom' message='...'")
    if action in ("off", "disable", "stop"):
        cfg = _load_cfg()
        cfg["secretary_mode"] = False
        _save_cfg(cfg)
        # The always-on remote dashboard (secretary_self_chat) keeps the
        # monitor running even with secretary OFF — the sweep simply stops
        # triaging third-party chats (it re-checks is_enabled() every poll).
        # Only stop the browser when there is no dashboard to serve.
        if cfg.get("secretary_self_chat"):
            stop_note = ("The remote dashboard stays connected — messages "
                         "to your self-chat still reach Jeeves")
        else:
            try:
                from actions.secretary_listener import stop_monitor
                stop_note = stop_monitor().capitalize()
            except Exception as e:
                stop_note = f"Background monitoring could not stop: {e}"
        overview = _session_overview()
        return (f"Secretary mode OFF. {stop_note}.\n\n"
                f"{overview}")
    if action == "status":
        if not is_enabled():
            return ("Secretary mode is OFF. Say 'secretary mode on' to let "
                    "Jeeves hold conversations for you.")
        pending = len(_state().get("inbox", []))
        monitor_note = ""
        try:
            from actions.secretary_listener import monitor_status
            monitor_note = f" WhatsApp monitor: {monitor_status()}."
        except Exception:
            pass
        return (f"Secretary mode is ON ({_secretary_sig()}). "
                f"{pending} escalated item(s) waiting in the inbox — "
                f"'secretary inbox' to review.{monitor_note}")
    if action == "handle":
        return handle_message(sender, text)
    if action == "inbox":
        items = _state().get("inbox", [])
        if not items:
            return "Inbox is empty — nothing has been escalated."
        # Priority-sorted display: urgent → today → week → fyi
        priority_order = {"urgent": 0, "today": 1, "week": 2, "fyi": 3}
        items_sorted = sorted(items,
                             key=lambda it: priority_order.get(it.get("priority", "fyi"), 3))
        prio_emoji = {"urgent": "🔴", "today": "🟡", "week": "🔵", "fyi": "⚪"}
        lines = [f"{len(items)} escalated item(s) for {_boss_name()}:"]
        current_prio = None
        for i, it in enumerate(items_sorted, 1):
            prio = it.get("priority", "fyi")
            if prio != current_prio:
                current_prio = prio
                lines.append(f"\n  {prio_emoji.get(prio, '⚪')} {prio.upper()}:")
            lines.append(f"    {i}. From {it['from']} ({it['at'][:16]}): "
                         f"\"{it['message'][:80]}{'…' if len(it.get('message', '')) > 80 else ''}\"")
            lines.append(f"       why: {', '.join(it.get('reasons', []))}")
            lines.append(f"       suggested: {it.get('draft', '')[:60]}")
        return "\n".join(lines)
    if action == "snooze":
        # Snooze an inbox item (move it to 'later')
        index = int(params.get("index", 0) or 0) - 1  # 1-based
        items = _state().get("inbox", [])
        if not items or index < 0 or index >= len(items):
            return "Usage: secretary snooze <number> (from inbox list)"
        item = items[index]
        item["snoozed_until"] = datetime.now().isoformat(timespec="seconds")
        item["priority"] = "week"  # demote to weekly
        st = _state()
        _save_state(st)
        return f"Snoozed: {item['from']} — moved to weekly priority."
    if action == "done":
        # Mark an inbox item as handled
        index = int(params.get("index", 0) or 0) - 1  # 1-based
        items = _state().get("inbox", [])
        if not items or index < 0 or index >= len(items):
            return "Usage: secretary done <number> (from inbox list)"
        removed = items.pop(index)
        st = _state()
        _save_state(st)
        return f"✅ Done: removed '{removed['from']}' from inbox."
    if action == "followups":
        # Show pending follow-ups (promises the boss made)
        followups = [f for f in _state().get("followups", [])
                     if not f.get("done")]
        if not followups:
            return "No pending follow-ups — you're all caught up! 🎉"
        lines = [f"📝 {len(followups)} pending follow-up(s):"]
        for i, f in enumerate(followups, 1):
            deadline = f.get("deadline", "no deadline")
            lines.append(f"  {i}. {f['from']}: \"{f['promise']}\" (by {deadline})")
            lines.append(f"     added: {f.get('created', '?')[:16]}")
        return "\n".join(lines)
    if action == "followup_done":
        # Mark a follow-up as complete
        index = int(params.get("index", 0) or 0) - 1
        followups = _state().get("followups", [])
        active = [f for f in followups if not f.get("done")]
        if not active or index < 0 or index >= len(active):
            return "Usage: secretary followup_done <number>"
        active[index]["done"] = True
        st = _state()
        _save_state(st)
        return f"✅ Follow-up marked done: {active[index]['promise'][:50]}"
    if action == "briefing":
        # Morning briefing: overnight messages + pending items + follow-ups
        return _morning_briefing()
    if action == "alerts":
        # Proactive alerts: overdue follow-ups, stale conversations
        return _proactive_alerts()
    if action == "contact":
        # Contact CRM: show or update a contact's info
        contact_name = str(params.get("sender", "") or params.get("name", "")).strip()
        if not contact_name:
            # List all contacts
            contacts = _state().get("contacts", {})
            if not contacts:
                return "No contacts tracked yet. Conversations will auto-populate."
            lines = [f"📇 {len(contacts)} contact(s):"]
            for key, c in sorted(contacts.items(),
                                key=lambda x: x[1].get("last_interaction", ""),
                                reverse=True):
                rel = c.get("relationship", "") or "—"
                notes = c.get("notes", "") or "—"
                lines.append(f"  • {c.get('name', key)}: {rel} ({notes[:40]})")
            return "\n".join(lines)
        # Show/update a specific contact
        relationship = str(params.get("relationship", "") or "").strip()
        notes = str(params.get("notes", "") or "").strip()
        if relationship or notes:
            _update_contact(contact_name, relationship=relationship, notes=notes)
            return f"📇 Updated contact: {contact_name}" + (
                f" (relationship: {relationship})" if relationship else "") + (
                f" (notes: {notes})" if notes else "")
        return _format_contact(contact_name)
    if action == "reply":
        if not sender or not text:
            return "reply needs sender and text: secretary reply \"Mom\" \"yes, sounds good\""
        try:
            from actions.send_message import send_message
            result = send_message({
                "receiver": sender, "message_text": text, "platform": "whatsapp",
            }, player=None)
            _log(sender, "outgoing", text)
            # clear this sender's escalated items
            st = _state()
            st["inbox"] = [it for it in st.get("inbox", [])
                           if it.get("from", "").lower() != sender.lower()]
            _save_state(st)
            return f"Sent to {sender}: {text}\n  (result: {result})"
        except Exception as e:
            return f"Reply failed: {e}"
    if action in ("inbox_clear", "clear"):
        _save_state({**_state(), "inbox": []})
        return "Escalated inbox cleared."
    # ── Calendar (Composio Google Calendar) ──
    if action == "calendar":
        sub = str(params.get("sub", "") or params.get("mode", "")).strip().lower()
        if sub in ("tomorrow", "tmr"):
            return _calendar_tomorrow()
        if sub in ("week", "this week"):
            return _calendar_week()
        if sub in ("next", "upcoming"):
            return _calendar_next()
        if sub in ("free", "available", "availability"):
            return _calendar_free(text or "now")
        if sub in ("schedule", "create", "add", "book"):
            return _calendar_schedule(text or "")
        return _calendar_today()
    # ── Email (Composio Gmail) ──
    if action == "email":
        sub = str(params.get("sub", "") or params.get("mode", "")).strip().lower()
        if sub in ("urgent", "important"):
            return _email_urgent()
        if sub in ("draft", "reply", "write"):
            return _email_draft(text or "")
        if sub in ("triage", "sort", "4d"):
            return _email_triage_report()
        if sub in ("summary", "briefing"):
            return _email_summary()
        return _email_inbox()
    # ── Delegation ──
    if action == "delegate_list":
        return _delegation_list()
    if action == "delegate_add":
        category = str(params.get("category", "") or "").strip()
        handler = str(params.get("handler", "") or params.get("text", "")).strip()
        if not category or not handler:
            return "Usage: secretary delegate_add category='legal' handler='Lawyer Bob'"
        return _delegation_add(category, handler)
    if action == "delegate_remove":
        category = str(params.get("category", "") or text or "").strip()
        if not category:
            return "Usage: secretary delegate_remove category='legal'"
        return _delegation_remove(category)
    if action == "delegate_check":
        msg = str(params.get("message", "") or text or "").strip()
        if not msg:
            return "Usage: secretary delegate_check message='we need legal review'"
        return _delegation_check(msg)
    if action == "delegate_forward":
        # Forward a message to a delegated handler
        handler = str(params.get("handler", "") or params.get("text", "") or text or "").strip()
        fwd_sender = str(params.get("sender", "") or "").strip()
        fwd_message = str(params.get("message", "") or "").strip()
        if not handler:
            return "Usage: secretary delegate_forward handler='Lawyer Bob' sender='Mom' message='...'"
        return _delegation_forward(handler, fwd_sender or "unknown", fwd_message or "(no message)")
    if action in ("delegate_forwarded", "forwarded"):
        return _delegation_forwarded_list()
    # ── Meeting prep ──
    if action in ("meeting_prep", "prep"):
        meeting_text = str(params.get("meeting", "") or params.get("name", "") or text or "").strip()
        sub = str(params.get("sub", "") or params.get("mode", "")).strip().lower()
        if sub in ("auto", "next", "calendar"):
            return _meeting_prep_calendar()
        return _meeting_prep(meeting_text or "next meeting")
    return ("Unknown action. Use: on | off | status | link | link close | "
            "handle | inbox | snooze | done | reply | inbox_clear | "
            "followups | followup_done | briefing | alerts | "
            "contact | scan | report | calendar | email | "
            "delegate_add/remove/list/check | meeting_prep.")
