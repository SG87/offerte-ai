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


class AIIncompleteResponse(RuntimeError):
    """The model stopped before giving an answer (token limit or refusal)."""


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


def _answer_text(message: Any) -> str:
    """Text of a finished response; a cut-off or refused answer raises instead of failing in the JSON parser."""
    text = _extract_text(message)
    if message.stop_reason == "refusal":
        raise AIIncompleteResponse("Het model weigerde deze aanvraag.")
    if message.stop_reason == "max_tokens":
        raise AIIncompleteResponse(
            f"AI-antwoord afgekapt na {message.usage.output_tokens} tokens"
            + ("" if text else " (nog geen antwoord, enkel redenering)")
            + "."
        )
    if not text:
        raise AIIncompleteResponse("Leeg AI-antwoord.")
    return text


def ask_json(
    system: str, content: list[dict[str, Any]] | str, max_tokens: int = 16000, effort: str | None = None
) -> Any:
    """One-shot call: system prompt + user content blocks -> parsed JSON.

    The model thinks before it answers and that thinking counts against max_tokens, so keep it generous;
    `effort` (low | medium | high | xhigh | max) bounds how much it thinks. Streamed, so long answers
    (multi-page plans) don't hit the HTTP timeout.
    """
    client = get_client()
    settings = get_settings()
    with client.messages.stream(
        model=settings.anthropic_model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": content}],
        **({"output_config": {"effort": effort}} if effort else {}),
    ) as stream:
        message = stream.get_final_message()
    return parse_json_block(_answer_text(message))


def ask_json_with_tools(
    system: str,
    content: list[dict[str, Any]] | str,
    tools: list[dict[str, Any]],
    max_tokens: int = 8000,
    max_continuations: int = 4,
    timeout: float = 120.0,
    effort: str | None = None,
) -> Any:
    """Like ask_json, but the model may use server-side tools (web search/fetch) first.

    Server tools run on Anthropic's side; a long tool loop ends in `pause_turn` and is resumed by sending
    the paused assistant turn back unchanged. The answer is the text after the last tool block.
    """
    client = get_client().with_options(timeout=timeout)
    settings = get_settings()
    messages: list[dict[str, Any]] = [{"role": "user", "content": content}]
    for _ in range(max_continuations + 1):
        message = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=max_tokens,
            system=system,
            tools=tools,
            messages=messages,
            **({"output_config": {"effort": effort}} if effort else {}),
        )
        if message.stop_reason != "pause_turn":
            break
        messages = [messages[0], {"role": "assistant", "content": message.content}]
    if message.stop_reason in ("refusal", "max_tokens"):
        _answer_text(message)  # raises AIIncompleteResponse
    blocks = list(getattr(message, "content", []) or [])
    last_tool = max((i for i, b in enumerate(blocks) if getattr(b, "type", None) != "text"), default=-1)
    final = "\n".join(b.text for b in blocks[last_tool + 1 :] if getattr(b, "type", None) == "text").strip()
    return parse_json_block(final or _extract_text(message))


def image_block(data_b64: str, media_type: str) -> dict[str, Any]:
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data_b64}}


def document_block(data_b64: str, media_type: str = "application/pdf") -> dict[str, Any]:
    return {"type": "document", "source": {"type": "base64", "media_type": media_type, "data": data_b64}}


def text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}
