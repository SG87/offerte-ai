"""Invoices: made as a concept when the work is done, approved by whoever handles invoicing, then sent —
over Peppol for business customers (UBL), as PDF (by mail) for private customers — and finally paid.

Numbers are only given on approval and approved invoices are never deleted, so the numbering has no gaps.
Amounts are computed here, never by the AI: lines hold quantity × unit price (excl. btw) and a btw rate.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta

from sqlmodel import Session, select

from app import i18n, todos
from app.models import Customer, Invoice, Organisation, Project, Quote

STATUSES = {"concept": "Concept", "goedgekeurd": "Goedgekeurd", "verzonden": "Verzonden", "betaald": "Betaald"}
EDITABLE = {"concept"}
TODO_KIND = "factuur_goedkeuren"


class InvoiceError(ValueError):
    pass


# ---------- amounts ----------


def _line(raw: dict) -> dict:
    qty = float(raw.get("qty") or 0)
    price = float(raw.get("unit_price") or 0)
    return {
        "description": str(raw.get("description") or "").strip(),
        "qty": qty,
        "unit": str(raw.get("unit") or ""),
        "unit_price": price,
        "vat_pct": float(raw.get("vat_pct") if raw.get("vat_pct") is not None else 21.0),
        "amount": round(qty * price, 2),
    }


def totals(inv: Invoice) -> dict:
    """{lines (with amount), rates [{pct, base, vat}], subtotal, vat, total} — btw per rate on the rate's base."""
    lines = [_line(raw) for raw in inv.lines]
    rates: dict[float, float] = {}
    for ln in lines:
        rates[ln["vat_pct"]] = rates.get(ln["vat_pct"], 0.0) + ln["amount"]
    groups = [{"pct": pct, "base": round(base, 2), "vat": round(base * pct / 100, 2)} for pct, base in sorted(rates.items())]
    subtotal = round(sum(g["base"] for g in groups), 2)
    vat = round(sum(g["vat"] for g in groups), 2)
    return {"lines": lines, "rates": groups, "subtotal": subtotal, "vat": vat, "total": round(subtotal + vat, 2)}


def display_lines(inv: Invoice, incl_vat: bool) -> list[dict]:
    """Lines as the customer reads them: incl. btw for private customers (adding up to the total), else excl."""
    t = totals(inv)
    if not incl_vat:
        return t["lines"]
    out = [{**ln, "shown": round(ln["amount"] * (1 + ln["vat_pct"] / 100), 2)} for ln in t["lines"]]
    rest = round(t["total"] - sum(ln["shown"] for ln in out), 2)
    if out and abs(rest) >= 0.005:
        out[-1]["shown"] = round(out[-1]["shown"] + rest, 2)
    for ln in out:
        ln["shown_unit"] = ln["shown"] / ln["qty"] if ln["qty"] else ln["shown"]
    return out


def due_date(inv: Invoice) -> date | None:
    return inv.invoice_date + timedelta(days=inv.due_days) if inv.invoice_date else None


def structured_ref(year: int, seq: int) -> str:
    """Belgian structured communication (OGM): 10 digits + mod-97 check (97 when the remainder is 0)."""
    base = int(f"{year % 100:02d}{seq:08d}")
    digits = f"{base:010d}{base % 97 or 97:02d}"
    return f"+++{digits[:3]}/{digits[3:7]}/{digits[7:]}+++"


# ---------- queries ----------


def get(session: Session, org: Organisation, invoice_id: int) -> Invoice | None:
    inv = session.get(Invoice, invoice_id)
    return inv if inv and inv.org_id == org.id else None


def list_invoices(session: Session, org: Organisation, status: str | None = None) -> list[Invoice]:
    q = select(Invoice).where(Invoice.org_id == org.id)
    if status:
        q = q.where(Invoice.status == status)
    return list(session.exec(q.order_by(Invoice.id.desc())).all())


def for_project(session: Session, project: Project) -> Invoice | None:
    return session.exec(select(Invoice).where(Invoice.project_id == project.id).order_by(Invoice.id.desc())).first()


# ---------- lifecycle ----------


