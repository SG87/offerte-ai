"""Project lifecycle: statuses, allowed transitions, history and the triggers that fire on a status.

offerte voorbereid -> gemaakt -> gestuurd -> aanvaard -> werk in uitvoering -> werk afgerond
  -> factuur verstuurd -> factuur betaald -> afgerond
Geweigerd ends the project (from any quote stage). Each forward step can be undone one step (misclicks);
triggers are idempotent per project, so going back and forth never sends the purchase list twice.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlmodel import Session, select

from app import todos
from app.config import get_settings
from app.i18n import t as i18n_t
from app.integrations import events, mail, stock
from app.models import Organisation, Project, ProjectEvent, Quote
from app.schemas import QuoteResult


@dataclass(frozen=True)
class Status:
    key: str
    label: str
    phase: str  # offerte | uitvoering | facturatie | afgesloten
    action: str  # button text to move *into* this status


STATUSES = [
    Status("voorbereid", "Offerte voorbereid", "offerte", "Terug naar voorbereiding"),
    Status("gemaakt", "Offerte gemaakt", "offerte", "Offerte gemaakt"),
    Status("gestuurd", "Offerte gestuurd", "offerte", "Markeer offerte als gestuurd"),
    Status("aanvaard", "Offerte aanvaard", "uitvoering", "Offerte aanvaard"),
    Status("in_uitvoering", "Werk in uitvoering", "uitvoering", "Start het werk"),
    Status("werk_afgerond", "Werk afgerond", "uitvoering", "Werk afgerond"),
    Status("factuur_verstuurd", "Factuur verstuurd", "facturatie", "Factuur verstuurd"),
    Status("factuur_betaald", "Factuur betaald", "facturatie", "Factuur betaald"),
    Status("afgerond", "Afgerond", "afgesloten", "Project afronden"),
    Status("geweigerd", "Offerte geweigerd", "afgesloten", "Offerte geweigerd"),
]
BY_KEY = {s.key: s for s in STATUSES}
PIPELINE = [s.key for s in STATUSES if s.key != "geweigerd"]  # the happy path, in order
PHASES = [
    ("offerte", "Offerte"),
    ("uitvoering", "Uitvoering"),
    ("facturatie", "Facturatie"),
    ("afgesloten", "Afgesloten"),
]
TERMINAL = {"afgerond", "geweigerd"}
REFUSABLE = {"voorbereid", "gemaakt", "gestuurd"}
NEEDS_QUOTE = {"gemaakt", "gestuurd", "aanvaard"}
LEGACY = {"nieuw": "voorbereid", "gemeten": "voorbereid", "offerte": "gemaakt", "verzonden": "gestuurd"}


class TransitionError(ValueError):
    pass


# statuses that record when it really happened: the user gives the date, today or earlier
DATED = {"in_uitvoering": ("work_started_on", "gestart"), "werk_afgerond": ("work_done_on", "afgerond")}


def label(key: str) -> str:
    return BY_KEY[key].label if key in BY_KEY else key


def next_status(key: str) -> str | None:
    if key in TERMINAL or key not in PIPELINE:
        return None
    i = PIPELINE.index(key)
    return PIPELINE[i + 1] if i + 1 < len(PIPELINE) else None


def previous_status(key: str) -> str | None:
    if key in TERMINAL or key not in PIPELINE:
        return None
    i = PIPELINE.index(key)
    return PIPELINE[i - 1] if i > 0 else None


def allowed(key: str) -> list[str]:
    """Statuses reachable from `key`: forward, refuse, one step back."""
    out = [s for s in (next_status(key),) if s]
    if key in REFUSABLE:
        out.append("geweigerd")
    if prev := previous_status(key):
        out.append(prev)
    return out


def _latest_quote(session: Session, project: Project) -> Quote | None:
    return session.exec(select(Quote).where(Quote.project_id == project.id).order_by(Quote.id.desc())).first()


def log(
    session: Session,
    project: Project,
    text: str,
    kind: str = "info",
    actor: str = "systeem",
    ref_id: int | None = None,
    from_status: str | None = None,
    to_status: str | None = None,
    link: str | None = None,
) -> ProjectEvent:
    ev = ProjectEvent(
        project_id=project.id,
        kind=kind,
        text=text,
        actor=actor,
        ref_id=ref_id,
        from_status=from_status,
        to_status=to_status,
        link=link,
    )
    session.add(ev)
    session.commit()
    return ev


def history(session: Session, project: Project) -> list[ProjectEvent]:
    return list(
        session.exec(
            select(ProjectEvent).where(ProjectEvent.project_id == project.id).order_by(ProjectEvent.id.desc())
        ).all()
    )


def change_status(
    session: Session,
    org: Organisation,
    project: Project,
    to: str,
    note: str | None = None,
    actor: str = "gebruiker",
    on: date | None = None,
) -> Project:
    """Validate, record, then run the triggers of the new status. Trigger failures never undo the change.
    `on`: the day the work really started / was finished (DATED statuses; default today, never in the future)."""
    current = LEGACY.get(project.status, project.status)
    if to not in BY_KEY:
        raise TransitionError(f"Onbekende status '{to}'.")
    if to == current:
        return project
    if to not in allowed(current):
        raise TransitionError(f"Van '{label(current)}' kan je niet naar '{label(to)}'.")
    if to in NEEDS_QUOTE and _latest_quote(session, project) is None:
        raise TransitionError(f"'{label(to)}' kan pas als er een offerte berekend is.")
    when = ""
    back = to == previous_status(current)
    if to in DATED and not (back and on is None and getattr(project, DATED[to][0])):  # a step back keeps the date
        field, verb = DATED[to]
        on = on or date.today()
        if on > date.today():
            raise TransitionError(f"Werk kan niet {verb} zijn op {on:%d/%m/%Y}: kies vandaag of een dag in het verleden.")
        if to == "werk_afgerond" and project.work_started_on and on < project.work_started_on:
            raise TransitionError(
                f"Afgerond op {on:%d/%m/%Y} kan niet: het werk is gestart op {project.work_started_on:%d/%m/%Y}."
            )
        setattr(project, field, on)
        when = f" ({verb} op {on:%d/%m/%Y})"
    if back and current in DATED:
        setattr(project, DATED[current][0], None)  # undone: that date no longer holds
    project.status, project.status_changed_at = to, datetime.now(UTC)
    session.add(project)
    session.commit()
    text = f"{label(current)} → {label(to)}{when}" + (f": {note.strip()}" if note and note.strip() else "")
    log(session, project, text, kind="status", actor=actor, from_status=current, to_status=to)
    triggers = list(ON_ENTER.get(to, []))
    if to == previous_status(current):
        triggers = list(ON_LEAVE_BACKWARDS.get(current, [])) + triggers
    for trigger in triggers:
        try:
            trigger(session, org, project)
        except Exception as exc:  # a broken integration must not block the project
            session.rollback()
            log(session, project, f"Trigger {trigger.__name__} mislukt: {type(exc).__name__}: {exc}", kind="trigger")
    return project


def on_quote_calculated(session: Session, project: Project, quote: Quote) -> None:
    """A calculated quote moves a project in preparation to 'gemaakt'; later recalculations are only logged."""
    log(session, project, f"Offerte {quote.number} v{quote.version} berekend.")
    if LEGACY.get(project.status, project.status) == "voorbereid":
        project.status, project.status_changed_at = "gemaakt", datetime.now(UTC)
        session.add(project)
        session.commit()
        log(session, project, "Offerte voorbereid → Offerte gemaakt", kind="status", from_status="voorbereid", to_status="gemaakt")


# ---------- quote by mail ----------


def quote_mail_defaults(session: Session, org: Organisation, project: Project, quote: Quote) -> dict[str, str]:
    """Pre-filled recipient, subject and text for mailing the quote, in the customer's language (editable in the
    form). The accept/decline buttons are added around this text when the mail is sent."""
    from app import i18n, planning, services

    customer = services.project_customer(session, project)
    lang = services.quote_language(org, customer)
    tr = services.translations_for(quote, lang)
    title = tr.get(project.name, project.name)
    company = org.company_name or org.email
    proposal = planning.proposal_sentence(project, {}, lang)
    name = (customer.contact_name if customer else None) or project.client_name or i18n.t(lang, "klant_aanspreking")
    body = (
        i18n.t(lang, "mail_aanhef", name=name) + "\n\n"
        + i18n.t(lang, "mail_bijlage", number=quote.number, title=title[:1].lower() + title[1:]) + "\n"
        + (f"{proposal}\n" if proposal else "")
        + "\n" + i18n.t(lang, "mail_vragen") + "\n\n"
        + i18n.t(lang, "mail_groet") + f"\n{company}\n"
        + (f"{org.phone}\n" if org.phone else "")
        + (f"{org.email}\n" if org.company_name else "")
    )
    return {
        "to": (customer.email if customer else None) or "",
        "subject": i18n.t(lang, "mail_onderwerp", number=quote.number, title=title) + f" — {company}",
        "body": body,
    }


def public_url_for(quote: Quote) -> str:
    """The answer page of a quote that already has its token."""
    return f"{get_settings().base_url.rstrip('/')}/o/{quote.public_token}"


def public_url(session: Session, quote: Quote) -> str:
    """The customer's answer page for this quote version; the secret token is made on first use."""
    if not quote.public_token:
        quote.public_token = secrets.token_urlsafe(24)
        session.add(quote)
        session.commit()
    return public_url_for(quote)


