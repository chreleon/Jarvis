# actions/secretary_listener.py
# SecretaryListener — background WhatsApp monitoring for secretary mode.
#
# The triage engine in actions/secretary.py only reacts to messages it is
# handed. This module gives it a background connection: it runs WhatsApp Web
# (web.whatsapp.com) in its own Playwright browser (headless by default) and
# polls the chat-list DOM for new unread messages, feeding each new one to
# the existing triage engine, which auto-replies — through the SAME
# background browser — or escalates to the inbox. Exactly the same decisions
# as hand-fed messages.
#
# No screen vision, no screenshots, no foreground requirement: everything is
# read from the page's DOM, so WhatsApp never has to be on screen, focused,
# or unlocked (a locked screen kills screenshots; it does not touch DOM
# reads). The only human step is a one-time QR link — see monitor_status().
#
# Cost: a persistent browser process (~200MB) while monitoring is on, plus a
# cheap DOM read every poll_seconds. No LLM per message — triage stays the
# same deterministic rules.
#
# Importing this module is cheap and it does nothing unless start_monitor()
# is called — same contract as clap_listen.py.

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

# Cheap module-level imports: whatsapp_bridge itself only imports stdlib (its
# playwright import is deferred to first use), so this module stays "import
# cheap, do nothing until start_monitor()/link_whatsapp() is called".
from actions.whatsapp_bridge import (
    acquire_shared_bridge,
    is_profile_linked,
    release_shared_bridge,
    stop_all_bridges,
)

# ── Poll tuning (override via config/api_keys.json) ─────────────────────────
DEFAULT_POLL_SECONDS = 15.0
LOGIN_POLL_SECONDS   = 5.0   # poll faster while waiting for the QR link

_CFG_PATH = Path(__file__).resolve().parent.parent / "config" / "api_keys.json"


def _fingerprint(sender: str, preview: str) -> str:
    """Stable, case-insensitive fingerprint for a (sender, message) pair."""
    raw = f"{sender.lower().strip()}|{preview.lower().strip()}"
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()


