"""
jeeves_mcp_server.py — Upgraded MCP-style HTTP bridge for Jeeves.

Gojo (Architecture) upgrades:
  - Auth via X-Jeeves-Secret header + Bearer token (shared pattern with agent/mcp_server.py)
  - Request correlation IDs for end-to-end traceability
  - Lazy config loading from shared core.utils
  - Shared error response format
  - CORS support for web clients

Stark (Engineering) upgrades:
  - Input validation (non-empty prompts, content-type checks)
  - Correlation IDs in all log messages and error responses
  - Consistent JSON-RPC error codes across all endpoints
  - Graceful degradation when config is missing
  - Config/secrets cached to avoid per-request disk reads
  - Request timeout on invoker calls to prevent hangs

YinYang (Performance) upgrades:
  - Lazy imports — composio_agent only loaded when first request hits
  - Eager module-level work eliminated (startup speed)
  - Config loaded on demand, not at import time
  - Secrets cached after first load

Endpoints:
  - GET  /          -> server info
  - GET  /health    -> health check with version + uptime
  - POST /call      -> direct tool invocation (simpler clients)
  - POST /mcp       -> JSON-RPC 2.0 MCP protocol
"""

import logging
import os
import time
import uuid
from functools import wraps
from pathlib import Path

from flask import Flask, g, jsonify, request

from core.utils import get_api_config

# ── Logging ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[MCP] %(levelname)s %(message)s",
)
logger = logging.getLogger("jeeves_mcp")

# ── App setup ─────────────────────────────────────────────────────────────
app = Flask(__name__)

# Track server start time for uptime reporting
_START_TIME = time.time()

# ── Stark: cached config/secrets (avoid per-request disk reads) ────────────
_CONFIG_CACHE: dict | None = None
_SECRETS_CACHE: tuple[str, str] | None = None


def _get_cached_config() -> dict:
    """Return cached config, reloading only if the cache is empty.

    Stark/YinYang: avoids reading config/api_keys.json from disk on every
    request. The config is small and changes rarely, so in-memory caching
    is safe and significantly reduces I/O overhead.
    """
    global _CONFIG_CACHE
    if _CONFIG_CACHE is None:
        _CONFIG_CACHE = get_api_config()
    return _CONFIG_CACHE


def _invalidate_config_cache():
    """Force a config reload on the next access. Called internally when
    secrets change at runtime (rare, but available for admin endpoints)."""
    global _CONFIG_CACHE, _SECRETS_CACHE
    _CONFIG_CACHE = None
    _SECRETS_CACHE = None


# ── YinYang: lazy-load the heavy (composio_agent) import ──────────────────
_INVOKER = None



def _get_invoker():
    """Lazy-load composio_agent on first request — avoids startup delay and
    allows config to be available before the import runs."""
    global _INVOKER
    if _INVOKER is None:
        from composio_agent import run_agentic_task
        _INVOKER = run_agentic_task
    return _INVOKER


# ── Stark: request timeout for invoker calls ───────────────────────────────
INVOKER_TIMEOUT = 120  # seconds — prevents runaway agent calls

# Module-level thread pool — avoids allocating a new thread per request
import concurrent.futures
_INVOKER_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="jeeves_invoke"
)


def _invoke_with_timeout(prompt: str, system_prompt: str | None = None) -> str:
    """Call the composio_agent invoker with a hard timeout.

    Stark: if the LLM or Composio hangs, this prevents the HTTP request
    from blocking indefinitely. Uses a shared thread pool so we don't
    allocate + tear down threads on every request.
    """
    invoker = _get_invoker()
    future = _INVOKER_POOL.submit(invoker, prompt, system_prompt=system_prompt)
    try:
        return future.result(timeout=INVOKER_TIMEOUT)
    except concurrent.futures.TimeoutError:
        logger.error("[%s] Invoker timed out after %ss",
                     getattr(g, "correlation_id", "?"), INVOKER_TIMEOUT)
        raise TimeoutError(
            f"Jeeves did not respond within {INVOKER_TIMEOUT} seconds. "
            f"The request may still be processing — try a simpler prompt."
        )


# ── Gojo: CORS support (Stark: security with origin validation) ────────────
_ALLOWED_ORIGINS = os.environ.get("JEEVES_CORS_ORIGINS", "*").split(",")


@app.after_request
def _add_cors_headers(response):
    """Attach CORS headers to every response so web clients can call
    the MCP bridge without being blocked by browser security policies."""
    origin = request.headers.get("Origin", "")
    if "*" in _ALLOWED_ORIGINS or origin in _ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = (
            "*" if "*" in _ALLOWED_ORIGINS else origin
        )
        response.headers["Access-Control-Allow-Headers"] = (
            "Content-Type, Authorization, X-Jeeves-Secret, X-Correlation-Id"
        )
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Max-Age"] = "3600"
    return response


