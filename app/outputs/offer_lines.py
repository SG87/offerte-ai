"""Quote lines as the customer reads them: incl. btw for B2C, excl. btw for B2B.

Shared by the web offerte and the PDF so both show the same amounts, and the lines always add up to the
headline total (cent differences from rounding btw per line go into the last line)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.i18n import t, unit
from app.schemas import QuoteResult


@dataclass
class OfferLine:
    description: str
    qty: float | None
    unit: str
    unit_price: float | None
    amount: float


def _pct(v: float, lang: str = "nl") -> str:
    return f"{v:g}" if lang == "en" else f"{v:g}".replace(".", ",")


def basis_label(incl_vat: bool, lang: str = "nl") -> str:
    return t(lang, "incl_btw" if incl_vat else "excl_btw")


def offer_lines(
    result: QuoteResult, incl_vat: bool, lang: str = "nl", tr: Callable[[str], str] | None = None
) -> list[OfferLine]:
    """`tr` translates free texts (zone names, post labels) into `lang`; the fixed words come from app.i18n."""
    tr = tr or (lambda text: text)
    tot = result.totals
    factor = 1 + tot.vat_pct / 100 if incl_vat else 1.0
    lines = []
    for line in result.lines:
        amount = round(line.line_total * factor, 2)
        lines.append(
            OfferLine(
                description=f"{tr(line.zone_name)}: {tr(line.post_label)}"
                + (f" ({t(lang, 'lagen', n=line.layers)})" if line.layers > 1 else ""),
                qty=line.net_qty,
                unit=unit(lang, line.unit),
                unit_price=amount / line.net_qty if line.net_qty else 0.0,
                amount=amount,
            )
        )
    target = tot.total_incl if incl_vat else tot.subtotal_excl
    rest = round(target - sum(ln.amount for ln in lines), 2)
    # labour is priced per line; the rounding of total hours is a separate line when it differs
    hours_rounding = round(tot.subtotal_excl - sum(line.line_total for line in result.lines), 2)
    if abs(hours_rounding) >= 0.01:
        lines.append(OfferLine(t(lang, "afronding_uren"), None, "", None, rest))
    elif lines and abs(rest) >= 0.005:
        last = lines[-1]
        last.amount = round(last.amount + rest, 2)
        last.unit_price = last.amount / last.qty if last.qty else 0.0
    return lines


def offer_totals(result: QuoteResult, incl_vat: bool, lang: str = "nl") -> list[tuple[str, float, bool]]:
    """(label, amount, headline) rows under the lines. The headline total matches the price basis."""
    tot = result.totals
    pct = _pct(tot.vat_pct, lang)
    if incl_vat:
        return [
            (t(lang, "totaal_incl"), tot.total_incl, True),
            (t(lang, "waarvan_btw", pct=pct), tot.vat, False),
            (t(lang, "totaal_excl"), tot.subtotal_excl, False),
        ]
    return [
        (t(lang, "totaal_excl"), tot.subtotal_excl, True),
        (t(lang, "btw_pct", pct=pct), tot.vat, False),
        (t(lang, "totaal_incl"), tot.total_incl, False),
    ]
