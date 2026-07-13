import json
import sys
import base64
import logging
from pathlib import Path
from typing import Optional

from groq import Groq

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("groq_client")


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
        key = data.get("groq_api_key", "").strip()
        if not key:
            raise ValueError("groq_api_key is empty in api_keys.json")
        return key
    except FileNotFoundError:
        raise RuntimeError(f"api_keys.json not found at: {API_KEY_PATH}")
    except Exception as e:
        raise RuntimeError(f"Failed to load Groq API key: {e}")


DEFAULT_MODEL       = "llama-3.3-70b-versatile"
LITE_MODEL          = "llama-3.1-8b-instant"
DEFAULT_MAX_TOKENS  = 4096
DEFAULT_TEMPERATURE = 0.7
DEFAULT_SYSTEM      = (
    "You are a component of MARK XXXIX, an AI assistant inspired by JARVIS. "
    "Be concise, helpful, and precise."
)


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
        self.api_key = _load_api_key()
        self._client = Groq(api_key=self.api_key)

    def _call(self, model, system, messages, max_tokens=DEFAULT_MAX_TOKENS, temperature=DEFAULT_TEMPERATURE) -> str:
        try:
            full_messages = [{"role": "system", "content": system}] + messages
            response = self._client.chat.completions.create(
                model=model, max_tokens=max_tokens, temperature=temperature, messages=full_messages,
            )
            return (response.choices[0].message.content or "").strip()
        except Exception as e:
            logger.error(f"[Groq] {model} -> Error: {e}")
            raise RuntimeError(f"Groq API call failed: {e}")

    def chat(self, prompt, system=DEFAULT_SYSTEM, model=None, max_tokens=DEFAULT_MAX_TOKENS, temperature=DEFAULT_TEMPERATURE) -> str:
        messages = [{"role": "user", "content": prompt}]
        return self._call(model or DEFAULT_MODEL, system, messages, max_tokens, temperature)

    def chat_json(self, prompt, system="Return ONLY valid JSON. No markdown fences, no extra text, no explanation.", model=None, max_tokens=DEFAULT_MAX_TOKENS) -> dict:
        messages = [{"role": "user", "content": prompt}]
        raw = self._call(model or DEFAULT_MODEL, system, messages, max_tokens, temperature=0.2)
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
            logger.error(f"[Groq] JSON parse failed: {e}\nRaw response (first 300 chars): {raw[:300]}")
            raise ValueError(f"Model returned unparseable JSON: {e}\nRaw output: {raw[:200]}")

    def vision(self, prompt, image_b64, mime="image/png", system="Analyze the image and describe what you see clearly and concisely.", model=None, max_tokens=1024) -> str:
        vision_model = "llama-3.2-90b-vision-preview"
        messages = [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
        ]}]
        try:
            full_messages = [{"role": "system", "content": system}] + messages
            response = self._client.chat.completions.create(
                model=vision_model, max_tokens=max_tokens, temperature=0.2, messages=full_messages,
            )
            return (response.choices[0].message.content or "").strip()
        except Exception as e:
            logger.error(f"[Groq Vision] {vision_model} -> Error: {e}")
            raise RuntimeError(f"Groq vision call failed: {e}")

    def vision_from_file(self, prompt, image_path, system="Analyze the image and describe what you see clearly and concisely.", model=None, max_tokens=1024) -> str:
        path = Path(image_path)
        mime_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}
        mime = mime_map.get(path.suffix.lower(), "image/png")
        with open(path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode("utf-8")
        return self.vision(prompt, image_b64, mime, system, model, max_tokens)

    def multi_turn(self, messages, model=None, max_tokens=DEFAULT_MAX_TOKENS, temperature=DEFAULT_TEMPERATURE) -> str:
        system = DEFAULT_SYSTEM
        chat_messages = []
        for m in messages:
            if m.get("role") == "system":
                system = m.get("content", system)
            else:
                chat_messages.append(m)
        return self._call(model or DEFAULT_MODEL, system, chat_messages, max_tokens, temperature)

    def available_models(self) -> dict:
        return {"provider": "groq", "text_model": DEFAULT_MODEL, "lite_model": LITE_MODEL}


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
        if self.model_name and "lite" in self.model_name.lower():
            return LITE_MODEL
        return DEFAULT_MODEL

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
