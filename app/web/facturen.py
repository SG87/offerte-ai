"""Web routes for invoices: the Facturen tab, the invoice page (edit by hand or with the AI, approve, send over
Peppol or by mail, mark paid) and the PDF / UBL downloads."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlmodel import Session, select

from app import ai_jobs, i18n, invoicing, services
from app.config import get_settings
from app.db import get_session
from app.models import Organisation, OutboxMessage, Project
from app.web.routes import _ai_started, _f, _flash, _render, _require, current_org

router = APIRouter(include_in_schema=False)


def _invoice(session: Session, org: Organisation, invoice_id: int):
    return invoicing.get(session, org, invoice_id)


@router.get("/facturen", response_class=HTMLResponse)
def facturen(
    request: Request,
    status: str | None = None,
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    if r := _require(request, org):
        return r
    rows = invoicing.list_invoices(session, org)
    counts = {k: sum(1 for i in rows if i.status == k) for k in invoicing.STATUSES}
    shown = [i for i in rows if not status or i.status == status]
    return _render(
        request,
        "facturen.html",
        org=org,
        invoices=[(i, invoicing.totals(i), invoicing.due_date(i)) for i in shown],
        statuses=invoicing.STATUSES,
        counts=counts,
        status=status,
        today=date.today(),
        open_amount=sum(invoicing.totals(i)["total"] for i in rows if i.status in ("goedgekeurd", "verzonden")),
    )


@router.get("/facturen/{invoice_id}", response_class=HTMLResponse)
def factuur(
    invoice_id: int,
    request: Request,
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    if r := _require(request, org):
        return r
    inv = _invoice(session, org, invoice_id)
    if not inv:
        return RedirectResponse("/facturen", status_code=303)
    lang = inv.language
    project = session.get(Project, inv.project_id) if inv.project_id else None
    return _render(
        request,
        "factuur.html",
        org=org,
        inv=inv,
        project=project,
        tot=invoicing.totals(inv),
        lines=invoicing.display_lines(inv, not inv.b2b),
        due=invoicing.due_date(inv),
        statuses=invoicing.STATUSES,
        language_name=i18n.LANGUAGES.get(lang, lang),
        t=lambda key, **kw: i18n.t(lang, key, **kw),
        peppol_missing=invoicing.peppol_missing(org, inv),
        peppol_ready=get_settings().peppol_ready,
        smtp_ready=get_settings().smtp_ready,
        mail_defaults=invoicing.mail_defaults(org, inv),
        mails=session.exec(
            select(OutboxMessage).where(OutboxMessage.invoice_id == inv.id).order_by(OutboxMessage.id.desc())
        ).all(),
        ai_job=next((j for j in ai_jobs.running(session, org, kind="factuur") if j.ref_id == inv.id), None),
        today=date.today(),
    )


@router.post("/facturen/{invoice_id}")
async def factuur_save(
    invoice_id: int,
    request: Request,
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    """Manual edit: rows line_<i>_<field>; a row with an empty description is dropped."""
    if r := _require(request, org):
        return r
    inv = _invoice(session, org, invoice_id)
    if not inv:
        return RedirectResponse("/facturen", status_code=303)
    form = await request.form()
    lines, i = [], 0
    while f"line_{i}_description" in form:
        if not form.get(f"line_{i}_delete"):
            lines.append(
                {
                    "description": form.get(f"line_{i}_description") or "",
                    "qty": _f(form.get(f"line_{i}_qty"), 1.0),
                    "unit": form.get(f"line_{i}_unit") or "",
                    "unit_price": _f(form.get(f"line_{i}_unit_price"), 0.0),
                    "vat_pct": _f(form.get(f"line_{i}_vat_pct"), 21.0),
                }
            )
        i += 1
    try:
        invoicing.save(
            session, inv, lines,
            notes=form.get("notes"),
            due_days=int(_f(form.get("due_days"), inv.due_days)),
            title=form.get("title"),
            client_name=form.get("client_name"),
            client_address=form.get("client_address"),
            client_vat=form.get("client_vat"),
        )
        _flash(request, "Factuur bewaard.")
    except invoicing.InvoiceError as exc:
        _flash(request, str(exc))
    return RedirectResponse(f"/facturen/{invoice_id}", status_code=303)


@router.post("/facturen/{invoice_id}/ai")
async def factuur_ai(
    invoice_id: int,
    request: Request,
    background: BackgroundTasks,
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    if r := _require(request, org):
        return r
    inv = _invoice(session, org, invoice_id)
    instruction = ((await request.form()).get("instruction") or "").strip()
    if not inv or not instruction:
        return RedirectResponse(f"/facturen/{invoice_id}", status_code=303)
    if not get_settings().ai_enabled:
        _flash(request, "AI staat uit (geen ANTHROPIC_API_KEY): pas de factuur manueel aan.")
        return RedirectResponse(f"/facturen/{invoice_id}", status_code=303)
    try:
        job, work = invoicing.start_ai_edit(session, org, inv, instruction)
        background.add_task(ai_jobs.run, job.id, work)
        _ai_started(request, f"Factuur aanpassen: {instruction[:60]}")
    except invoicing.InvoiceError as exc:
        _flash(request, str(exc))
    return RedirectResponse(f"/facturen/{invoice_id}", status_code=303)


def _action(invoice_id: int, request: Request, org, session, fn, ok: str):
    if r := _require(request, org):
        return r
    inv = _invoice(session, org, invoice_id)
    if not inv:
        return RedirectResponse("/facturen", status_code=303)
    try:
        fn(inv)
        _flash(request, ok.format(inv=inv))
    except invoicing.InvoiceError as exc:
        _flash(request, str(exc))
    return RedirectResponse(f"/facturen/{invoice_id}", status_code=303)


@router.post("/facturen/{invoice_id}/goedkeuren")
def factuur_approve(invoice_id: int, request: Request, org: Organisation | None = Depends(current_org),
                    session: Session = Depends(get_session)):
    return _action(invoice_id, request, org, session, lambda inv: invoicing.approve(session, org, inv),
                   "Factuur {inv.number} goedgekeurd.")


@router.post("/facturen/{invoice_id}/concept")
def factuur_unapprove(invoice_id: int, request: Request, org: Organisation | None = Depends(current_org),
                      session: Session = Depends(get_session)):
    return _action(invoice_id, request, org, session, lambda inv: invoicing.back_to_concept(session, org, inv),
                   "Factuur {inv.number} is weer een concept.")


@router.post("/facturen/{invoice_id}/peppol")
def factuur_peppol(invoice_id: int, request: Request, org: Organisation | None = Depends(current_org),
                   session: Session = Depends(get_session)):
    def go(inv):
        invoicing.send_peppol(session, org, inv)
        if inv.peppol_status == "mislukt":
            raise invoicing.InvoiceError(f"Peppol mislukt: {inv.peppol_error}")

    msg = "Factuur {inv.number} via Peppol verstuurd." if get_settings().peppol_ready else \
        "Factuur {inv.number} is klaar voor Peppol: download de UBL en laad ze op bij je access point, of stel OFFERTE_PEPPOL_API_URL in."
    return _action(invoice_id, request, org, session, go, msg)


@router.post("/facturen/{invoice_id}/mail")
async def factuur_mail(invoice_id: int, request: Request, org: Organisation | None = Depends(current_org),
                       session: Session = Depends(get_session)):
    form = await request.form()
    to, subject, body = (form.get("to") or "").strip(), form.get("subject") or "", form.get("body") or ""

    def go(inv):
        if not to:
            raise invoicing.InvoiceError("Vul een e-mailadres in.")
        msg = invoicing.mail(session, org, inv, to, subject, body)
        if msg.status == "mislukt":
            raise invoicing.InvoiceError(f"Mail mislukt: {msg.error}")

    ok = "Factuur {inv.number} gemaild naar " + to + "." if get_settings().smtp_ready else \
        "Mail met factuur {inv.number} klaargezet in de outbox (nog geen mailserver)."
    return _action(invoice_id, request, org, session, go, ok)


@router.post("/facturen/{invoice_id}/verstuurd")
def factuur_sent(invoice_id: int, request: Request, org: Organisation | None = Depends(current_org),
                 session: Session = Depends(get_session)):
    return _action(invoice_id, request, org, session, lambda inv: invoicing.mark_sent(session, org, inv, "manueel"),
                   "Factuur {inv.number} gemarkeerd als verstuurd.")


@router.post("/facturen/{invoice_id}/betaald")
async def factuur_paid(invoice_id: int, request: Request, org: Organisation | None = Depends(current_org),
                       session: Session = Depends(get_session)):
    raw = (await request.form()).get("on")

    def go(inv):
        try:
            on = date.fromisoformat(raw) if raw else None
        except ValueError:
            raise invoicing.InvoiceError(f"Ongeldige datum '{raw}'.") from None
        invoicing.mark_paid(session, org, inv, on)

    return _action(invoice_id, request, org, session, go, "Factuur {inv.number} betaald.")


@router.post("/facturen/{invoice_id}/verwijder")
def factuur_delete(invoice_id: int, request: Request, org: Organisation | None = Depends(current_org),
                   session: Session = Depends(get_session)):
    if r := _require(request, org):
        return r
    inv = _invoice(session, org, invoice_id)
    try:
        if inv:
            invoicing.delete_concept(session, org, inv)
            _flash(request, "Conceptfactuur verwijderd.")
    except invoicing.InvoiceError as exc:
        _flash(request, str(exc))
        return RedirectResponse(f"/facturen/{invoice_id}", status_code=303)
    return RedirectResponse("/facturen", status_code=303)


@router.post("/projects/{project_id}/factuur")
def project_invoice(project_id: int, request: Request, org: Organisation | None = Depends(current_org),
                    session: Session = Depends(get_session)):
    """Make (or open) the invoice of a project by hand, e.g. for work finished before invoices existed."""
    if r := _require(request, org):
        return r
    p = services.get_project(session, org, project_id)
    inv = invoicing.create_from_project(session, org, p, actor="gebruiker") if p else None
    if not inv:
        _flash(request, "Geen offerte in dit project: bereken eerst de offerte.")
        return RedirectResponse(f"/projects/{project_id}", status_code=303)
    return RedirectResponse(f"/facturen/{inv.id}", status_code=303)


@router.get("/facturen/{invoice_id}/factuur.pdf")
def factuur_pdf(invoice_id: int, request: Request, org: Organisation | None = Depends(current_org),
                session: Session = Depends(get_session)):
    if r := _require(request, org):
        return r
    inv = _invoice(session, org, invoice_id)
    if not inv:
        return RedirectResponse("/facturen", status_code=303)
    return Response(invoicing.invoice_pdf(session, org, inv), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="factuur-{inv.number or "concept"}.pdf"'})


@router.get("/facturen/{invoice_id}/ubl.xml")
def factuur_ubl(invoice_id: int, request: Request, org: Organisation | None = Depends(current_org),
                session: Session = Depends(get_session)):
    if r := _require(request, org):
        return r
    inv = _invoice(session, org, invoice_id)
    if not inv:
        return RedirectResponse("/facturen", status_code=303)
    return Response(invoicing.ubl(session, org, inv), media_type="application/xml",
                    headers={"Content-Disposition": f'attachment; filename="factuur-{inv.number or "concept"}.xml"'})
