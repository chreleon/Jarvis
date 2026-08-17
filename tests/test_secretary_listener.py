"""SecretaryListener + WhatsAppBridge: DOM-poll normalization, dedupe, and
the injectable background sender.

The heavy parts (launching Playwright, the live web.whatsapp.com page) are
not exercised here — those need a browser and a linked account. These tests
cover the deterministic logic around them: JS-result normalization, per-
process + persisted dedupe, the persisted fingerprint helpers, and the
send_fn override that lets auto-replies go through the background bridge
instead of the foreground sender.
"""

import contextlib
import threading
import time
import unittest
from unittest.mock import patch

from actions.secretary_listener import (
    _fingerprint,
    SecretaryListener,
)
from actions.whatsapp_bridge import normalize_unread


class NormalizeUnreadTests(unittest.TestCase):
    """Turning the page's JS rows into triage-ready (sender, preview) pairs."""

    def test_parses_rows(self):
        data = [
            {"title": "Mom", "preview": "dinner at 7?", "unread": 2,
             "time": "22:37"},
            {"title": "Dad", "preview": "ok", "unread": 1, "time": "08:09"},
        ]
        out = normalize_unread(data)
        self.assertEqual(out, [
            {"sender": "Mom", "preview": "dinner at 7?", "time": "22:37"},
            {"sender": "Dad", "preview": "ok", "time": "08:09"},
        ])

    def test_empty_preview_becomes_placeholder(self):
        # image / voice / reaction-only messages have no visible text
        out = normalize_unread([{"title": "Mom", "preview": "", "unread": 1}])
        self.assertEqual(out, [{"sender": "Mom", "preview": "(new message)",
                                "time": ""}])

    def test_missing_preview_defaults(self):
        self.assertEqual(
            normalize_unread([{"title": "Mom", "unread": 1}]),
            [{"sender": "Mom", "preview": "(new message)", "time": ""}],
        )

    def test_time_is_carried_through(self):
        out = normalize_unread([{"title": "Mom", "preview": "hi", "unread": 1,
                                 "time": "22:37"}])
        self.assertEqual(out, [{"sender": "Mom", "preview": "hi",
                                "time": "22:37"}])

    def test_blank_title_skipped(self):
        self.assertEqual(normalize_unread([{"title": "  ", "preview": "x"}]), [])
        self.assertEqual(normalize_unread(None), [])
        self.assertEqual(normalize_unread("garbage"), [])

    def test_groups_are_never_returned(self):
        # the boss asked: monitor everything, but never reply to groups
        data = [
            {"title": "Mom", "preview": "hi", "unread": 1, "group": False},
            {"title": "Family (5)", "preview": "who's coming?",
             "unread": 3, "group": True},
            {"title": "Work Group", "preview": "standup in 10",
             "unread": 2, "group": True},
        ]
        out = normalize_unread(data)
        self.assertEqual(out, [{"sender": "Mom", "preview": "hi", "time": ""}])

    def test_missed_call_rows_are_flagged(self):
        # a missed call is a call, not a text — the listener must escalate,
        # never auto-reply with "thanks for your message"
        data = [
            {"title": "Mom", "preview": "Missed voice call", "unread": 1,
             "group": False, "time": "22:10"},
            {"title": "Dad", "preview": "Missed video call", "unread": 1,
             "group": False, "time": "22:11"},
            {"title": "Bro", "preview": "Voice call", "unread": 1,
             "group": False, "time": "22:12"},
        ]
        out = normalize_unread(data)
        self.assertTrue(all(o.get("call") for o in out))
        self.assertEqual(out[0]["preview"], "Missed voice call")

    def test_normal_text_is_not_flagged_as_call(self):
        out = normalize_unread([{"title": "Mom", "preview": "dinner at 7?",
                                 "unread": 1, "group": False}])
        self.assertNotIn("call", out[0])


class ProcessedFingerprintTests(unittest.TestCase):
    """Persisted (cross-process) dedupe helpers in actions.secretary."""

    def test_mark_then_check(self):
        from actions.secretary import _is_processed, _mark_processed
        state = {"conversations": {}, "inbox": []}
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("actions.secretary._state", return_value=state))
            stack.enter_context(patch("actions.secretary._save_state", lambda s: None))
            self.assertFalse(_is_processed("abc"))
            _mark_processed("abc")
            self.assertTrue(_is_processed("abc"))
            self.assertFalse(_is_processed("def"))
        self.assertEqual(state.get("processed"), ["abc"])

    def test_mark_many_is_one_batched_write(self):
        from actions.secretary import _is_processed, _mark_processed_many
        state = {"conversations": {}, "inbox": []}
        saved = []
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("actions.secretary._state", return_value=state))
            stack.enter_context(patch("actions.secretary._save_state",
                                      side_effect=lambda s: saved.append(s)))
            _mark_processed_many(["a", "b", "a", "c"])
            self.assertEqual(state.get("processed"), ["a", "b", "c"])
            self.assertTrue(_is_processed("a"))
            self.assertTrue(_is_processed("c"))
            self.assertEqual(len(saved), 1)   # one write for the whole batch

    def test_is_processed_many_batches_one_state_load(self):
        # YinYang: the sweep must build the processed-set ONCE, not per item
        from actions.secretary import _is_processed_many
        state = {"conversations": {}, "inbox": [],
                 "processed": ["a", "c"]}
        loads = []
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch(
                "actions.secretary._state",
                side_effect=lambda: loads.append(1) or state))
            out = _is_processed_many(["a", "b", "c", "d"])
        self.assertEqual(out, {"a", "c"})
        self.assertEqual(len(loads), 1)      # one state load for the batch
        self.assertEqual(_is_processed_many([]), set())
        self.assertEqual(_is_processed_many(None), set())


class DedupeTests(unittest.TestCase):
    """A message is handed to the triage engine exactly once."""

    def _listener(self, handled):
        return SecretaryListener(
            on_message=lambda s, t: handled.append((s, t)))

    def test_same_message_handled_once(self):
        handled = []
        with patch("actions.secretary._is_processed", return_value=False), \
             patch("actions.secretary._mark_processed", lambda fp: None):
            listener = self._listener(handled)
            listener._handle_new("Mom", "dinner at 7?")
            listener._handle_new("Mom", "dinner at 7?")
        self.assertEqual(handled, [("Mom", "dinner at 7?")])

    def test_persisted_fingerprint_blocks_reprocessing(self):
        # a second process already handled it → skip, no re-reply
        handled = []
        with patch("actions.secretary._is_processed", return_value=True), \
             patch("actions.secretary._mark_processed", lambda fp: None):
            listener = self._listener(handled)
            listener._handle_new("Mom", "dinner at 7?")
        self.assertEqual(handled, [])

    def test_different_messages_both_handled(self):
        handled = []
        with patch("actions.secretary._is_processed", return_value=False), \
             patch("actions.secretary._mark_processed", lambda fp: None):
            listener = self._listener(handled)
            listener._handle_new("Mom", "dinner at 7?")
            listener._handle_new("Mom", "dinner at 8?")
        self.assertEqual(len(handled), 2)

    def test_fingerprint_is_stable_and_case_insensitive(self):
        self.assertEqual(
            _fingerprint("Mom", "Dinner at 7?"),
            _fingerprint("mom", "dinner at 7?"),
        )


class HandleMessageSendFnTests(unittest.TestCase):
    """Auto-replies can go through an injected sender (the background bridge)
    instead of the foreground send_message path."""

    def _patches(self, state, enabled=True):
        # _meta_draft is the Meta AI drafting hook — these tests cover the
        # send path, so it's pinned to the deterministic draft (the Meta AI
        # drafting logic has its own test class below).
        return [
            patch("actions.secretary.is_enabled", return_value=enabled),
            patch("actions.secretary._state", return_value=state),
            patch("actions.secretary._save_state", lambda s: None),
            patch("actions.secretary._load_cfg", return_value={"boss_name": "Boss"}),
            patch("actions.secretary._meta_draft",
                   side_effect=lambda s, m, d, media_kind=None: d),
        ]

    def test_send_fn_used_instead_of_send_message(self):
        from actions.secretary import handle_message
        state = {"conversations": {}, "inbox": []}
        sent = []
        with contextlib.ExitStack() as stack:
            for p in self._patches(state):
                stack.enter_context(p)
            sm = stack.enter_context(
                patch("actions.send_message.send_message", return_value="unused"))
            out = handle_message(
                "Mom", "hey how are you",
                send_fn=lambda s, t: sent.append((s, t)) or "bridge-sent")
        self.assertIn("Replied to Mom", out)
        self.assertIn("bridge-sent", out)
        self.assertEqual(len(sent), 1)
        self.assertIn("Thanks", sent[0][1])      # the drafted reply text
        sm.assert_not_called()                   # foreground sender untouched

    def test_send_fn_failure_reported_not_raised(self):
        from actions.secretary import handle_message
        state = {"conversations": {}, "inbox": []}

        def _boom(s, t):
            raise RuntimeError("bridge down")

        with contextlib.ExitStack() as stack:
            for p in self._patches(state):
                stack.enter_context(p)
            out = handle_message("Mom", "hey", send_fn=_boom)
        self.assertIn("sending failed", out)
        self.assertIn("bridge down", out)

    def test_no_send_fn_keeps_foreground_path(self):
        # default behavior unchanged: the foreground send_message is used
        from actions.secretary import handle_message
        state = {"conversations": {}, "inbox": []}
        with contextlib.ExitStack() as stack:
            for p in self._patches(state):
                stack.enter_context(p)
            sm = stack.enter_context(
                patch("actions.send_message.send_message", return_value="sent"))
            out = handle_message("Mom", "hey how are you")
        self.assertIn("Replied to Mom", out)
        sm.assert_called_once()


