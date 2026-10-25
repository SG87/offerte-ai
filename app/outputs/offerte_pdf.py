"""Offerte as PDF (reportlab, pure Python). Lines are grouped per zone; the meetstaat is an appendix."""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import date, timedelta
from html import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.i18n import t, unit
from app.outputs.offer_lines import basis_label, offer_lines, offer_totals
from app.schemas import QuoteResult


@dataclass
class OffertePartijen:
    company_name: str
    company_address: str | None
    company_vat: str | None
    company_email: str | None
    client_name: str | None
    client_address: str | None
    number: str
    title: str
    intro: str | None = None
    conditions: str | None = None
    validity_days: int = 30
    prices_incl_vat: bool = True  # B2C: incl. btw; B2B: excl. btw (btw added at the bottom)
    client_vat: str | None = None
    language: str = "nl"  # the customer's language (app/i18n.py)
    translations: dict[str, str] = field(default_factory=dict)  # free texts (post labels, rooms, assumptions)


def _eur(v: float) -> str:
    return f"€ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _qty(v: float) -> str:
    return f"{v:g}".replace(".", ",")


def build_offerte_pdf(result: QuoteResult, parties: OffertePartijen) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm
    )
    styles = getSampleStyleSheet()
    small = ParagraphStyle(
        "small", parent=styles["Normal"], fontSize=8.5, leading=11, textColor=colors.HexColor("#444444")
    )
    body = ParagraphStyle("body", parent=styles["Normal"], fontSize=9.5, leading=13)
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=18, spaceAfter=2)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=12, spaceBefore=8, spaceAfter=4)

    lang = parties.language

    def tr(text: str) -> str:
        return parties.translations.get(text, text)

    today = result.created_on or date.today()
    valid = today + timedelta(days=parties.validity_days)
    story = []

    head = Table(
        [
            [
                Paragraph(
                    f"<b>{parties.company_name}</b><br/>{parties.company_address or ''}<br/>{parties.company_vat or ''}<br/>{parties.company_email or ''}",
                    body,
                ),
                Paragraph(
                    f"<b>{t(lang, 'offerte').upper()}</b><br/>{t(lang, 'nr')} {parties.number}<br/>"
                    f"{t(lang, 'datum')}: {today:%d-%m-%Y}<br/>{t(lang, 'geldig_tot')}: {valid:%d-%m-%Y}",
                    body,
                ),
            ]
        ],
        colWidths=[100 * mm, 74 * mm],
    )
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (1, 0), (1, 0), "RIGHT")]))
    story += [head, Spacer(1, 6 * mm)]
    story += [
        Paragraph(
            f"<b>{t(lang, 'klant')}</b><br/>{escape(parties.client_name or '[KLANT]')}<br/>{escape(parties.client_address or '')}"
            + (f"<br/>{t(lang, 'btw_nr')} {parties.client_vat}" if parties.client_vat else ""),
            body,
        ),
        Spacer(1, 4 * mm),
    ]
    story += [Paragraph(escape(parties.title), h1)]
    if parties.intro:
        story += [Paragraph(escape(parties.intro), body), Spacer(1, 3 * mm)]
    if result.indicative:
        story += [
            Paragraph(
                f"<i>{t(lang, 'indicatief')}</i>",
                small,
            ),
            Spacer(1, 2 * mm),
        ]

    # quote lines (per zone/post, material+labour merged into one price per unit), on the customer's price basis
    basis = basis_label(parties.prices_incl_vat, lang)
    rows = [[t(lang, "omschrijving"), t(lang, "hoev"), t(lang, "eenh"), f"{t(lang, 'prijs_eenh')}\n{basis}", f"{t(lang, 'totaal')}\n{basis}"]]
    for ol in offer_lines(result, parties.prices_incl_vat, lang, tr):
        rows.append(
            [
                Paragraph(escape(ol.description), body),
                _qty(ol.qty) if ol.qty is not None else "",
                ol.unit,
                _eur(ol.unit_price) if ol.unit_price is not None else "",
                _eur(ol.amount),
            ]
        )
    totals = result.totals
    rows += [["", "", "", label, _eur(amount)] for label, amount, _ in offer_totals(result, parties.prices_incl_vat, lang)]
    table = Table(rows, colWidths=[86 * mm, 18 * mm, 14 * mm, 28 * mm, 28 * mm], repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E6E6E6")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBELOW", (0, 0), (-1, -4), 0.3, colors.HexColor("#DCDCDC")),
                ("FONTNAME", (3, -3), (-1, -3), "Helvetica-Bold"),  # headline total, on the price basis
                ("TEXTCOLOR", (3, -2), (-1, -1), colors.HexColor("#444444")),
                ("LINEABOVE", (3, -3), (-1, -3), 0.8, colors.black),
            ]
        )
    )
    story += [table, Spacer(1, 5 * mm)]

    story += [Paragraph(t(lang, "inbegrepen"), h2)]
    excluded = [escape(tr(a.text)) for a in result.assumptions]
    story += [Paragraph(t(lang, "gemeten"), body)]
    if excluded:
        story += [Paragraph(f"{t(lang, 'aannames')}: " + " · ".join(excluded), small)]
    if parties.conditions:
        story += [Paragraph(t(lang, "voorwaarden"), h2), Paragraph(escape(parties.conditions), small)]

    story += [
        Spacer(1, 10 * mm),
        Paragraph(t(lang, "handtekening"), small),
    ]

    # appendix: meetstaat
    story += [Spacer(1, 8 * mm), Paragraph(t(lang, "bijlage_meetstaat"), h2)]
    mrows = [[t(lang, k) for k in ("ruimte_zone", "post", "bruto", "aftrek", "netto", "eenh", "uren")]]
    for line in result.lines:
        mrows.append(
            [
                tr(line.zone_name),
                Paragraph(escape(tr(line.post_label)), small),
                _qty(line.gross_qty),
                _qty(line.deduction_qty),
                _qty(line.net_qty),
                unit(lang, line.unit),
                _qty(line.hours),
            ]
        )
    mrows.append([t(lang, "totaal"), "", "", "", f"{_qty(totals.area_total_m2)} m²", "", _qty(totals.hours_total)])
    mt = Table(mrows, colWidths=[30 * mm, 70 * mm, 16 * mm, 16 * mm, 18 * mm, 12 * mm, 12 * mm], repeatRows=1)
    mt.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E6E6E6")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
                ("LINEBELOW", (0, 0), (-1, -2), 0.3, colors.HexColor("#DCDCDC")),
                ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ]
        )
    )
    story += [mt]

    doc.build(story)
    return buf.getvalue()
