"""Public page for the supplier: behind the secret link in the order mail, confirm the order and give the delivery
per line. No login; the token in the URL is the key. The answer can be updated while the order is placed."""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlmodel import Session

from app import branding, workflow
from app.db import get_session
from app.integrations import stock
from app.models import Supplier
from app.web.routes import templates

router = APIRouter(include_in_schema=False)


def _not_found() -> HTMLResponse:
    return HTMLResponse("<!doctype html><meta charset=utf-8><title>Bestelling</title><p style='font-family:sans-serif;padding:24px'>"
                        "Deze link is niet (meer) geldig.</p>", status_code=404)


def _render(request: Request, session: Session, org, order, error: str | None = None, form=None, status_code: int = 200):
    supplier = session.get(Supplier, order.supplier_id) if order.supplier_id else None
    default_date = date.today() + timedelta(days=supplier.lead_days if supplier else 3)
    rows = []
    for ln in stock.order_lines(session, order):
        if form is not None:  # show what was submitted after an error
            status, when, note = form.get(f"status_{ln.id}"), form.get(f"datum_{ln.id}"), form.get(f"opmerking_{ln.id}")
        else:
            status, when, note = ln.delivery_status or "leverbaar", (ln.delivery_date or default_date).isoformat(), ln.delivery_note
        rows.append({"line": ln, "status": status, "date": when or "", "note": note or ""})
    return templates.TemplateResponse(
        request,
        "leverancier_bestelling.html",
        {
            "company": org,
            "order": order,
            "supplier": supplier,
            "rows": rows,
            "statuses": stock.DELIVERY_STATUSES,
            "contact": (form.get("naam") if form is not None else order.supplier_contact) or "",
            "note": (form.get("opmerking") if form is not None else order.supplier_note) or "",
            "error": error,
            "saved": request.query_params.get("ok") == "1",
            "editable": order.status == "besteld",
            "theme": branding.theme_vars(org.brand_primary),
        },
        status_code=status_code,
    )


@router.get("/l/{token}", response_class=HTMLResponse)
def supplier_order(token: str, request: Request, session: Session = Depends(get_session)):
    if not (found := workflow.order_by_token(session, token)):
        return _not_found()
    return _render(request, session, *found)


@router.post("/l/{token}")
async def supplier_answer(token: str, request: Request, session: Session = Depends(get_session)):
    if not (found := workflow.order_by_token(session, token)):
        return _not_found()
    org, order = found
    form = await request.form()
    lines = {}
    for ln in stock.order_lines(session, order):
        try:
            when = date.fromisoformat(form.get(f"datum_{ln.id}") or "")
        except ValueError:
            when = None
        lines[ln.id] = (form.get(f"status_{ln.id}"), when, form.get(f"opmerking_{ln.id}"))
    ok, msg = workflow.supplier_response(session, org, order, lines, form.get("naam"), form.get("opmerking"))
    if not ok:
        return _render(request, session, org, order, error=msg, form=form, status_code=422)
    return RedirectResponse(f"/l/{token}?ok=1", status_code=303)


@router.get("/l/{token}/logo")
def supplier_logo(token: str, session: Session = Depends(get_session)):
    found = workflow.order_by_token(session, token)
    path = branding.logos_dir() / found[0].logo_file if found and found[0].logo_file else None
    if path is None or not path.is_file():
        return Response(status_code=404)
    return FileResponse(
        path,
        headers={
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "public, max-age=3600",
        },
    )