class SharedBridgeRegistryTests(unittest.TestCase):
    """One WhatsAppBridge per process, refcounted: the monitor and one-shot
    sends share a single browser (Playwright locks the profile dir), and it
    only exits when the last reference is released."""

    def _fake_bridge(self):
        calls = []

        class Fake:
            def __init__(self, **kw):
                calls.append(("new", kw))

            def start(self):
                calls.append(("start",))

            def stop(self):
                calls.append(("stop",))

        return calls, Fake

    def test_second_acquire_reuses_first_bridge(self):
        from actions.whatsapp_bridge import (
            acquire_shared_bridge, release_shared_bridge)
        calls, Fake = self._fake_bridge()
        with patch("actions.whatsapp_bridge.WhatsAppBridge", Fake):
            b1, created1 = acquire_shared_bridge()
            b2, created2 = acquire_shared_bridge()
            try:
                self.assertTrue(created1)
                self.assertFalse(created2)
                self.assertIs(b1, b2)
                self.assertEqual(calls, [("new", {"headless": True, "cdp_url": None})])
            finally:
                release_shared_bridge(b2)
                release_shared_bridge(b1)
        # one acquire held the browser alive past the first release
        self.assertEqual(calls.count(("stop",)), 1)

    def test_last_release_stops_browser(self):
        from actions.whatsapp_bridge import (
            acquire_shared_bridge, release_shared_bridge)
        calls, Fake = self._fake_bridge()
        with patch("actions.whatsapp_bridge.WhatsAppBridge", Fake):
            b, _ = acquire_shared_bridge()
            release_shared_bridge(b)
        self.assertEqual(calls, [("new", {"headless": True, "cdp_url": None}),
                                 ("stop",)])

    def test_release_foreign_bridge_is_noop(self):
        from actions.whatsapp_bridge import (
            acquire_shared_bridge, release_shared_bridge)
        calls, Fake = self._fake_bridge()
        with patch("actions.whatsapp_bridge.WhatsAppBridge", Fake):
            b, _ = acquire_shared_bridge()
            try:
                release_shared_bridge(object())   # unknown bridge — ignored
            finally:
                release_shared_bridge(b)
        self.assertEqual(calls.count(("stop",)), 1)


class LinkWindowTests(unittest.TestCase):
    """secretary link: open the persistent WhatsApp window (visible, same
    profile) so the boss scans the QR once — then stay connected forever."""

    class FakeBridge:
        def __init__(self, **kw):
            self.kw = kw
            self.visible = not kw.get("headless", True)
            self.started = False
            self.logged_in = False
            self.stopped = False

        def start(self):
            self.started = True

        def is_logged_in(self):
            return self.logged_in

        def stop(self):
            self.stopped = True

    def _patches(self, bridge, cdp=None, enabled=False):
        from unittest.mock import MagicMock
        acquire = MagicMock(return_value=(bridge, True))
        return [
            patch("actions.secretary_listener._cdp_url_from_config",
                  return_value=cdp),
            patch("actions.secretary_listener.acquire_shared_bridge", acquire),
            patch("actions.secretary_listener.release_shared_bridge"),
            patch("actions.secretary_listener.stop_all_bridges"),
            patch("actions.secretary_listener._listener", None),
            patch("actions.secretary_listener._monitor_bridge", None),
            patch("actions.secretary.is_enabled", return_value=enabled),
        ]

    def _run(self, bridge, cdp=None, enabled=False):
        from actions.secretary_listener import link_whatsapp
        with contextlib.ExitStack() as stack:
            for p in self._patches(bridge, cdp, enabled):
                stack.enter_context(p)
            return link_whatsapp(), bridge

    def test_not_linked_shows_qr_and_saves_session(self):
        from actions.secretary_listener import link_whatsapp as fn
        bridge = self.FakeBridge()
        bridge.visible = True      # the real acquire uses headless=False
        with contextlib.ExitStack() as stack:
            for p in self._patches(bridge):
                stack.enter_context(p)
            out = fn()
        self.assertTrue(bridge.started)
        self.assertTrue(bridge.visible)          # visible, not headless
        self.assertIn("QR code", out)
        self.assertIn("saved permanently", out)
        self.assertIn("same window is reused", out)

    def test_already_linked_reports_no_rescan(self):
        from actions.secretary_listener import link_whatsapp as fn
        bridge = self.FakeBridge()
        bridge.logged_in = True
        with contextlib.ExitStack() as stack:
            for p in self._patches(bridge):
                stack.enter_context(p)
            out = fn()
        self.assertIn("already linked", out)
        self.assertIn("never need to scan again", out)

    def test_cdp_mode_needs_no_window(self):
        from actions.secretary_listener import link_whatsapp
        bridge = self.FakeBridge()
        with contextlib.ExitStack() as stack:
            for p in self._patches(bridge, cdp="http://127.0.0.1:9222"):
                stack.enter_context(p)
            acquire = stack.enter_context(
                patch("actions.secretary_listener.acquire_shared_bridge"))
            out = link_whatsapp()
        self.assertIn("your own browser", out)
        acquire.assert_not_called()

    def test_resumes_monitor_on_same_window_when_enabled(self):
        from actions.secretary_listener import link_whatsapp as fn
        bridge = self.FakeBridge()
        bridge.logged_in = True
        with contextlib.ExitStack() as stack:
            for p in self._patches(bridge, enabled=True):
                stack.enter_context(p)
            start = stack.enter_context(
                patch("actions.secretary_listener.start_monitor",
                      return_value="restarted"))
            out = fn()
        self.assertIn("same window", out)
        start.assert_called_once()

    def test_start_failure_releases_bridge(self):
        from actions.secretary_listener import link_whatsapp as fn

        class Boom(self.FakeBridge):
            def start(self):
                raise RuntimeError("playwright missing")

        bridge = Boom()
        with contextlib.ExitStack() as stack:
            for p in self._patches(bridge):
                stack.enter_context(p)
            release = stack.enter_context(
                patch("actions.secretary_listener.release_shared_bridge"))
            out = fn()
        self.assertIn("Could not open the WhatsApp window", out)
        release.assert_called_once()


class StartMonitorLinkWindowTests(unittest.TestCase):
    """start_monitor opens a visible window for the one-time link (first
    run) and goes headless once the profile is linked."""

    class FakeBridge:
        def __init__(self, **kw):
            self.kw = kw
            self.visible = not kw.get("headless", True)
            self.mode = "headless"
            self.stopped = False

        def start(self):
            pass

        def stop(self):
            self.stopped = True

        def poll_unread(self):
            return []

        def needs_qr(self):
            return False

        def capture_qr(self):
            return None

        def is_logged_in(self):
            return True

        def send_message(self, r, t):
            return "sent"

    def _patches(self, first_link, headless, cdp=None):
        return [
            patch("actions.secretary_listener._needs_visible_link_window",
                  return_value=first_link),
            patch("actions.secretary_listener._headless_from_config",
                  return_value=headless),
            patch("actions.secretary_listener._cdp_url_from_config",
                  return_value=cdp),
            patch("actions.secretary_listener._listener", None),
            patch("actions.secretary_listener._monitor_bridge", None),
        ]

    def test_first_run_opens_visible_window(self):
        from actions.secretary_listener import start_monitor, stop_monitor
        calls = {}

        def _acquire(**kw):
            calls.update(kw)
            return self.FakeBridge(**kw), True

        with contextlib.ExitStack() as stack:
            for p in self._patches(first_link=True, headless=True):
                stack.enter_context(p)
            stack.enter_context(
                patch("actions.secretary_listener.acquire_shared_bridge", _acquire))
            out = start_monitor()
            stop_monitor()
        self.assertFalse(calls["headless"])     # visible for the one-time link
        self.assertIn("window just opened", out)
        self.assertIn("QR code", out)

    def test_linked_profile_stays_headless(self):
        from actions.secretary_listener import start_monitor, stop_monitor
        calls = {}

        def _acquire(**kw):
            calls.update(kw)
            return self.FakeBridge(**kw), True

        with contextlib.ExitStack() as stack:
            for p in self._patches(first_link=False, headless=True):
                stack.enter_context(p)
            stack.enter_context(
                patch("actions.secretary_listener.acquire_shared_bridge", _acquire))
            out = start_monitor()
            stop_monitor()
        self.assertTrue(calls["headless"])
        self.assertIn("headless", out)


class LoopReleaseTests(unittest.TestCase):
    """The monitor loop must never leak a bridge reference or browser."""

    def test_start_failure_releases_shared_ref(self):
        from actions.secretary_listener import SecretaryListener

        class Boom:
            def start(self):
                raise RuntimeError("start exploded")

        released = []
        with patch("actions.secretary_listener.release_shared_bridge",
                   side_effect=lambda b: released.append(b)):
            listener = SecretaryListener(bridge=Boom(), poll_seconds=60)
            listener._loop()          # returns immediately on start failure
        self.assertEqual(listener._state, "error")
        self.assertIn("start exploded", listener._last_error)
        self.assertEqual(len(released), 1)

    def test_start_failure_stops_private_bridge(self):
        from actions.secretary_listener import SecretaryListener

        class Boom:
            def __init__(self, **kw):
                self.stopped = False

            def start(self):
                raise RuntimeError("no browser")

            def stop(self):
                self.stopped = True

        with patch("actions.whatsapp_bridge.WhatsAppBridge",
                   side_effect=Boom) as WB:
            listener = SecretaryListener(poll_seconds=60)  # no bridge injected
            listener._loop()
        self.assertEqual(listener._state, "error")
        self.assertTrue(WB.return_value.stopped)  # private bridge stopped

    def test_loop_rebuilds_wedged_bridge(self):
        """3 consecutive poll failures tear down the wedged bridge and
        acquire a fresh one — the monitor self-heals instead of dying
        silently (daktari)."""
        from actions.secretary_listener import SecretaryListener
        state = {"polls": 0, "rebuilds": 0}

        class Wedged:
            def start(self):
                pass

            def stop(self):
                pass

            def poll_unread(self):
                state["polls"] += 1
                raise RuntimeError("browser wedged")

        class Healthy:
            def start(self):
                pass

            def stop(self):
                pass

            def poll_unread(self):
                # stop the loop after the rebuilt bridge's first healthy poll
                listener._stop_event.set()
                return []

            def ensure_unread_filter(self):
                return True

            def poll_calls(self):
                return []

        def fake_acquire(headless=True, cdp_url=None):
            state["rebuilds"] += 1
            return Healthy(), True

        # _rebuild_bridge imports these from actions.whatsapp_bridge itself
        with patch("actions.whatsapp_bridge.stop_all_bridges"), \
             patch("actions.whatsapp_bridge.acquire_shared_bridge",
                   side_effect=fake_acquire), \
             patch.object(SecretaryListener, "_sleep_interruptible",
                          return_value=None):
            listener = SecretaryListener(bridge=Wedged(), poll_seconds=60)
            listener._loop()
        self.assertEqual(state["rebuilds"], 1)
        self.assertIsInstance(listener._bridge, Healthy)

    def test_ring_dedupe_prunes_stale_entries(self):
        """The ring-escalation dedupe map is bounded: entries older than the
        5-minute window are pruned, so it can't grow without bound (YinYang)."""
        from actions.secretary_listener import SecretaryListener

        class NoCalls:
            def poll_calls(self):
                return []

        listener = SecretaryListener(poll_seconds=60)
        listener._ring_escalated = {
            "ring|old|audio": 0.0,                      # stale
            "ring|fresh|audio": time.time(),            # current
        }
        listener._handle_ringing_calls(NoCalls())
        self.assertEqual(list(listener._ring_escalated.keys()),
                         ["ring|fresh|audio"])


