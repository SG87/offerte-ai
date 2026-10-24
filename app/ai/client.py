"""Thin wrapper around the Anthropic SDK: JSON-in / JSON-out with robust parsing."""

from __future__ import annotations

import json
import re
from typing import Any

from app.config import get_settings

try:  # the SDK is a hard dependency, but keep the import local so tests never need a key
    import anthropic
except ImportError:  # pragma: no cover
    anthropic = None  # type: ignore[assignment]


class AIUnavailable(RuntimeError):
    """Raised when no API key is configured. Callers fall back to manual/heuristic paths."""


def get_client():
    settings = get_settings()
    if not settings.ai_enabled or anthropic is None:
        raise AIUnavailable("ANTHROPIC_API_KEY ontbreekt; AI-functies zijn uitgeschakeld.")
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


def _extract_text(message: Any) -> str:
    """Join the text blocks of a response (skips thinking/tool blocks if present)."""
    parts = []
    for block in getattr(message, "content", []) or []:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts).strip()


def parse_json_block(text: str) -> Any:
    """Parse JSON from model output, tolerating ```json fences and leading prose."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # fall back to the first {...} or [...] block
        for opener, closer in (("{", "}"), ("[", "]")):
            start, end = text.find(opener), text.rfind(closer)
            if start != -1 and end > start:
                try:
                    return json.loads(text[start : end + 1])
                except json.JSONDecodeError:
                    continue
        raise


def ask_json(system: str, content: list[dict[str, Any]] | str, max_tokens: int = 4000) -> Any:
    """One-shot call: system prompt + user content blocks -> parsed JSON."""
    client = get_client()
    settings = get_settings()
    message = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": content}],
    )
    return parse_json_block(_extract_text(message))


def image_block(data_b64: str, media_type: str) -> dict[str, Any]:
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data_b64}}


def text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}
