"""Public pages for the customer: the quote behind the secret link from the mail, to accept or decline it online.

No login: the token in the URL is the key (one per quote version). Answering needs a POST with a confirming
click, so link scanners that open the buttons in the mail never accept or decline anything.
"""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import quote as urlquote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlmodel import Session

from app import branding, i18n, planning, services, workflow
from app.db import get_session
from app.engine import load_skill
from app.outputs import calendar
from app.outputs.offer_lines import offer_lines, offer_totals
from app.web.routes import templates

router = APIRouter(include_in_schema=False)


def _not_found() -> HTMLResponse:
    return HTMLResponse("<!doctype html><meta charset=utf-8><title>Offerte</title><p style='font-family:sans-serif;padding:24px'>"
                        "Deze link is niet (meer) geldig. / Ce lien n'est plus valable. / This link is no longer valid.</p>",
                        status_code=404)


def _client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else None)


def _calendar_event(org, project, quote, lang: str) -> calendar.CalendarEvent | None:
    """The proposed execution period as an all-day event; None without a proposal."""
    if not project.proposed_start:
        return None
    tr = services.translations_for(quote, lang)
    company = org.company_name or org.email
    title = i18n.t(lang, "agenda_titel", title=tr.get(project.name, project.name), company=company)
    if quote.decision != "aanvaard":
        title += i18n.t(lang, "agenda_voorgesteld")
    return calendar.CalendarEvent(
        uid=f"{quote.public_token}@offer-ai",
        title=title,
        start=project.proposed_start,
        end=project.proposed_end or project.proposed_start,
        description=f"{planning.proposal_sentence(project, {}, lang)}\n\n{workflow.public_url_for(quote)}",
        location=project.client_address or "",
    )


@router.get("/o/{token}", response_class=HTMLResponse)
def customer_quote(token: str, request: Request, session: Session = Depends(get_session)):
    if not (found := workflow.quote_by_token(session, token)):
        return _not_found()
    org, project, quote = found
    customer = services.project_customer(session, project)
    lang = services.quote_language(org, customer)
    tr = services.translations_for(quote, lang)
    result = services.quote_result(quote)
    keuze = request.query_params.get("keuze")
    return templates.TemplateResponse(
        request,
        "klant_offerte.html",
        {
            "company": org,
            "project": project,
            "quote": quote,
            "result": result,
            "customer": customer,
            "incl_vat": services.prices_incl_vat(customer),
            "lang": lang,
            "t": lambda key, **kw: i18n.t(lang, key, **kw),
            "tr": lambda text: tr.get(text, text),
            "intro": services.offer_intro(project, lang, tr),
            "offer_lines": offer_lines,
            "offer_totals": offer_totals,
            "valid_until": result.created_on + timedelta(days=services.current_config_validity(load_skill(org.sector))),
            "blocked": None if quote.decision else workflow.answer_blocked(session, project, quote),
            "keuze": keuze if keuze in ("aanvaard", "geweigerd") else None,
            "fout": request.query_params.get("fout") if request.query_params.get("fout") in i18n.STRINGS else None,
            "theme": branding.theme_vars(org.brand_primary),
            "agenda": (ev := _calendar_event(org, project, quote, lang))
            and {"google": calendar.google_url(ev), "outlook": calendar.outlook_url(ev)},
        },
    )


@router.post("/o/{token}")
def customer_answer(
    token: str,
    request: Request,
    keuze: str = Form(""),
    naam: str = Form(""),
    akkoord: str | None = Form(None),
    opmerking: str = Form(""),
    session: Session = Depends(get_session),
):
    if not (found := workflow.quote_by_token(session, token)):
        return _not_found()
    org, project, quote = found
    back = f"/o/{urlquote(token)}"
    if keuze == "aanvaard" and (akkoord != "on" or not naam.strip()):
        return RedirectResponse(f"{back}?keuze=aanvaard&fout=naam_verplicht#antwoord", status_code=303)
    try:
        workflow.customer_decision(session, org, project, quote, keuze, naam, opmerking, ip=_client_ip(request))
    except workflow.TransitionError as exc:
        fout = str(exc) if str(exc) in i18n.STRINGS else ""
        return RedirectResponse(f"{back}?keuze={urlquote(keuze)}&fout={fout}#antwoord", status_code=303)
    return RedirectResponse(back, status_code=303)  # the answer shows at the top


@router.get("/o/{token}/offerte.pdf")
def customer_pdf(token: str, session: Session = Depends(get_session)):
    if not (found := workflow.quote_by_token(session, token)):
        return _not_found()
    org, project, quote = found
    return Response(
        services.offerte_pdf(session, org, project, quote),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="offerte-{quote.number}-v{quote.version}.pdf"'},
    )


@router.get("/o/{token}/agenda.ics")
def customer_calendar(token: str, session: Session = Depends(get_session)):
    if not (found := workflow.quote_by_token(session, token)):
        return _not_found()
    org, project, quote = found
    ev = _calendar_event(org, project, quote, services.quote_language(org, services.project_customer(session, project)))
    if not ev:
        return Response(status_code=404)
    return Response(
        calendar.build_ics(ev),
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="uitvoering-{quote.number}.ics"'},
    )


@router.get("/o/{token}/logo")
def customer_logo(token: str, session: Session = Depends(get_session)):
    """The company logo for the answer page and the quote mail (mail clients load it without a session)."""
    found = workflow.quote_by_token(session, token)
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
