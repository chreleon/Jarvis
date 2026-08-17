"""Unit tests for or_client.py message shrinking on 413 retries.

The regression being guarded: after a "request too large" retry, the trim
must keep the system message intact — it carries the tool declarations at
the end, and chopping them made Jeeves answer "I don't have any tools
available" instead of using a tool.
"""

import unittest

from or_client import ClaudeClient


class ShrinkMessagesTests(unittest.TestCase):

    def _shrink(self, messages, limit=3000):
        return ClaudeClient._shrink_messages(messages, limit)

    def test_system_message_is_never_trimmed(self):
        system = {"role": "system", "content": "x" * 9000}
        out = self._shrink([system])
        self.assertEqual(out, [system])  # preserved verbatim, even when huge

    def test_conversation_messages_are_trimmed(self):
        big = {"role": "user", "content": "a" * 9000}
        out = self._shrink([big])
        self.assertLess(len(out[0]["content"]), 3005)
        self.assertIn("[trimmed]", out[0]["content"])

    def test_short_messages_untouched(self):
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        self.assertEqual(self._shrink(messages), messages)

    def test_system_kept_even_when_others_trimmed(self):
        """The exact 413-retry shape: system prompt + long conversation."""
        system = {"role": "system", "content": "[TOOLS] open_app, web_search, agent_task..."}
        out = self._shrink([system, {"role": "user", "content": "z" * 8000}])
        self.assertEqual(out[0], system)               # tools section intact
        self.assertNotEqual(out[1]["content"], "z" * 8000)


if __name__ == "__main__":
    unittest.main()
