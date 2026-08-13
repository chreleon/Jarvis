"""composio_shim.py — Shared Composio compatibility layer for Jeeves.

Provides a unified ComposioToolSet that works across multiple SDK versions:
  - Legacy `composio_openai.ComposioToolSet`
  - Newer `composio.Composio` + `composio_openai.OpenAIProvider`

All Jeeves modules (composio_agent.py, doctor.py, composio_connect.py)
should import from here instead of duplicating the fallback logic.
"""

import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger("composio_shim")

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"


# Try each SDK variant in order of preference
_composio_legacy = None
_composio_new = None
_OpenAIProvider = None

try:
    from composio_openai import ComposioToolSet as _composio_legacy, App
except Exception:
    _composio_legacy = None
    try:
        from composio import Composio as _composio_new
        from composio_openai import OpenAIProvider as _OpenAIProvider
    except Exception:
        _composio_new = None
        _OpenAIProvider = None


class App:
    """Lightweight App enum for environments where composio_openai is not installed."""
    GITHUB = "GITHUB"
    GMAIL = "GMAIL"
    GOOGLECALENDAR = "GOOGLECALENDAR"


def _is_expired_account_error(exc: Exception) -> bool:
    """True when a tool-execution error reports an expired connection."""
    text = str(exc)
    return (
        "410" in text
        or "expired" in text.lower()
        or "ConnectedAccountExpired" in text
    )


def _app_to_slug(app: Any) -> str:
    """Normalize an App enum / constant / slug string to a lowercase toolkit slug.

    Handles ``App.GITHUB``, ``GITHUB``, ``<App.GITHUB: 'GITHUB'>`` and plain
    ``"github"`` -- all resolve to ``"github"``.
    """
    name = app if isinstance(app, str) else str(app)
    tokens = re.findall(r"[A-Za-z0-9_]+", name)
    return (tokens[-1] if tokens else name).lower()


def _load_composio_credentials() -> tuple[str, str]:
    """Return (api_key, user_id) from environment or config/api_keys.json."""
    api_key = (os.environ.get("COMPOSIO_API_KEY") or os.environ.get("COMPOSIO_KEY") or "").strip()
    user_id = (os.environ.get("COMPOSIO_USER_ID") or "").strip()

    if not api_key or not user_id:
        try:
            if CONFIG_PATH.exists():
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                if not api_key:
                    api_key = str(data.get("composio_api_key", "") or "").strip()
                if not user_id:
                    user_id = str(data.get("composio_user_id", "") or "").strip()
        except Exception as exc:
            logger.warning("Failed to read config/api_keys.json: %s", exc)

    return api_key, user_id or "default"


