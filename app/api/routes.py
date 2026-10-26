"""REST API v1. Authentication: header `X-API-Key: <org api key>` (shown on the configuratie page)."""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, File, Header, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, EmailStr, Field
from sqlmodel import Session, select

from app import address, ai_jobs, invoicing, services, todos, workflow
from app import planning as planning_mod
from app.ai.client import AIUnavailable
from app.db import get_session
from app.engine import calculate_quote, load_skill
from app.integrations import events as events_mod
from app.integrations import stock as stock_mod
from app.models import Customer, IntegrationEvent, Organisation
from app.schemas import Adjustment, CompanyLookup, Geometry, PriceConfig, QuoteResult

router = APIRouter(tags=["api-v1"])


def require_org(
    x_api_key: str | None = Header(default=None), session: Session = Depends(get_session)
) -> Organisation:
    if not x_api_key:
        raise HTTPException(401, "X-API-Key header ontbreekt")
    org = services.get_org_by_api_key(session, x_api_key)
    if not org:
        raise HTTPException(401, "Ongeldige API-key")
    return org


class SignupIn(BaseModel):
    email: EmailStr
    lookup: bool = True


class SignupOut(BaseModel):
    org_id: int
    email: str
    api_key: str
    sector: str
    created: bool
    lookup: CompanyLookup | None = None


@router.post("/signup", response_model=SignupOut)
def signup(body: SignupIn, session: Session = Depends(get_session)):
    org, lookup, created = services.signup(session, body.email, do_lookup=body.lookup)
    return SignupOut(
        org_id=org.id, email=org.email, api_key=org.api_key, sector=org.sector, created=created, lookup=lookup
    )


@router.get("/me")
def me(org: Organisation = Depends(require_org)):
    return {
        "org_id": org.id,
        "email": org.email,
        "company_name": org.company_name,
        "sector": org.sector,
        "address": org.address,
        "vat_number": org.vat_number,
    }


@router.get("/sectors")
def sectors():
    return services.available_sector_list()


@router.get("/config", response_model=PriceConfig)
def get_config(org: Organisation = Depends(require_org), session: Session = Depends(get_session)):
    return services.current_config(session, org)


@router.put("/config", response_model=PriceConfig)
def put_config(
    config: PriceConfig, org: Organisation = Depends(require_org), session: Session = Depends(get_session)
):
    services.save_config(session, org, config)
    return services.current_config(session, org)


class OverrideIn(BaseModel):
    free_text: str


@router.put("/skill-override")
def put_override(
    body: OverrideIn, org: Organisation = Depends(require_org), session: Session = Depends(get_session)
):
    row, unsupported = services.save_override(session, org, body.free_text)
    return {
        "version": row.version,
        "translated_by": row.translated_by,
        "adjustments": row.adjustments,
        "unsupported": unsupported,
    }


def _customer_out(c: Customer) -> dict:
    return {
        "customer_id": c.id,
        "name": c.name,
        "kind": c.kind,
        "language": c.language,
        "prices": "incl. btw" if services.prices_incl_vat(c) else "excl. btw",
        "contact_name": c.contact_name,
        "email": c.email,
        "phone": c.phone,
        "address": c.address,
        **c.address_parts,
        "vat_number": c.vat_number,
        "active": c.active,
    }


class CustomerIn(BaseModel):
    name: str
    kind: Literal["b2c", "b2b"] = Field("b2c", description="b2c: offerte incl. btw | b2b: offerte excl. btw")
    language: Literal["nl", "fr", "en"] | None = Field(None, description="taal van de offerte; leeg = taal van je bedrijf")
    contact_name: str | None = None
    email: str | None = None
    phone: str | None = None
    street: str | None = None
    house_number: str | None = None
    postal_code: str | None = None
    city: str | None = None
    country: str | None = Field(None, description="ISO-code, bv. BE")
    address: str | None = Field(None, description="adres op één regel, als alternatief voor de onderdelen")
    vat_number: str | None = None
    notes: str | None = None


@router.get("/customers")
def list_customers(org: Organisation = Depends(require_org), session: Session = Depends(get_session)):
    rows = session.exec(select(Customer).where(Customer.org_id == org.id).order_by(Customer.name)).all()
    return [_customer_out(c) for c in rows]


