"""Distance by road from the company to the work site, for the kilometre allowance on the quote.

Geocoding with OpenStreetMap Nominatim, routing with OSRM: both public services with a fair-use policy, so the
caller looks a distance up once per address pair and keeps it on the project. An address Nominatim can't
find falls back to the centre of its Belgian postcode (app/data/postcodes_be.tsv), and the note says so.
Nothing found -> None: the user types the distance on the project page.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app import address
from app.config import get_settings

log = logging.getLogger(__name__)
TIMEOUT = 6.0


@dataclass
class Distance:
    km: float  # one way, by road
    source: str  # route | postcode
    note: str


def _headers() -> dict[str, str]:
    # Nominatim asks for a User-Agent that identifies the application
    return {"User-Agent": f"Offerte-AI/0.1 (+{get_settings().base_url})", "Accept-Language": "nl"}


def geocode(text: str, country: str | None = None) -> tuple[float, float] | None:
    s = get_settings()
    params = {"q": text, "format": "jsonv2", "limit": 1}
    if country and len(country) == 2:
        params["countrycodes"] = country.lower()
    try:
        r = httpx.get(s.geocoder_url, params=params, headers=_headers(), timeout=TIMEOUT)
        r.raise_for_status()
        hits = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Geocoding mislukt voor %r: %s", text, exc)
        return None
    if not hits:
        return None
    return float(hits[0]["lat"]), float(hits[0]["lon"])


def route_km(a: tuple[float, float], b: tuple[float, float]) -> float | None:
    s = get_settings()
    url = f"{s.router_url.rstrip('/')}/route/v1/driving/{a[1]},{a[0]};{b[1]},{b[0]}"
    try:
        r = httpx.get(url, params={"overview": "false"}, headers=_headers(), timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Route mislukt: %s", exc)
        return None
    if data.get("code") != "Ok" or not data.get("routes"):
        return None
    return round(data["routes"][0]["distance"] / 1000, 1)


def _locate(text: str, country: str | None) -> tuple[tuple[float, float], bool] | None:
    """(point, exact); not exact = centre of the postcode."""
    if point := geocode(text, country):
        return point, True
    parts = address.parse(text)
    if (country or parts["country"] or address.HOME) == "BE" and (point := address.centre(parts["postal_code"])):
        return point, False
    return None


def road_distance(origin: str, destination: str, country: str | None = None) -> Distance | None:
    """Origin and destination on one line ('Kerkstraat 1, 3000 Leuven'). None when it can't be found."""
    if not get_settings().geo_enabled:
        return None
    a, b = _locate(origin, country), _locate(destination, country)
    if not a or not b:
        return None
    km = route_km(a[0], b[0])
    if km is None:
        return None
    exact = a[1] and b[1]
    return Distance(km, "route" if exact else "postcode",
                    "berekend over de weg" if exact else "berekend over de weg, vanaf het midden van de postcode")
