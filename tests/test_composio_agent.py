"""Unit tests for composio_agent.py -- the Groq tool budget fixes.

Covers the two helpers added to fix the "/agent read my email" 400 failure
(Groq rejects requests with more than 128 tools):
  * _select_apps     -- keyword-based toolkit selection so a request only
                        loads the toolkits it actually needs
  * _merge_and_cap_tools -- enforces the 128-tool hard cap (MCP tools kept
                        first, then Composio tools fill the rest of the budget)
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# Ensure project root is on sys.path so composio_agent is importable
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from composio_agent import (  # noqa: E402
    MAX_TOOLS,
    _merge_and_cap_tools,
    _select_apps,
)
from composio_shim import App  # noqa: E402


class SelectAppsTests(unittest.TestCase):
    """Keyword detection must pick the toolkit the request is about."""

    def test_email_request_selects_gmail(self):
        self.assertEqual(_select_apps("read my email"), [App.GMAIL])
        self.assertEqual(_select_apps("check my inbox"), [App.GMAIL])
        self.assertEqual(_select_apps("Do I have any new mail?"), [App.GMAIL])

    def test_calendar_request_selects_googlecalendar(self):
        self.assertEqual(
            _select_apps("what's on my calendar today"),
            [App.GOOGLECALENDAR],
        )
        self.assertEqual(
            _select_apps("schedule a meeting at 3pm"),
            [App.GOOGLECALENDAR],
        )

    def test_github_request_selects_github(self):
        self.assertEqual(_select_apps("star my repository"), [App.GITHUB])
        self.assertEqual(_select_apps("open a pull request"), [App.GITHUB])

    def test_triggercmd_request_selects_triggercmd(self):
        self.assertEqual(_select_apps("run triggercmd command"), [App.TRIGGERCMD])

    def test_unrelated_request_returns_none(self):
        """No keyword match -> None so all connected tools are used."""
        self.assertIsNone(_select_apps("tell me a joke"))
        self.assertIsNone(_select_apps(""))


class MergeAndCapToolsTests(unittest.TestCase):
    """The merged tools list must never exceed Groq's 128-tool cap."""

    def test_under_cap_returns_merged_unchanged(self):
        merged = _merge_and_cap_tools([1, 2, 3], [4, 5], max_tools=128)
        self.assertEqual(merged, [1, 2, 3, 4, 5])

    def test_over_cap_keeps_mcp_tools_first(self):
        composio = list(range(200))
        mcp = ["m1", "m2"]
        merged = _merge_and_cap_tools(composio, mcp, max_tools=128)
        self.assertEqual(len(merged), 128)
        # MCP tools are preserved even though they sort last alphabetically.
        self.assertEqual(merged[:2], ["m1", "m2"])
        self.assertEqual(merged[2], 0)

    def test_mcp_tools_alone_over_cap_are_truncated(self):
        merged = _merge_and_cap_tools([], list(range(150)), max_tools=128)
        self.assertEqual(len(merged), 128)
        self.assertEqual(merged[0], 0)

    def test_exact_cap_unchanged(self):
        merged = _merge_and_cap_tools(list(range(100)), list(range(28)), max_tools=128)
        self.assertEqual(len(merged), 128)

    def test_default_cap_is_128(self):
        self.assertEqual(MAX_TOOLS, 128)


