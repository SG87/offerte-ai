"""Photo, scan or PDF of a plan -> Geometry (zones, openings, assumptions). The model proposes, the user confirms.

PDFs go to Claude as a document block: it sees every page as an image plus the text layer, so printed
dimensions and room labels in architect PDFs are read directly. (Exact polygons from vector PDFs: roadmap.)
"""

from __future__ import annotations

import base64
import io
import json

from PIL import Image

from app.ai.client import ask_json, document_block, image_block, text_block
from app.engine.skills import Skill
from app.schemas import Geometry

MAX_EDGE_PX = 2000  # keep well under the API's downscale limits and cost
MAX_PDF_BYTES = 30_000_000  # base64-encoded size; the API caps a request at 32 MB
PDF_MEDIA_TYPE = "application/pdf"
PLAN_MAX_TOKENS = 64000
PLAN_EFFORT = "medium"


class PlanTooLarge(ValueError):
    pass


def is_pdf(data: bytes, media_type: str | None = None) -> bool:
    return data[:5] == b"%PDF-" or media_type == PDF_MEDIA_TYPE


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
        "Gebruik meters met 2 decimalen. Antwoord met enkel JSON."
    )


def analyze_plan_image(
    data: bytes, media_type: str | None, skill: Skill, hints: str | None = None
) -> Geometry:
    """Raises AIUnavailable when no API key is configured, PlanTooLarge for oversized PDFs."""
    pdf = is_pdf(data, media_type)
    if pdf:
        if len(data) * 4 / 3 > MAX_PDF_BYTES:
            raise PlanTooLarge(f"PDF te groot ({len(data) / 1e6:.0f} MB); upload enkel de plattegronden.")
        content = [document_block(base64.b64encode(data).decode())]
        instruction = (
            "Lees dit plan (PDF) en geef de Geometry als JSON. Bevat de PDF meerdere pagina's, gebruik dan de "
            "plattegronden en neem elke ruimte één keer op; vermeld in de aannames welke pagina's je gebruikte. "
            "Maten die als tekst op het plan staan gaan voor op afmeten op schaal."
        )
    else:
        b64, mt = prepare_image(data, media_type)
        content = [image_block(b64, mt)]
        instruction = "Lees dit plan en geef de Geometry als JSON."
    if hints:
        instruction += f"\n\nExtra context van de gebruiker (scope/opmerkingen): {hints}"
    content.append(text_block(instruction))
    # Multi-page plan sets made the model think through its whole 32k budget at the default effort (high):
    # medium effort reads plans fine, and 64k leaves room for many rooms (the model allows up to 128k, streamed).
    raw = ask_json(_system_prompt(skill), content, max_tokens=PLAN_MAX_TOKENS, effort=PLAN_EFFORT)
    geometry = Geometry.model_validate(raw)
    geometry.source = "pdf" if pdf else "photo"
    geometry.scale_confirmed = False  # the user always confirms the scale explicitly
    for a in geometry.assumptions:
        a.confirmed = False
        a.source = "ai"
    return geometry
