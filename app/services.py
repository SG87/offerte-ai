"""Use-cases shared by the web app, the REST API and the MCP server. All DB access goes through here."""

from __future__ import annotations

import json
import re
from datetime import date

from sqlmodel import Session, select

from app.ai.client import AIUnavailable
from app.ai.company_lookup import lookup_company
from app.ai.plan_analysis import analyze_plan_image
from app.ai.skill_assist import assist_edit, translate_override
from app.config import get_settings
from app.engine import calculate_quote, load_skill
from app.models import ConfigVersion, Organisation, Plan, Project, Quote, SectorRequest, SkillOverride
from app.outputs import build_meetstaat_xlsx, build_offerte_pdf, build_plan_svg
from app.outputs.offerte_pdf import OffertePartijen
from app.schemas import Adjustment, CompanyLookup, Geometry, Opening, PriceConfig, QuoteResult

WORK_TYPES = {
    "renovatie_woning_10j": ("Renovatie woning > 10 jaar (6 % btw BE)", 6.0),
    "nieuwbouw": ("Nieuwbouw (21 % btw)", 21.0),
    "niet_woning": ("Niet-woning / bedrijf (21 % btw)", 21.0),
}

SCOPES_BY_SECTOR = {
    "schilder": ["muren", "plafonds", "houtwerk", "buiten"],
    "tuinaanleg": ["terras", "gazon", "beplanting", "graafwerk"],
    "generiek": ["muren", "plafonds", "houtwerk"],
}


# ---------- organisation / onboarding ----------


def get_org_by_email(session: Session, email: str) -> Organisation | None:
    return session.exec(select(Organisation).where(Organisation.email == email.strip().lower())).first()


def get_org_by_api_key(session: Session, api_key: str) -> Organisation | None:
    return session.exec(select(Organisation).where(Organisation.api_key == api_key)).first()


def signup(
    session: Session, email: str, do_lookup: bool = True
) -> tuple[Organisation, CompanyLookup | None, bool]:
    """Create (or fetch) the organisation for an e-mail; run the domain lookup for new ones."""
    email = email.strip().lower()
    org = get_org_by_email(session, email)
    if org:
        return org, None, False
    lookup = lookup_company(email) if do_lookup else None
    org = Organisation(email=email)
    if lookup:
        org.company_name = lookup.company_name
        org.website = lookup.website
        org.address = lookup.address
        org.vat_number = lookup.vat_number
        org.phone = lookup.phone
        org.language = lookup.language or "nl"
        if lookup.sector in ("schilder", "tuinaanleg") and lookup.sector_confidence >= 0.5:
            org.sector = lookup.sector
    session.add(org)
    session.commit()
    session.refresh(org)
    return org, lookup, True


def complete_onboarding(session: Session, org: Organisation, sector: str, **fields) -> Organisation:
    org.sector = sector if sector in {s["sector"] for s in available_sector_list()} else "generiek"
    for k, v in fields.items():
        if v is not None and hasattr(org, k):
            setattr(org, k, v)
    org.onboarding_done = True
    session.add(org)
    session.commit()
    session.refresh(org)
    ensure_config(session, org)
    return org


def available_sector_list() -> list[dict[str, str]]:
    from app.engine import available_sectors

    return available_sectors()


def request_sector(
    session: Session, email: str, description: str, org_id: int | None = None
) -> SectorRequest:
    req = SectorRequest(org_id=org_id, email=email, description=description.strip())
    session.add(req)
    session.commit()
    session.refresh(req)
    return req


# ---------- configuration ----------