class SendMessageLockedTests(unittest.TestCase):
    """The bridge send is verified end-to-end: right chat, message sent.

    Mirrors the live page (verified Aug 2026): the search box is an <input
    aria-label="Search or start a new chat">, search results render inline
    in #pane-side rows (section headers contribute empty titles), and the
    matching row is clicked with a real Playwright click."""

    class FakeLocator:
        def __init__(self, page, key):
            self._page = page
            self._key = key
            self.first = self

        def click(self, timeout=None):
            self._page.calls.append(("click", self._key))
            if self._key.startswith("row") and self._page.header_queue:
                self._page.header_text = self._page.header_queue.pop(0)
            if self._key == "send":
                self._page.box_text = ""
            return None

        def nth(self, i):
            return SendMessageLockedTests.FakeLocator(self._page, f"row{i}")

        def wait_for(self, state=None, timeout=None):
            self._page.calls.append(("wait_for", self._key))
            return None

        def inner_text(self):
            return getattr(self._page, self._key + "_text", "")

        def get_attribute(self, name):
            return None

    class FakeKeyboard:
        def __init__(self, page):
            self._page = page

        def press(self, key):
            self._page.calls.append(("press", key))
            if key == "Enter" and self._page.enter_clears:
                self._page.box_text = ""

        def type(self, text, delay=0):
            self._page.calls.append(("type", text))
            self._page.box_text = text

    class FakePage:
        def __init__(self):
            self.calls = []
            # live-style search results: headers empty, "Momanyi" at idx 3
            self.row_titles = ["", "Dorca", "", "Momanyi", "Momanyi"]
            self.header_text = ""
            self.header_queue = ["Momanyi"]
            self.subtitle_text = "online"   # individual status text
            self.box_text = ""
            self.enter_clears = True
            self.match_timeout = False   # True → search never finds a match
            self.keyboard = SendMessageLockedTests.FakeKeyboard(self)

        def locator(self, sel):
            if "pane-side" in sel:
                return SendMessageLockedTests.FakeLocator(self, "rows")
            if ("Search or start a new chat" in sel or "data-tab=\"3\"" in sel
                    or "chat-list-search" in sel):
                return SendMessageLockedTests.FakeLocator(self, "search")
            if "conversation-title" in sel or "header span" in sel:
                return SendMessageLockedTests.FakeLocator(self, "header")
            if "chat-subtitle" in sel:
                return SendMessageLockedTests.FakeLocator(self, "subtitle")
            if "compose-btn-send" in sel:
                return SendMessageLockedTests.FakeLocator(self, "send")
            return SendMessageLockedTests.FakeLocator(self, "box")

        def evaluate(self, js, arg=None):
            return list(self.row_titles)

        def wait_for_function(self, js, arg=None, timeout=None):
            if self.match_timeout:
                raise TimeoutError("no matching row appeared")
            return None

    def _send(self, page, receiver="Mom", text="hi there"):
        from actions.whatsapp_bridge import WhatsAppBridge
        bridge = WhatsAppBridge()
        bridge._page = page
        return bridge._send_message_locked(receiver, text), page

    def test_happy_path_sends(self):
        page = self.FakePage()
        out, page = self._send(page)
        self.assertEqual(out, "sent via WhatsApp Web (Mom)")
        self.assertIn(("click", "row3"), page.calls)   # matched row clicked
        self.assertIn(("type", "hi there"), page.calls)
        self.assertIn(("press", "Enter"), page.calls)
        self.assertNotIn(("click", "send"), page.calls)  # no fallback needed

    def test_contact_not_found_no_send(self):
        page = self.FakePage()
        page.row_titles = ["", "Dorca"]   # nothing starting with "Mom"
        page.match_timeout = True
        out, page = self._send(page)
        self.assertIn("could not find a chat named 'Mom'", out)
        self.assertNotIn(("type", "hi there"), page.calls)
        self.assertNotIn(("click", "row3"), page.calls)

    def test_group_chat_is_never_sent_to(self):
        # photo-less groups look like individuals in the list, but the
        # opened chat's subtitle lists members — the send must refuse.
        page = self.FakePage()
        page.subtitle_text = ("MEDICINE AND HEALTH SCIENCES SEPT 2025"
                              "~L!m0, Angel, Britney, Didier, Esther, Gift")
        out, page = self._send(page)
        self.assertIn("group chat", out)
        self.assertIn("no message was sent", out)
        self.assertNotIn(("type", "hi there"), page.calls)
        self.assertNotIn(("press", "Enter"), page.calls)

    def test_group_info_hint_refuses_send(self):
        # shown immediately on open — before the member list renders
        page = self.FakePage()
        page.subtitle_text = "click here for group info"
        out, page = self._send(page)
        self.assertIn("group chat", out)
        self.assertNotIn(("type", "hi there"), page.calls)

    def test_individual_status_subtitle_still_sends(self):
        # "online", "last seen ...", "click here for contact info" are
        # individual status text — never mistaken for a group member list.
        for subtitle in ("online", "last seen today at 12:30",
                         "click here for contact info", "typing\u2026"):
            page = self.FakePage()
            page.subtitle_text = subtitle
            out, _ = self._send(page)
            self.assertEqual(out, "sent via WhatsApp Web (Mom)", subtitle)

    def test_phone_number_receiver_matches_digit_normalized_row(self):
        """"Message yourself" renders as "+254 112 093400" with RTL marks —
        a digits-only receiver must still find it."""
        page = self.FakePage()
        page.row_titles = ["", "\u200f\u202a+254 112 093400\u202c\u200f",
                           "NURSING CLASS 09/25"]
        page.header_queue = ["+254 112 093400"]
        out, page = self._send(page, receiver="254112093400")
        self.assertEqual(out, "sent via WhatsApp Web (254112093400)")
        self.assertIn(("click", "row1"), page.calls)   # matched by digits
        self.assertIn(("press", "Enter"), page.calls)

    def test_wrong_chat_retries_search_once(self):
        page = self.FakePage()
        page.header_queue = ["Wrong Chat", "Momanyi"]  # wrong chat first
        out, page = self._send(page)
        self.assertEqual(out, "sent via WhatsApp Web (Mom)")
        # searched twice (initial + retry after the wrong chat opened)
        self.assertEqual(page.calls.count(("type", "Mom")), 2)

    def test_send_retried_when_compose_box_still_holds_text(self):
        page = self.FakePage()
        page.enter_clears = False       # Enter alone does not send (drift)
        out, page = self._send(page, text="hello")
        self.assertEqual(out, "sent via WhatsApp Web (Mom)")
        enters = [c for c in page.calls if c == ("press", "Enter")]
        self.assertGreaterEqual(len(enters), 2)   # Enter, then Enter again
        self.assertIn(("click", "send"), page.calls)  # send-button fallback


class SubtitleGroupGuardTests(unittest.TestCase):
    """The opened-chat subtitle is the definitive individual-vs-group test:
    groups list members (commas), individuals show status text."""

    def test_member_list_is_group(self):
        from actions.whatsapp_bridge import _subtitle_is_group
        self.assertTrue(_subtitle_is_group("A, B, C"))
        self.assertTrue(_subtitle_is_group("You, Angel, Britney, Didier"))
        self.assertTrue(_subtitle_is_group("~L!m0, Angel, Britney, Gift"))

    def test_group_info_hint_is_group(self):
        # shown immediately on open, before the member list renders
        from actions.whatsapp_bridge import _subtitle_is_group
        self.assertTrue(_subtitle_is_group("click here for group info"))

    def test_status_text_is_individual(self):
        from actions.whatsapp_bridge import _subtitle_is_group
        self.assertFalse(_subtitle_is_group("online"))
        self.assertFalse(_subtitle_is_group("last seen today at 12:30"))
        self.assertFalse(_subtitle_is_group("click here for contact info"))
        self.assertFalse(_subtitle_is_group("typing\u2026"))
        self.assertFalse(_subtitle_is_group("recording\u2026"))
        self.assertFalse(_subtitle_is_group("encrypted"))

    def test_empty_or_unknown_subtitle_is_not_group(self):
        from actions.whatsapp_bridge import _subtitle_is_group
        self.assertFalse(_subtitle_is_group(""))
        self.assertFalse(_subtitle_is_group(None))


class WaitLoggedInTests(unittest.TestCase):
    """A cold browser takes ~10s to restore the WhatsApp session — the send
    path must wait for login instead of failing a one-shot check."""

    def test_waits_until_logged_in(self):
        from actions.whatsapp_bridge import WhatsAppBridge
        bridge = WhatsAppBridge()
        state = {"calls": 0}

        def fake_login():
            state["calls"] += 1
            return state["calls"] >= 3   # becomes logged in on the 3rd check

        with patch.object(bridge, "_is_logged_in_impl", side_effect=fake_login), \
             patch.object(bridge, "_needs_qr_impl", return_value=False):
            ok = bridge._wait_logged_in_impl(timeout=10)
        self.assertTrue(ok)
        self.assertEqual(state["calls"], 3)

    def test_qr_appearing_means_not_linked(self):
        from actions.whatsapp_bridge import WhatsAppBridge
        bridge = WhatsAppBridge()
        with patch.object(bridge, "_is_logged_in_impl", return_value=False), \
             patch.object(bridge, "_needs_qr_impl", return_value=True):
            ok = bridge._wait_logged_in_impl(timeout=10)
        self.assertFalse(ok)   # QR up → genuinely needs linking, give up fast

    def test_times_out_when_never_ready(self):
        from actions.whatsapp_bridge import WhatsAppBridge
        bridge = WhatsAppBridge()
        with patch.object(bridge, "_is_logged_in_impl", return_value=False), \
             patch.object(bridge, "_needs_qr_impl", return_value=False), \
             patch("actions.whatsapp_bridge.time.sleep", lambda s: None):
            ok = bridge._wait_logged_in_impl(timeout=0.001)
        self.assertFalse(ok)


class HeadlessConfigTests(unittest.TestCase):
    """secretary_headless: null must mean default headless, not a visible
    window (bool(None) is False)."""

    def test_null_means_headless(self):
        from actions.secretary_listener import _headless_from_config
        with patch("actions.secretary_listener._read_cfg",
                   return_value={"secretary_headless": None}):
            self.assertTrue(_headless_from_config())

    def test_explicit_false_means_visible(self):
        from actions.secretary_listener import _headless_from_config
        with patch("actions.secretary_listener._read_cfg",
                   return_value={"secretary_headless": False}):
            self.assertFalse(_headless_from_config())

    def test_missing_key_defaults_headless(self):
        from actions.secretary_listener import _headless_from_config
        with patch("actions.secretary_listener._read_cfg", return_value={}):
            self.assertTrue(_headless_from_config())


