"""Photo / scan of a plan -> Geometry (zones, openings, assumptions). Vision model proposes, user confirms."""

from __future__ import annotations

import base64
import io
import json

from PIL import Image

from app.ai.client import ask_json, image_block, text_block
from app.engine.skills import Skill
from app.schemas import Geometry

MAX_EDGE_PX = 2000  # keep well under the API's downscale limits and cost


def prepare_image(data: bytes, media_type: str | None) -> tuple[str, str]:
    """Downscale large photos, normalise to JPEG/PNG, return (base64, media_type)."""
    img = Image.open(io.BytesIO(data))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    if max(img.size) > MAX_EDGE_PX:
        img.thumbnail((MAX_EDGE_PX, MAX_EDGE_PX))
    buf = io.BytesIO()
    if media_type == "image/png":
        img.save(buf, format="PNG", optimize=True)
        out_type = "image/png"
    else:
        img.save(buf, format="JPEG", quality=88)
        out_type = "image/jpeg"
    return base64.b64encode(buf.getvalue()).decode(), out_type


def _system_prompt(skill: Skill) -> str:
    schema = json.dumps(Geometry.model_json_schema(), ensure_ascii=False)
    return (
        f"{skill.prompt}\n\n"
        "## Geometry JSON-schema (antwoord moet hieraan voldoen)\n"
        f"{schema}\n\n"
        'Gebruik meters met 2 decimalen. Zet `source` op "photo". Antwoord met enkel JSON.'
    )


def analyze_plan_image(
    data: bytes, media_type: str | None, skill: Skill, hints: str | None = None
) -> Geometry:
    """Raises AIUnavailable when no API key is configured."""
    b64, mt = prepare_image(data, media_type)
    content = [image_block(b64, mt)]
    instruction = "Lees dit plan en geef de Geometry als JSON."
    if hints:
        instruction += f"\n\nExtra context van de gebruiker (scope/opmerkingen): {hints}"
    content.append(text_block(instruction))
    raw = ask_json(_system_prompt(skill), content, max_tokens=6000)
    geometry = Geometry.model_validate(raw)
    geometry.source = "photo"
    geometry.scale_confirmed = False  # the user always confirms the scale explicitly
    for a in geometry.assumptions:
        a.confirmed = False
        a.source = "ai"
    return geometry