@router.post("/customers")
def create_customer(
    body: CustomerIn, org: Organisation = Depends(require_org), session: Session = Depends(get_session)
):
    data = body.model_dump(exclude={"language", "address", *address.PARTS})
    parts = services.address_from_input(body.address, body.model_dump(include=set(address.PARTS)))
    c = Customer(org_id=org.id, **data, **parts, language=body.language or org.language)
    c.vat_number = re.sub(r"[\s.]", "", c.vat_number or "").upper() or None
    session.add(c)
    session.commit()
    session.refresh(c)
    return _customer_out(c)


class ProjectIn(BaseModel):
    name: str
    customer_id: int | None = Field(None, description="bestaande klant (GET /customers)")
    client_name: str | None = Field(None, description="zonder customer_id: klant met deze naam, of een nieuwe")
    client_type: Literal["b2c", "b2b"] | None = Field(None, description="voor een nieuwe klant; standaard b2c")
    client_language: Literal["nl", "fr", "en"] | None = Field(None, description="voor een nieuwe klant; standaard je eigen taal")
    client_vat_number: str | None = None
    client_email: str | None = None
    client_address: str | None = Field(None, description="werfadres; leeg = adres van de klant")
    work_type: str | None = Field(None, description="leeg: renovatie_woning_10j (b2c) of niet_woning (b2b)")
    scope: list[str] | None = None
    notes: str | None = None


@router.post("/projects")
def create_project(
    body: ProjectIn, org: Organisation = Depends(require_org), session: Session = Depends(get_session)
):
    try:
        customer = services.resolve_customer(
            session,
            org,
            customer_id=body.customer_id,
            name=body.client_name,
            kind=body.client_type,
            address=body.client_address,
            vat_number=body.client_vat_number,
            email=body.client_email,
            language=body.client_language,
        )
    except services.CustomerRequired as exc:
        raise HTTPException(422, str(exc)) from exc
    p = services.create_project(
        session,
        org,
        body.name,
        customer,
        client_address=body.client_address,
        work_type=body.work_type,
        scope=body.scope,
        notes=body.notes,
    )
    return {
        "project_id": p.id,
        "name": p.name,
        "customer": _customer_out(customer),
        "scope": p.scope,
        "vat_pct": p.vat_pct,
    }


@router.get("/projects")
def list_projects(org: Organisation = Depends(require_org), session: Session = Depends(get_session)):
    return [
        {
            "project_id": p.id,
            "name": p.name,
            "client_name": p.client_name,
            "customer_id": p.customer_id,
            "status": p.status,
            "status_label": workflow.label(p.status),
        }
        for p in services.list_projects(session, org)
    ]


def _project(session: Session, org: Organisation, project_id: int):
    p = services.get_project(session, org, project_id)
    if not p:
        raise HTTPException(404, "Project niet gevonden")
    return p


@router.delete("/projects/{project_id}")
def delete_project(project_id: int, org: Organisation = Depends(require_org), session: Session = Depends(get_session)):
    """Definitief: plan, offertes, historiek, planning en to-do's; gereserveerd materiaal gaat terug naar de voorraad."""
    name = services.delete_project(session, org, _project(session, org, project_id))
    return {"deleted": project_id, "name": name}


@router.post("/projects/{project_id}/plan", response_model=Geometry)
async def upload_plan(
    project_id: int,
    file: UploadFile = File(...),
    org: Organisation = Depends(require_org),
    session: Session = Depends(get_session),
):
    p = _project(session, org, project_id)
    data = await file.read()
    geometry, _ = services.analyze_upload(session, org, p, data, file.filename or "plan", file.content_type)
    return geometry


@router.get("/projects/{project_id}/geometry", response_model=Geometry)
def get_geometry(
    project_id: int, org: Organisation = Depends(require_org), session: Session = Depends(get_session)
):
    return services.current_geometry(session, _project(session, org, project_id))


@router.put("/projects/{project_id}/geometry", response_model=Geometry)
def put_geometry(
    project_id: int,
    geometry: Geometry,
    org: Organisation = Depends(require_org),
    session: Session = Depends(get_session),
):
    p = _project(session, org, project_id)
    services.save_geometry(session, p, geometry)
    return geometry


class AssistIn(BaseModel):
    instruction: str = Field(min_length=2)


@router.post("/projects/{project_id}/assist")
def assist(
    project_id: int,
    body: AssistIn,
    org: Organisation = Depends(require_org),
    session: Session = Depends(get_session),
):
    p = _project(session, org, project_id)
    try:
        geometry, summary = services.run_assist(session, org, p, body.instruction)
    except AIUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"summary": summary, "geometry": geometry}