# ── Gojo: correlation ID middleware ───────────────────────────────────────
def _correlation_id():
    """Generate or extract a correlation ID for the current request.

    Prefers X-Correlation-Id from the caller so multi-hop traces remain
    connected; falls back to a fresh UUID.
    """
    return request.headers.get("X-Correlation-Id") or str(uuid.uuid4())


@app.before_request
def _attach_correlation_id():
    g.correlation_id = _correlation_id()


# ── Stark: explicit OPTIONS handler for CORS preflight ──────────────────────
@app.before_request
def _handle_cors_preflight():
    """Return 200 for CORS preflight (OPTIONS) requests immediately.

    Without this, Flask returns 405 on unbounded OPTIONS routes, which
    some strict-mode CORS clients reject even if the response has headers.
    """
    if request.method == "OPTIONS":
        return jsonify({"ok": True}), 200


# ── Gojo: auth ─────────────────────────────────────────────────────────────
def _load_secrets() -> tuple[str, str]:
    """Load API secret and callback secret from config or environment.

    Returns (api_secret, callback_secret). Both may be empty if not configured,
    which disables auth and HMAC signing respectively.

    Stark: secrets are cached after first load to avoid repeated disk reads.
    """
    global _SECRETS_CACHE
    if _SECRETS_CACHE is not None:
        return _SECRETS_CACHE

    config = _get_cached_config()
    api_secret = (
        os.environ.get("JEEVES_API_SECRET")
        or config.get("jeeves_api_secret")
        or ""
    )
    callback_secret = (
        os.environ.get("JEEVES_CALLBACK_SECRET")
        or config.get("jeeves_callback_secret")
        or ""
    )
    _SECRETS_CACHE = (api_secret.strip(), callback_secret.strip())
    return _SECRETS_CACHE


def _check_auth() -> bool:
    """Validate the incoming request against the configured API secret.

    Supports both:
      - X-Jeeves-Secret header (simple, matches agent/mcp_server.py)
      - Authorization: Bearer <secret> header (standard)
    """
    api_secret, _ = _load_secrets()
    if not api_secret:
        return True  # No auth configured — allow all (development mode)

    x_secret = request.headers.get("X-Jeeves-Secret", "")
    if x_secret == api_secret:
        return True

    auth_header = request.headers.get("Authorization", "")
    parts = auth_header.split()
    if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1] == api_secret:
        return True

    return False