def quote_mail(session: Session, org: Organisation, project: Project, quote: Quote, subject: str, body: str):
    """Everything the HTML quote mail needs (built at send time, so a mail from the outbox gets the same)."""
    from app import branding, services
    from app.outputs.offer_lines import offer_totals
    from app.outputs.quote_mail import QuoteMail, eur

    customer = services.project_customer(session, project)
    lang = services.quote_language(org, customer)
    tr = services.translations_for(quote, lang)
    view = public_url(session, quote)
    headline = next((row for row in offer_totals(services.quote_result(quote), services.prices_incl_vat(customer), lang) if row[2]), None)
    # most mail clients block SVG images, so an SVG logo falls back to the company name
    logo = f"{view}/logo" if org.logo_file and not org.logo_file.endswith(".svg") else None
    return QuoteMail(
        lang=lang,
        company=org.company_name or org.email,
        subject=subject,
        body=body,
        quote_label=f"{i18n_t(lang, 'offerte')} {quote.number} v{quote.version}",
        title=tr.get(project.name, project.name),
        view_url=view,
        accent=branding.theme_vars(org.brand_primary).get("--accent", "#1f6f4a"),
        logo_url=logo,
        total=(headline[0], eur(headline[1])) if headline else None,
    )


def quote_mails(session: Session, project: Project):
    from app.models import OutboxMessage

    return list(
        session.exec(
            select(OutboxMessage)
            .where(OutboxMessage.project_id == project.id, OutboxMessage.kind == "offerte")
            .order_by(OutboxMessage.id.desc())
        ).all()
    )


