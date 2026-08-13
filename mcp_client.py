"""mcp_client.py -- MCP client for Jeeves.

Lets Jeeves consume tools from external Model Context Protocol (MCP) servers
-- for example Composio's hosted gateway (https://connect.composio.dev/mcp) --
alongside its native Composio SDK tools. MCP tool schemas are converted to the
OpenAI ``{"type": "function", ...}`` format the Groq brain already understands,
so they slot straight into the same agentic loop (see composio_agent.py).

How it works
------------
The official ``mcp`` Python SDK is asyncio-only, but Jeeves' runtime is
synchronous and heavily threaded (Flask, PyQt, the agent loop). Each configured
MCP server therefore gets its own daemon thread with a private asyncio event
loop that keeps one persistent ``ClientSession`` alive. Sync callers submit
coroutines with ``asyncio.run_coroutine_threadsafe`` and block on the result.

Configuration
-------------
Servers are read from ``mcp_servers`` in ``config/api_keys.json`` (a list or
dict) or the ``MCP_SERVERS`` JSON environment variable (which wins).
Example config entry::

    {
      "mcp_servers": [
        {
          "name": "composio",
          "transport": "streamablehttp",
          "url": "https://connect.composio.dev/mcp",
          "auth": {"type": "bearer", "key_ref": "composio_mcp_token"}
        }
      ]
    }

Composio note: ``connect.composio.dev/mcp`` authenticates with a **Bearer
AuthKit JWT** (obtained from the Composio dashboard's AI Clients / Connect
section) -- the SDK ``composio_api_key`` is NOT accepted there. Put the JWT in
``composio_mcp_token`` (or any key referenced by ``key_ref``).

Transports: ``streamablehttp`` (default) and ``stdio``. Header values support
``${config_key}`` templates resolved from config/api_keys.json / environment.

Graceful degradation: if the ``mcp`` package is missing or a server is
unreachable, ``get_tools()`` returns [] and ``execute()`` returns an error
string -- Jeeves keeps working without MCP tools.
"""

import asyncio
import json
import logging
import os
import threading
from typing import Any

from core.utils import get_api_config

logger = logging.getLogger("mcp_client")

CONNECT_TIMEOUT = 10.0   # seconds to wait for a server handshake
CALL_TIMEOUT = 60.0      # seconds to wait for a single tool call

_mcp_package = None
_streamable_v2 = None  # True if mcp >= 2.0 (streamable_http_client + httpx2)


def _mcp_available() -> bool:
    """Return True if the official 'mcp' package can be imported."""
    global _mcp_package
    if _mcp_package is None:
        try:
            import mcp  # noqa: F401
            _mcp_package = True
        except Exception:
            _mcp_package = False
    return _mcp_package


def _streamable_version() -> str:
    """Detect the mcp client transport API version ('v2' or 'v1')."""
    global _streamable_v2
    if _streamable_v2 is None:
        try:
            from mcp.client.streamable_http import streamable_http_client  # noqa: F401
            _streamable_v2 = True
        except Exception:
            _streamable_v2 = False
    return "v2" if _streamable_v2 else "v1"


# ── Config helpers ──────────────────────────────────────────────────────────

def _resolve_ref(ref: str) -> str:
    """Resolve '${key}' style references against config/api_keys.json / env."""
    if not ref:
        return ""
    cfg = get_api_config()
    return str(cfg.get(ref, "") or os.environ.get(ref, "") or "").strip()


def _build_headers(spec: dict) -> dict:
    """Build request headers for a server spec (auth block + ${key} templates)."""
    headers: dict = dict(spec.get("headers") or {})
    auth = spec.get("auth") or {}
    if isinstance(auth, dict):
        auth_type = str(auth.get("type", "")).lower()
        if auth_type == "header":
            name = auth.get("name")
            value = auth.get("value") or _resolve_ref(auth.get("key_ref") or "")
            if name and value:
                headers[str(name)] = value
        elif auth_type == "bearer":
            token = auth.get("token") or _resolve_ref(auth.get("key_ref") or "")
            if token:
                headers["Authorization"] = f"Bearer {token}"
    # Expand ${config_key} templates in any header value
    for key, value in list(headers.items()):
        if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
            headers[key] = _resolve_ref(value[2:-1])
    return headers


def load_mcp_servers() -> list[dict]:
    """Return the list of MCP server specs from config and/or environment.

    Order of precedence: ``MCP_SERVERS`` env var (JSON list) > ``mcp_servers``
    in ``config/api_keys.json``.
    """
    config = get_api_config()
    servers = config.get("mcp_servers") or []
    if isinstance(servers, dict):
        servers = list(servers.values())
    if not isinstance(servers, list):
        servers = []
    servers = [s for s in servers if isinstance(s, dict)]

    env_json = os.environ.get("MCP_SERVERS")
    if env_json:
        try:
            env_servers = json.loads(env_json)
            if isinstance(env_servers, list):
                servers = [s for s in env_servers if isinstance(s, dict)]
        except Exception as exc:
            logger.warning("Ignoring invalid MCP_SERVERS env var: %s", exc)

    return servers


