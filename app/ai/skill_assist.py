"""Free text -> structured adjustments (skill override), and AI-assist edits in the workspace."""

from __future__ import annotations

import json
import re

from app.ai.client import AIUnavailable, ask_json, text_block
from app.engine.skills import Skill
from app.schemas import Adjustment, Geometry

ADJUSTMENT_DOC = """
Beschikbare operaties (één JSON-object per regel, allemaal in één JSON-lijst):
- {"op":"set_param","key":"<param>","value":<getal>}                      # bv. layers_default = 3
- {"op":"tag_zones","match":"<regex op naam/kind>","tag":"<tag>"}          # bv. keuken|badkamer -> vochtig
- {"op":"exclude_zones","match":"<regex>"}                                 # zone buiten scope
- {"op":"set_layers","post":"<postcode>","layers":<int>,"match":"<regex of null>"}
- {"op":"disable_post","post":"<postcode>"}                                # post nooit meerekenen
- {"op":"hours_factor","when":"height_gt","value":<meter>,"factor":<float>}
Voeg aan elk object een korte "reason" toe (NL). Iets dat niet in deze operaties past: laat weg en
vermeld het in "unsupported".
"""


def _skill_context(skill: Skill) -> str:
    posts = ", ".join(f"{p['code']} ({p.get('label', '')})" for p in skill.posts)
    params = json.dumps(skill.params, ensure_ascii=False)
    return f"Sector: {skill.sector}. Posten: {posts}. Parameters: {params}."


def translate_override(free_text: str, skill: Skill) -> tuple[list[Adjustment], list[str], str]:
    """Return (adjustments, unsupported_notes, method). Falls back to heuristics without an API key."""
    free_text = free_text.strip()
    if not free_text:
        return [], [], "none"
    try:
        system = (
            "Je vertaalt rekenregels van een vakman (gewone taal) naar gestructureerde aanpassingen voor een "
            "deterministische offerte-engine. Verzin niets; vertaal enkel wat er staat.\n"
            + _skill_context(skill)
            + ADJUSTMENT_DOC
            + '\nAntwoord met JSON: {"adjustments":[...], "unsupported":["..."]}'
        )
        raw = ask_json(system, [text_block(free_text)], max_tokens=2000)
        adjustments = [Adjustment.model_validate(a) for a in raw.get("adjustments", [])]
        return adjustments, list(raw.get("unsupported", [])), "ai"
    except AIUnavailable:
        return (
            _heuristic_override(free_text),
            ["AI uitgeschakeld: enkel eenvoudige regels herkend."],
            "heuristic",
        )


def _heuristic_override(text: str) -> list[Adjustment]:
    """Tiny rule-based fallback so the app is usable without an API key."""
    out: list[Adjustment] = []
    for line in re.split(r"[\n;.]+", text):
        low = line.lower().strip()
        if not low:
            continue
        if m := re.search(r"(\d)\s*lagen", low):
            out.append(
                Adjustment(op="set_param", key="layers_default", value=int(m.group(1)), reason=line.strip())
            )
        if "niet" in low or "zonder" in low:
            for word in ("badkamer", "keuken", "hal", "garage", "zolder", "kelder", "plinten"):
                if word in low:
                    if word == "plinten":
                        out.append(Adjustment(op="disable_post", post="plinten", reason=line.strip()))
                    else:
                        out.append(Adjustment(op="exclude_zones", match=word, reason=line.strip()))
        if "vochtwerend" in low:
            for word in ("keuken", "badkamer", "wasplaats", "toilet"):
                if word in low:
                    out.append(Adjustment(op="tag_zones", match=word, tag="vochtig", reason=line.strip()))
        if m := re.search(r"boven\s*(\d[,.]\d+)\s*m.*?(\d+)\s*%", low):
            height = float(m.group(1).replace(",", "."))
            factor = 1 + int(m.group(2)) / 100
            out.append(
                Adjustment(
                    op="hours_factor", when="height_gt", value=height, factor=factor, reason=line.strip()
                )
            )
    return out


def assist_edit(instruction: str, geometry: Geometry, skill: Skill) -> tuple[Geometry, list[Adjustment], str]:
    """Workspace AI-assist: apply a natural-language instruction to the geometry (and/or add adjustments).

    Returns (new_geometry, extra_adjustments, summary_nl). Raises AIUnavailable without an API key.
    """
    system = (
        "Je bent de meet-assistent in een offerteplatform. Je krijgt de huidige Geometry (JSON) en een "
        "instructie van de vakman. Pas de Geometry aan (zones toevoegen/verwijderen, in_scope, tags, hoogtes, "
        "openingen) en/of geef aanpassingen voor de rekenregels. Verander nooit maten die niet in de "
        "instructie staan.\n"
        + _skill_context(skill)
        + ADJUSTMENT_DOC
        + '\nAntwoord met JSON: {"geometry": <Geometry>, "adjustments": [...], "summary": "<1-2 zinnen NL>"}\n'
        "Geometry-schema: " + json.dumps(Geometry.model_json_schema(), ensure_ascii=False)
    )
    content = [
        text_block("Huidige Geometry:\n" + geometry.model_dump_json()),
        text_block("Instructie: " + instruction.strip()),
    ]
    raw = ask_json(system, content, max_tokens=8000)
    new_geometry = Geometry.model_validate(raw["geometry"])
    adjustments = [Adjustment.model_validate(a) for a in raw.get("adjustments", [])]
    return new_geometry, adjustments, str(raw.get("summary", "Aangepast."))
