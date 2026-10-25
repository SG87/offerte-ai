"""Edit a concept invoice from an instruction in plain language ("korting 5 % op het lakwerk", "voeg 2 uur
extra werk toe aan 45 euro", "vertaal naar het Frans"). Claude returns the new lines; amounts and btw are
computed by the app (app/invoicing.py)."""

from __future__ import annotations

import json

from app.ai.client import ask_json

SYSTEM = """Je past een conceptfactuur aan van een Belgische aannemer volgens de opdracht van de gebruiker.

Je krijgt de factuur als JSON: title, language (nl|fr|en: taal van de klant), b2b, due_days, notes en lines.
Elke regel heeft description, qty, unit, unit_price (EXCL. btw, per eenheid) en vat_pct.

Regels:
- Verander alleen wat de opdracht vraagt; laat de andere regels exact zoals ze zijn.
- Prijzen zijn altijd excl. btw. Noemt de gebruiker een bedrag incl. btw, reken het om met het btw-tarief.
- Een korting of creditregel is een regel met een negatieve unit_price en een duidelijke omschrijving.
- Nieuwe regels krijgen het btw-tarief van de andere regels, tenzij de opdracht iets anders zegt.
- Teksten op de factuur staan in de taal van de klant (language), tenzij de opdracht vraagt om te vertalen.
- Reken geen totalen uit; die berekent de app.

Antwoord met enkel JSON:
{"title": str, "lines": [{"description": str, "qty": number, "unit": str, "unit_price": number, "vat_pct": number}],
 "notes": str|null, "due_days": int, "summary": "korte uitleg in het Nederlands van wat je veranderde"}"""


def edit_invoice(invoice: dict, instruction: str) -> dict:
    raw = ask_json(
        SYSTEM,
        f"Factuur:\n{json.dumps(invoice, ensure_ascii=False)}\n\nOpdracht: {instruction}",
        max_tokens=32000,
        effort="medium",
    )
    if not isinstance(raw, dict) or not isinstance(raw.get("lines"), list):
        raise ValueError("Onverwacht antwoord van de AI (geen regels).")
    return raw
