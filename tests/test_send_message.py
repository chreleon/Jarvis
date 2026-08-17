"""Unit tests for actions/send_message.py.

WhatsApp sends are background-first: the Playwright bridge is the primary
path, and the classic desktop-app / web-keyboard flows are the last
alternative when the bridge is unavailable or not linked.

Covers:
  * _is_app_installed          — decides desktop vs browser fallback
  * _send_whatsapp_bridge      — background send, raises on failure
  * _send_whatsapp_auto        — bridge primary, classic flow fallback
  * send_message dispatch      — default = bridge; explicit methods kept
  * _send_whatsapp_web         — vision-locates the search + message boxes
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import actions.send_message as sm  # noqa: E402


class AppInstalledTests(unittest.TestCase):
    """_is_app_installed must be safe and conservative."""

    def test_non_windows_returns_false(self):
        with patch("sys.platform", "linux"):
            self.assertFalse(sm._is_app_installed("WhatsApp"))

    def test_empty_name_returns_false(self):
        self.assertFalse(sm._is_app_installed(""))
        self.assertFalse(sm._is_app_installed(None))

    def test_never_crashes_on_bad_input(self):
        with patch("sys.platform", "win32"):
            with patch("actions.send_message.Path.iterdir", side_effect=OSError("denied")):
                self.assertFalse(sm._is_app_installed("WhatsApp"))


class BridgeFirstTests(unittest.TestCase):
    """WhatsApp sends default to the background bridge; the classic flows are
    the last alternative when the bridge fails."""

    def test_bridge_succeeds_without_touching_foreground_flow(self):
        with patch.object(sm, "_send_whatsapp_bridge", return_value="BRIDGE") as br:
            with patch.object(sm, "_send_whatsapp", return_value="DESKTOP") as desk:
                with patch.object(sm, "_send_whatsapp_web_shortcut",
                                 return_value="KEYBOARD") as kb:
                    out = sm.send_message(
                        {"receiver": "Mom", "message_text": "hi", "platform": "whatsapp"})
        self.assertEqual(out, "BRIDGE")
        br.assert_called_once()
        desk.assert_not_called()
        kb.assert_not_called()

    def test_bridge_failure_falls_back_to_desktop_when_installed(self):
        with patch.object(sm, "_send_whatsapp_bridge",
                         side_effect=RuntimeError("not linked")) as br:
            with patch.object(sm, "_is_app_installed", return_value=True):
                with patch.object(sm, "_send_whatsapp", return_value="DESKTOP") as desk:
                    with patch.object(sm, "_send_whatsapp_web_shortcut",
                                     return_value="KEYBOARD") as kb:
                        out = sm.send_message(
                            {"receiver": "Mom", "message_text": "hi", "platform": "whatsapp"})
        self.assertEqual(out, "DESKTOP")
        desk.assert_called_once()
        kb.assert_not_called()

    def test_bridge_failure_falls_back_to_web_when_no_desktop_app(self):
        """Missing app → browser path only; the desktop sender must never run."""
        with patch.object(sm, "_send_whatsapp_bridge",
                         side_effect=RuntimeError("no bridge")):
            with patch.object(sm, "_is_app_installed", return_value=False):
                with patch.object(sm, "_send_whatsapp", return_value="DESKTOP") as desk:
                    with patch.object(sm, "_send_whatsapp_web_shortcut",
                                     return_value="KEYBOARD") as kb:
                        out = sm.send_message(
                            {"receiver": "Mom", "message_text": "hi", "platform": "whatsapp"})
        self.assertEqual(out, "KEYBOARD")
        kb.assert_called_once()
        desk.assert_not_called()

    def test_explicit_bridge_method_reports_failure_without_fallback(self):
        with patch.object(sm, "_send_whatsapp_bridge",
                         side_effect=RuntimeError("boom")) as br:
            with patch.object(sm, "_is_app_installed", return_value=True):
                with patch.object(sm, "_send_whatsapp", return_value="DESKTOP") as desk:
                    out = sm.send_message(
                        {"receiver": "Mom", "message_text": "hi", "platform": "whatsapp",
                         "method": "bridge"})
        self.assertIn("Background WhatsApp send failed", out)
        self.assertIn("boom", out)
        br.assert_called_once()
        desk.assert_not_called()

    def test_not_linked_error_points_to_link_whatsapp(self):
        """A not-linked background browser says how to fix it: link once."""
        err = RuntimeError(
            "WhatsApp Web is not linked in the background browser — "
            "say 'link whatsapp' (or 'secretary link') once to open "
            "the window and scan the QR with your phone; the session "
            "is then saved and reused forever")
        with patch.object(sm, "_send_whatsapp_bridge", side_effect=err) as br:
            with patch.object(sm, "_is_app_installed", return_value=True):
                with patch.object(sm, "_send_whatsapp", return_value="DESKTOP") as desk:
                    out = sm.send_message(
                        {"receiver": "Mom", "message_text": "hi", "platform": "whatsapp",
                         "method": "bridge"})
        self.assertIn("link whatsapp", out)
        self.assertIn("scan the QR", out)
        desk.assert_not_called()

    def test_explicit_shortcut_keeps_legacy_keyboard_path(self):
        """method='shortcut' bypasses the bridge and uses the keyboard path."""
        with patch.object(sm, "_send_whatsapp_bridge", return_value="BRIDGE") as br:
            with patch.object(sm, "_send_whatsapp_web_shortcut",
                             return_value="KEYBOARD") as kb:
                out = sm.send_message(
                    {"receiver": "Mom", "message_text": "hi", "platform": "whatsapp",
                     "method": "shortcut"})
        self.assertEqual(out, "KEYBOARD")
        kb.assert_called_once()
        br.assert_not_called()


class UnlinkedPreCheckTests(unittest.TestCase):
    """When the profile was never linked (and no CDP attach is configured),
    the bridge must fail fast instead of cold-launching Chromium (~15s /
    ~200MB) just to discover the QR is needed (YinYang: skip work known to
    fail)."""

    class FakeBridge:
        def __init__(self):
            self.started = False

        def start(self):
            self.started = True

        def wait_logged_in(self, timeout):
            return True

        def needs_qr(self):
            return False

        def send_message(self, receiver, text):
            return "sent"

    def test_unlinked_created_raises_fast_without_starting(self):
        fb = self.FakeBridge()
        with patch("actions.send_message._bridge_config",
                   return_value=(True, None)), \
             patch("actions.whatsapp_bridge.is_profile_linked",
                   return_value=False), \
             patch("actions.whatsapp_bridge.acquire_shared_bridge",
                   return_value=(fb, True)), \
             patch("actions.whatsapp_bridge.release_shared_bridge",
                   lambda b: None):
            with self.assertRaisesRegex(RuntimeError, "not linked"):
                sm._send_whatsapp_bridge("Mom", "hi")
        self.assertFalse(fb.started)   # never launched the browser

    def test_linked_proceeds_normally(self):
        fb = self.FakeBridge()
        with patch("actions.send_message._bridge_config",
                   return_value=(True, None)), \
             patch("actions.whatsapp_bridge.is_profile_linked",
                   return_value=True), \
             patch("actions.whatsapp_bridge.acquire_shared_bridge",
                   return_value=(fb, True)), \
             patch("actions.whatsapp_bridge.release_shared_bridge",
                   lambda b: None):
            out = sm._send_whatsapp_bridge("Mom", "hi")
        self.assertTrue(fb.started)
        self.assertIn("Message sent to Mom", out)

    def test_existing_shared_bridge_used_even_if_marker_missing(self):
        # created=False means the monitor's bridge is already running — the
        # marker check must not block the send
        fb = self.FakeBridge()
        with patch("actions.send_message._bridge_config",
                   return_value=(True, None)), \
             patch("actions.whatsapp_bridge.is_profile_linked",
                   return_value=False), \
             patch("actions.whatsapp_bridge.acquire_shared_bridge",
                   return_value=(fb, False)), \
             patch("actions.whatsapp_bridge.release_shared_bridge",
                   lambda b: None):
            out = sm._send_whatsapp_bridge("Mom", "hi")
        self.assertTrue(fb.started)
        self.assertIn("Message sent to Mom", out)

    def test_cdp_attach_needs_no_profile_marker(self):
        fb = self.FakeBridge()
        with patch("actions.send_message._bridge_config",
                   return_value=(True, "http://127.0.0.1:9222")), \
             patch("actions.whatsapp_bridge.is_profile_linked",
                   return_value=False), \
             patch("actions.whatsapp_bridge.acquire_shared_bridge",
                   return_value=(fb, True)), \
             patch("actions.whatsapp_bridge.release_shared_bridge",
                   lambda b: None):
            out = sm._send_whatsapp_bridge("Mom", "hi")
        self.assertTrue(fb.started)
        self.assertIn("Message sent to Mom", out)


class DispatchTests(unittest.TestCase):
    """send_message must validate input and route non-WhatsApp platforms."""

    def test_telegram_missing_uses_web(self):
        with patch.object(sm, "_is_app_installed", return_value=False):
            with patch.object(sm, "_send_telegram", return_value="DESKTOP") as desk:
                with patch.object(sm, "_send_telegram_web", return_value="WEB") as web:
                    out = sm.send_message(
                        {"receiver": "Bro", "message_text": "yo", "platform": "telegram"})
        self.assertEqual(out, "WEB")
        web.assert_called_once()

    def test_missing_receiver_validated_first(self):
        out = sm.send_message({"receiver": "", "message_text": "hi", "platform": "whatsapp"})
        self.assertIn("who to send", out)


class ShortcutWebTests(unittest.TestCase):
    """The default WhatsApp Web path uses keyboard shortcuts, not vision."""

    def setUp(self):
        self._sleep = patch.object(sm.time, "sleep")
        self._open = patch.object(sm.webbrowser, "open")
        self._sleep.start()
        self._open.start()

    def tearDown(self):
        self._open.stop()
        self._sleep.stop()

    def test_keyboard_happy_path(self):
        """Ctrl+Alt+N → type contact → Enter → type message → Enter, no vision."""
        with patch.object(sm, "_activate_window_by_title", return_value=True):
            with patch.object(sm, "_get_pyautogui") as _pg:
                pg = _pg.return_value
                out = sm._send_whatsapp_web_shortcut("Mom", "hi there")
        self.assertEqual(out, "Message sent to Mom via WhatsApp Web (keyboard).")
        pg.hotkey.assert_called_once_with("ctrl", "alt", "n")
        self.assertEqual(pg.write.call_count, 2)   # contact + message
        self.assertEqual(pg.press.call_count, 2)   # select chat + send
        self.assertFalse(pg.click.called)          # no coordinate clicking

    def test_no_whatsapp_tab_returns_guidance_without_typing(self):
        """Never type into an unknown window: stop if the tab isn't found."""
        with patch.object(sm, "_activate_window_by_title", return_value=False):
            with patch.object(sm, "_get_pyautogui") as _pg:
                pg = _pg.return_value
                out = sm._send_whatsapp_web_shortcut("Mom", "hi")
        self.assertIn("open web.whatsapp.com", out)
        pg.hotkey.assert_not_called()
        pg.write.assert_not_called()

    def test_dispatch_method_vision_uses_vision(self):
        with patch.object(sm, "_is_app_installed", return_value=False):
            with patch.object(sm, "_send_whatsapp_web_shortcut",
                             return_value="KEYBOARD") as kb:
                with patch.object(sm, "_send_whatsapp_web", return_value="VISION") as vis:
                    out = sm.send_message(
                        {"receiver": "Mom", "message_text": "hi", "platform": "whatsapp",
                         "method": "vision"})
        self.assertEqual(out, "VISION")
        vis.assert_called_once()
        kb.assert_not_called()

    def test_reuses_open_tab_without_opening_new_one(self):
        """Already-open WhatsApp tab → focused and reused, no new tab."""
        with patch.object(sm.webbrowser, "open") as open_mock:
            with patch.object(sm, "_activate_window_by_title", return_value=True):
                with patch.object(sm, "_get_pyautogui") as _pg:
                    pg = _pg.return_value
                    out = sm._send_whatsapp_web_shortcut("Mom", "hi")
        self.assertEqual(out, "Message sent to Mom via WhatsApp Web (keyboard).")
        open_mock.assert_not_called()

    def test_opens_new_tab_only_when_none_open(self):
        """No WhatsApp tab open → webbrowser.open called exactly once."""
        with patch.object(sm.webbrowser, "open") as open_mock:
            with patch.object(sm, "_activate_window_by_title", return_value=False):
                with patch.object(sm, "_get_pyautogui") as _pg:
                    pg = _pg.return_value
                    out = sm._send_whatsapp_web_shortcut("Mom", "hi")
        self.assertIn("open web.whatsapp.com", out)
        open_mock.assert_called_once_with("https://web.whatsapp.com/")