class SecretaryListener:
    """Controllable background WhatsApp monitor (ClapListener-style start /
    stop so a live `secretary on/off` toggle works without restarting)."""

    def __init__(self, on_message: Callable[[str, str], str] | None = None,
                 poll_seconds: float = DEFAULT_POLL_SECONDS,
                 bridge=None,
                 on_self_chat: Callable[[str, str], str] | None = None):
        """on_self_chat(sender, text) → reply: called when a message arrives
        from the boss's OWN self-chat (e.g. "Omoke Jr" — texting themselves
        from another number). The reply is sent back into that chat. Without
        it, self-chat messages are treated like any other contact."""
        self._on_message = on_message or self._default_on_message
        self._on_self_chat = on_self_chat
        self._self_chat_titles = _self_chat_titles()
        self._poll_seconds = max(5.0, float(poll_seconds))
        self._bridge = bridge          # injected in tests; else built in _loop
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._seen: set[str] = set()   # this-process dedupe (fast path)
        self._ring_escalated: dict[str, float] = {}  # ring key → last escalated
        self._filter_applied = False   # Unread-only filter active on the pane
        self._state = "starting"       # starting | qr | ok | error
        self._qr_path: str | None = None
        self._qr_notified = False
        self._last_poll_at: float | None = None
        self._last_error: str | None = None
        self._pet_scan_started = False   # one background pet-name scan per run
        self._sweeps_done = 0

    # ── lifecycle ──

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="SecretaryListenThread")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        self._thread = None

    # ── default triage hook (uses the background bridge to reply) ───────────

    def _default_on_message(self, sender: str, preview: str) -> str:
        from actions.secretary import handle_message
        send_fn = None
        forward_fn = None
        if self._bridge is not None:
            send_fn = self._bridge.send_message
            # Forwardable media (photo/video/document) goes to Meta AI for
            # REAL analysis — the bridge opens the sender's chat, forwards
            # the newest incoming media via WhatsApp's native forward path,
            # and returns Meta AI's actual reply about the content.
            forward_fn = self._bridge.forward_last_media_to_meta_ai
        return handle_message(sender, preview, send=True, send_fn=send_fn,
                              forward_fn=forward_fn)

    # ── main loop ──

    def _loop(self) -> None:
        bridge = self._bridge
        # A bridge passed in by start_monitor()/link_whatsapp() holds a
        # shared-registry reference that must be released on exit; a bridge
        # built here (fallback) is private and must be stopped directly.
        was_shared = bridge is not None
        if bridge is None:
            try:
                from actions.whatsapp_bridge import WhatsAppBridge
                bridge = WhatsAppBridge(headless=_headless_from_config())
                self._bridge = bridge
            except Exception as e:
                self._state = "error"
                self._last_error = f"Playwright/WhatsApp bridge unavailable: {e}"
                print(f"[SecretaryListener] {self._last_error}")
                while not self._stop_event.is_set():
                    self._sleep_interruptible(LOGIN_POLL_SECONDS)
                return
        try:
            bridge.start()
        except Exception as e:
            self._state = "error"
            self._last_error = str(e)
            print(f"[SecretaryListener] could not start WhatsApp Web: {e}")
            # Never leak: release the shared reference (or stop a private
            # fallback bridge) even when startup failed.
            self._release_bridge(bridge, was_shared)
            return
        print("[SecretaryListener] WhatsApp Web connected — "
              "monitoring in the background"
              + ("" if getattr(bridge, "visible", False)
                 else " (no screen needed)"))

        # daktari: a wedged browser (hung Playwright call) must not kill the
        # monitor silently. Consecutive poll failures trip a full bridge
        # rebuild — the dashboard and secretary self-heal instead of dying
        # with a stale _last_error forever.
        consecutive_failures = 0
        while not self._stop_event.is_set():
            try:
                unread = bridge.poll_unread()
                if unread is None:
                    # not logged in (yet): QR up = needs a one-time link
                    if bridge.needs_qr():
                        self._state = "qr"
                        if getattr(bridge, "visible", False):
                            # The QR is ON the visible window — nothing to
                            # capture; point the boss at the screen once.
                            if not self._qr_notified:
                                self._qr_notified = True
                                self._qr_path = None
                                print("[SecretaryListener] 🔑 WhatsApp Web "
                                      "needs linking once — scan the QR code "
                                      "shown in the WhatsApp window with your "
                                      "phone (WhatsApp > Linked devices). You "
                                      "only do this once.")
                        else:
                            # WhatsApp's QR rotates every ~30-60s, so refresh
                            # the capture on every poll (overwrites
                            # qr_login.png) — only the guidance prints once.
                            path = bridge.capture_qr()
                            if path and not self._qr_notified:
                                self._qr_notified = True
                                self._qr_path = path
                                print(f"[SecretaryListener] 🔑 WhatsApp Web "
                                      f"needs linking once — open {path} in a "
                                      f"viewer and scan it with your phone "
                                      f"(WhatsApp > Linked devices).")
                    else:
                        self._state = "starting"
                    self._sleep_interruptible(LOGIN_POLL_SECONDS)
                    continue
                if not self._filter_applied:
                    # Show only chats with unread messages (WhatsApp's own
                    # filter) so the poll walks a small pane instead of the
                    # whole chat list every sweep. Applied before the first
                    # handled sweep, then the pane is re-read so even the
                    # first pass only ever sees unread chats.
                    try:
                        if bridge.ensure_unread_filter():
                            self._filter_applied = True
                    except Exception as e:
                        self._last_error = f"unread filter: {e}"
                    unread = bridge.poll_unread()
                self._state = "ok"
                self._last_poll_at = time.time()
                self._handle_sweep(unread)
                # Also watch for a call ringing right now (audio or video).
                # The secretary can't pick up — it escalates to the boss so
                # the call is never missed silently.
                self._handle_ringing_calls(bridge)
                # One-time (per 24h) background scan of recent chats for
                # what each contact calls the boss — a STATIC map, never
                # re-extracted per draft (YinYang: the scan is the only
                # place chat text is read). Delayed until the browser has
                # warmed (2 sweeps ≈ 30s+), so chat threads aren't still
                # "Syncing older messages" when the scan reads them.
                self._sweeps_done += 1
                if self._sweeps_done >= 2:
                    self._maybe_scan_pet_names(bridge)
                consecutive_failures = 0
            except Exception as e:
                self._last_error = str(e)
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    print(f"[SecretaryListener] bridge failed "
                          f"{consecutive_failures} times ({e}) — rebuilding",
                          flush=True)
                    rebuilt = self._rebuild_bridge()
                    if rebuilt[2]:
                        bridge, was_shared, _ = rebuilt
                        consecutive_failures = 0
                    else:
                        # Rebuild failed (browser still wedged) — back off
                        # and retry; the loop will trip the rebuild again.
                        self._sleep_interruptible(LOGIN_POLL_SECONDS)
                        continue
            self._sleep_interruptible(self._poll_seconds)

        # Release the monitor's reference on the shared bridge: if a one-shot
        # send (or the visible link window) holds another reference, the
        # browser stays alive for it; otherwise it exits here (refcount hits
        # zero). A private fallback bridge is stopped directly.
        self._release_bridge(bridge, was_shared)
        print("[SecretaryListener] stopped.")

    def _release_bridge(self, bridge, was_shared: bool) -> None:
        """Release a shared-registry reference, or stop a private bridge."""
        if was_shared:
            release_shared_bridge(bridge)
        else:
            try:
                bridge.stop()
            except Exception:
                pass

    def _rebuild_bridge(self):
        """Replace a wedged/failed bridge with a fresh one.

        A stuck Playwright call can't be cancelled from another thread (the
        sync API is thread-bound), so the whole bridge is torn down and a
        brand-new one is acquired. Returns (bridge, was_shared, ok); also
        updates self._bridge and resets the Unread-filter flag so the new
        pane gets re-filtered."""
        from actions.whatsapp_bridge import (acquire_shared_bridge,
                                             stop_all_bridges)
        try:
            # Drop every registry reference (bounded by the bridge _submit
            # timeout) so a fresh browser can take over the profile.
            if self._bridge is not None and not \
                    getattr(self._bridge, "_ready", False):
                try:
                    self._bridge.stop()
                except Exception:
                    pass
            stop_all_bridges()
            bridge, _ = acquire_shared_bridge(
                headless=_headless_from_config(),
                cdp_url=_cdp_url_from_config())
            bridge.start()
            self._bridge = bridge
            self._filter_applied = False   # re-apply the Unread filter
            return bridge, True, True
        except Exception as e:
            self._last_error = f"bridge rebuild failed: {e}"
            print(f"[SecretaryListener] {self._last_error}", flush=True)
            return None, False, False

    def _sleep_interruptible(self, seconds: float) -> None:
        deadline = time.time() + seconds
        while not self._stop_event.is_set() and time.time() < deadline:
            time.sleep(0.5)

    # ── one sweep (the poll's unread set) ──

    def _handle_sweep(self, items) -> None:
        """Process one poll's worth of unread chats.

        Individuals only (groups are dropped upstream) and only chats whose
        last message is from the past 24h — newest first — so the recent
        catch-up is tended before anything else. Missed calls (preview says
        "Missed voice/video call") are escalated to the boss instead of
        auto-replied. Every handled item in the sweep is marked processed in
        ONE batched write instead of one file write per item (YinYang: flat
        disk I/O in the background)."""
        from actions.secretary import (_is_processed_many, _mark_processed_many,
                                       is_enabled)
        recent = [it for it in (items or []) if _is_recent(it.get("time", ""))]
        recent.sort(key=lambda it: _recency_key(it.get("time", "")),
                    reverse=True)
        # Secretary OFF → only the always-on remote dashboard is active (the
        # boss's own self-chat, configured via secretary_self_chat). Third-
        # party chats stay COMPLETELY untouched here — not fingerprinted, not
        # marked processed — so turning secretary back on later still handles
        # their backlog normally (daktari: never lose a backlog to the
        # dashboard's dedupe).
        if not is_enabled():
            recent = [it for it in recent
                      if self._is_self_chat(it.get("sender", ""))]
        # Batch the processed-check: _is_processed rebuilds the whole
        # processed-set per call, so a sweep of N chats would build it N
        # times. One batched check = one state load + one set build for the
        # whole sweep (YinYang: flat I/O on the background poll path).
        candidates = []
        for it in recent:
            fp = _fingerprint(it.get("sender", ""), it.get("preview", ""))
            if fp in self._seen:
                continue
            candidates.append((fp, it.get("sender", ""),
                               it.get("preview", ""), bool(it.get("call"))))
        already = _is_processed_many([fp for fp, _, _, _ in candidates]) \
            if candidates else set()
        fresh = [c for c in candidates if c[0] not in already]
        for fp, _, _, _ in fresh:
            self._seen.add(fp)
        self._prune_seen()
        from actions.secretary import _media_kind_of
        for fp, sender, preview, is_call in fresh:
            try:
                if is_call:
                    self._handle_call(sender, preview)
                elif _media_kind_of(preview) == "skip":
                    # Reaction / recall / system notice — not a message to
                    # answer or even log (including in the self-chat, where
                    # "Reacted to ..." is not a remote command).
                    continue
                elif self._is_self_chat(sender):
                    self._handle_self_chat(sender, preview)
                elif _is_meta_ai_chat(sender):
                    # Meta AI is the assistant WE message (meta_ai tool /
                    # brain fallback) — its replies are answers to our own
                    # questions, not a third party to triage. Auto-replying
                    # would make the secretary argue with the AI forever.
                    continue
                else:
                    self._on_message(sender, preview)
            except Exception as e:
                # Mark processed even when sending failed so a stuck message
                # isn't retried every poll (it already landed in the
                # conversation log; the boss can still reply manually).
                self._last_error = f"handling {sender}: {e}"
        if fresh:
            try:
                _mark_processed_many([fp for fp, _, _, _ in fresh])
            except Exception:
                pass

    def _prune_seen(self) -> None:
        """Cap the in-process dedupe cache. `_seen` only speeds up re-checks
        within one process (the persisted fingerprints are the real guard), so
        once it grows past 1000 entries it's trimmed to the newest 500 — an
        unbounded set would otherwise grow forever while monitoring runs 24/7
        (YinYang: bound in-memory growth)."""
        if len(self._seen) > 1000:
            self._seen = set(list(self._seen)[-500:])

    def _handle_call(self, sender: str, preview: str) -> None:
        """A missed call row ("Missed voice call" / "Missed video call")
        surfaced through the poll — escalate it to the boss's inbox so it is
        never auto-replied to like a normal text."""
        from actions.secretary import _escalate_call
        kind = "video" if "video" in (preview or "").lower() else "audio"
        _escalate_call(sender, kind)

    def _is_self_chat(self, sender: str) -> bool:
        """True when this chat is the boss's own self-chat (texting
        themselves from another number) — configured via secretary_self_chat
        in config/api_keys.json (e.g. "Omoke Jr"). These messages are remote
        commands to Jeeves, not third-party chats to triage."""
        s = (sender or "").strip().lower()
        return bool(s) and any(
            s == t.lower() or s in t.lower()
            for t in self._self_chat_titles if t
        )

    def _handle_self_chat(self, sender: str, text: str) -> None:
        """A message from the boss's own chat: treat it as a command to the
        full Jeeves CLI brain (shortcuts first, then the LLM + tools), and
        send the reply back into the same chat — a remote dashboard via
        WhatsApp. When no brain handler is wired (non-daemon context) the
        message is acknowledged briefly instead."""
        if not self._on_self_chat:
            print(f"[SecretaryListener] self-chat message from {sender} "
                  f"with no brain handler wired — ignoring: {text[:80]}")
            return
        reply = self._on_self_chat(sender, text)
        if not reply or not str(reply).strip():
            return
        try:
            if self._bridge is not None:
                self._bridge.send_message(sender, str(reply).strip())
            else:
                from actions.send_message import send_message
                send_message({"receiver": sender,
                              "message_text": str(reply).strip(),
                              "platform": "whatsapp"}, player=None)
        except Exception as e:
            self._last_error = f"self-chat reply to {sender}: {e}"
            print(f"[SecretaryListener] could not reply to self-chat: {e}")

    def _handle_ringing_calls(self, bridge) -> None:
        """Escalate a call ringing right now (audio or video). Deduped so the
        same ring isn't escalated every poll while it's still ringing."""
        # YinYang: bound the dedupe map — an entry older than the 5-minute
        # dedupe window can never suppress a new escalation again, so it's
        # dead weight (the map would otherwise grow without bound on a 24/7
        # monitor that sees many different callers).
        now = time.time()
        for k in [k for k, ts in self._ring_escalated.items()
                  if now - ts > 300]:
            del self._ring_escalated[k]
        try:
            calls = bridge.poll_calls()
        except Exception as e:
            self._last_error = f"poll_calls: {e}"
            return
        if not calls:
            return
        from actions.secretary import _escalate_call
        for c in calls:
            sender = str(c.get("from") or "").strip()
            kind = "video" if str(c.get("kind") or "") == "video" else "audio"
            if not sender:
                continue
            key = f"ring|{sender.lower()}|{kind}"
            # remember when this ring was last escalated; skip repeats for 5
            # minutes so a call that rings for a while isn't re-escalated
            if self._ring_escalated.get(key, 0) > now - 300:
                continue
            self._ring_escalated[key] = now
            _escalate_call(sender, kind, ringing=True)

    # ── one new message (single-shot helper, kept for tests/compat) ──

    def _handle_new(self, sender: str, preview: str) -> None:
        """Process one unread message exactly once (per-process + persisted)."""
        from actions.secretary import _is_processed, _mark_processed
        if _is_meta_ai_chat(sender):
            return
        fp = _fingerprint(sender, preview)
        if fp in self._seen or _is_processed(fp):
            return
        self._seen.add(fp)
        self._prune_seen()
        try:
            self._on_message(sender, preview)
        except Exception as e:
            self._last_error = f"handling {sender}: {e}"
        finally:
            try:
                _mark_processed(fp)
            except Exception:
                pass


    def _maybe_scan_pet_names(self, bridge) -> None:
        """Build the static {sender: pet name} map once per day, in a
        background thread so the sweep is never delayed by the scan. Gated:
        secretary on (pet names only matter for third-party auto-replies),
        the feature enabled, and not scanned in the last 24h — the map is
        static, so nothing here ever runs per-draft."""
        if self._pet_scan_started:
            return
        self._pet_scan_started = True
        try:
            from actions.secretary import (_pet_names_scan_needed,
                                           is_enabled, scan_pet_names)
            if not is_enabled() or not _pet_names_scan_needed():
                return
        except Exception:
            return

        def _run():
            try:
                print("[SecretaryListener] scanning chats for pet names "
                      "(what contacts call the boss)…", flush=True)
                summary = scan_pet_names(bridge=self._bridge)
                print(f"[SecretaryListener] {summary}", flush=True)
            except Exception as e:
                print(f"[SecretaryListener] pet-name scan failed: {e}",
                      flush=True)

        threading.Thread(target=_run, daemon=True,
                         name="PetNameScan").start()


