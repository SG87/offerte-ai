"""Use-cases shared by the web app, the REST API and the MCP server. All DB access goes through here."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, date, datetime
from pathlib import Path

from sqlmodel import Session, select

from app import ai_jobs, branding, i18n, planning, workflow
from app.ai.client import AIUnavailable
from app.ai.company_lookup import lookup_company
from app.ai.plan_analysis import PDF_MEDIA_TYPE, PlanTooLarge, analyze_plan_image, is_pdf
from app.ai.skill_assist import assist_edit, translate_override
from app.ai.translate import translate_texts
from app.config import get_settings
from app.db import session_scope
from app.engine import calculate_quote, load_skill
from app.engine.geometry import wall_net
from app.models import (
    ConfigVersion,
    Customer,
    Organisation,
    Plan,
    Project,
    Quote,
    SectorRequest,
    SkillOverride,
)
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


log = logging.getLogger(__name__)


# ---------- organisation / onboarding ----------


def get_org_by_email(session: Session, email: str) -> Organisation | None:
    return session.exec(select(Organisation).where(Organisation.email == email.strip().lower())).first()


def get_org_by_api_key(session: Session, api_key: str) -> Organisation | None:
    return session.exec(select(Organisation).where(Organisation.api_key == api_key)).first()


def signup(
    session: Session, email: str, do_lookup: bool = True
) -> tuple[Organisation, CompanyLookup | None, bool]:
    """Create (or fetch) the organisation for an e-mail; run the domain lookup for new ones (synchronously:
    API). The web sign-up creates the account first and runs the lookup as a background AI job."""
    email = email.strip().lower()
    org = get_org_by_email(session, email)
    if org:
        return org, None, False
    org = Organisation(email=email)
    session.add(org)
    session.commit()
    session.refresh(org)
    lookup = lookup_company(email) if do_lookup else None
    if lookup:
        apply_lookup(session, org, lookup, set_sector=True)
    return org, lookup, True


LOOKUP_FIELDS = ("company_name", "website", "address", "vat_number", "phone")


def apply_lookup(
    session: Session, org: Organisation, lookup: CompanyLookup, overwrite: bool = False, set_sector: bool = False
) -> list[str]:
    """Copy what the lookup found onto the organisation. Without `overwrite` only empty fields are filled, so a
    re-run never clobbers what the user typed. Returns the names of the fields that changed."""
    changed = []
    for name in LOOKUP_FIELDS:
        value = getattr(lookup, name)
        if value and (overwrite or not getattr(org, name)) and getattr(org, name) != value:
            setattr(org, name, value)
            changed.append(name)
    if overwrite or not org.brand_primary:
        for name in ("brand_primary", "brand_secondary"):
            if getattr(lookup, name) and getattr(org, name) != getattr(lookup, name):
                setattr(org, name, getattr(lookup, name))
                changed.append(name)
    if lookup.language and not org.onboarding_done:
        org.language = lookup.language
    if set_sector and lookup.sector in ("schilder", "tuinaanleg") and lookup.sector_confidence >= 0.5:
        org.sector = lookup.sector
    session.add(org)
    session.commit()
    if (lookup.logo_svg or lookup.logo_url) and (overwrite or not org.logo_file):
        name = (
            branding.store_logo_svg(org.id, lookup.logo_svg)
            if lookup.logo_svg
            else branding.store_logo_url(org.id, lookup.logo_url)
        )
        if name:
            org.logo_file = name
            changed.append("logo")
            session.add(org)
            session.commit()
    session.refresh(org)
    return changed


def start_company_lookup(session: Session, org: Organisation, overwrite: bool = False):
    """Start the website lookup as a background AI job. Returns (job, work) for the caller's background task."""
    job = ai_jobs.start(session, org, "bedrijf", f"Bedrijfsgegevens opzoeken via {org.email.split('@')[-1]}",
                        link="/onboarding" if not org.onboarding_done else "/account")

    def work(session: Session, org: Organisation, job) -> tuple[str, str | None]:
        lookup = lookup_company(org.email)
        changed = apply_lookup(session, org, lookup, overwrite=overwrite, set_sector=not org.onboarding_done)
        if not lookup.website and not changed:
            return "Geen website gevonden voor dit e-maildomein; vul de gegevens zelf in.", None
        found = lookup.company_name or lookup.domain
        return (f"{found} herkend via {lookup.website or 'het web'}: "
                + (f"{len(changed)} gegeven{'' if len(changed) == 1 else 's'} ingevuld." if changed else "niets nieuws gevonden.")), None

    return job, work


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


