"""Unit tests for mcp_client.py -- the MCP client for Jeeves.

Covers config loading (auto-added Composio gateway, env override), header
building, MCP tool -> OpenAI conversion, call-result flattening, and the
manager's tool routing -- using fake connections so no real MCP server or the
'mcp' package is required.
"""

import json
import os
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import mcp_client
from mcp_client import (
    MCPClientManager,
    _build_headers,
    _result_to_text,
    _tool_to_openai,
    load_mcp_servers,
)


def _cfg(extra=None):
    base = {"composio_api_key": "ak_test", "composio_user_id": "user1"}
    base.update(extra or {})
    return base


class TestLoadMcpServers(unittest.TestCase):
    def test_no_servers_returns_empty(self):
        with patch("mcp_client.get_api_config", return_value={}):
            self.assertEqual(load_mcp_servers(), [])

    def test_config_servers_returned_as_is(self):
        with patch("mcp_client.get_api_config", return_value=_cfg({
            "mcp_servers": [
                {"name": "composio", "url": "https://connect.composio.dev/mcp",
                 "transport": "streamablehttp",
                 "auth": {"type": "bearer", "key_ref": "composio_mcp_token"}}
            ]
        })):
            servers = load_mcp_servers()
        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0]["name"], "composio")
        self.assertEqual(servers[0]["auth"]["key_ref"], "composio_mcp_token")

    def test_dict_form_servers_accepted(self):
        with patch("mcp_client.get_api_config", return_value=_cfg({
            "mcp_servers": {"a": {"name": "a", "url": "https://a"}}
        })):
            servers = load_mcp_servers()
        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0]["name"], "a")

    def test_env_var_overrides_config(self):
        with patch("mcp_client.get_api_config", return_value=_cfg()), \
             patch.dict(os.environ, {"MCP_SERVERS": json.dumps([
                 {"name": "envsrv", "url": "https://env", "transport": "streamablehttp"}
             ])}, clear=False):
            servers = load_mcp_servers()
        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0]["name"], "envsrv")


class TestBuildHeaders(unittest.TestCase):
    def test_header_auth_with_key_ref(self):
        with patch("mcp_client.get_api_config", return_value=_cfg()):
            headers = _build_headers({
                "auth": {"type": "header", "name": "x-consumer-api-key",
                         "key_ref": "composio_api_key"},
            })
        self.assertEqual(headers.get("x-consumer-api-key"), "ak_test")

    def test_bearer_auth(self):
        with patch("mcp_client.get_api_config", return_value=_cfg()):
            headers = _build_headers({"auth": {"type": "bearer", "key_ref": "composio_api_key"}})
        self.assertEqual(headers.get("Authorization"), "Bearer ak_test")

    def test_template_expansion_in_headers(self):
        with patch("mcp_client.get_api_config", return_value=_cfg()):
            headers = _build_headers({"headers": {"X-Key": "${composio_api_key}"}})
        self.assertEqual(headers.get("X-Key"), "ak_test")


class TestToolConversion(unittest.TestCase):
    def test_mcp_tool_to_openai(self):
        tool = SimpleNamespace(
            name="COMPOSIO_SEARCH_TOOLS",
            description="Search the Composio catalog",
            inputSchema={"type": "object", "$schema": "http://x",
                         "properties": {"q": {"type": "string"}},
                         "required": ["q"]},
        )
        out = _tool_to_openai(tool)
        self.assertEqual(out["type"], "function")
        self.assertEqual(out["function"]["name"], "COMPOSIO_SEARCH_TOOLS")
        self.assertNotIn("$schema", out["function"]["parameters"])
        self.assertEqual(out["function"]["parameters"]["type"], "object")

    def test_missing_schema_defaults_to_object(self):
        tool = SimpleNamespace(name="t", description="d", inputSchema=None)
        out = _tool_to_openai(tool)
        self.assertEqual(out["function"]["parameters"]["type"], "object")


class TestResultToText(unittest.TestCase):
    def test_text_block_legacy_field(self):
        block = SimpleNamespace(type="text", text="hello")
        result = SimpleNamespace(isError=False, content=[block])
        self.assertEqual(_result_to_text(result, "t"), "hello")

    def test_text_block_new_content_field(self):
        block = SimpleNamespace(type="text", text=None, content="world")
        result = SimpleNamespace(isError=False, content=[block])
        self.assertEqual(_result_to_text(result, "t"), "world")

    def test_error_flag_raises(self):
        result = SimpleNamespace(isError=True, content=[])
        with self.assertRaises(RuntimeError):
            _result_to_text(result, "t")

    def test_structured_content_appended(self):
        result = SimpleNamespace(isError=False, content=[], structuredContent={"a": 1})
        self.assertIn('"a": 1', _result_to_text(result, "t"))