@router.post("/projects/{project_id}/quote", response_model=QuoteResult)
def quote(
    project_id: int,
    background: BackgroundTasks,
    org: Organisation = Depends(require_org),
    session: Session = Depends(get_session),
):
    """Calculates a new quote version; for a customer in another language the translation follows in the background."""
    p = _project(session, org, project_id)
    q, result = services.run_quote(session, org, p)
    if started := services.start_translation(session, org, p, q):
        background.add_task(ai_jobs.run, started[0].id, started[1])
    return result


@router.get("/projects/{project_id}/quote.pdf")
def quote_pdf(
    project_id: int, org: Organisation = Depends(require_org), session: Session = Depends(get_session)
):
    p = _project(session, org, project_id)
    q = services.latest_quote(session, p)
    if not q:
        raise HTTPException(404, "Nog geen offerte; roep eerst POST .../quote aan")
    return Response(services.offerte_pdf(session, org, p, q), media_type="application/pdf")


class QuoteMailIn(BaseModel):
    to: str | None = Field(default=None, description="Leeg = e-mailadres van de klant")
    subject: str | None = None
    body: str | None = Field(default=None, description="Leeg = standaardtekst; de PDF zit altijd in bijlage")


@router.post("/projects/{project_id}/quote/mail")
def quote_mail(
    project_id: int,
    body: QuoteMailIn | None = None,
    org: Organisation = Depends(require_org),
    session: Session = Depends(get_session),
):
    """Mail the latest quote (PDF attached) to the customer. Status 'wacht' = no mail server, it is in the outbox."""
    p = _project(session, org, project_id)
    q = services.latest_quote(session, p)
    if not q:
        raise HTTPException(404, "Nog geen offerte; roep eerst POST .../quote aan")
    body = body or QuoteMailIn()
    defaults = workflow.quote_mail_defaults(session, org, p, q)
    try:
        msg = workflow.send_quote(
            session, org, p, q, body.to or defaults["to"], body.subject or defaults["subject"],
            body.body or defaults["body"], actor="api",
        )
    except workflow.TransitionError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "mail_id": msg.id, "to": msg.to, "sent_to": msg.sent_to, "status": msg.status, "error": msg.error,
        "quote_status": q.status, "project_status": p.status,
    }


@router.get("/projects/{project_id}/meetstaat.xlsx")
def quote_xlsx(
    project_id: int, org: Organisation = Depends(require_org), session: Session = Depends(get_session)
):
    p = _project(session, org, project_id)
    q = services.latest_quote(session, p)
    if not q:
        raise HTTPException(404, "Nog geen offerte")
    return Response(
        services.meetstaat_xlsx(p, q),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@router.get("/projects/{project_id}/plan.svg")
def quote_svg(
    project_id: int, org: Organisation = Depends(require_org), session: Session = Depends(get_session)
):
    p = _project(session, org, project_id)
    return Response(
        services.plan_svg(session, p, services.latest_quote(session, p)), media_type="image/svg+xml"
    )


class PreviewIn(BaseModel):
    geometry: Geometry
    scope: list[str]
    sector: str | None = None
    vat_pct: float | None = None
    adjustments: list[Adjustment] = Field(default_factory=list)


@router.post("/quote/preview", response_model=QuoteResult)
def preview(
    body: PreviewIn, org: Organisation = Depends(require_org), session: Session = Depends(get_session)
):
    """Stateless calculation with the org's current config: handy for integrations and tests."""
    skill = load_skill(body.sector or org.sector)
    config = (
        services.current_config(session, org)
        if (body.sector or org.sector) == org.sector
        else skill.default_price_config()
    )
    adjustments, version = services.current_adjustments(session, org)
    return calculate_quote(
        body.geometry,
        skill,
        config,
        scope=body.scope,
        adjustments=adjustments + body.adjustments,
        vat_pct=body.vat_pct,
        override_version=version,
    )


# ---------- project management ----------


class StatusIn(BaseModel):
    to: str = Field(description="voorbereid | gemaakt | gestuurd | aanvaard | in_uitvoering | werk_afgerond | "
                    "factuur_verstuurd | factuur_betaald | afgerond | geweigerd")
    note: str | None = None
    on: date | None = Field(
        None,
        description="in_uitvoering / werk_afgerond: dag waarop het werk echt startte of af was; "
        "vandaag (standaard) of vroeger, nooit in de toekomst. De planning schuift mee.",
    )


@router.get("/statuses")
def statuses():
    """All project statuses in order, with the statuses reachable from each."""
    return [
        {"key": s.key, "label": s.label, "phase": s.phase, "next": workflow.allowed(s.key)} for s in workflow.STATUSES
    ]


@router.post("/projects/{project_id}/status")
def set_status(
    project_id: int, body: StatusIn, org: Organisation = Depends(require_org), session: Session = Depends(get_session)
):
    p = _project(session, org, project_id)
    try:
        workflow.change_status(session, org, p, body.to, body.note, actor="api", on=body.on)
    except workflow.TransitionError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"project_id": p.id, "status": p.status, "status_label": workflow.label(p.status)}