class ComposioToolSet:
    """Unified Composio toolset that adapts to whichever SDK version is installed.

    If neither the legacy nor new SDK is available, methods return empty results
    so the rest of Jeeves continues to work without crashing.
    """

    def __init__(self):
        self._backend = None
        self._composio_instance = None
        self._accounts_cache: dict[str, str] | None = None
        self._accounts_lock = threading.Lock()

        if _composio_legacy is not None:
            self._backend = "legacy"
            try:
                api_key, _ = _load_composio_credentials()
                kwargs = {"api_key": api_key} if api_key else {}
                self._composio_instance = _composio_legacy(**kwargs)
            except Exception as exc:
                logger.warning("Failed to init legacy ComposioToolSet: %s", exc)
        elif _composio_new is not None and _OpenAIProvider is not None:
            self._backend = "new"
            try:
                api_key, _ = _load_composio_credentials()
                if api_key:
                    self._composio_instance = _composio_new(
                        api_key=api_key, provider=_OpenAIProvider()
                    )
            except Exception as exc:
                logger.warning("Failed to init new Composio SDK: %s", exc)
        else:
            self._backend = "none"

    def get_tools(self, apps: list[Any] | None = None) -> list[Any]:
        """Fetch tools for the given apps (or all connected apps)."""
        if self._composio_instance is None:
            return []

        if self._backend == "legacy":
            return self._composio_instance.get_tools(apps=apps)

        # New SDK path
        toolkits = []
        for app in apps or []:
            slug = _app_to_slug(app)
            if slug in {"github", "gmail", "googlecalendar"}:
                toolkits.append(slug)

        try:
            _, user_id = _load_composio_credentials()

            # Explicit app filter -> one batched request for those toolkits.
            if toolkits:
                return self._composio_instance.tools.get(
                    user_id=user_id, toolkits=toolkits, limit=100
                )

            # No explicit filter -> fetch tools for the user's actually-connected
            # toolkits. A bare empty search returns an arbitrary first page of
            # unrelated tools, and one batched request gets truncated by `limit`
            # (github alone filled a 200-tool page), so fetch each toolkit
            # separately and merge the results.
            #
            # NOTE: `limit` per toolkit is a documented cap -- toolkits with more
            # tools than the limit (e.g. github) expose only their first N tools
            # to callers. Raise the limit or paginate here if full coverage is
            # needed.
            connected = self._get_connected_accounts(user_id)
            if not connected:
                return self._composio_instance.tools.get(
                    user_id=user_id, search="", limit=200
                )

            merged: list[Any] = []
            seen: set[str] = set()
            for toolkit in sorted(connected.keys()):
                try:
                    batch = self._composio_instance.tools.get(
                        user_id=user_id, toolkits=[toolkit], limit=200
                    )
                except Exception as exc:
                    logger.warning("Failed to fetch tools for %s: %s", toolkit, exc)
                    continue
                for tool in batch:
                    name = ""
                    if isinstance(tool, dict):
                        name = str((tool.get("function") or {}).get("name", "") or "")
                    if name and name in seen:
                        continue
                    if name:
                        seen.add(name)
                    merged.append(tool)
            return merged
        except Exception as exc:
            logger.warning("Failed to fetch Composio tools: %s", exc)
            return []

    def initiate_connection(self, app: Any, **kwargs: Any) -> Any:
        """Start an OAuth connection for ``app`` and return a connection request.

        The returned object exposes a redirect URL (``redirectUrl`` or
        ``redirect_url``) that the caller should open in a browser.

        - Legacy SDK: delegates to the underlying ``initiate_connection``.
        - New SDK (composio >= 0.18): resolves the app's auth config, then uses
          ``connected_accounts.link()`` (the recommended API -- ``initiate()``
          was retired for Composio-managed OAuth) and returns the
          ``ConnectionRequest``.
        - No usable SDK: raises ``AttributeError`` so callers can fall back to
          opening the Composio dashboard manually.
        """
        if self._composio_instance is None:
            raise AttributeError("Composio SDK not available")

        user_id = kwargs.get("user_id") or kwargs.get("entity_id") or ""
        if not user_id:
            _, user_id = _load_composio_credentials()

        slug = _app_to_slug(app)

        if self._backend == "legacy":
            initiate = getattr(self._composio_instance, "initiate_connection", None)
            if initiate is None:
                raise AttributeError("Composio SDK does not expose initiate_connection")
            # Legacy SDK expects an App enum member, not a bare slug string.
            app_enum = getattr(App, slug.upper(), app)
            return initiate(app=app_enum, entity_id=user_id)

        # New SDK path: resolve auth config, then create a connect link.
        accounts = getattr(self._composio_instance, "connected_accounts", None)
        if accounts is None:
            raise AttributeError("Composio SDK does not expose connected_accounts")

        auth_config_id = self._resolve_auth_config_id(slug)
        if not auth_config_id:
            raise RuntimeError(
                f"No auth config found for app '{slug}'. "
                "Set one up in the Composio dashboard first."
            )

        link = getattr(accounts, "link", None)
        if callable(link):
            return link(
                user_id=user_id, auth_config_id=auth_config_id, allow_multiple=True
            )
        return accounts.initiate(
            user_id=user_id, auth_config_id=auth_config_id, allow_multiple=True
        )

    def _resolve_auth_config_id(self, slug: str) -> str:
        """Return the id of the first auth config for ``slug`` ('' if none)."""
        try:
            listing = self._composio_instance.auth_configs.list(
                toolkit_slug=slug, limit=10, show_disabled=False
            )
        except Exception as exc:
            logger.warning("Failed to list auth configs for %s: %s", slug, exc)
            return ""
        items = getattr(listing, "items", None) or []
        if not items:
            return ""
        return str(getattr(items[0], "id", "") or "")

    def handle_tool_calls(self, response: Any) -> list[dict]:
        """Execute tool calls from an LLM response.

        - Legacy SDK: delegates to the underlying ``handle_tool_calls``.
        - New SDK (composio >= 0.18): executes each tool call via
          ``client.tools.execute()``. Tool names ARE tool slugs, so no
          remapping is needed; the connected account for the tool's toolkit is
          resolved from ``connected_accounts.list()`` when possible.
        - No SDK: returns [] / error stubs so callers keep working.
        """
        if self._backend == "legacy" and self._composio_instance is not None:
            return self._composio_instance.handle_tool_calls(response)
        if self._backend != "new" or self._composio_instance is None:
            return []

        choice = getattr(response, "choices", None)
        message = getattr(choice[0], "message", None) if choice else None
        tool_calls = getattr(message, "tool_calls", None) or []
        if not tool_calls:
            return []

        _, user_id = _load_composio_credentials()
        results: list[dict] = []
        for tool_call in tool_calls:
            try:
                results.append(self._execute_tool_call(tool_call, user_id))
            except Exception as exc:
                logger.warning(
                    "Composio tool execution failed (%s): %s",
                    getattr(tool_call, "id", "?"), exc,
                )
                results.append({"successful": False, "error": str(exc), "data": None})
        return results

    def _execute_tool_call(self, tool_call: Any, user_id: str) -> dict:
        """Execute a single OpenAI-style tool call on the new SDK."""
        function = getattr(tool_call, "function", None)
        name = getattr(function, "name", "") or ""
        if not name:
            return {
                "successful": False,
                "error": "Tool call is missing a function name",
                "data": None,
            }
        raw_arguments = getattr(function, "arguments", None) or ""
        if isinstance(raw_arguments, dict):
            arguments = raw_arguments
        else:
            try:
                arguments = json.loads(raw_arguments)
                if not isinstance(arguments, dict):
                    arguments = {}
            except (json.JSONDecodeError, TypeError):
                arguments = {}

        try:
            result = self._composio_instance.tools.execute(
                slug=name,
                arguments=arguments,
                user_id=user_id,
                connected_account_id=self._resolve_connected_account(name, user_id),
                # Agentic calls fetch the latest tool version; skip the guard that
                # would otherwise raise ToolVersionRequiredError for 'latest'.
                dangerously_skip_version_check=True,
            )
        except Exception as exc:
            # A connection can expire while the process runs (long-lived daemon
            # / agent loop). Drop the cached account map and retry once so a
            # freshly re-authorized ACTIVE connection is picked up instead of
            # every call failing with 410 ConnectedAccountExpired.
            if not _is_expired_account_error(exc):
                raise
            with self._accounts_lock:
                self._accounts_cache = None
            result = self._composio_instance.tools.execute(
                slug=name,
                arguments=arguments,
                user_id=user_id,
                connected_account_id=self._resolve_connected_account(name, user_id),
                dangerously_skip_version_check=True,
            )
        if hasattr(result, "model_dump"):
            result = result.model_dump()
        return result

    def _resolve_connected_account(self, slug: str, user_id: str) -> str | None:
        """Return the connected-account id for a tool's toolkit ('' if none)."""
        toolkit = (slug or "").split("_")[0].strip().lower()
        if not toolkit:
            return None
        return self._get_connected_accounts(user_id).get(toolkit)

    def _get_connected_accounts(self, user_id: str) -> dict[str, str]:
        """Map toolkit slug -> connected-account id (cached per instance).

        ACTIVE connections are preferred over EXPIRED/INITIALIZING ones so a
        stale account never shadows a working one; non-active accounts are
        only used as a fallback when no ACTIVE connection exists for a toolkit.

        The map is only cached after a successful list -- a transient failure
        retries on the next call (listing is read-only, so unlike tool
        execution there is no duplicate-side-effect risk). The cache is also
        dropped by _execute_tool_call when execution reports an expired
        account, so a fresh re-authorization is picked up without a restart.
        """
        with self._accounts_lock:
            if self._accounts_cache is not None:
                return self._accounts_cache
            mapping: dict[str, str] = {}
            pending: dict[str, str] = {}
            success = False
            try:
                listing = self._composio_instance.connected_accounts.list(
                    user_ids=[user_id]
                )
                for item in getattr(listing, "items", None) or []:
                    account_id = str(getattr(item, "id", "") or "")
                    toolkit = getattr(getattr(item, "toolkit", None), "slug", "") or ""
                    if not account_id or not toolkit:
                        continue
                    # Prefer ACTIVE connections: a stale EXPIRED account listed
                    # first would otherwise be selected and fail every call
                    # (observed: 410 ConnectedAccountExpired on a live Gmail run).
                    if str(getattr(item, "status", "") or "").upper() == "ACTIVE":
                        mapping.setdefault(toolkit, account_id)
                    else:
                        pending.setdefault(toolkit, account_id)
                # Only fall back to a non-active account when no ACTIVE one exists
                # for that toolkit.
                for toolkit, account_id in pending.items():
                    mapping.setdefault(toolkit, account_id)
                success = True
            except Exception as exc:
                logger.warning("Failed to list connected accounts: %s", exc)
            if success:
                self._accounts_cache = mapping
            return mapping
