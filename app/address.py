"""Postal addresses of customers and suppliers: street, house number, postcode, city and country.

parse() splits an address written on one line ('Kerkstraat 12 bus 3, 3000 Leuven'). complete() fills in a
missing postcode, city or country only where the Belgian postcode list (app/data/postcodes_be.tsv) leaves no
doubt: 3000 -> Leuven, but 'Antwerpen' has several postcodes and 3020 several towns, so those stay empty.
What does not add up (3000 Gent) is reported, never corrected. Nothing is guessed.
"""

from __future__ import annotations

import math
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any

PARTS = ("street", "house_number", "postal_code", "city", "country")
HOME = "BE"  # left out when the address is written on one line
COUNTRIES = {"BE": "België", "NL": "Nederland", "FR": "Frankrijk", "DE": "Duitsland", "LU": "Luxemburg"}
DATA_FILE = Path(__file__).parent / "data" / "postcodes_be.tsv"
NEAR_KM = 8.0  # a city this close to the postcode's area counts as matching (Kessel-Lo written as Leuven)


def _key(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return re.sub(r"[^a-z0-9]+", "", "".join(c for c in s if not unicodedata.combining(c)).lower())


_COUNTRY_NAMES = {
    _key(name): code
    for code, names in {
        "BE": ["België", "Belgie", "Belgium", "Belgique", "Belgien"],
        "NL": ["Nederland", "Netherlands", "The Netherlands", "Pays-Bas", "Holland", "Niederlande"],
        "FR": ["Frankrijk", "France", "Frankreich"],
        "DE": ["Duitsland", "Germany", "Allemagne", "Deutschland"],
        "LU": ["Luxemburg", "Luxembourg", "Groothertogdom Luxemburg"],
    }.items()
    for name in names
}
_PREFIX = {"B": "BE", "BE": "BE", "NL": "NL", "F": "FR", "FR": "FR", "D": "DE", "DE": "DE", "L": "LU", "LU": "LU"}

# Names in the other national language -> the name in the postcode list (Brussels and a few border towns are
# only listed in French there). Some names are also a town of their own (Vorst): then both count.
_ALIASES = {
    _key(a): _key(b)
    for a, b in {
        "Brussel": "Bruxelles", "Brussels": "Bruxelles", "Laken": "Laeken", "Schaarbeek": "Schaerbeek",
        "Elsene": "Ixelles", "Sint-Gillis": "Saint-Gilles", "Sint-Jans-Molenbeek": "Molenbeek-Saint-Jean",
        "Molenbeek": "Molenbeek-Saint-Jean", "Sint-Agatha-Berchem": "Berchem-Sainte-Agathe",
        "Sint-Pieters-Woluwe": "Woluwe-Saint-Pierre", "Oudergem": "Auderghem",
        "Watermaal-Bosvoorde": "Watermael-Boitsfort", "Ukkel": "Uccle", "Vorst": "Forest",
        "Sint-Lambrechts-Woluwe": "Woluwe-Saint-Lambert", "Sint-Joost-ten-Node": "Saint-Josse-Ten-Noode",
        "Luik": "Liège", "Namen": "Namur", "Doornik": "Tournai", "Nijvel": "Nivelles", "Waver": "Wavre",
        "Aarlen": "Arlon", "Hoei": "Huy", "Edingen": "Enghien", "Moeskroen": "Mouscron", "Geldenaken": "Jodoigne",
        "Bastenaken": "Bastogne", "Anvers": "Antwerpen", "Gand": "Gent", "Bruges": "Brugge", "Louvain": "Leuven",
        "Malines": "Mechelen", "Courtrai": "Kortrijk", "Ostende": "Oostende", "Tongres": "Tongeren",
        "Alost": "Aalst", "Termonde": "Dendermonde", "Ypres": "Ieper", "Audenarde": "Oudenaarde",
        "Vilvorde": "Vilvoorde", "Tirlemont": "Tienen", "Ronse": "Renaix", "Sint-Genesius-Rode": "Rhode-Saint-Genèse",
        "Voeren": "Fourons", "Kelmis": "La Calamine", "Komen": "Comines",
    }.items()
}


@lru_cache
def _postcodes() -> tuple[dict[str, list[str]], dict[str, set[str]], dict[str, tuple[float, float]]]:
    """(postcode -> towns, town key -> postcodes, postcode -> centre)."""
    towns: dict[str, list[str]] = {}
    codes: dict[str, set[str]] = {}
    points: dict[str, list[tuple[float, float]]] = {}
    for line in DATA_FILE.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        code, town, lat, lon = line.split("\t")
        towns.setdefault(code, []).append(town)
        codes.setdefault(_key(town), set()).add(code)
        points.setdefault(code, []).append((float(lat), float(lon)))
    centre = {c: (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)) for c, pts in points.items()}
    return towns, codes, centre


