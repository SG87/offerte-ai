"""Account settings: company data, login e-mail, sector, house style, AI re-lookup, API key, integrations."""

from __future__ import annotations

import re
import secrets

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session

from app import ai_jobs, branding, services
from app.config import get_settings
from app.db import get_session
from app.i18n import LANGUAGES
from app.models import Organisation
from app.web.routes import _ai_started, _flash, _render, _require, current_org

router = APIRouter(include_in_schema=False)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
COUNTRIES = {"BE": "België", "NL": "Nederland"}


def _back(section: str) -> RedirectResponse:
    return RedirectResponse(f"/account#{section}", status_code=303)


@router.get("/account", response_class=HTMLResponse)
def account(request: Request, org: Organisation | None = Depends(current_org), session: Session = Depends(get_session)):
    if r := _require(request, org):
        return r
    settings = get_settings()
    return _render(
        request,
        "account.html",
        org=org,
        sectors=services.available_sector_list(),
        languages=LANGUAGES,
        countries=COUNTRIES,
        lookup_job=next(iter(ai_jobs.running(session, org, kind="bedrijf")), None),
        integrations=[
            ("Claude (AI)", settings.ai_enabled, f"model {settings.anthropic_model}" if settings.ai_enabled else "zet ANTHROPIC_API_KEY in .env"),
            (
                "Mailserver",
                settings.smtp_ready,
                (f"{settings.smtp_host} als {settings.smtp_from or settings.smtp_user or org.email}" if settings.smtp_ready
                 else "OFFERTE_SMTP_PASSWORD ontbreekt (Google: app-wachtwoord)" if settings.smtp_host
                 else "geen OFFERTE_SMTP_HOST: mails blijven in de outbox")
                + (f" · TESTMODUS: alle mails gaan naar {settings.mail_redirect_to}" if settings.mail_redirect_to else ""),
            ),
            ("Webhook (voorraad-agent, planningstool)", bool(settings.webhook_url), settings.webhook_url or "geen OFFERTE_WEBHOOK_URL: tools halen events op via de API"),
            ("MCP-server", True, f"{settings.base_url.rstrip('/')}/mcp" + ("" if settings.mcp_org_email in (None, org.email) else f" (werkt nu voor {settings.mcp_org_email})")),
        ],
        base_url=settings.base_url.rstrip("/"),
    )


