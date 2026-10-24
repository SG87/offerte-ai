"""Geometric measures on a Zone. Pure functions; every number here is explainable."""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.schemas import Zone


@dataclass(frozen=True)
class Measure:
    gross: float
    deduction: float
    net: float
    unit: str
    note: str | None = None


def _openings_area(zone: Zone, min_m2: float) -> float:
    """Area of openings that are large enough to be deducted."""
    return sum(o.area_m2 for o in zone.openings if o.width_m * o.height_m >= min_m2)


def _reveals_area(zone: Zone, reveal_depth_m: float) -> float:
    """Reveal (dagkant) surface: two jambs + head for every opening, times reveal depth."""
    if reveal_depth_m <= 0:
        return 0.0
    return sum((2 * o.height_m + o.width_m) * reveal_depth_m * o.count for o in zone.openings)


def estimate_perimeter(area_m2: float) -> float:
    """Fallback when a plan gives floor area but no wall lengths: assume a square room."""
    return 4 * math.sqrt(area_m2)


def wall_net(zone: Zone, height_m: float, params: dict) -> Measure:
    perimeter = zone.perimeter_m if zone.perimeter_m is not None else estimate_perimeter(zone.area_m2 or 0.0)
    gross = perimeter * height_m
    deduction = _openings_area(zone, float(params.get("opening_deduct_min_m2", 1.0)))
    reveals = _reveals_area(zone, float(params.get("reveal_depth_m", 0.15)))
    net = max(gross - deduction + reveals, 0.0)
    note = f"omtrek {perimeter:.2f} m × hoogte {height_m:.2f} m − openingen {deduction:.2f} m² + dagkanten {reveals:.2f} m²"
    return Measure(round(gross, 2), round(deduction, 2), round(net, 2), "m2", note)


def ceiling(zone: Zone, params: dict) -> Measure:
    area = zone.area_m2 or 0.0
    return Measure(round(area, 2), 0.0, round(area, 2), "m2", "plafond = vloeroppervlak")


def floor(zone: Zone, params: dict) -> Measure:
    area = zone.area_m2 or 0.0
    return Measure(round(area, 2), 0.0, round(area, 2), "m2")


def perimeter_minus_doors(zone: Zone, params: dict) -> Measure:
    perimeter = zone.perimeter_m if zone.perimeter_m is not None else estimate_perimeter(zone.area_m2 or 0.0)
    doors = sum(o.width_m * o.count for o in zone.openings if o.kind in ("deur", "poort"))
    net = max(perimeter - doors, 0.0)
    return Measure(round(perimeter, 2), round(doors, 2), round(net, 2), "lm", "omtrek − deurbreedtes")


def doors_count(zone: Zone, params: dict) -> Measure:
    n = sum(o.count for o in zone.openings if o.kind in ("deur", "poort"))
    return Measure(n, 0, n, "stuk")


def windows_count(zone: Zone, params: dict) -> Measure:
    n = sum(o.count for o in zone.openings if o.kind == "raam")
    return Measure(n, 0, n, "stuk")


def area(zone: Zone, params: dict) -> Measure:
    a = zone.area_m2 or 0.0
    return Measure(round(a, 2), 0.0, round(a, 2), "m2")


def length(zone: Zone, params: dict) -> Measure:
    length_m = zone.length_m if zone.length_m is not None else (zone.perimeter_m or 0.0)
    return Measure(round(length_m, 2), 0.0, round(length_m, 2), "lm")


def count(zone: Zone, params: dict) -> Measure:
    n = zone.count or 0
    return Measure(n, 0, n, "stuk")


def volume(zone: Zone, params: dict, default_depth_m: float) -> Measure:
    a = zone.area_m2 or 0.0
    depth = zone.depth_m if zone.depth_m is not None else default_depth_m
    v = a * depth
    return Measure(round(v, 3), 0.0, round(v, 3), "m3", f"{a:.2f} m² × diepte {depth:.2f} m")


def per_zone(zone: Zone, params: dict) -> Measure:
    return Measure(1, 0, 1, "ruimte")


def measure(zone: Zone, name: str, params: dict, height_m: float, post: dict) -> Measure:
    """Dispatch a measure by name. Unknown names raise, so skills can't silently miscompute."""
    if name == "wall_net":
        return wall_net(zone, height_m, params)
    if name == "ceiling":
        return ceiling(zone, params)
    if name == "floor":
        return floor(zone, params)
    if name == "perimeter_minus_doors":
        return perimeter_minus_doors(zone, params)
    if name == "doors_count":
        return doors_count(zone, params)
    if name == "windows_count":
        return windows_count(zone, params)
    if name == "area":
        return area(zone, params)
    if name == "length":
        return length(zone, params)
    if name == "count":
        return count(zone, params)
    if name == "volume":
        depth_param = post.get("depth_param", "default_depth_m")
        return volume(zone, params, float(params.get(depth_param, 0.25)))
    if name == "per_zone":
        return per_zone(zone, params)
    raise ValueError(f"Onbekende maatregel in skill: {name!r}")