def towns_for(postal_code: str) -> list[str]:
    return _postcodes()[0].get(postal_code, [])


def postcodes_for(city: str) -> set[str]:
    _, codes, _ = _postcodes()
    k = _key(city)
    return codes.get(k, set()) | codes.get(_ALIASES.get(k, ""), set())


def centre(postal_code: str | None) -> tuple[float, float] | None:
    """Middle of a Belgian postcode area (lat, lon)."""
    return _postcodes()[2].get(postal_code or "")


def km_between(a: tuple[float, float], b: tuple[float, float]) -> float:
    """As the crow flies."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def normalize_country(value: str | None) -> str | None:
    """'België', 'Belgique', 'be' -> 'BE'. An unknown country is kept as written."""
    value = (value or "").strip()
    if not value:
        return None
    if code := _COUNTRY_NAMES.get(_key(value)):
        return code
    if re.fullmatch(r"[A-Za-z]{2}", value):
        return value.upper()
    return value


def country_name(code: str | None) -> str:
    return COUNTRIES.get(code or "", code or "")


def _normalize_postcode(value: str | None, country: str | None) -> tuple[str | None, str | None]:
    """('B-3000', None) -> ('3000', 'BE'); ('1234ab', 'NL') -> ('1234 AB', 'NL')."""
    pc = re.sub(r"\s+", " ", (value or "").strip()).upper()
    if not pc:
        return None, country
    if m := re.fullmatch(r"([A-Z]{1,2})\s*-\s*(.+)", pc):
        if prefix := _PREFIX.get(m.group(1)):
            pc, country = m.group(2), country or prefix
    if m := re.fullmatch(r"(\d{4})\s*([A-Z]{2})", pc):
        pc = f"{m.group(1)} {m.group(2)}"
    return pc, country


_NUMBER = r"\d+\s?[A-Za-z]?(?:\s?[-/]\s?\d+\s?[A-Za-z]?)?(?:\s*(?:bus|bte|boîte|box|b)\.?\s*[\w/]+)?"


def split_street(text: str | None) -> tuple[str | None, str | None]:
    """'Kerkstraat 12A bus 3' -> ('Kerkstraat', '12A bus 3'); '12 rue de la Paix' -> ('rue de la Paix', '12')."""
    s = re.sub(r"\s+", " ", (text or "").strip(" ,"))
    if not s:
        return None, None
    if m := re.fullmatch(rf"(.*?[^\d\s,])[\s,]+(?:nr\.?|n°|no\.?)?\s*({_NUMBER})", s, flags=re.IGNORECASE):
        return m.group(1).strip(), m.group(2).strip()
    if m := re.fullmatch(rf"({_NUMBER}),?\s+([^\d].*)", s, flags=re.IGNORECASE):
        return m.group(2).strip(), m.group(1).strip()
    return s, None


def parse(text: str | None) -> dict[str, str | None]:
    """Split an address on one line into PARTS. What can't be placed stays in street."""
    out: dict[str, str | None] = dict.fromkeys(PARTS)
    s = re.sub(r"\s+", " ", re.sub(r"[\r\n]+", ", ", text or "")).strip(" ,")
    if not s:
        return out
    segs = [x.strip() for x in s.split(",") if x.strip()]
    if len(segs) > 1 and (code := _COUNTRY_NAMES.get(_key(segs[-1]))):
        out["country"] = code
        segs.pop()
    s = ", ".join(segs)
    m = re.search(r"(?:^|[\s,])(?:([A-Z]{1,2})-)?(\d{4,5})\s+(?:([A-Z]{2})\s+)?([^\d,][^,]*)$", s)
    if m and (not m.group(1) or m.group(1) in _PREFIX):
        prefix, digits, letters, rest = m.groups()
        country = out["country"] or (_PREFIX[prefix] if prefix else None)
        dutch = letters and len(digits) == 4 and (country == "NL" or (country is None and not towns_for(digits)))
        out["postal_code"] = f"{digits} {letters}" if dutch else digits
        out["city"] = rest.strip() if dutch or not letters else f"{letters} {rest.strip()}"
        out["country"] = country or ("NL" if dutch else None)
        street = s[: m.start()].strip(" ,")
    elif len(segs) > 1 and re.fullmatch(r"(?:[A-Z]{1,2}-)?\d{4,5}(?: ?[A-Z]{2})?", segs[-1]):  # '…, 3000'
        out["postal_code"], out["country"] = _normalize_postcode(segs[-1], out["country"])
        street = ", ".join(segs[:-1])
    elif len(segs) > 1 and not re.search(r"\d", segs[-1]):
        out["city"] = segs[-1]
        street = ", ".join(segs[:-1])
    else:
        street = s
    out["street"], out["house_number"] = split_street(street)
    return out