@router.get("/projects/{project_id}/history")
def project_history(project_id: int, org: Organisation = Depends(require_org), session: Session = Depends(get_session)):
    p = _project(session, org, project_id)
    return [e.model_dump() for e in workflow.history(session, p)]


@router.get("/events")
def list_events(
    type: str | None = None,
    status: str | None = None,
    org: Organisation = Depends(require_org),
    session: Session = Depends(get_session),
):
    """Triggers for agents/tools, e.g. ?type=voorraad.controle&status=open. Mark them done with /events/{id}/ack."""
    return [events_mod.envelope(e) for e in events_mod.list_events(session, org, type, status)]


@router.post("/events/{event_id}/ack")
def ack_event(event_id: int, org: Organisation = Depends(require_org), session: Session = Depends(get_session)):
    ev = session.get(IntegrationEvent, event_id)
    if not ev or ev.org_id != org.id:
        raise HTTPException(404, "Event niet gevonden")
    ev.status = "afgehandeld"
    session.add(ev)
    session.commit()
    return events_mod.envelope(ev)


class StockIn(BaseModel):
    code: str = Field(description="code van de post in de prijslijst, bv. muurverf")
    qty: float
    unit: str | None = None


@router.get("/stock")
def get_stock(org: Organisation = Depends(require_org), session: Session = Depends(get_session)):
    return [
        {
            "code": s.code,
            "qty": s.qty,
            "reserved": s.reserved,
            "available": s.available,
            "reorder_point": s.reorder_point,
            "pack_size": s.pack_size,
            "supplier_id": s.supplier_id,
            "unit": s.unit,
            "updated_at": s.updated_at.isoformat(),
        }
        for s in stock_mod.stock_levels(session, org).values()
    ]


@router.put("/stock")
def put_stock(
    body: list[StockIn], org: Organisation = Depends(require_org), session: Session = Depends(get_session)
):
    """Set stock levels (e.g. by the stock agent). Codes come from the price list."""
    items = {i.code: i for i in services.current_config(session, org).items}
    unknown = [b.code for b in body if b.code not in items]
    if unknown:
        raise HTTPException(422, f"Onbekende codes: {', '.join(unknown)}")
    for b in body:
        item = items[b.code]
        stock_mod.set_stock(session, org, b.code, b.qty, b.unit or item.material_unit or item.unit)
    return get_stock(org, session)


# ---------- stock orders, to-dos, planning (for agents and tools) ----------


def _order_json(session: Session, order) -> dict:
    return {
        "id": order.id,
        "number": order.number,
        "status": order.status,
        "supplier_id": order.supplier_id,
        "mail_id": order.mail_id,
        "created_at": order.created_at.isoformat(),
        "lines": [
            {"code": ln.code, "label": ln.label, "qty": ln.qty, "unit": ln.unit, "project_id": ln.project_id, "reason": ln.reason}
            for ln in stock_mod.order_lines(session, order)
        ],
    }


@router.get("/orders")
def list_orders(org: Organisation = Depends(require_org), session: Session = Depends(get_session)):
    return [_order_json(session, o) for o in stock_mod.all_orders(session, org)]


@router.post("/orders/{order_id}/{action}")
def order_action(
    order_id: int, action: str, org: Organisation = Depends(require_org), session: Session = Depends(get_session)
):
    """action: bevestig (mails the supplier) | ontvangen (books the stock in) | annuleer."""
    from app.models import PurchaseOrder

    order = session.get(PurchaseOrder, order_id)
    if not order or order.org_id != org.id:
        raise HTTPException(404, "Bestelling niet gevonden")
    fn = {"bevestig": workflow.confirm_order, "ontvangen": workflow.receive_order, "annuleer": workflow.cancel_order}.get(action)
    if not fn:
        raise HTTPException(404, "Onbekende actie")
    ok, msg = fn(session, org, order, by="api") if action == "bevestig" else fn(session, org, order)
    if not ok:
        raise HTTPException(409, msg)
    return {"message": msg, "order": _order_json(session, order)}


@router.get("/todos")
def list_todos(status: str = "open", org: Organisation = Depends(require_org), session: Session = Depends(get_session)):
    return [t.model_dump() for t in todos.list_todos(session, org, status if status in ("open", "gedaan") else None)]


