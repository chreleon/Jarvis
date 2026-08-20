# actions/whatsapp_bridge.py
# WhatsAppBridge — background WhatsApp Web automation via Playwright.
#
# Two ways to get a WhatsApp Web session:
#
#   • CDP attach (preferred when the user is already logged in): connect to
#     the user's own Chrome/Edge started with --remote-debugging-port and
#     drive their existing web.whatsapp.com tab. No QR link, no separate
#     browser, uses their real session. The bridge never closes their
#     browser — only tabs it created itself.
#
#   • Dedicated headless browser (fallback): own persistent profile, one-time
#     QR link (captured to qr_login.png at 3x so it scans reliably).
#
# Either way the chat list is read straight from the page's DOM, so nothing
# has to be on screen, focused, or unlocked. Group chats are read but never
# handed to the triage engine (the boss asked: monitor everything, never
# reply to groups).
#
# Threading: the sync Playwright API is not thread-safe, so ALL bridge calls
# (poll, send) must not interleave. SecretaryListener runs its poll → triage
# → reply cycle on one monitor thread, but one-shot sends (send_message tool)
# arrive on OTHER threads — so every public operation here is serialized by
# a per-bridge RLock. A whole operation (start / poll / send / stop) is
# atomic; nothing interleaves inside Playwright.
#
# One bridge per process: the secretary monitor and one-shot sends share a
# single WhatsAppBridge through acquire_shared_bridge()/release_shared_bridge()
# below — Playwright refuses to open the same profile directory twice, so a
# second browser would crash. The registry is refcounted: the monitor holds a
# reference for its lifetime, sends take a reference per call, and the browser
# exits when the last reference is released.

from __future__ import annotations

import queue
import re
import threading
import time
from pathlib import Path

BASE_DIR    = Path(__file__).resolve().parent.parent
PROFILE_DIR = BASE_DIR / ".whatsapp_profile"
QR_PATH     = BASE_DIR / "qr_login.png"

# Marker written inside the persistent profile once it has been linked to a
# phone. The login itself lives in PROFILE_DIR (Playwright's user_data_dir
# persists cookies/IndexedDB), so scanning once really is enough — this flag
# only lets us remember that linking happened, so first-run can show a real
# window with the QR on screen instead of a saved PNG.
_LINKED_FLAG = PROFILE_DIR / ".linked"


def is_profile_linked() -> bool:
    """True once the dedicated profile has been linked to a phone at least
    once. The actual session is stored in PROFILE_DIR; this is just the
    marker that tells callers whether a one-time link is still needed."""
    return _LINKED_FLAG.exists()


def mark_profile_linked() -> bool:
    """Persist the linked marker (idempotent). Returns True when written."""
    try:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        _LINKED_FLAG.write_text("linked\n", encoding="utf-8")
        return True
    except Exception:
        return False

_WHATSAPP_URL = "https://web.whatsapp.com/"

# Chat list is present when logged in; the QR canvas appears when not linked.
_CHAT_LIST = 'div#pane-side, [data-testid="chat-list"]'

# The search box: currently an <input aria-label="Search or start a new chat">
# (data-tab=3); older builds used a contenteditable div, and yet older ones
# put the testid on the element or a wrapper — cover all of them.
_SEARCH_INPUT = (
    'input[aria-label="Search or start a new chat"], '
    'input[data-tab="3"], '
    '[data-testid="chat-list-search"] input, '
    '[data-testid="chat-list-search-container"] input, '
    'div[contenteditable="true"][data-tab="3"]'
)

# Search results render INLINE in the chat list (same #pane-side rows as the
# normal list) — the old [data-testid="chat-list-search-result"] wrapper is
# gone. Section headers ("Chats"/"Contacts") are also rows but contribute no
# title, so they never match a receiver.
_CHAT_ROWS = '#pane-side div[role="button"], #pane-side [role="row"]'

# Titles of every chat row in DOM order ('' for section headers).
_ROW_TITLES_JS = """
() => Array.from(document.querySelectorAll('#pane-side div[role="button"], #pane-side [role="row"]')).map(r => {
  const t = r.querySelector('[data-testid="conversation-title"]')
    || r.querySelector('span[title]');
  // daktari: the old fallback '[title]' was too broad — it matched ANY
  // element with a title attribute, including message-preview tooltips
  // ("with exactly ..."), turning random text into fake sender names.
  // The span[title] fallback is tight enough: WhatsApp's chat titles are
  // always in a span[title] when the testid is absent.
  if (!t) return '';
  const raw = (t.getAttribute('title') || t.textContent || '').trim();
  // daktari: reject titles that look like sentence fragments (>40 chars,
  // contain multiple spaces, end with punctuation typical of a preview,
  // or start with a preposition like "with", "the", "and" — real
  // contact names never start with these).
  if (raw.length > 40 || /\s{3,}/.test(raw) || /[.!?]\s*$/.test(raw)) return '';
  const low = raw.toLowerCase();
  const preps = ['with ','the ','a ','an ','my ','your ','and ','but ','or ','in ','on ','at ','to ','for ','from ','by '];
  if (preps.some(p => low.startsWith(p))) return '';
  return raw;
})
"""
_QR_CANVAS = ('canvas[aria-label="Scan this QR code to link a device!"], '
              'canvas[aria-label="Scan me!"], [data-testid="qrcode"], canvas')

# Locates the QR canvas's CSS bounding box, used to crop the full-page shot.
_QR_BBOX_JS = r"""
() => {
  const c = document.querySelector('canvas[aria-label="Scan this QR code to link a device!"]')
    || document.querySelector('canvas[aria-label="Scan me!"]')
    || document.querySelector('[data-testid="qrcode"]');
  if (!c) return null;
  const r = c.getBoundingClientRect();
  return { x: r.x, y: r.y, w: r.width, h: r.height };
}
"""

