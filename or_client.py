import json
import sys
import base64
import logging
from pathlib import Path
from typing import Optional

import anthropic

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("claude_client")


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
        key = data.get("anthropic_api_key", "").strip()
        if not key:
            raise ValueError("anthropic_api_key is empty in api_keys.json")
        return key
    except FileNotFoundError:
        raise RuntimeError(f"api_keys.json not found at: {API_KEY_PATH}")
    except Exception as e:
        raise RuntimeError(f"Failed to load Anthropic API key: {e}")


DEFAULT_MODEL       = "claude-sonnet-5"
LITE_MODEL          = "claude-haiku-4-5-20251001"
DEFAULT_MAX_TOKENS  = 4096
DEFAULT_TEMPERATURE = 0.7
DEFAULT_SYSTEM      = (
    "You are a component of MARK XXXIX, an AI assistant inspired by JARVIS. "
    "Be concise, helpful, and precise."
)


class ClaudeClient:
    """
    Talks directly to Anthropic's Claude API (previously routed through
    OpenRouter to Gemini/other free models). Public method signatures are
    kept identical to the old OpenRouterClient (chat, chat_json, vision,
    vision_from_file, multi_turn, available_models) so existing call sites
    elsewhere in the codebase do not need to change.
    """

    def __init__(self) -> None:
        self.api_key = _load_api_key()
        self._client = anthropic.Anthropic(api_key=self.api_key)

    def _call(
        self,
        model: str,
        system: str,
        messages: list,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> str:
        try:
            response = self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system,
                messages=messages,
            )
            text = "".join(
                block.text for block in response.content
                if getattr(block, "type", None) == "text"
            )
            return text.strip()
        except Exception as e:
            logger.error(f"[Claude] {model} -> Error: {e}")
            raise RuntimeError(f"Claude API call failed: {e}")

    def chat(
        self,
        prompt: str,
        system: str = DEFAULT_SYSTEM,
        model: Optional[str] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> str:
        messages = [{"role": "user", "content": prompt}]
        return self._call(model or DEFAULT_MODEL, system, messages, max_tokens, temperature)

    def chat_json(
        self,
        prompt: str,
        system: str = (
            "Return ONLY valid JSON. No markdown fences, no extra text, no explanation."
        ),
        model: Optional[str] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> dict:
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
            logger.error(f"[Claude] JSON parse failed: {e}\nRaw response (first 300 chars): {raw[:300]}")
            raise ValueError(f"Model returned unparseable JSON: {e}\nRaw output: {raw[:200]}")

    def vision(
        self,
        prompt: str,
        image_b64: str,
        mime: str = "image/png",
        system: str = "Analyze the image and describe what you see clearly and concisely.",
        model: Optional[str] = None,
        max_tokens: int = 1024,
    ) -> str:
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": mime, "data": image_b64},
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ]
        return self._call(model or DEFAULT_MODEL, system, messages, max_tokens, temperature=0.2)

    def vision_from_file(
        self,
        prompt: str,
        image_path: str,
        system: str = "Analyze the image and describe what you see clearly and concisely.",
        model: Optional[str] = None,
        max_tokens: int = 1024,
    ) -> str:
        path = Path(image_path)
        mime_map = {
            ".png":  "image/png",
            ".jpg":  "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif":  "image/gif",
        }
        mime = mime_map.get(path.suffix.lower(), "image/png")

        with open(path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode("utf-8")

        return self.vision(prompt, image_b64, mime, system, model, max_tokens)

    def multi_turn(
        self,
        messages: list,
        model: Optional[str] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> str:
        system = DEFAULT_SYSTEM
        chat_messages = []
        for m in messages:
            if m.get("role") == "system":
                system = m.get("content", system)
            else:
                chat_messages.append(m)
        return self._call(model or DEFAULT_MODEL, system, chat_messages, max_tokens, temperature)

    def available_models(self) -> dict:
        return {
            "provider":    "anthropic",
            "text_model":  DEFAULT_MODEL,
            "lite_model":  LITE_MODEL,
        }


client = ClaudeClient()


class _GenerateContentResponse:
    """Mimics google.generativeai's response object (`.text` attribute)."""
    def __init__(self, text: str):
        self.text = text


class ClaudeModelShim:
    """
    Drop-in replacement for google.generativeai.GenerativeModel.
    Lets old call sites of the form:

        model = genai.GenerativeModel(model_name="...", system_instruction="...")
        response = model.generate_content(prompt)
        text = response.text

    keep working almost unchanged, just backed by Claude instead of Gemini.
    """

    def __init__(self, model_name: Optional[str] = None, system_instruction: Optional[str] = None, **kwargs):
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
            parts = []
            for item in contents:
                if isinstance(item, str):
                    parts.append(item)
                else:
                    parts.append(str(item))
            prompt = "\n\n".join(parts)
        else:
            prompt = str(contents)

        if self.system_instruction:
            text = client.chat(prompt, system=self.system_instruction, model=self._resolve_model())
        else:
            text = client.chat(prompt, model=self._resolve_model())

        return _GenerateContentResponse(text)


if __name__ == "__main__":
    print("=" * 55)
    print("  MARK XXXIX-OR -- Claude Client Self-Test")
    print("=" * 55)

    print("\n[TEST 1] Basic chat...")
    try:
        reply = client.chat("Introduce yourself in one sentence.")
        print(f"  Response : {reply}")
        print(f"  Status   : PASS")
    except Exception as e:
        print(f"  Status   : FAIL -- {e}")

    print("\n[TEST 2] JSON mode...")
    try:
        data = client.chat_json(
            'List 3 programming languages. Format: {"languages": ["a", "b", "c"]}',
            system="Return only valid JSON. No extra text.",
        )
        print(f"  Response : {data}")
        print(f"  Status   : PASS")
    except Exception as e:
        print(f"  Status   : FAIL -- {e}")

    print("\n[TEST 3] Multi-turn conversation...")
    try:
        history = [
            {"role": "system",    "content": "You are a helpful assistant. Be brief."},
            {"role": "user",      "content": "My name is Tony."},
            {"role": "assistant", "content": "Hello Tony, how can I help you?"},
            {"role": "user",      "content": "What is my name?"},
        ]
        reply = client.multi_turn(history)
        print(f"  Response : {reply}")
        print(f"  Status   : PASS")
    except Exception as e:
        print(f"  Status   : FAIL -- {e}")

    print("\n[TEST 4] GenerativeModel-shim compatibility...")
    try:
        model = ClaudeModelShim(model_name="claude-sonnet-5", system_instruction="Be very brief.")
        response = model.generate_content("Say hello.")
        print(f"  Response : {response.text}")
        print(f"  Status   : PASS")
    except Exception as e:
        print(f"  Status   : FAIL -- {e}")

    print("\n" + "=" * 55)
    print("  All tests complete.")
    print("=" * 55)
