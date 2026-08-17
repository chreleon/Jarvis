"""Unit tests for actions/meta_ai.py — asking Meta AI (WhatsApp's built-in
assistant) through the background WhatsApp bridge.

The heavy live part (launching Playwright, the real Meta AI chat) is not
exercised here — that needs a browser and a linked account. These tests
cover the deterministic wrapper logic: validation, the bridge call, and the
failure strings.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import actions.meta_ai as ma  # noqa: E402


class MetaAiToolTests(unittest.TestCase):

    def test_missing_question_returns_help(self):
        out = ma.meta_ai({})
        self.assertIn("question", out.lower())
        out2 = ma.meta_ai(None)
        self.assertIn("question", out2.lower())

    def test_success_returns_reply(self):
        with patch("actions.meta_ai._ask_bridge", return_value="2 + 2 = 4"):
            self.assertEqual(ma.meta_ai({"question": "what is 2+2?"}),
                             "2 + 2 = 4")

    def test_prompt_alias_works(self):
        with patch("actions.meta_ai._ask_bridge", return_value="ok"):
            self.assertEqual(ma.meta_ai({"prompt": "hello"}), "ok")

    def test_empty_reply_reported(self):
        with patch("actions.meta_ai._ask_bridge", return_value="   "):
            self.assertIn("nothing", ma.meta_ai({"question": "q"}).lower())

    def test_runtime_error_clean_message(self):
        with patch("actions.meta_ai._ask_bridge",
                   side_effect=RuntimeError("WhatsApp Web is not linked")):
            out = ma.meta_ai({"question": "q"})
        self.assertIn("Meta AI unavailable", out)
        self.assertIn("not linked", out)

    def test_unexpected_error_clean_message(self):
        with patch("actions.meta_ai._ask_bridge",
                   side_effect=OSError("socket down")):
            out = ma.meta_ai({"question": "q"})
        self.assertIn("Meta AI failed", out)
        self.assertIn("OSError", out)

    def test_bridge_acquisition_failure_reported(self):
        # _ask_bridge surfaces an acquire failure (e.g. playwright missing)
        # as a clean tool error, never a traceback.
        with patch("actions.whatsapp_bridge.acquire_shared_bridge",
                   side_effect=RuntimeError("boom")):
            out = ma.meta_ai({"question": "q"})
        self.assertIn("Meta AI unavailable", out)

    def test_unlinked_profile_fails_fast(self):
        # Never linked → don't cold-launch Chromium to discover the QR; the
        # secretary falls back to its instant deterministic draft instead
        # (YinYang: skip work known to fail).
        class FakeBridge:
            def __init__(self):
                self.started = False

            def start(self):
                self.started = True

        fb = FakeBridge()
        with patch("actions.meta_ai._bridge_config",
                   return_value=(True, None)), \
             patch("actions.whatsapp_bridge.is_profile_linked",
                   return_value=False), \
             patch("actions.whatsapp_bridge.acquire_shared_bridge",
                   return_value=(fb, True)), \
             patch("actions.whatsapp_bridge.release_shared_bridge",
                   lambda b: None):
            out = ma.meta_ai({"question": "q"})
        self.assertIn("Meta AI unavailable", out)
        self.assertIn("not linked", out)
        self.assertFalse(fb.started)   # browser never launched

    def test_shared_bridge_used_even_if_marker_missing(self):
        # created=False → the monitor's browser is already running; proceed
        class FakeBridge:
            def start(self):
                pass

            def wait_logged_in(self, timeout):
                return True

            def needs_qr(self):
                return False

            def meta_ai_ask(self, question, timeout):
                return "answer!"

        with patch("actions.meta_ai._bridge_config",
                   return_value=(True, None)), \
             patch("actions.whatsapp_bridge.is_profile_linked",
                   return_value=False), \
             patch("actions.whatsapp_bridge.acquire_shared_bridge",
                   return_value=(FakeBridge(), False)), \
             patch("actions.whatsapp_bridge.release_shared_bridge",
                   lambda b: None):
            self.assertEqual(ma.meta_ai({"question": "q"}), "answer!")


if __name__ == "__main__":
    unittest.main()
