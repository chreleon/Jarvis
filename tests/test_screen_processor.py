"""screen_process must RETURN the analysis text (the remote WhatsApp
dashboard shows the actual description) instead of a bare bool — the live
session's transcript when available, the still-image analysis otherwise,
False on failure. The module is imported lazily per test: it pulls in
sounddevice/numpy (~seconds) and must never be a suite-level import cost."""

import sys
import unittest
from unittest.mock import patch


def setUpModule():
    # screen_processor prints emoji progress lines; a bare unittest run has
    # cp1252 stdout on Windows, which would crash those prints. The app
    # itself reconfigures to UTF-8 — mirror that here.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


class ScreenProcessTextTests(unittest.TestCase):
    """screen_process() returns str analysis text on success, False on failure."""

    def _mod(self):
        from actions import screen_processor
        return screen_processor

    def test_returns_still_analysis_text(self):
        mod = self._mod()
        with patch.object(mod, "_ensure_genai", return_value=False), \
             patch.object(mod, "_capture_screenshot", return_value=b"img"), \
             patch.object(mod, "_analyze_still",
                          return_value="A code editor with Python open."):
            out = mod.screen_process({"text": "describe", "angle": "screen"})
        self.assertEqual(out, "A code editor with Python open.")

    def test_returns_live_transcript(self):
        mod = self._mod()
        mod._live._last_text = None

        def fake_analyze(self, image_bytes, mime_type, user_text):
            self._last_text = "A terminal with the daemon running."

        with patch.object(mod, "_ensure_genai", return_value=True), \
             patch.object(mod, "_get_api_key", return_value="key"), \
             patch.object(mod, "_ensure_started", return_value=None), \
             patch.object(mod._LiveSession, "analyze", fake_analyze), \
             patch.object(mod, "_capture_screenshot", return_value=b"img"):
            out = mod.screen_process({"text": "describe", "angle": "screen"})
        self.assertEqual(out, "A terminal with the daemon running.")

    def test_live_timeout_falls_back_to_still_text(self):
        mod = self._mod()
        mod._live._last_text = None
        with patch.object(mod, "_ensure_genai", return_value=True), \
             patch.object(mod, "_get_api_key", return_value="key"), \
             patch.object(mod, "_ensure_started", return_value=None), \
             patch.object(mod._LiveSession, "analyze",
                          lambda self, image_bytes, mime_type, user_text: None), \
             patch.object(mod, "VISION_TEXT_TIMEOUT", 0.01), \
             patch.object(mod, "_analyze_still",
                          return_value="still fallback description"), \
             patch.object(mod, "_capture_screenshot", return_value=b"img"):
            out = mod.screen_process({"text": "describe", "angle": "screen"})
        self.assertEqual(out, "still fallback description")

    def test_missing_text_returns_false(self):
        mod = self._mod()
        with patch.object(mod, "_capture_screenshot", return_value=b"img"):
            out = mod.screen_process({"angle": "screen"})   # no text prompt
        self.assertIs(out, False)

    def test_capture_failure_returns_false(self):
        mod = self._mod()

        def boom():
            raise RuntimeError("camera not found")

        with patch.object(mod, "_capture_camera", side_effect=boom):
            out = mod.screen_process({"text": "look", "angle": "camera"})
        self.assertIs(out, False)


class CleanVisionReplyTests(unittest.TestCase):
    """_clean_vision_reply strips <think>…</think> reasoning so the WhatsApp
    reply shows the answer, not the model's internal monologue."""

    def _mod(self):
        from actions import screen_processor
        return screen_processor

    def test_strips_think_block(self):
        mod = self._mod()
        raw = "<think>\nLet me analyze step by step.\n</think>\nThe screen shows a browser."
        self.assertEqual(mod._clean_vision_reply(raw),
                         "The screen shows a browser.")

    def test_plain_reply_untouched(self):
        mod = self._mod()
        self.assertEqual(mod._clean_vision_reply("A code editor."), "A code editor.")

    def test_empty_reply_stays_empty(self):
        mod = self._mod()
        self.assertEqual(mod._clean_vision_reply("   "), "")
        self.assertEqual(mod._clean_vision_reply(""), "")


if __name__ == "__main__":
    unittest.main()
