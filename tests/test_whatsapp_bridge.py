"""WhatsAppBridge _submit must never hang its caller: a wedged browser
surfaces as a TimeoutError (daktari), and worker exceptions are re-raised.
The module imports cheaply (playwright is deferred to first use)."""

import threading
import time
import unittest
import unittest.mock

from actions.whatsapp_bridge import WhatsAppBridge


class SubmitBoundedTests(unittest.TestCase):
    """Bounded waits on the bridge's single Playwright thread."""

    def test_submit_times_out_when_worker_is_stuck(self):
        bridge = WhatsAppBridge(headless=True)
        blocker = threading.Event()

        def stuck():
            blocker.wait(30)   # never completes within the test window
            return "unreachable"

        t0 = time.time()
        with self.assertRaises(TimeoutError):
            bridge._submit(stuck, timeout=0.3)
        self.assertLess(time.time() - t0, 5.0)
        # the stuck worker is abandoned (daemon thread), not awaited

    def test_submit_reraises_worker_exception(self):
        bridge = WhatsAppBridge(headless=True)

        def boom():
            raise RuntimeError("browser exploded")

        with self.assertRaisesRegex(RuntimeError, "browser exploded"):
            bridge._submit(boom, timeout=5.0)

    def test_submit_returns_result(self):
        bridge = WhatsAppBridge(headless=True)
        self.assertEqual(bridge._submit(lambda: 42, timeout=5.0), 42)


class ForwardMediaTests(unittest.TestCase):
    """forward_last_media_to_meta_ai: thread-dispatched, bounded, with the
    same login guards as every other bridge call. (The DOM flow itself is
    verified live against the real WhatsApp Web build — these tests pin the
    wrapper contract.)"""

    def test_forward_dispatches_and_returns_analysis(self):
        bridge = WhatsAppBridge(headless=True)
        with unittest.mock.patch.object(
                bridge, "_forward_last_media_to_meta_ai_impl",
                return_value="Nimeipata CV yako 💪"):
            out = bridge.forward_last_media_to_meta_ai("Omoke Jr", timeout=30)
        self.assertEqual(out, "Nimeipata CV yako 💪")

    def test_forward_raises_when_not_linked(self):
        bridge = WhatsAppBridge(headless=True)
        with unittest.mock.patch.object(bridge, "_ensure_alive",
                                        return_value=True), \
             unittest.mock.patch.object(bridge, "_is_logged_in_impl",
                                        return_value=False), \
             unittest.mock.patch.object(bridge, "_needs_qr_impl",
                                        return_value=True):
            with self.assertRaisesRegex(RuntimeError, "not linked"):
                bridge.forward_last_media_to_meta_ai("Omoke Jr", timeout=10)

    def test_forward_raises_when_bridge_unready(self):
        bridge = WhatsAppBridge(headless=True)
        with unittest.mock.patch.object(bridge, "_ensure_alive",
                                        return_value=False):
            with self.assertRaisesRegex(RuntimeError, "not ready"):
                bridge.forward_last_media_to_meta_ai("Omoke Jr", timeout=10)

    def test_forward_propagates_impl_failure(self):
        bridge = WhatsAppBridge(headless=True)
        with unittest.mock.patch.object(
                bridge, "_forward_last_media_to_meta_ai_impl",
                side_effect=RuntimeError("no incoming media message found")):
            with self.assertRaisesRegex(RuntimeError, "no incoming media"):
                bridge.forward_last_media_to_meta_ai("Omoke Jr", timeout=10)


