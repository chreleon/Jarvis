"""Unit tests for composio_shim.py — shared Composio compatibility layer.

Tests the unified ComposioToolSet that adapts across legacy and new SDK
versions, and the App constant class. All tests validate the fallback /
empty-backend behavior which is the most critical path — if Composio is
installed the real SDK is exercised; if not, graceful degradation is tested.
"""

import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Ensure project root is on sys.path so composio_shim is importable
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from composio_shim import (
    App,
    ComposioToolSet,
    _app_to_slug,
    _load_composio_credentials,
)


class TestAppConstants(unittest.TestCase):
    """App class provides string constants for the three supported apps."""

    def test_github_constant(self):
        self.assertEqual(App.GITHUB, "GITHUB")

    def test_gmail_constant(self):
        self.assertEqual(App.GMAIL, "GMAIL")

    def test_googlecalendar_constant(self):
        self.assertEqual(App.GOOGLECALENDAR, "GOOGLECALENDAR")


class TestLoadComposioCredentials(unittest.TestCase):
    """_load_composio_credentials() must return (api_key, user_id) strings."""

    def test_returns_tuple_of_strings(self):
        api_key, user_id = _load_composio_credentials()
        self.assertIsInstance(api_key, str)
        self.assertIsInstance(user_id, str)

    @patch.dict(os.environ, {
        "COMPOSIO_API_KEY": "env_key_123",
        "COMPOSIO_USER_ID": "env_user_456",
    })
    def test_env_vars_take_precedence(self):
        api_key, user_id = _load_composio_credentials()
        self.assertEqual(api_key, "env_key_123")
        self.assertEqual(user_id, "env_user_456")

    @patch.dict(os.environ, {}, clear=True)
    @patch("composio_shim.CONFIG_PATH")
    def test_empty_env_falls_back_to_config(self, mock_path):
        """When env vars are absent, credentials should come from config."""
        mock_path.exists.return_value = True
        mock_path.read_text.return_value = json.dumps({
            "composio_api_key": "cfg_key_789",
            "composio_user_id": "cfg_user_abc",
        })
        api_key, user_id = _load_composio_credentials()
        self.assertEqual(api_key, "cfg_key_789")
        self.assertEqual(user_id, "cfg_user_abc")

    @patch.dict(os.environ, {}, clear=True)
    def test_no_credentials_anywhere(self):
        """When neither env nor config has credentials, returns ("", "default")."""
        with patch("composio_shim.CONFIG_PATH") as mock_path:
            mock_path.exists.return_value = False
            api_key, user_id = _load_composio_credentials()
        self.assertEqual(api_key, "")
        self.assertEqual(user_id, "default")


class TestComposioToolSet(unittest.TestCase):
    """ComposioToolSet must gracefully degrade when no SDK is available."""

    def test_can_instantiate(self):
        """Constructor should never raise, even with no SDK."""
        ts = ComposioToolSet()
        self.assertIsInstance(ts, ComposioToolSet)

    def test_backend_is_string(self):
        ts = ComposioToolSet()
        self.assertIsInstance(ts._backend, str)

    def test_get_tools_returns_list(self):
        """get_tools() should always return a list, never crash."""
        ts = ComposioToolSet()
        result = ts.get_tools()
        self.assertIsInstance(result, list)

    def test_get_tools_with_apps_returns_list(self):
        ts = ComposioToolSet()
        result = ts.get_tools(apps=[App.GITHUB, App.GMAIL])
        self.assertIsInstance(result, list)

    def test_handle_tool_calls_returns_list_of_dicts(self):
        """handle_tool_calls() should return a list, never crash."""
        ts = ComposioToolSet()
        with patch.object(ts, "_backend", "none"):
            with patch.object(ts, "_composio_instance", None):
                result = ts.handle_tool_calls(
                    MagicMock(choices=[MagicMock(message=MagicMock(tool_calls=[MagicMock()]))])
                )
        self.assertIsInstance(result, list)
        if result:
            self.assertIn("error", result[0])

    def test_no_backend_returns_empty_tools(self):
        """When _backend is 'none', get_tools must return []."""
        ts = ComposioToolSet()
        with patch.object(ts, "_backend", "none"):
            with patch.object(ts, "_composio_instance", None):
                self.assertEqual(ts.get_tools(), [])

    def test_no_backend_graceful_degradation(self):
        """Simulate no-SDK environment by forcing instance state directly."""
        ts = ComposioToolSet()
        with patch.object(ts, "_backend", "none"):
            with patch.object(ts, "_composio_instance", None):
                self.assertEqual(ts.get_tools(), [])
                # handle_tool_calls should also not crash
                result = ts.handle_tool_calls(
                    MagicMock(choices=[MagicMock(message=MagicMock(tool_calls=[MagicMock()]))])
                )
                self.assertIsInstance(result, list)
                if result:
                    self.assertIn("error", result[0])