@router.post("/account/gegevens")
def account_details(
    request: Request,
    company_name: str | None = Form(None),
    address: str | None = Form(None),
    vat_number: str | None = Form(None),
    phone: str | None = Form(None),
    website: str | None = Form(None),
    iban: str | None = Form(None),
    bic: str | None = Form(None),
    email: str = Form(...),
    purchase_contact_email: str | None = Form(None),
    language: str = Form("nl"),
    country: str = Form("BE"),
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    if r := _require(request, org):
        return r
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        _flash(request, "Dat is geen geldig e-mailadres.")
        return _back("gegevens")
    if email != org.email and services.get_org_by_email(session, email):
        _flash(request, f"Er bestaat al een account voor {email}.")
        return _back("gegevens")
    purchase = (purchase_contact_email or "").strip().lower() or None
    if purchase and not EMAIL_RE.match(purchase):
        _flash(request, "Het e-mailadres van de contactpersoon aankoop is niet geldig.")
        return _back("gegevens")
    changed_login = email != org.email
    org.email = email
    org.company_name = (company_name or "").strip() or None
    org.address = (address or "").strip() or None
    org.vat_number = re.sub(r"[\s.]", "", vat_number or "").upper() or None
    org.phone = (phone or "").strip() or None
    org.iban = re.sub(r"\s", "", iban or "").upper() or None
    org.bic = (bic or "").strip().upper() or None
    org.website = (website or "").strip() or None
    org.purchase_contact_email = purchase
    org.language = language if language in LANGUAGES else org.language
    org.country = country if country in COUNTRIES else org.country
    session.add(org)
    session.commit()
    _flash(request, "Bedrijfsgegevens bewaard." + (f" Je meldt je voortaan aan met {email}." if changed_login else ""))
    return _back("gegevens")


@router.post("/account/huisstijl")
async def account_branding(
    request: Request,
    brand_primary: str | None = Form(None),
    brand_secondary: str | None = Form(None),
    remove_logo: str | None = Form(None),
    logo: UploadFile | None = File(None),
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    if r := _require(request, org):
        return r
    msg = "Huisstijl bewaard."
    if remove_logo == "on" and org.logo_file:
        (branding.logos_dir() / org.logo_file).unlink(missing_ok=True)
        org.logo_file = None
        msg = "Logo verwijderd."
    elif logo is not None and logo.filename:
        if not services.set_logo(session, org, await logo.read(), logo.content_type):
            msg = "Dat logo kon niet gebruikt worden (svg, png, jpg of webp, max. 2 MB)."
    org.brand_primary = branding.normalize_hex(brand_primary) or org.brand_primary
    org.brand_secondary = branding.normalize_hex(brand_secondary) or org.brand_secondary
    session.add(org)
    session.commit()
    _flash(request, msg)
    return _back("huisstijl")


@router.post("/account/sector")
def account_sector(
    request: Request,
    sector: str = Form(...),
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    if r := _require(request, org):
        return r
    known = {s["sector"] for s in services.available_sector_list()}
    if sector not in known or sector == org.sector:
        return _back("sector")
    org.sector = sector
    session.add(org)
    session.commit()
    row = services.ensure_config(session, org)  # new price-list version with this sector's defaults
    _flash(request, f"Sector gewijzigd. Prijslijst v{row.version} met de standaardprijzen van de sector; pas ze aan onder Configuratie.")
    return _back("sector")


@router.post("/account/opzoeken")
def account_lookup(
    request: Request,
    background: BackgroundTasks,
    overwrite: str | None = Form(None),
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    if r := _require(request, org):
        return r
    if ai_jobs.running(session, org, kind="bedrijf"):
        _flash(request, "De opzoeking loopt al.")
        return _back("opzoeken")
    job, work = services.start_company_lookup(session, org, overwrite=overwrite == "on")
    background.add_task(ai_jobs.run, job.id, work)
    _ai_started(request, f"We halen je bedrijfsgegevens opnieuw op via {org.email.split('@')[-1]}")
    return _back("opzoeken")


@router.post("/account/testmail")
def account_testmail(
    request: Request,
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    """Send a test mail to the account's own address to check the SMTP settings."""
    from app.integrations import mail

    if r := _require(request, org):
        return r
    if not get_settings().smtp_ready:
        _flash(request, "Er is nog geen mailserver klaar: vul OFFERTE_SMTP_HOST (en bij Gmail het app-wachtwoord) in .env in.")
        return _back("integraties")
    msg = mail.queue_mail(
        session, org, org.email, "Testmail van Offerte-AI",
        "Deze testmail bevestigt dat je mailserver werkt. Offertes en bestelmails kunnen nu verstuurd worden.\n",
        kind="test",
    )
    _flash(request, f"Testmail verzonden naar {msg.sent_to}." if msg.status == "verzonden" else f"Testmail mislukt: {msg.error}")
    return _back("integraties")


@router.post("/account/api-key")
def account_api_key(
    request: Request,
    confirm: str | None = Form(None),
    org: Organisation | None = Depends(current_org),
    session: Session = Depends(get_session),
):
    if r := _require(request, org):
        return r
    if confirm != "on":
        _flash(request, "Vink eerst aan dat je de oude sleutel wil laten vervallen.")
        return _back("api")
    org.api_key = secrets.token_urlsafe(24)
    session.add(org)
    session.commit()
    _flash(request, "Nieuwe API-sleutel aangemaakt. De oude werkt niet meer; pas je integraties aan.")
    return _back("api")