def send_quote(
    session: Session,
    org: Organisation,
    project: Project,
    quote: Quote,
    to: str,
    subject: str,
    body: str,
    actor: str = "gebruiker",
):
    """Mail the quote PDF to the customer. Once it is really sent, the quote is 'verzonden' and the project moves
    to 'Offerte gestuurd'; without a mail server it waits in the outbox and 'Verstuur nu' finishes the job."""
    to = (to or "").strip()
    if "@" not in to or " " in to:
        raise TransitionError("Geef een geldig e-mailadres voor de klant.")
    if not subject.strip() or not body.strip():
        raise TransitionError("Onderwerp en tekst mogen niet leeg zijn.")
    from app import services

    customer = services.project_customer(session, project)
    if customer and not customer.email:  # remember the address for next time
        customer.email = to
        session.add(customer)
        session.commit()
    public_url(session, quote)  # the buttons in the mail need the token
    msg = mail.queue_mail(
        session, org, to, subject.strip(), body.strip() + "\n", kind="offerte", project_id=project.id, quote_id=quote.id
    )
    state = {"verzonden": "verzonden", "mislukt": "mislukt"}.get(msg.status, "in outbox")
    log(session, project, f"Offerte {quote.number} v{quote.version} naar {to} ({state}).", kind="mail", actor=actor, ref_id=msg.id)
    after_delivery(session, org, msg, actor=actor)
    return msg


