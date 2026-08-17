"""Unit tests for cli.py — context trimming + daemon token logic.

Covers the two helpers added to keep brain requests inside the free-tier
token budget (413 "Payload Too Large" failures) and to harden the daemon
auth token:
  * _trim_context  -- per-message char cap + total history budget; the
                      newest message is always kept
  * _daemon_token  -- reuses `jeeves_api_secret` from config/api_keys.json,
                      generates + persists one when missing, and serializes
                      the read-modify-write behind a lock
"""

import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

# Ensure project root is on sys.path so cli is importable
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import cli  # noqa: E402


class TrimContextTests(unittest.TestCase):
    """_trim_context must cap message size and total history size while
    keeping the newest message and preserving order + metadata."""

    def test_small_messages_pass_through_unchanged(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello there"},
        ]
        self.assertEqual(cli._trim_context(msgs), msgs)

    def test_empty_input_returns_empty(self):
        self.assertEqual(cli._trim_context([]), [])

    def test_long_message_capped_with_ellipsis(self):
        msg = {"role": "user", "content": "x" * 3000}
        out = cli._trim_context([msg])
        self.assertEqual(len(out), 1)
        content = out[0]["content"]
        # cap includes the ellipsis marker, so result <= MAX_MSG_CHARS
        self.assertEqual(len(content), cli.MAX_MSG_CHARS)
        self.assertTrue(content.endswith("…"))
        self.assertTrue(content.startswith("x" * (cli.MAX_MSG_CHARS - 1)))

    def test_exactly_at_limit_is_untouched(self):
        msg = {"role": "user", "content": "y" * cli.MAX_MSG_CHARS}
        out = cli._trim_context([msg])
        self.assertEqual(out[0]["content"], "y" * cli.MAX_MSG_CHARS)

    def test_role_and_extra_keys_preserved(self):
        msgs = [{"role": "assistant", "content": "a" * 3000, "extra": 42}]
        out = cli._trim_context(msgs)
        self.assertEqual(out[0]["role"], "assistant")
        self.assertEqual(out[0]["extra"], 42)
        self.assertEqual(len(out[0]["content"]), cli.MAX_MSG_CHARS)

    def test_budget_drops_oldest_keeps_suffix(self):
        """With 20 medium messages, the oldest ones are dropped but the
        output stays a contiguous suffix (order preserved, newest last)."""
        msgs = [{"role": "user", "content": "m" * 500} for _ in range(20)]
        out = cli._trim_context(msgs)
        self.assertTrue(out)                     # newest always kept
        self.assertEqual(out[-1], msgs[-1])      # newest message present
        self.assertEqual(out, msgs[-len(out):])  # contiguous suffix, order kept

        total = sum(len(m["content"]) + 64 for m in out)
        self.assertLessEqual(total, cli.MAX_HISTORY_CHARS)
        # the next-oldest message would not have fit inside the budget
        if len(out) < len(msgs):
            extra = len(msgs[-len(out) - 1]["content"]) + 64
            self.assertGreater(total + extra, cli.MAX_HISTORY_CHARS)

    def test_newest_kept_even_when_alone_over_budget(self):
        """Non-string content bypasses the per-message char cap; a huge
        list can exceed the whole budget on its own, yet must still be
        included (and everything older dropped)."""
        big = {"role": "user", "content": ["chunk"] * 10000}
        small = {"role": "assistant", "content": "ok"}
        self.assertEqual(cli._trim_context([small, big]), [big])

    def test_non_string_content_preserved_when_it_fits(self):
        msg = {"role": "user", "content": {"a": [1, 2, 3]}}
        self.assertEqual(cli._trim_context([msg]), [msg])

    def test_missing_or_none_content_treated_as_empty(self):
        out = cli._trim_context([{"role": "user"}, {"role": "user", "content": None}])
        self.assertEqual(out, [
            {"role": "user", "content": ""},
            {"role": "user", "content": ""},
        ])

    def test_input_not_mutated(self):
        msgs = [{"role": "user", "content": "x" * 3000}]
        cli._trim_context(msgs)
        self.assertEqual(len(msgs[0]["content"]), 3000)