def set_logo(session: Session, org: Organisation, data: bytes, content_type: str | None) -> bool:
    """Replace the organisation logo with an uploaded file; False when the file isn't a usable image."""
    name = branding.store_logo_bytes(org.id, data, content_type)
    if not name:
        return False
    org.logo_file = name
    session.add(org)
    session.commit()
    return True


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


CUSTOMER_KINDS = {
    "b2c": "B2C (particulier): prijzen incl. btw",
    "b2b": "B2B (bedrijf): prijzen excl. btw",
}
DEFAULT_WORK_TYPE = {"b2c": "renovatie_woning_10j", "b2b": "niet_woning"}


class CustomerRequired(ValueError):
    """A project always belongs to a customer: create or pick one first."""


# ---------- customers ----------


def get_customer(session: Session, org: Organisation, customer_id: int | None) -> Customer | None:
    c = session.get(Customer, customer_id) if customer_id else None
    return c if c and c.org_id == org.id else None


def resolve_customer(
    session: Session,
    org: Organisation,
    customer_id: int | None = None,
    name: str | None = None,
    kind: str | None = None,
    address: str | None = None,
    vat_number: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    contact_name: str | None = None,
    language: str | None = None,
) -> Customer:
    """The customer with this id; else the one with this name (case-insensitive); else a new one.
    Neither an id nor a name -> CustomerRequired."""
    if customer_id is not None:
        customer = get_customer(session, org, customer_id)
        if customer is None:
            raise CustomerRequired(f"Klant {customer_id} bestaat niet.")
        return customer
    name = (name or "").strip()
    if not name:
        raise CustomerRequired("Een project heeft een klant nodig: kies een klant of maak er eerst een aan.")
    existing = session.exec(select(Customer).where(Customer.org_id == org.id)).all()
    match = next((c for c in existing if c.name.strip().lower() == name.lower()), None)
    if match:
        return match
    customer = Customer(
        org_id=org.id,
        name=name,
        kind=kind if kind in CUSTOMER_KINDS else ("b2b" if vat_number else "b2c"),
        address=(address or "").strip() or None,
        vat_number=re.sub(r"[\s.]", "", vat_number or "").upper() or None,
        email=(email or "").strip() or None,
        phone=(phone or "").strip() or None,
        contact_name=(contact_name or "").strip() or None,
        language=language if language in i18n.LANGUAGES else org.language,
    )
    session.add(customer)
    session.commit()
    session.refresh(customer)
    return customer


def project_customer(session: Session, project: Project) -> Customer | None:
    return session.get(Customer, project.customer_id) if project.customer_id else None


def prices_incl_vat(customer: Customer | None) -> bool:
    """B2C quotes show prices incl. btw, B2B excl. btw. Old projects without a customer count as B2C."""
    return not (customer and customer.is_b2b)


# ---------- projects / plans ----------