class SecretaryCallAndSessionTests(unittest.TestCase):
    """_escalate_call records + escalates; the session report built by
    `secretary off` summarizes what happened."""

    def _state_with(self, **kw):
        st = {"conversations": {}, "inbox": [], "calls": [], **kw}
        return st

    def test_escalate_call_adds_inbox_and_call_log(self):
        from actions.secretary import _escalate_call
        st = self._state_with()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("actions.secretary._state", return_value=st))
            stack.enter_context(patch("actions.secretary._save_state", lambda s: None))
            out = _escalate_call("Mom", "video")
        self.assertIn("Mom", out)
        self.assertEqual(len(st["inbox"]), 1)
        self.assertEqual(st["inbox"][0]["message"], "video call")
        self.assertEqual(len(st["calls"]), 1)
        self.assertEqual(st["calls"][0]["kind"], "video")
        self.assertFalse(st["calls"][0]["ringing"])

    def test_escalate_ringing_call_marks_ringing(self):
        from actions.secretary import _escalate_call
        st = self._state_with()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("actions.secretary._state", return_value=st))
            stack.enter_context(patch("actions.secretary._save_state", lambda s: None))
            _escalate_call("Dad", "audio", ringing=True)
        self.assertTrue(st["calls"][0]["ringing"])

    def test_session_overview_reports_activity(self):
        from actions.secretary import _session_overview
        st = self._state_with()
        st["session_start"] = "2026-08-16T08:00:00"
        st["conversations"] = {
            "Mom": [{"role": "incoming", "text": "dinner?",
                      "at": "2026-08-16T09:00:00"},
                    {"role": "outgoing", "text": "Hi Mom! Thanks...",
                      "at": "2026-08-16T09:00:01"}],
            "Dad": [{"role": "incoming", "text": "ok",
                      "at": "2026-08-16T09:05:00"}],
        }
        st["calls"] = [{"from": "Esther", "kind": "video", "ringing": True,
                          "at": "2026-08-16T10:00:00"}]
        st["inbox"] = [{"from": "Bank", "message": "payment due",
                          "reasons": ["money"], "draft": "",
                          "at": "2026-08-16T10:30:00"}]
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("actions.secretary._state", return_value=st))
            stack.enter_context(patch("actions.secretary._save_state", lambda s: None))
            out = _session_overview()
        self.assertIn("Talked to 1", out)          # Mom replied; Dad only incoming
        self.assertIn("Mom", out)
        self.assertIn("Hi Mom! Thanks", out)
        self.assertIn("Esther: video call (ringing)", out)
        self.assertIn("Bank", out)
        self.assertIn("payment due", out)

    def test_session_overview_excludes_pre_session_entries(self):
        from actions.secretary import _session_overview
        st = self._state_with()
        st["session_start"] = "2026-08-16T08:00:00"
        st["conversations"] = {
            "Old Contact": [{"role": "outgoing", "text": "old reply",
                              "at": "2026-08-15T09:00:00"}],
        }
        st["inbox"] = [{"from": "Old", "message": "old urgent",
                          "reasons": [], "draft": "",
                          "at": "2026-08-15T09:00:00"}]
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("actions.secretary._state", return_value=st))
            stack.enter_context(patch("actions.secretary._save_state", lambda s: None))
            out = _session_overview()
        self.assertNotIn("Old Contact", out)
        self.assertNotIn("old urgent", out)
        self.assertIn("No conversations", out)


class RecencyTests(unittest.TestCase):
    """Only chats with a message from the past 24h are tended."""

    def test_today_times_are_recent(self):
        from actions.secretary_listener import _is_recent
        self.assertTrue(_is_recent("22:37"))
        self.assertTrue(_is_recent("08:09"))
        self.assertTrue(_is_recent("0:05"))

    def test_older_labels_are_not_recent(self):
        from actions.secretary_listener import _is_recent
        self.assertFalse(_is_recent("Yesterday"))
        self.assertFalse(_is_recent("Thursday"))
        self.assertFalse(_is_recent("15/08/2026"))

    def test_unknown_time_defaults_to_recent(self):
        # a parsing quirk must never silently drop a real message
        from actions.secretary_listener import _is_recent
        self.assertTrue(_is_recent(""))
        self.assertTrue(_is_recent(None))

    def test_recency_key_orders_newest_first(self):
        from actions.secretary_listener import _recency_key
        self.assertGreater(_recency_key("23:00"), _recency_key("22:00"))
        self.assertGreater(_recency_key(""), _recency_key("23:00"))


class SweepTests(unittest.TestCase):
    """The catch-up sweep: individuals, recent-only, newest first, batched."""

    def test_recent_handled_stale_skipped(self):
        handled = []
        items = [
            {"sender": "Mom", "preview": "hi", "time": "22:00"},
            {"sender": "Old Guy", "preview": "hey", "time": "Yesterday"},
            {"sender": "Weeks Ago", "preview": "yo", "time": "Thursday"},
        ]
        with patch("actions.secretary._is_processed", return_value=False), \
             patch("actions.secretary._mark_processed_many") as mark:
            listener = SecretaryListener(
                on_message=lambda s, t: handled.append((s, t)))
            listener._handle_sweep(items)
        self.assertEqual(handled, [("Mom", "hi")])
        mark.assert_called_once()
        self.assertEqual(len(mark.call_args[0][0]), 1)

    def test_newest_first_order(self):
        handled = []
        items = [
            {"sender": "A", "preview": "older", "time": "08:00"},
            {"sender": "B", "preview": "newer", "time": "22:00"},
            {"sender": "C", "preview": "unknown", "time": ""},
        ]
        with patch("actions.secretary._is_processed_many", return_value=set()), \
             patch("actions.secretary._mark_processed_many", lambda fps: None):
            listener = SecretaryListener(
                on_message=lambda s, t: handled.append((s, t)))
            listener._handle_sweep(items)
        # unknown sorts newest, then 22:00, then 08:00
        self.assertEqual(handled, [("C", "unknown"), ("B", "newer"),
                                   ("A", "older")])

    def test_whole_sweep_marked_in_one_batch(self):
        handled = []
        items = [{"sender": "Mom", "preview": "1", "time": "22:00"},
                 {"sender": "Dad", "preview": "2", "time": "22:01"},
                 {"sender": "Sis", "preview": "3", "time": "22:02"}]
        with patch("actions.secretary._is_processed", return_value=False), \
             patch("actions.secretary._mark_processed_many") as mark:
            listener = SecretaryListener(
                on_message=lambda s, t: handled.append((s, t)))
            listener._handle_sweep(items)
        self.assertEqual(len(handled), 3)
        mark.assert_called_once()
        self.assertEqual(len(mark.call_args[0][0]), 3)

    def test_duplicates_handled_once_across_sweeps(self):
        handled = []
        items = [{"sender": "Mom", "preview": "hi", "time": "22:00"}]
        with patch("actions.secretary._is_processed_many", return_value=set()), \
             patch("actions.secretary._mark_processed_many", lambda fps: None):
            listener = SecretaryListener(
                on_message=lambda s, t: handled.append((s, t)))
            listener._handle_sweep(items)
            listener._handle_sweep(items)   # same sweep twice
        self.assertEqual(handled, [("Mom", "hi")])

    def test_seen_cache_is_capped(self):
        # `_seen` is only a same-process fast path (persisted fingerprints
        # are the real guard) — it must not grow without bound while the
        # monitor runs 24/7 (YinYang: bound in-memory growth).
        handled = []
        with patch("actions.secretary._is_processed_many", return_value=set()), \
             patch("actions.secretary._mark_processed_many", lambda fps: None):
            listener = SecretaryListener(
                on_message=lambda s, t: handled.append((s, t)))
            for i in range(1100):
                listener._handle_sweep([
                    {"sender": f"P{i}", "preview": f"m{i}", "time": "22:00"}])
        self.assertEqual(len(handled), 1100)      # every message still handled
        self.assertLessEqual(len(listener._seen), 1000)  # bounded, not growing unbounded

    def test_missed_call_escalated_not_auto_replied(self):
        handled, escalated = [], []
        items = [{"sender": "Mom", "preview": "Missed video call",
                  "time": "22:10", "call": True}]
        with patch("actions.secretary._is_processed_many", return_value=set()), \
             patch("actions.secretary._mark_processed_many", lambda fps: None), \
             patch("actions.secretary._escalate_call",
                   side_effect=lambda s, k: escalated.append((s, k))):
            listener = SecretaryListener(
                on_message=lambda s, t: handled.append((s, t)))
            listener._handle_sweep(items)
        self.assertEqual(handled, [])                # never auto-replied
        self.assertEqual(escalated, [("Mom", "video")])

    def test_ringing_call_escalated_once(self):
        escalated = []
        with patch("actions.secretary._escalate_call",
                   side_effect=lambda s, k, ringing=False:
                   escalated.append((s, k, ringing))):
            listener = SecretaryListener(on_message=lambda s, t: None)
            class FakeBridge:
                def poll_calls(self):
                    return [{"from": "Mom", "kind": "video"}]
            listener._handle_ringing_calls(FakeBridge())
            listener._handle_ringing_calls(FakeBridge())  # still ringing
        self.assertEqual(escalated, [("Mom", "video", True)])

    def test_ringing_call_different_callers_both_escalated(self):
        escalated = []
        with patch("actions.secretary._escalate_call",
                   side_effect=lambda s, k, ringing=False:
                   escalated.append((s, k, ringing))):
            listener = SecretaryListener(on_message=lambda s, t: None)
            class FakeBridge:
                def poll_calls(self):
                    return [{"from": "Mom", "kind": "video"}]
            listener._handle_ringing_calls(FakeBridge())
            class FakeBridge2:
                def poll_calls(self):
                    return [{"from": "Dad", "kind": "audio"}]
            listener._handle_ringing_calls(FakeBridge2())
        self.assertEqual(len(escalated), 2)