def after_delivery(session: Session, org: Organisation, msg, actor: str = "systeem") -> None:
    """A mailed quote that really went out marks the quote as sent and moves the project on; same for an invoice."""
    if msg.kind == "factuur" and msg.status == "verzonden" and msg.invoice_id:
        from app import invoicing

        inv = invoicing.get(session, org, msg.invoice_id)
        if inv and inv.status == "goedgekeurd":
            invoicing.mark_sent(session, org, inv, "email", actor, note=f"naar {msg.to}")
        return
    if msg.kind != "offerte" or msg.status != "verzonden" or not msg.quote_id:
        return
    quote = session.get(Quote, msg.quote_id)
    project = session.get(Project, msg.project_id) if msg.project_id else None
    if not quote or not project or project.org_id != org.id:
        return
    if quote.status == "concept":
        quote.status = "verzonden"
        session.add(quote)
        session.commit()
    if LEGACY.get(project.status, project.status) == "gemaakt":
        change_status(session, org, project, "gestuurd", f"per e-mail naar {msg.to}", actor=actor)


# ---------- the customer's answer (/o/{token}) ----------

OPEN_FOR_ANSWER = {"gemaakt", "gestuurd"}


def quote_by_token(session: Session, token: str) -> tuple[Organisation, Project, Quote] | None:
    if not token:
        return None
    quote = session.exec(select(Quote).where(Quote.public_token == token)).first()
    project = session.get(Project, quote.project_id) if quote else None
    org = session.get(Organisation, project.org_id) if project else None
    return (org, project, quote) if org else None


def answer_blocked(session: Session, project: Project, quote: Quote) -> str | None:
    """Why the customer can't answer online (an i18n key), or None. An answered quote shows its answer instead."""
    latest = _latest_quote(session, project)
    if latest and latest.id != quote.id:
        return "vervangen"
    if LEGACY.get(project.status, project.status) not in OPEN_FOR_ANSWER:
        return "afgesloten"
    return None


def customer_decision(
    session: Session,
    org: Organisation,
    project: Project,
    quote: Quote,
    decision: str,
    name: str | None = None,
    note: str | None = None,
    ip: str | None = None,
) -> None:
    """The customer accepted or declined online: record it, move the project (accepting runs the usual triggers:
    stock reservation, planning) and let the company know by mail."""
    if decision not in ("aanvaard", "geweigerd"):
        raise TransitionError("Onbekende keuze.")
    if quote.decision:
        return  # answered before (double click, back button): keep the first answer
    if reason := answer_blocked(session, project, quote):
        raise TransitionError(reason)
    name, note = (name or "").strip()[:120] or None, (note or "").strip()[:2000] or None
    if decision == "aanvaard" and not name:
        raise TransitionError("naam_verplicht")
    quote.decision, quote.decided_at, quote.decided_name = decision, datetime.now(UTC), name
    quote.decision_note, quote.decision_ip, quote.status = note, ip, decision
    session.add(quote)
    session.commit()
    who = name or project.client_name or "de klant"
    text = f"online {decision} door {who}" + (f": {note}" if note else "")
    if decision == "aanvaard":
        if LEGACY.get(project.status, project.status) == "gemaakt":  # link shared without marking it sent
            change_status(session, org, project, "gestuurd", "link geopend door de klant", actor="klant")
        change_status(session, org, project, "aanvaard", text, actor="klant")
    else:
        change_status(session, org, project, "geweigerd", text, actor="klant")
    body = (
        f"{who} heeft offerte {quote.number} v{quote.version} voor '{project.name}' online {decision}"
        f" op {quote.decided_at:%d/%m/%Y om %H:%M} (UTC).\n"
        + (f"\nOpmerking van de klant:\n{note}\n" if note else "")
        + ("\nDe voorraadcontrole en de planning zijn gestart.\n" if decision == "aanvaard" else "")
        + f"\nBekijk het project: {_project_url(project)}\n"
    )
    msg = mail.queue_mail(
        session, org, org.email, f"Offerte {quote.number} {decision} door {who}", body, kind="klantreactie",
        project_id=project.id,
    )
    log(session, project, f"Melding klantreactie naar {org.email}.", kind="mail", ref_id=msg.id)