def create_from_project(session: Session, org: Organisation, project: Project, actor: str = "systeem") -> Invoice | None:
    """Concept invoice from the project's latest quote, in the customer's language, plus a to-do to approve it.
    Idempotent: a project that already has an invoice gets no second one. None without a quote."""
    from app import services, workflow

    if existing := for_project(session, project):
        return existing
    quote = services.latest_quote(session, project)
    if quote is None:
        return None
    customer = services.project_customer(session, project)
    lang = services.quote_language(org, customer)
    tr = services.translations_for(quote, lang)
    from app.outputs.offer_lines import offer_lines

    result = services.quote_result(quote)
    lines = [
        {
            "description": ol.description,
            "qty": ol.qty if ol.qty is not None else 1.0,
            "unit": ol.unit,
            # enough decimals that quantity × price gives back the quoted amount to the cent
            "unit_price": round(ol.amount / ol.qty, 6) if ol.qty else ol.amount,
            "vat_pct": project.vat_pct,
        }
        for ol in offer_lines(result, False, lang, lambda text: tr.get(text, text))
    ]
    inv = Invoice(
        org_id=org.id,
        project_id=project.id,
        customer_id=customer.id if customer else None,
        quote_id=quote.id,
        b2b=bool(customer and customer.is_b2b),
        language=lang,
        client_name=project.client_name or (customer.name if customer else None),
        client_address=(customer.address if customer and customer.address else project.client_address),
        client_vat=customer.vat_number if customer else None,
        client_email=customer.email if customer else None,
        title=tr.get(project.name, project.name),
        lines_json=json.dumps(lines, ensure_ascii=False),
    )
    session.add(inv)
    session.commit()
    session.refresh(inv)
    invoicers = todos.responsible(session, org, "invoicer")
    todos.add(session, org, f"Factuur goedkeuren: {project.name}", kind=TODO_KIND, link=f"/facturen/{inv.id}",
              project_id=project.id, ref_id=inv.id, assignee_id=invoicers[0].id if invoicers else None, due_in_days=2,
              detail=f"Conceptfactuur van {totals(inv)['total']:.2f} EUR incl. btw voor {inv.client_name or 'de klant'}.")
    workflow.log(session, project, f"Conceptfactuur aangemaakt ({totals(inv)['total']:.2f} EUR incl. btw).",
                 kind="trigger", actor=actor, link=f"/facturen/{inv.id}")
    return inv


def save(
    session: Session,
    inv: Invoice,
    lines: list[dict],
    notes: str | None = None,
    due_days: int | None = None,
    title: str | None = None,
    client_name: str | None = None,
    client_address: str | None = None,
    client_vat: str | None = None,
) -> Invoice:
    if inv.status not in EDITABLE:
        raise InvoiceError("Een goedgekeurde factuur pas je niet meer aan; zet ze eerst terug naar concept.")
    clean = [{k: v for k, v in _line(raw).items() if k != "amount"} for raw in lines if str(raw.get("description") or "").strip()]
    if not clean:
        raise InvoiceError("Een factuur heeft minstens één regel nodig.")
    inv.lines_json = json.dumps(clean, ensure_ascii=False)
    inv.notes = (notes or "").strip() or None
    if due_days is not None:
        inv.due_days = max(0, int(due_days))
    for field, value in (("title", title), ("client_name", client_name), ("client_address", client_address)):
        if value is not None:
            setattr(inv, field, value.strip() or None)
    if client_vat is not None:
        inv.client_vat = re.sub(r"[\s.]", "", client_vat).upper() or None
    session.add(inv)
    session.commit()
    session.refresh(inv)
    return inv


def _next_seq(session: Session, org: Organisation, year: int) -> int:
    numbers = session.exec(
        select(Invoice.number).where(Invoice.org_id == org.id, Invoice.number.like(f"F{year}-%"))  # type: ignore[union-attr]
    ).all()
    return max((int(n.split("-")[1]) for n in numbers if n), default=0) + 1


def approve(session: Session, org: Organisation, inv: Invoice, actor: str = "gebruiker") -> Invoice:
    """Number (once), date and payment reference; the approval to-do is done."""
    if inv.status != "concept":
        raise InvoiceError("Deze factuur is al goedgekeurd.")
    if not totals(inv)["lines"]:
        raise InvoiceError("Een factuur zonder regels kan je niet goedkeuren.")
    if not inv.number:
        year = date.today().year
        seq = _next_seq(session, org, year)
        inv.number, inv.structured_ref = f"F{year}-{seq:04d}", structured_ref(year, seq)
    inv.invoice_date = date.today()
    inv.status, inv.approved_at = "goedgekeurd", datetime.now(UTC)
    session.add(inv)
    session.commit()
    todos.complete_matching(session, org, TODO_KIND, ref_id=inv.id)
    _log(session, inv, f"Factuur {inv.number} goedgekeurd.", actor)
    return inv