class TestAppToSlug(unittest.TestCase):
    """_app_to_slug() normalizes any App representation to a toolkit slug."""

    def test_enum_constant(self):
        self.assertEqual(_app_to_slug(App.GITHUB), "github")

    def test_bare_string(self):
        self.assertEqual(_app_to_slug("GOOGLECALENDAR"), "googlecalendar")

    def test_enum_repr(self):
        self.assertEqual(_app_to_slug("<App.GMAIL: 'GMAIL'>"), "gmail")

    def test_already_lowercase(self):
        self.assertEqual(_app_to_slug("github"), "github")


class TestInitiateConnection(unittest.TestCase):
    """initiate_connection() must adapt to every shim backend."""

    def test_no_backend_raises_attribute_error(self):
        """With no SDK, raise AttributeError so callers fall back gracefully."""
        ts = ComposioToolSet()
        with patch.object(ts, "_backend", "none"), patch.object(ts, "_composio_instance", None):
            with self.assertRaises(AttributeError):
                ts.initiate_connection(App.GITHUB)

    def test_legacy_backend_delegates_with_app_enum(self):
        """Legacy path forwards an App enum and the resolved entity_id."""
        inst = MagicMock()
        inst.initiate_connection.return_value = {"redirect_url": "https://legacy"}
        ts = ComposioToolSet()
        with patch.object(ts, "_backend", "legacy"), patch.object(ts, "_composio_instance", inst):
            result = ts.initiate_connection("GITHUB")
        args, kwargs = inst.initiate_connection.call_args
        self.assertEqual(str(args[0] if args else kwargs.get("app")), "GITHUB")
        self.assertTrue(kwargs.get("entity_id"))
        self.assertEqual(result["redirect_url"], "https://legacy")

    def test_new_backend_resolves_auth_config_and_links(self):
        """New SDK path looks up the auth config, then calls connected_accounts.link."""
        inst = MagicMock()
        inst.auth_configs.list.return_value = MagicMock(
            items=[MagicMock(id="ac_123")]
        )
        inst.connected_accounts.link.return_value = MagicMock(
            redirect_url="https://composio.dev/connect/xyz"
        )
        ts = ComposioToolSet()
        with patch.object(ts, "_backend", "new"), patch.object(ts, "_composio_instance", inst):
            result = ts.initiate_connection(App.GITHUB)
        inst.auth_configs.list.assert_called_once_with(
            toolkit_slug="github", limit=10, show_disabled=False
        )
        _, kwargs = inst.connected_accounts.link.call_args
        self.assertEqual(kwargs["auth_config_id"], "ac_123")
        self.assertTrue(kwargs["allow_multiple"])
        self.assertEqual(result.redirect_url, "https://composio.dev/connect/xyz")

    def test_new_backend_missing_auth_config_raises_runtime_error(self):
        """No auth config for the app -> clear RuntimeError, not a silent fallback."""
        inst = MagicMock()
        inst.auth_configs.list.return_value = MagicMock(items=[])
        ts = ComposioToolSet()
        with patch.object(ts, "_backend", "new"), patch.object(ts, "_composio_instance", inst):
            with self.assertRaisesRegex(RuntimeError, "gmail"):
                ts.initiate_connection("gmail")


def _make_response(tool_calls):
    """Build a minimal response object shaped like an OpenAI chat completion."""
    message = MagicMock(tool_calls=tool_calls)
    return MagicMock(choices=[MagicMock(message=message)])


