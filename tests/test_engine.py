"""Engine tests: every number is checked by hand so a skill change can't silently break the maths."""

import json
from pathlib import Path

import pytest

from app.engine import calculate_quote, load_skill
from app.engine.geometry import wall_net
from app.schemas import Adjustment, Geometry, Opening, Zone

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def geometry() -> Geometry:
    return Geometry.model_validate(json.loads((FIXTURES / "sample_geometry.json").read_text()))


def test_wall_net_subtracts_large_openings_and_adds_reveals():
    zone = Zone(
        id="r",
        name="Test",
        perimeter_m=20.0,
        openings=[
            Opening(kind="raam", width_m=1.0, height_m=1.0),  # exactly 1 m² -> deducted
            Opening(kind="raam", width_m=0.5, height_m=0.5),  # 0.25 m² -> not deducted, reveals still count
        ],
    )
    m = wall_net(zone, height_m=2.5, params={"opening_deduct_min_m2": 1.0, "reveal_depth_m": 0.15})
    # gross 50, deduction 1.0, reveals (2*1+1)*0.15 + (2*0.5+0.5)*0.15 = 0.45 + 0.225 = 0.675
    assert m.gross == 50.0
    assert m.deduction == 1.0
    assert m.net == pytest.approx(49.68, abs=0.01)


def test_schilder_quote_lines_and_totals(geometry: Geometry):
    skill = load_skill("schilder")
    config = skill.default_price_config()
    result = calculate_quote(geometry, skill, config, scope=["muren", "plafonds"])

    # Badkamer is out of scope -> no lines
    assert not any(line.zone_id == "badkamer" for line in result.lines)

    living_wall = next(
        line for line in result.lines if line.zone_id == "living" and line.post_code == "muurverf"
    )
    # gross 21.6*2.6 = 56.16; deduction 2*(1.8*1.4)=5.04 + 0.83*2.04=1.6932 -> 6.73
    # reveals: ramen 2*(2*1.4+1.8)*0.15 = 1.38 ; deur (2*2.04+0.83)*0.15 = 0.7365 -> 2.1165
    assert living_wall.gross_qty == pytest.approx(56.16, abs=0.01)
    assert living_wall.deduction_qty == pytest.approx(6.73, abs=0.01)
    assert living_wall.net_qty == pytest.approx(51.55, abs=0.02)
    assert living_wall.layers == 2
    # material: 51.55 m² * 2 lagen * 1.10 waste / 8 m²/l = 14.18 l
    assert living_wall.material_qty == pytest.approx(14.18, abs=0.05)
    assert living_wall.material_unit == "l"

    # Keuken has tag 'vochtig' -> vochtwerend replaces muurverf
    keuken_codes = {line.post_code for line in result.lines if line.zone_id == "keuken"}
    assert "muurverf_vochtwerend" in keuken_codes
    assert "muurverf" not in keuken_codes

    # ceiling only when 'plafonds' in scope; houtwerk not in scope -> no deuren/plinten
    assert any(line.post_code == "plafondverf" for line in result.lines)
    assert not any(line.post_code in ("lak_deuren", "plinten") for line in result.lines)

    # keuken has no height -> assumption from config; scale confirmed -> no scale assumption
    keys = {a.key for a in result.assumptions}
    assert "height:keuken" in keys
    assert "scale" not in keys
    assert result.indicative is True  # unconfirmed height assumption

    # totals are internally consistent
    t = result.totals
    assert t.subtotal_excl == pytest.approx(t.material_cost + t.margin + t.labour_cost, abs=0.02)
    assert t.total_incl == pytest.approx(t.subtotal_excl * 1.06, abs=0.02)
    assert t.hours_total % 0.5 == 0


def test_adjustments_change_the_result(geometry: Geometry):
    skill = load_skill("schilder")
    config = skill.default_price_config()
    base = calculate_quote(geometry, skill, config, scope=["muren"])
    adj = [
        Adjustment(op="set_layers", post="muurverf", layers=3, match="living"),
        Adjustment(op="exclude_zones", match="hal"),
        Adjustment(op="hours_factor", when="height_gt", value=2.5, factor=1.5),
    ]
    changed = calculate_quote(geometry, skill, config, scope=["muren"], adjustments=adj)

    living = next(line for line in changed.lines if line.zone_id == "living" and line.post_code == "muurverf")
    assert living.layers == 3
    slaap = next(
        line for line in changed.lines if line.zone_id == "slaapkamer1" and line.post_code == "muurverf"
    )
    assert slaap.layers == 2  # match only applied to living
    assert not any(line.zone_id == "hal" for line in changed.lines)
    assert changed.totals.hours_total > base.totals.hours_total


def test_missing_price_item_gives_warning_not_crash(geometry: Geometry):
    skill = load_skill("schilder")
    config = skill.default_price_config()
    config.items = [i for i in config.items if i.code != "plafondverf"]
    result = calculate_quote(geometry, skill, config, scope=["plafonds"])
    assert any("plafondverf" in w for w in result.warnings)
    assert all(line.material_cost == 0 for line in result.lines if line.post_code == "plafondverf")


def test_tuinaanleg_skill():
    skill = load_skill("tuinaanleg")
    config = skill.default_price_config()
    g = Geometry(
        scale_confirmed=True,
        zones=[
            Zone(id="t", name="Terras", kind="terras", area_m2=20.0),
            Zone(id="g", name="Gazon", kind="gazon", area_m2=80.0),
            Zone(id="h", name="Haag achteraan", kind="haag", length_m=12.0),
            Zone(id="b", name="Border", kind="border", count=15),
        ],
    )
    result = calculate_quote(g, skill, config, scope=["terras", "gazon", "beplanting", "graafwerk"])
    codes = {(line.zone_id, line.post_code) for line in result.lines}
    assert (
        ("t", "graafwerk") in codes
        and ("t", "fundering_stabilise") in codes
        and ("t", "terras_tegels") in codes
    )
    assert ("g", "gazon_aanleg") in codes and ("h", "haag") in codes and ("b", "beplanting") in codes
    fund = next(line for line in result.lines if line.post_code == "fundering_stabilise")
    assert fund.net_qty == pytest.approx(20 * 0.15, abs=0.001)
    haag = next(line for line in result.lines if line.post_code == "haag")
    assert haag.material_qty == pytest.approx(12 / 0.25 * 1.08, abs=0.01)  # 4 planten/lm + 8 % verlies
    assert result.indicative is False
