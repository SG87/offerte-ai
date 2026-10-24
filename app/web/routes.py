"""Web routes. Session cookie holds the organisation id (single-account v1; Google/OAuth is a TODO)."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from app import services
from app.ai.client import AIUnavailable
from app.config import get_settings
from app.db import get_session
from app.engine import load_skill
from app.models import Organisation
from app.schemas import PriceConfig, PriceItem, Zone

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["eur"] = lambda v: f"€ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
templates.env.filters["qty"] = lambda v: f"{v:g}".replace(".", ",")


def current_org(request: Request, session: Session = Depends(get_session)) -> Organisation | None:
    org_id = request.session.get("org_id")
    return session.get(Organisation, org_id) if org_id else None


def _render(request: Request, name: str, **ctx):
    ctx.setdefault("org", None)
    ctx["ai_enabled"] = get_settings().ai_enabled
    ctx["flash"] = request.session.pop("flash", None)
    return templates.TemplateResponse(request, name, ctx)


def _flash(request: Request, message: str) -> None:
    request.session["flash"] = message


def _require(request: Request, org: Organisation | None):
    if org is None:
        return RedirectResponse("/", status_code=303)
    if not org.onboarding_done and not request.url.path.startswith("/onboarding"):
        return RedirectResponse("/onboarding", status_code=303)
    return None


# ---------- account ----------


@router.get("/", response_class=HTMLResponse)
def home(request: Request, org: Organisation | None = Depends(current_org)):
    if org:
        return RedirectResponse("/projects" if org.onboarding_done else "/onboarding", status_code=303)
    return _render(request, "login.html")


@router.post("/login")
def login(request: Request, email: str = Form(...), session: Session = Depends(get_session)):
    org, lookup, created = services.signup(session, email)
    request.session["org_id"] = org.id
    if created and lookup and lookup.website:
        _flash(
            request,
            f"We herkenden {lookup.company_name or lookup.domain} via {lookup.website} — controleer de gegevens.",
        )
    return RedirectResponse("/onboarding" if not org.onboarding_done else "/projects", status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@router.get("/onboarding", response_class=HTMLResponse)
def onboarding(request: Request, org: Organisation | None = Depends(current_org)):
    if org is None:
        return RedirectResponse("/", status_code=303)
    return _render(request, "onboarding.html", org=org, sectors=services.available_sector_list())


@router.post("/onboarding")
def onboarding_post(
    request: Request,
    sector: str = Form(...),
    company_name: str | None = Form(None),
    address: str | None = Form(None),
    vat_number: str | None = Form(None),
    phone: str | None = Form(None),
    sector_request: str | None = Form(None),
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    if org is None:
        return RedirectResponse("/", status_code=303)
    if sector == "andere":
        if sector_request:
            services.request_sector(session, org.email, sector_request, org.id)
            _flash(request, "Sectoraanvraag ontvangen. Je werkt voorlopig met de generieke skill.")
        sector = "generiek"
    services.complete_onboarding(
        session, org, sector, company_name=company_name, address=address, vat_number=vat_number, phone=phone
    )
    return RedirectResponse("/config", status_code=303)


# ---------- configuratie ----------


@router.get("/config", response_class=HTMLResponse)
def config_page(
    request: Request, org: Organisation | None = Depends(current_org), session: Session = Depends(get_session)
):
    if r := _require(request, org):
        return r
    cfg = services.current_config(session, org)
    override = services.current_override(session, org)
    skill = load_skill(org.sector)
    return _render(
        request,
        "config.html",
        org=org,
        cfg=cfg,
        override=override,
        skill=skill,
        skill_rules=(skill.path / "rules.yaml").read_text(encoding="utf-8") if skill.path else "",
    )


@router.post("/config")
async def config_post(
    request: Request, org: Organisation | None = Depends(current_org), session: Session = Depends(get_session)
):
    if r := _require(request, org):
        return r
    form = await request.form()
    cfg = services.current_config(session, org)
    items = []
    for item in cfg.items:
        code = item.code
        items.append(
            PriceItem(
                code=code,
                label=form.get(f"label_{code}", item.label),
                unit=item.unit,
                material_price=_f(form.get(f"price_{code}"), item.material_price),
                material_unit=item.material_unit,
                coverage=_f(form.get(f"coverage_{code}"), item.coverage) if item.coverage else None,
                hours_per_unit=_f(form.get(f"hours_{code}"), item.hours_per_unit),
                hours_scale_with_layers=item.hours_scale_with_layers,
            )
        )
    new_cfg = PriceConfig(
        sector=org.sector,
        hourly_rate=_f(form.get("hourly_rate"), cfg.hourly_rate),
        margin_pct=_f(form.get("margin_pct"), cfg.margin_pct),
        waste_pct=_f(form.get("waste_pct"), cfg.waste_pct),
        vat_pct=_f(form.get("vat_pct"), cfg.vat_pct),
        vat_label=form.get("vat_label", cfg.vat_label),
        validity_days=int(_f(form.get("validity_days"), cfg.validity_days)),
        round_hours_to=cfg.round_hours_to,
        items=items,
    )
    row = services.save_config(session, org, new_cfg)
    msg = f"Prijslijst bewaard als v{row.version}."
    free_text = (form.get("override_text") or "").strip()
    prev = services.current_override(session, org)
    if free_text != (prev.free_text if prev else ""):
        orow, unsupported = services.save_override(session, org, free_text)
        msg += f" Skill-aanvullingen v{orow.version} ({orow.translated_by}): {len(orow.adjustments)} regels."
        if unsupported:
            msg += " Niet vertaald: " + "; ".join(unsupported)
    _flash(request, msg)
    return RedirectResponse("/config", status_code=303)


def _f(value, default):
    try:
        return float(str(value).replace(",", ".")) if value not in (None, "") else default
    except ValueError:
        return default


# ---------- projecten ----------


@router.get("/projects", response_class=HTMLResponse)
def projects(
    request: Request, org: Organisation | None = Depends(current_org), session: Session = Depends(get_session)
):
    if r := _require(request, org):
        return r
    return _render(
        request,
        "projects.html",
        org=org,
        projects=services.list_projects(session, org),
        work_types=services.WORK_TYPES,
        scopes=services.SCOPES_BY_SECTOR.get(org.sector, []),
    )


@router.post("/projects")
async def projects_post(
    request: Request, org: Organisation | None = Depends(current_org), session: Session = Depends(get_session)
):
    if r := _require(request, org):
        return r
    form = await request.form()
    scope = form.getlist("scope") or None
    p = services.create_project(
        session,
        org,
        form.get("name") or "Nieuw project",
        client_name=form.get("client_name") or None,
        client_address=form.get("client_address") or None,
        work_type=form.get("work_type") or "renovatie_woning_10j",
        scope=scope,
        notes=form.get("notes") or None,
    )
    return RedirectResponse(f"/projects/{p.id}", status_code=303)


@router.get("/projects/{project_id}", response_class=HTMLResponse)
def project_page(
    project_id: int,
    request: Request,
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    if r := _require(request, org):
        return r
    p = services.get_project(session, org, project_id)
    if not p:
        return RedirectResponse("/projects", status_code=303)
    geometry = services.current_geometry(session, p)
    quote = services.latest_quote(session, p)
    result = services.quote_result(quote) if quote else None
    skill = load_skill(org.sector)
    return _render(
        request,
        "project.html",
        org=org,
        project=p,
        geometry=geometry,
        quote=quote,
        result=result,
        skill=skill,
        plan_svg=services.plan_svg(session, p, quote),
        format_openings=services.format_openings,
        scopes=services.SCOPES_BY_SECTOR.get(org.sector, []),
        plan=services.latest_plan(session, p),
    )


@router.post("/projects/{project_id}/plan")
async def project_plan(
    project_id: int,
    request: Request,
    file: UploadFile,
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    if r := _require(request, org):
        return r
    p = services.get_project(session, org, project_id)
    data = await file.read()
    if not data:
        _flash(request, "Geen bestand ontvangen.")
        return RedirectResponse(f"/projects/{project_id}", status_code=303)
    try:
        _, message = services.analyze_upload(
            session, org, p, data, file.filename or "plan", file.content_type
        )
    except Exception as exc:  # the vision call can fail on odd files; keep the page usable
        message = (
            f"Plan opgeslagen, analyse mislukt ({type(exc).__name__}: {exc}). Voeg de zones manueel toe."
        )
    _flash(request, message)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/geometry")
async def project_geometry(
    project_id: int,
    request: Request,
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    """Manual zone editor: rows zone_<i>_<field>, plus 'confirm_<key>' checkboxes for assumptions."""
    if r := _require(request, org):
        return r
    p = services.get_project(session, org, project_id)
    form = await request.form()
    geometry = services.current_geometry(session, p)
    zones: list[Zone] = []
    i = 0
    while f"zone_{i}_name" in form:
        name = (form.get(f"zone_{i}_name") or "").strip()
        if name and not form.get(f"zone_{i}_delete"):
            zones.append(
                Zone(
                    id=form.get(f"zone_{i}_id") or f"z{i}",
                    name=name,
                    kind=form.get(f"zone_{i}_kind") or "ruimte",
                    area_m2=_opt(form.get(f"zone_{i}_area")),
                    perimeter_m=_opt(form.get(f"zone_{i}_perimeter")),
                    length_m=_opt(form.get(f"zone_{i}_length")),
                    height_m=_opt(form.get(f"zone_{i}_height")),
                    count=int(_opt(form.get(f"zone_{i}_count")) or 0) or None,
                    openings=services.parse_openings(form.get(f"zone_{i}_openings") or ""),
                    tags=[t.strip() for t in (form.get(f"zone_{i}_tags") or "").split(",") if t.strip()],
                    in_scope=bool(form.get(f"zone_{i}_in_scope")),
                    polygon=geometry.zone(form.get(f"zone_{i}_id") or "").polygon
                    if geometry.zone(form.get(f"zone_{i}_id") or "")
                    else None,
                )
            )
        i += 1
    geometry.zones = zones
    geometry.scale_confirmed = bool(form.get("scale_confirmed"))
    geometry.scale_note = form.get("scale_note") or geometry.scale_note
    geometry.default_height_m = _opt(form.get("default_height_m"))
    for a in geometry.assumptions:
        a.confirmed = bool(form.get(f"confirm_{a.key}"))
    services.save_geometry(session, p, geometry)
    _flash(request, f"Geometrie bewaard: {len(zones)} zones.")
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


def _opt(value) -> float | None:
    try:
        return float(str(value).replace(",", ".")) if value not in (None, "") else None
    except ValueError:
        return None


@router.post("/projects/{project_id}/assist")
def project_assist(
    project_id: int,
    request: Request,
    instruction: str = Form(...),
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    if r := _require(request, org):
        return r
    p = services.get_project(session, org, project_id)
    try:
        _, summary = services.run_assist(session, org, p, instruction)
        _flash(request, "AI-assist: " + summary)
    except AIUnavailable as exc:
        _flash(request, str(exc))
    except Exception as exc:
        _flash(request, f"AI-assist mislukt: {type(exc).__name__}: {exc}")
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/quote")
def project_quote(
    project_id: int,
    request: Request,
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    if r := _require(request, org):
        return r
    p = services.get_project(session, org, project_id)
    quote, result = services.run_quote(session, org, p)
    _flash(
        request,
        f"Offerte {quote.number} v{quote.version} aangemaakt ({'indicatief' if result.indicative else 'definitief'}).",
    )
    return RedirectResponse(f"/projects/{project_id}/offerte", status_code=303)


@router.get("/projects/{project_id}/offerte", response_class=HTMLResponse)
def offerte_page(
    project_id: int,
    request: Request,
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    if r := _require(request, org):
        return r
    p = services.get_project(session, org, project_id)
    quote = services.latest_quote(session, p)
    if not quote:
        return RedirectResponse(f"/projects/{project_id}", status_code=303)
    return _render(
        request, "offerte.html", org=org, project=p, quote=quote, result=services.quote_result(quote)
    )


@router.get("/projects/{project_id}/offerte.pdf")
def offerte_pdf(
    project_id: int,
    request: Request,
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    if r := _require(request, org):
        return r
    p = services.get_project(session, org, project_id)
    quote = services.latest_quote(session, p)
    if not quote:
        return RedirectResponse(f"/projects/{project_id}", status_code=303)
    return Response(
        services.offerte_pdf(org, p, quote),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="offerte-{quote.number}.pdf"'},
    )


@router.get("/projects/{project_id}/meetstaat.xlsx")
def meetstaat_xlsx(
    project_id: int,
    request: Request,
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    if r := _require(request, org):
        return r
    p = services.get_project(session, org, project_id)
    quote = services.latest_quote(session, p)
    if not quote:
        return RedirectResponse(f"/projects/{project_id}", status_code=303)
    return Response(
        services.meetstaat_xlsx(p, quote),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="meetstaat-{quote.number}.xlsx"'},
    )


@router.get("/projects/{project_id}/plan.svg")
def plan_svg(
    project_id: int,
    request: Request,
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    if r := _require(request, org):
        return r
    p = services.get_project(session, org, project_id)
    return Response(
        services.plan_svg(session, p, services.latest_quote(session, p)), media_type="image/svg+xml"
    )


@router.get("/projects/{project_id}/plan-image")
def plan_image(
    project_id: int,
    request: Request,
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    """The uploaded photo/scan itself (shown next to the schematic plan in the werkruimte)."""
    if r := _require(request, org):
        return r
    p = services.get_project(session, org, project_id)
    plan = services.latest_plan(session, p) if p else None
    if not plan or not plan.path:
        return Response(status_code=404)
    path = Path(plan.path).resolve()
    if not path.is_file() or get_settings().uploads_dir.resolve() not in path.parents:
        return Response(status_code=404)
    return FileResponse(path, media_type=plan.media_type or "application/octet-stream")


@router.get("/projects/{project_id}/geometry.json")
def geometry_json(
    project_id: int,
    request: Request,
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    if r := _require(request, org):
        return r
    p = services.get_project(session, org, project_id)
    return Response(
        json.dumps(services.current_geometry(session, p).model_dump(), ensure_ascii=False, indent=2),
        media_type="application/json",
    )