class ChatIntrospectionTests(unittest.TestCase):
    """list_chat_titles / read_recent_incoming: the read-only helpers behind
    the pet-name scan — thread-dispatched with the same login guards."""

    def test_list_chat_titles_dispatches(self):
        bridge = WhatsAppBridge(headless=True)
        with unittest.mock.patch.object(
                bridge, "_list_chat_titles_impl",
                return_value=["Mom", "ALIXON"]):
            self.assertEqual(bridge.list_chat_titles(), ["Mom", "ALIXON"])

    def test_list_chat_titles_not_linked(self):
        bridge = WhatsAppBridge(headless=True)
        with unittest.mock.patch.object(bridge, "_ensure_alive",
                                        return_value=True), \
             unittest.mock.patch.object(bridge, "_is_logged_in_impl",
                                        return_value=False), \
             unittest.mock.patch.object(bridge, "_needs_qr_impl",
                                        return_value=True):
            with self.assertRaisesRegex(RuntimeError, "not linked"):
                bridge.list_chat_titles()

    def test_read_recent_incoming_dispatches(self):
        bridge = WhatsAppBridge(headless=True)
        with unittest.mock.patch.object(
                bridge, "_read_recent_incoming_impl",
                return_value=["baby, where are you?"]):
            out = bridge.read_recent_incoming("Mom", limit=10)
        self.assertEqual(out, ["baby, where are you?"])

    def test_read_recent_incoming_not_linked(self):
        bridge = WhatsAppBridge(headless=True)
        with unittest.mock.patch.object(bridge, "_ensure_alive",
                                        return_value=True), \
             unittest.mock.patch.object(bridge, "_is_logged_in_impl",
                                        return_value=False), \
             unittest.mock.patch.object(bridge, "_needs_qr_impl",
                                        return_value=True):
            with self.assertRaisesRegex(RuntimeError, "not linked"):
                bridge.read_recent_incoming("Mom")


class EnsureLoggedInWaitTests(unittest.TestCase):
    """_ensure_logged_in_locked: a cold browser restores the saved session
    asynchronously (~10-20s), so read/send paths must wait — not declare
    "not linked" — while bailing fast on a genuine QR."""

    def _clock_patches(self):
        clock = {"t": 0.0}
        return clock, \
            unittest.mock.patch("actions.whatsapp_bridge.time.time",
                                side_effect=lambda: clock["t"]), \
            unittest.mock.patch("actions.whatsapp_bridge.time.sleep",
                                side_effect=lambda dt: clock.__setitem__("t",
                                    clock["t"] + dt))

    def test_true_immediately_when_session_restored(self):
        bridge = WhatsAppBridge(headless=True)
        with unittest.mock.patch.object(bridge, "_is_logged_in_impl",
                                        return_value=True):
            self.assertTrue(bridge._ensure_logged_in_locked())

    def test_false_fast_when_qr_genuinely_showing(self):
        bridge = WhatsAppBridge(headless=True)
        with unittest.mock.patch.object(bridge, "_is_logged_in_impl",
                                        return_value=False), \
             unittest.mock.patch.object(bridge, "_needs_qr_impl",
                                        return_value=True), \
             self._clock_patches()[1], self._clock_patches()[2]:
            self.assertFalse(bridge._ensure_logged_in_locked(timeout=60))

    def test_waits_for_session_restore_then_true(self):
        """The cold-browser race: no QR, session still loading → poll until
        the chat list appears instead of falsely reporting 'not linked'."""
        bridge = WhatsAppBridge(headless=True)
        clock, tpatch, spatch = self._clock_patches()
        with unittest.mock.patch.object(
                bridge, "_is_logged_in_impl",
                side_effect=[False, False, True]), \
             unittest.mock.patch.object(bridge, "_needs_qr_impl",
                                        return_value=False), \
             tpatch, spatch:
            self.assertTrue(bridge._ensure_logged_in_locked(timeout=30))
        self.assertEqual(clock["t"], 2.0)   # two 1s polls before success

    def test_bounded_false_when_never_restored(self):
        bridge = WhatsAppBridge(headless=True)
        clock, tpatch, spatch = self._clock_patches()
        with unittest.mock.patch.object(bridge, "_is_logged_in_impl",
                                        return_value=False), \
             unittest.mock.patch.object(bridge, "_needs_qr_impl",
                                        return_value=False), \
             tpatch, spatch:
            self.assertFalse(bridge._ensure_logged_in_locked(timeout=5))
        self.assertEqual(clock["t"], 5.0)   # bounded, no infinite wait


if __name__ == "__main__":
    unittest.main()