def back_to_concept(session: Session, org: Organisation, inv: Invoice, actor: str = "gebruiker") -> Invoice:
    """Approved but not sent yet: editable again. The number stays reserved for this invoice."""
    if inv.status != "goedgekeurd":
        raise InvoiceError("Alleen een goedgekeurde, nog niet verzonden factuur kan terug naar concept.")
    inv.status = "concept"
    session.add(inv)
    session.commit()
    _log(session, inv, f"Factuur {inv.number} terug naar concept.", actor)
    return inv


def mark_sent(session: Session, org: Organisation, inv: Invoice, via: str, actor: str = "gebruiker", note: str | None = None) -> Invoice:
    """Sent (Peppol, mail or by hand): the project moves to 'factuur verstuurd'."""
    if inv.status == "concept":
        raise InvoiceError("Keur de factuur eerst goed.")
    if inv.status == "goedgekeurd":
        inv.status = "verzonden"
    inv.sent_via, inv.sent_at = via, datetime.now(UTC)
    session.add(inv)
    session.commit()
    _log(session, inv, f"Factuur {inv.number} verstuurd ({via}){': ' + note if note else ''}.", actor)
    _project_to(session, org, inv, "factuur_verstuurd", f"factuur {inv.number} ({via})", actor)
    return inv


def mark_paid(session: Session, org: Organisation, inv: Invoice, on: date | None = None, actor: str = "gebruiker") -> Invoice:
    if inv.status not in ("goedgekeurd", "verzonden"):
        raise InvoiceError("Alleen een goedgekeurde of verzonden factuur kan betaald zijn.")
    on = on or date.today()
    if on > date.today():
        raise InvoiceError("Een betaling in de toekomst kan niet: kies vandaag of vroeger.")
    if inv.status == "goedgekeurd":  # paid without our sending (handed over on site): count it as sent
        mark_sent(session, org, inv, "manueel", actor)
    inv.status, inv.paid_on = "betaald", on
    session.add(inv)
    session.commit()
    _log(session, inv, f"Factuur {inv.number} betaald op {on:%d/%m/%Y}.", actor)
    _project_to(session, org, inv, "factuur_betaald", f"factuur {inv.number}", actor)
    return inv


def delete_concept(session: Session, org: Organisation, inv: Invoice) -> None:
    if inv.status != "concept" or inv.number:
        raise InvoiceError("Alleen een concept zonder nummer kan weg; genummerde facturen blijven bewaard.")
    todos.complete_matching(session, org, TODO_KIND, ref_id=inv.id)
    session.delete(inv)
    session.commit()


def _project_to(session: Session, org: Organisation, inv: Invoice, status: str, note: str, actor: str) -> None:
    from app import workflow

    project = session.get(Project, inv.project_id) if inv.project_id else None
    if project and workflow.next_status(workflow.LEGACY.get(project.status, project.status)) == status:
        try:
            workflow.change_status(session, org, project, status, note, actor=actor)
        except workflow.TransitionError:
            pass  # the project went its own way; the invoice is still recorded


def _log(session: Session, inv: Invoice, text: str, actor: str) -> None:
    from app import workflow

    project = session.get(Project, inv.project_id) if inv.project_id else None
    if project:
        workflow.log(session, project, text, kind="info", actor=actor, link=f"/facturen/{inv.id}")


# ---------- sending ----------


def peppol_missing(org: Organisation, inv: Invoice) -> list[str]:
    """What a Peppol invoice still needs (both parties are identified by their Belgian enterprise number)."""
    missing = []
    if not inv.b2b:
        missing.append("Peppol is voor zakelijke klanten (B2B)")
    if not org.vat_number:
        missing.append("btw-nummer van je bedrijf (Account)")
    if not org.iban:
        missing.append("IBAN van je bedrijf (Account)")
    if not inv.client_vat:
        missing.append("btw-nummer van de klant")
    if inv.status == "concept":
        missing.append("goedkeuring van de factuur")
    return missing


def ubl(session: Session, org: Organisation, inv: Invoice) -> bytes:
    from app.outputs.ubl import build_invoice_ubl

    return build_invoice_ubl(org, inv, totals(inv), due_date(inv))