def create_project(
    session: Session,
    org: Organisation,
    name: str,
    customer: Customer,
    client_address: str | None = None,
    work_type: str | None = None,
    scope: list[str] | None = None,
    notes: str | None = None,
) -> Project:
    """client_address is the work site; empty = the customer's address. No work_type -> by customer type."""
    if customer is None or customer.org_id != org.id:
        raise CustomerRequired("Een project heeft een klant nodig: kies een klant of maak er eerst een aan.")
    work_type = work_type if work_type in WORK_TYPES else DEFAULT_WORK_TYPE.get(customer.kind, "renovatie_woning_10j")
    label, vat = WORK_TYPES[work_type]
    scope = scope or SCOPES_BY_SECTOR.get(org.sector, [])[:2]
    project = Project(
        org_id=org.id,
        name=name.strip(),
        client_name=customer.name,
        client_address=client_address or customer.address,
        work_type=work_type,
        vat_pct=vat,
        scope_json=json.dumps(scope),
        notes=notes,
        customer_id=customer.id,
        status="voorbereid",
        status_changed_at=datetime.now(UTC),
    )
    session.add(project)
    session.commit()
    session.refresh(project)
    workflow.log(session, project, "Project aangemaakt.", kind="status", to_status="voorbereid", actor="gebruiker")
    return project  # no execution proposal yet: that comes once the plan is read (propose_after_reading)


def set_project_customer(session: Session, project: Project, customer: Customer) -> Project:
    """Link (another) customer; the work site follows unless it was set apart from the old customer's address."""
    old = project_customer(session, project)
    if not project.client_address or (old and project.client_address == old.address):
        project.client_address = customer.address
    project.customer_id = customer.id
    project.client_name = customer.name
    session.add(project)
    session.commit()
    session.refresh(project)
    workflow.log(session, project, f"Klant: {customer.name} ({customer.kind.upper()}).", kind="info")
    return project


def delete_project(session: Session, org: Organisation, project: Project) -> str:
    """Remove a project with its plans (and uploaded files), quotes, history, planning, to-dos and AI jobs.
    Reserved material goes back to stock. Mails, purchase orders and integration events stay as a record,
    detached from the project. Returns the project name."""
    from app.integrations import stock as stock_mod
    from app.models import (
        AIJob,
        IntegrationEvent,
        OutboxMessage,
        PlanningItem,
        ProjectEvent,
        PurchaseOrderLine,
        StockReservation,
        Todo,
    )

    if project.org_id != org.id:
        raise ValueError("Project hoort niet bij deze organisatie.")
    name, pid = project.name, project.id
    stock_mod.release_for_project(session, org, project)
    for item in session.exec(select(PlanningItem).where(PlanningItem.project_id == pid)).all():
        planning.delete_item(session, item)
    for plan in session.exec(select(Plan).where(Plan.project_id == pid)).all():
        if plan.path:
            Path(plan.path).unlink(missing_ok=True)
        session.delete(plan)
    for model in (Quote, ProjectEvent, StockReservation, Todo, AIJob):
        for row in session.exec(select(model).where(model.project_id == pid)).all():
            session.delete(row)
    for model in (OutboxMessage, PurchaseOrderLine):
        for row in session.exec(select(model).where(model.project_id == pid)).all():
            row.project_id = None
            session.add(row)
    for ev in session.exec(select(IntegrationEvent).where(IntegrationEvent.project_id == pid)).all():
        ev.project_id = None
        if ev.status == "open":
            ev.status, ev.error = "afgehandeld", f"Project '{name}' verwijderd."
        session.add(ev)
    session.delete(project)
    session.commit()
    return name


def propose_execution(session: Session, org: Organisation, project: Project, hours_total: float | None = None) -> Project:
    """Suggest crew and dates; skipped once the user edited the proposal by hand."""
    if project.proposal_locked:
        return project
    s = planning.suggest(session, org, project, hours_total)
    project = planning.apply_suggestion(session, project, s)
    if s["crew"]:
        workflow.log(session, project, f"Voorstel uitvoering: {s['text']}", kind="info", link=f"/projects/{project.id}#voorstel")
    return project


