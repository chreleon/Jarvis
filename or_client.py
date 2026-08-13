from __future__ import annotations

import base64
import json
import logging
import os
import sys
import threading
import time
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


GROQ_DEFAULT_MODEL  = "llama-3.3-70b-versatile"
GROQ_LITE_MODEL     = "llama-3.1-8b-instant"
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
    """Round-robin pool of Groq API keys with automatic rotation + quarantine.

    Reads the full key list from config/api_keys.json on every access, so
    keys added at any time (no restart needed, any number of keys) are
    picked up immediately. Behaviour:

      * After each successful call the pool advances, spreading usage
        evenly across all configured keys so no single free-tier key gets
        exhausted first.
      * On rate-limit/quota (429-style) errors the current key is rotated
        out and the SAME request is retried with the next key, so Jeeves
        keeps working as long as any key still has quota.
      * Keys that hit a rate limit are quarantined for a short cooldown
        (RATE_LIMIT_COOLDOWN_S) so subsequent requests start from a healthy
        key instead of re-hammering the one that just got exhausted.
    """

    RATE_LIMIT_COOLDOWN_S = 60.0

    def __init__(self):
        self._lock = threading.Lock()
        self._idx = 0
        self._cooldown_until: dict[str, float] = {}

    def _keys(self) -> list[str]:
        try:
            return normalize_api_key_list(_load_config().get("groq_api_key", ""))
        except Exception:
            return []

    def size(self) -> int:
        return len(self._keys())

    def current(self) -> str:
        """Next key in round-robin order, skipping any in cooldown.

        Falls back to the current slot when every key is cooling down so
        requests never stall waiting for one."""
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
            # every key is cooling down — use the current slot anyway
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
        self._config = _load_config()
        self.provider = _normalize_provider(self._config.get("brain_provider"))
        self.api_key = self._resolve_api_key(self.provider)
        self._preferred_text_model = self._default_model_for_provider(self.provider)
        self._provider_change_callbacks: list = []

    def _default_model_for_provider(self, provider: str) -> str:
        return GITHUB_DEFAULT_MODEL if provider == "github_models" else GROQ_DEFAULT_MODEL

    def _resolve_api_key(self, provider: str) -> str:
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

        return Groq(api_key=_groq_pool.current())

    def _groq_create(self, model, max_tokens, temperature, messages) -> str:
        """Call Groq with automatic key rotation.

        Uses the pool's current round-robin key; on rate-limit/quota errors
        it quarantines that key, rotates to the next configured key and
        retries the same request, so one exhausted free-tier key never
        stops the assistant while others still have quota. Advances the
        pool after success to spread usage across all keys.
        """
        attempts = max(1, _groq_pool.size())
        last_error: Exception | None = None
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
                if self._is_capacity_error(e) and attempts > 1:
                    _groq_pool.mark_rate_limited(key)
                    _groq_pool.advance()
                    logger.warning(
                        f"[groq] {model} hit a rate limit on one key; "
                        f"rotating to the next Groq key (pool of {attempts})"
                    )
                    continue
                raise
        raise RuntimeError(
            f"Groq API call failed after {attempts} key(s): {last_error}"
        ) from last_error

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

    def _is_transient_error(self, error: Exception) -> bool:
        """Return True for errors that should allow trying a fallback provider.

        This includes capacity/rate-limit errors and auth/token-expiry errors
        that we want to recover from by switching providers.
        """
        return self._is_capacity_error(error) or self._is_auth_error(error)

    def _candidate_models(self, model: str | None) -> list[str]:
        candidates = []
        requested = model or self._preferred_text_model or self._default_model_for_provider(self.provider)
        provider_defaults = GITHUB_TEXT_MODELS if self.provider == "github_models" else GROQ_TEXT_MODELS

        for candidate in (requested, *provider_defaults):
            if candidate and candidate not in candidates:
                candidates.append(candidate)

        return candidates

    def _call(self, model, system, messages, max_tokens=DEFAULT_MAX_TOKENS, temperature=DEFAULT_TEMPERATURE) -> str:
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
                    text = self._groq_create(candidate_model, max_tokens, temperature, full_messages)
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

    def chat(self, prompt, system=DEFAULT_SYSTEM, model=None, max_tokens=DEFAULT_MAX_TOKENS, temperature=DEFAULT_TEMPERATURE) -> str:
        messages = [{"role": "user", "content": prompt}]
        return self._call(model or self._default_model_for_provider(self.provider), system, messages, max_tokens, temperature)

    def chat_json(self, prompt, system="Return ONLY valid JSON. No markdown fences, no extra text, no explanation.", model=None, max_tokens=DEFAULT_MAX_TOKENS) -> dict:
        messages = [{"role": "user", "content": prompt}]
        raw = self._call(model or self._default_model_for_provider(self.provider), system, messages, max_tokens, temperature=0.2)
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
        vision_model = "gpt-4.1" if self.provider == "github_models" else "llama-3.2-90b-vision-preview"
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

    def multi_turn(self, messages, model=None, max_tokens=DEFAULT_MAX_TOKENS, temperature=DEFAULT_TEMPERATURE) -> str:
        system = DEFAULT_SYSTEM
        chat_messages = []
        for m in messages:
            if m.get("role") == "system":
                system = m.get("content", system)
            else:
                chat_messages.append(m)
        return self._call(model or self._default_model_for_provider(self.provider), system, chat_messages, max_tokens, temperature)

    def available_models(self) -> dict:
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
