"""Translate the free texts of a quote (post labels, room names, assumptions, project title) for the customer.

One call per quote version; the result is stored on the quote, so downloading the PDF again costs nothing."""

from __future__ import annotations

import json

from app.ai.client import ask_json

TARGETS = {"fr": "Frans (zoals in België gebruikt)", "en": "Engels"}

SYSTEM = (
    "Je vertaalt korte teksten uit een offerte van een Belgische aannemer (schilderwerken, tuinaanleg) van het "
    "Nederlands naar het {target}. Gebruik de gangbare vakterm, blijf beknopt en behoud de betekenis. Laat "
    "getallen, maten, eenheden en eigennamen ongewijzigd. Antwoord met enkel een JSON-object dat elke brontekst "
    "exact als sleutel heeft en de vertaling als waarde."
)


def translate_texts(texts: list[str], target: str) -> dict[str, str]:
    """{source: translation} for the given Dutch texts. Raises AIUnavailable without an API key."""
    texts = list(dict.fromkeys(t for t in texts if t and t.strip()))
    if not texts or target not in TARGETS:
        return {}
    raw = ask_json(
        SYSTEM.format(target=TARGETS[target]),
        json.dumps(texts, ensure_ascii=False),
        max_tokens=16000,
        effort="low",  # plain term translation: no need for long reasoning
    )
    if not isinstance(raw, dict):
        return {}
    return {src: dst.strip() for src, dst in raw.items() if src in texts and isinstance(dst, str) and dst.strip()}