# ---------- the supplier's answer (/l/{token}) ----------


def order_public_url(order) -> str:
    return f"{get_settings().base_url.rstrip('/')}/l/{order.public_token}"


def order_mail(session: Session, org: Organisation, order_id: int, subject: str, body: str):
    """Everything the HTML order mail needs; None when the order has no supplier link."""
    from app import branding
    from app.models import PurchaseOrder
    from app.outputs.order_mail import OrderMail

    order = session.get(PurchaseOrder, order_id)
    if not order or order.org_id != org.id or not order.public_token:
        return None
    logo = f"{order_public_url(order)}/logo" if org.logo_file and not org.logo_file.endswith(".svg") else None
    return OrderMail(
        company=org.company_name or org.email,
        subject=subject,
        body=body,
        url=order_public_url(order),
        accent=branding.theme_vars(org.brand_primary).get("--accent", "#1f6f4a"),
        logo_url=logo,
    )


def order_by_token(session: Session, token: str):
    from app.models import PurchaseOrder

    if not token:
        return None
    order = session.exec(select(PurchaseOrder).where(PurchaseOrder.public_token == token)).first()
    org = session.get(Organisation, order.org_id) if order else None
    return (org, order) if org else None


def supplier_response(
    session: Session,
    org: Organisation,
    order,
    lines: dict[int, tuple[str, date | None, str | None]],
    contact: str | None = None,
    note: str | None = None,
) -> tuple[bool, str]:
    """Record the supplier's confirmation, move the delivery check to the latest delivery date, ask for an
    alternative for what can't be delivered, and let the stock manager know."""
    from app.models import Supplier

    first = order.supplier_confirmed_at is None
    ok, msg = stock.supplier_response(session, order, lines, contact, note)
    if not ok:
        return ok, msg
    rows = stock.order_lines(session, order)
    supplier = session.get(Supplier, order.supplier_id) if order.supplier_id else None
    sup_name = supplier.name if supplier else "De leverancier"
    link = f"/voorraad#bestelling-{order.id}"
    dates = [ln.delivery_date for ln in rows if ln.delivery_date]
    if dates and (todo := todos.find_open(session, org, "levering", ref_id=order.id)):
        todo.due = max(dates)
        session.add(todo)
        session.commit()
    missing = [ln for ln in rows if ln.delivery_status == "niet_leverbaar"]
    if missing and not todos.find_open(session, org, "voorraad", ref_id=order.id):
        managers = todos.responsible(session, org, "stock_manager")
        todos.add(
            session, org, f"Alternatief zoeken: niet leverbaar bij {sup_name} ({order.number})", kind="voorraad",
            detail=", ".join(ln.label for ln in missing), link=link, ref_id=order.id,
            assignee_id=managers[0].id if managers else None, due_in_days=1,
        )

    def line_text(ln) -> str:
        what = stock.DELIVERY_STATUSES[ln.delivery_status]
        when = f" op {ln.delivery_date:%d/%m/%Y}" if ln.delivery_date else ""
        return f"- {ln.label} ({ln.qty:g} {ln.unit or ''}".rstrip() + f"): {what.lower()}{when}" + (f" — {ln.delivery_note}" if ln.delivery_note else "")

    who = order.supplier_contact or sup_name
    summary = f"{sup_name} heeft bestelling {order.number} {'bevestigd' if first else 'bijgewerkt'}" + (f" ({who})" if who != sup_name else "")
    body = (
        f"{summary}.\n\n" + "\n".join(line_text(ln) for ln in rows) + "\n"
        + (f"\nOpmerking van de leverancier:\n{order.supplier_note}\n" if order.supplier_note else "")
        + ("\nNiet leverbaar: zoek een alternatief (to-do aangemaakt).\n" if missing else "")
        + f"\nBekijk de bestelling: {get_settings().base_url.rstrip('/')}{link}\n"
    )
    for to in todos.responsible_emails(session, org, "stock_manager"):
        mail.queue_mail(session, org, to, f"{summary}", body, kind="leverancierreactie")
    for pid in {ln.project_id for ln in rows if ln.project_id}:
        if (p := session.get(Project, pid)) and p.org_id == org.id:
            log(session, p, summary + ".", kind="trigger", actor="leverancier", link=link)
    return True, summary + "."