def ensure_config(session: Session, org: Organisation) -> ConfigVersion:
    latest = latest_config_row(session, org)
    if latest and latest.sector == org.sector:
        return latest
    skill = load_skill(org.sector)
    cfg = skill.default_price_config()
    cfg.version = (latest.version + 1) if latest else 1
    row = ConfigVersion(
        org_id=org.id, version=cfg.version, sector=org.sector, data_json=cfg.model_dump_json()
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def latest_config_row(session: Session, org: Organisation) -> ConfigVersion | None:
    return session.exec(
        select(ConfigVersion).where(ConfigVersion.org_id == org.id).order_by(ConfigVersion.version.desc())
    ).first()


def current_config(session: Session, org: Organisation) -> PriceConfig:
    row = ensure_config(session, org)
    return PriceConfig.model_validate_json(row.data_json)


def save_config(session: Session, org: Organisation, config: PriceConfig) -> ConfigVersion:
    latest = latest_config_row(session, org)
    config.version = (latest.version + 1) if latest else 1
    config.sector = org.sector
    row = ConfigVersion(
        org_id=org.id, version=config.version, sector=org.sector, data_json=config.model_dump_json()
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def current_override(session: Session, org: Organisation) -> SkillOverride | None:
    return session.exec(
        select(SkillOverride)
        .where(SkillOverride.org_id == org.id, SkillOverride.sector == org.sector)
        .order_by(SkillOverride.version.desc())
    ).first()


def save_override(session: Session, org: Organisation, free_text: str) -> tuple[SkillOverride, list[str]]:
    skill = load_skill(org.sector)
    adjustments, unsupported, method = translate_override(free_text, skill)
    prev = current_override(session, org)
    row = SkillOverride(
        org_id=org.id,
        sector=org.sector,
        version=(prev.version + 1) if prev else 1,
        free_text=free_text,
        adjustments_json=json.dumps(
            [a.model_dump(exclude_none=True) for a in adjustments], ensure_ascii=False
        ),
        translated_by=method,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row, unsupported


def current_adjustments(session: Session, org: Organisation) -> tuple[list[Adjustment], int | None]:
    row = current_override(session, org)
    if not row:
        return [], None
    return [Adjustment.model_validate(a) for a in row.adjustments], row.version


# ---------- projects / plans ----------


def create_project(
    session: Session,
    org: Organisation,
    name: str,
    client_name: str | None = None,
    client_address: str | None = None,
    work_type: str = "renovatie_woning_10j",
    scope: list[str] | None = None,
    notes: str | None = None,
) -> Project:
    label, vat = WORK_TYPES.get(work_type, WORK_TYPES["renovatie_woning_10j"])
    scope = scope or SCOPES_BY_SECTOR.get(org.sector, [])[:2]
    project = Project(
        org_id=org.id,
        name=name.strip(),
        client_name=client_name,
        client_address=client_address,
        work_type=work_type,
        vat_pct=vat,
        scope_json=json.dumps(scope),
        notes=notes,
    )
    session.add(project)
    session.commit()
    session.refresh(project)
    return project


def get_project(session: Session, org: Organisation, project_id: int) -> Project | None:
    p = session.get(Project, project_id)
    return p if p and p.org_id == org.id else None


def list_projects(session: Session, org: Organisation) -> list[Project]:
    return list(session.exec(select(Project).where(Project.org_id == org.id).order_by(Project.id.desc())))


def latest_plan(session: Session, project: Project) -> Plan | None:
    return session.exec(select(Plan).where(Plan.project_id == project.id).order_by(Plan.id.desc())).first()


def current_geometry(session: Session, project: Project) -> Geometry:
    plan = latest_plan(session, project)
    if plan and plan.geometry_json not in ("{}", ""):
        return Geometry.model_validate_json(plan.geometry_json)
    return Geometry()


def save_geometry(
    session: Session,
    project: Project,
    geometry: Geometry,
    filename: str | None = None,
    media_type: str | None = None,
    path: str | None = None,
) -> Plan:
    plan = latest_plan(session, project)
    if plan is None or filename:
        plan = Plan(project_id=project.id, filename=filename, media_type=media_type, path=path)
    plan.geometry_json = geometry.model_dump_json()
    project.status = "gemeten" if geometry.zones else project.status
    session.add(plan)
    session.add(project)
    session.commit()
    session.refresh(plan)
    return plan


def analyze_upload(
    session: Session, org: Organisation, project: Project, data: bytes, filename: str, media_type: str | None
) -> tuple[Geometry, str]:
    """Store the upload, run the vision analysis (if AI enabled). Returns (geometry, message)."""
    settings = get_settings()
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename)[:80]
    path = settings.uploads_dir / f"p{project.id}_{safe}"
    path.write_bytes(data)
    skill = load_skill(org.sector)
    hints = f"scope: {', '.join(project.scope)}" + (
        f"; opmerkingen: {project.notes}" if project.notes else ""
    )
    try:
        geometry = analyze_plan_image(data, media_type, skill, hints)
        message = f"Plan gelezen: {len(geometry.zones)} zones herkend. Bevestig de schaal en de aannames."
    except AIUnavailable:
        geometry = current_geometry(session, project)
        geometry.source = "manual"
        message = "AI uitgeschakeld (geen ANTHROPIC_API_KEY): voeg de zones hieronder manueel toe."
    save_geometry(session, project, geometry, filename=filename, media_type=media_type, path=str(path))
    return geometry, message


def run_assist(
    session: Session, org: Organisation, project: Project, instruction: str
) -> tuple[Geometry, str]:
    skill = load_skill(org.sector)
    geometry = current_geometry(session, project)
    new_geometry, extra, summary = assist_edit(instruction, geometry, skill)
    save_geometry(session, project, new_geometry)
    if extra:
        # project-scoped adjustments are kept on the project notes for now (v1); skill-wide rules go via config
        project.notes = (project.notes or "") + "\n[AI-assist] " + "; ".join(a.reason or a.op for a in extra)
        session.add(project)
        session.commit()
    return new_geometry, summary


# ---------- quotes ----------


def _next_quote_number(session: Session, org: Organisation) -> str:
    year = date.today().year
    rows = session.exec(
        select(Quote).join(Project).where(Project.org_id == org.id, Quote.number.like(f"{year}-%"))
    ).all()
    return f"{year}-{len(rows) + 1:04d}"


def run_quote(
    session: Session, org: Organisation, project: Project, geometry: Geometry | None = None
) -> tuple[Quote, QuoteResult]:
    skill = load_skill(org.sector)
    config = current_config(session, org)
    adjustments, override_version = current_adjustments(session, org)
    geometry = geometry or current_geometry(session, project)
    result = calculate_quote(
        geometry,
        skill,
        config,
        scope=project.scope,
        adjustments=adjustments,
        vat_pct=project.vat_pct,
        override_version=override_version,
    )
    prev = latest_quote(session, project)
    quote = Quote(
        project_id=project.id,
        number=prev.number if prev else _next_quote_number(session, org),
        version=(prev.version + 1) if prev else 1,
        config_version=config.version,
        skill_version=skill.version,
        result_json=result.model_dump_json(),
    )
    project.status = "offerte"
    session.add(quote)
    session.add(project)
    session.commit()
    session.refresh(quote)
    return quote, result


def latest_quote(session: Session, project: Project) -> Quote | None:
    return session.exec(select(Quote).where(Quote.project_id == project.id).order_by(Quote.id.desc())).first()


def quote_result(quote: Quote) -> QuoteResult:
    return QuoteResult.model_validate_json(quote.result_json)


def offerte_pdf(
    org: Organisation, project: Project, quote: Quote, intro: str | None = None, conditions: str | None = None
) -> bytes:
    result = quote_result(quote)
    skill = load_skill(org.sector)
    parties = OffertePartijen(
        company_name=org.company_name or "[JOUW BEDRIJF]",
        company_address=org.address,
        company_vat=org.vat_number,
        company_email=org.email,
        client_name=project.client_name,
        client_address=project.client_address,
        number=f"{quote.number} v{quote.version}",
        title=project.name,
        intro=intro
        or f"Beste {project.client_name or 'klant'}, hierbij onze offerte voor {project.name.lower()}, gemeten op het aangeleverde plan.",
        conditions=conditions
        or "Betaling 30 dagen na factuurdatum. Prijzen geldig zoals vermeld. Meerwerk wordt vooraf besproken.",
        validity_days=current_config_validity(skill),
    )
    return build_offerte_pdf(result, parties)


def current_config_validity(skill) -> int:
    return int(skill.default_prices.get("validity_days", 30))


def meetstaat_xlsx(project: Project, quote: Quote) -> bytes:
    return build_meetstaat_xlsx(quote_result(quote), f"Meetstaat — {project.name}")


def plan_svg(session: Session, project: Project, quote: Quote | None = None) -> str:
    geometry = current_geometry(session, project)
    return build_plan_svg(geometry, quote_result(quote) if quote else None)


# ---------- small helpers for manual entry ----------

_OPENING_RE = re.compile(
    r"(raam|deur|poort|andere)\s*(\d+(?:[.,]\d+)?)\s*[x×]\s*(\d+(?:[.,]\d+)?)(?:\s*[x×]\s*(\d+))?",
    re.IGNORECASE,
)


def parse_openings(text: str) -> list[Opening]:
    """'raam 1.8x1.4 x2; deur 0.83x2.04' -> [Opening(...), Opening(...)]"""
    out = []
    for m in _OPENING_RE.finditer(text or ""):
        kind, w, h, n = m.groups()
        out.append(
            Opening(
                kind=kind.lower(),
                width_m=float(w.replace(",", ".")),
                height_m=float(h.replace(",", ".")),
                count=int(n or 1),
            )
        )
    return out


def format_openings(openings: list[Opening]) -> str:
    return "; ".join(
        f"{o.kind} {o.width_m:g}x{o.height_m:g}" + (f" x{o.count}" if o.count > 1 else "") for o in openings
    )