def _tool_call(tool_id, name, arguments="{}"):
    return SimpleNamespace(
        id=tool_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


class TestHandleToolCallsNewSdk(unittest.TestCase):
    """handle_tool_calls() on the new SDK must really execute tools."""

    def _toolset(self, inst):
        ts = ComposioToolSet()
        ts._backend = "new"
        ts._composio_instance = inst
        return ts

    @patch("composio_shim._load_composio_credentials", return_value=("k", "user_1"))
    def test_executes_with_resolved_account(self, _creds):
        inst = MagicMock()
        inst.connected_accounts.list.return_value = MagicMock(items=[
            MagicMock(id="ca_github", toolkit=MagicMock(slug="github")),
            MagicMock(id="ca_gmail", toolkit=MagicMock(slug="gmail")),
        ])
        inst.tools.execute.return_value = {
            "successful": True, "data": {"ok": 1}, "error": None,
        }
        ts = self._toolset(inst)

        results = ts.handle_tool_calls(_make_response([
            _tool_call("call_1", "GITHUB_STAR_A_REPOSITORY",
                       '{"owner": "a", "repo": "b"}'),
        ]))

        inst.connected_accounts.list.assert_called_once_with(user_ids=["user_1"])
        _, kwargs = inst.tools.execute.call_args
        self.assertEqual(kwargs["slug"], "GITHUB_STAR_A_REPOSITORY")
        self.assertEqual(kwargs["arguments"], {"owner": "a", "repo": "b"})
        self.assertEqual(kwargs["connected_account_id"], "ca_github")
        self.assertEqual(kwargs["user_id"], "user_1")
        self.assertTrue(kwargs["dangerously_skip_version_check"])
        self.assertEqual(results[0]["data"], {"ok": 1})

    @patch("composio_shim._load_composio_credentials", return_value=("k", "user_1"))
    def test_no_account_leaves_connected_account_id_none(self, _creds):
        inst = MagicMock()
        inst.connected_accounts.list.return_value = MagicMock(items=[])
        inst.tools.execute.return_value = {"successful": True, "data": {}, "error": None}
        ts = self._toolset(inst)

        ts.handle_tool_calls(_make_response([
            _tool_call("c1", "GOOGLE_SEND_EMAIL"),
        ]))
        _, kwargs = inst.tools.execute.call_args
        self.assertIsNone(kwargs["connected_account_id"])

    @patch("composio_shim._load_composio_credentials", return_value=("k", "user_1"))
    def test_invalid_arguments_fall_back_to_empty_dict(self, _creds):
        inst = MagicMock()
        inst.connected_accounts.list.return_value = MagicMock(items=[])
        inst.tools.execute.return_value = {"successful": True, "data": {}, "error": None}
        ts = self._toolset(inst)

        ts.handle_tool_calls(_make_response([
            _tool_call("c1", "GITHUB_GET_ISSUE", "not-json{{{"),
        ]))
        _, kwargs = inst.tools.execute.call_args
        self.assertEqual(kwargs["arguments"], {})

    @patch("composio_shim._load_composio_credentials", return_value=("k", "user_1"))
    def test_execution_failure_returns_error_result(self, _creds):
        inst = MagicMock()
        inst.connected_accounts.list.return_value = MagicMock(items=[])
        inst.tools.execute.side_effect = RuntimeError("No connected account")
        ts = self._toolset(inst)

        results = ts.handle_tool_calls(_make_response([
            _tool_call("c1", "GITHUB_GET_ISSUE"),
        ]))
        self.assertEqual(results[0]["successful"], False)
        self.assertIn("No connected account", results[0]["error"])

    @patch("composio_shim._load_composio_credentials", return_value=("k", "user_1"))
    def test_account_list_is_cached(self, _creds):
        inst = MagicMock()
        inst.connected_accounts.list.return_value = MagicMock(items=[
            MagicMock(id="ca_github", toolkit=MagicMock(slug="github")),
        ])
        inst.tools.execute.return_value = {"successful": True, "data": {}, "error": None}
        ts = self._toolset(inst)

        ts.handle_tool_calls(_make_response([
            _tool_call("c1", "GITHUB_GET_ISSUE"),
            _tool_call("c2", "GITHUB_LIST_REPOS"),
        ]))
        self.assertEqual(inst.connected_accounts.list.call_count, 1)

    @patch("composio_shim._load_composio_credentials", return_value=("k", "user_1"))
    def test_expired_account_retries_after_cache_invalidation(self, _creds):
        """410/expired execution failures drop the account cache and retry once,
        so a re-authorized ACTIVE connection is picked up."""
        inst = MagicMock()
        # First resolve: only an EXPIRED account exists (stale state).
        # After invalidation, the re-list reveals an ACTIVE one.
        inst.connected_accounts.list.side_effect = [
            MagicMock(items=[
                MagicMock(id="ca_expired", toolkit=MagicMock(slug="gmail"), status="EXPIRED"),
            ]),
            MagicMock(items=[
                MagicMock(id="ca_expired", toolkit=MagicMock(slug="gmail"), status="EXPIRED"),
                MagicMock(id="ca_active", toolkit=MagicMock(slug="gmail"), status="ACTIVE"),
            ]),
        ]
        inst.tools.execute.side_effect = [
            RuntimeError(
                "Error code: 410 - Connected account ca_expired for toolkit "
                "'gmail' is in EXPIRED state"
            ),
            {"successful": True, "data": {"ok": 1}, "error": None},
        ]
        ts = self._toolset(inst)
        results = ts.handle_tool_calls(_make_response([_tool_call("c1", "GMAIL_FETCH_EMAILS")]))
        self.assertTrue(results[0]["successful"])
        self.assertEqual(results[0]["data"], {"ok": 1})
        # The retried execute used the ACTIVE account, not the stale EXPIRED one.
        second_kwargs = inst.tools.execute.call_args_list[1].kwargs
        self.assertEqual(second_kwargs["connected_account_id"], "ca_active")

    def test_no_tool_calls_returns_empty(self):
        ts = self._toolset(MagicMock())
        self.assertEqual(ts.handle_tool_calls(_make_response([])), [])

    def test_legacy_still_delegates(self):
        inst = MagicMock()
        inst.handle_tool_calls.return_value = [{"successful": True, "data": {}}]
        ts = ComposioToolSet()
        ts._backend = "legacy"
        ts._composio_instance = inst
        results = ts.handle_tool_calls(_make_response([_tool_call("c1", "X")]))
        inst.handle_tool_calls.assert_called_once()
        self.assertEqual(results[0]["successful"], True)

    def test_model_dump_results_are_flattened(self):
        inst = MagicMock()
        inst.connected_accounts.list.return_value = MagicMock(items=[])
        dumped = {"successful": True, "data": {"a": 1}, "error": None}
        result_model = MagicMock()
        result_model.model_dump.return_value = dumped
        inst.tools.execute.return_value = result_model
        ts = self._toolset(inst)
        with patch("composio_shim._load_composio_credentials", return_value=("k", "u")):
            results = ts.handle_tool_calls(_make_response([_tool_call("c1", "X")]))
        self.assertEqual(results[0]["data"], {"a": 1})


class TestConnectedAccountSelection(unittest.TestCase):
    """Account resolution must prefer ACTIVE connections over EXPIRED ones."""

    def _toolset(self, inst):
        ts = ComposioToolSet()
        ts._backend = "new"
        ts._composio_instance = inst
        return ts

    def _listing(self, *pairs):
        """pairs: (account_id, toolkit_slug, status)."""
        return MagicMock(items=[
            MagicMock(id=aid, toolkit=MagicMock(slug=slug), status=status)
            for aid, slug, status in pairs
        ])

    @patch("composio_shim._load_composio_credentials", return_value=("k", "user_1"))
    def test_prefers_active_over_expired(self, _creds):
        """EXPIRED listed first must NOT win; an ACTIVE connection must."""
        inst = MagicMock()
        inst.connected_accounts.list.return_value = self._listing(
            ("ca_expired", "gmail", "EXPIRED"),
            ("ca_active1", "gmail", "ACTIVE"),
            ("ca_active2", "gmail", "ACTIVE"),
            ("ca_gh", "github", "ACTIVE"),
        )
        ts = self._toolset(inst)
        mapping = ts._get_connected_accounts("user_1")
        self.assertEqual(mapping["gmail"], "ca_active1")
        self.assertEqual(mapping["github"], "ca_gh")

    @patch("composio_shim._load_composio_credentials", return_value=("k", "user_1"))
    def test_falls_back_to_non_active_when_no_active_exists(self, _creds):
        """A toolkit with only EXPIRED connections still resolves (no empty map)."""
        inst = MagicMock()
        inst.connected_accounts.list.return_value = self._listing(
            ("ca_expired", "gmail", "EXPIRED"),
        )
        ts = self._toolset(inst)
        mapping = ts._get_connected_accounts("user_1")
        self.assertEqual(mapping["gmail"], "ca_expired")


class TestGetToolsToolkitSelection(unittest.TestCase):
    """get_tools() with no app filter must target connected toolkits, not an
    arbitrary empty search (which returned unrelated tools like ACCREDIBLE)."""

    def _toolset(self, inst):
        ts = ComposioToolSet()
        ts._backend = "new"
        ts._composio_instance = inst
        return ts

    @patch("composio_shim._load_composio_credentials", return_value=("k", "user_1"))
    def test_no_apps_uses_connected_toolkits(self, _creds):
        """Fetches each connected toolkit separately and merges results."""
        inst = MagicMock()
        inst.connected_accounts.list.return_value = MagicMock(items=[
            MagicMock(id="ca1", toolkit=MagicMock(slug="gmail"), status="ACTIVE"),
            MagicMock(id="ca2", toolkit=MagicMock(slug="github"), status="ACTIVE"),
        ])
        inst.tools.get.side_effect = [
            [{"function": {"name": "GITHUB_GET_USER"}}],
            [{"function": {"name": "GMAIL_FETCH_EMAILS"}}],
        ]
        ts = self._toolset(inst)
        result = ts.get_tools()
        calls = inst.tools.get.call_args_list
        self.assertEqual(len(calls), 2)
        # sorted() order: github before gmail
        self.assertEqual(calls[0].kwargs["toolkits"], ["github"])
        self.assertEqual(calls[1].kwargs["toolkits"], ["gmail"])
        self.assertEqual(calls[0].kwargs["user_id"], "user_1")
        names = [t["function"]["name"] for t in result]
        self.assertIn("GMAIL_FETCH_EMAILS", names)
        self.assertIn("GITHUB_GET_USER", names)

    @patch("composio_shim._load_composio_credentials", return_value=("k", "user_1"))
    def test_duplicate_tools_across_toolkits_are_merged(self, _creds):
        """Same tool name from two toolkits appears only once."""
        inst = MagicMock()
        inst.connected_accounts.list.return_value = MagicMock(items=[
            MagicMock(id="ca1", toolkit=MagicMock(slug="gmail"), status="ACTIVE"),
            MagicMock(id="ca2", toolkit=MagicMock(slug="github"), status="ACTIVE"),
        ])
        inst.tools.get.side_effect = [
            [{"function": {"name": "SHARED_TOOL"}}, {"function": {"name": "GITHUB_GET_USER"}}],
            [{"function": {"name": "SHARED_TOOL"}}, {"function": {"name": "GMAIL_FETCH_EMAILS"}}],
        ]
        ts = self._toolset(inst)
        result = ts.get_tools()
        names = [t["function"]["name"] for t in result]
        self.assertEqual(names.count("SHARED_TOOL"), 1)

    @patch("composio_shim._load_composio_credentials", return_value=("k", "user_1"))
    def test_no_connected_accounts_falls_back_to_empty_search(self, _creds):
        inst = MagicMock()
        inst.connected_accounts.list.return_value = MagicMock(items=[])
        inst.tools.get.return_value = []
        ts = self._toolset(inst)
        ts.get_tools()
        kwargs = inst.tools.get.call_args.kwargs
        self.assertEqual(kwargs.get("search"), "")
        self.assertEqual(inst.tools.get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