def propose_after_reading(session: Session, org: Organisation, project: Project) -> str | None:
    """The plan was read (AI analysis, or Claude via MCP): the zones give the hours, so now propose who does
    the work and when. Never fails the analysis; returns the proposal text or None."""
    if project.proposal_locked:
        return None
    try:
        result = preview_quote(session, org, project)
        propose_execution(session, org, project, result.totals.hours_total if result else None)
    except Exception:
        log.exception("Voorstel uitvoering mislukt (project %s)", project.id)
        return None
    return project.proposal_note


def projects_overview(session: Session, org: Organisation) -> list[dict]:
    """Projects with what the board needs: status, latest quote total, days in the current status."""
    now = datetime.now(UTC)
    customers = {c.id: c for c in session.exec(select(Customer).where(Customer.org_id == org.id)).all()}
    rows = []
    for p in list_projects(session, org):
        quote = latest_quote(session, p)
        totals = quote_result(quote).totals if quote else None
        incl = prices_incl_vat(customers.get(p.customer_id))
        since = p.status_changed_at or p.created_at
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        rows.append(
            {
                "project": p,
                "status": p.status,
                "status_label": workflow.label(p.status),
                "phase": workflow.BY_KEY[p.status].phase if p.status in workflow.BY_KEY else "offerte",
                "quote": quote,
                "customer": customers.get(p.customer_id),
                "incl_vat": incl,
                "amount": (totals.total_incl if incl else totals.subtotal_excl) if totals else None,
                "subtotal_excl": totals.subtotal_excl if totals else None,
                "days": (now - since).days,
            }
        )
    return rows


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
    session.add(plan)
    session.add(project)
    session.commit()
    session.refresh(plan)
    return plan


ANALYSIS_STALE_AFTER_S = 20 * 60  # a "running" analysis this old was cut off (e.g. server restart)


def store_plan_upload(
    session: Session, org: Organisation, project: Project, data: bytes, filename: str, media_type: str | None
) -> Plan:
    """Save the file as the project's new plan. Zones carry over until the analysis replaces them."""
    if is_pdf(data, media_type):
        media_type = PDF_MEDIA_TYPE  # browsers/curl sometimes send octet-stream; the template needs the real type
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename)[:80]
    path = get_settings().uploads_dir / f"p{project.id}_{safe}"
    path.write_bytes(data)
    plan = Plan(
        project_id=project.id,
        filename=filename,
        media_type=media_type,
        path=str(path),
        geometry_json=current_geometry(session, project).model_dump_json(),
    )
    if get_settings().ai_enabled:
        plan.analysis_status = "running"
        plan.analysis_started_at = datetime.now(UTC)
    else:
        plan.analysis_message = "AI uitgeschakeld (geen ANTHROPIC_API_KEY): voeg de zones hieronder manueel toe."
    session.add(plan)
    session.commit()
    session.refresh(plan)
    if plan.analysis_status == "running":
        ai_jobs.start(session, org, "plan", f"Plan lezen: {filename}", link=f"/projects/{project.id}",
                      project_id=project.id, ref_id=plan.id)
    return plan


def _plan_job(session: Session, plan: Plan):
    from app.models import AIJob

    return session.exec(select(AIJob).where(AIJob.kind == "plan", AIJob.ref_id == plan.id)).first()


