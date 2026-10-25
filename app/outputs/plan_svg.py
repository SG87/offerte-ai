"""Annotated plan as SVG: zones coloured by post, out-of-scope zones hatched. Used inline in the web app and as attachment."""

from __future__ import annotations

import html
import math

from app.schemas import Geometry, QuoteResult

COLOURS = {
    "muurverf": ("#dbe7fb", "#1f5fbf"),
    "muurverf_vochtwerend": ("#cfe0fb", "#1f5fbf"),
    "plafondverf": ("#dbe7fb", "#1f5fbf"),
    "lak_deuren": ("#fbe6d2", "#c2620a"),
    "plinten": ("#fbe6d2", "#c2620a"),
    "terras_tegels": ("#e8e0d2", "#7a5c2e"),
    "oprit_klinkers": ("#e0dad0", "#5c4a30"),
    "gazon_aanleg": ("#dff1d6", "#2f7a2a"),
    "haag": ("#cfe9c6", "#2f7a2a"),
    "beplanting": ("#e6f0d6", "#4f7a1f"),
}
DEFAULT = ("#eeeeee", "#555555")


def _box(z) -> tuple[float, float]:
    """Width and depth of a zone without polygon: from area + perimeter when both are known, else a square."""
    if z.area_m2:
        if z.perimeter_m:
            half = z.perimeter_m / 2  # w + d = P/2, w * d = A
            disc = half * half - 4 * z.area_m2
            if disc >= 0:
                w = (half + math.sqrt(disc)) / 2
                return w, z.area_m2 / w
        side = math.sqrt(z.area_m2)
        return side, side
    return max(z.length_m or 2.0, 1.0), 0.6


def _auto_layout(geometry: Geometry) -> dict[str, list[tuple[float, float]]]:
    """Zones without a polygon: rectangles proportional to their size, wrapped into rows like a floor plan sheet."""
    polys: dict[str, list[tuple[float, float]]] = {}
    boxes = {z.id: _box(z) for z in geometry.zones if not z.polygon}
    gap = 0.6
    total = sum(w * d for w, d in boxes.values())
    row_width = max(math.sqrt(total) * 1.8, max((w for w, _ in boxes.values()), default=1.0))
    x = y = row_depth = 0.0
    for z in geometry.zones:
        if z.polygon:
            polys[z.id] = [tuple(p) for p in z.polygon]
            continue
        w, d = boxes[z.id]
        if x > 0 and x + w > row_width:
            x, y, row_depth = 0.0, y + row_depth + gap, 0.0
        polys[z.id] = [(x, y), (x + w, y), (x + w, y + d), (x, y + d)]
        x += w + gap
        row_depth = max(row_depth, d)
    return polys


def build_plan_svg(geometry: Geometry, result: QuoteResult | None = None, width_px: int = 800) -> str:
    polys = _auto_layout(geometry)
    if not polys:
        return f'<svg xmlns="http://www.w3.org/2000/svg" width="{width_px}" height="120"><text x="16" y="60" font-family="sans-serif">Geen zones</text></svg>'
    xs = [p[0] for poly in polys.values() for p in poly]
    ys = [p[1] for poly in polys.values() for p in poly]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    span_x = max(maxx - minx, 1e-6)
    span_y = max(maxy - miny, 1e-6)
    pad = 24
    scale = (width_px - 2 * pad) / span_x
    height_px = int(span_y * scale + 2 * pad)

    posts_by_zone: dict[str, list[str]] = {}
    if result:
        for line in result.lines:
            posts_by_zone.setdefault(line.zone_id, []).append(line.post_code)

    def tx(x: float) -> float:
        return pad + (x - minx) * scale

    def ty(y: float) -> float:
        return pad + (y - miny) * scale

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width_px}" height="{height_px}" viewBox="0 0 {width_px} {height_px}" font-family="Inter, system-ui, sans-serif">',
        '<defs><pattern id="hatch" width="8" height="8" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
        '<rect width="8" height="8" fill="#f2f2f2"/><line x1="0" y1="0" x2="0" y2="8" stroke="#cfcfcf" stroke-width="4"/></pattern></defs>',
        f'<rect x="0" y="0" width="{width_px}" height="{height_px}" fill="#ffffff"/>',
    ]
    for z in geometry.zones:
        poly = polys[z.id]
        pts = " ".join(f"{tx(x):.1f},{ty(y):.1f}" for x, y in poly)
        codes = posts_by_zone.get(z.id, [])
        if not z.in_scope or (result and not codes):
            fill, stroke = "url(#hatch)", "#8a8a8a"
        else:
            fill, stroke = COLOURS.get(codes[0] if codes else "", DEFAULT)
        parts.append(
            f'<polygon points="{pts}" fill="{fill}" stroke="{stroke}" stroke-width="2.5" data-zone="{html.escape(z.id)}"/>'
        )
        cx = sum(tx(x) for x, _ in poly) / len(poly)
        cy = sum(ty(y) for _, y in poly) / len(poly)
        box_w = max(tx(x) for x, _ in poly) - min(tx(x) for x, _ in poly)
        box_h = max(ty(y) for _, y in poly) - min(ty(y) for _, y in poly)
        name = z.name if len(z.name) * 7 < box_w - 6 else z.name[: max(int((box_w - 6) / 7) - 1, 3)] + "…"
        label = html.escape(name)
        sub = f"{z.area_m2:g} m²" if z.area_m2 else (f"{z.length_m:g} lm" if z.length_m else "")
        if not z.in_scope:
            sub = "buiten scope"
        elif codes:
            sub += " · " + ", ".join(sorted(set(codes)))[:40]
        size = 12 if box_w > 90 else 10
        parts.append(
            f'<text x="{cx:.1f}" y="{cy - 3:.1f}" text-anchor="middle" font-size="{size}" font-weight="600" fill="#2b2b2b"><title>{html.escape(z.name)}</title>{label}</text>'
        )
        if box_h > 34 and box_w > 60:
            sub = sub if len(sub) * 5.5 < box_w - 6 else sub[: max(int((box_w - 6) / 5.5) - 1, 3)] + "…"
            parts.append(
                f'<text x="{cx:.1f}" y="{cy + 12:.1f}" text-anchor="middle" font-size="10" fill="#444444">{html.escape(sub)}</text>'
            )
        # openings as small ticks along the first edge (schematic, not positioned)
        if z.openings and len(poly) >= 2:
            (x0, y0), (x1, y1) = poly[0], poly[1]
            n = sum(o.count for o in z.openings)
            for i in range(n):
                f = (i + 1) / (n + 1)
                ox, oy = tx(x0 + (x1 - x0) * f), ty(y0 + (y1 - y0) * f)
                parts.append(
                    f'<circle cx="{ox:.1f}" cy="{oy:.1f}" r="4" fill="#ffffff" stroke="{stroke}" stroke-width="2"/>'
                )
    parts.append("</svg>")
    return "".join(parts)