class _FakeConnection:
    def __init__(self, spec, tool_names=None):
        self.name = spec["name"]
        self.spec = spec
        names = tuple(tool_names or spec.get("tools") or ("alpha", "beta"))
        self.tools = [
            {"type": "function",
             "function": {"name": n, "description": "", "parameters": {"type": "object", "properties": {}}}}
            for n in names
        ]
        self._ready = threading.Event()
        self._ready.set()
        self.calls = []

    def start(self):
        pass

    def is_connected(self):
        return True

    def list_tools(self):
        return list(self.tools)

    def refresh(self):
        pass

    def has_tool(self, name):
        return any(t["function"]["name"] == name for t in self.tools)

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return f"ran {name}", None

    def close(self):
        pass


class TestMCPClientManager(unittest.TestCase):
    def test_unavailable_package_returns_no_tools(self):
        with patch("mcp_client._mcp_available", return_value=False), \
             patch("mcp_client.MCPServerConnection", _FakeConnection):
            manager = MCPClientManager(servers=[{"name": "s1"}])
        self.assertEqual(manager.get_tools(), [])
        self.assertEqual(manager.execute("alpha", {}), ("", "No MCP server exposes a tool named 'alpha'"))

    def test_get_tools_merges_across_servers(self):
        with patch("mcp_client._mcp_available", return_value=True), \
             patch("mcp_client.MCPServerConnection", _FakeConnection):
            manager = MCPClientManager(servers=[
                {"name": "a", "tools": ("t1",)},
                {"name": "b", "tools": ("t2", "t3")},
            ])
        names = [t["function"]["name"] for t in manager.get_tools()]
        self.assertEqual(sorted(names), ["t1", "t2", "t3"])

    def test_has_tool_and_execute_routing(self):
        with patch("mcp_client._mcp_available", return_value=True), \
             patch("mcp_client.MCPServerConnection", _FakeConnection):
            manager = MCPClientManager(servers=[{"name": "a", "tools": ("alpha",)}])
        self.assertTrue(manager.has_tool("alpha"))
        self.assertFalse(manager.has_tool("nope"))
        text, err = manager.execute("alpha", {"x": 1})
        self.assertEqual(text, "ran alpha")
        self.assertIsNone(err)
        text, err = manager.execute("nope", {})
        self.assertIsNotNone(err)

    def test_execute_routes_to_correct_server(self):
        with patch("mcp_client._mcp_available", return_value=True), \
             patch("mcp_client.MCPServerConnection", _FakeConnection):
            manager = MCPClientManager(servers=[
                {"name": "a", "tools": ("t1",)},
                {"name": "b", "tools": ("t2",)},
            ])
        manager.execute("t2", {})
        conn_b = manager.find_connection("t2")
        self.assertEqual(conn_b.name, "b")
        self.assertEqual(conn_b.calls[0][0], "t2")

    def test_refresh_clears_cache(self):
        with patch("mcp_client._mcp_available", return_value=True), \
             patch("mcp_client.MCPServerConnection", _FakeConnection):
            manager = MCPClientManager(servers=[{"name": "a", "tools": ("t1",)}])
        self.assertEqual([t["function"]["name"] for t in manager.get_tools()], ["t1"])
        manager.refresh()
        self.assertIsNone(manager._tools_cache)
        self.assertEqual([t["function"]["name"] for t in manager.get_tools()], ["t1"])


class TestAgentWiringHelpers(unittest.TestCase):
    def test_response_with_calls_shape(self):
        from composio_agent import _response_with_calls
        tc = MagicMock()
        wrapped = _response_with_calls([tc])
        self.assertIs(wrapped.choices[0].message.tool_calls[0], tc)


@unittest.skipUnless(mcp_client._mcp_available(), "requires the 'mcp' package")
class TestEndToEndStdio(unittest.TestCase):
    """Full round trip against a real in-process MCP server over stdio."""

    def test_list_and_call_tool_over_stdio(self):
        import tempfile

        server_code = (
            "import asyncio\n"
            "from mcp.server.mcpserver import MCPServer\n"
            "server = MCPServer('demo')\n"
            "@server.tool()\n"
            "def add(a: float, b: float) -> str:\n"
            "    '''Add two numbers.'''\n"
            "    return str(a + b)\n"
            "asyncio.run(server.run_stdio_async())\n"
        )
        fd, path = tempfile.mkstemp(suffix=".py", prefix="mcp_demo_server_")
        with os.fdopen(fd, "w") as f:
            f.write(server_code)
        try:
            manager = MCPClientManager(servers=[
                {"name": "demo", "transport": "stdio",
                 "command": sys.executable, "args": [path]}
            ])
            names = [t["function"]["name"] for t in manager.get_tools()]
            self.assertEqual(names, ["add"])
            text, err = manager.execute("add", {"a": 2, "b": 3})
            self.assertIsNone(err)
            self.assertEqual(text.strip(), "5.0")
            manager.close_all()
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