class SelfChatTests(unittest.TestCase):
    """The boss's own chat (secretary_self_chat, e.g. "Omoke Jr" — texting
    themselves from another number) is a remote dashboard: its messages run
    through the Jeeves brain and the reply is sent back into the chat —
    never triaged like a third party."""

    def test_is_self_chat_matches_case_insensitively(self):
        listener = SecretaryListener()
        listener._self_chat_titles = ["Omoke Jr"]
        self.assertTrue(listener._is_self_chat("omoke jr"))
        self.assertTrue(listener._is_self_chat("OMOKE JR"))
        self.assertTrue(listener._is_self_chat("Omoke Jr"))
        self.assertFalse(listener._is_self_chat("Mom"))
        self.assertFalse(listener._is_self_chat(""))

    def test_self_chat_titles_parses_config(self):
        from actions.secretary_listener import _self_chat_titles
        with patch("actions.secretary_listener._read_cfg",
                   return_value={"secretary_self_chat": "Omoke Jr"}):
            self.assertEqual(_self_chat_titles(), ["Omoke Jr"])
        with patch("actions.secretary_listener._read_cfg",
                   return_value={"secretary_self_chat": "Omoke Jr, Boss 2"}):
            self.assertEqual(_self_chat_titles(), ["Omoke Jr", "Boss 2"])
        with patch("actions.secretary_listener._read_cfg",
                   return_value={"secretary_self_chat": ["Omoke Jr", "Boss 2"]}):
            self.assertEqual(_self_chat_titles(), ["Omoke Jr", "Boss 2"])
        with patch("actions.secretary_listener._read_cfg", return_value={}):
            self.assertEqual(_self_chat_titles(), [])

    def test_handle_self_chat_runs_brain_and_sends_reply_back(self):
        brain, sent = [], []

        class FakeBridge:
            def send_message(self, receiver, text):
                sent.append((receiver, text))

        listener = SecretaryListener(
            on_self_chat=lambda s, t: brain.append((s, t)) or "Here's the answer",
            bridge=FakeBridge())
        listener._handle_self_chat("Omoke Jr", "what's the weather")
        self.assertEqual(brain, [("Omoke Jr", "what's the weather")])
        self.assertEqual(sent, [("Omoke Jr", "Here's the answer")])

    def test_handle_self_chat_without_brain_wired_sends_nothing(self):
        sent = []

        class FakeBridge:
            def send_message(self, receiver, text):
                sent.append((receiver, text))

        listener = SecretaryListener(bridge=FakeBridge())
        listener._handle_self_chat("Omoke Jr", "hello")   # no on_self_chat
        self.assertEqual(sent, [])

    def test_sweep_routes_self_chat_to_brain_not_triage(self):
        triaged, brain, sent = [], [], []
        items = [
            {"sender": "Omoke Jr", "preview": "system status", "time": "22:00"},
            {"sender": "Mom", "preview": "hi", "time": "22:01"},
        ]

        class FakeBridge:
            def send_message(self, receiver, text):
                sent.append((receiver, text))

        with patch("actions.secretary._is_processed_many", return_value=set()), \
             patch("actions.secretary._mark_processed_many", lambda fps: None):
            listener = SecretaryListener(
                on_message=lambda s, t: triaged.append((s, t)),
                on_self_chat=lambda s, t: brain.append((s, t)) or "Done.",
                bridge=FakeBridge())
            listener._handle_sweep(items)
        self.assertEqual(brain, [("Omoke Jr", "system status")])
        self.assertEqual(triaged, [("Mom", "hi")])
        self.assertEqual(sent, [("Omoke Jr", "Done.")])


class MetaAiNeverTriageTests(unittest.TestCase):
    """Meta AI is the assistant Jeeves messages on purpose (meta_ai tool /
    brain fallback) — its replies must never be triaged or auto-replied to."""

    def test_is_meta_ai_chat(self):
        from actions.secretary_listener import _is_meta_ai_chat
        self.assertTrue(_is_meta_ai_chat("Meta AI"))
        self.assertTrue(_is_meta_ai_chat("meta ai"))
        self.assertTrue(_is_meta_ai_chat("Meta AI (beta)"))
        self.assertFalse(_is_meta_ai_chat("Mom"))
        self.assertFalse(_is_meta_ai_chat(""))

    def test_sweep_skips_meta_ai_rows(self):
        triaged, escalated = [], []
        items = [
            {"sender": "Meta AI", "preview": "2 + 2 = 4", "time": "22:00"},
            {"sender": "Mom", "preview": "hi", "time": "22:01"},
        ]
        with patch("actions.secretary._is_processed_many", return_value=set()), \
             patch("actions.secretary._mark_processed_many", lambda fps: None), \
             patch("actions.secretary._escalate_call",
                   side_effect=lambda s, k: escalated.append((s, k))):
            listener = SecretaryListener(
                on_message=lambda s, t: triaged.append((s, t)))
            listener._handle_sweep(items)
        self.assertEqual(triaged, [("Mom", "hi")])
        self.assertEqual(escalated, [])

    def test_handle_new_skips_meta_ai(self):
        handled = []
        with patch("actions.secretary._is_processed", return_value=False), \
             patch("actions.secretary._mark_processed", lambda fp: None):
            listener = SecretaryListener(
                on_message=lambda s, t: handled.append((s, t)))
            listener._handle_new("Meta AI", "2 + 2 = 4")
        self.assertEqual(handled, [])


class MetaAiDraftTests(unittest.TestCase):
    """Secretary auto-replies can be drafted by Meta AI (WhatsApp's built-in
    assistant) in the boss's casual register — with the deterministic draft
    as the instant fallback for every failure mode, and a guardrail so a
    drafted reply can never commit the boss to anything."""

    def _draft_patches(self):
        return [
            patch("actions.secretary._load_cfg",
                  return_value={"boss_name": "Boss"}),
            patch("actions.secretary._meta_drafts_enabled", return_value=True),
        ]

    def test_drafts_enabled_config(self):
        from actions.secretary import _meta_drafts_enabled
        with patch("actions.secretary._load_cfg", return_value={}):
            self.assertTrue(_meta_drafts_enabled())       # default on
        with patch("actions.secretary._load_cfg",
                   return_value={"secretary_meta_ai_drafts": None}):
            self.assertTrue(_meta_drafts_enabled())       # null = on
        with patch("actions.secretary._load_cfg",
                   return_value={"secretary_meta_ai_drafts": False}):
            self.assertFalse(_meta_drafts_enabled())
        with patch("actions.secretary._load_cfg",
                   return_value={"secretary_meta_ai_drafts": True}):
            self.assertTrue(_meta_drafts_enabled())

    def test_draft_prompt_has_sender_message_and_guardrails(self):
        from actions.secretary import _meta_draft_prompt
        with patch("actions.secretary._load_cfg",
                   return_value={"boss_name": "Boss"}), \
             patch("actions.secretary._pet_names_map", return_value={}):
            p = _meta_draft_prompt("Mom", "hi!", "deterministic draft")
        self.assertIn("Mom", p)
        self.assertIn("hi!", p)
        self.assertIn("My boss", p)          # unknown sender → neutral name
        self.assertNotIn("Boss's", p)        # configured name not used for replies
        self.assertIn("never confirm", p.lower())
        self.assertIn("commit", p.lower())
        self.assertIn("under 25 words", p)
        self.assertIn("deterministic draft", p)

    def test_over_commit_detection(self):
        from actions.secretary import _meta_draft_over_commits
        self.assertTrue(_meta_draft_over_commits("count me in!"))
        self.assertTrue(_meta_draft_over_commits("Sure, see you at 2pm"))
        self.assertTrue(_meta_draft_over_commits("I'm in, confirmed"))
        self.assertFalse(_meta_draft_over_commits("Poa sawa! 😄"))
        self.assertFalse(_meta_draft_over_commits("I'll pass it on to the boss"))
        self.assertFalse(_meta_draft_over_commits(""))

    def test_meta_draft_uses_meta_ai_when_available(self):
        from actions.secretary import _meta_draft
        with contextlib.ExitStack() as stack:
            for p in self._draft_patches():
                stack.enter_context(p)
            stack.enter_context(patch(
                "actions.meta_ai._ask_bridge",
                return_value="Poa sawa! 😄 I'll let the boss know."))
            out = _meta_draft("Mom", "hi!", "deterministic")
        self.assertEqual(out, "Poa sawa! 😄 I'll let the boss know.")

    def test_meta_draft_disabled_uses_deterministic(self):
        from actions.secretary import _meta_draft
        with patch("actions.secretary._meta_drafts_enabled",
                   return_value=False):
            out = _meta_draft("Mom", "hi", "deterministic")
        self.assertEqual(out, "deterministic")

    def test_meta_draft_fallback_on_bridge_failure(self):
        from actions.secretary import _meta_draft
        with contextlib.ExitStack() as stack:
            for p in self._draft_patches():
                stack.enter_context(p)
            stack.enter_context(patch(
                "actions.meta_ai._ask_bridge",
                side_effect=RuntimeError("not linked")))
            out = _meta_draft("Mom", "hi", "deterministic")
        self.assertEqual(out, "deterministic")

    def test_meta_draft_rejects_commitment(self):
        from actions.secretary import _meta_draft
        with contextlib.ExitStack() as stack:
            for p in self._draft_patches():
                stack.enter_context(p)
            stack.enter_context(patch(
                "actions.meta_ai._ask_bridge",
                return_value="Yes! See you at 2pm!"))
            out = _meta_draft("Mom", "lunch at 2?", "deterministic")
        self.assertEqual(out, "deterministic")

    def test_meta_draft_burst_cap(self):
        # after 3 drafts in 60s, replies go instant (deterministic) until the
        # burst clears — the AI must not become the reply bottleneck
        from actions.secretary import _meta_draft, _draft_times
        _draft_times[:] = []
        calls = {"n": 0}

        def _fake_bridge(*a, **k):
            calls["n"] += 1
            return "sawa 😄"

        try:
            with contextlib.ExitStack() as stack:
                for p in self._draft_patches():
                    stack.enter_context(p)
                stack.enter_context(patch(
                    "actions.meta_ai._ask_bridge", side_effect=_fake_bridge))
                for i in range(4):
                    out = _meta_draft("Mom", "hi", "deterministic")
            self.assertEqual(calls["n"], 3)          # 4th draft was capped
            self.assertEqual(out, "deterministic")
        finally:
            _draft_times[:] = []

    def test_meta_draft_blank_or_huge_reply_falls_back(self):
        from actions.secretary import _meta_draft
        with contextlib.ExitStack() as stack:
            for p in self._draft_patches():
                stack.enter_context(p)
            stack.enter_context(patch("actions.meta_ai._ask_bridge",
                                      return_value="   "))
            self.assertEqual(_meta_draft("Mom", "hi", "deterministic"),
                             "deterministic")
        with contextlib.ExitStack() as stack:
            for p in self._draft_patches():
                stack.enter_context(p)
            stack.enter_context(patch("actions.meta_ai._ask_bridge",
                                      return_value="x" * 700))
            self.assertEqual(_meta_draft("Mom", "hi", "deterministic"),
                             "deterministic")

    def test_handle_message_sends_meta_ai_draft(self):
        # end-to-end: handle_message sends the Meta AI draft through send_fn
        from actions.secretary import handle_message
        state = {"conversations": {}, "inbox": []}
        sent = []
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("actions.secretary.is_enabled",
                                      return_value=True))
            stack.enter_context(patch("actions.secretary._state",
                                      return_value=state))
            stack.enter_context(patch("actions.secretary._save_state",
                                      lambda s: None))
            stack.enter_context(patch("actions.secretary._load_cfg",
                                      return_value={"boss_name": "Boss"}))
            stack.enter_context(patch(
                "actions.secretary._meta_draft",
                side_effect=lambda s, m, d, media_kind=None:
                "META-DRAFTED: " + m))
            out = handle_message("Mom", "hey how are you",
                                 send_fn=lambda s, t: sent.append((s, t)) or "ok")
        self.assertIn("Replied to Mom", out)
        self.assertEqual(sent[0][1], "META-DRAFTED: hey how are you")

    def test_escalation_never_drafted(self):
        from actions.secretary import handle_message
        state = {"conversations": {}, "inbox": []}
        drafted = []
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("actions.secretary.is_enabled",
                                      return_value=True))
            stack.enter_context(patch("actions.secretary._state",
                                      return_value=state))
            stack.enter_context(patch("actions.secretary._save_state",
                                      lambda s: None))
            stack.enter_context(patch("actions.secretary._load_cfg",
                                      return_value={"boss_name": "Boss"}))
            stack.enter_context(patch(
                "actions.secretary._meta_draft",
                side_effect=lambda s, m, d, media_kind=None:
                drafted.append(m) or d))
            out = handle_message("Mom", "URGENT — call me about the contract")
        self.assertIn("ESCALATED", out)
        self.assertEqual(drafted, [])   # escalations go to the inbox, never AI-drafted