# Reads every chat row, keeps only rows with an unread badge, and returns
# [{title, preview, unread, group, time}]. Defensive by design: tries both the
# modern row-role shape and the older button-role shape, dedupes by title, and
# extracts the badge count / preview with several selector fallbacks, so
# WhatsApp Web UI drift degrades to "nothing new this poll" instead of a
# crash. `time` is the row's display timestamp ('HH:MM' today, 'Yesterday',
# weekday or date) — the listener uses it to only tend messages from the
# past 24h. Group detection covers BOTH the legacy markers and the current
# DOM (verified Aug 2026): group rows carry [data-testid="chat-msg-symbol"]
# or a megaphone svg, or stack 2+ avatar images — the old
# [data-testid="avatar"] element no longer exists, so that heuristic alone
# silently missed groups. Groups are filtered out by normalize_unread().
_READ_UNREAD_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  const pick = (els) => {
    for (const row of els) {
      const titleEl = row.querySelector('[data-testid="conversation-title"]')
        || row.querySelector('[data-testid="conversation-info-header"]')
        || row.querySelector('[title]');
      const title = titleEl
        ? (titleEl.getAttribute('title') || titleEl.textContent || '').trim()
        : '';
      if (!title) continue;
      const key = title.toLowerCase();
      if (seen.has(key)) continue;
      const badge = row.querySelector(
        '[data-testid="icon-unread-count"], [data-testid="unread-count"]');
      const badgeText = badge
        ? (badge.textContent || badge.getAttribute('data-count') || '').trim()
        : '';
      const unread = parseInt(badgeText, 10) || 0;
      if (unread <= 0) continue;
      // Preview: current build uses cell-frame-secondary / last-msg-status
      // (conversation-last-message is gone). last-msg-status carries the
      // clean text; cell-frame-secondary appends the badge count ("Hi3"),
      // so only use it as a last resort and strip trailing digits.
      const previewEl = row.querySelector(
        '[data-testid="last-msg-status"], ' +
        '[data-testid="conversation-last-message"], .copyable-text');
      let preview = previewEl ? (previewEl.textContent || '').trim() : '';
      if (!preview) {
        const sec = row.querySelector('[data-testid="cell-frame-secondary"]');
        if (sec) preview = (sec.textContent || '').trim().replace(/\d+$/, '');
      }
      const timeEl = row.querySelector(
        '[data-testid="cell-frame-primary-detail"] span, ' +
        '[data-testid="conversation-time"]');
      const time = timeEl ? (timeEl.textContent || '').trim() : '';
      const svgTitles = Array.from(row.querySelectorAll('svg title'))
        .map(t => t.textContent || '');
      // Group rows stack 2+ member avatars. INDIVIDUAL rows with emoji in
      // the name also render 2 imgs (photo + a tiny emoji gif, class
      // contains 'emoji') — so emoji imgs are excluded from the count, or
      // every emoji-named contact would be misread as a group and silently
      // never answered (caught live: Esther 😜, Hąţąƙę 🙂↕️). Photo-less
      // groups (single initial avatar) still pass here — the send-time
      // header guard catches those.
      const avatars = Array.from(row.querySelectorAll('img'))
        .filter(i => !/\bemoji\b/.test(i.className || ''));
      const group = !!(
        row.querySelector('[data-testid="avatar-group"], [data-icon="group"], ' +
          '[data-testid="conversation-info-group"], ' +
          '[data-testid="chat-msg-symbol"]')
        || avatars.length > 1
        || svgTitles.some(t => /group|campaign/i.test(t))
      );
      seen.add(key);
      out.push({ title: title, preview: preview, unread: unread,
                 group: group, time: time });
    }
  };
  pick(document.querySelectorAll('#pane-side div[role="button"]'));
  pick(document.querySelectorAll('#pane-side [role="row"]'));
  return out;
}
"""


# Reads the state of the LAST incoming message in the open chat — used to
# wait for Meta AI's reply (verified live Aug 2026): while Meta AI
# generates, its incoming bubble (data-testid="tail-in") shows "Thinking";
# when done, the real answer is in .copyable-text.selectable-text (the text
# is split across child spans, so textContent joins it). An "imagine" image
# reply has no copyable text but mounts an image element. Returns
# {found, thinking, text, hasImage}.
_META_READ_REPLY_JS = r"""
() => {
  const scope = document.querySelector('[data-testid="conversation-panel-messages"]')
    || document.querySelector('#main');
  if (!scope) return { found: false };
  const rows = Array.from(scope.querySelectorAll('[data-id]'));
  const incoming = rows.filter(r => r.querySelector('[data-testid="tail-in"]'));
  const last = incoming.length ? incoming[incoming.length - 1] : null;
  if (!last) return { found: false };
  const textEl = last.querySelector('.copyable-text.selectable-text');
  const text = textEl ? (textEl.textContent || '').trim() : '';
  return {
    found: true,
    thinking: /^\s*thinking\s*$/i.test(text),
    text: text,
    hasImage: !!last.querySelector('[data-testid="image-message"], img[src^="blob:"], img[src^="data:"]'),
  };
}
"""


# Detects an incoming call ringing right now. Verified against the current
# WhatsApp Web bundle (Aug 2026): while a call rings the page shows
# [data-testid="voip-accept-call-button"] with the caller in
# [data-testid="voip-call-participant-info-name"]; a video call also mounts
# [data-testid="voip-container-video-call"]. The accept-button icon names
# (WDSIconIcVideocamFilled vs WDSIconIcCallFilled) are the audio/video tie-
# breaker when the container isn't mounted yet.
_READ_CALL_JS = r"""
() => {
  const accept = document.querySelector('[data-testid="voip-accept-call-button"]');
  if (!accept) return [];
  const nameEl = document.querySelector('[data-testid="voip-call-participant-info-name"]');
  const from = nameEl ? (nameEl.textContent || '').trim() : '';
  const video = !!document.querySelector('[data-testid="voip-container-video-call"]')
    || /videocam/i.test(accept.outerHTML || '');
  return [{ from: from, kind: video ? 'video' : 'audio' }];
}
"""


_CALL_PREVIEW_RE = re.compile(
    r"(missed\s+)?(voice|video)\s+call|group\s+call", re.IGNORECASE)


def normalize_unread(data) -> list[dict]:
    """Turn the JS result into [{sender, preview, time, call}] — groups
    dropped, so the triage engine never sees (or replies to) group chats.
    Pure + testable.

    Rows without a visible text preview (images, voice notes, reactions)
    become "(new message)" so the triage engine still sees an incoming
    message instead of silently dropping it. Rows whose preview is a call
    log line ("Missed voice call", "Missed video call", "Voice call") are
    flagged call=True so the listener can escalate them to the boss instead
    of auto-replying to a phone call.
    """
    out: list[dict] = []
    for d in data or []:
        if not isinstance(d, dict):
            continue
        if d.get("group"):
            continue  # the boss asked: never reply to groups
        title = str(d.get("title") or "").strip()
        if not title:
            continue
        # daktari: reject titles that look like message-preview fragments
        # ("with exactly ...", "I want to send you money on...") — the
        # old [title] selector grabbed tooltip text as the chat name.
        if len(title) > 40 or re.search(r"\s{3,}", title) or \
                re.search(r"[.!?]\s*$", title):
            continue
        # Also reject titles starting with common prepositions/conjunctions
        # — real contact names never start with "with", "the", "and", etc.
        _PREPOSITIONS = (
            "with ", "the ", "a ", "an ", "my ", "your ", "our ",
            "and ", "but ", "or ", "in ", "on ", "at ", "to ",
            "for ", "from ", "by ", "of ", "if ", "so ", "no ",
            "is ", "it ", "he ", "she ", "we ", "they ",
        )
        low_t = title.lower()
        if any(low_t.startswith(p) for p in _PREPOSITIONS):
            continue
        preview = str(d.get("preview") or "").strip() or "(new message)"
        row = {"sender": title, "preview": preview,
               "time": str(d.get("time") or "").strip()}
        if _CALL_PREVIEW_RE.search(preview):
            row["call"] = True   # a missed call, not a text to auto-reply
        out.append(row)
    return out


class WhatsAppBridge:
    """One Playwright session driving WhatsApp Web in the background."""

    def __init__(self, headless: bool = True, cdp_url: str | None = None):
        self._headless = bool(headless)
        self.visible = not self._headless  # True → a real window is on screen
        self._cdp_url = cdp_url        # "http://127.0.0.1:9222" → user's Chrome
        self.mode = "cdp" if cdp_url else "headless"
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._owns_browser = True      # False when attached to the user's Chrome
        self._created_pages: list = []  # tabs we opened (close only these)
        self._ready = False
        self._linked_marked = False    # wrote the .linked marker already
        self._qr_captured_at: float | None = None
        self._last_error: str | None = None
        self._lock = threading.RLock()  # guards the state fields above
        # ── Single-threaded Playwright access ──────────────────────────────
        # Playwright's sync API is THREAD-AFFINE: the greenlet the driver's
        # event loop switches back into belongs to whichever thread started
        # the driver, so calling page.evaluate()/locator()/... from any other
        # thread raises "greenlet.error: Cannot switch to a different thread".
        # The secretary monitor polls on its own thread while one-shot sends
        # (send_message) arrive on other threads — so ALL Playwright work runs
        # on ONE dedicated worker thread that owns the driver; every public
        # method submits a job to it and waits for the result.
        self._job_q: "queue.Queue" = queue.Queue()
        self._worker: threading.Thread | None = None

    # ── the single Playwright I/O thread ─────────────────────────────────────

    def _ensure_worker(self) -> None:
        """Start the bridge's Playwright worker thread (lazy, once)."""
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(
                target=self._worker_loop, daemon=True,
                name="WhatsAppBridgeIO")
            self._worker.start()

    def _worker_loop(self) -> None:
        """Run submitted jobs one at a time; the driver, context and page are
        only ever touched from here, so the sync API never crosses threads."""
        while True:
            job = self._job_q.get()
            if job is None:
                break
            fn, args, result_q = job
            try:
                result_q.put(("ok", fn(*args)))
            except BaseException as e:  # noqa: BLE001 — must always answer
                result_q.put(("err", e))

    def _submit(self, fn, *args, timeout: float = 90.0):
        """Run fn on the bridge's single Playwright thread and return its
        result; re-raise any exception on the caller's thread. Public methods
        delegate to a private _*_impl that runs here.

        `timeout` bounds how long the caller waits: a wedged browser (hung
        Playwright call) must surface as an error, never hang the CLI, the
        daemon, or the monitor forever (daktari). The stuck worker thread is
        abandoned and the listener rebuilds the whole bridge on repeated
        failures."""
        self._ensure_worker()
        result_q: "queue.Queue" = queue.Queue()
        self._job_q.put((fn, args, result_q))
        try:
            status, payload = result_q.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(
                f"WhatsApp bridge call '{getattr(fn, '__name__', fn)}' did not "
                f"respond within {timeout:.0f}s — the background browser may "
                f"be wedged (the monitor will rebuild it)")
        if status == "err":
            raise payload
        return payload

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Connect the session. With cdp_url set this attaches to the user's
        own Chrome (their logged-in WhatsApp); otherwise a dedicated headless
        browser is launched (first run shows a QR). Idempotent — a running
        session is left untouched (the monitor and one-shot sends may both
        call it). Runs on the bridge's single Playwright thread."""
        self._submit(self._start_impl)

    def _start_impl(self) -> None:
        with self._lock:
            if self._ready:
                return
            if self._cdp_url:
                self._connect_cdp(self._cdp_url)
                return
            self._launch_dedicated()

    def _launch_dedicated(self) -> None:
        from playwright.sync_api import sync_playwright

        if self._pw is None:  # reuse a live driver on relaunch (window closed)
            self._pw = sync_playwright().start()
        # daktari: a stale lock from a crashed/killed Chrome blocks the next
        # launch.  Clean + retry with backoff so the OS has time to release
        # file handles (Windows holds them longer than Linux after kill).
        last_err = None
        for attempt in range(4):
            self._clean_stale_lock()
            try:
                self._context = self._launch_context()
                break
            except Exception as e:
                last_err = e
                if attempt < 3:
                    time.sleep(2 * (attempt + 1))  # 2s, 4s, 6s backoff
        else:
            raise last_err  # type: ignore[misc]
        self._page = self._context.pages[0] if self._context.pages \
            else self._context.new_page()
        self._page.goto(_WHATSAPP_URL, wait_until="domcontentloaded",
                        timeout=30000)
        self._ready = True

    @staticmethod
    def _clean_stale_lock() -> None:
        """Remove Chromium singleton lock files that survive a crash/kill.

        When Chrome is force-killed (taskkill, Task Manager, BSOD), it never
        gets to delete its SingletonLock/SingletonCookie/SingletonSocket files
        or the Default/LOCK file.  The next launch sees the lock and crashes
        with exit code 21 ("the profile is already in use").  This removes
        those stale markers so a fresh browser can claim the profile.  The
        files live at the root of user_data_dir and are safe to delete —
        Chromium recreates them on startup."""
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                (PROFILE_DIR / name).unlink(missing_ok=True)
            except Exception:
                pass
        # Default/LOCK is Chromium's internal profile lock — also left
        # behind after a force-kill.
        try:
            (PROFILE_DIR / "Default" / "LOCK").unlink(missing_ok=True)
        except Exception:
            pass

    def _launch_context(self):
        """Try the bundled Chromium first, then the user's installed Chrome
        (same engine — the dedicated profile keeps it isolated from their
        normal browsing session). device_scale_factor=3 makes the QR canvas
        big enough for phones to scan reliably."""
        base = {
            "user_data_dir": str(PROFILE_DIR),
            "device_scale_factor": 3,
            # YinYang: keep the background browser lean so it never competes
            # with the boss's own apps for RAM/GPU — no GPU pipeline, no
            # extensions, no window-occlusion tracking.
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-gpu",
                "--disable-software-rasterizer",
                "--disable-extensions",
                "--disable-features=CalculateNativeWinOcclusion",
            ],
        }
        attempts = [
            {"headless": self._headless},                       # bundled chromium
            {"headless": self._headless, "channel": "chrome"},  # installed Chrome
        ]
        last_error: Exception | None = None
        for extra in attempts:
            try:
                return self._pw.chromium.launch_persistent_context(
                    **base, **extra)
            except Exception as e:
                last_error = e
        raise RuntimeError(
            f"could not launch a browser for WhatsApp Web (install with "
            f"'python -m playwright install chromium'): {last_error}")

    def _connect_cdp(self, url: str) -> None:
        """Attach to the user's own Chrome (started with
        --remote-debugging-port) and use their logged-in WhatsApp Web
        session. The user's browser is never closed or modified beyond
        opening a WhatsApp tab if none is open."""
        from playwright.sync_api import sync_playwright

        if self._pw is None:
            self._pw = sync_playwright().start()
        browser = self._pw.chromium.connect_over_cdp(url)
        self._browser = browser
        self._owns_browser = False
        for ctx in browser.contexts:
            for pg in ctx.pages:
                if "web.whatsapp.com" in (pg.url or ""):
                    self._page = pg
                    self._context = ctx
                    self._created_pages = []
                    self._ready = True
                    return
        # No WhatsApp tab open — create one in their first context (same
        # logged-in session).
        ctx = browser.contexts[0] if browser.contexts \
            else browser.new_context()
        page = ctx.new_page()
        self._created_pages = [page]
        self._context = ctx
        self._page = page
        page.goto(_WHATSAPP_URL, wait_until="domcontentloaded",
                  timeout=30000)
        self._ready = True

    def stop(self) -> None:
        """Close the browser / detach from the user's Chrome. Runs on the
        bridge's single Playwright thread (never cross-thread). Bounded so a
        wedged browser can't hang the caller."""
        self._submit(self._stop_impl, timeout=30.0)

    def _stop_impl(self) -> None:
        with self._lock:
            self._ready = False
            if self._owns_browser and self._context is not None:
                try:
                    self._context.close()
                except Exception:
                    pass
            else:
                # attached to the user's Chrome: close only tabs we opened
                for pg in self._created_pages:
                    try:
                        pg.close()
                    except Exception:
                        pass
            try:
                if self._pw is not None:
                    self._pw.stop()
            except Exception:
                pass
            self._context = None
            self._pw = None

    def _ensure_alive(self) -> bool:
        """Make sure the browser/page is still usable; relaunch the dedicated
        profile browser or re-attach to the user's Chrome when the window or
        tab was closed (e.g. the boss closed the visible link window). Returns
        True when a usable page exists. Callers hold the bridge lock."""
        if not self._ready:
            return False
        closed = self._page is None or self._page.is_closed()
        if not closed:
            return True
        self._ready = False
        self._page = None
        try:
            if self._cdp_url:
                self._connect_cdp(self._cdp_url)
            else:
                self._launch_dedicated()
            return True
        except Exception:
            return False

    def _mark_linked(self) -> None:
        """Remember (once) that the dedicated profile is linked, so future
        runs know the one-time QR step is done."""
        if self._linked_marked or not self._owns_browser:
            return
        if mark_profile_linked():
            self._linked_marked = True

    # ── login state ──────────────────────────────────────────────────────────

    def is_logged_in(self) -> bool:
        return self._submit(self._is_logged_in_impl)

    def _is_logged_in_impl(self) -> bool:
        with self._lock:
            if not self._ensure_alive():
                return False
            try:
                ok = self._page.locator(_CHAT_LIST).count() > 0
                if ok:
                    self._mark_linked()
                return ok
            except Exception:
                return False

    def wait_logged_in(self, timeout: float = 45.0) -> bool:
        """Wait for the saved session to restore (or a QR to appear).

        A cold browser needs ~10s to load the WhatsApp SPA and restore the
        login — a single immediate is_logged_in() check wrongly reports
        "not linked" and forces the caller into the foreground fallback.
        Polls until logged in, a QR appears (genuinely not linked), or the
        timeout elapses. Runs on the bridge's single Playwright thread."""
        return self._submit(self._wait_logged_in_impl, timeout,
                            timeout=float(timeout) + 60.0)

    def _wait_logged_in_impl(self, timeout: float = 45.0) -> bool:
        deadline = time.time() + max(5.0, float(timeout))
        while time.time() < deadline:
            if self._is_logged_in_impl():
                return True
            if self._needs_qr_impl():
                return False   # genuinely needs linking, don't keep waiting
            time.sleep(1.0)
        return False

    def needs_qr(self) -> bool:
        return self._submit(self._needs_qr_impl)

    def _needs_qr_impl(self) -> bool:
        with self._lock:
            if not self._ensure_alive():
                return False
            try:
                return self._page.locator(_QR_CANVAS).count() > 0
            except Exception:
                return False

    def _ensure_logged_in_locked(self, timeout: float = 40.0) -> bool:
        """True when the saved session is restored; never raises.

        A cold browser needs ~10-20s to load the WhatsApp SPA and restore
        the login, so an immediate is_logged_in() check wrongly reports
        "not linked" right after a daemon/bridge start — forcing sends into
        the foreground fallback and breaking scans (daktari). This polls
        (bounded) until the chat list appears; it bails immediately when a
        QR is showing (genuinely not linked) or the wait elapses. Callers
        already hold self._lock (an RLock), so calling the locked impls
        below is safe."""
        deadline = time.time() + max(5.0, float(timeout))
        while True:
            if self._is_logged_in_impl():
                return True
            if self._needs_qr_impl():
                return False          # genuinely needs linking — don't wait
            if time.time() >= deadline:
                return False
            time.sleep(1.0)

    def capture_qr(self, max_attempts: int = 60) -> str | None:
        """Save a SCANNABLE WhatsApp link QR to QR_PATH.

        Runs on the bridge's single Playwright thread for the whole sampling
        loop, so a concurrent send waits until a QR is captured (or given up
        on) — same serialization as before, now without cross-thread calls.
        """
        return self._submit(self._capture_qr_locked, max_attempts)

    def _capture_qr_locked(self, max_attempts: int = 60) -> str | None:
        """Save a SCANNABLE WhatsApp link QR to QR_PATH.

        Three quirks make this non-trivial (verified against a live page):
          • the QR lives on a canvas, and a canvas-element screenshot comes
            back blank — the page is screenshot whole and cropped to the QR's
            bounding box instead;
          • the QR crossfades to a new code roughly every 40s, and a capture
            that lands mid-fade does not decode — so captures are sampled
            continuously until a QR decoder confirms one decodes (max_attempts
            ≈ one full rotation);
          • a screenshot 'clip' of the region renders differently from the
            full page, so the crop is done locally from the full-page shot.
        Returns the path, or None when no QR is showing."""
        if not self._ready or self._page is None:
            return None
        try:
            try:
                from PIL import Image as PILImage
            except Exception:
                PILImage = None
            try:
                import cv2  # QR verification (optional)
            except Exception:
                cv2 = None
            last_saved: str | None = None
            for _ in range(max_attempts):
                box = self._page.evaluate(_QR_BBOX_JS)
                if not box:
                    return None
                self._page.screenshot(path=str(QR_PATH))  # full page
                if PILImage is not None:
                    img = PILImage.open(str(QR_PATH))
                    # box is in CSS px; the screenshot is DPR-scaled
                    scale = img.width / self._page.viewport_size["width"]
                    pad = 40 * scale
                    left = max(0, int(box["x"] * scale - pad))
                    top = max(0, int(box["y"] * scale - pad))
                    right = min(img.width,
                                int((box["x"] + box["w"]) * scale + pad))
                    bottom = min(img.height,
                                 int((box["y"] + box["h"]) * scale + pad))
                    img.crop((left, top, right, bottom)).save(str(QR_PATH))
                last_saved = str(QR_PATH)
                if cv2 is None:
                    break  # can't verify — hand over the best capture
                decoded, _, _ = cv2.QRCodeDetector().detectAndDecode(
                    cv2.imread(str(QR_PATH)))
                if decoded:
                    break
            self._qr_captured_at = time.time()
            return last_saved
        except Exception as e:
            self._last_error = f"QR capture failed: {e}"
            return None

    # ── read (the poll) ──────────────────────────────────────────────────────

    def poll_unread(self) -> list[dict] | None:
        """Unread non-group chats as [{sender, preview}]; [] when logged in
        with nothing unread; None when the page isn't logged in / not ready
        yet (caller should check needs_qr() to tell 'needs link' from 'still
        loading'). Runs on the bridge's single Playwright thread."""
        return self._submit(self._poll_unread_impl)

    def _poll_unread_impl(self) -> list[dict] | None:
        with self._lock:
            if not self._ensure_alive():
                return None
            # Not logged in yet: the row-walk JS would return [] either way,
            # which must NOT be confused with "connected, nothing unread" —
            # the caller needs None to enter the QR-wait state.
            if not self._ensure_logged_in_locked():
                return None
            try:
                data = self._page.evaluate(_READ_UNREAD_JS)
                return normalize_unread(data)
            except Exception as e:
                self._last_error = str(e)
                return None

    # ── unread filter (secretary efficiency) ───────────────────────────────

    def ensure_unread_filter(self) -> bool:
        """Make the chat list show ONLY chats with unread messages.

        WhatsApp's own filter (filter-button → Unread tab) hides read chats,
        so the poll walks a small pane instead of the whole list — the
        secretary never scans every chat every poll. Idempotent: returns True
        when the filter is (or becomes) active. Runs on the bridge's single
        Playwright thread."""
        return self._submit(self._ensure_unread_filter_impl)

    def _ensure_unread_filter_impl(self) -> bool:
        """Apply the Unread filter for real.

        Live-verified Aug 2026: the filter-button's dropdown only opens on a
        real Playwright click (React ignores native el.click()), and once
        open the "Unread" tab renders in a React portal OUTSIDE the button,
        so it's found document-wide, not under the button. After selecting,
        Escape closes the dropdown so it never covers the pane."""
        with self._lock:
            if not self._ensure_alive():
                return False
            try:
                btn = self._page.locator('[data-testid="filter-button"]').first
                if btn.count() == 0:
                    return True   # no control (layout drift) — poll is badge-filtered anyway
                btn.click(timeout=4000)
                self._page.wait_for_timeout(350)   # let the portal mount
                unread = self._page.locator('[role="tab"]', has_text="Unread")
                if unread.count() == 0:
                    self._page.keyboard.press("Escape")
                    return True   # no Unread control — badge filter still guards
                if unread.get_attribute("aria-selected") != "true":
                    unread.click()
                    self._page.wait_for_timeout(350)   # pane re-renders unread-only
                self._page.keyboard.press("Escape")
                self._page.wait_for_timeout(150)
                return True
            except Exception as e:
                self._last_error = str(e)
                return False

    # ── calls (secretary monitoring) ────────────────────────────────────────

    def poll_calls(self) -> list[dict] | None:
        """Detect an incoming call (audio or video) ringing right now.

        Returns [{from, kind}] — one entry per ringing call — or [] when no
        call is active, and None when the page isn't logged in / ready yet
        (same contract as poll_unread). Verified against the current WhatsApp
        Web bundle (Aug 2026): the accept button (voip-accept-call-button)
        is present while a call rings, the participant name element carries
        the caller, and the video container / accept-button icon tells audio
        from video. Runs on the bridge's single Playwright thread."""
        return self._submit(self._poll_calls_impl)

    def _poll_calls_impl(self) -> list[dict] | None:
        with self._lock:
            if not self._ensure_alive():
                return None
            if not self._ensure_logged_in_locked():
                return None
            try:
                data = self._page.evaluate(_READ_CALL_JS)
                return [d for d in (data or []) if isinstance(d, dict)]
            except Exception as e:
                self._last_error = str(e)
                return None

    # ── write (secretary auto-replies, background) ──────────────────────────

    def send_message(self, receiver: str, text: str) -> str:
        """Search the contact, open the chat, type + send through the page's
        own inputs (Playwright trusted keyboard, so React-controlled inputs
        update). Runs in the background — no OS screen interaction. Raises a
        clear RuntimeError when the session isn't linked, so callers can fall
        back to a foreground flow. Runs on the bridge's single Playwright
        thread."""
        return self._submit(self._send_message_impl, receiver, text)

    def _send_message_impl(self, receiver: str, text: str) -> str:
        with self._lock:
            if not self._ensure_alive():
                raise RuntimeError("WhatsApp bridge is not ready")
            if not self._ensure_logged_in_locked():
                raise RuntimeError(
                    "WhatsApp Web is not linked in the background browser — "
                    "say 'link whatsapp' (or 'secretary link') once to open "
                    "the window and scan the QR with your phone; the session "
                    "is then saved and reused forever")
            return self._send_message_locked(receiver, text)

    def _open_chat_locked(self, receiver: str) -> tuple[bool, str]:
        """Search for a chat and open it, verifying the opened conversation
        matches the receiver. Returns (True, '') on success or (False,
        reason). Caller holds the bridge lock. Shared by the send path and
        the media-download path (self-chat file drop)."""
        page = self._page
        receiver = (receiver or "").strip()
        rlower = receiver.lower()

        def _row_titles() -> list[str]:
            """Titles of the (search-filtered) chat rows, in DOM order."""
            try:
                titles = page.evaluate(_ROW_TITLES_JS)
                return [str(t or "").strip() for t in (titles or [])]
            except Exception:
                return []

        # Phone-number receivers (e.g. the "Message yourself" self-chat, which
        # renders as "+254 ..." with RTL marks) match on digits only, so
        # "254112093400" still finds "‏‪+254 112 093400‬‏". Names never
        # contain digits, so this can't misfire on contacts.
        receiver_digits = re.sub(r"\D", "", receiver) if receiver else ""

        def _title_matches(title: str) -> bool:
            """True when a chat title matches the receiver: exact, starts
            with, or digit-normalized equal (phone numbers / the "Message
            yourself" self-chat render with +, spaces and RTL marks)."""
            t = title.lower()
            if t == rlower or t.startswith(rlower):
                return True
            if receiver_digits:
                return re.sub(r"\D", "", title) == receiver_digits
            return False

        def _best_row_index() -> int | None:
            """Index of the row that best matches the receiver (exact title,
            starts-with, or digit-normalized phone). None = absent."""
            titles = _row_titles()
            for i, t in enumerate(titles):
                if _title_matches(t):
                    return i
            return None

        def _wait_for_match() -> int | None:
            """Wait for the filtered list to render a row matching the
            receiver (results appear async after typing), then return its
            index — or None when no chat matches."""
            try:
                page.wait_for_function(
                    """(receiver) => {
                        const digits = (s) => (s || '').replace(/\\D/g, '');
                        const rows = Array.from(document.querySelectorAll('#pane-side div[role="button"], #pane-side [role="row"]'));
                        for (const r of rows) {
                          const t = r.querySelector('[data-testid="conversation-title"]')
                            || r.querySelector('span[title]') || r.querySelector('[title]');
                          const name = t ? (t.getAttribute('title') || t.textContent || '').trim().toLowerCase() : '';
                          if (name === receiver || name.startsWith(receiver)
                              || (digits(receiver) && digits(name) === digits(receiver))) return true;
                        }
                        return false;
                    }""",
                    arg=rlower, timeout=8000)
            except Exception:
                pass  # timed out → _best_row_index() decides (None = not found)
            return _best_row_index()

        def _search_and_open() -> bool:
            search = page.locator(_SEARCH_INPUT).first
            search.click(timeout=10000)
            try:
                page.keyboard.press("Control+A")  # clear any leftover query
            except Exception:
                pass
            page.keyboard.type(receiver, delay=35)
            idx = _wait_for_match()
            if idx is None:
                return False
            try:
                page.locator(_CHAT_ROWS).nth(idx).click(timeout=8000)
                return True
            except Exception:
                return False

        def _chat_header_title() -> str:
            try:
                header = page.locator(
                    'header [data-testid="conversation-title"], '
                    '#main header span[dir="auto"]'
                ).first
                header.wait_for(state="visible", timeout=8000)
                return (header.inner_text() or "").strip()
            except Exception:
                return ""

        if not _search_and_open():
            return (False,
                    f"could not find a chat named '{receiver}' in WhatsApp "
                    f"(check the spelling — the contact has to already be "
                    f"in your WhatsApp)")

        # Confirm we're in the right chat; retry the search once if the
        # opened conversation doesn't match the intended receiver.
        header_title = _chat_header_title()
        if header_title and not _title_matches(header_title):
            _search_and_open()
            header_title = _chat_header_title()
        if header_title and not _title_matches(header_title):
            return (False,
                    f"opened a chat but could not verify it was "
                    f"'{receiver}' (found '{header_title}')")
        return True, ""

    def _send_message_locked(self, receiver: str, text: str) -> str:
        """The actual DOM send; caller holds the bridge lock. Verified
        end-to-end so a stale page layout can't silently drop or misdirect
        the message: the search is retried when the opened chat doesn't
        match the receiver, and the send is confirmed by the compose box
        emptying (with an Enter + send-button fallback)."""
        page = self._page
        receiver = (receiver or "").strip()
        ok, err = self._open_chat_locked(receiver)
        if not ok:
            return err

        def _chat_subtitle() -> str:
            """The opened chat's subtitle ('online', 'last seen ...', or a
            group's comma-separated member list). Empty when unavailable."""
            try:
                sub = page.locator('[data-testid="chat-subtitle"]').first
                sub.wait_for(state="visible", timeout=5000)
                return (sub.inner_text() or "").strip()
            except Exception:
                return ""

        # Refuse to send to groups. The boss asked for individuals only, and
        # photo-less groups can look exactly like individual chats in the
        # chat list — but the OPENED chat's subtitle never lies: groups show
        # "click here for GROUP info" immediately and a member list a moment
        # later ("A, B, C"); individuals show "click here for contact info"
        # or status text. Retry briefly so the member list has time to render.
        subtitle = _chat_subtitle()
        for _ in range(4):
            if _subtitle_is_group(subtitle) or subtitle:
                break
            time.sleep(0.5)
            subtitle = _chat_subtitle()
        if _subtitle_is_group(subtitle):
            return (f"'{receiver}' looks like a group chat — the secretary "
                    f"only sends to individuals, so no message was sent")

        box = page.locator(
            'footer div[contenteditable="true"], '
            '[data-testid="conversation-compose-box-input"]'
        ).first
        box.click(timeout=8000)
        page.keyboard.type(text, delay=20)
        page.keyboard.press("Enter")

        # Confirm the send: after Enter the compose box should be empty. If
        # it still holds the text (layout drift), press Enter again, then
        # click the send button as the final fallback.
        def _box_emptied() -> bool:
            try:
                box.wait_for(state="visible", timeout=5000)
                return (box.inner_text() or "").strip() == ""
            except Exception:
                return False

        if not _box_emptied():
            try:
                page.keyboard.press("Enter")
            except Exception:
                pass
            if not _box_emptied():
                try:
                    page.locator('[data-testid="compose-btn-send"]').first \
                        .click(timeout=4000)
                except Exception:
                    pass
        return f"sent via WhatsApp Web ({receiver})"

    # ── Media download (self-chat file drop) ────────────────────────────────
    # The boss texts a file to their own self-chat ("Omoke Jr") from their
    # phone; this pulls it down to the PC so Jeeves can process it like
    # /attach. Verified live against the current WhatsApp Web build:
    # documents are a [data-testid="document-thumb"] whose title starts with
    # "Download ..." and trigger a real browser download when clicked;
    # photos/videos open a lightbox ([data-testid="media-viewer-modal"]) with
    # an [data-testid="ic-download"] button. Both are captured with
    # page.expect_download() and saved locally.

    def download_last_media(self, chat_title: str, save_dir: str,
                            timeout: int = 60) -> dict:
        """Download the most recent incoming media message from a chat.

        Returns {"path", "kind" ("document"|"image"|"video"), "name"}.
        Raises RuntimeError with a clear message when the session isn't
        linked, the newest message isn't downloadable media (voice notes and
        stickers aren't supported), or the download fails. Runs on the
        bridge's single Playwright thread."""
        return self._submit(self._download_last_media_impl,
                            chat_title, save_dir, timeout,
                            timeout=float(timeout) + 30.0)

    def _download_last_media_impl(self, chat_title: str, save_dir: str,
                                  timeout: int) -> dict:
        with self._lock:
            if not self._ensure_alive():
                raise RuntimeError("WhatsApp bridge is not ready")
            if not self._ensure_logged_in_locked():
                raise RuntimeError(
                    "WhatsApp Web is not linked in the background browser — "
                    "say 'link whatsapp' (or 'secretary link') once to open "
                    "the window and scan the QR with your phone; the session "
                    "is then saved and reused forever")
            return self._download_last_media_locked(chat_title, save_dir,
                                                    timeout)

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        """Make a suggested download name safe to save locally."""
        name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_",
                      (name or "")).strip(" .")
        return (name or "media")[:120]

    def _save_download(self, download, save_dir: str, kind: str) -> dict:
        """Persist a captured Playwright download into save_dir."""
        d = Path(save_dir)
        d.mkdir(parents=True, exist_ok=True)
        name = self._sanitize_filename(download.suggested_filename)
        target = d / name
        download.save_as(str(target))
        return {"path": str(target), "kind": kind, "name": name}

    def _download_last_media_locked(self, chat_title: str, save_dir: str,
                                    timeout: int) -> dict:
        """The actual DOM download; caller holds the bridge lock. Walks the
        message thread from the newest message backwards and downloads the
        first incoming (tail-in) media message it finds."""
        page = self._page
        ok, err = self._open_chat_locked(chat_title)
        if not ok:
            raise RuntimeError(err)

        containers = page.locator('#main [data-testid="msg-container"]')
        # On a cold browser the thread can take a while to render (the SPA
        # streams messages in after "Your messages are downloading") — poll
        # up to 30s instead of one wait_for.
        deadline = time.time() + 30
        while time.time() < deadline:
            if containers.count() > 0:
                break
            time.sleep(1)
        n = containers.count()
        for i in range(n - 1, -1, -1):
            c = containers.nth(i)
            if c.locator('[data-testid="tail-in"]').count() == 0:
                continue   # outgoing — not the file the boss just sent

            # Plain text messages carry no media indicators — skip them and
            # keep walking back to the newest actual media message.
            has_media = (
                c.locator('[data-testid="document-thumb"]').count()
                or c.locator('[data-testid="image-thumb"]').count()
                or c.locator('[data-testid="video-thumb"]').count()
                or c.locator('[data-testid="sticker-container"]').count()
                or c.locator('[data-testid="audio-thumb"]').count()
                or c.locator('[data-testid="ptt-thumb"]').count()
            )
            if not has_media:
                continue

            # Documents: the thumb is a "Download ..." button (or, when
            # already downloaded, opens a preview viewer with a download
            # button).
            doc = c.locator('[data-testid="document-thumb"]')
            if doc.count():
                title = (doc.first.get_attribute("title") or "").strip()
                if title.lower().startswith("download"):
                    with page.expect_download(timeout=timeout) as dl_info:
                        doc.first.click(timeout=10000)
                    return self._save_download(dl_info.value, save_dir,
                                               "document")
                doc.first.click(timeout=10000)   # opens the preview viewer
                try:
                    page.locator('[data-testid="media-viewer-modal"]') \
                        .wait_for(state="visible", timeout=10000)
                except Exception:
                    page.keyboard.press("Escape")
                    continue
                ic = page.locator('[data-testid="media-viewer-modal"] '
                                  '[data-testid="ic-download"]')
                if not ic.count():
                    page.keyboard.press("Escape")
                    continue
                with page.expect_download(timeout=timeout) as dl_info:
                    ic.first.click(timeout=10000)
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
                return self._save_download(dl_info.value, save_dir,
                                           "document")

            # Photos/videos: open the lightbox, click its download button.
            thumb = c.locator('[data-testid="image-thumb"], '
                              '[data-testid="video-thumb"]')
            if thumb.count():
                kind = ("video"
                        if c.locator('[data-testid="video-thumb"]').count()
                        else "image")
                thumb.first.click(timeout=10000)
                try:
                    page.locator('[data-testid="media-viewer-modal"]') \
                        .wait_for(state="visible", timeout=10000)
                except Exception:
                    page.keyboard.press("Escape")
                    continue
                ic = page.locator('[data-testid="media-viewer-modal"] '
                                  '[data-testid="ic-download"]')
                if not ic.count():
                    page.keyboard.press("Escape")
                    continue
                with page.expect_download(timeout=timeout) as dl_info:
                    ic.first.click(timeout=10000)
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
                return self._save_download(dl_info.value, save_dir, kind)

            # The newest incoming message is media we can't pull down.
            raise RuntimeError(
                "the newest message from you is media I can't download yet "
                "(voice notes and stickers aren't supported — send a "
                "document, photo or video)")

        raise RuntimeError(
            "no incoming media message found in that chat — send a "
            "document, photo or video first")

    # ── Forward media to Meta AI (real content analysis) ───────────────────
    # The secretary can only ever see a media message's chat-list PREVIEW
    # ("Photo", "Document", ...) — never its content. WhatsApp's native
    # forward-to-Meta-AI path solves that: hover the media message, click
    # the "Forward media" button, pick the Meta AI chat, send — Meta AI
    # then ANALYZES the actual file and replies with a real description /
    # commentary. Verified live Aug 2026: forwarding the boss's CV docx made
    # Meta AI reply "Nimeipata CV yako 💪 Unataka ni-edit nini ndani ama
    # ni-check tu?" — genuine content awareness the attach path couldn't
    # achieve. Selectors verified on the current Web build: the hover
    # overlay exposes [aria-label="Forward media"] on documents AND
    # photos/videos; the picker dialog lists the Meta AI chat with a search
    # input; sending can pop a one-time "Media and captions are now
    # forwarded together" notice with an OK button.

    def forward_last_media_to_meta_ai(self, chat_title: str,
                                      timeout: int = 120) -> str:
        """Forward the newest incoming media message from a chat to the
        in-WhatsApp Meta AI assistant and return its analysis reply text.

        Returns Meta AI's reply verbatim (e.g. a description of a photo, or
        "I got your CV ..." for a document). Runs entirely in the background
        browser through the bridge's single Playwright thread.

        Raises RuntimeError with a clear message when the session isn't
        linked, the chat doesn't exist, the newest incoming message isn't
        forwardable media (voice notes/stickers aren't supported by this
        path), the Meta AI chat isn't available (region/feature-flagged),
        or the reply never arrives within `timeout`."""
        return self._submit(self._forward_last_media_to_meta_ai_impl,
                            chat_title, int(timeout),
                            timeout=float(timeout) + 45.0)

    def _forward_last_media_to_meta_ai_impl(self, chat_title: str,
                                            timeout: int) -> str:
        with self._lock:
            if not self._ensure_alive():
                raise RuntimeError("WhatsApp bridge is not ready")
            if not self._ensure_logged_in_locked():
                raise RuntimeError(
                    "WhatsApp Web is not linked in the background browser — "
                    "say 'link whatsapp' (or 'secretary link') once to open "
                    "the window and scan the QR with your phone; the session "
                    "is then saved and reused forever")
            return self._forward_last_media_to_meta_ai_locked(chat_title,
                                                              timeout)

    def _forward_last_media_to_meta_ai_locked(self, chat_title: str,
                                              timeout: int) -> str:
        """The actual DOM flow; caller holds the bridge lock. Walks the
        thread from the newest message backwards, forwards the first
        incoming (tail-in) media message to Meta AI, then reads Meta AI's
        analysis reply from the Meta AI chat."""
        page = self._page
        ok, err = self._open_chat_locked(chat_title)
        if not ok:
            raise RuntimeError(err)

        def _walk() -> tuple:
            """Return ("media", container) for the newest incoming
            FORWARDABLE media message, or ("none", saw_unsupported) when
            nothing forwardable is rendered yet. Stickers/voice notes are
            skipped (they can't be forwarded) — a sticker on top of a photo
            must not block analyzing the photo. Cheap locator-count walk,
            safe to re-run while the thread streams in on a cold browser."""
            containers = page.locator('#main [data-testid="msg-container"]')
            n = containers.count()
            saw_unsupported = False
            for i in range(n - 1, -1, -1):
                c = containers.nth(i)
                if c.locator('[data-testid="tail-in"]').count() == 0:
                    continue          # outgoing — not media the boss received
                has_media = (
                    c.locator('[data-testid="document-thumb"]').count()
                    or c.locator('[data-testid="image-thumb"]').count()
                    or c.locator('[data-testid="video-thumb"]').count()
                    or c.locator('[data-testid="sticker-container"]').count()
                    or c.locator('[data-testid="ptt-thumb"]').count()
                )
                if not has_media:
                    continue          # plain text — keep walking back
                if (c.locator('[data-testid="sticker-container"]').count()
                        or c.locator('[data-testid="ptt-thumb"]').count()):
                    saw_unsupported = True
                    continue          # skip — can't forward these
                return ("media", c)
            return ("none", saw_unsupported)

        # On a cold browser the thread streams in over several seconds (the
        # SPA shows "Your messages are downloading" first) — poll the walk
        # so a slow render can't make us miss the media. If by the deadline
        # only stickers/voice notes were ever seen, that's the honest answer.
        target = None
        saw_unsupported = False
        deadline = time.time() + 30
        while time.time() < deadline:
            state, found = _walk()
            if state == "media":
                target = found
                break
            saw_unsupported = saw_unsupported or found
            time.sleep(1)
        if target is None:
            if saw_unsupported:
                raise RuntimeError(
                    "the newest incoming media is only stickers/voice notes, "
                    "which Meta AI can't analyze through forwarding — send a "
                    "document, photo or video")
            raise RuntimeError(
                "no incoming media message found in that chat — send a "
                "document, photo or video first")

        # 1. Hover the message to reveal the action overlay, click Forward.
        target.hover(timeout=10000)
        time.sleep(1.0)
        page.locator('[aria-label="Forward media"]').first.click(
            timeout=8000)

        # 2. Pick the Meta AI chat in the forward picker (search first so a
        #    crowded recent list can't hide it; the filtered row matches on
        #    the name exactly).
        search = page.locator('[role="dialog"] '
                              'input[aria-label*="Search"]').first
        search.click(timeout=10000)
        page.keyboard.type("Meta AI", delay=35)
        time.sleep(1.0)
        picker_rows = page.locator('[role="dialog"] [role="button"], '
                                   '[role="dialog"] [role="listitem"]')
        found = False
        for i in range(picker_rows.count()):
            try:
                txt = picker_rows.nth(i).inner_text(timeout=3000)
            except Exception:
                continue
            if "Meta AI" in txt:
                picker_rows.nth(i).click(timeout=5000)
                found = True
                break
        if not found:
            page.keyboard.press("Escape")
            raise RuntimeError(
                "the Meta AI chat isn't available on this WhatsApp account "
                "(Meta AI is region/feature-flagged) — nothing was sent")
        time.sleep(1.0)

        # 3. Send; WhatsApp can pop a one-time "Media and captions are now
        #    forwarded together" notice with an OK button that must be
        #    dismissed (and then Send clicked again) for the forward to go
        #    out.
        page.locator('[aria-label="Send"]').first.click(timeout=8000)
        time.sleep(1.5)
        notice = page.locator('[role="dialog"] button:has-text("OK")')
        if notice.count():
            try:
                notice.first.click(timeout=5000)
                time.sleep(1.5)
            except Exception:
                pass
        if page.locator('[role="dialog"]').count():
            page.locator('[aria-label="Send"]').first.click(timeout=8000)
        gone = time.time() + 20
        while time.time() < gone and page.locator('[role="dialog"]').count():
            time.sleep(0.5)
        if page.locator('[role="dialog"]').count():
            raise RuntimeError(
                "the forward picker did not close — the media was not sent")

        # 4. Open the Meta AI chat and wait for its analysis reply. Snapshot
        #    the last incoming message BEFORE waiting, so an older reply from
        #    a previous question is never mistaken for the new answer (the
        #    forward produces a brand-new incoming bubble).
        ok, err = self._open_chat_locked("Meta AI")
        if not ok:
            raise RuntimeError(
                "forwarded, but could not open the Meta AI chat to read its "
                f"reply: {err}")
        for _ in range(10):   # "Syncing older messages" can take ~5-10s
            try:
                syncing = page.evaluate(
                    "() => /syncing older messages/i.test("
                    "document.querySelector('#main')?.textContent || '')")
            except Exception:
                syncing = False
            if not syncing:
                break
            time.sleep(1.0)
        time.sleep(2.0)
        before = self._meta_last_incoming_locked()
        return self._wait_meta_reply_locked(timeout, await_new=before)

    def _meta_last_incoming_locked(self) -> tuple:
        """(data-id, text, thinking) of the last incoming message in the open
        chat — used to detect when the Meta AI reply arrives. `thinking` is
        True while the bubble shows the "Thinking" placeholder."""
        try:
            return tuple(self._page.evaluate(
                r"""() => {
                  const scope = document.querySelector('[data-testid="conversation-panel-messages"]')
                    || document.querySelector('#main');
                  if (!scope) return [null, '', false];
                  const rows = Array.from(scope.querySelectorAll('[data-id]'));
                  const incoming = rows.filter(r => r.querySelector('[data-testid="tail-in"]'));
                  const last = incoming.length ? incoming[incoming.length - 1] : null;
                  if (!last) return [null, '', false];
                  const textEl = last.querySelector('.copyable-text.selectable-text');
                  const text = textEl ? (textEl.textContent || '').trim() : '';
                  return [last.getAttribute('data-id'), text, /^\s*thinking\s*$/i.test(text)];
                }"""))
        except Exception:
            return (None, "", False)

    def _wait_meta_reply_locked(self, timeout: int,
                                await_new: tuple | None = None) -> str:
        """Poll the open chat for Meta AI's finished reply. When await_new is
        a snapshot (last-incoming id, row count), first wait for a NEW
        incoming bubble to appear so a stale previous answer can't be
        returned. Returns the reply text, or the image placeholder when Meta
        AI generated an image."""
        deadline = time.time() + timeout
        stable_text = None
        while time.time() < deadline:
            try:
                st = self._page.evaluate(_META_READ_REPLY_JS)
            except Exception:
                st = None
            if st and st.get("found"):
                if await_new is not None:
                    sid, stext, sthinking = await_new
                    cid, ctext, _ = self._meta_last_incoming_locked()
                    # Hold ONLY while the last incoming is still the SAME
                    # settled OLD reply (same id, same text, not thinking) —
                    # i.e. the new answer hasn't appeared yet. A new bubble
                    # (id change), a reply already streaming/thinking at
                    # snapshot, or changed text all mean the answer is on
                    # its way — proceed to the stability check below.
                    if (cid == sid and ctext == stext
                            and not sthinking and ctext):
                        time.sleep(2.0)
                        continue
                if st.get("hasImage") and not st.get("text"):
                    return "🖼️ Meta AI generated an image."
                if not st.get("thinking") and st.get("text"):
                    if stable_text == st["text"]:
                        return st["text"]
                    stable_text = st["text"]
            time.sleep(2.0)
        raise RuntimeError(
            f"Meta AI didn't finish replying to the media within {timeout}s "
            f"(last state: {stable_text or 'no new incoming message yet'})")

    # ── Chat introspection (pet-name scan) ─────────────────────────────────
    # The secretary replies with the name each contact uses for the boss
    # ("baby" from the wife, "bro" from a friend) instead of a generic
    # "boss". That name is discovered ONCE by scanning the existing chats
    # (not per-reply — the map is static), so the bridge exposes two
    # read-only helpers: the chat-list titles, and the recent incoming
    # message texts of one chat.

    def list_chat_titles(self, timeout: int = 30) -> list[str]:
        """Titles of the chats currently in the left pane, newest first.
        Runs on the bridge's single Playwright thread."""
        return self._submit(self._list_chat_titles_impl, timeout=timeout)

    def _list_chat_titles_impl(self) -> list[str]:
        with self._lock:
            if not self._ensure_alive():
                raise RuntimeError("WhatsApp bridge is not ready")
            if not self._ensure_logged_in_locked():
                raise RuntimeError(
                    "WhatsApp Web is not linked in the background browser — "
                    "say 'link whatsapp' (or 'secretary link') once to open "
                    "the window and scan the QR with your phone; the session "
                    "is then saved and reused forever")
            try:
                titles = self._page.evaluate(_ROW_TITLES_JS)
            except Exception:
                return []
            return [str(t or "").strip()
                    for t in (titles or []) if str(t or "").strip()]

    def read_recent_incoming(self, chat_title: str, limit: int = 25,
                             timeout: int = 60) -> list[str]:
        """The text of the most recent incoming (tail-in) messages in a
        chat, newest first — media rows (no text) are skipped. Read-only.
        Returns up to `limit` texts; raises RuntimeError when the chat can't
        be opened. Chat titles often carry emoji/glyphs ("ALIXON 🤓 🤯");
        the search matches on the normalized name, so any decorated title
        works."""
        return self._submit(self._read_recent_incoming_impl, chat_title,
                            int(limit), timeout,
                            timeout=float(timeout) + 20.0)

    def _read_recent_incoming_impl(self, chat_title: str, limit: int,
                                   timeout: int) -> list[str]:
        with self._lock:
            if not self._ensure_alive():
                raise RuntimeError("WhatsApp bridge is not ready")
            if not self._ensure_logged_in_locked():
                raise RuntimeError(
                    "WhatsApp Web is not linked in the background browser — "
                    "say 'link whatsapp' (or 'secretary link') once to open "
                    "the window and scan the QR with your phone; the session "
                    "is then saved and reused forever")
            return self._read_recent_incoming_locked(chat_title, limit,
                                                     timeout)

    @staticmethod
    def _normalize_chat_title(t: str) -> str:
        """Strip emoji/decoration from a chat title for search matching:
        astral-plane chars (most emoji), variation selectors, ZWJ/ZWNJ, and
        bidi/format marks — then collapse whitespace. 'ALIXON 🤓 🤯' →
        'alixon'; '😻もま かて' → 'もま かて'. CJK characters survive."""
        keep = []
        for ch in (t or ""):
            o = ord(ch)
            if o >= 0x10000:
                continue
            if ch in "\ufe0f\u200d\u200c\u200b\u200e\u200f" \
                    "\u202a\u202b\u202c\u202d\u202e\u2060":
                continue
            keep.append(ch)
        return re.sub(r"\s+", " ", "".join(keep)).strip().lower()

    def _open_scan_chat_locked(self, chat_title: str) -> tuple:
        """Open a chat by clicking its row in the left pane (no typing — the
        pane is already rendered). Returns (True, '') or (False, reason).
        Caller holds the bridge lock. Titles match on the NORMALIZED name so
        emoji-decorated titles ("ALIXON 🤓 🤯") open cleanly; the header is
        verified the same way to catch misclicks."""
        page = self._page
        target = self._normalize_chat_title(chat_title)
        try:
            titles = page.evaluate(_ROW_TITLES_JS)
            idx = next(
                (i for i, t in enumerate(titles or [])
                 if self._normalize_chat_title(str(t or "")) == target),
                None)
            if idx is None:
                return (False, f"could not find a chat named '{chat_title}'")
            page.locator(_CHAT_ROWS).nth(idx).click(timeout=8000)
        except Exception as e:
            return (False, f"could not open '{chat_title}': {e}")
        try:
            header = page.locator(
                'header [data-testid="conversation-title"], '
                '#main header span[dir="auto"]'
            ).first
            header.wait_for(state="visible", timeout=8000)
            got = self._normalize_chat_title(header.inner_text() or "")
            if got and got != target:
                return (False, f"opened a chat but it was '{got}', not "
                               f"'{chat_title}'")
        except Exception:
            pass   # header unreadable — proceed; the read is best-effort
        return True, ""

    def _read_recent_incoming_locked(self, chat_title: str, limit: int,
                                     timeout: int) -> list[str]:
        """The actual DOM read; caller holds the bridge lock. One evaluate
        walks the whole thread (fast on cold renders where per-element reads
        time out) after a pane-click open."""
        page = self._page
        ok, err = self._open_scan_chat_locked(chat_title)
        if not ok:
            raise RuntimeError(err)
        # A chat with a large history shows "Syncing older messages" while
        # WhatsApp downloads it — the thread is not rendered until that
        # banner clears. Keep waiting (bounded) while syncing OR while no
        # messages have rendered; a still-syncing chat returns [] and the
        # scan simply skips it (its name can be caught by the next scan).
        deadline = time.time() + 45
        while time.time() < deadline:
            try:
                syncing = page.evaluate(
                    "() => /syncing older messages/i.test("
                    "document.querySelector('#main')?.textContent || '')")
                if (not syncing
                        and page.locator('#main [data-id]').count() > 0):
                    break
            except Exception:
                pass
            time.sleep(1)
        try:
            return page.evaluate(
                """(limit) => {
                  const scope = document.querySelector('[data-testid="conversation-panel-messages"]')
                    || document.querySelector('#main');
                  if (!scope) return [];
                  const rows = Array.from(scope.querySelectorAll('[data-id]'));
                  const out = [];
                  for (let i = rows.length - 1; i >= 0; i--) {
                    const r = rows[i];
                    if (!r.querySelector('[data-testid="tail-in"]')) continue;
                    const el = r.querySelector('.copyable-text.selectable-text');
                    if (!el) continue;
                    const txt = (el.textContent || '').trim();
                    if (txt) { out.push(txt); if (out.length >= limit) break; }
                  }
                  return out;
                }""", limit)
        except Exception:
            return []

    # ── Meta AI (the in-WhatsApp assistant) ────────────────────────────────

    def meta_ai_ask(self, question: str, timeout: int = 90) -> str:
        """Ask Meta AI (WhatsApp's built-in assistant) a question and return
        its reply text. Runs entirely in the background browser — no screen
        interaction — through the bridge's single Playwright thread.

        Flow (all selectors verified live Aug 2026): open the "Meta AI" chat
        from the chat list, wait out the "Syncing older messages" banner,
        type + send the question, then poll the last incoming bubble until
        its "Thinking" placeholder is replaced by a stable answer.

        Raises RuntimeError with a clear message when the session isn't
        linked, the Meta AI chat doesn't exist on this account (region/
        feature-flagged), or the reply never arrives within `timeout`."""
        return self._submit(self._meta_ai_ask_impl, question, int(timeout),
                            timeout=float(timeout) + 30.0)

    def _meta_ai_ask_impl(self, question: str, timeout: int) -> str:
        with self._lock:
            if not self._ensure_alive():
                raise RuntimeError("WhatsApp bridge is not ready")
            if not self._ensure_logged_in_locked():
                raise RuntimeError(
                    "WhatsApp Web is not linked in the background browser — "
                    "say 'link whatsapp' (or 'secretary link') once to open "
                    "the window and scan the QR with your phone")
            return self._meta_ai_ask_locked(question, timeout)

    def _meta_ai_ask_locked(self, question: str, timeout: int) -> str:
        """The actual DOM flow; caller holds the bridge lock."""
        page = self._page
        question = (question or "").strip()
        if not question:
            raise RuntimeError("no question to ask Meta AI")

        # 1. Open the Meta AI chat (it lives in the chat list like any chat).
        #    Match on the TITLE only — a row whose preview mentions "Meta AI"
        #    must not be mistaken for the assistant chat.
        try:
            titles = page.evaluate(_ROW_TITLES_JS)
            idx = next(
                (i for i, t in enumerate(titles)
                 if str(t).strip().lower() == "meta ai"),
                None)
            if idx is None:
                raise RuntimeError("meta ai row not in list")
            page.locator(_CHAT_ROWS).nth(idx).click(timeout=8000)
        except Exception:
            raise RuntimeError(
                "the Meta AI chat isn't available on this WhatsApp account "
                "(Meta AI is region/feature-flagged) — nothing was sent")

        # 2. Wait for the chat to open, then for the sync banner to clear.
        try:
            box = page.locator('footer div[contenteditable="true"]').first
            box.wait_for(state="visible", timeout=15000)
        except Exception:
            raise RuntimeError("the Meta AI chat did not open")
        for _ in range(10):   # "Syncing older messages" can take ~5-10s
            try:
                syncing = page.evaluate(
                    "() => /syncing older messages/i.test("
                    "document.querySelector('#main')?.textContent || '')")
            except Exception:
                syncing = False
            if not syncing:
                break
            time.sleep(1.0)

        # 3. Type the question and send it (trusted keyboard, same as sends).
        box.click(timeout=8000)
        try:
            page.keyboard.press("Control+A")
        except Exception:
            pass
        page.keyboard.type(question, delay=15)
        page.keyboard.press("Enter")

        # 4. Wait for the reply: the last incoming bubble starts as
        #    "Thinking" and is replaced by (or streams into) the answer.
        #    Stability check — two consecutive identical reads — catches
        #    streaming mid-write.
        deadline = time.time() + timeout
        stable_text = None
        last_state = None
        while time.time() < deadline:
            try:
                st = page.evaluate(_META_READ_REPLY_JS)
            except Exception:
                st = None
            if st and st.get("found"):
                if st.get("hasImage") and not st.get("text"):
                    return "🖼️ Meta AI generated an image."
                if not st.get("thinking") and st.get("text"):
                    if stable_text == st["text"]:
                        return st["text"]
                    stable_text = st["text"]
            time.sleep(2.0)
        raise RuntimeError(
            f"Meta AI didn't finish replying within {timeout}s "
            f"(last state: {last_state or 'no incoming message yet'})")