class DaemonTokenTests(unittest.TestCase):
    """_daemon_token must reuse, generate, and persist the shared token."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.config_dir = Path(self._tmp.name) / "config"
        self.config_dir.mkdir()
        self.cfg_path = self.config_dir / "api_keys.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _call(self):
        with patch("cli.API_CONFIG_PATH", self.cfg_path):
            return cli._daemon_token()

    def test_reuses_existing_token_without_rewriting(self):
        self.cfg_path.write_text(
            json.dumps({"jeeves_api_secret": "abc123", "other": 1}),
            encoding="utf-8",
        )
        token = self._call()
        self.assertEqual(token, "abc123")
        # file untouched
        self.assertEqual(
            json.loads(self.cfg_path.read_text(encoding="utf-8")),
            {"jeeves_api_secret": "abc123", "other": 1},
        )

    def test_whitespace_around_token_is_stripped(self):
        self.cfg_path.write_text(
            json.dumps({"jeeves_api_secret": "  tok-123  "}), encoding="utf-8",
        )
        self.assertEqual(self._call(), "tok-123")

    def test_generates_and_persists_when_missing(self):
        token = self._call()
        self.assertRegex(token, r"^[0-9a-f]{32}$")  # secrets.token_hex(16)
        data = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(data["jeeves_api_secret"], token)

    def test_generates_when_token_is_empty_or_blank(self):
        self.cfg_path.write_text(
            json.dumps({"jeeves_api_secret": "   "}), encoding="utf-8",
        )
        token = self._call()
        self.assertRegex(token, r"^[0-9a-f]{32}$")
        self.assertEqual(
            json.loads(self.cfg_path.read_text(encoding="utf-8"))["jeeves_api_secret"],
            token,
        )

    def test_preserves_existing_config_keys(self):
        self.cfg_path.write_text(
            json.dumps({"brain_provider": "groq"}), encoding="utf-8",
        )
        token = self._call()
        data = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(data["brain_provider"], "groq")
        self.assertEqual(data["jeeves_api_secret"], token)

    def test_corrupt_config_is_replaced_with_valid_json(self):
        self.cfg_path.write_text("not json {{{", encoding="utf-8")
        token = self._call()
        self.assertRegex(token, r"^[0-9a-f]{32}$")
        data = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(data["jeeves_api_secret"], token)

    def test_persist_failure_still_returns_token(self):
        import contextlib
        import io
        buf = io.StringIO()
        with patch("cli.Path.write_text", side_effect=OSError("readonly")):
            with contextlib.redirect_stdout(buf):
                token = self._call()
        # generation succeeds; the persistence warning is swallowed
        self.assertRegex(token, r"^[0-9a-f]{32}$")
        self.assertIn("Could not persist daemon token", buf.getvalue())

    def test_concurrent_calls_share_one_token(self):
        """The lock must serialize the read-modify-write so racing daemon
        threads never generate two different tokens.

        The patch is held for the whole block: a per-call patch would be
        exited by one worker while another is mid-read, briefly restoring
        API_CONFIG_PATH to the real config and leaking its token.
        """
        with patch("cli.API_CONFIG_PATH", self.cfg_path):
            with patch("cli.secrets.token_hex",
                       return_value="a" * 32) as mock_hex:
                with ThreadPoolExecutor(max_workers=8) as ex:
                    tokens = list(
                        ex.map(lambda _: cli._daemon_token(), range(16))
                    )
        # All 16 calls agree on one token and it was generated exactly once
        # (the lock, not the file I/O, serialized the generation).
        self.assertEqual(tokens, ["a" * 32] * 16)
        mock_hex.assert_called_once()
        self.assertEqual(
            json.loads(self.cfg_path.read_text(encoding="utf-8"))["jeeves_api_secret"],
            "a" * 32,
        )


if __name__ == "__main__":
    unittest.main()
