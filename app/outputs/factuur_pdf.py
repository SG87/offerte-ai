"""Invoice as PDF (reportlab), in the customer's language. Private customers read prices incl. btw,
business customers excl. btw; the btw per rate and the amount to pay are always shown."""

from __future__ import annotations

import io
from datetime import date
from html import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.i18n import t
from app.models import Invoice, Organisation


def _eur(v: float) -> str:
    return f"€ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _num(v: float, lang: str) -> str:
    s = f"{v:g}"
    return s if lang == "en" else s.replace(".", ",")


def build_factuur_pdf(org: Organisation, inv: Invoice, tot: dict, lines: list[dict], due: date | None) -> bytes:
    lang = inv.language
    incl = not inv.b2b
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm)
    styles = getSampleStyleSheet()
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=8.5, leading=11, textColor=colors.HexColor("#444444"))
    body = ParagraphStyle("body", parent=styles["Normal"], fontSize=9.5, leading=13)
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=18, spaceAfter=2)
    warn = ParagraphStyle("warn", parent=body, textColor=colors.HexColor("#b45309"))
    story = []

    company = "<br/>".join(
        escape(x) for x in [org.address or "", org.vat_number or "", org.email or "", f"IBAN {org.iban}" if org.iban else ""] if x
    )
    meta = [f"<b>{t(lang, 'factuur').upper()}</b>", f"{t(lang, 'nr')} {inv.number or '—'}"]
    if inv.invoice_date:
        meta.append(f"{t(lang, 'factuurdatum')}: {inv.invoice_date:%d-%m-%Y}")
    if due:
        meta.append(f"{t(lang, 'vervaldatum')}: {due:%d-%m-%Y}")
    head = Table(
        [[Paragraph(f"<b>{escape(org.company_name or '[JOUW BEDRIJF]')}</b><br/>{company}", body), Paragraph("<br/>".join(meta), body)]],
        colWidths=[100 * mm, 74 * mm],
    )
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (1, 0), (1, 0), "RIGHT")]))
    story += [head, Spacer(1, 6 * mm)]
    client = f"<b>{t(lang, 'klant')}</b><br/>{escape(inv.client_name or '[KLANT]')}<br/>{escape(inv.client_address or '')}"
    if inv.client_vat:
        client += f"<br/>{t(lang, 'btw_nr')} {escape(inv.client_vat)}"
    story += [Paragraph(client, body), Spacer(1, 4 * mm)]
    if inv.status == "concept":
        story += [Paragraph(f"<b>{t(lang, 'concept')}</b>", warn), Spacer(1, 2 * mm)]
    if inv.title:
        story += [Paragraph(escape(inv.title), h1)]

    basis = t(lang, "incl_btw" if incl else "excl_btw")
    rows = [[t(lang, "omschrijving"), t(lang, "hoev"), t(lang, "eenh"), f"{t(lang, 'prijs_eenh')}\n{basis}", f"{t(lang, 'totaal')}\n{basis}"]]
    for ln in lines:
        amount = ln["shown"] if incl else ln["amount"]
        unit_price = ln["shown_unit"] if incl else ln["unit_price"]
        rows.append([Paragraph(escape(ln["description"]), body), _num(ln["qty"], lang), ln["unit"], _eur(unit_price), _eur(amount)])
    n_lines = len(rows)
    if incl:
        rows.append(["", "", "", t(lang, "totaal_incl"), _eur(tot["total"])])
        for g in tot["rates"]:
            rows.append(["", "", "", t(lang, "waarvan_btw", pct=_num(g["pct"], lang)), _eur(g["vat"])])
        rows.append(["", "", "", t(lang, "totaal_excl"), _eur(tot["subtotal"])])
        bold_row = n_lines
    else:
        rows.append(["", "", "", t(lang, "totaal_excl"), _eur(tot["subtotal"])])
        for g in tot["rates"]:
            rows.append(["", "", "", t(lang, "btw_pct", pct=_num(g["pct"], lang)), _eur(g["vat"])])
        rows.append(["", "", "", t(lang, "totaal_incl"), _eur(tot["total"])])
        bold_row = len(rows) - 1
    table = Table(rows, colWidths=[86 * mm, 18 * mm, 14 * mm, 28 * mm, 28 * mm], repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E6E6E6")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBELOW", (0, 0), (-1, n_lines - 1), 0.3, colors.HexColor("#DCDCDC")),
                ("LINEABOVE", (3, n_lines), (-1, n_lines), 0.8, colors.black),
                ("FONTNAME", (3, bold_row), (-1, bold_row), "Helvetica-Bold"),  # the amount to pay
            ]
        )
    )
    story += [table, Spacer(1, 6 * mm)]

    if inv.structured_ref and due:
        key = "betaling" if org.iban else "betaling_zonder_iban"
        story += [Paragraph(t(lang, key, amount=_eur(tot["total"]), due=f"{due:%d/%m/%Y}", iban=org.iban or "", ref=inv.structured_ref), body)]
    if inv.paid_on:
        story += [Spacer(1, 2 * mm), Paragraph(f"<b>{t(lang, 'betaald', date=f'{inv.paid_on:%d/%m/%Y}')}</b>", body)]
    if inv.notes:
        story += [Spacer(1, 4 * mm), Paragraph(f"<b>{t(lang, 'opmerkingen')}</b>", body), Paragraph(escape(inv.notes), small)]
    doc.build(story)
    return buf.getvalue()
