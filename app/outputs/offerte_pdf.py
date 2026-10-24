"""Offerte as PDF (reportlab, pure Python). Lines are grouped per zone; the meetstaat is an appendix."""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date, timedelta

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

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

    today = result.created_on or date.today()
    story = []

    head = Table(
        [
            [
                Paragraph(
                    f"<b>{parties.company_name}</b><br/>{parties.company_address or ''}<br/>{parties.company_vat or ''}<br/>{parties.company_email or ''}",
                    body,
                ),
                Paragraph(
                    f"<b>OFFERTE</b><br/>Nr. {parties.number}<br/>Datum: {today:%d-%m-%Y}<br/>Geldig tot: {today + timedelta(days=parties.validity_days):%d-%m-%Y}",
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
            f"<b>Klant</b><br/>{parties.client_name or '[KLANT]'}<br/>{parties.client_address or ''}", body
        ),
        Spacer(1, 4 * mm),
    ]
    story += [Paragraph(parties.title, h1)]
    if parties.intro:
        story += [Paragraph(parties.intro, body), Spacer(1, 3 * mm)]
    if result.indicative:
        story += [
            Paragraph(
                "<i>Indicatieve offerte: hoeveelheden gemeten op plan; aannames hieronder zijn nog te bevestigen.</i>",
                small,
            ),
            Spacer(1, 2 * mm),
        ]

    # quote lines (per zone/post, material+labour merged into one price per unit)
    rows = [["Omschrijving", "Hoev.", "Eenh.", "Prijs/eenh.", "Totaal"]]
    for line in result.lines:
        desc = f"{line.zone_name}: {line.post_label}" + (f" ({line.layers} lagen)" if line.layers > 1 else "")
        unit_price = line.line_total / line.net_qty if line.net_qty else 0.0
        rows.append(
            [Paragraph(desc, body), _qty(line.net_qty), line.unit, _eur(unit_price), _eur(line.line_total)]
        )
    t = result.totals
    # labour is priced per line above; the rounding of total hours is shown as a separate line when it differs
    line_sum = sum(line.line_total for line in result.lines)
    rounding = round(t.subtotal_excl - line_sum, 2)
    if abs(rounding) >= 0.01:
        rows.append([Paragraph("Afronding uren", body), "", "", "", _eur(rounding)])
    rows += [
        ["", "", "", "Subtotaal excl. btw", _eur(t.subtotal_excl)],
        ["", "", "", f"Btw {t.vat_pct:g} %", _eur(t.vat)],
        ["", "", "", "Totaal incl. btw", _eur(t.total_incl)],
    ]
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
                ("FONTNAME", (3, -1), (-1, -1), "Helvetica-Bold"),
                ("LINEABOVE", (3, -3), (-1, -3), 0.8, colors.black),
            ]
        )
    )
    story += [table, Spacer(1, 5 * mm)]

    story += [Paragraph("Inbegrepen / niet inbegrepen", h2)]
    excluded = [a.text for a in result.assumptions]
    story += [
        Paragraph(
            "Hoeveelheden zijn gemeten op het aangeleverde plan; afwijkingen op de werf worden verrekend als meerwerk.",
            body,
        )
    ]
    if excluded:
        story += [Paragraph("Aannames: " + " · ".join(excluded), small)]
    if parties.conditions:
        story += [Paragraph("Voorwaarden", h2), Paragraph(parties.conditions, small)]

    story += [
        Spacer(1, 10 * mm),
        Paragraph(
            "Handtekening aannemer: ____________________ &nbsp;&nbsp;&nbsp;&nbsp; Voor akkoord klant (naam, datum): ____________________",
            small,
        ),
    ]

    # appendix: meetstaat
    story += [Spacer(1, 8 * mm), Paragraph("Bijlage A — Meetstaat", h2)]
    mrows = [["Ruimte/zone", "Post", "Bruto", "Aftrek", "Netto", "Eenh.", "Uren"]]
    for line in result.lines:
        mrows.append(
            [
                line.zone_name,
                Paragraph(line.post_label, small),
                _qty(line.gross_qty),
                _qty(line.deduction_qty),
                _qty(line.net_qty),
                line.unit,
                _qty(line.hours),
            ]
        )
    mrows.append(["Totaal", "", "", "", f"{_qty(t.area_total_m2)} m²", "", _qty(t.hours_total)])
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