def _is_meta_ai_chat(sender: str) -> bool:
    """True when this chat is Meta AI (WhatsApp's built-in assistant).
    Jeeves messages it on purpose (meta_ai tool / brain fallback), so its
    replies must never be triaged or auto-replied to — that would make the
    secretary argue with the AI in a loop."""
    s = (sender or "").strip().lower()
    return s == "meta ai" or s.startswith("meta ai")


# ── Recency filter (tend only messages from the past 24h) ──────────────────
# WhatsApp Web row times are: 'HH:MM' (today → within 24h), 'Yesterday', a
# weekday name, or a date. Only today's HH:MM counts as recent; anything a
# day or more old is NOT tended, so the secretary never answers a week-old
# unread. Empty/unknown times default to recent — a parsing quirk can never
# silently drop a real message.
_RECENT_TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")


def _is_recent(time_text: str | None) -> bool:
    """True when the chat's last message is from the past ~24h."""
    t = (time_text or "").strip()
    if not t:
        return True
    return bool(_RECENT_TIME_RE.match(t))


def _recency_key(time_text: str | None) -> int:
    """Sort key for the sweep: today's HH:MM → minutes since midnight (higher
    = newer); unknown times sort as newest so they're handled first."""
    t = (time_text or "").strip()
    m = _RECENT_TIME_RE.match(t)
    if m:
        hh, mm = (int(x) for x in t.split(":"))
        return hh * 60 + mm
    return 10**9