# ── Group guard ───────────────────────────────────────────────────────────
# Photo-less groups give ZERO row-level signal (no megaphone, one avatar —
# verified live Aug 2026: "MEDICINE AND HEALTH SCIENCES SEPT 2025" looks
# exactly like an individual row), so the poll can't catch every group. The
# reliable check is the OPENED chat's subtitle: individuals show status text
# ("online", "last seen ...", "click here for contact info"), groups show a
# comma-separated member list. The send path checks it after opening the
# chat and refuses to send to groups — the user asked for individuals only.


def _subtitle_is_group(subtitle: str | None) -> bool:
    """True when an opened chat's subtitle indicates a group chat.

    Two independent signals (verified live Aug 2026):
      * the header hint is literally "click here for GROUP info" for groups
        vs "click here for contact info" for individuals — available
        immediately on open;
      * once the member list loads (~1-3s), groups show comma-separated
        participants ("You, A, B" or "A, B, C") — never status text.
    Individual status text ("online", "last seen ...", "typing\u2026") is
    never mistaken for a member list."""
    s = (subtitle or "").strip()
    if not s:
        return False
    low = s.lower()
    if "group info" in low:
        return True          # "click here for group info"
    if low.startswith(("online", "last seen", "typing", "recording",
                       "click here", "encrypted", "messages and calls")):
        return False
    return s.count(",") >= 2