def require_auth(f):
    """Decorator that enforces authentication on protected endpoints."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _check_auth():
            cid = getattr(g, "correlation_id", "?")
            logger.warning("[%s] Auth rejected: %s %s", cid, request.method, request.path)
            return jsonify({
                "ok": False,
                "error": "Unauthorized",
                "correlation_id": cid,
            }), 401
        return f(*args, **kwargs)
    return decorated


# ── Stark: input validation ────────────────────────────────────────────────
def _validate_payload(payload: dict, endpoint: str) -> str | None:
    """Validate a request payload. Returns an error message or None if valid."""
    if not isinstance(payload, dict):
        return "Request body must be a JSON object"

    if endpoint == "call":
        name = payload.get("name") or payload.get("tool")
        if not name:
            return "Missing field: name or tool"
        if name != "jeeves_run":
            return f"Unknown tool: {name}"

    if endpoint == "mcp":
        method = payload.get("method")
        if not method:
            return "Missing field: method"

    return None


def _validate_prompt(prompt: str | None) -> str | None:
    """Validate a prompt string. Returns an error message or None if valid."""
    if not prompt or not prompt.strip():
        return "A non-empty prompt is required"
    return None


# ── Gojo: response helpers ────────────────────────────────────────────────
def _ok_response(data: dict, status: int = 200):
    """Standard success response with correlation ID."""
    data["correlation_id"] = getattr(g, "correlation_id", "?")
    return jsonify(data), status


def _error_response(message: str, status: int = 400, details: dict | None = None):
    """Standard error response with correlation ID."""
    resp = {
        "ok": False,
        "error": message,
        "correlation_id": getattr(g, "correlation_id", "?"),
    }
    if details:
        resp.update(details)
    return jsonify(resp), status


def _jsonrpc_ok(request_id, result: dict):
    """Standard JSON-RPC 2.0 success response."""
    return jsonify({
        "jsonrpc": "2.0",
        "id": request_id,
        "result": result,
    })


def _jsonrpc_err(request_id, code: int, message: str):
    """Standard JSON-RPC 2.0 error response."""
    return jsonify({
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }), 200  # JSON-RPC errors use 200 status


# ── Stark: pubic endpoints ────────────────────────────────────────────────

@app.get("/")
def index():
    return _ok_response({
        "name": "jeeves-mcp",
        "version": "2.0.0",
        "description": "Upgraded MCP bridge — auth, correlation IDs, shared config.",
        "endpoints": {
            "health": "GET /health",
            "call":   "POST /call (auth required)",
            "mcp":    "POST /mcp (auth required)",
        },
    })


@app.get("/health")
def health():
    config = _get_cached_config()
    has_secret = bool(config.get("jeeves_api_secret"))
    uptime_s = int(time.time() - _START_TIME)
    return _ok_response({
        "ok": True,
        "service": "jeeves-mcp",
        "version": "2.0.0",
        "uptime_seconds": uptime_s,
        "auth_configured": has_secret,
    })


@app.post("/call")
@require_auth
def call_tool():
    cid = getattr(g, "correlation_id", "?")
    content_type = request.content_type or ""

    # Stark: content-type validation
    if "application/json" not in content_type:
        logger.warning("[%s] /call: invalid content-type '%s'", cid, content_type)
        return _error_response(f"Expected application/json, got {content_type}")

    payload = request.get_json(silent=True) or {}

    # Stark: input validation
    validation_error = _validate_payload(payload, "call")
    if validation_error:
        logger.warning("[%s] /call: validation failed: %s", cid, validation_error)
        return _error_response(validation_error)

    name = payload.get("name") or payload.get("tool")
    arguments = payload.get("arguments") or payload.get("args") or {}
    prompt = (arguments.get("prompt") or "").strip()
    system_prompt = arguments.get("system_prompt")

    # Stark: prompt validation
    prompt_error = _validate_prompt(prompt)
    if prompt_error:
        logger.warning("[%s] /call: prompt validation failed", cid)
        return _error_response(prompt_error)

    # Gojo: log with correlation ID
    logger.info("[%s] /call invoking jeeves_run (prompt=%s…)", cid, prompt[:80])

    try:
        result = _invoke_with_timeout(prompt, system_prompt=system_prompt)
        logger.info("[%s] /call succeeded (result=%s…)", cid, str(result)[:80])
        return _ok_response({"ok": True, "result": result})
    except Exception as exc:
        logger.exception("[%s] /call invocation failed", cid)
        return _error_response(str(exc), status=500)


@app.post("/mcp")
@require_auth
def mcp_endpoint():
    cid = getattr(g, "correlation_id", "?")
    content_type = request.content_type or ""

    # Stark: content-type validation
    if "application/json" not in content_type:
        logger.warning("[%s] /mcp: invalid content-type '%s'", cid, content_type)
        return _jsonrpc_err(None, -32700, f"Expected application/json, got {content_type}")

    payload = request.get_json(silent=True) or {}

    # Stark: input validation
    validation_error = _validate_payload(payload, "mcp")
    if validation_error:
        logger.warning("[%s] /mcp: validation failed: %s", cid, validation_error)
        return _jsonrpc_err(None, -32602, validation_error)

    method = payload.get("method")
    request_id = payload.get("id")

    if method == "initialize":
        logger.info("[%s] /mcp initialize", cid)
        return _jsonrpc_ok(request_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {
                "name": "jeeves-mcp",
                "version": "2.0.0",
            },
        })

    if method == "tools/list":
        logger.info("[%s] /mcp tools/list", cid)
        return _jsonrpc_ok(request_id, {
            "tools": [
                {
                    "name": "jeeves_run",
                    "description": (
                        "Run Jeeves with a prompt. Jeeves can use configured local tools "
                        "and connected Composio app surface (GitHub, Gmail, Calendar)."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "prompt": {
                                "type": "string",
                                "description": "The user request to send to Jeeves.",
                            },
                            "system_prompt": {
                                "type": "string",
                                "description": "Optional system prompt override.",
                            },
                        },
                        "required": ["prompt"],
                    },
                }
            ]
        })

    if method == "tools/call":
        params = payload.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        prompt = (arguments.get("prompt") or "").strip()
        system_prompt = arguments.get("system_prompt")

        # Stark: prompt validation
        prompt_error = _validate_prompt(prompt)
        if prompt_error:
            logger.warning("[%s] /mcp tools/call: %s", cid, prompt_error)
            return _jsonrpc_err(request_id, -32602, prompt_error)

        if name != "jeeves_run":
            return _jsonrpc_err(request_id, -32601, f"Unknown tool: {name}")

        logger.info("[%s] /mcp tools/call jeeves_run (prompt=%s…)", cid, prompt[:80])

        try:
            result = _invoke_with_timeout(prompt, system_prompt=system_prompt)
            logger.info("[%s] /mcp tools/call succeeded", cid)
            return _jsonrpc_ok(request_id, {
                "content": [{"type": "text", "text": result}],
            })
        except Exception as exc:
            logger.exception("[%s] /mcp tools/call failed", cid)
            return _jsonrpc_err(request_id, -32603, str(exc))

    logger.warning("[%s] /mcp unsupported method: %s", cid, method)
    return _jsonrpc_err(request_id, -32601, f"Unsupported method: {method}")


if __name__ == "__main__":
    config = _get_cached_config()
    port = int(config.get("jeeves_public_port", 5051))
    logger.info("Starting jeeves-mcp on 0.0.0.0:%s", port)
    logger.info("Auth configured: %s", bool(config.get("jeeves_api_secret")))
    logger.info("CORS origins: %s", ",".join(_ALLOWED_ORIGINS))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