class WhatsAppWebTests(unittest.TestCase):
    """The vision-based WhatsApp Web fallback locates boxes on screen."""

    def setUp(self):
        # Skip the real 8s SPA-load sleeps (and browser opens) in tests.
        self._sleep = patch.object(sm.time, "sleep")
        self._open = patch.object(sm.webbrowser, "open")
        self._sleep.start()
        self._open.start()

    def tearDown(self):
        self._open.stop()
        self._sleep.stop()

    def test_happy_path_types_and_sends(self):
        with patch.object(sm, "_screen_find_element",
                         side_effect=[(100, 50), (300, 600)]):
            with patch.object(sm, "_get_pyautogui") as _pg:
                pg = _pg.return_value
                out = sm._send_whatsapp_web("Mom", "hello from jeeves")
        self.assertEqual(out, "Message sent to Mom via WhatsApp Web.")
        self.assertEqual(pg.click.call_count, 2)   # search box + message box
        self.assertEqual(pg.write.call_count, 2)   # contact + message
        self.assertEqual(pg.press.call_count, 2)   # enter after search + send

    def test_not_logged_in_returns_guidance(self):
        with patch.object(sm, "_screen_find_element", return_value=None):
            with patch.object(sm, "_get_pyautogui") as _pg:
                pg = _pg.return_value
                out = sm._send_whatsapp_web("Mom", "hello")
        self.assertIn("logged in", out)
        pg.click.assert_not_called()

    def test_vision_internal_failure_returns_guidance(self):
        """When the inner vision locator fails, _screen_find_element returns
        None (it swallows exceptions) and the sender degrades gracefully."""
        with patch("actions.computer_control._screen_find",
                   side_effect=RuntimeError("vision down")):
            self.assertIsNone(sm._screen_find_element("any box"))
        with patch.object(sm, "_screen_find_element", return_value=None):
            with patch.object(sm, "_get_pyautogui"):
                out = sm._send_whatsapp_web("Mom", "hello")
        self.assertIn("logged in", out)