class MediaReplyTests(unittest.TestCase):
    """Media messages (photo/video/voice note/document/sticker/...) get a
    reply that fits the TYPE — Meta AI drafts it when enabled, the
    deterministic media acknowledgment otherwise. Reactions/recalls/system
    notices get NO reply at all."""

    def test_looks_like_media_preview(self):
        from actions.secretary import _looks_like_media_preview
        # media labels
        self.assertTrue(_looks_like_media_preview("Document"))
        self.assertTrue(_looks_like_media_preview("Photo"))
        self.assertTrue(_looks_like_media_preview("wds-ic-documentDocument"))
        # filenames (the self-chat shows e.g. "CV update English.docx")
        self.assertTrue(_looks_like_media_preview("CV update English.docx"))
        self.assertTrue(_looks_like_media_preview("notes.pdf"))
        self.assertTrue(_looks_like_media_preview("budget.xlsx"))
        # ordinary text / commands are not media
        self.assertFalse(_looks_like_media_preview("Hi"))
        self.assertFalse(_looks_like_media_preview("summarize this"))
        self.assertFalse(_looks_like_media_preview("what's on my screen"))
        self.assertFalse(_looks_like_media_preview("Reacted to: \"ok\""))
        self.assertFalse(_looks_like_media_preview(""))

    def test_media_kind_of_labels(self):
        from actions.secretary import _media_kind_of
        self.assertEqual(_media_kind_of("wds-ic-readic-videocamVideo"), "video")
        self.assertEqual(_media_kind_of("wds-ic-stickerSticker"), "sticker")
        self.assertEqual(_media_kind_of("wds-ic-view-oncePhoto"), "photo")
        self.assertEqual(_media_kind_of("Photo"), "photo")
        self.assertEqual(_media_kind_of("Video, no caption"), "video")
        self.assertEqual(_media_kind_of("Voice message"), "voice note")
        self.assertEqual(_media_kind_of("Voice note"), "voice note")
        self.assertEqual(_media_kind_of("Document"), "document")
        self.assertEqual(_media_kind_of("GIF"), "gif")
        self.assertEqual(_media_kind_of("Location"), "location")
        self.assertEqual(_media_kind_of("Poll"), "poll")
        self.assertEqual(_media_kind_of("Image"), "photo")

    def test_media_kind_of_skip_and_text(self):
        from actions.secretary import _media_kind_of
        self.assertEqual(_media_kind_of("Reacted  to: \"Yes yes try it\""), "skip")
        self.assertEqual(_media_kind_of("recalledYou deleted this message"), "skip")
        self.assertEqual(_media_kind_of("You scheduled this message"), "skip")
        self.assertEqual(_media_kind_of("You recalled this message"), "skip")
        # real text must NOT be mistaken for media
        self.assertIsNone(_media_kind_of("send me that photo please"))
        self.assertIsNone(_media_kind_of("You're welcome"))
        self.assertIsNone(_media_kind_of("Hi"))
        self.assertIsNone(_media_kind_of("(new message)"))
        self.assertIsNone(_media_kind_of(""))

    def test_draft_media_reply(self):
        from actions.secretary import _draft_media_reply
        with patch("actions.secretary._load_cfg",
                   return_value={"boss_name": "Boss"}):
            self.assertIn("stunning", _draft_media_reply("Mom", "photo"))
            self.assertIn("voice note", _draft_media_reply("Mom", "voice note"))
            self.assertIn("document", _draft_media_reply("Mom", "document").lower())

    def test_meta_draft_uses_media_prompt_for_media(self):
        from actions.secretary import _meta_draft
        prompts = []

        def fake_bridge(prompt, timeout=60):
            prompts.append(prompt)
            return "Wow, stunning photo! 😍"

        with patch("actions.secretary._load_cfg",
                   return_value={"boss_name": "Boss"}), \
             patch("actions.secretary._meta_drafts_enabled", return_value=True), \
             patch("actions.meta_ai._ask_bridge", side_effect=fake_bridge):
            out = _meta_draft("Mom", "wds-ic-readic-videocamVideo",
                              "det-fallback", media_kind="video")
        self.assertEqual(out, "Wow, stunning photo! 😍")
        self.assertIn("sent a video", prompts[0])
        # the media prompt replaces the text prompt (no "Their message:")
        self.assertNotIn("Their message:", prompts[0])
        self.assertIn("claim to have seen the actual content", prompts[0])

    def test_handle_message_media_uses_media_draft(self):
        from actions.secretary import handle_message
        state = {"conversations": {}, "inbox": []}
        seen = {}
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("actions.secretary.is_enabled",
                                      return_value=True))
            stack.enter_context(patch("actions.secretary._state",
                                      return_value=state))
            stack.enter_context(patch("actions.secretary._save_state",
                                      lambda s: None))
            stack.enter_context(patch("actions.secretary._load_cfg",
                                      return_value={"boss_name": "Boss"}))
            stack.enter_context(patch(
                "actions.secretary._meta_draft",
                side_effect=lambda s, m, d, media_kind=None:
                seen.update(sender=s, kind=media_kind, deterministic=d) or "sent!"))
            out = handle_message("Mom", "wds-ic-readic-videocamVideo",
                                 send_fn=lambda s, t: "ok")
        self.assertIn("Replied to Mom", out)
        self.assertEqual(seen["kind"], "video")
        self.assertIn("Nice one", seen["deterministic"])   # media fallback, not text draft

    def test_handle_message_skips_reactions(self):
        from actions.secretary import handle_message
        state = {"conversations": {}, "inbox": []}
        sent = []
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("actions.secretary.is_enabled",
                                      return_value=True))
            stack.enter_context(patch("actions.secretary._state",
                                      return_value=state))
            stack.enter_context(patch("actions.secretary._save_state",
                                      lambda s: None))
            stack.enter_context(patch("actions.secretary._load_cfg",
                                      return_value={"boss_name": "Boss"}))
            out = handle_message("Mom", "Reacted  to: \"Yes yes\"",
                                 send_fn=lambda s, t: sent.append((s, t)) or "ok")
        self.assertIn("No reply needed", out)
        self.assertEqual(sent, [])

    # ── forward-to-Meta-AI (real content analysis) ──────────────────────────

    def _forward_patches(self, state, drafts=True):
        return [
            patch("actions.secretary.is_enabled", return_value=True),
            patch("actions.secretary._state", return_value=state),
            patch("actions.secretary._save_state", lambda s: None),
            patch("actions.secretary._load_cfg",
                   return_value={"boss_name": "Boss"}),
            patch("actions.secretary._meta_drafts_enabled",
                   return_value=drafts),
        ]

    def test_forwardable_media_set(self):
        from actions.secretary import _FORWARDABLE_MEDIA
        self.assertEqual(_FORWARDABLE_MEDIA,
                         {"photo", "video", "gif", "document", "media"})

    def test_handle_message_forwards_media_to_meta_ai(self):
        # forwardable media + drafts on + forwarder → ack goes out FIRST,
        # then Meta AI's real analysis of the file as a follow-up.
        from actions.secretary import handle_message
        state = {"conversations": {}, "inbox": []}
        sent = []
        forwarded = []
        drafted = []
        with contextlib.ExitStack() as stack:
            for p in self._forward_patches(state):
                stack.enter_context(p)
            stack.enter_context(patch(
                "actions.secretary._meta_draft",
                side_effect=lambda s, m, d, media_kind=None:
                drafted.append(m) or d))
            out = handle_message(
                "Mom", "Photo",
                send_fn=lambda s, t: sent.append((s, t)) or "ok",
                forward_fn=lambda s: forwarded.append(s) or "Wow, stunning photo! 😍 A beautiful sunset.")
        self.assertIn("forwarded the media to Meta AI", out)
        self.assertEqual(forwarded, ["Mom"])
        self.assertEqual(len(sent), 2)          # ack, then the analysis
        self.assertEqual(sent[0][0], "Mom")
        self.assertIn("stunning photo", sent[0][1].lower())  # type ack first
        self.assertEqual(sent[1][1], "Wow, stunning photo! 😍 A beautiful sunset.")
        self.assertEqual(drafted, [])           # real analysis replaced the type-draft

    def test_handle_message_forwards_document(self):
        from actions.secretary import handle_message
        state = {"conversations": {}, "inbox": []}
        sent = []
        with contextlib.ExitStack() as stack:
            for p in self._forward_patches(state):
                stack.enter_context(p)
            out = handle_message(
                "Mom", "Document",
                send_fn=lambda s, t: sent.append((s, t)) or "ok",
                forward_fn=lambda s: "I got the document — it's the CV with a summary.")
        self.assertIn("1. ", out)
        self.assertIn("2. I got the document", out)
        self.assertEqual(len(sent), 2)

    def test_handle_message_no_forward_without_drafts(self):
        # drafts OFF → the deterministic ack only; forward_fn never called
        from actions.secretary import handle_message
        state = {"conversations": {}, "inbox": []}
        sent = []
        with contextlib.ExitStack() as stack:
            for p in self._forward_patches(state, drafts=False):
                stack.enter_context(p)
            out = handle_message(
                "Mom", "Photo",
                send_fn=lambda s, t: sent.append((s, t)) or "ok",
                forward_fn=lambda s: (_ for _ in ()).throw(
                    AssertionError("forward_fn must not run")))
        self.assertIn("Replied to Mom", out)
        self.assertEqual(len(sent), 1)
        self.assertIn("stunning photo", sent[0][1].lower())

    def test_handle_message_forward_failure_falls_back_to_ack(self):
        # forward fails (bridge error / Meta AI timeout) → the ack already
        # went out; a clear message returns, nothing crashes.
        from actions.secretary import handle_message
        state = {"conversations": {}, "inbox": []}
        sent = []
        with contextlib.ExitStack() as stack:
            for p in self._forward_patches(state):
                stack.enter_context(p)
            out = handle_message(
                "Mom", "Photo",
                send_fn=lambda s, t: sent.append((s, t)) or "ok",
                forward_fn=lambda s: (_ for _ in ()).throw(
                    RuntimeError("Meta AI didn't finish replying")))
        self.assertIn("asking Meta AI to analyze it failed", out)
        self.assertEqual(len(sent), 1)          # the ack is all that went out

    def test_handle_message_forward_empty_analysis(self):
        # Meta AI returns nothing → the ack stands, no empty follow-up sent
        from actions.secretary import handle_message
        state = {"conversations": {}, "inbox": []}
        sent = []
        with contextlib.ExitStack() as stack:
            for p in self._forward_patches(state):
                stack.enter_context(p)
            out = handle_message(
                "Mom", "Photo",
                send_fn=lambda s, t: sent.append((s, t)) or "ok",
                forward_fn=lambda s: "   ")
        self.assertIn("Meta AI gave nothing back", out)
        self.assertEqual(len(sent), 1)

    def test_handle_message_non_forwardable_media_keeps_old_path(self):
        # sticker / voice note can't be forwarded → the type-aware
        # _meta_draft path stays; forward_fn never called.
        from actions.secretary import handle_message
        state = {"conversations": {}, "inbox": []}
        seen = {}
        with contextlib.ExitStack() as stack:
            for p in self._forward_patches(state):
                stack.enter_context(p)
            stack.enter_context(patch(
                "actions.secretary._meta_draft",
                side_effect=lambda s, m, d, media_kind=None:
                seen.update(kind=media_kind) or "sticker reply"))
            out = handle_message(
                "Mom", "wds-ic-stickerSticker",
                send_fn=lambda s, t: "ok",
                forward_fn=lambda s: (_ for _ in ()).throw(
                    AssertionError("forward_fn must not run for stickers")))
        self.assertIn("Replied to Mom", out)
        self.assertEqual(seen.get("kind"), "sticker")

    def test_handle_message_no_forward_fn_keeps_old_path(self):
        # no forwarder wired (foreground/CLI use) → unchanged behavior
        from actions.secretary import handle_message
        state = {"conversations": {}, "inbox": []}
        seen = {}
        with contextlib.ExitStack() as stack:
            for p in self._forward_patches(state):
                stack.enter_context(p)
            stack.enter_context(patch(
                "actions.secretary._meta_draft",
                side_effect=lambda s, m, d, media_kind=None:
                seen.update(kind=media_kind) or "video reply"))
            out = handle_message("Mom", "Video",
                                 send_fn=lambda s, t: "ok")
        self.assertIn("Replied to Mom", out)
        self.assertEqual(seen.get("kind"), "video")

    def test_default_on_message_wires_forward_fn(self):
        # the monitor's default hook passes the bridge forwarder, so media
        # reaching the secretary gets real Meta AI analysis, not just a type
        # draft.
        from actions.secretary_listener import SecretaryListener
        calls = []

        class FakeBridge:
            def send_message(self, s, t):
                calls.append(("send", s, t))
                return "ok"

            def forward_last_media_to_meta_ai(self, s):
                calls.append(("forward", s))
                return "analysis"

        state = {"conversations": {}, "inbox": []}
        listener = SecretaryListener(bridge=FakeBridge())
        with contextlib.ExitStack() as stack:
            for p in self._forward_patches(state):
                stack.enter_context(p)
            out = listener._default_on_message("Mom", "Photo")
        self.assertIn("forwarded the media to Meta AI", out)
        self.assertEqual(calls[0][0], "send")      # ack first
        self.assertEqual(calls[1][0], "forward")   # then the forward
        self.assertEqual(calls[2][0], "send")      # then the analysis reply

    def test_sweep_skips_reactions_but_handles_media(self):
        handled = []
        items = [
            {"sender": "Mom", "preview": "wds-ic-stickerSticker", "time": "22:00"},
            {"sender": "Dad", "preview": "Reacted  to: \"ok\"", "time": "22:01"},
        ]
        with patch("actions.secretary._is_processed_many", return_value=set()), \
             patch("actions.secretary._mark_processed_many", lambda fps: None):
            listener = SecretaryListener(
                on_message=lambda s, t: handled.append((s, t)))
            listener._handle_sweep(items)
        # the sticker is handled (media), the reaction is skipped entirely
        self.assertEqual(handled, [("Mom", "wds-ic-stickerSticker")])


