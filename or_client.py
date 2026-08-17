from __future__ import annotations

import base64
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests

from core.utils import normalize_api_key, normalize_api_key_list

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("jeeves_brain")


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


BASE_DIR     = _get_base_dir()
API_KEY_PATH = BASE_DIR / "config" / "api_keys.json"


def _load_api_key() -> str:
    try:
        with open(API_KEY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        key = normalize_api_key(data.get("groq_api_key", "") or "")
        if not key:
            raise ValueError("groq_api_key is empty in api_keys.json")
        return key
    except FileNotFoundError:
        raise RuntimeError(f"api_keys.json not found at: {API_KEY_PATH}")
    except Exception as e:
        raise RuntimeError(f"Failed to load Groq API key: {e}")


def _load_config() -> dict:
    try:
        with open(API_KEY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("api_keys.json must contain a JSON object")
        return data
    except FileNotFoundError:
        raise RuntimeError(f"api_keys.json not found at: {API_KEY_PATH}")
    except Exception as e:
        raise RuntimeError(f"Failed to load brain config: {e}")


# ── Config cache (YinYang) ────────────────────────────────────────────────────
# The Groq key pool reads api_keys.json on every current()/advance()/size() —
# i.e. on every brain request AND every retry. The file is small, but re-
# opening + re-parsing it per request is pure waste. Cache it keyed on
# (mtime_ns, size): editing the file (adding/removing keys) bumps mtime, so
# the "keys picked up immediately, no restart" contract is preserved exactly
# while steady-state requests do zero disk I/O.
_config_cache: dict | None = None
_config_mtime_ns: int = -1
_config_size: int = -1


def _load_config_cached() -> dict:
    global _config_cache, _config_mtime_ns, _config_size
    try:
        st = os.stat(API_KEY_PATH)
        if (_config_cache is not None
                and st.st_mtime_ns == _config_mtime_ns
                and st.st_size == _config_size):
            return _config_cache
    except OSError:
        pass  # fall through: let _load_config raise the canonical error
    cfg = _load_config()
    try:
        st = os.stat(API_KEY_PATH)
        _config_mtime_ns, _config_size = st.st_mtime_ns, st.st_size
    except OSError:
        pass
    _config_cache = cfg
    return cfg


# Current Groq text models (verified against the API on 2026-08-17):
#   * the older llama-3.3-70b-versatile / llama-3.1-8b-instant ids now
#     return 404 "model_not_found" on free keys
#   * groq/compound 413s on Jeeves' full system prompt (~14k chars) — its
#     input cap is too small, so the flagship is openai/gpt-oss-120b, which
#     accepts the full prompt and answers correctly (verified live)
#   * openai/gpt-oss-20b is the lite model (accepts the full prompt, fast)
GROQ_DEFAULT_MODEL  = "openai/gpt-oss-120b"
GROQ_LITE_MODEL     = "openai/gpt-oss-20b"
GROQ_TEXT_MODELS    = (GROQ_DEFAULT_MODEL, GROQ_LITE_MODEL)
GITHUB_DEFAULT_MODEL = "gpt-4.1-mini"
GITHUB_TEXT_MODELS   = ("gpt-4.1-mini", "gpt-4.1", "gpt-4o-mini", "gpt-4o")
DEFAULT_MAX_TOKENS  = 4096
DEFAULT_TEMPERATURE = 0.7
DEFAULT_PROVIDER    = "groq"
GITHUB_MODELS_BASE_URL = "https://models.github.ai/inference"
DEFAULT_SYSTEM      = (
    "You are a component of MARK XXXIX, an AI assistant inspired by JARVIS. "
    "Be concise, helpful, and precise."
)


def _normalize_provider(value: str | None) -> str:
    provider = (value or DEFAULT_PROVIDER).strip().lower()
    if provider in {"github", "github_models", "github-models", "copilot"}:
        return "github_models"
    return "groq"


class _GroqKeyPool:
    """Round-robin pool of Groq API keys with rotation + two-tier quarantine.

    Reads the full key list from config/api_keys.json on every access, so
    keys added at any time (no restart needed, any number of keys) are
    picked up immediately. Behaviour:

      * After each successful call the pool advances, spreading usage
        evenly across all configured keys so no single free-tier key gets
        exhausted first.
      * On rate-limit/quota (429-style) errors the current key is rotated
        out and the SAME request is retried with the next key, so Jeeves
        keeps working as long as any key still has quota.
      * Keys that hit a per-minute limit are quarantined for a short
        cooldown (RATE_LIMIT_COOLDOWN_S) — those recover in ~1 minute.
      * Keys that hit their DAILY quota (detected from the error text) are
        parked until the next calendar day — retrying them in 60s just
        burns requests, so the pool skips them entirely (see current()).
    """

    # Per-minute rate-limit window — recovers in ~1 minute.
    RATE_LIMIT_COOLDOWN_S = 60.0
    # Floor for daily-capped keys (hours) in case next midnight is far away.
    DAILY_CAP_COOLDOWN_S  = 8 * 3600.0
    # Upper bound for a single recovery wait. The per-minute cooldown is
    # 60s, so one wait always clears it; 90s leaves margin for slow clocks.
    MAX_RECOVERY_WAIT_S   = 90.0
    # Wait-and-retry cycles after a full lap fails: every configured key is
    # re-tried after EACH recovery window, so a key that needs a longer
    # cooldown is not abandoned early. 2 waits -> 3 full laps per request.
    RECOVERY_RETRIES      = 2
    # Hard ceiling on total time spent waiting for recovery across all
    # cycles — guarantees a request can never be held hostage by a pool
    # that keeps re-quarantining itself (e.g. one key past its daily cap).
    TOTAL_EXHAUST_BUDGET_S = 210.0

    def __init__(self):
        self._lock = threading.Lock()
        self._idx = 0
        self._cooldown_until: dict[str, float] = {}

    def _keys(self) -> list[str]:
        try:
            return normalize_api_key_list(_load_config_cached().get("groq_api_key", ""))
        except Exception:
            return []

    def size(self) -> int:
        return len(self._keys())

    def current(self) -> str:
        """Next key in round-robin order, skipping any in cooldown.

        When every key is cooling down, prefer the one that recovers
        soonest — and never hand out a key parked long-term (daily cap),
        so we don't waste requests on keys that won't recover for hours.
        Falls back to the current slot when even the soonest recovery is
        hours away (keeps the old no-stall guarantee)."""
        keys = self._keys()
        if not keys:
            raise RuntimeError("No Groq API keys configured in config/api_keys.json.")
        now = time.time()
        with self._lock:
            for i in range(len(keys)):
                idx = (self._idx + i) % len(keys)
                if self._cooldown_until.get(keys[idx], 0.0) <= now:
                    self._idx = idx
                    return keys[idx]
            # every key is cooling down — pick the one recovering soonest,
            # unless all are parked long-term (daily caps)
            best_at = float("inf")
            best: str | None = None
            for k in keys:
                at = self._cooldown_until.get(k, 0.0)
                if at < best_at:
                    best_at, best = at, k
            if best is not None and best_at - now <= self.MAX_RECOVERY_WAIT_S:
                self._idx = keys.index(best)
                return best
            # all parked hours away — use the current slot anyway
            self._idx = self._idx % len(keys)
            return keys[self._idx]

    def advance(self) -> None:
        keys = self._keys()
        with self._lock:
            if keys:
                self._idx = (self._idx + 1) % len(keys)

    def mark_rate_limited(self, key: str) -> None:
        with self._lock:
            self._cooldown_until[key] = time.time() + self.RATE_LIMIT_COOLDOWN_S

    def mark_daily_capped(self, key: str) -> None:
        """Park a key until the next calendar day (daily-quota exhaustion).

        Daily limits do NOT recover in the 60s per-minute window, so the
        key is quarantined until next local midnight + a 5 min buffer,
        bounded to at most 24h and at least DAILY_CAP_COOLDOWN_S. Local
        midnight is the pragmatic reset assumption (Groq's exact daily
        window is account-dependent); the 8h floor guards clock skew.
        """
        now = datetime.now()
        next_midnight = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        until = next_midnight.timestamp() + 300
        with self._lock:
            self._cooldown_until[key] = min(
                until, time.time() + self.DAILY_CAP_COOLDOWN_S
            )

    def earliest_recovery(self) -> float | None:
        """Timestamp when the first quarantined key recovers; None if none cooling."""
        now = time.time()
        keys = self._keys()
        with self._lock:
            cooling = [
                self._cooldown_until[k] for k in keys
                if self._cooldown_until.get(k, 0.0) > now
            ]
        return min(cooling) if cooling else None


_groq_pool = _GroqKeyPool()


class ClaudeClient:
    """
    NOTE: class name kept as ClaudeClient (module still imported elsewhere
    as `from or_client import client`) so existing call sites across the
    migrated files do not need to change. Under the hood this now talks to
    Groq's free API (no cost, no credit card, no billing-gated preview
    access -- unlike Gemini's Live API).

    Public method signatures: chat, chat_json, vision, vision_from_file,
    multi_turn, available_models.
    """

    def __init__(self) -> None:
        # Lazy init: do NOT read config/api_keys.json or resolve any key
        # here. main.py imports this module before the setup screen has a
        # chance to create the config file — constructing eagerly would
        # crash the whole app on first launch. Everything below is resolved
        # on first use (see _ensure_loaded / _resolve_api_key).
        self._init_lock = threading.Lock()
        self._config = None
        self.provider = DEFAULT_PROVIDER
        self.api_key = None
        self._preferred_text_model = None
        self._provider_change_callbacks: list = []

    def _ensure_loaded(self) -> None:
        """Load config + resolve provider/key on first use (not at import).

        Double-checked locking: the cheap unlocked check keeps the hot path
        (every call after the first) lock-free; the lock only guards the
        one-time load so concurrent callers (background monitors + a live
        user turn can all hit this in the same second) don't redo the work
        or race each other.
        """
        if self._config is not None:
            return
        with self._init_lock:
            if self._config is None:
                self._config = _load_config()
                self.provider = _normalize_provider(self._config.get("brain_provider"))
                self.api_key = self._resolve_api_key(self.provider)
                if self._preferred_text_model is None:
                    self._preferred_text_model = self._default_model_for_provider(self.provider)

    def _default_model_for_provider(self, provider: str) -> str:
        return GITHUB_DEFAULT_MODEL if provider == "github_models" else GROQ_DEFAULT_MODEL

    def _resolve_api_key(self, provider: str) -> str:
        self._ensure_loaded()
        if provider == "github_models":
            key = str(self._config.get("github_models_api_key", "")).strip()
            if not key:
                key = str(self._config.get("github_token", "")).strip()
            if not key:
                key = os.environ.get("GITHUB_TOKEN", "").strip()
            if not key:
                raise RuntimeError(
                    "GitHub Models provider selected, but github_models_api_key/github_token/GITHUB_TOKEN is missing"
                )
            return key

        key = normalize_api_key(self._config.get("groq_api_key", "") or "")
        if not key:
            raise RuntimeError("groq_api_key is empty in api_keys.json")
        return key

    def _groq_client(self) -> "Groq":
        # Lazy import: the `groq` SDK costs ~2.5s to import on this machine.
        # Defer it to first use so `import or_client` stays fast (spawnable
        # one-shot invocations and the daemon don't pay for it up front).
        from groq import Groq

        # Explicit timeout: the SDK's default is 10 minutes — a hung
        # connection would block the CLI/daemon request thread for that long.
        # 60s is plenty for a completion and bounds every failure (daktari).
        return Groq(api_key=_groq_pool.current(), timeout=60.0)

    def _groq_create(self, model, max_tokens, temperature, messages, wait_for_recovery: bool = True) -> str:
        """Call Groq with automatic key rotation + wait-for-recovery retry.

        Lap 1: round-robin across all configured keys. On 429-style errors
        the key is quarantined (60s for per-minute limits, until tomorrow
        for daily-cap exhaustion) and the same request is retried with the
        next key. If the whole pool is exhausted, wait until the soonest
        key recovers and retry the same request — repeating through every
        recovery window (RECOVERY_RETRIES) until ALL keys are genuinely
        exhausted, then surface the error so the provider fallback
        (github_models) can take over. Bounded by MAX_RECOVERY_WAIT_S per
        wait and TOTAL_EXHAUST_BUDGET_S overall, so a request can never be
        held hostage by a pool that never recovers.

        wait_for_recovery=False runs a single lap with NO recovery sleeps —
        for callers (alerts, background tasks) that must fail fast and fall
        back to their own path instead of stalling behind a quarantined
        pool (which is what turned every alert into a 30s timeout when the
        free-tier quota was exhausted).
        """
        attempts = max(1, _groq_pool.size())
        if not wait_for_recovery:
            return self._groq_lap(model, max_tokens, temperature, messages, attempts)
        last_error: Exception | None = None
        started = time.monotonic()
        for cycle in range(1 + _groq_pool.RECOVERY_RETRIES):
            try:
                return self._groq_lap(model, max_tokens, temperature, messages, attempts)
            except RuntimeError as e:
                last_error = e
                if cycle >= _groq_pool.RECOVERY_RETRIES:
                    break
                recovery_at = _groq_pool.earliest_recovery()
                if recovery_at is None:
                    break
                wait_s = recovery_at - time.time()
                if wait_s <= 0:
                    wait_s = 1.0
                if wait_s > _groq_pool.MAX_RECOVERY_WAIT_S:
                    # keys parked long-term (daily caps) — don't hang the
                    # request; surface the error so the provider fallback
                    # (github_models) can take over instead.
                    break
                if time.monotonic() - started + wait_s > _groq_pool.TOTAL_EXHAUST_BUDGET_S:
                    # every key has been re-tried through every recoverable
                    # window and the pool is still hot — genuinely exhausted.
                    break
                logger.warning(
                    f"[groq] all {attempts} key(s) rate-limited; waiting "
                    f"{wait_s:.0f}s for recovery before retrying "
                    f"(cycle {cycle + 1}/{_groq_pool.RECOVERY_RETRIES}, "
                    f"budget {_groq_pool.TOTAL_EXHAUST_BUDGET_S:.0f}s)"
                )
                time.sleep(wait_s + 0.5)
        raise RuntimeError(
            f"Groq API call failed after {attempts} key(s): {last_error}"
        ) from last_error

    def _groq_lap(self, model, max_tokens, temperature, messages, attempts: int) -> str:
        """One round-robin pass over all keys. Returns text or raises.

        Capacity errors quarantine the key (60s or daily-park depending on
        the error text) and move to the next key. Non-capacity errors
        (auth, 5xx, network) raise immediately — never retried, never
        waited on.

        Payload errors (413 / "request too large") get the payload trimmed
        and retried ONCE on the same key; if still too large they raise
        immediately. Waiting out a 60s cooldown or rotating keys can never
        shrink a request — before this fix, every oversized request burned
        the full recovery budget (2 waits x 3 keys) on a failure that could
        never succeed.
        """
        last_error: Exception | None = None
        shrunk_once = False
        for _ in range(attempts):
            key = _groq_pool.current()
            try:
                response = self._groq_client().chat.completions.create(
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    messages=messages,
                )
                _groq_pool.advance()
                return (response.choices[0].message.content or "").strip()
            except Exception as e:
                last_error = e
                if self._is_payload_error(e):
                    # The request itself is too big — quarantining keys or
                    # waiting can't fix that. Trim once and retry; if it's
                    # still too large, surface immediately so the caller
                    # fails over fast instead of burning the 60s recovery
                    # dance on a request that can never succeed.
                    if not shrunk_once:
                        shrunk_once = True
                        messages = self._shrink_messages(messages)
                        logger.warning(
                            f"[groq] {model} request too large — trimming "
                            "messages and retrying once"
                        )
                        continue
                    raise
                if not self._is_capacity_error(e):
                    raise
                if self._is_daily_cap_error(e):
                    _groq_pool.mark_daily_capped(key)
                    logger.warning(
                        f"[groq] {model} hit its DAILY quota on a key; "
                        f"parked until tomorrow (pool of {attempts})"
                    )
                else:
                    _groq_pool.mark_rate_limited(key)
                    logger.warning(
                        f"[groq] {model} hit a rate limit on one key; "
                        f"rotating to the next Groq key (pool of {attempts})"
                    )
                _groq_pool.advance()
                continue
        raise RuntimeError(
            f"Groq API call failed after {attempts} key(s): {last_error}"
        ) from last_error

    @staticmethod
    def _is_daily_cap_error(error: Exception) -> bool:
        """Detect daily-quota exhaustion (won't recover in ~60s) from error text.

        Groq embeds the limit window in its 429 message. Daily-window
        markers get a long quarantine; everything else gets the 60s
        per-minute cooldown. Heuristic by design — a false negative just
        means one extra 60s retry, never a hang.
        """
        message = str(error).lower()
        return any(
            token in message
            for token in (
                "per day",
                "requests per day",
                "rpd",
                "daily limit",
                "daily quota",
                "daily rate limit",
                "per 24",
                "24 hour",
                "24-hour",
            )
        )

    def _github_models_request(self, payload: dict) -> dict:
        response = requests.post(
            f"{GITHUB_MODELS_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json=payload,
            timeout=60,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"GitHub Models API call failed: {response.status_code} {response.text[:400]}")
        return response.json()

    @staticmethod
    def _is_payload_error(error: Exception) -> bool:
        """Detect 'request too large' rejections (413 / context overflow).

        These never recover by waiting or rotating keys — the request
        itself is too big — so they must NOT go through the quarantine /
        recovery cycle (that was the endless 60s-wait 413 loop in the
        logs). Checked BEFORE _is_capacity_error because Groq's 413 body
        also contains the word 'tokens'.
        """
        message = str(error).lower()
        return any(
            token in message
            for token in (
                "413",
                "request too large",
                "payload too large",
                "reduce your message size",
                "too large for model",
                "maximum context",
                "context length",
                "context window",
                "context_length_exceeded",
                "token limit exceeded",
            )
        )

    @staticmethod
    def _shrink_messages(messages: list, limit: int = 3000) -> list:
        """Truncate oversized message content in place of a retry.

        The system message is preserved verbatim: it carries the tool
        declarations, and chopping its tail after a 413 retry is exactly
        what made Jeeves reply "I don't have any tools available" (the tools
        live at the end of the prompt). Conversation messages are trimmed;
        the cap includes the trim marker, so each is at most `limit` chars."""
        suffix = "\n…[trimmed]"
        out = []
        for m in messages:
            if m.get("role") == "system":
                out.append(m)
                continue
            content = m.get("content")
            if isinstance(content, str) and len(content) > limit:
                content = content[:limit - len(suffix)].rstrip() + suffix
            out.append({**m, "content": content})
        return out

    @staticmethod
    def _is_capacity_error(error: Exception) -> bool:
        message = str(error).lower()
        return any(
            token in message
            for token in (
                "429",
                "quota",
                "rate limit",
                "resource_exhausted",
                "insufficient_quota",
                "token limit",
                "tokens",
                "too many requests",
            )
        )

    @staticmethod
    def _is_auth_error(error: Exception) -> bool:
        """Detect auth / token-expiry style errors from provider responses."""
        message = str(error).lower()
        return any(
            token in message
            for token in (
                "401",
                "403",
                "unauthorized",
                "invalid token",
                "invalid_api_key",
                "invalid api key",
                "authentication",
                "permission denied",
                "forbidden",
            )
        )

    @staticmethod
    def _is_model_error(error: Exception) -> bool:
        """Detect "model does not exist / no access" errors.

        Groq renames/retires model ids over time (llama-3.3-70b-versatile now
        returns 404 model_not_found), so a stale or renamed model id must be
        treated as retryable-across-candidates: try the next model in the
        pool (e.g. the lite model) instead of hard-failing every request.
        """
        message = str(error).lower()
        return any(
            token in message
            for token in (
                "model_not_found",
                "model does not exist",
                "not found",
                "no access",
                "model_not_accessible",
            )
        )

    def _is_transient_error(self, error: Exception) -> bool:
        """Return True for errors that should allow trying a fallback provider.

        This includes capacity/rate-limit errors and auth/token-expiry errors
        that we want to recover from by switching providers.
        """
        return (self._is_capacity_error(error) or self._is_auth_error(error)
                or self._is_model_error(error))

    def _candidate_models(self, model: str | None) -> list[str]:
        candidates = []
        requested = model or self._preferred_text_model or self._default_model_for_provider(self.provider)
        provider_defaults = GITHUB_TEXT_MODELS if self.provider == "github_models" else GROQ_TEXT_MODELS

        for candidate in (requested, *provider_defaults):
            if candidate and candidate not in candidates:
                candidates.append(candidate)

        return candidates

    def _call(self, model, system, messages, max_tokens=DEFAULT_MAX_TOKENS, temperature=DEFAULT_TEMPERATURE, wait_for_recovery: bool = True) -> str:
        self._ensure_loaded()
        full_messages = [{"role": "system", "content": system}] + messages
        last_error: Exception | None = None
        # First try the configured provider. If it fails due to capacity or
        # transient provider errors, automatically try the other provider as a
        # fallback.
        tried_providers = []

        def _attempt_with_provider(provider_name: str) -> tuple[bool, Exception | None, str | None]:
            """Attempt the request using the named provider. Returns (ok, err, text)."""
            nonlocal full_messages
            err = None
            text = None
            for candidate_model in self._candidate_models(model):
                try:
                    if provider_name == "github_models":
                        payload = {
                            "model": candidate_model,
                            "messages": full_messages,
                            "max_tokens": max_tokens,
                            "temperature": temperature,
                        }
                        response = self._github_models_request(payload)
                        self._preferred_text_model = candidate_model
                        text = (response["choices"][0]["message"]["content"] or "").strip()
                        return True, None, text

                    # groq path — auto-rotates across all configured keys
                    # on rate limits, so one exhausted free key never stalls
                    text = self._groq_create(candidate_model, max_tokens, temperature, full_messages, wait_for_recovery)
                    self._preferred_text_model = candidate_model
                    return True, None, text

                except Exception as e:
                    err = e
                    logger.error(f"[{provider_name}] {candidate_model} -> Error: {e}")
                    # If the error is not transient (capacity/auth), bubble up immediately
                    if not self._is_transient_error(e):
                        return False, e, None
                    # otherwise try the next candidate model
                    continue

            return False, err, None

        primary = self.provider
        fallback = "github_models" if primary == "groq" else "groq"

        ok, err, text = _attempt_with_provider(primary)
        if ok:
            return text

        # If failure was not transient, raise immediately
        if err and not self._is_transient_error(err):
            raise RuntimeError(f"{primary} API call failed: {err}") from err

        logger.info(f"Primary provider '{primary}' failed or rate-limited; trying fallback '{fallback}'")

        # Try fallback provider
        try:
            # ensure API key for fallback is resolved (may raise)
            self.api_key = self._resolve_api_key(fallback)
        except Exception as e:
            logger.error(f"Failed to resolve API key for fallback provider '{fallback}': {e}")
            raise RuntimeError(f"Both primary provider '{primary}' failed and fallback provider key missing: {e}") from e

        ok2, err2, text2 = _attempt_with_provider(fallback)
        if ok2:
            # adopt fallback as new active provider
            logger.info(f"Switching provider from '{primary}' to '{fallback}' due to previous errors.")
            old = self.provider
            self.provider = fallback
            # notify callbacks
            try:
                for cb in self._provider_change_callbacks:
                    try:
                        cb(old, self.provider)
                    except Exception:
                        logger.exception("Provider change callback raised an exception")
            except Exception:
                logger.exception("Error while running provider change callbacks")
            return text2

        # Neither provider succeeded
        final_err = err2 or err
        raise RuntimeError(f"Both providers failed: {final_err}") from final_err

    def chat(self, prompt, system=DEFAULT_SYSTEM, model=None, max_tokens=DEFAULT_MAX_TOKENS, temperature=DEFAULT_TEMPERATURE, wait_for_recovery: bool = True) -> str:
        self._ensure_loaded()
        messages = [{"role": "user", "content": prompt}]
        return self._call(model or self._default_model_for_provider(self.provider), system, messages, max_tokens, temperature, wait_for_recovery)

    def chat_json(self, prompt, system="Return ONLY valid JSON. No markdown fences, no extra text, no explanation.", model=None, max_tokens=DEFAULT_MAX_TOKENS, wait_for_recovery: bool = True) -> dict:
        self._ensure_loaded()
        messages = [{"role": "user", "content": prompt}]
        raw = self._call(model or self._default_model_for_provider(self.provider), system, messages, max_tokens, temperature=0.2, wait_for_recovery=wait_for_recovery)
        clean = raw.strip()
        if clean.startswith("```"):
            parts = clean.split("```")
            clean = parts[1] if len(parts) > 1 else clean
            if clean.startswith("json"):
                clean = clean[4:]
        clean = clean.strip().rstrip("`").strip()
        try:
            return json.loads(clean)
        except json.JSONDecodeError as e:
            logger.error(f"[{self.provider}] JSON parse failed: {e}\nRaw response (first 300 chars): {raw[:300]}")
            raise ValueError(f"Model returned unparseable JSON: {e}\nRaw output: {raw[:200]}")

    def vision(self, prompt, image_b64, mime="image/png", system="Analyze the image and describe what you see clearly and concisely.", model=None, max_tokens=1024) -> str:
        self._ensure_loaded()
        # An explicit `model` wins; otherwise use the current recommended
        # vision model per provider. The previous Groq default
        # (llama-3.2-90b-vision-preview) was decommissioned and now returns
        # 400 model_decommissioned, which broke the still-image fallback.
        vision_model = model or (
            "gpt-4.1" if self.provider == "github_models" else "qwen/qwen3.6-27b"
        )
        messages = [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
        ]}]

        full_messages = [{"role": "system", "content": system}] + messages

        primary = self.provider
        fallback = "github_models" if primary == "groq" else "groq"

        # try primary
        try:
            if primary == "github_models":
                response = self._github_models_request({
                    "model": vision_model,
                    "messages": full_messages,
                    "max_tokens": max_tokens,
                    "temperature": 0.2,
                })
                return (response["choices"][0]["message"]["content"] or "").strip()

            return self._groq_create(vision_model, max_tokens, 0.2, full_messages)

        except Exception as e:
            logger.error(f"[{primary} Vision] {vision_model} -> Error: {e}")
            if not self._is_transient_error(e):
                raise RuntimeError(f"{primary} vision call failed: {e}") from e

        # try fallback
        logger.info(f"Vision: primary provider '{primary}' failed; trying fallback '{fallback}'")
        try:
            self.api_key = self._resolve_api_key(fallback)
            if fallback == "github_models":
                response = self._github_models_request({
                    "model": vision_model,
                    "messages": full_messages,
                    "max_tokens": max_tokens,
                    "temperature": 0.2,
                })
                logger.info(f"Switching provider from '{primary}' to '{fallback}' due to previous errors.")
                old = self.provider
                self.provider = fallback
                try:
                    for cb in self._provider_change_callbacks:
                        try:
                            cb(old, self.provider)
                        except Exception:
                            logger.exception("Provider change callback raised an exception")
                except Exception:
                    logger.exception("Error while running provider change callbacks")
                return (response["choices"][0]["message"]["content"] or "").strip()

            text = self._groq_create(vision_model, max_tokens, 0.2, full_messages)
            logger.info(f"Switching provider from '{primary}' to '{fallback}' due to previous errors.")
            old = self.provider
            self.provider = fallback
            try:
                for cb in self._provider_change_callbacks:
                    try:
                        cb(old, self.provider)
                    except Exception:
                        logger.exception("Provider change callback raised an exception")
            except Exception:
                logger.exception("Error while running provider change callbacks")
            return text

        except Exception as e2:
            logger.error(f"Fallback vision provider '{fallback}' failed: {e2}")
            raise RuntimeError(f"Both providers failed for vision: {e2}") from e2

    def vision_from_file(self, prompt, image_path, system="Analyze the image and describe what you see clearly and concisely.", model=None, max_tokens=1024) -> str:
        self._ensure_loaded()
        path = Path(image_path)
        mime_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}
        mime = mime_map.get(path.suffix.lower(), "image/png")
        with open(path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode("utf-8")
        return self.vision(prompt, image_b64, mime, system, model, max_tokens)

    def register_provider_change_callback(self, callback):
        """Register a callback `callback(old_provider, new_provider)` to be
        notified when the active provider changes due to fallback.
        """
        if not callable(callback):
            raise TypeError("callback must be callable")
        self._provider_change_callbacks.append(callback)

    def multi_turn(self, messages, model=None, max_tokens=DEFAULT_MAX_TOKENS, temperature=DEFAULT_TEMPERATURE, wait_for_recovery: bool = True) -> str:
        self._ensure_loaded()
        system = DEFAULT_SYSTEM
        chat_messages = []
        for m in messages:
            if m.get("role") == "system":
                system = m.get("content", system)
            else:
                chat_messages.append(m)
        return self._call(model or self._default_model_for_provider(self.provider), system, chat_messages, max_tokens, temperature, wait_for_recovery)

    def available_models(self) -> dict:
        self._ensure_loaded()
        if self.provider == "github_models":
            return {
                "provider": "github_models",
                "text_models": ["gpt-4.1", "gpt-4.1-mini", "gpt-4o-mini", "claude-3.5-sonnet"],
                "active_text_model": self._preferred_text_model,
            }
        return {
            "provider": "groq",
            "text_models": list(GROQ_TEXT_MODELS),
            "active_text_model": self._preferred_text_model,
        }


client = ClaudeClient()


class _GenerateContentResponse:
    def __init__(self, text: str):
        self.text = text


class ClaudeModelShim:
    """Drop-in replacement for google.generativeai.GenerativeModel, backed by Groq."""

    def __init__(self, model_name=None, system_instruction=None, **kwargs):
        self.model_name = model_name
        self.system_instruction = system_instruction

    def _resolve_model(self) -> str:
        client._ensure_loaded()
        if self.model_name:
            lower = self.model_name.lower()
            if "mini" in lower or "lite" in lower:
                return GROQ_LITE_MODEL if client.provider == "groq" else self.model_name
            return self.model_name
        if client.provider == "github_models":
            return GITHUB_DEFAULT_MODEL
        return GROQ_DEFAULT_MODEL

    def generate_content(self, contents, **kwargs):
        if isinstance(contents, str):
            prompt = contents
        elif isinstance(contents, (list, tuple)):
            parts = [item if isinstance(item, str) else str(item) for item in contents]
            prompt = "\n\n".join(parts)
        else:
            prompt = str(contents)

        if self.system_instruction:
            text = client.chat(prompt, system=self.system_instruction, model=self._resolve_model())
        else:
            text = client.chat(prompt, model=self._resolve_model())
        return _GenerateContentResponse(text)


if __name__ == "__main__":
    print("MARK XXXIX-OR -- Groq Client Self-Test")
    try:
        print(client.chat("Introduce yourself in one sentence."))
    except Exception as e:
        print("FAIL:", e)