# ── Config helpers ───────────────────────────────────────────────────────────

def _read_cfg() -> dict:
    try:
        return json.loads(_CFG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _poll_seconds_from_config() -> float:
    try:
        return float(_read_cfg().get("secretary_poll_seconds",
                                     DEFAULT_POLL_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_POLL_SECONDS


def _headless_from_config() -> bool:
    """Headless by default (truly background). Set secretary_headless: false
    in config to get a visible browser window (handy while QR-linking).
    None/null in config means "unset" → default headless (bool(None) would
    be False and wrongly pop a visible window every time)."""
    h = _read_cfg().get("secretary_headless", True)
    return True if h is None else bool(h)


def _cdp_url_from_config() -> str | None:
    """When the user's own Chrome runs with --remote-debugging-port=<port>,
    set secretary_cdp_url: "http://127.0.0.1:<port>" and the monitor attaches
    to their already-logged-in WhatsApp Web session — no QR link needed."""
    url = str(_read_cfg().get("secretary_cdp_url", "") or "").strip()
    return url or None


def _self_chat_titles() -> list[str]:
    """Chat titles that are the boss's OWN self-chat (texting themselves
    from another number), configured via secretary_self_chat in
    config/api_keys.json — a string or a list, e.g. "Omoke Jr". Messages
    from these chats are treated as remote commands to Jeeves, never
    triaged like a third party."""
    v = _read_cfg().get("secretary_self_chat")
    if isinstance(v, str):
        return [t.strip() for t in v.split(",") if t.strip()]
    if isinstance(v, list):
        return [str(t).strip() for t in v if str(t).strip()]
    return []


# ── Module-level manager (one listener per process) ─────────────────────────

_manager_lock = threading.RLock()  # RLock: link_whatsapp() → start_monitor()
_listener: SecretaryListener | None = None
_monitor_bridge = None  # the shared bridge this listener holds a ref on


def _needs_visible_link_window() -> bool:
    """True when the dedicated profile has never been linked and no CDP
    attach is configured. First run opens a REAL window so the boss can scan
    the QR on screen instead of hunting for qr_login.png; the marker is
    written once linked, so this only happens once ever."""
    if _cdp_url_from_config():
        return False
    return not is_profile_linked()


def start_monitor(on_self_chat=None) -> str:
    """Start (or keep) the background WhatsApp monitor; returns a status line.

    First-ever run (profile not linked yet) opens a visible window with the
    QR so linking happens once, on screen; that same window then stays as the
    shared bridge, so every later `secretary on` reuses it — never a new
    window. Once linked, later runs go headless per config.

    on_self_chat(sender, text) → reply: wired by the daemon to the full Jeeves
    brain so messages from the boss's own chat (secretary_self_chat) run as
    remote CLI commands and the reply is sent back into WhatsApp."""
    global _listener, _monitor_bridge
    with _manager_lock:
        if _listener is not None and _listener.is_running():
            return "already monitoring WhatsApp in the background"
        first_link = _needs_visible_link_window()
        headless = bool(_headless_from_config()) and not first_link
        bridge = None
        try:
            bridge, _ = acquire_shared_bridge(
                headless=headless, cdp_url=_cdp_url_from_config())
            _monitor_bridge = bridge
        except Exception as e:
            bridge = None  # _loop will report the exact failure in status
            _monitor_bridge = None
            print(f"[SecretaryListener] bridge unavailable: {e}")
        _listener = SecretaryListener(
            bridge=bridge, poll_seconds=_poll_seconds_from_config(),
            on_self_chat=on_self_chat)
        _listener.start()
    if bridge is None:
        return ("now monitoring WhatsApp in the background — but the browser "
                "could not start (see 'secretary status')")
    if first_link:
        return ("now monitoring WhatsApp in the background — a window just "
                "opened showing the WhatsApp QR code. Scan it once with your "
                "phone and you're connected permanently: the session is saved "
                "and the same window is reused every time (no re-login).")
    mode = "attached to your browser" if _cdp_url_from_config() else \
        ("visible window" if getattr(bridge, "visible", False) else "headless")
    return f"now monitoring WhatsApp in the background ({mode} — see 'secretary status')"


def stop_monitor() -> str:
    """Stop the background monitor; returns a status line. The visible link
    window (if one is open) stays up — it keeps its own reference."""
    global _listener, _monitor_bridge
    with _manager_lock:
        if _listener is not None:
            _listener.stop()
            _listener = None
            if _monitor_bridge is not None:
                release_shared_bridge(_monitor_bridge)  # no-op if released
                _monitor_bridge = None
            return "WhatsApp monitoring stopped"
    return "WhatsApp monitoring was not running"


def link_whatsapp() -> str:
    """Open the dedicated background browser window (visible) on WhatsApp
    Web's login page so the boss can link the account once — scanning on
    screen, not a saved PNG. The session is saved in the persistent profile
    (.whatsapp_profile), and every later monitor/send reuses the SAME window,
    so linking once means staying connected forever.

    When secretary mode is on, monitoring resumes on the same window."""
    global _listener, _monitor_bridge
    if _cdp_url_from_config():
        return ("You're using your own browser (secretary_cdp_url) — no "
                "separate window is needed; WhatsApp already runs in your "
                "logged-in browser tab.")
    with _manager_lock:
        # No second browser may hold the profile while the visible one opens
        # (Playwright locks the profile dir), so stop any running monitor.
        if _listener is not None and _listener.is_running():
            _listener.stop()
            _listener = None
        if _monitor_bridge is not None:
            release_shared_bridge(_monitor_bridge)
            _monitor_bridge = None
        stop_all_bridges()          # drop straggler refs (e.g. a send)
        # If a link window is already open this returns it — same window.
        bridge, _ = acquire_shared_bridge(headless=False, cdp_url=None)
        try:
            bridge.start()
        except Exception as e:
            release_shared_bridge(bridge)
            return f"Could not open the WhatsApp window: {e}"
        linked = bool(bridge.is_logged_in())
        if linked:
            msg = ("The WhatsApp window is open and already linked — the "
                   "session is saved, so you'll never need to scan again.")
        else:
            msg = ("The WhatsApp window is now open showing the QR code — "
                   "scan it once with your phone (WhatsApp > Linked "
                   "devices). The session is saved permanently and the same "
                   "window is reused from now on.")
        try:
            from actions.secretary import is_enabled
            if is_enabled():
                start_monitor()      # re-acquires a ref on the same window
                msg += " Secretary monitoring is running on the same window."
        except Exception as e:
            msg += f" (monitor not resumed: {e})"
        return msg


def close_link_window() -> str:
    """Close the visible WhatsApp link window (and any monitor sharing its
    browser). Secretary mode itself stays as configured."""
    global _listener, _monitor_bridge
    with _manager_lock:
        if _listener is not None and _listener.is_running():
            _listener.stop()
            _listener = None
        if _monitor_bridge is not None:
            release_shared_bridge(_monitor_bridge)
            _monitor_bridge = None
        stop_all_bridges()
    return "WhatsApp link window closed."


def is_monitoring() -> bool:
    """True while the background monitor is alive (daemon keepalive uses this)."""
    with _manager_lock:
        return _listener is not None and _listener.is_running()


def monitor_status() -> str:
    """Human-readable monitor state for `secretary status`."""
    with _manager_lock:
        if _listener is None or not _listener.is_running():
            return "not running"
        state, qr, last, err = (_listener._state, _listener._qr_path,
                                _listener._last_poll_at, _listener._last_error)
        bridge = getattr(_listener, "_bridge", None)
    mode = "headless"
    if bridge is not None:
        if getattr(bridge, "mode", "") == "cdp":
            mode = "attached to your browser"
        elif getattr(bridge, "visible", False):
            mode = "visible window"
    if state == "qr":
        where = ("the QR code shown in the WhatsApp window"
                 if mode == "visible window" else f"{qr or 'qr_login.png'}")
        bits = [f"waiting for the one-time WhatsApp link — scan "
                f"{where} with your phone (WhatsApp > Linked devices)"]
    elif state == "starting":
        bits = ["starting WhatsApp Web..."]
    elif state == "error":
        bits = [f"not running — {err or 'unknown error'}"]
    else:
        bits = [f"WhatsApp Web connected ({mode}) — "
                f"monitoring in the background"]
    if last:
        bits.append(f"last scan {datetime.fromtimestamp(last).strftime('%H:%M:%S')}")
    if err and state != "error":
        bits.append(f"last error: {err}")
    return "running — " + ", ".join(bits)