def send_peppol(session: Session, org: Organisation, inv: Invoice, actor: str = "gebruiker") -> Invoice:
    """To the configured access point; without one the invoice is 'klaar voor Peppol' (download the UBL)."""
    from app.config import get_settings
    from app.integrations import peppol

    if missing := peppol_missing(org, inv):
        raise InvoiceError("Nog nodig voor Peppol: " + ", ".join(missing) + ".")
    xml = ubl(session, org, inv)
    if not get_settings().peppol_ready:
        inv.peppol_status, inv.peppol_error = "klaar", None
        session.add(inv)
        session.commit()
        return inv
    ok, ref, error = peppol.send(xml, inv)
    inv.peppol_status, inv.peppol_ref, inv.peppol_error = ("verzonden" if ok else "mislukt"), ref, error
    session.add(inv)
    session.commit()
    if ok:
        mark_sent(session, org, inv, "peppol", actor)
    return inv


def mail(session: Session, org: Organisation, inv: Invoice, to: str, subject: str, body: str, actor: str = "gebruiker"):
    """Mail with the invoice PDF (rebuilt when it is sent). It counts as sent once the mail really went out."""
    from app.integrations.mail import queue_mail

    if inv.status == "concept":
        raise InvoiceError("Keur de factuur eerst goed.")
    msg = queue_mail(session, org, to, subject, body, kind="factuur", project_id=inv.project_id, invoice_id=inv.id)
    if msg.status == "verzonden":
        mark_sent(session, org, inv, "email", actor, note=f"naar {to}")
    return msg


def mail_defaults(org: Organisation, inv: Invoice) -> dict[str, str]:
    lang = inv.language
    t = totals(inv)
    amount = f"€ {t['total']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    due = due_date(inv)
    pay = i18n.t(lang, "betaling" if org.iban else "betaling_zonder_iban", amount=amount,
                 due=f"{due:%d/%m/%Y}" if due else "", iban=org.iban or "", ref=inv.structured_ref or "")
    name = inv.client_name or i18n.t(lang, "klant_aanspreking")
    subject = f"{i18n.t(lang, 'factuur')} {inv.number or ''} — {org.company_name or org.email}".strip()
    body = (
        f"{i18n.t(lang, 'mail_aanhef', name=name)}\n\n{i18n.t(lang, 'factuur')} {inv.number}: {inv.title or ''}\n{pay}\n\n"
        f"{i18n.t(lang, 'mail_groet')}\n{org.company_name or org.email}\n"
    )
    return {"to": inv.client_email or "", "subject": subject, "body": body}


def invoice_pdf(session: Session, org: Organisation, inv: Invoice) -> bytes:
    from app.outputs.factuur_pdf import build_factuur_pdf

    return build_factuur_pdf(org, inv, totals(inv), display_lines(inv, not inv.b2b), due_date(inv))


def customer_of(session: Session, inv: Invoice) -> Customer | None:
    return session.get(Customer, inv.customer_id) if inv.customer_id else None


def quote_of(session: Session, inv: Invoice) -> Quote | None:
    return session.get(Quote, inv.quote_id) if inv.quote_id else None


# ---------- AI ----------


def editable_view(inv: Invoice) -> dict:
    return {
        "title": inv.title,
        "language": inv.language,
        "b2b": inv.b2b,
        "due_days": inv.due_days,
        "notes": inv.notes,
        "lines": [{k: v for k, v in ln.items() if k != "amount"} for ln in totals(inv)["lines"]],
    }


def start_ai_edit(session: Session, org: Organisation, inv: Invoice, instruction: str):
    """Background job: Claude rewrites the lines from the instruction; returns (job, work) for ai_jobs.run."""
    from app import ai_jobs
    from app.ai.invoice_assist import edit_invoice

    if inv.status not in EDITABLE:
        raise InvoiceError("Alleen een conceptfactuur kan je laten aanpassen.")
    job = ai_jobs.start(session, org, "factuur", f"Factuur aanpassen: {instruction[:60]}", link=f"/facturen/{inv.id}",
                        project_id=inv.project_id, ref_id=inv.id)

    def work(session: Session, org: Organisation, job) -> tuple[str, str | None]:
        current = get(session, org, inv.id)
        before = totals(current)["total"]
        out = edit_invoice(editable_view(current), instruction)
        save(session, current, out["lines"], notes=out.get("notes"), due_days=out.get("due_days"), title=out.get("title"))
        after = totals(current)["total"]
        _log(session, current, f"Factuur aangepast door AI: {instruction[:80]}", "ai")
        return f"{out.get('summary') or 'Factuur aangepast.'} Totaal {before:.2f} → {after:.2f} EUR incl. btw.", f"/facturen/{inv.id}"

    return job, work