# ---------- triggers ----------


def _project_url(project: Project) -> str:
    return f"{get_settings().base_url.rstrip('/')}/projects/{project.id}"


def _quote_result(session: Session, project: Project) -> tuple[Quote, QuoteResult]:
    quote = _latest_quote(session, project)
    return quote, QuoteResult.model_validate_json(quote.result_json)


def _labels(session: Session, org: Organisation):
    from app import services  # services imports this module
    from app.engine import load_skill

    config = services.current_config(session, org)
    tracked = stock.tracked_codes(config, load_skill(org.sector).default_price_config())
    return config, tracked, {i.code: i.label for i in config.items}


def trigger_reserve_stock(session: Session, org: Organisation, project: Project) -> None:
    """Reserve the quote's material. Shortage (or stock under its reorder point) becomes an order proposal per
    supplier, with a to-do and a mail for the stock manager, who confirms it in the app."""
    quote, result = _quote_result(session, project)
    config, tracked, labels = _labels(session, org)
    rows = stock.reserve_for_project(session, org, project, result, tracked)
    if not rows:
        if stock.active_reservations(session, org, project.id):
            log(session, project, "Materiaal was al gereserveerd voor dit project.", kind="trigger")
        else:
            log(session, project, "Geen voorraadmateriaal in deze offerte; niets te reserveren.", kind="trigger")
        return
    short = [r for r in rows if r["shortage"] > 0]
    log(
        session,
        project,
        f"Materiaal gereserveerd: {len(rows)} artikel{'' if len(rows) == 1 else 'en'}"
        + (f", tekort voor {len(short)}" if short else ", alles op voorraad")
        + ".",
        kind="trigger",
        link="/voorraad",
    )
    events.emit(
        session,
        org,
        "voorraad.controle",
        {
            "project": {"id": project.id, "name": project.name, "client": project.client_name, "url": _project_url(project)},
            "quote": {"number": quote.number, "version": quote.version},
            "materials": rows,
            "shortage": bool(short),
        },
        project_id=project.id,
    )
    for order in stock.propose_orders(session, org, labels, project):
        _ask_to_confirm_order(session, org, order, project)


def _ask_to_confirm_order(session: Session, org: Organisation, order, project: Project | None) -> None:
    from app.models import Supplier

    lines = stock.order_lines(session, order)
    supplier = session.get(Supplier, order.supplier_id) if order.supplier_id else None
    managers = todos.responsible(session, org, "stock_manager")
    link = f"/voorraad#bestelling-{order.id}"
    todos.add(
        session,
        org,
        f"Bestelling {order.number} bevestigen" + (f" bij {supplier.name}" if supplier else " (leverancier kiezen)"),
        kind="bestelling",
        detail="; ".join(f"{ln.label} {ln.qty:g} {ln.unit or ''}".strip() for ln in lines),
        link=link,
        project_id=project.id if project else None,
        ref_id=order.id,
        assignee_id=managers[0].id if managers else None,
        due_in_days=2,
    )
    body = (
        (f"Offerte voor '{project.name}' is aanvaard en er is niet genoeg voorraad.\n\n" if project else "")
        + f"Bestelvoorstel {order.number}" + (f" bij {supplier.name}" if supplier else " (nog geen leverancier gekoppeld)") + ":\n\n"
        + "\n".join(f"- {ln.label}: {ln.qty:g} {ln.unit or ''} ({ln.reason})".replace("  ", " ") for ln in lines)
        + f"\n\nBevestig de bestelling in het platform: {get_settings().base_url.rstrip('/')}{link}\n"
        "Na bevestiging gaat de bestelmail automatisch naar de leverancier.\n"
    )
    for to in todos.responsible_emails(session, org, "stock_manager"):
        msg = mail.queue_mail(
            session, org, to, f"Te bevestigen: bestelling {order.number}", body, kind="bestelvoorstel",
            project_id=project.id if project else None,
        )
        if project:
            state = "verzonden" if msg.status == "verzonden" else "in outbox"
            log(session, project, f"Bestelvoorstel {order.number} ter bevestiging naar {to} ({state}).", kind="mail", ref_id=msg.id)