# ── Shared-bridge registry (one browser per process) ────────────────────────
# The secretary monitor and one-shot sends (send_message) share ONE bridge:
# Playwright refuses to open the same profile directory twice, so a second
# WhatsAppBridge would crash on the profile lock. acquire_shared_bridge()
# returns the live bridge (creating it on first use); each caller must pair
# it with release_shared_bridge() when done, and the browser only exits when
# the last reference is released. The visible link window (secretary link)
# holds a long-lived reference, so toggling the monitor on/off never kills
# or replaces that window — the daemon reuses it every time.

_registry_lock = threading.Lock()
_shared_bridge: WhatsAppBridge | None = None
_shared_refs = 0


def acquire_shared_bridge(headless: bool = True,
                          cdp_url: str | None = None) -> tuple:
    """Return (bridge, created). Reuses the process-wide bridge when one is
    live (e.g. the secretary monitor's browser); otherwise creates and
    registers a new one. Pair every call with release_shared_bridge()."""
    global _shared_bridge, _shared_refs
    with _registry_lock:
        if _shared_bridge is not None:
            _shared_refs += 1
            return _shared_bridge, False
        bridge = WhatsAppBridge(headless=bool(headless), cdp_url=cdp_url)
        _shared_bridge = bridge
        _shared_refs = 1
        return bridge, True


def release_shared_bridge(bridge) -> None:
    """Drop a reference; when the last one is released the bridge is stopped
    and unregistered (its browser exits). No-op for unknown bridges."""
    global _shared_bridge, _shared_refs
    with _registry_lock:
        if _shared_bridge is not bridge:
            return
        _shared_refs -= 1
        if _shared_refs > 0:
            return
        bridge, _shared_bridge = _shared_bridge, None
    try:
        bridge.stop()
    except Exception:
        pass


def stop_all_bridges() -> None:
    """Force-stop and unregister the shared bridge, dropping every reference.

    Used when switching modes (e.g. a headless monitor → the visible link
    window): the profile directory can only be opened by one browser, so the
    old one must exit before a visible one takes over. Any in-flight sender
    then fails fast and falls back to the foreground flow."""
    global _shared_bridge, _shared_refs
    with _registry_lock:
        bridge = _shared_bridge
        _shared_bridge = None
        _shared_refs = 0
    if bridge is not None:
        try:
            bridge.stop()
        except Exception:
            pass