class ReceiverAliasTests(unittest.TestCase):
    """Memory aliases: "msg wife" resolves to the stored contact name even
    though no WhatsApp contact is called "wife"."""

    MEMORY = {
        "relationships": {"wife": {"value": "\U0001f63b\u3082\u307e \u304b\u3066"},
                           "husband": {"value": "not specified"}},
        "identity": {"name": {"value": "sir"},
                      "wife": {"value": "momma"}},
    }

    def test_relationship_alias_resolves(self):
        with patch("memory.memory_manager.load_memory", return_value=self.MEMORY):
            self.assertEqual(sm._resolve_receiver("wife"), "\U0001f63b\u3082\u307e \u304b\u3066")

    def test_case_insensitive(self):
        with patch("memory.memory_manager.load_memory", return_value=self.MEMORY):
            self.assertEqual(sm._resolve_receiver("WIFE"), "\U0001f63b\u3082\u307e \u304b\u3066")

    def test_identity_fallback_when_no_relationship(self):
        mem = {"identity": {"wife": {"value": "Momma"}}}
        with patch("memory.memory_manager.load_memory", return_value=mem):
            self.assertEqual(sm._resolve_receiver("wife"), "Momma")

    def test_unknown_name_passes_through(self):
        with patch("memory.memory_manager.load_memory", return_value={}):
            self.assertEqual(sm._resolve_receiver("Mom"), "Mom")

    def test_not_specified_ignored(self):
        mem = {"relationships": {"husband": {"value": "not specified"}}}
        with patch("memory.memory_manager.load_memory", return_value=mem):
            self.assertEqual(sm._resolve_receiver("husband"), "husband")

    def test_send_message_sends_to_resolved_contact(self):
        with patch.object(sm, "_resolve_receiver",
                          return_value="\U0001f63b\u3082\u307e \u304b\u3066") as rs:
            with patch.object(sm, "_send_whatsapp_bridge",
                              return_value="SENT") as br:
                out = sm.send_message({"receiver": "wife",
                                       "message_text": "dinner?",
                                       "platform": "whatsapp"})
        rs.assert_called_once_with("wife")
        br.assert_called_once_with("\U0001f63b\u3082\u307e \u304b\u3066", "dinner?")
        self.assertEqual(out, "SENT")


if __name__ == "__main__":
    unittest.main()