class PetNameTests(unittest.TestCase):
    """Pet names — what each contact calls the boss (wife → 'baby').
    Discovered ONCE by scanning existing chats, stored as a static map, and
    looked up per reply from that map (never re-scanned per draft). Unknown
    senders get 'My boss'."""

    def test_vocative_terms_in_positions(self):
        from actions.secretary import _vocative_terms_in
        # message-initial: bare start (casual WhatsApp has no comma) or
        # before punctuation
        self.assertEqual(_vocative_terms_in("baby, how are you?"), {"baby"})
        self.assertEqual(_vocative_terms_in("baby! where are you"), {"baby"})
        self.assertEqual(_vocative_terms_in("Baby I miss you"), {"baby"})
        self.assertEqual(_vocative_terms_in("mzee uko wapi"), {"mzee"})
        # message-final after punctuation/space
        self.assertEqual(_vocative_terms_in("miss you baby"), {"baby"})
        self.assertEqual(_vocative_terms_in("how are you, love!"), {"love"})
        # multi-word term preferred
        self.assertEqual(_vocative_terms_in("my love, I am here"),
                         {"my love"})
        # phrase words are NOT vocatives: "love you" / formal "Dear John,"
        self.assertEqual(_vocative_terms_in("love you"), set())
        self.assertEqual(_vocative_terms_in("Dear John, please send it"),
                         set())
        # greetings are noise, not names
        self.assertEqual(_vocative_terms_in("hey, how are you"), set())
        self.assertEqual(_vocative_terms_in("ok, thanks"), set())
        self.assertEqual(_vocative_terms_in(""), set())

    def test_extract_vocative_most_frequent(self):
        from actions.secretary import _extract_vocative
        msgs = ["baby, come home", "miss you baby", "baby when are you back?"]
        self.assertEqual(_extract_vocative(msgs), "baby")

    def test_extract_vocative_none(self):
        from actions.secretary import _extract_vocative
        self.assertIsNone(_extract_vocative([]))
        self.assertIsNone(_extract_vocative(["how are you", "see you soon"]))
        self.assertIsNone(_extract_vocative(None))

    def test_extract_vocative_excludes_sender_own_name(self):
        from actions.secretary import _extract_vocative
        # the chat title is 'Mom' — a message starting "Mom, ..." is not the
        # sender calling the boss "Mom"
        msgs = ["Mom, are you there?", "Mom, please reply"]
        self.assertIsNone(_extract_vocative(msgs, sender="Mom"))

    def test_extract_vocative_novel_nickname(self):
        # a short message-initial word used in ≥3 messages (not a stopword)
        # is a nickname the dictionary can't list
        from actions.secretary import _extract_vocative
        msgs = ["Ziii niweke ata za lunch", "Ziii uko wapi?",
                "Ziii nataka tuzungumze", "hawataweza kukupenda"]
        self.assertEqual(_extract_vocative(msgs), "ziii")
        # a once-off start word is not a name
        self.assertIsNone(_extract_vocative(
            ["Hawataweza kukupenda", "hello", "sawa"], sender="Mom"))

    def test_extract_vocative_phrase_words_not_names(self):
        from actions.secretary import _extract_vocative
        # "love you" is an expression, not an address term — never picked
        self.assertIsNone(_extract_vocative(["love you so much",
                                             "love you too"]))

    def test_pet_name_for_static_map(self):
        from actions.secretary import _pet_name_for
        with patch("actions.secretary._pet_names_map",
                   return_value={"Mom": "baby", "もま かて.": "mrembo"}):
            self.assertEqual(_pet_name_for("Mom"), "baby")
            self.assertEqual(_pet_name_for("mom"), "baby")        # case-fold
            self.assertEqual(_pet_name_for("Mum 母"), "My boss")    # unknown
            self.assertEqual(_pet_name_for(""), "My boss")
            self.assertEqual(_pet_name_for("もま かて."), "mrembo")

    def test_pet_name_for_substring_match(self):
        # contact titles can carry extra glyphs (e.g. "Mum 母") — a stored
        # entry whose title contains (or is contained in) the sender wins
        from actions.secretary import _pet_name_for
        with patch("actions.secretary._pet_names_map",
                   return_value={"Omoke Jr": "boss"}):
            self.assertEqual(_pet_name_for("Omoke Jr アニメ"), "boss")

    def test_pet_name_for_novel_capitalized_dict_lowercase(self):
        from actions.secretary import _pet_name_for
        # dictionary terms read naturally lowercase; novel nicknames (from
        # the LLM pass) are capitalized in a sentence
        with patch("actions.secretary._pet_names_map",
                   return_value={"Mom": "baby", "Wife": "ziii"}):
            self.assertEqual(_pet_name_for("Mom"), "baby")
            self.assertEqual(_pet_name_for("Wife"), "Ziii")

    def test_draft_media_reply_uses_pet_name(self):
        from actions.secretary import _draft_media_reply
        with patch("actions.secretary._load_cfg",
                   return_value={"boss_name": "Boss"}), \
             patch("actions.secretary._pet_names_map",
                   return_value={"Mom": "baby"}):
            out = _draft_media_reply("Mom", "photo")
        self.assertIn("make sure baby sees it", out)
        self.assertNotIn("Boss", out)

    def test_draft_media_reply_unknown_uses_my_boss(self):
        from actions.secretary import _draft_media_reply
        with patch("actions.secretary._load_cfg",
                   return_value={"boss_name": "Boss"}), \
             patch("actions.secretary._pet_names_map", return_value={}):
            out = _draft_media_reply("Mom", "photo")
        self.assertIn("make sure My boss sees it", out)
        self.assertNotIn("Boss", out)

    def test_draft_reply_sig_uses_pet_name(self):
        from actions.secretary import _draft_reply
        with patch("actions.secretary._load_cfg",
                   return_value={"boss_name": "Boss"}), \
             patch("actions.secretary._pet_names_map",
                   return_value={"Mom": "baby"}):
            out = _draft_reply("Mom", "hi there!")
        self.assertIn("on behalf of baby", out)
        self.assertIn("make sure baby sees it", out)

    def test_meta_prompt_uses_pet_name(self):
        from actions.secretary import _meta_draft_prompt
        with patch("actions.secretary._load_cfg",
                   return_value={"boss_name": "Boss"}), \
             patch("actions.secretary._pet_names_map",
                   return_value={"Mom": "baby"}):
            p = _meta_draft_prompt("Mom", "hi", "deterministic")
        self.assertIn("You are baby's WhatsApp assistant", p)

    def test_scan_pet_names_persists_map(self):
        from actions.secretary import scan_pet_names
        state = {"conversations": {}, "inbox": [], "pet_names": {}}
        saved = {}

        class FakeBridge:
            def list_chat_titles(self):
                return ["Mom", "Meta AI", "ALIXON"]

            def read_recent_incoming(self, title, limit=25):
                if title == "Mom":
                    return ["baby, where are you?", "miss you baby"]
                if title == "ALIXON":
                    return ["see you tomorrow"]
                return []

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("actions.secretary._state",
                                      return_value=state))
            stack.enter_context(patch("actions.secretary._save_state",
                                      lambda st: saved.update(st)))
            out = scan_pet_names(bridge=FakeBridge())
        self.assertIn("Mom → 'baby'", out)
        self.assertNotIn("Meta AI", out)          # never scanned
        self.assertEqual(saved["pet_names"].get("Mom"), "baby")
        self.assertIn("pet_names_scanned_at", saved)

    def test_scan_pet_names_llm_refines(self):
        # the dictionary misses a novel nickname; the one-time LLM pass
        # catches it (deterministic + LLM both persisted)
        from actions.secretary import scan_pet_names
        state = {"conversations": {}, "inbox": [], "pet_names": {}}
        saved = {}

        class FakeBridge:
            def list_chat_titles(self):
                return ["Wife"]

            def read_recent_incoming(self, title, limit=25):
                return ["Ziii niweke ata za lunch"]

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("actions.secretary._state",
                                      return_value=state))
            stack.enter_context(patch("actions.secretary._save_state",
                                      lambda st: saved.update(st)))
            stack.enter_context(patch("actions.secretary._llm_scan_chat",
                                      return_value="ziii"))
            out = scan_pet_names(bridge=FakeBridge())
        self.assertIn("Wife → 'ziii'", out)
        self.assertEqual(saved["pet_names"].get("Wife"), "ziii")

    def test_scan_pet_names_llm_off(self):
        # llm=False skips the brain pass entirely (free background scan)
        from actions.secretary import scan_pet_names
        state = {"conversations": {}, "inbox": []}
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("actions.secretary._state",
                                      return_value=state))
            stack.enter_context(patch("actions.secretary._save_state",
                                      lambda st: None))
            stack.enter_context(patch("actions.secretary._llm_scan_chat",
                                      side_effect=AssertionError("no LLM")))

            class FakeBridge:
                def list_chat_titles(self):
                    return ["Wife"]

                def read_recent_incoming(self, title, limit=25):
                    return ["Ziii niweke ata za lunch"]

            out = scan_pet_names(bridge=FakeBridge(), llm=False)
        self.assertIn("No address terms found", out)

    def test_scan_pet_names_none_found(self):
        from actions.secretary import scan_pet_names
        state = {"conversations": {}, "inbox": []}
        saved = {}

        class FakeBridge:
            def list_chat_titles(self):
                return ["ALIXON"]

            def read_recent_incoming(self, title, limit=25):
                return ["see you tomorrow"]

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("actions.secretary._state",
                                      return_value=state))
            stack.enter_context(patch("actions.secretary._save_state",
                                      lambda st: saved.update(st)))
            out = scan_pet_names(bridge=FakeBridge())
        self.assertIn("No address terms found", out)
        self.assertEqual(saved["pet_names"], {})

    def test_scan_pet_names_skips_static_senders(self):
        """User-approved names (pet_names_static) are never re-derived or
        overwritten: the wife stays 'Junior' even though the scan would find
        'ziii' in her chat, while other chats are still scanned."""
        from actions.secretary import scan_pet_names
        state = {"conversations": {}, "inbox": [],
                 "pet_names": {"😻もま かて": "Junior"},
                 "pet_names_static": ["😻もま かて"]}
        saved = {}
        scanned = []

        class FakeBridge:
            def list_chat_titles(self):
                return ["😻もま かて", "Mom"]

            def read_recent_incoming(self, title, limit=25):
                scanned.append(title)
                if title == "Mom":
                    return ["baby, where are you?"]
                return ["Ziii niweke ata za lunch"]

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("actions.secretary._state",
                                      return_value=state))
            stack.enter_context(patch("actions.secretary._save_state",
                                      lambda st: saved.update(st)))
            stack.enter_context(patch("actions.secretary._llm_scan_chat",
                                      return_value=None))
            out = scan_pet_names(bridge=FakeBridge())
        self.assertNotIn("😻もま かて", scanned)     # static chat never opened
        self.assertEqual(saved["pet_names"]["😻もま かて"], "Junior")
        self.assertEqual(saved["pet_names"]["Mom"], "baby")
        self.assertIn("Mom → 'baby'", out)

    def test_pet_names_scan_needed(self):
        from actions.secretary import _pet_names_scan_needed
        from datetime import datetime, timedelta
        # never scanned → needed
        with patch("actions.secretary._state",
                   return_value={"conversations": {}}), \
             patch("actions.secretary._load_cfg", return_value={}):
            self.assertTrue(_pet_names_scan_needed())
        # scanned 1 hour ago → not needed
        old = (datetime.now() - timedelta(hours=1)).isoformat()
        with patch("actions.secretary._state",
                   return_value={"pet_names_scanned_at": old}), \
             patch("actions.secretary._load_cfg", return_value={}):
            self.assertFalse(_pet_names_scan_needed())
        # scanned 2 days ago → needed again
        stale = (datetime.now() - timedelta(days=2)).isoformat()
        with patch("actions.secretary._state",
                   return_value={"pet_names_scanned_at": stale}), \
             patch("actions.secretary._load_cfg", return_value={}):
            self.assertTrue(_pet_names_scan_needed())
        # feature disabled → never needed
        with patch("actions.secretary._state",
                   return_value={"conversations": {}}), \
             patch("actions.secretary._load_cfg",
                   return_value={"secretary_pet_names": False}):
            self.assertFalse(_pet_names_scan_needed())

    def test_maybe_scan_pet_names_runs_once(self):
        from actions.secretary_listener import SecretaryListener
        started = []
        listener = SecretaryListener()
        with patch("actions.secretary._pet_names_scan_needed",
                   return_value=True), \
             patch("actions.secretary.is_enabled", return_value=True), \
             patch("actions.secretary.scan_pet_names",
                   return_value="scan done"), \
             patch("threading.Thread.start",
                   side_effect=lambda: started.append(1)):
            listener._maybe_scan_pet_names(object())
            listener._maybe_scan_pet_names(object())
        self.assertEqual(len(started), 1)          # one background scan per run