# ── Tool / result conversion ────────────────────────────────────────────────

def _tool_to_openai(tool: Any) -> dict:
    """Convert an MCP tool object to the OpenAI function format Groq accepts."""
    name = str(getattr(tool, "name", "") or "")
    description = str(getattr(tool, "description", "") or "")
    schema = getattr(tool, "inputSchema", None) or {}
    if not isinstance(schema, dict):
        schema = {}
    schema = dict(schema)
    schema.pop("$schema", None)
    schema.pop("$id", None)
    if schema.get("type") != "object":
        # Some servers omit the top-level "type"; rebuild as an object while
        # preserving required fields.
        required = schema.get("required")
        schema = {"type": "object", "properties": schema.get("properties") or {}}
        if required:
            schema["required"] = required
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": schema},
    }


def _result_to_text(result: Any, tool_name: str) -> str:
    """Flatten an MCP CallToolResult into plain text for the LLM."""
    if getattr(result, "isError", False):
        raise RuntimeError(f"MCP tool '{tool_name}' reported an error")
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        btype = getattr(block, "type", "text")
        if btype == "text":
            text = getattr(block, "text", None)
            if text is None:
                # Newer mcp SDKs renamed TextContent.text -> TextContent.content
                text = getattr(block, "content", "")
            if text:
                parts.append(str(text))
        elif hasattr(block, "model_dump"):
            parts.append(json.dumps(block.model_dump(), default=str))
        else:
            parts.append(str(block))
    structured = getattr(result, "structuredContent", None)
    if structured:
        parts.append(json.dumps(structured, default=str))
    return "\n".join(p for p in parts if p)


# ── Per-server connection (dedicated event-loop thread) ─────────────────────

class MCPServerConnection:
    """One persistent MCP session, owned by a dedicated event-loop thread."""

    def __init__(self, spec: dict):
        self.name = str(spec.get("name") or "mcp")
        self.spec = spec
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session = None
        self._transport_cm = None
        self._ready = threading.Event()
        self._error: str | None = None
        self._tools_cache: list[dict] | None = None
        self._tool_names: set[str] | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name=f"mcp-{self.name}", daemon=True
        )
        self._thread.start()

    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._open())
        finally:
            loop.run_forever()

    async def _open(self):
        try:
            if not _mcp_available():
                raise RuntimeError("the 'mcp' package is not installed")
            from mcp import ClientSession

            transport = str(self.spec.get("transport") or "streamablehttp").lower()
            headers = _build_headers(self.spec)

            if transport == "stdio":
                from mcp import StdioServerParameters
                from mcp.client.stdio import stdio_client

                command = self.spec.get("command", "")
                if not command:
                    raise ValueError(f"MCP server '{self.name}': stdio requires 'command'")
                params = StdioServerParameters(
                    command=command,
                    args=list(self.spec.get("args") or []),
                    env=self.spec.get("env"),
                )
                self._transport_cm = stdio_client(params)
            else:
                url = self.spec.get("url", "")
                if not url:
                    raise ValueError(f"MCP server '{self.name}': missing 'url'")
                if _streamable_version() == "v2":
                    from mcp.client.streamable_http import (
                        create_mcp_http_client,
                        streamable_http_client,
                    )
                    # mcp >= 2.0: headers are carried by an httpx2.AsyncClient.
                    http_client = (
                        create_mcp_http_client(headers=headers) if headers else None
                    )
                    self._transport_cm = streamable_http_client(
                        url, http_client=http_client
                    )
                else:
                    from mcp.client.streamable_http import streamablehttp_client
                    self._transport_cm = streamablehttp_client(url, headers=headers)

            streams = await self._transport_cm.__aenter__()
            read, write = streams[0], streams[1]
            self._session = ClientSession(read, write)
            # ClientSession is an async context manager: entering it starts the
            # dispatcher/run loop, which initialize() requires (mcp >= 2.0).
            await self._session.__aenter__()
            await self._session.initialize()
            logger.info("[mcp:%s] connected (%s)", self.name, transport)
        except Exception as exc:  # noqa: BLE001 -- report and degrade
            self._error = f"{type(exc).__name__}: {exc}"
            logger.warning("[mcp:%s] connection failed: %s", self.name, self._error)
        finally:
            self._ready.set()

    def is_connected(self) -> bool:
        return self._session is not None

    def error(self) -> str | None:
        return self._error

    def _submit(self, coro, timeout: float):
        if self._loop is None or not (self._thread and self._thread.is_alive()):
            raise ConnectionError(f"MCP server '{self.name}' is not running")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    # -- tool operations ----------------------------------------------------

    def refresh(self):
        """Drop cached tools so the next get_tools() re-fetches from the server."""
        self._tools_cache = None
        self._tool_names = None

    def list_tools(self) -> list[dict]:
        """Return cached (or freshly fetched) tool definitions, OpenAI format."""
        if self._tools_cache is not None:
            return list(self._tools_cache)
        if self._session is None:
            return []
        try:
            tools = self._submit(self._async_list_tools(), timeout=CALL_TIMEOUT)
            self._tools_cache = tools
            self._tool_names = {t["function"]["name"] for t in tools}
            return list(tools)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[mcp:%s] list_tools failed: %s", self.name, exc)
            return []

    async def _async_list_tools(self) -> list[dict]:
        result = await self._session.list_tools()
        return [_tool_to_openai(t) for t in result.tools]

    def has_tool(self, name: str) -> bool:
        if self._tool_names is None:
            self.list_tools()
        return bool(self._tool_names) and name in self._tool_names

    def call_tool(self, name: str, arguments: dict) -> tuple[str, str | None]:
        """Execute a tool. Returns (result_text, error)."""
        if self._session is None:
            return "", self._error or f"MCP server '{self.name}' is not connected"
        try:
            text = self._submit(
                self._async_call_tool(name, arguments or {}), timeout=CALL_TIMEOUT
            )
            return text, None
        except Exception as exc:  # noqa: BLE001
            logger.warning("[mcp:%s] call_tool(%s) failed: %s", self.name, name, exc)
            return "", f"{type(exc).__name__}: {exc}"

    async def _async_call_tool(self, name: str, arguments: dict) -> str:
        result = await self._session.call_tool(name, arguments)
        return _result_to_text(result, name)

    def close(self):
        async def _shutdown():
            if self._session is not None:
                try:
                    await self._session.__aexit__(None, None, None)
                except Exception:  # noqa: BLE001
                    pass
                self._session = None
            if self._transport_cm is not None:
                try:
                    await self._transport_cm.__aexit__(None, None, None)
                except Exception:  # noqa: BLE001
                    pass
            self._transport_cm = None

        if self._loop is not None and self._thread and self._thread.is_alive():
            try:
                self._submit(_shutdown(), timeout=10)
            except Exception:  # noqa: BLE001
                pass
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:  # noqa: BLE001
                pass


