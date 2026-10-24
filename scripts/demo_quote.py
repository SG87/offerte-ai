"""Demo zonder web-app of API-key: geometrie-JSON -> meetstaat + offerte + plan.

    python scripts/demo_quote.py tests/fixtures/sample_geometry.json [--sector schilder] [--scope muren,plafonds,houtwerk]

Schrijft data/outputs/demo-offerte.pdf, demo-meetstaat.xlsx en demo-plan.svg.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.engine import calculate_quote, load_skill  # noqa: E402
from app.outputs import build_meetstaat_xlsx, build_offerte_pdf, build_plan_svg  # noqa: E402
from app.outputs.offerte_pdf import OffertePartijen  # noqa: E402
from app.schemas import Geometry  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("geometry", type=Path)
    ap.add_argument("--sector", default="schilder")
    ap.add_argument("--scope", default="muren,plafonds,houtwerk")
    args = ap.parse_args()

    geometry = Geometry.model_validate(json.loads(args.geometry.read_text(encoding="utf-8")))
    skill = load_skill(args.sector)
    config = skill.default_price_config()
    result = calculate_quote(geometry, skill, config, scope=args.scope.split(","))

    print(f"{'Zone':<14}{'Post':<34}{'Netto':>9} {'Eenh':<6}{'Uren':>7}{'Regel €':>10}")
    for line in result.lines:
        print(
            f"{line.zone_name:<14}{line.post_label[:33]:<34}{line.net_qty:>9.2f} {line.unit:<6}{line.hours:>7.2f}{line.line_total:>10.2f}"
        )
    t = result.totals
    print(
        f"\nTotaal: {t.area_total_m2} m² · {t.hours_total} u · excl. btw € {t.subtotal_excl} · incl. € {t.total_incl}"
    )
    print("Indicatief:", result.indicative)
    for a in result.assumptions:
        print("  ⚠", a.text)
    for w in result.warnings:
        print("  !", w)

    out = get_settings().outputs_dir
    parties = OffertePartijen(
        company_name="[JOUW BEDRIJF]",
        company_address="[adres]",
        company_vat="[btw-nr]",
        company_email="[e-mail]",
        client_name="[KLANT]",
        client_address="[werfadres]",
        number="DEMO-0001",
        title=f"Offerte {skill.label.lower()}werk",
        intro="Hierbij onze offerte, gemeten op het aangeleverde plan.",
        conditions="Betaling 30 dagen na factuurdatum. Prijzen geldig 30 dagen.",
        validity_days=config.validity_days,
    )
    (out / "demo-offerte.pdf").write_bytes(build_offerte_pdf(result, parties))
    (out / "demo-meetstaat.xlsx").write_bytes(build_meetstaat_xlsx(result, "Meetstaat demo"))
    (out / "demo-plan.svg").write_text(build_plan_svg(geometry, result), encoding="utf-8")
    print(f"\nBestanden in {out.resolve()}: demo-offerte.pdf, demo-meetstaat.xlsx, demo-plan.svg")


if __name__ == "__main__":
    main()