def run_plan_analysis(session: Session, org: Organisation, project: Project, plan: Plan) -> tuple[Geometry, str]:
    """Read the stored plan with Claude and put the zones on it. Unexpected errors are recorded, then re-raised."""
    skill = load_skill(org.sector)
    hints = f"scope: {', '.join(project.scope)}" + (f"; opmerkingen: {project.notes}" if project.notes else "")
    geometry = Geometry.model_validate_json(plan.geometry_json)
    try:
        data = Path(plan.path).read_bytes()
        geometry = analyze_plan_image(data, plan.media_type, skill, hints)
        plan.analysis_status = "done"
        plan.analysis_message = (
            f"Plan gelezen: {len(geometry.zones)} zones herkend. Bevestig de schaal en de aannames."
        )
    except AIUnavailable:
        geometry.source = "manual"
        plan.analysis_status = None
        plan.analysis_message = "AI uitgeschakeld (geen ANTHROPIC_API_KEY): voeg de zones hieronder manueel toe."
    except PlanTooLarge as exc:
        plan.analysis_status = "failed"
        plan.analysis_message = f"Plan opgeslagen, maar niet geanalyseerd: {exc}"
    except Exception as exc:
        plan.analysis_status = "failed"
        plan.analysis_message = f"Analyse mislukt ({type(exc).__name__}: {exc}). Voeg de zones manueel toe."
        session.add(plan)
        session.commit()
        if job := _plan_job(session, plan):
            ai_jobs.finish(session, job, False, plan.analysis_message)
        raise
    plan.geometry_json = geometry.model_dump_json()
    session.add(plan)
    session.commit()
    if plan.analysis_status == "done" and geometry.zones and propose_after_reading(session, org, project):
        plan.analysis_message += " Voorstel uitvoering staat klaar."
        session.add(plan)
        session.commit()
    if job := _plan_job(session, plan):
        ai_jobs.finish(session, job, plan.analysis_status != "failed", plan.analysis_message or "")
    return geometry, plan.analysis_message


def analyze_upload(
    session: Session, org: Organisation, project: Project, data: bytes, filename: str, media_type: str | None
) -> tuple[Geometry, str]:
    """Store the upload and analyse it right away (API/MCP). Returns (geometry, message)."""
    plan = store_plan_upload(session, org, project, data, filename, media_type)
    return run_plan_analysis(session, org, project, plan)


def analyze_plan_in_background(plan_id: int) -> None:
    """Web flow: the upload returns at once, the page polls plan_status() until this finishes."""
    with session_scope() as session:
        plan = session.get(Plan, plan_id)
        project = session.get(Project, plan.project_id) if plan else None
        org = session.get(Organisation, project.org_id) if project else None
        if not (plan and project and org):
            return
        try:
            run_plan_analysis(session, org, project, plan)
        except Exception:
            log.exception("Plan-analyse mislukt (plan %s)", plan_id)  # already recorded on the plan


def plan_status(plan: Plan | None) -> dict:
    """{'status', 'message', 'elapsed_s'} for the page and its poller; a stale 'running' counts as failed."""
    if plan is None:
        return {"status": None, "message": None, "elapsed_s": 0}
    elapsed = 0
    if plan.analysis_started_at:
        started = plan.analysis_started_at
        if started.tzinfo is None:  # SQLite hands datetimes back without zone; we store UTC
            started = started.replace(tzinfo=UTC)
        elapsed = int((datetime.now(UTC) - started).total_seconds())
    if plan.analysis_status == "running" and elapsed > ANALYSIS_STALE_AFTER_S:
        return {"status": "failed", "message": "Analyse onderbroken. Upload het plan opnieuw.", "elapsed_s": elapsed}
    return {"status": plan.analysis_status, "message": plan.analysis_message, "elapsed_s": elapsed}


def start_assist(session: Session, org: Organisation, project: Project, instruction: str):
    job = ai_jobs.start(session, org, "assist", f"AI-assist: {instruction[:60]}", link=f"/projects/{project.id}",
                        project_id=project.id)

    def work(session: Session, org: Organisation, job) -> tuple[str, str | None]:
        _, summary = run_assist(session, org, session.get(Project, project.id), instruction)
        return summary, None

    return job, work


def start_skill_override(session: Session, org: Organisation, free_text: str):
    job = ai_jobs.start(session, org, "skill", "Skill-aanvullingen vertalen naar rekenregels", link="/config")

    def work(session: Session, org: Organisation, job) -> tuple[str, str | None]:
        row, unsupported = save_override(session, org, free_text)
        msg = f"Skill-aanvullingen v{row.version}: {len(row.adjustments)} regel{'' if len(row.adjustments) == 1 else 's'}."
        if unsupported:
            msg += " Niet vertaald: " + "; ".join(unsupported)
        return msg, None

    return job, work