class QuotaAndRotationTests(unittest.TestCase):
    """Quota detection + key rotation on 429/413 keep multi-turn agent loops alive."""

    def test_quota_markers_detected(self):
        from composio_agent import _is_quota_error
        self.assertTrue(_is_quota_error(RuntimeError("Error code: 429 - rate limit")))
        self.assertTrue(_is_quota_error(
            RuntimeError("Request too large for model `groq/compound`")))
        self.assertTrue(_is_quota_error(
            RuntimeError("tokens per minute (TPM): Limit 12000")))
        self.assertFalse(_is_quota_error(RuntimeError("400 - bad request")))
        self.assertFalse(_is_quota_error(RuntimeError("401 - invalid api key")))

    def test_rotation_retries_with_next_key_on_quota_error(self):
        import composio_agent

        calls = {"n": 0}

        class FakePool:
            MAX_RECOVERY_WAIT_S = 90.0

            def __init__(self):
                self._keys = ["k1", "k2", "k3"]
                self.marked = []

            def current(self):
                return self._keys[0]

            def advance(self):
                self._keys = self._keys[1:] + self._keys[:1]

            def size(self):
                return len(self._keys)

            def mark_rate_limited(self, key):
                self.marked.append(key)

            def mark_daily_capped(self, key):
                self.marked.append(("daily", key))

            def earliest_recovery(self):
                return None

        class FakeCompletions:
            def __init__(self):
                self.chat = SimpleNamespace(
                    completions=SimpleNamespace(create=self._create)
                )

            def _create(self, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("429 rate limit reached for model")
                return "ok"

        real_groq = composio_agent.Groq
        pool = FakePool()
        composio_agent.Groq = lambda api_key=None: FakeCompletions()
        try:
            with patch("composio_agent._groq_pool", pool):
                result = composio_agent._create_with_key_rotation([], [])
        finally:
            composio_agent.Groq = real_groq
        self.assertEqual(result, "ok")
        self.assertEqual(calls["n"], 2)          # exactly one retry
        self.assertEqual(pool.marked, ["k1"])    # the failed key was quarantined

    def test_non_quota_error_raises_immediately(self):
        import composio_agent

        class FakePool:
            MAX_RECOVERY_WAIT_S = 90.0

            def current(self):
                return "k1"

            def size(self):
                return 2

            def advance(self):
                pass

            def mark_rate_limited(self, key):
                pass

            def mark_daily_capped(self, key):
                pass

            def earliest_recovery(self):
                return None

        class Boom:
            def __init__(self):
                self.chat = SimpleNamespace(
                    completions=SimpleNamespace(create=self._create)
                )

            def _create(self, **kwargs):
                raise RuntimeError("400 - invalid request")

        real_groq = composio_agent.Groq
        composio_agent.Groq = lambda api_key=None: Boom()
        try:
            with patch("composio_agent._groq_pool", FakePool()):
                with self.assertRaisesRegex(RuntimeError, "400 - invalid request"):
                    composio_agent._create_with_key_rotation([], [])
        finally:
            composio_agent.Groq = real_groq

    def test_daily_cap_parks_keys_then_falls_back_to_lite_model(self):
        """Every key TPD-capped on the big model (the observed "tokens per
        day" 429s) must not kill the agent: keys get parked until tomorrow
        and the call retries on the lite model, which has its own budget.
        """
        import time

        import composio_agent

        calls = {"models": []}

        class FakePool:
            MAX_RECOVERY_WAIT_S = 90.0

            def __init__(self):
                self._keys = ["k1", "k2", "k3"]
                self.marked = []
                self._recovery = None

            def current(self):
                return self._keys[0]

            def advance(self):
                self._keys = self._keys[1:] + self._keys[:1]

            def size(self):
                return len(self._keys)

            def mark_rate_limited(self, key):
                self.marked.append(("min", key))

            def mark_daily_capped(self, key):
                self.marked.append(("daily", key))
                self._recovery = time.time() + 8 * 3600  # parked until tomorrow

            def earliest_recovery(self):
                return self._recovery

        class FakeCompletions:
            def __init__(self):
                self.chat = SimpleNamespace(
                    completions=SimpleNamespace(create=self._create)
                )

            def _create(self, **kwargs):
                calls["models"].append(kwargs["model"])
                if kwargs["model"] != composio_agent.GROQ_LITE_MODEL:
                    raise RuntimeError(
                        "429 - tokens per day (TPD): Limit 100000, Used 98548"
                    )
                return "ok"

        real_groq = composio_agent.Groq
        pool = FakePool()
        composio_agent.Groq = lambda api_key=None: FakeCompletions()
        try:
            with patch("composio_agent._groq_pool", pool):
                result = composio_agent._create_with_key_rotation([], [])
        finally:
            composio_agent.Groq = real_groq

        self.assertEqual(result, "ok")
        # big model tried (and its key parked), then lite model answered
        self.assertEqual(calls["models"],
                         [composio_agent.AGENT_MODEL, composio_agent.GROQ_LITE_MODEL])
        self.assertIn(("daily", "k1"), pool.marked)


if __name__ == "__main__":
    unittest.main()