class SelfChatOnlySweepTests(unittest.TestCase):
    """With secretary mode OFF the monitor still runs — but only the
    always-on remote dashboard (the boss's self-chat) is handled. Third-party
    chats are left completely untouched: not replied to, not fingerprinted,
    not marked processed — so turning secretary back on later handles their
    backlog normally."""

    def _run_sweep(self, items, secretary_on):
        from actions.secretary_listener import SecretaryListener
        handled = []
        self_replies = []
        marked = []
        with patch("actions.secretary.is_enabled", return_value=secretary_on), \
             patch("actions.secretary._is_processed_many", return_value=set()), \
             patch("actions.secretary._mark_processed_many",
                   side_effect=lambda fps: marked.extend(fps)), \
             patch("actions.send_message.send_message", return_value="ok"):
            listener = SecretaryListener(
                on_message=lambda s, t: handled.append((s, t)),
                on_self_chat=lambda s, t: self_replies.append((s, t)) or "")
            listener._handle_sweep(items)
        return handled, self_replies, marked

    def test_off_handles_only_self_chat(self):
        items = [
            {"sender": "Omoke Jr", "preview": "system status", "time": "22:00"},
            {"sender": "Mom", "preview": "URGENT call me", "time": "22:01"},
        ]
        handled, replies, marked = self._run_sweep(items, secretary_on=False)
        self.assertEqual(replies, [("Omoke Jr", "system status")])
        self.assertEqual(handled, [])       # Mom untouched
        self.assertEqual(len(marked), 1)    # only the self-chat fingerprinted

    def test_on_handles_everything(self):
        items = [
            {"sender": "Omoke Jr", "preview": "system status", "time": "22:00"},
            {"sender": "Mom", "preview": "hey", "time": "22:01"},
        ]
        handled, replies, marked = self._run_sweep(items, secretary_on=True)
        self.assertEqual(replies, [("Omoke Jr", "system status")])
        self.assertEqual(handled, [("Mom", "hey")])
        self.assertEqual(len(marked), 2)

    def test_off_leaves_call_rows_untouched(self):
        items = [{"sender": "Bri", "preview": "Missed voice call",
                  "time": "22:00", "call": True}]
        handled, replies, marked = self._run_sweep(items, secretary_on=False)
        self.assertEqual(handled, [])
        self.assertEqual(replies, [])
        self.assertEqual(marked, [])   # not even fingerprinted


class UnreadFilterLoopTests(unittest.TestCase):
    """The monitor applies WhatsApp's own Unread filter (unread-only pane)
    once it's logged in, and re-reads the pane so even the first handled
    sweep only sees unread chats."""

    def _run_loop_until(self, listener, predicate, timeout=8.0):
        t = threading.Thread(target=listener._loop, daemon=True)
        t.start()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                break
            time.sleep(0.05)
        listener._stop_event.set()
        t.join(timeout=3.0)
        self.assertFalse(t.is_alive())

    def test_filter_applied_before_first_sweep_and_pane_reread(self):
        events = []

        class FakeBridge:
            def start(self):
                pass

            def poll_unread(self):
                events.append("poll")
                return []

            def needs_qr(self):
                return False

            def ensure_unread_filter(self):
                events.append("filter")
                return True

            def poll_calls(self):
                return []

        listener = SecretaryListener(bridge=FakeBridge(), poll_seconds=0.2)
        self._run_loop_until(listener, lambda: len(events) >= 3)
        # order: first poll (login gate), then filter, then re-poll of the
        # filtered pane — the first handled sweep only sees unread chats
        self.assertEqual(events[:3], ["poll", "filter", "poll"])
        self.assertTrue(listener._filter_applied)

    def test_filter_failure_does_not_break_loop(self):
        class FakeBridge:
            def start(self):
                pass

            def poll_unread(self):
                return []

            def needs_qr(self):
                return False

            def ensure_unread_filter(self):
                raise RuntimeError("no filter control")

            def poll_calls(self):
                return []

        listener = SecretaryListener(bridge=FakeBridge(), poll_seconds=0.2)
        self._run_loop_until(listener, lambda: listener._state == "ok",
                             timeout=6.0)
        self.assertTrue(listener._state == "ok")
        self.assertFalse(listener._filter_applied)   # keeps polling full pane


if __name__ == "__main__":
    unittest.main()
