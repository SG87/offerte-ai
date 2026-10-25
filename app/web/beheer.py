"""Web routes for the management sections: contacten (klanten, leveranciers, werknemers), voorraad, to-do, planning."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, select

from app import ai_jobs, imports, planning, services, todos, workflow
from app.ai import import_mapping
from app.db import get_session
from app.engine import load_skill
from app.i18n import LANGUAGES
from app.integrations import stock as stock_mod
from app.models import (
    Absence,
    Customer,
    Employee,
    Organisation,
    PlanningItem,
    Project,
    PurchaseOrder,
    StockImportRow,
    StockReservation,
    Supplier,
    Todo,
)
from app.web.routes import _ai_started, _f, _flash, _render, _require, current_org

router = APIRouter(include_in_schema=False)
MONTHS = ["januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus", "september", "oktober", "november", "december"]


def _d(value: str | None, default: date | None = None) -> date | None:
    try:
        return date.fromisoformat(value) if value else default
    except ValueError:
        return default


def _ym(year: int, month: int, delta: int) -> str:
    y, m = planning.shift_month(year, month, delta)
    return f"{y:04d}-{m:02d}"


def _i(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "", "0") else None
    except (TypeError, ValueError):
        return None


# ---------- contacten ----------


@dataclass
class Kind:
    model: type
    label: str
    singular: str
    fields: list[tuple]  # (name, label, type, required?, options?)
    columns: list[str]
    note: str = ""
    defaults: dict = field(default_factory=dict)


ROLES = ["vakman", "ploegbaas", "voorraad", "administratie", "zaakvoerder"]
KINDS: dict[str, Kind] = {
    "klanten": Kind(
        Customer,
        "Klanten",
        "Klant",
        [
            ("name", "Naam", "text", True),
            ("kind", "Type klant", "select", True, list(services.CUSTOMER_KINDS.items())),
            ("language", "Taal (offerte)", "select", True, list(LANGUAGES.items())),
            ("contact_name", "Contactpersoon", "text"),
            ("email", "E-mail", "email"),
            ("phone", "Telefoon", "tel"),
            ("address", "Adres", "text"),
            ("vat_number", "Btw-nummer", "text"),
            ("website", "Website", "url"),
            ("notes", "Notities", "textarea"),
        ],
        ["name", "kind", "language", "contact_name", "email", "phone", "address"],
        "Een project hoort altijd bij een klant: maak die eerst aan. B2C-klanten (particulieren) krijgen een offerte "
        "met prijzen incl. btw, B2B-klanten (bedrijven) excl. btw. De offerte komt in de taal van de klant.",
        {"kind": "b2c"},
    ),
    "leveranciers": Kind(
        Supplier,
        "Leveranciers",
        "Leverancier",
        [
            ("name", "Naam", "text", True),
            ("contact_name", "Contactpersoon", "text"),
            ("email", "E-mail voor bestellingen", "email"),
            ("phone", "Telefoon", "tel"),
            ("address", "Adres", "text"),
            ("vat_number", "Btw-nummer", "text"),
            ("website", "Website", "url"),
            ("customer_number", "Ons klantnummer", "text"),
            ("lead_days", "Levertermijn (dagen)", "number"),
            ("notes", "Notities", "textarea"),
        ],
        ["name", "contact_name", "email", "phone", "lead_days"],
        "Koppel een leverancier aan een materiaal op de voorraadpagina; bevestigde bestellingen gaan naar dit e-mailadres.",
    ),
    "werknemers": Kind(
        Employee,
        "Werknemers",
        "Werknemer",
        [
            ("name", "Naam", "text", True),
            ("role", "Rol", "select", False, ROLES),
            ("email", "E-mail", "email"),
            ("phone", "Telefoon", "tel"),
            ("channels", "Gewenste communicatiemiddelen", "checkboxes", True,
             [(k, v if k in todos.AVAILABLE_CHANNELS else f"{v} (binnenkort)", k not in todos.AVAILABLE_CHANNELS)
              for k, v in todos.CHANNELS.items()]),
            ("hours_per_day", "Uren per dag", "number"),
            ("color", "Kleur in de planning", "color"),
            ("stock_manager", "Verantwoordelijke voorraad (bevestigt bestellingen)", "checkbox"),
            ("planner", "Planner (krijgt 'werk inplannen')", "checkbox"),
            ("invoicer", "Facturatie (keurt facturen goed)", "checkbox"),
            ("notes", "Notities", "textarea"),
        ],
        ["name", "role", "email", "phone", "channels", "flags"],
        "Afwezigheden (verlof, ziekte) zet je op de pagina van de werknemer; de planning houdt er rekening mee.",
        {"hours_per_day": 8.0, "channels": ["mail"]},
    ),
}


def _rows(session: Session, org: Organisation, kind: Kind, show_inactive: bool, review_only: bool = False):
    q = select(kind.model).where(kind.model.org_id == org.id)
    if not show_inactive:
        q = q.where(kind.model.active == True)  # noqa: E712
    if review_only and hasattr(kind.model, "needs_review"):
        q = q.where(kind.model.needs_review == True)  # noqa: E712
    return list(session.exec(q.order_by(kind.model.name)).all())


def _review_count(session: Session, org: Organisation, kind: Kind) -> int:
    if not hasattr(kind.model, "needs_review"):
        return 0
    return len(session.exec(select(kind.model.id).where(
        kind.model.org_id == org.id, kind.model.needs_review == True, kind.model.active == True  # noqa: E712
    )).all())


@router.get("/contacten")
def contacten_root():
    return RedirectResponse("/contacten/klanten", status_code=303)


@router.get("/contacten/{kind_key}", response_class=HTMLResponse)
def contacten(
    request: Request,
    kind_key: str,
    edit: int | None = None,
    inactief: int = 0,
    review: int = 0,
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    if r := _require(request, org):
        return r
    kind = KINDS.get(kind_key)
    if not kind:
        return RedirectResponse("/contacten/klanten", status_code=303)
    editing = session.get(kind.model, edit) if edit else None
    if editing and editing.org_id != org.id:
        editing = None
    return _render(
        request,
        "contacten.html",
        org=org,
        channels=todos.CHANNELS,
        kinds=KINDS,
        kind_key=kind_key,
        kind=kind,
        rows=_rows(session, org, kind, bool(inactief), bool(review)),
        editing=editing,
        review_only=bool(review),
        review_count=_review_count(session, org, kind),
        ai_kind=kind_key in imports.MODELS,
        busy=ai_jobs.running(session, org, kind="contact") + ai_jobs.running(session, org, kind="import"),
        defaults={**kind.defaults, **({"language": org.language} if kind.model is Customer else {})},
        show_inactive=bool(inactief),
        color_of=planning.employee_color,
    )


@router.post("/contacten/{kind_key}")
async def contacten_post(
    request: Request,
    kind_key: str,
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    if r := _require(request, org):
        return r
    kind = KINDS[kind_key]
    form = await request.form()
    row = session.get(kind.model, _i(form.get("id"))) if _i(form.get("id")) else None
    if row and row.org_id != org.id:
        row = None
    row = row or kind.model(org_id=org.id, name="", **kind.defaults)
    for spec in kind.fields:
        name, ftype = spec[0], spec[2]
        raw = form.get(name)
        if ftype == "checkboxes":  # several choices, stored as JSON in <name>_json; disabled options can't be chosen
            allowed = [o[0] for o in spec[4] if not (len(o) > 2 and o[2])]
            values = [v for v in allowed if v in form.getlist(name)] or allowed[:1]
            setattr(row, f"{name}_json", json.dumps(values))
            continue
        if ftype == "checkbox":
            value: Any = raw == "on"
        elif ftype == "number":
            value = _f(raw, getattr(row, name) or 0)
            if name == "lead_days":
                value = int(value)
        elif ftype == "select":
            # (value, label, disabled) options can be shown but not chosen
            allowed = [o[0] if isinstance(o, tuple) else o for o in spec[4] if not (isinstance(o, tuple) and len(o) > 2 and o[2])]
            value = raw if raw in allowed else (getattr(row, name) or allowed[0])
        else:
            value = (raw or "").strip() or None
            if name == "name":
                value = value or "(zonder naam)"
            elif name == "vat_number" and value:
                value = value.replace(" ", "").replace(".", "").upper()
        setattr(row, name, value)
    if getattr(row, "needs_review", False):  # saved through the form = checked by the user
        row.needs_review, row.review_note = False, None
    session.add(row)
    session.commit()
    _flash(request, f"{kind.singular} bewaard.")
    return RedirectResponse(f"/contacten/{kind_key}", status_code=303)


@router.post("/contacten/{kind_key}/ai-zoek")
def contacten_ai_lookup(
    request: Request,
    kind_key: str,
    background: BackgroundTasks,
    query: str = Form(""),
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    """Name or e-mail address -> background lookup that creates the supplier/customer."""
    if r := _require(request, org):
        return r
    query = query.strip()[:200]
    if kind_key not in imports.MODELS or not query:
        _flash(request, "Geef een naam of e-mailadres.")
        return RedirectResponse(f"/contacten/{kind_key}", status_code=303)
    job, work = imports.start_lookup(session, org, kind_key, query)
    background.add_task(ai_jobs.run, job.id, work)
    _ai_started(request, f"We zoeken de gegevens van “{query}” op")
    return RedirectResponse(f"/contacten/{kind_key}", status_code=303)


@router.post("/contacten/{kind_key}/{row_id}/ai-aanvullen")
def contacten_ai_complete(
    request: Request,
    kind_key: str,
    row_id: int,
    background: BackgroundTasks,
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    """Fill the empty fields of an existing supplier/customer from its e-mail address or name."""
    if r := _require(request, org):
        return r
    model = imports.MODELS.get(kind_key)
    row = session.get(model, row_id) if model else None
    if row is None or row.org_id != org.id:
        return RedirectResponse(f"/contacten/{kind_key}", status_code=303)
    job, work = imports.start_lookup(session, org, kind_key, row.email or row.name, row_id=row.id)
    background.add_task(ai_jobs.run, job.id, work)
    _ai_started(request, f"We vullen de gegevens van {row.name} aan")
    return RedirectResponse(f"/contacten/{kind_key}?edit={row.id}", status_code=303)


@router.post("/contacten/{kind_key}/import")
async def contacten_import(
    request: Request,
    kind_key: str,
    background: BackgroundTasks,
    file: UploadFile = File(...),
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    """Free-format Excel/CSV of suppliers or customers -> background AI mapping."""
    if r := _require(request, org):
        return r
    if kind_key not in imports.MODELS:
        return RedirectResponse("/contacten/klanten", status_code=303)
    name = file.filename or "bestand"
    try:
        sheets = import_mapping.read_table(await file.read(import_mapping.MAX_BYTES + 1), name)
    except import_mapping.ImportError_ as exc:
        _flash(request, f"Import mislukt: {exc}")
        return RedirectResponse(f"/contacten/{kind_key}", status_code=303)
    job, work = imports.start_contact_import(session, org, kind_key, name, sheets)
    background.add_task(ai_jobs.run, job.id, work)
    _ai_started(request, f"We lezen {name} in")
    return RedirectResponse(f"/contacten/{kind_key}", status_code=303)


@router.post("/contacten/{kind_key}/{row_id}/actief")
def contacten_toggle(
    request: Request,
    kind_key: str,
    row_id: int,
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    if r := _require(request, org):
        return r
    kind = KINDS[kind_key]
    row = session.get(kind.model, row_id)
    if row and row.org_id == org.id:
        row.active = not row.active
        session.add(row)
        session.commit()
        _flash(request, f"{kind.singular} {'geactiveerd' if row.active else 'gearchiveerd'}.")
    return RedirectResponse(f"/contacten/{kind_key}", status_code=303)


@router.get("/contacten/werknemers/{emp_id}", response_class=HTMLResponse)
def werknemer(
    request: Request,
    emp_id: int,
    maand: str | None = None,
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    if r := _require(request, org):
        return r
    emp = session.get(Employee, emp_id)
    if not emp or emp.org_id != org.id:
        return RedirectResponse("/contacten/werknemers", status_code=303)
    today = date.today()
    y, m = (int(maand[:4]), int(maand[5:7])) if maand and len(maand) >= 7 else (today.year, today.month)
    start, end = planning.month_bounds(y, m)
    weeks = planning.month_grid(y, m)
    rows = planning.absences(session, [emp.id], weeks[0][0], weeks[-1][-1])
    items = [it for it in planning.items_between(session, org, weeks[0][0], weeks[-1][-1])]
    who = planning.assignees(session, items)
    mine = [it for it in items if emp.id in who.get(it.id, [])]
    projects = {p.id: p for p in services.list_projects(session, org)}
    return _render(
        request,
        "werknemer.html",
        org=org,
        channels=todos.CHANNELS,
        emp=emp,
        color=planning.employee_color(emp),
        year=y,
        month=m,
        month_label=f"{MONTHS[m - 1]} {y}",
        prev_month=_ym(y, m, -1),
        next_month=_ym(y, m, 1),
        weeks=weeks,
        today=today,
        in_month=lambda d: start <= d <= end,
        absent_days=planning.absences_by_day(rows, weeks[0][0], weeks[-1][-1]),
        work_days=planning.by_day(mine, weeks[0][0], weeks[-1][-1]),
        all_absences=sorted(
            session.exec(select(Absence).where(Absence.employee_id == emp.id)).all(), key=lambda a: a.date_from, reverse=True
        ),
        projects=projects,
        hours_month=planning.workload(session, org, start, end).get(emp.id, 0.0),
    )


@router.post("/contacten/werknemers/{emp_id}/afwezigheid")
def afwezigheid_post(
    request: Request,
    emp_id: int,
    date_from: str = Form(...),
    date_to: str | None = Form(None),
    reason: str = Form("verlof"),
    note: str | None = Form(None),
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    if r := _require(request, org):
        return r
    emp = session.get(Employee, emp_id)
    if not emp or emp.org_id != org.id:
        return RedirectResponse("/contacten/werknemers", status_code=303)
    d1 = _d(date_from)
    d2 = _d(date_to, d1)
    if not d1:
        _flash(request, "Geef een geldige datum.")
    else:
        planning.add_absence(session, emp, d1, d2, reason, note)
        _flash(request, f"Afwezigheid bewaard: {reason} {d1:%d/%m}–{d2:%d/%m}.")
    return RedirectResponse(f"/contacten/werknemers/{emp_id}?maand={d1 or date.today():%Y-%m}", status_code=303)


@router.post("/contacten/werknemers/{emp_id}/afwezigheid/{abs_id}/verwijder")
def afwezigheid_delete(
    request: Request,
    emp_id: int,
    abs_id: int,
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    if r := _require(request, org):
        return r
    row = session.get(Absence, abs_id)
    emp = session.get(Employee, emp_id)
    if row and emp and emp.org_id == org.id and row.employee_id == emp.id:
        session.delete(row)
        session.commit()
        _flash(request, "Afwezigheid verwijderd.")
    return RedirectResponse(f"/contacten/werknemers/{emp_id}", status_code=303)


# ---------- voorraad ----------


def _stock_context(session: Session, org: Organisation) -> dict:
    cfg = services.current_config(session, org)
    tracked = stock_mod.tracked_codes(cfg, load_skill(org.sector).default_price_config())
    suppliers = {s.id: s for s in session.exec(select(Supplier).where(Supplier.org_id == org.id)).all()}
    orders = stock_mod.all_orders(session, org)
    projects = {p.id: p for p in services.list_projects(session, org)}
    reservations = session.exec(
        select(StockReservation).where(StockReservation.org_id == org.id, StockReservation.status == "gereserveerd")
    ).all()
    by_project: dict[int, list[StockReservation]] = {}
    for res in reservations:
        by_project.setdefault(res.project_id, []).append(res)
    return {
        "rows": stock_mod.overview(session, org, cfg, tracked),
        "suppliers": suppliers,
        "active_suppliers": [s for s in suppliers.values() if s.active],
        "orders": orders,
        "order_lines": {o.id: stock_mod.order_lines(session, o) for o in orders},
        "projects": projects,
        "reservations": by_project,
        "labels": {i.code: i.label for i in cfg.items},
        "managers": todos.responsible(session, org, "stock_manager"),
        "import_rows": imports.open_stock_rows(session, org),
        "stocked": [i for i in cfg.items if i.code in tracked],
        "busy": ai_jobs.running(session, org, kind="import"),
    }


@router.get("/voorraad", response_class=HTMLResponse)
def voorraad(request: Request, org: Organisation | None = Depends(current_org), session: Session = Depends(get_session)):
    if r := _require(request, org):
        return r
    return _render(request, "voorraad.html", org=org, **_stock_context(session, org))


@router.post("/voorraad")
async def voorraad_post(
    request: Request, org: Organisation | None = Depends(current_org), session: Session = Depends(get_session)
):
    if r := _require(request, org):
        return r
    form = await request.form()
    cfg = services.current_config(session, org)
    tracked = stock_mod.tracked_codes(cfg, load_skill(org.sector).default_price_config())
    for item in cfg.items:
        if item.code not in tracked or f"qty_{item.code}" not in form:
            continue
        level = stock_mod.level_for(session, org, item.code, item.material_unit or item.unit)
        stock_mod.set_stock(
            session,
            org,
            item.code,
            _f(form.get(f"qty_{item.code}"), level.qty),
            item.material_unit or item.unit,
            reorder_point=_f(form.get(f"reorder_{item.code}"), level.reorder_point),
            pack_size=max(_f(form.get(f"pack_{item.code}"), level.pack_size), 0.01),
            supplier_id=_i(form.get(f"supplier_{item.code}")),
            location=(form.get(f"location_{item.code}") or "").strip() or None,
        )
    labels = {i.code: i.label for i in cfg.items}
    new = stock_mod.propose_orders(session, org, labels) if form.get("check") else []
    for order in new:
        workflow._ask_to_confirm_order(session, org, order, None)
    msg = "Voorraad bewaard."
    if form.get("check"):
        msg += f" {len(new)} nieuw bestelvoorstel{'' if len(new) == 1 else 'len'}." if new else " Niets onder het bestelpunt."
    _flash(request, msg)
    return RedirectResponse("/voorraad", status_code=303)


@router.post("/voorraad/import")
async def voorraad_import(
    request: Request,
    background: BackgroundTasks,
    file: UploadFile | None = File(None),
    text: str = Form(""),
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    """A free-format stock list (Excel/CSV) or a line of text ("12 emmers muurverf wit, rek A2")."""
    if r := _require(request, org):
        return r
    if file is not None and file.filename:
        source = file.filename
        try:
            sheets = import_mapping.read_table(await file.read(import_mapping.MAX_BYTES + 1), source)
        except import_mapping.ImportError_ as exc:
            _flash(request, f"Import mislukt: {exc}")
            return RedirectResponse("/voorraad", status_code=303)
    elif text.strip():
        source, sheets = "snelle invoer", imports.text_as_sheet(text[:5000])
    else:
        _flash(request, "Kies een bestand of typ wat er in voorraad is.")
        return RedirectResponse("/voorraad", status_code=303)
    job, work = imports.start_stock_import(session, org, source, sheets)
    background.add_task(ai_jobs.run, job.id, work)
    _ai_started(request, "We verwerken je voorraadlijst" if source != "snelle invoer" else "We verwerken je invoer")
    return RedirectResponse("/voorraad", status_code=303)


@router.post("/voorraad/import/{row_id}/{action}")
async def voorraad_import_row(
    request: Request,
    row_id: int,
    action: str,
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    if r := _require(request, org):
        return r
    row = session.get(StockImportRow, row_id)
    if row is None or row.org_id != org.id or row.status != "open" or action not in ("toepassen", "negeer"):
        return RedirectResponse("/voorraad", status_code=303)
    form = await request.form()
    qty = _f(form.get("qty"), -1.0) if form.get("qty") not in (None, "") else None
    ok, msg = imports.resolve_stock_row(session, org, row, action, code=form.get("code"), qty=qty)
    _flash(request, msg)
    return RedirectResponse("/voorraad" + ("" if ok else "#na-te-kijken"), status_code=303)


def _order(session: Session, org: Organisation, order_id: int) -> PurchaseOrder | None:
    order = session.get(PurchaseOrder, order_id)
    return order if order and order.org_id == org.id else None


@router.post("/voorraad/bestellingen/{order_id}/{action}")
def bestelling_action(
    request: Request,
    order_id: int,
    action: str,
    supplier_id: str | None = Form(None),
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    if r := _require(request, org):
        return r
    order = _order(session, org, order_id)
    if not order:
        return RedirectResponse("/voorraad", status_code=303)
    if action == "leverancier":
        order.supplier_id = _i(supplier_id)
        session.add(order)
        session.commit()
        msg = "Leverancier gekoppeld aan de bestelling."
    elif action == "bevestig":
        _, msg = workflow.confirm_order(session, org, order, by=org.company_name or org.email)
    elif action == "ontvangen":
        _, msg = workflow.receive_order(session, org, order)
    elif action == "annuleer":
        _, msg = workflow.cancel_order(session, org, order)
    else:
        msg = "Onbekende actie."
    _flash(request, msg)
    return RedirectResponse(f"/voorraad#bestelling-{order.id}", status_code=303)


# ---------- to-do ----------


@router.get("/todo", response_class=HTMLResponse)
def todo_page(
    request: Request,
    status: str = "open",
    wie: int | None = None,
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    if r := _require(request, org):
        return r
    emps = {e.id: e for e in planning.employees(session, org, active_only=False)}
    projects = {p.id: p for p in services.list_projects(session, org)}
    return _render(
        request,
        "todo.html",
        org=org,
        status=status if status in ("open", "gedaan") else "open",
        wie=wie,
        items=todos.list_todos(session, org, status if status in ("open", "gedaan") else "open", wie),
        emps=emps,
        active_emps=[e for e in emps.values() if e.active],
        projects=projects,
        today=date.today(),
        n_open=todos.count_open(session, org),
    )


@router.post("/todo")
def todo_post(
    request: Request,
    title: str = Form(...),
    detail: str | None = Form(None),
    due: str | None = Form(None),
    assignee_id: str | None = Form(None),
    project_id: str | None = Form(None),
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    if r := _require(request, org):
        return r
    todos.add(
        session, org, title.strip() or "To-do", detail=(detail or "").strip() or None, due=_d(due),
        assignee_id=_i(assignee_id), project_id=_i(project_id),
        link=f"/projects/{_i(project_id)}" if _i(project_id) else None,
    )
    _flash(request, "To-do toegevoegd.")
    return RedirectResponse("/todo", status_code=303)


@router.post("/todo/{todo_id}/{action}")
def todo_action(
    request: Request,
    todo_id: int,
    action: str,
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    if r := _require(request, org):
        return r
    todo = session.get(Todo, todo_id)
    back = request.headers.get("referer") or "/todo"
    if not todo or todo.org_id != org.id:
        return RedirectResponse("/todo", status_code=303)
    if action == "klaar":
        todos.complete(session, todo)
    elif action == "heropen":
        todos.reopen(session, todo)
    elif action == "verwijder":
        session.delete(todo)
        session.commit()
    return RedirectResponse(back if back.startswith(("http://", "https://", "/")) else "/todo", status_code=303)


# ---------- planning ----------


@router.get("/planning", response_class=HTMLResponse)
def planning_page(
    request: Request,
    maand: str | None = None,
    dag: str | None = None,
    project: int | None = None,
    item: int | None = None,
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    if r := _require(request, org):
        return r
    today = date.today()
    selected = _d(dag)
    editing = session.get(PlanningItem, item) if item else None
    if editing and editing.org_id != org.id:
        editing = None
    if editing and not selected:
        selected = editing.date_from
    if maand and len(maand) >= 7:
        y, m = int(maand[:4]), int(maand[5:7])
    elif selected:
        y, m = selected.year, selected.month
    else:
        y, m = today.year, today.month
    start, end = planning.month_bounds(y, m)
    weeks = planning.month_grid(y, m)
    span0, span1 = weeks[0][0], weeks[-1][-1]
    items = planning.items_between(session, org, span0, span1)
    who = planning.assignees(session, items)
    emps = planning.employees(session, org, active_only=False)
    emp_by_id = {e.id: e for e in emps}
    active = [e for e in emps if e.active]
    abs_rows = planning.absences(session, [e.id for e in emps], span0, span1)
    all_projects = services.list_projects(session, org)
    open_projects = [p for p in all_projects if p.status not in ("afgerond", "geweigerd")]
    day_items = planning.by_day(items, span0, span1).get(selected, []) if selected else []
    absent_sel = planning.absent_on(session, [e.id for e in active], selected) if selected else set()
    editing_emps = who.get(editing.id, []) if editing else []
    return _render(
        request,
        "planning.html",
        org=org,
        year=y,
        month=m,
        month_label=f"{MONTHS[m - 1]} {y}",
        prev_month=_ym(y, m, -1),
        next_month=_ym(y, m, 1),
        weeks=weeks,
        today=today,
        in_month=lambda d: start <= d <= end,
        items_by_day=planning.by_day(items, span0, span1),
        absences_by_day=planning.absences_by_day(abs_rows, span0, span1),
        who=who,
        emps=emp_by_id,
        active_emps=active,
        color_of=planning.employee_color,
        projects={p.id: p for p in all_projects},
        open_projects=open_projects,
        preselect_project=project,
        selected=selected,
        day_items=day_items,
        absent_sel=absent_sel,
        editing=editing,
        editing_emps=editing_emps,
        workload=planning.workload(session, org, start, end),
        wf=workflow,
        open_plan_todos=[t for t in todos.list_todos(session, org, "open") if t.kind == "inplannen"],
    )


@router.post("/planning/items")
async def planning_item_post(
    request: Request, org: Organisation | None = Depends(current_org), session: Session = Depends(get_session)
):
    if r := _require(request, org):
        return r
    form = await request.form()
    item = session.get(PlanningItem, _i(form.get("item_id"))) if _i(form.get("item_id")) else None
    if item and item.org_id != org.id:
        item = None
    d1 = _d(form.get("date_from"))
    if not d1:
        _flash(request, "Geef een begindatum.")
        return RedirectResponse("/planning", status_code=303)
    d2 = _d(form.get("date_to"), d1)
    project_id = _i(form.get("project_id"))
    if project_id and not services.get_project(session, org, project_id):
        project_id = None
    emp_ids = [i for i in (_i(v) for v in form.getlist("employee_ids")) if i]
    item, warnings = planning.save_item(
        session,
        org,
        item,
        project_id=project_id,
        title=form.get("title"),
        date_from=d1,
        date_to=d2,
        employee_ids=emp_ids,
        start_time=form.get("start_time"),
        hours=_f(form.get("hours"), None),
        note=form.get("note"),
        status=form.get("status") or "gepland",
    )
    if project_id and (p := session.get(Project, project_id)):
        todos.complete_matching(session, org, "inplannen", project_id=p.id)
        workflow.log(session, p, f"Ingepland {d1:%d/%m}{'' if d2 == d1 else f'–{d2:%d/%m}'}" + (f" ({len(emp_ids) - len(warnings)} pers.)" if emp_ids else ""), kind="info", actor="gebruiker", link=f"/planning?dag={d1}")
    _flash(request, "Planning bewaard." + (" " + " ".join(warnings) if warnings else ""))
    return RedirectResponse(f"/planning?dag={d1}", status_code=303)


@router.post("/planning/items/{item_id}/verwijder")
def planning_item_delete(
    request: Request,
    item_id: int,
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    if r := _require(request, org):
        return r
    item = session.get(PlanningItem, item_id)
    day = item.date_from if item else date.today()
    if item and item.org_id == org.id:
        planning.delete_item(session, item)
        _flash(request, "Planning verwijderd.")
    return RedirectResponse(f"/planning?dag={day}", status_code=303)


def parse_dt(value: str) -> datetime | None:  # kept for templates/tests that need it
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