@router.post("/todos/{todo_id}/klaar")
def todo_done(todo_id: int, org: Organisation = Depends(require_org), session: Session = Depends(get_session)):
    from app.models import Todo

    t = session.get(Todo, todo_id)
    if not t or t.org_id != org.id:
        raise HTTPException(404, "To-do niet gevonden")
    return todos.complete(session, t).model_dump()


@router.get("/planning")
def list_planning(
    van: str, tot: str, org: Organisation = Depends(require_org), session: Session = Depends(get_session)
):
    """Planned work between two dates (YYYY-MM-DD), with the assigned employees."""
    from datetime import date as _date

    start, end = _date.fromisoformat(van), _date.fromisoformat(tot)
    items = planning_mod.items_between(session, org, start, end)
    who = planning_mod.assignees(session, items)
    return [{**it.model_dump(), "employee_ids": who.get(it.id, [])} for it in items]


# ---------- invoices ----------


def _invoice_out(inv) -> dict:
    tot = invoicing.totals(inv)
    due = invoicing.due_date(inv)
    return {
        "invoice_id": inv.id,
        "number": inv.number,
        "status": inv.status,
        "project_id": inv.project_id,
        "customer": inv.client_name,
        "b2b": inv.b2b,
        "language": inv.language,
        "invoice_date": inv.invoice_date.isoformat() if inv.invoice_date else None,
        "due_date": due.isoformat() if due else None,
        "structured_ref": inv.structured_ref,
        "lines": tot["lines"],
        "subtotal_excl": tot["subtotal"],
        "vat": tot["vat"],
        "total_incl": tot["total"],
        "sent_via": inv.sent_via,
        "peppol_status": inv.peppol_status,
        "paid_on": inv.paid_on.isoformat() if inv.paid_on else None,
    }


def _inv(session: Session, org: Organisation, invoice_id: int):
    inv = invoicing.get(session, org, invoice_id)
    if not inv:
        raise HTTPException(404, "Factuur niet gevonden")
    return inv


@router.get("/invoices")
def list_invoices(status: str | None = None, org: Organisation = Depends(require_org), session: Session = Depends(get_session)):
    """concept | goedgekeurd | verzonden | betaald"""
    return [_invoice_out(i) for i in invoicing.list_invoices(session, org, status)]


@router.get("/invoices/{invoice_id}")
def get_invoice(invoice_id: int, org: Organisation = Depends(require_org), session: Session = Depends(get_session)):
    return _invoice_out(_inv(session, org, invoice_id))


@router.post("/invoices/{invoice_id}/approve")
def approve_invoice(invoice_id: int, org: Organisation = Depends(require_org), session: Session = Depends(get_session)):
    try:
        return _invoice_out(invoicing.approve(session, org, _inv(session, org, invoice_id), actor="api"))
    except invoicing.InvoiceError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/invoices/{invoice_id}/peppol")
def send_invoice_peppol(invoice_id: int, org: Organisation = Depends(require_org), session: Session = Depends(get_session)):
    """Naar het Peppol access point; zonder koppeling: peppol_status 'klaar' (haal de UBL op via .../ubl.xml)."""
    try:
        return _invoice_out(invoicing.send_peppol(session, org, _inv(session, org, invoice_id), actor="api"))
    except invoicing.InvoiceError as exc:
        raise HTTPException(409, str(exc)) from exc


class PaidIn(BaseModel):
    on: date | None = Field(None, description="betaaldatum; vandaag (standaard) of vroeger")


@router.post("/invoices/{invoice_id}/paid")
def invoice_paid(invoice_id: int, body: PaidIn, org: Organisation = Depends(require_org), session: Session = Depends(get_session)):
    try:
        return _invoice_out(invoicing.mark_paid(session, org, _inv(session, org, invoice_id), body.on, actor="api"))
    except invoicing.InvoiceError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/invoices/{invoice_id}/invoice.pdf")
def invoice_pdf(invoice_id: int, org: Organisation = Depends(require_org), session: Session = Depends(get_session)):
    return Response(invoicing.invoice_pdf(session, org, _inv(session, org, invoice_id)), media_type="application/pdf")


@router.get("/invoices/{invoice_id}/ubl.xml")
def invoice_ubl(invoice_id: int, org: Organisation = Depends(require_org), session: Session = Depends(get_session)):
    return Response(invoicing.ubl(session, org, _inv(session, org, invoice_id)), media_type="application/xml")