def confirm_order(session: Session, org: Organisation, order, by: str = "gebruiker") -> tuple[bool, str]:
    """Stock manager approved an order proposal: mail the supplier, close the to-do, ask to check the delivery."""
    ok, msg = stock.confirm_order(session, org, order, by=by)
    if not ok:
        return ok, msg
    todos.complete_matching(session, org, "bestelling", ref_id=order.id)
    todos.add(
        session, org, f"Levering {order.number} controleren en inboeken", kind="levering",
        link=f"/voorraad#bestelling-{order.id}", ref_id=order.id, due_in_days=5,
    )
    for pid in {ln.project_id for ln in stock.order_lines(session, order) if ln.project_id}:
        if (p := session.get(Project, pid)) and p.org_id == org.id:
            log(session, p, msg, kind="mail", ref_id=order.mail_id, link=f"/voorraad#bestelling-{order.id}")
    return ok, msg


def receive_order(session: Session, org: Organisation, order) -> tuple[bool, str]:
    ok, msg = stock.receive_order(session, org, order)
    if ok:
        todos.complete_matching(session, org, "levering", ref_id=order.id)
    return ok, msg


def cancel_order(session: Session, org: Organisation, order) -> tuple[bool, str]:
    ok, msg = stock.cancel_order(session, org, order)
    if ok:
        todos.complete_matching(session, org, "bestelling", ref_id=order.id)
        todos.complete_matching(session, org, "levering", ref_id=order.id)
    return ok, msg


def trigger_planning(session: Session, org: Organisation, project: Project) -> None:
    """Schedule the work: the proposal on the quote becomes the planning block (when its dates are still ahead);
    otherwise the planner gets a to-do. An event goes to an external planning tool either way."""
    from app import planning

    quote, result = _quote_result(session, project)
    planners = todos.responsible(session, org, "planner")
    planner_id = planners[0].id if planners else None
    proposal_ok = project.proposed_start and project.proposed_start >= date.today()
    if proposal_ok and not planning.items_for_project(session, project):
        end = project.proposed_end or project.proposed_start
        item, warnings = planning.save_item(
            session, org, None, project_id=project.id, title=None, date_from=project.proposed_start, date_to=end,
            employee_ids=project.proposed_crew, start_time="08:00", hours=None, note="Uit het voorstel op de offerte",
        )
        link = f"/planning?dag={project.proposed_start}&item={item.id}"
        log(session, project, f"Ingepland volgens het voorstel: {project.proposed_start:%d/%m}–{end:%d/%m}."
            + (" " + " ".join(warnings) if warnings else ""), kind="trigger", link=link)
        if (warnings or not project.proposed_crew) and not todos.find_open(session, org, "inplannen", project_id=project.id):
            todos.add(session, org, f"Planning nakijken: {project.name} ({'niemand toegewezen' if not project.proposed_crew else 'niet iedereen beschikbaar'})",
                      kind="inplannen", link=link, project_id=project.id, assignee_id=planner_id, due_in_days=3)
    elif not todos.find_open(session, org, "inplannen", project_id=project.id):
        todos.add(
            session,
            org,
            f"Werk inplannen: {project.name} ({result.totals.hours_total:g} u)",
            kind="inplannen",
            detail=f"{project.client_name or ''} {project.client_address or ''}".strip() or None,
            link=f"/planning?project={project.id}",
            project_id=project.id,
            assignee_id=planner_id,
            due_in_days=5,
        )
    if not events.already_emitted(session, project.id, "werk.inplannen"):
        ev = events.emit(
            session,
            org,
            "werk.inplannen",
            {
                "project": {"id": project.id, "name": project.name, "url": _project_url(project)},
                "client": {"name": project.client_name, "address": project.client_address},
                "scope": project.scope,
                "hours_total": result.totals.hours_total,
                "proposed": {"start": str(project.proposed_start) if project.proposed_start else None,
                             "end": str(project.proposed_end) if project.proposed_end else None,
                             "crew": project.proposed_crew},
                "quote": {"number": quote.number, "version": quote.version, "total_incl": result.totals.total_incl},
            },
            project_id=project.id,
        )
        if ev.status == "afgeleverd":
            log(session, project, "Werk doorgegeven aan de planningstool.", kind="trigger", ref_id=ev.id)


