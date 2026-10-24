"""Turn Geometry + Skill + PriceConfig (+ user adjustments) into a QuoteResult.

Pipeline:
  1. apply adjustments (user's skill override) to a *copy* of params / posts / zones
  2. per zone in scope, per applicable post: measure -> quantity
  3. price: material (with waste + margin), labour (hours x rate, with height factor)
  4. totals, VAT, rounding, assumptions -> QuoteResult
"""

from __future__ import annotations

import copy
import math
import re
from typing import Any

from app.engine import geometry as geo
from app.engine.skills import Skill
from app.schemas import (
    Adjustment,
    Assumption,
    Geometry,
    MeetstaatLine,
    PriceConfig,
    QuoteResult,
    QuoteTotals,
    Zone,
)


def _matches(pattern: str | None, zone: Zone) -> bool:
    if not pattern:
        return True
    rx = re.compile(pattern, re.IGNORECASE)
    return bool(rx.search(zone.name) or rx.search(zone.kind) or any(rx.search(t) for t in zone.tags))


def apply_adjustments(
    geometry: Geometry, skill: Skill, adjustments: list[Adjustment]
) -> tuple[Geometry, dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (geometry, params, posts, hour_factors) with the user's rules applied."""
    g = geometry.model_copy(deep=True)
    params = copy.deepcopy(skill.params)
    posts = copy.deepcopy(skill.posts)
    hour_factors: list[dict[str, Any]] = []

    for adj in adjustments:
        if adj.op == "set_param" and adj.key:
            params[adj.key] = adj.value
        elif adj.op == "tag_zones" and adj.tag:
            for z in g.zones:
                if _matches(adj.match, z) and adj.tag not in z.tags:
                    z.tags.append(adj.tag)
        elif adj.op == "exclude_zones":
            for z in g.zones:
                if _matches(adj.match, z):
                    z.in_scope = False
        elif adj.op == "set_layers" and adj.post and adj.layers:
            for p in posts:
                if p["code"] == adj.post:
                    if adj.match:
                        p.setdefault("layers_overrides", []).append(
                            {"match": adj.match, "layers": adj.layers}
                        )
                    else:
                        p["layers"] = adj.layers
        elif adj.op == "disable_post" and adj.post:
            posts = [p for p in posts if p["code"] != adj.post]
        elif adj.op == "hours_factor" and adj.factor:
            hour_factors.append({"when": adj.when, "value": adj.value, "factor": adj.factor})
    return g, params, posts, hour_factors


def _resolve_layers(post: dict[str, Any], params: dict[str, Any], zone: Zone) -> int:
    """Layers for this post on this zone: skill default (possibly a param name), then zone-scoped overrides."""
    layers = post.get("layers", 1)
    if isinstance(layers, str):
        layers = params.get(layers, 1)
    for ov in post.get("layers_overrides", []):
        if _matches(ov["match"], zone):
            layers = ov["layers"]
    return int(layers)


def _post_applies(post: dict[str, Any], zone: Zone, scope: list[str], posts: list[dict[str, Any]]) -> bool:
    if zone.kind not in post.get("applies_to", ["*"]) and "*" not in post.get("applies_to", ["*"]):
        return False
    post_scope = post.get("scope", "always")
    if post_scope != "always" and post_scope not in scope:
        return False
    if (tag := post.get("condition_tag")) and tag not in zone.tags:
        return False
    # if another applicable post *replaces* this one for this zone, skip it
    for other in posts:
        if other.get("replaces") == post["code"] and other is not post:
            if (
                (t := other.get("condition_tag"))
                and t in zone.tags
                and other.get("scope", "always") in (*scope, "always")
            ):
                return False
    return True


def _round_up_to(value: float, step: float) -> float:
    if step <= 0:
        return round(value, 2)
    return math.ceil(round(value / step, 6)) * step


def calculate_quote(
    geometry: Geometry,
    skill: Skill,
    config: PriceConfig,
    scope: list[str],
    adjustments: list[Adjustment] | None = None,
    vat_pct: float | None = None,
    override_version: int | None = None,
) -> QuoteResult:
    adjustments = adjustments or []
    g, params, posts, hour_factors = apply_adjustments(geometry, skill, adjustments)
    vat = config.vat_pct if vat_pct is None else vat_pct

    assumptions: list[Assumption] = [a.model_copy() for a in g.assumptions]
    warnings: list[str] = []
    lines: list[MeetstaatLine] = []

    default_height = g.default_height_m or float(params.get("default_height_m", 2.6))
    if not g.scale_confirmed:
        assumptions.append(
            Assumption(
                key="scale",
                text="Schaal van het plan is niet bevestigd; hoeveelheden zijn indicatief.",
                source="skill",
            )
        )

    for zone in g.zones:
        if not zone.in_scope:
            continue
        height = zone.height_m
        if height is None and zone.kind == "ruimte":
            height = default_height
            assumptions.append(
                Assumption(
                    key=f"height:{zone.id}",
                    text=f"{zone.name}: plafondhoogte {default_height:.2f} m aangenomen (uit configuratie).",
                    source="config",
                )
            )
        if zone.kind == "ruimte" and zone.perimeter_m is None and zone.area_m2:
            assumptions.append(
                Assumption(
                    key=f"perimeter:{zone.id}",
                    text=f"{zone.name}: omtrek geschat als vierkante ruimte uit {zone.area_m2:.1f} m² ({geo.estimate_perimeter(zone.area_m2):.2f} m).",
                    source="skill",
                )
            )

        for post in posts:
            if not _post_applies(post, zone, scope, posts):
                continue
            m = geo.measure(zone, post["measure"], params, height or default_height, post)
            if m.net <= 0:
                continue
            layers = _resolve_layers(post, params, zone)
            item = config.item(post["code"])
            if item is None:
                warnings.append(f"Geen prijs geconfigureerd voor post '{post['code']}' — kost op 0 gezet.")
                material_qty = material_cost = hours = 0.0
                material_unit = None
                unit = m.unit
                label = skill.post_label(post["code"])
            else:
                unit = item.unit
                label = item.label
                waste = 1 + config.waste_pct / 100
                coverage = item.coverage or 1.0
                material_qty = m.net * layers * waste / coverage
                material_unit = item.material_unit or item.unit
                material_cost = material_qty * item.material_price
                hours = m.net * item.hours_per_unit * (layers if item.hours_scale_with_layers else 1)
                if (
                    post.get("height_factor")
                    and height
                    and height > float(params.get("high_ceiling_threshold_m", 99))
                ):
                    hours *= float(params.get("high_ceiling_hours_factor", 1.0))
                for hf in hour_factors:
                    if hf["when"] == "height_gt" and height and height > float(hf["value"] or 0):
                        hours *= float(hf["factor"])
            labour_cost = hours * config.hourly_rate
            lines.append(
                MeetstaatLine(
                    zone_id=zone.id,
                    zone_name=zone.name,
                    post_code=post["code"],
                    post_label=label,
                    unit=unit,
                    gross_qty=m.gross,
                    deduction_qty=m.deduction,
                    net_qty=m.net,
                    layers=layers,
                    material_qty=round(material_qty, 2),
                    material_unit=material_unit,
                    material_cost=round(material_cost, 2),
                    hours=round(hours, 2),
                    labour_cost=round(labour_cost, 2),
                    line_total=round(material_cost * (1 + config.margin_pct / 100) + labour_cost, 2),
                    note=m.note,
                )
            )

    material_cost = sum(line.material_cost for line in lines)
    margin = material_cost * config.margin_pct / 100
    hours_total = _round_up_to(sum(line.hours for line in lines), config.round_hours_to)
    labour_cost = hours_total * config.hourly_rate
    subtotal = material_cost + margin + labour_cost
    vat_amount = subtotal * vat / 100
    area_total = sum(line.net_qty for line in lines if line.unit == "m2")

    # dedupe assumptions by key
    seen: set[str] = set()
    unique_assumptions = []
    for a in assumptions:
        if a.key not in seen:
            seen.add(a.key)
            unique_assumptions.append(a)

    indicative = (not g.scale_confirmed) or any(not a.confirmed for a in unique_assumptions)
    return QuoteResult(
        sector=skill.sector,
        lines=lines,
        totals=QuoteTotals(
            area_total_m2=round(area_total, 2),
            hours_total=round(hours_total, 2),
            material_cost=round(material_cost, 2),
            margin=round(margin, 2),
            labour_cost=round(labour_cost, 2),
            subtotal_excl=round(subtotal, 2),
            vat_pct=vat,
            vat=round(vat_amount, 2),
            total_incl=round(subtotal + vat_amount, 2),
            currency=config.currency,
        ),
        assumptions=unique_assumptions,
        warnings=warnings,
        config_version=config.version,
        skill_version=skill.version,
        override_version=override_version,
        indicative=indicative,
    )