def _city_matches(city: str, postal_code: str) -> bool:
    keys = {_key(city), _ALIASES.get(_key(city), "")}
    if any(_key(t) in keys for t in towns_for(postal_code)):
        return True
    here = centre(postal_code)
    others = [centre(c) for c in postcodes_for(city)]
    return bool(here) and any(o and km_between(here, o) <= NEAR_KM for o in others)


def complete(parts: dict[str, Any]) -> tuple[dict[str, str | None], list[str], list[str]]:
    """(parts, what was filled in, what does not add up). Fills only empty parts, and only without doubt."""
    p: dict[str, str | None] = {k: (str(parts[k]).strip() or None) if parts.get(k) not in (None, "") else None
                                for k in PARTS}
    filled: list[str] = []
    issues: list[str] = []
    p["country"] = normalize_country(p["country"])
    p["postal_code"], p["country"] = _normalize_postcode(p["postal_code"], p["country"])
    if p["street"] and not p["house_number"]:
        p["street"], p["house_number"] = split_street(p["street"])
    pc, city = p["postal_code"], p["city"]
    belgian = None  # True when the postcode list confirms a Belgian address
    if p["country"] in (None, "BE") and pc and re.fullmatch(r"\d{4}", pc):
        towns = towns_for(pc)
        if not towns:
            if p["country"] == "BE":
                issues.append(f"postcode {pc} bestaat niet in België")
        elif city:
            belgian = _city_matches(city, pc)
            if not belgian:
                issues.append(f"gemeente '{city}' past niet bij postcode {pc} ({', '.join(towns[:4])})")
        elif len(towns) == 1:
            p["city"] = towns[0]
            filled.append(f"gemeente {towns[0]} uit postcode {pc}")
            belgian = True
    elif p["country"] in (None, "BE") and not pc and city:
        codes = postcodes_for(city)
        if len(codes) == 1:
            p["postal_code"] = next(iter(codes))
            filled.append(f"postcode {p['postal_code']} uit gemeente {city}")
            belgian = True
    elif p["country"] is None and pc and re.fullmatch(r"\d{4} [A-Z]{2}", pc):
        p["country"] = "NL"
        filled.append("land Nederland uit de postcode")
    if belgian and p["country"] is None:
        p["country"] = "BE"
        filled.append("land België")
    return p, filled, issues


def one_line(parts: dict[str, Any]) -> str | None:
    """'Kerkstraat 12, 3000 Leuven' (+ ', Nederland' outside Belgium)."""
    street = " ".join(str(parts[k]) for k in ("street", "house_number") if parts.get(k))
    town = " ".join(str(parts[k]) for k in ("postal_code", "city") if parts.get(k))
    code = parts.get("country")
    country = country_name(code) if code and code != HOME else None
    return ", ".join(x for x in (street, town, country) if x) or None


class AddressMixin:
    """The one-line address of a model with the PARTS columns (Customer, Supplier)."""

    @property
    def address(self) -> str | None:
        return one_line(self.address_parts)

    @property
    def address_parts(self) -> dict[str, str | None]:
        return {k: getattr(self, k) for k in PARTS}

    def set_address(self, parts: dict[str, Any]) -> None:
        for k in PARTS:
            setattr(self, k, parts.get(k) or None)