def trigger_release_stock(session: Session, org: Organisation, project: Project) -> None:
    n = stock.release_for_project(session, org, project)
    if n:
        log(session, project, f"Reservatie van {n} artikel{'' if n == 1 else 'en'} vrijgegeven.", kind="trigger")


def trigger_work_started(session: Session, org: Organisation, project: Project) -> None:
    from app import planning

    todos.complete_matching(session, org, "inplannen", project_id=project.id)
    for line in planning.follow_start(session, org, project, project.work_started_on or date.today()):
        log(session, project, line, kind="trigger", link=f"/planning?dag={project.work_started_on or date.today()}")


def trigger_work_unstarted(session: Session, org: Organisation, project: Project) -> None:
    from app import planning

    planning.set_project_items_status(session, project, "gepland")


def trigger_work_reopened(session: Session, org: Organisation, project: Project) -> None:
    from app import planning

    planning.set_project_items_status(session, project, "bezig")


def trigger_work_done(session: Session, org: Organisation, project: Project) -> None:
    from app import planning

    for line in planning.follow_done(session, org, project, project.work_done_on or date.today()):
        log(session, project, line, kind="trigger", link=f"/planning?dag={project.work_done_on or date.today()}")
    n = stock.consume_for_project(session, org, project)
    if n:
        log(session, project, f"Gereserveerd materiaal afgeboekt van de voorraad ({n} artikel{'' if n == 1 else 'en'}).", kind="trigger")
    from app import invoicing

    # the concept invoice (from the latest quote) with a to-do to approve it; without a quote: a reminder
    if invoicing.create_from_project(session, org, project) is None and not todos.find_open(
        session, org, "factuur", project_id=project.id
    ):
        todos.add(session, org, f"Factuur opmaken en versturen: {project.name}", kind="factuur",
                  link=f"/projects/{project.id}#status", project_id=project.id, due_in_days=3)


def trigger_invoice_sent(session: Session, org: Organisation, project: Project) -> None:
    todos.complete_matching(session, org, "factuur", project_id=project.id)
    todos.complete_matching(session, org, "factuur_goedkeuren", project_id=project.id)
    if not todos.find_open(session, org, "betaling", project_id=project.id):
        todos.add(session, org, f"Betaling opvolgen: {project.name}", kind="betaling",
                  link=f"/projects/{project.id}#status", project_id=project.id, due_in_days=30)


def trigger_paid(session: Session, org: Organisation, project: Project) -> None:
    todos.complete_matching(session, org, "betaling", project_id=project.id)


def trigger_closed(session: Session, org: Organisation, project: Project) -> None:
    for t in todos.list_todos(session, org, "open"):
        if t.project_id == project.id and t.kind != "bestelling":  # orders may still be needed for stock
            todos.complete(session, t)


ON_ENTER = {
    "aanvaard": [trigger_reserve_stock, trigger_planning],
    "in_uitvoering": [trigger_work_started],
    "werk_afgerond": [trigger_work_done],
    "factuur_verstuurd": [trigger_invoice_sent],
    "factuur_betaald": [trigger_paid],
    "afgerond": [trigger_closed],
    "geweigerd": [trigger_closed],
}
# going back from 'aanvaard' to 'gestuurd' undoes the reservation
ON_LEAVE_BACKWARDS = {
    "aanvaard": [trigger_release_stock],
    "in_uitvoering": [trigger_work_unstarted],
    "werk_afgerond": [trigger_work_reopened],  # back to 'in uitvoering': blocks are 'bezig' again
}
