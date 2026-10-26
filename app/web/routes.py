"""Web routes. Session cookie holds the organisation id (single-account v1; Google/OAuth is a TODO)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app import address, ai_jobs, branding, i18n, invoicing, services, todos, workflow
from app import planning as planning_mod
from app.config import get_settings
from app.db import get_session
from app.engine import load_skill
from app.engine.skills import tag_columns, tag_value
from app.integrations import stock as stock_mod
from app.models import Customer, Organisation, OutboxMessage
from app.outputs.offer_lines import offer_lines, offer_totals
from app.schemas import Geometry, PriceConfig, PriceItem, Zone

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["eur"] = lambda v: f"€ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
templates.env.filters["qty"] = lambda v: f"{v:g}".replace(".", ",")
STATIC_DIR = Path(__file__).parent / "static"
# cache-busting: the stylesheet URL changes whenever the file does
templates.env.globals["countries"] = address.COUNTRIES
templates.env.globals["asset_v"] = lambda name: int((STATIC_DIR / name).stat().st_mtime)


def current_org(request: Request, session: Session = Depends(get_session)) -> Organisation | None:
    org_id = request.session.get("org_id")
    return session.get(Organisation, org_id) if org_id else None


def _render(request: Request, name: str, **ctx):
    ctx.setdefault("org", None)
    ctx["theme"] = branding.theme_vars(ctx["org"].brand_primary) if ctx["org"] else {}
    ctx["ai_popup"] = request.session.pop("ai_popup", None)
    if ctx["org"]:
        from app.db import session_scope

        with session_scope() as s:
            ctx["ai_state"] = ai_jobs.snapshot(s, ctx["org"])
            if ctx["org"].onboarding_done:
                ctx["todo_open"] = todos.count_open(s, ctx["org"])
    ctx["ai_enabled"] = get_settings().ai_enabled
    ctx["flash"] = request.session.pop("flash", None)
    return templates.TemplateResponse(request, name, ctx)


def _flash(request: Request, message: str) -> None:
    request.session["flash"] = message


def _ai_started(request: Request, title: str) -> None:
    """Popup on the next page: the AI is working, the user can go on elsewhere."""
    request.session["ai_popup"] = title


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
    """Existing accounts only; new companies go through /signup (which runs the website lookup)."""
    org = services.get_org_by_email(session, email)
    if org is None:
        return _render(
            request,
            "login.html",
            email=email,
            error="We vonden geen account voor dit e-mailadres. Kies \u201cMaak nieuw account aan\u201d.",
        )
    request.session["org_id"] = org.id
    return RedirectResponse("/projects" if org.onboarding_done else "/onboarding", status_code=303)


@router.post("/signup")
def signup(
    request: Request, background: BackgroundTasks, email: str = Form(...), session: Session = Depends(get_session)
):
    """Create the account at once; the website lookup (company data, logo, colours) runs as a background job."""
    org, _, created = services.signup(session, email, do_lookup=False)
    request.session["org_id"] = org.id
    if not created:
        _flash(request, "Er bestond al een account voor dit e-mailadres; je bent aangemeld.")
    else:
        job, work = services.start_company_lookup(session, org)
        background.add_task(ai_jobs.run, job.id, work)
        _ai_started(request, "We zoeken je bedrijf op via je website")
    return RedirectResponse("/onboarding" if not org.onboarding_done else "/projects", status_code=303)


@router.get("/ai/jobs")
def ai_jobs_state(org: Organisation | None = Depends(current_org), session: Session = Depends(get_session)):
    if org is None:
        return JSONResponse({"running": [], "finished": []}, status_code=401)
    return ai_jobs.snapshot(session, org)


@router.post("/ai/jobs/seen")
async def ai_jobs_seen(
    request: Request, org: Organisation | None = Depends(current_org), session: Session = Depends(get_session)
):
    if org is None:
        return JSONResponse({}, status_code=401)
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    ai_jobs.mark_seen(session, org, body.get("id"))
    return {"ok": True}


@router.get("/branding/logo")
def branding_logo(org: Organisation | None = Depends(current_org)):
    path = branding.logos_dir() / org.logo_file if org and org.logo_file else None
    if path is None or not path.is_file():
        return Response(status_code=404)
    return FileResponse(
        path,
        headers={  # logos come from third-party sites: never let an SVG run script, even when opened directly
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-cache",  # same URL after a new upload
        },
    )


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@router.get("/onboarding", response_class=HTMLResponse)
def onboarding(
    request: Request, org: Organisation | None = Depends(current_org), session: Session = Depends(get_session)
):
    if org is None:
        return RedirectResponse("/", status_code=303)
    return _render(
        request,
        "onboarding.html",
        org=org,
        sectors=services.available_sector_list(),
        lookup_job=next(iter(ai_jobs.running(session, org, kind="bedrijf")), None),
    )


@router.post("/onboarding")
async def onboarding_post(
    request: Request,
    sector: str = Form(...),
    company_name: str | None = Form(None),
    address: str | None = Form(None),
    vat_number: str | None = Form(None),
    phone: str | None = Form(None),
    sector_request: str | None = Form(None),
    brand_primary: str | None = Form(None),
    brand_secondary: str | None = Form(None),
    logo: UploadFile | None = File(None),
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    if org is None:
        return RedirectResponse("/", status_code=303)
    if logo is not None and logo.filename:
        if not services.set_logo(session, org, await logo.read(), logo.content_type):
            _flash(request, "Dat logo kon niet gebruikt worden (svg, png, jpg of webp, max. 2 MB).")
    if sector == "andere":
        if sector_request:
            services.request_sector(session, org.email, sector_request, org.id)
            _flash(request, "Sectoraanvraag ontvangen. Je werkt voorlopig met de generieke skill.")
        sector = "generiek"
    services.complete_onboarding(
        session,
        org,
        sector,
        company_name=company_name,
        address=address,
        vat_number=vat_number,
        phone=phone,
        brand_primary=branding.normalize_hex(brand_primary),
        brand_secondary=branding.normalize_hex(brand_secondary),
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
        skill_job=next(iter(ai_jobs.running(session, org, kind="skill")), None),
        stock=stock_mod.stock_levels(session, org),
        tracked=stock_mod.tracked_codes(cfg, skill.default_price_config()),
    )


@router.post("/config")
async def config_post(
    request: Request,
    background: BackgroundTasks,
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
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
                stock=form.get(f"track_{code}") == "on",
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
        km_rate=max(0.0, _f(form.get("km_rate"), cfg.km_rate)),
        km_hours_per_trip=max(0.0, _f(form.get("km_hours_per_trip"), cfg.km_hours_per_trip)),
        items=items,
    )
    row = services.save_config(session, org, new_cfg)
    msg = f"Prijslijst bewaard als v{row.version}."
    org.purchase_contact_email = (form.get("purchase_contact_email") or "").strip() or None
    session.add(org)
    session.commit()
    levels = stock_mod.stock_levels(session, org)
    for item in new_cfg.items:
        raw = form.get(f"stock_{item.code}")
        if raw is None or not item.stock:
            continue
        if str(raw).strip() == "":  # cleared: stock unknown again
            if item.code in levels:
                session.delete(levels[item.code])
                session.commit()
        elif (qty := _f(raw, None)) is not None:
            stock_mod.set_stock(session, org, item.code, qty, item.material_unit or item.unit)
    free_text = (form.get("override_text") or "").strip()
    prev = services.current_override(session, org)
    if free_text != (prev.free_text if prev else ""):
        if get_settings().ai_enabled:
            job, work = services.start_skill_override(session, org, free_text)
            background.add_task(ai_jobs.run, job.id, work)
            _ai_started(request, "Claude vertaalt je skill-aanvullingen naar rekenregels")
        else:  # the heuristic translation is instant
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
    request: Request,
    nieuw: int = 0,
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    if r := _require(request, org):
        return r
    board = services.projects_overview(session, org)
    return _render(
        request,
        "projects.html",
        org=org,
        board=board,
        phases=workflow.PHASES,
        customers=_active_customers(session, org),
        customer_kinds=services.CUSTOMER_KINDS,
        languages=i18n.LANGUAGES,
        default_work_type=services.DEFAULT_WORK_TYPE,
        work_types=services.WORK_TYPES,
        scopes=services.SCOPES_BY_SECTOR.get(org.sector, []),
        open_form=bool(nieuw) or not board,
    )


def _active_customers(session: Session, org: Organisation) -> list[Customer]:
    return list(
        session.exec(
            select(Customer).where(Customer.org_id == org.id, Customer.active == True).order_by(Customer.name)  # noqa: E712
        ).all()
    )


def _customer_from_form(session: Session, org: Organisation, form) -> Customer:
    """The chosen customer, or a new one from the 'nieuwe klant' fields. Raises CustomerRequired."""
    choice = form.get("customer_id") or ""
    if choice == "new":
        return services.resolve_customer(
            session,
            org,
            name=form.get("new_name"),
            kind=form.get("new_kind") or "b2c",
            **{k: form.get(f"new_{k}") for k in address.PARTS},
            vat_number=form.get("new_vat_number"),
            email=form.get("new_email"),
            language=form.get("new_language"),
        )
    return services.resolve_customer(session, org, customer_id=int(choice) if choice.isdigit() else None)


@router.post("/projects")
async def projects_post(
    request: Request, org: Organisation | None = Depends(current_org), session: Session = Depends(get_session)
):
    if r := _require(request, org):
        return r
    form = await request.form()
    try:
        customer = _customer_from_form(session, org, form)
    except services.CustomerRequired as exc:
        _flash(request, str(exc))
        return RedirectResponse("/projects?nieuw=1", status_code=303)
    p = services.create_project(
        session,
        org,
        form.get("name") or "Nieuw project",
        customer,
        client_address=form.get("client_address") or None,
        work_type=form.get("work_type") or None,
        scope=form.getlist("scope") or None,
        notes=form.get("notes") or None,
    )
    return RedirectResponse(f"/projects/{p.id}", status_code=303)


@router.post("/projects/{project_id}/verwijder")
def project_delete(
    project_id: int,
    request: Request,
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    if r := _require(request, org):
        return r
    p = services.get_project(session, org, project_id)
    if p:
        _flash(request, f"Project \u201c{services.delete_project(session, org, p)}\u201d verwijderd.")
    return RedirectResponse("/projects", status_code=303)


@router.post("/projects/{project_id}/klant")
async def project_customer_post(
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
    try:
        customer = _customer_from_form(session, org, await request.form())
    except services.CustomerRequired as exc:
        _flash(request, str(exc))
        return RedirectResponse(f"/projects/{project_id}", status_code=303)
    services.set_project_customer(session, p, customer)
    incl = services.prices_incl_vat(customer)
    _flash(request, f"Klant: {customer.name}. De offerte toont prijzen {'incl.' if incl else 'excl.'} btw.")
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


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
    saved = services.quote_result(quote) if quote else None
    result = services.preview_quote(session, org, p)  # live: always the current zones, prices and rules
    skill = load_skill(org.sector)
    plan = services.latest_plan(session, p)
    customer = services.project_customer(session, p)
    return _render(
        request,
        "project.html",
        org=org,
        project=p,
        geometry=geometry,
        quote=quote,
        result=result,
        saved=saved,
        diff=services.quote_diff(saved, result),
        skill=skill,
        plan_svg=services.plan_svg(session, p, quote, width_px=480),  # drawn at pane width: legible labels
        format_openings=services.format_openings,
        scopes=services.SCOPES_BY_SECTOR.get(org.sector, []),
        plan=plan,
        analysis=services.plan_status(plan),
        wf=workflow,
        status=workflow.LEGACY.get(p.status, p.status),
        history=workflow.history(session, p),
        mails={m.id: m for m in session.exec(select(OutboxMessage).where(OutboxMessage.project_id == p.id)).all()},
        plan_items=planning_mod.items_for_project(session, p),
        plan_who=planning_mod.assignees(session, planning_mod.items_for_project(session, p)),
        emps={e.id: e for e in planning_mod.employees(session, org, active_only=False)},
        color_of=planning_mod.employee_color,
        reservations=stock_mod.active_reservations(session, org, p.id),
        open_todos=[t for t in todos.list_todos(session, org, "open") if t.project_id == p.id],
        active_emps=planning_mod.employees(session, org),
        assist_job=next(iter(ai_jobs.running(session, org, project_id=p.id, kind="assist")), None),
        tag_cols=tag_columns(skill, geometry.zones),
        walls=services.wall_areas(org, geometry),
        show_walls=any(post.get("measure") == "wall_net" for post in skill.posts),
        tag_value=tag_value,
        proposal_conflicts=planning_mod.proposal_conflicts(session, org, p),
        proposal_sentence=planning_mod.proposal_sentence(p, {}),
        today=date.today(),
        invoice=invoicing.for_project(session, p),
        customer=customer,
        incl_vat=services.prices_incl_vat(customer),
        customers=_active_customers(session, org),
        km_rate=services.current_config(session, org).km_rate,
    )


@router.post("/projects/{project_id}/afstand")
async def project_distance_post(
    project_id: int,
    request: Request,
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    """Distance to the work site for the kilometre allowance: typed by hand, or looked up again."""
    if r := _require(request, org):
        return r
    p = services.get_project(session, org, project_id)
    if not p:
        return RedirectResponse("/projects", status_code=303)
    form = await request.form()
    km = None if form.get("opnieuw") else _f(form.get("distance_km"), None)
    services.set_distance(session, org, p, km)
    if p.distance_km is not None:
        km_text = f"{p.distance_km:g}".replace(".", ",")
        _flash(request, f"Afstand tot de werf: {km_text} km ({p.distance_note}). Herbereken de offerte om ze mee te nemen.")
    else:
        _flash(request, p.distance_note or "Afstand niet gevonden; vul ze zelf in.")
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/plan")
async def project_plan(
    project_id: int,
    request: Request,
    file: UploadFile,
    background: BackgroundTasks,
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    """Store the plan and return at once; the analysis runs after the response and the page polls its status."""
    if r := _require(request, org):
        return r
    p = services.get_project(session, org, project_id)
    data = await file.read()
    if not data:
        _flash(request, "Geen bestand ontvangen.")
        return RedirectResponse(f"/projects/{project_id}", status_code=303)
    plan = services.store_plan_upload(session, org, p, data, file.filename or "plan", file.content_type)
    if plan.analysis_status == "running":
        background.add_task(services.analyze_plan_in_background, plan.id)
        _ai_started(request, f"Claude leest je plan ({file.filename or 'plan'})")
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.get("/projects/{project_id}/plan-status")
def project_plan_status(
    project_id: int,
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    p = services.get_project(session, org, project_id) if org else None
    if not p:
        return JSONResponse({"status": None}, status_code=404)
    return services.plan_status(services.latest_plan(session, p))


def _geometry_from_form(form, geometry: Geometry) -> tuple[Geometry, list[str]]:
    """The zone editor's fields on top of the stored geometry: rows zone_<i>_<field>, tag columns
    tagcol_<j>_key/type with cells zone_<i>_tag_<j>, and 'confirm_<key>' checkboxes for assumptions.
    Returns (geometry, names of zones marked for deletion)."""
    tag_cols = []  # (column index, key, type)
    j = 0
    while f"tagcol_{j}_key" in form:
        key = (form.get(f"tagcol_{j}_key") or "").replace("=", "").strip()
        if key:
            tag_cols.append((j, key, form.get(f"tagcol_{j}_type") or "bool"))
        j += 1
    zones: list[Zone] = []
    deleted: list[str] = []
    i = 0
    while f"zone_{i}_name" in form:
        name = (form.get(f"zone_{i}_name") or "").strip()
        if name and form.get(f"zone_{i}_delete"):
            deleted.append(name)
        elif name:
            tags = [t.strip() for t in (form.get(f"zone_{i}_tags") or "").split(",") if t.strip()]  # API-style field
            for j, key, ttype in tag_cols:
                raw = (form.get(f"zone_{i}_tag_{j}") or "").strip()
                tag = (key if raw else None) if ttype == "bool" else (f"{key}={raw}" if raw else None)
                if tag and tag not in tags:
                    tags.append(tag)
            old = geometry.zone(form.get(f"zone_{i}_id") or "")
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
                    tags=tags,
                    in_scope=bool(form.get(f"zone_{i}_in_scope")),
                    polygon=old.polygon if old else None,
                )
            )
        i += 1
    geometry = geometry.model_copy(deep=True)
    geometry.zones = zones
    geometry.scale_confirmed = bool(form.get("scale_confirmed"))
    geometry.scale_note = form.get("scale_note") or geometry.scale_note
    geometry.default_height_m = _opt(form.get("default_height_m"))
    for a in geometry.assumptions:
        a.confirmed = bool(form.get(f"confirm_{a.key}"))
    return geometry, deleted


@router.post("/projects/{project_id}/geometry")
async def project_geometry(
    project_id: int,
    request: Request,
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    """Manual zone editor (see _geometry_from_form)."""
    if r := _require(request, org):
        return r
    p = services.get_project(session, org, project_id)
    geometry, deleted = _geometry_from_form(await request.form(), services.current_geometry(session, p))
    zones = geometry.zones
    services.save_geometry(session, p, geometry)
    gone = f" Verwijderd: {', '.join(deleted)}." if deleted else ""
    _flash(request, f"Geometrie bewaard: {len(zones)} zones.{gone}")
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/geometry/preview")
async def project_geometry_preview(
    project_id: int,
    request: Request,
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    """Live figures for the zone editor while typing: wall area per row and the meetstaat totals. Saves nothing."""
    p = services.get_project(session, org, project_id) if org else None
    if not p:
        return JSONResponse({}, status_code=404)
    geometry, _ = _geometry_from_form(await request.form(), services.current_geometry(session, p))
    walls = services.wall_areas(org, geometry)
    result = services.preview_quote(session, org, p, geometry)
    incl = services.prices_incl_vat(services.project_customer(session, p))
    t = result.totals if result else None
    return {
        "walls": {z.id: walls.get(z.id) for z in geometry.zones},
        "totals": {
            "area_total_m2": t.area_total_m2,
            "hours_total": t.hours_total,
            "subtotal_excl": t.subtotal_excl,
            "total_incl": t.total_incl,
            "headline": t.total_incl if incl else t.subtotal_excl,
        }
        if t
        else None,
    }


def _opt(value) -> float | None:
    try:
        return float(str(value).replace(",", ".")) if value not in (None, "") else None
    except ValueError:
        return None


@router.post("/projects/{project_id}/assist")
def project_assist(
    project_id: int,
    request: Request,
    background: BackgroundTasks,
    instruction: str = Form(...),
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    if r := _require(request, org):
        return r
    p = services.get_project(session, org, project_id)
    if not p:
        return RedirectResponse("/projects", status_code=303)
    if not get_settings().ai_enabled:
        _flash(request, "AI staat uit (geen ANTHROPIC_API_KEY).")
    elif ai_jobs.running(session, org, project_id=p.id):
        _flash(request, "De AI is nog bezig met dit project; wacht tot die klaar is.")
    else:
        job, work = services.start_assist(session, org, p, instruction)
        background.add_task(ai_jobs.run, job.id, work)
        _ai_started(request, "AI-assist past je zones aan")
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/voorstel")
async def project_proposal(
    project_id: int,
    request: Request,
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    """Save (or recompute) the proposed crew and execution dates."""
    if r := _require(request, org):
        return r
    p = services.get_project(session, org, project_id)
    if not p:
        return RedirectResponse("/projects", status_code=303)
    form = await request.form()
    if form.get("opnieuw"):
        p.proposal_locked = False
        session.add(p)
        session.commit()
        quote = services.latest_quote(session, p)
        services.propose_execution(session, org, p, services.quote_result(quote).totals.hours_total if quote else None)
        _flash(request, "Nieuw voorstel berekend.")
        return RedirectResponse(f"/projects/{project_id}#voorstel", status_code=303)
    try:
        start = date.fromisoformat(form.get("proposed_start") or "")
    except ValueError:
        start = None
    try:
        end = date.fromisoformat(form.get("proposed_end") or "") if form.get("proposed_end") else start
    except ValueError:
        end = start
    if start and end and end < start:
        start, end = end, start
    crew = [int(v) for v in form.getlist("crew") if str(v).isdigit()]
    p.proposed_start, p.proposed_end = start, end
    p.proposed_crew_json = json.dumps(crew)
    p.proposal_note = (form.get("proposal_note") or "").strip() or None
    p.proposal_locked = True
    session.add(p)
    session.commit()
    workflow.log(session, p, "Voorstel uitvoering aangepast" + (f": {start:%d/%m}–{end:%d/%m}" if start else ""), kind="info", actor="gebruiker")
    _flash(request, "Voorstel bewaard. Het staat op de offerte.")
    return RedirectResponse(f"/projects/{project_id}#voorstel", status_code=303)


@router.post("/outbox/{msg_id}/verstuur")
def outbox_send(
    request: Request,
    msg_id: int,
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    """(Re)send a mail that waited in the outbox or failed, e.g. after the mail server was set up."""
    from app.integrations import mail

    if r := _require(request, org):
        return r
    msg = session.get(OutboxMessage, msg_id)
    back = f"/projects/{msg.project_id}#historiek" if msg and msg.project_id else "/projects"
    if not msg or msg.org_id != org.id:
        return RedirectResponse("/projects", status_code=303)
    if not get_settings().smtp_ready:
        _flash(request, "Er is nog geen mailserver klaar (OFFERTE_SMTP_HOST en het wachtwoord in .env).")
    elif msg.status == "verzonden":
        _flash(request, "Deze mail is al verzonden.")
    else:
        mail.deliver(session, org, msg)
        workflow.after_delivery(session, org, msg, actor="gebruiker")
        _flash(request, f"Verzonden naar {msg.sent_to}." if msg.status == "verzonden" else f"Versturen mislukt: {msg.error}")
    if msg.kind == "offerte" and request.query_params.get("terug") == "offerte":
        back = f"/projects/{msg.project_id}/offerte#mailen"
    return RedirectResponse(back, status_code=303)


@router.post("/projects/{project_id}/status")
def project_status(
    project_id: int,
    request: Request,
    to: str = Form(...),
    note: str | None = Form(None),
    on: str | None = Form(None),
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    if r := _require(request, org):
        return r
    p = services.get_project(session, org, project_id)
    if not p:
        return RedirectResponse("/projects", status_code=303)
    try:
        try:
            when = date.fromisoformat(on) if on else None
        except ValueError:
            raise workflow.TransitionError(f"Ongeldige datum '{on}'.") from None
        workflow.change_status(session, org, p, to, note, on=when)
        _flash(request, f"Status: {workflow.label(to)}.")
    except workflow.TransitionError as exc:
        _flash(request, str(exc))
    return RedirectResponse(f"/projects/{project_id}#status", status_code=303)


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
    background: BackgroundTasks,
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    if r := _require(request, org):
        return r
    p = services.get_project(session, org, project_id)
    quote = services.latest_quote(session, p)
    if not quote:
        return RedirectResponse(f"/projects/{project_id}", status_code=303)
    customer = services.project_customer(session, p)
    lang = services.quote_language(org, customer)
    tr = services.translations_for(quote, lang)
    if started := services.start_translation(session, org, p, quote):  # e.g. the customer or its language changed
        background.add_task(ai_jobs.run, started[0].id, started[1])
    return _render(
        request,
        "offerte.html",
        org=org,
        project=p,
        quote=quote,
        result=services.quote_result(quote),
        proposal_sentence=planning_mod.proposal_sentence(p, {}),
        offer_lines=offer_lines,
        offer_totals=offer_totals,
        customer=customer,
        incl_vat=services.prices_incl_vat(customer),
        lang=lang,
        language_name=i18n.LANGUAGES[lang],
        t=lambda key, **kw: i18n.t(lang, key, **kw),
        tr=lambda text: tr.get(text, text),
        intro=services.offer_intro(p, lang, tr),
        untranslated=len(services.missing_translations(p, quote, lang)),
        translating=bool(ai_jobs.running(session, org, project_id=p.id, kind="vertaling")),
        mail_defaults=workflow.quote_mail_defaults(session, org, p, quote),
        public_link=workflow.public_url(session, quote),
        quote_mails=workflow.quote_mails(session, p),
        smtp_ready=get_settings().smtp_ready,
        mail_redirect_to=get_settings().mail_redirect_to,
    )


@router.post("/projects/{project_id}/offerte/mail")
def offerte_mail(
    project_id: int,
    request: Request,
    to: str = Form(""),
    subject: str = Form(""),
    body: str = Form(""),
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    """Mail the latest quote (PDF attached) to the customer."""
    if r := _require(request, org):
        return r
    p = services.get_project(session, org, project_id)
    quote = services.latest_quote(session, p) if p else None
    if not quote:
        return RedirectResponse(f"/projects/{project_id}", status_code=303)
    try:
        msg = workflow.send_quote(session, org, p, quote, to, subject, body)
    except workflow.TransitionError as exc:
        _flash(request, str(exc))
        return RedirectResponse(f"/projects/{project_id}/offerte#mailen", status_code=303)
    if msg.status == "verzonden":
        test = get_settings().mail_redirect_to
        _flash(request, f"Offerte verzonden naar {msg.to}." if not test else f"Testmodus: offerte voor {msg.to} ging naar {msg.sent_to}.")
    elif msg.status == "mislukt":
        _flash(request, f"Versturen mislukt: {msg.error}. De mail staat in de outbox; probeer opnieuw met 'Verstuur nu'.")
    else:
        _flash(request, "Er is nog geen mailserver ingesteld: de mail staat klaar in de outbox. Verstuur hem zodra SMTP ingesteld is.")
    return RedirectResponse(f"/projects/{project_id}/offerte#mailen", status_code=303)


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
        services.offerte_pdf(session, org, p, quote),
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
    media_type = plan.media_type or "application/octet-stream"
    headers = {"X-Content-Type-Options": "nosniff"}
    if media_type == "image/svg+xml":  # uploaded file opened directly: never run its scripts
        headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; img-src data:"
    return FileResponse(path, media_type=media_type, headers=headers)


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