def run_assist(
    session: Session, org: Organisation, project: Project, instruction: str
) -> tuple[Geometry, str]:
    skill = load_skill(org.sector)
    geometry = current_geometry(session, project)
    new_geometry, extra, summary = assist_edit(instruction, geometry, skill)
    save_geometry(session, project, new_geometry)
    workflow.log(session, project, f"AI-assist: “{instruction}” — {summary}", kind="info", actor="ai")
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


def preview_quote(
    session: Session, org: Organisation, project: Project, geometry: Geometry | None = None
) -> QuoteResult | None:
    """The meetstaat for the current (or given, unsaved) zones, prices and rules, without saving a quote version."""
    geometry = geometry or current_geometry(session, project)
    if not geometry.zones:
        return None
    adjustments, override_version = current_adjustments(session, org)
    return calculate_quote(
        geometry,
        load_skill(org.sector),
        current_config(session, org),
        scope=project.scope,
        adjustments=adjustments,
        vat_pct=project.vat_pct,
        override_version=override_version,
    )


def wall_areas(org: Organisation, geometry: Geometry) -> dict[str, float]:
    """Net wall area per room (perimeter × height − openings + reveals), as the engine measures it.
    Empty for sectors without wall posts (e.g. tuinaanleg)."""
    skill = load_skill(org.sector)
    if not any(p.get("measure") == "wall_net" for p in skill.posts):
        return {}
    default_height = geometry.default_height_m or float(skill.params.get("default_height_m", 2.6))
    return {
        z.id: wall_net(z, z.height_m or default_height, skill.params).net
        for z in geometry.zones
        if z.kind in skill.zone_kinds and (z.perimeter_m or z.area_m2)
    }


def quote_diff(saved: QuoteResult | None, live: QuoteResult | None) -> dict | None:
    """What changed since the saved quote (zones, prices or rules). None when nothing changed."""
    if not saved or not live:
        return None
    keys = ("area_total_m2", "hours_total", "subtotal_excl", "total_incl")
    delta = {k: round(getattr(live.totals, k) - getattr(saved.totals, k), 2) for k in keys}
    lines_changed = [(ln.zone_id, ln.post_code, ln.net_qty, ln.layers) for ln in saved.lines] != [
        (ln.zone_id, ln.post_code, ln.net_qty, ln.layers) for ln in live.lines
    ]
    if not lines_changed and all(abs(v) < 0.005 for v in delta.values()) and saved.indicative == live.indicative:
        return None
    return {**delta, "indicative_changed": saved.indicative != live.indicative}


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
    session.add(quote)
    session.commit()
    session.refresh(quote)
    workflow.on_quote_calculated(session, project, quote)
    if project.proposed_start:  # refine an existing proposal with the exact hours; it is made when the plan is read
        propose_execution(session, org, project, result.totals.hours_total)
    return quote, result


def latest_quote(session: Session, project: Project) -> Quote | None:
    return session.exec(select(Quote).where(Quote.project_id == project.id).order_by(Quote.id.desc())).first()


def quote_result(quote: Quote) -> QuoteResult:
    return QuoteResult.model_validate_json(quote.result_json)


def offerte_pdf(
    session: Session,
    org: Organisation,
    project: Project,
    quote: Quote,
    intro: str | None = None,
    conditions: str | None = None,
) -> bytes:
    """The quote as PDF in the customer's language (free texts as far as translated; see start_translation)."""
    result = quote_result(quote)
    skill = load_skill(org.sector)
    customer = project_customer(session, project)
    lang = quote_language(org, customer)
    tr = translations_for(quote, lang)
    parties = OffertePartijen(
        company_name=org.company_name or "[JOUW BEDRIJF]",
        company_address=org.address,
        company_vat=org.vat_number,
        company_email=org.email,
        client_name=project.client_name,
        client_address=project.client_address,
        number=f"{quote.number} v{quote.version}",
        title=tr.get(project.name, project.name),
        intro=intro or offer_intro(project, lang, tr),
        conditions=conditions or i18n.t(lang, "voorwaarden_standaard"),
        validity_days=current_config_validity(skill),
        prices_incl_vat=prices_incl_vat(customer),
        client_vat=customer.vat_number if customer else None,
        language=lang,
        translations=tr,
    )
    return build_offerte_pdf(result, parties)


