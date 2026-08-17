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
        tok = re.split(r"[\s,!:;?…—\-–]+", m, 1)[0].strip("'\"….!?")
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

    Returns {"action": "reply"|"escalate", "reasons": [...], "draft": str}.
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

    if reasons:
        return {"action": "escalate", "reasons": reasons[:3], "draft": draft}
    return {"action": "reply", "reasons": [], "draft": draft}


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


def _escalate(sender: str, message: str, reasons: list[str], draft: str) -> str:
    st = _state()
    inbox = st.setdefault("inbox", [])
    inbox.append({
        "from": sender, "message": message, "reasons": reasons,
        "draft": draft, "at": datetime.now().isoformat(timespec="seconds"),
    })
    _save_state(st)
    return (
        f"[ESCALATED to {_boss_name()}] from {sender}:\n"
        f"  \"{message}\"\n"
        f"  why: {', '.join(reasons)}.\n"
        f"  suggested reply: {draft}\n"
        f"  → answer it with: secretary reply \"{sender}\" \"<your words>\""
    )


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

    if decision["action"] == "escalate":
        return _escalate(sender, message, decision["reasons"], decision["draft"])

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


# ── Tool entry point ──────────────────────────────────────────────────────────

def secretary(parameters: dict, player=None, session_memory=None) -> str:
    """Tool dispatcher.

    Actions:
      on / off / status        — toggle and query secretary mode (off ends
                                 with a session report: who it talked to,
                                 what it told them, calls seen, urgent items)
      link                     — open the persistent WhatsApp window (scan
                                 the QR once, stay connected forever)
      link close               — close that window (monitoring stops too)
      handle  sender, message  — process one incoming message
      inbox                    — escalated items waiting for the boss
      reply   sender, text     — send a personal reply as the boss
      inbox_clear              — clear escalated items

    While monitoring, the secretary also watches for incoming audio/video
    calls (and missed calls in the chat list) — it can't pick up, so it
    escalates them to the boss and logs them in the session report.
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
        lines = [f"{len(items)} escalated item(s) for {_boss_name()}:"]
        for i, it in enumerate(items, 1):
            lines.append(f"\n{i}. From {it['from']} ({it['at'][:16]}): "
                         f"\"{it['message']}\"")
            lines.append(f"   why: {', '.join(it.get('reasons', []))}")
            lines.append(f"   suggested: {it.get('draft', '')}")
        return "\n".join(lines)
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
    return ("Unknown action. Use: on, off, status, link, link close, handle, "
            "inbox, reply, inbox_clear.")