# ── Manager (all configured servers) ────────────────────────────────────────

class MCPClientManager:
    """Owns connections to every configured MCP server."""

    def __init__(self, servers: list[dict] | None = None,
                 connect_timeout: float = CONNECT_TIMEOUT):
        self._servers = servers if servers is not None else load_mcp_servers()
        self._connect_timeout = connect_timeout
        self._connections: dict[str, MCPServerConnection] = {}
        self._tools_cache: list[dict] | None = None
        self._lock = threading.Lock()
        self._unavailable: str | None = None

        if not _mcp_available():
            self._unavailable = "the 'mcp' package is not installed (pip install mcp)"
            logger.warning("[mcp] disabled: %s", self._unavailable)
            return

        for spec in self._servers:
            conn = MCPServerConnection(spec)
            conn.start()
            self._connections[conn.name] = conn

    def get_tools(self) -> list[dict]:
        """All MCP tools across servers, merged into OpenAI format (cached)."""
        if self._unavailable:
            return []
        with self._lock:
            if self._tools_cache is None:
                for conn in self._connections.values():
                    conn._ready.wait(timeout=self._connect_timeout)
                merged: list[dict] = []
                for conn in self._connections.values():
                    merged.extend(conn.list_tools())
                self._tools_cache = merged
            return list(self._tools_cache)

    def server_count(self) -> int:
        return len(self._connections)

    def connected_servers(self) -> list[str]:
        return [c.name for c in self._connections.values() if c.is_connected()]

    def has_tool(self, name: str) -> bool:
        return self.find_connection(name) is not None

    def find_connection(self, name: str) -> MCPServerConnection | None:
        for conn in self._connections.values():
            if conn.has_tool(name):
                return conn
        return None

    def execute(self, name: str, arguments: dict) -> tuple[str, str | None]:
        """Execute an MCP tool. Returns (result_text, error)."""
        conn = self.find_connection(name)
        if conn is None:
            return "", f"No MCP server exposes a tool named '{name}'"
        return conn.call_tool(name, arguments or {})

    def refresh(self):
        """Drop the merged tool cache so the next get_tools() re-fetches.

        Useful after fixing credentials for a server that previously failed.
        """
        with self._lock:
            for conn in self._connections.values():
                conn.refresh()
            self._tools_cache = None

    def close_all(self):
        for conn in list(self._connections.values()):
            conn.close()
        self._connections.clear()


_manager: MCPClientManager | None = None
_manager_lock = threading.Lock()


def get_mcp_manager() -> MCPClientManager:
    """Return the process-wide MCP client manager (lazy singleton)."""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = MCPClientManager()
        return _manager