def offer_intro(project: Project, lang: str, tr: dict[str, str]) -> str:
    title = tr.get(project.name, project.name)
    intro = i18n.t(lang, "intro", name=project.client_name or i18n.t(lang, "klant_aanspreking"), title=title[:1].lower() + title[1:])
    proposal = planning.proposal_sentence(project, {}, lang)
    return intro + (f" {proposal}" if proposal else "")


# ---------- language of the quote ----------


def quote_language(org: Organisation, customer: Customer | None) -> str:
    """The customer's language; without a customer the company's own."""
    return i18n.lang_or_default(customer.language if customer else org.language)


def translations_for(quote: Quote, lang: str) -> dict[str, str]:
    return {} if lang == i18n.SOURCE_LANG else quote.translations.get(lang, {})


def quote_free_texts(project: Project, quote: Quote) -> list[str]:
    """What the user or the AI typed and the customer reads: title, rooms, posts, assumptions."""
    result = quote_result(quote)
    texts = [project.name]
    for line in result.lines:
        texts += [line.zone_name, line.post_label]
    texts += [a.text for a in result.assumptions]
    return list(dict.fromkeys(t for t in texts if t and t.strip()))


def missing_translations(project: Project, quote: Quote, lang: str) -> list[str]:
    if lang == i18n.SOURCE_LANG:
        return []
    done = translations_for(quote, lang)
    return [t for t in quote_free_texts(project, quote) if t not in done]


def start_translation(session: Session, org: Organisation, project: Project, quote: Quote | None):
    """Background job that translates the quote's free texts into the customer's language, once per version.
    Returns (job, work) for ai_jobs.run, or None when nothing needs translating (Dutch, done, running, no AI)."""
    if quote is None or not get_settings().ai_enabled:
        return None
    lang = quote_language(org, project_customer(session, project))
    if not missing_translations(project, quote, lang) or ai_jobs.running(session, org, project_id=project.id, kind="vertaling"):
        return None
    label = i18n.LANGUAGES[lang].lower()
    job = ai_jobs.start(session, org, "vertaling", f"Offerte {quote.number} v{quote.version} naar het {label}",
                        link=f"/projects/{project.id}/offerte", project_id=project.id, ref_id=quote.id)

    def work(session: Session, org: Organisation, job) -> tuple[str, str | None]:
        q, p = session.get(Quote, quote.id), session.get(Project, project.id)
        texts = missing_translations(p, q, lang)
        got = translate_texts(texts, lang)
        data = q.translations
        data.setdefault(lang, {}).update(got)
        q.translations_json = json.dumps(data, ensure_ascii=False)
        session.add(q)
        session.commit()
        return f"Offerte vertaald naar het {label} ({len(got)} van {len(texts)} teksten).", f"/projects/{p.id}/offerte"

    return job, work


def current_config_validity(skill) -> int:
    return int(skill.default_prices.get("validity_days", 30))


def meetstaat_xlsx(project: Project, quote: Quote) -> bytes:
    return build_meetstaat_xlsx(quote_result(quote), f"Meetstaat — {project.name}")


def plan_svg(session: Session, project: Project, quote: Quote | None = None, width_px: int = 800) -> str:
    geometry = current_geometry(session, project)
    return build_plan_svg(geometry, quote_result(quote) if quote else None, width_px=width_px)


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
