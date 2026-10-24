"""MCP server: "neem een foto van het plan in Claude en krijg een offerte".

Design: the MCP *host* (Claude app) already sees the photo. Claude reads the plan itself using the
sector SKILL.md (exposed as a prompt/resource here) and calls `quote_from_zones` with structured
zones. All arithmetic then happens in the deterministic engine. `analyze_plan_image_url` is the
alternative for hosts that can pass an image URL.

Run:
  stdio (Claude Desktop / Claude Code):   python -m app.mcp_server
  HTTP  (Claude.ai remote connector):     the web app mounts it at /mcp  (see app/main.py)

Organisation (v1, single account): OFFERTE_MCP_ORG_EMAIL in .env, else the first organisation.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from mcp.server import MCPServer
from sqlmodel import select

from app import services
from app.ai.client import AIUnavailable
from app.ai.plan_analysis import analyze_plan_image
from app.config import get_settings
from app.db import init_db, session_scope
from app.engine import load_skill
from app.models import Organisation
from app.schemas import Geometry, Zone

mcp = MCPServer(
    "Offerte-AI",
    instructions=(
        "Offerte-engine voor vakmensen. Workflow: (1) lees de sector-instructies via de prompt "
        "'plan_lezen', (2) haal zones (ruimtes/tuinzones) met maten uit het plan of de foto, "
        "(3) roep quote_from_zones aan. Rekenen doet de engine; verzin geen prijzen."
    ),
)


def _org() -> Organisation:
    settings = get_settings()
    with session_scope() as session:
        org = None
        if settings.mcp_org_email:
            org = services.get_org_by_email(session, settings.mcp_org_email)
        if org is None:
            org = session.exec(select(Organisation).order_by(Organisation.id)).first()
        if org is None:
            raise RuntimeError(
                "Nog geen organisatie: maak eerst een account aan in de web-app (http://localhost:8000)."
            )
        session.expunge(org)
        return org


@mcp.prompt()
def plan_lezen() -> str:
    """Instructies om een plan/foto voor de sector van de gebruiker om te zetten naar zones."""
    org = _org()
    skill = load_skill(org.sector)
    schema = json.dumps(Zone.model_json_schema(), ensure_ascii=False)
    return f"{skill.prompt}\n\nZone-schema voor quote_from_zones:\n{schema}"


@mcp.tool()
def get_pricing_config() -> dict[str, Any]:
    """Huidige prijslijst, uurtarief, btw en sector van de gebruiker (alleen-lezen)."""
    org = _org()
    with session_scope() as session:
        cfg = services.current_config(session, org)
        override = services.current_override(session, org)
    return {
        "sector": org.sector,
        "company": org.company_name,
        "config": cfg.model_dump(),
        "skill_override": override.free_text if override else None,
        "scopes": services.SCOPES_BY_SECTOR.get(org.sector, []),
    }


@mcp.tool()
def quote_from_zones(
    zones: list[Zone],
    scope: list[str],
    project_name: str,
    client_name: str | None = None,
    client_address: str | None = None,
    work_type: str = "renovatie_woning_10j",
    scale_confirmed: bool = False,
    notes: str | None = None,
) -> dict[str, Any]:
    """Maak een project + offerte uit zones (ruimtes of tuinzones) met maten.

    zones: per zone naam, kind (ruimte|terras|gazon|haag|border|oprit|vijver), area_m2, perimeter_m,
           height_m, openings (raam/deur: width_m, height_m, count), tags (bv. 'vochtig'), in_scope.
    scope: welke werken, bv. ["muren","plafonds","houtwerk"] of ["terras","gazon","beplanting","graafwerk"].
    work_type: renovatie_woning_10j (6 % btw BE) | nieuwbouw | niet_woning (21 %).
    Geeft meetstaat, totalen, aannames en download-links terug.
    """
    org = _org()
    settings = get_settings()
    with session_scope() as session:
        project = services.create_project(
            session,
            org,
            project_name,
            client_name=client_name,
            client_address=client_address,
            work_type=work_type,
            scope=scope,
            notes=notes,
        )
        geometry = Geometry(source="mcp", scale_confirmed=scale_confirmed, zones=zones)
        services.save_geometry(session, project, geometry)
        quote, result = services.run_quote(session, org, project)
        base = f"{settings.base_url}/projects/{project.id}"
        return {
            "project_id": project.id,
            "quote_number": f"{quote.number} v{quote.version}",
            "indicative": result.indicative,
            "totals": result.totals.model_dump(),
            "lines": [
                {
                    "zone": line.zone_name,
                    "post": line.post_label,
                    "net": line.net_qty,
                    "unit": line.unit,
                    "hours": line.hours,
                    "total": line.line_total,
                }
                for line in result.lines
            ],
            "assumptions": [a.text for a in result.assumptions],
            "warnings": result.warnings,
            "links": {
                "werkruimte": base,
                "offerte_pdf": f"{base}/offerte.pdf",
                "meetstaat_xlsx": f"{base}/meetstaat.xlsx",
                "plan_svg": f"{base}/plan.svg",
            },
        }


@mcp.tool()
def analyze_plan_image_url(
    image_url: str, scope: list[str] | None = None, notes: str | None = None
) -> dict[str, Any]:
    """Laat de server zelf een plan-afbeelding (URL) lezen met Claude Vision; geeft zones terug om te bevestigen."""
    org = _org()
    skill = load_skill(org.sector)
    r = httpx.get(image_url, timeout=20, follow_redirects=True)
    r.raise_for_status()
    hints = (f"scope: {', '.join(scope)}" if scope else "") + (f"; {notes}" if notes else "")
    try:
        geometry = analyze_plan_image(
            r.content, r.headers.get("content-type", "image/jpeg").split(";")[0], skill, hints or None
        )
    except AIUnavailable as exc:
        return {"error": str(exc)}
    return geometry.model_dump()


@mcp.tool()
def update_skill_override(free_text: str) -> dict[str, Any]:
    """Voeg rekenregels in gewone taal toe aan de sector-skill van de gebruiker (bv. 'altijd 3 lagen bij donkere kleuren')."""
    org = _org()
    with session_scope() as session:
        row, unsupported = services.save_override(session, org, free_text)
        return {
            "version": row.version,
            "translated_by": row.translated_by,
            "adjustments": row.adjustments,
            "unsupported": unsupported,
        }


@mcp.tool()
def request_sector(description: str) -> dict[str, Any]:
    """Vraag een nieuwe sector aan voor het platform."""
    org = _org()
    with session_scope() as session:
        req = services.request_sector(session, org.email, description, org.id)
        return {"request_id": req.id, "status": "ontvangen"}


def main() -> None:
    init_db()
    mcp.run()  # stdio


if __name__ == "__main__":
    main()
