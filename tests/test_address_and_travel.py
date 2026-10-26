"""Addresses in parts (street, number, postcode, city, country), completed only where the postcode list leaves
no doubt; and the kilometre allowance on the quote from the distance to the work site."""

import json
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlmodel import select

from app import address, services
from app.ai import import_mapping
from app.db import _split_legacy_addresses, get_engine, session_scope
from app.engine import calculate_quote, load_skill
from app.integrations import geo
from app.models import Customer, Organisation, Project, Supplier
from app.outputs.offer_lines import offer_lines
from app.schemas import Geometry

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "sample_geometry.json").read_text())


def _login(client, email):
    client.cookies.clear()
    client.post("/signup", data={"email": email})
    client.post("/onboarding", data={"sector": "schilder"})


def _org_id(email):
    with session_scope() as s:
        return s.exec(select(Organisation).where(Organisation.email == email)).one().id


# ---------- address ----------


def test_parse_splits_one_line():
    assert address.parse("Kerkstraat 12A bus 3, 3000 Leuven") == {
        "street": "Kerkstraat", "house_number": "12A bus 3", "postal_code": "3000", "city": "Leuven", "country": None}
    assert address.parse("12 rue de la Paix, 7070 Le Roeulx")["street"] == "rue de la Paix"
    assert address.parse("7070 Le Roeulx")["city"] == "Le Roeulx"  # not a Dutch postcode '7070 LE'
    nl = address.parse("Damrak 1, 1012 LG Amsterdam, Nederland")
    assert (nl["postal_code"], nl["city"], nl["country"]) == ("1012 LG", "Amsterdam", "NL")
    assert address.parse("B-9000 Gent")["country"] == "BE"
    assert address.parse("Industrieweg 4, Gent")["city"] == "Gent"


def test_complete_fills_only_without_doubt():
    parts, filled, issues = address.complete({"postal_code": "3000"})
    assert (parts["city"], parts["country"]) == ("Leuven", "BE") and filled and not issues
    parts, _, _ = address.complete({"city": "Leuven"})
    assert parts["postal_code"] == "3000"
    parts, filled, _ = address.complete({"city": "Antwerpen"})  # 2000, 2018, 2020…: not filled
    assert parts["postal_code"] is None and not filled
    parts, filled, _ = address.complete({"postal_code": "3020"})  # Herent, Veltem-Beisem, Winksele
    assert parts["city"] is None and not filled
    _, _, issues = address.complete({"postal_code": "3000", "city": "Gent"})
    assert issues and "Gent" in issues[0]  # reported, not corrected
    assert not address.complete({"postal_code": "3010", "city": "Leuven"})[2]  # Kessel-Lo is part of Leuven
    assert not address.complete({"postal_code": "1000", "city": "Brussel"})[2]  # list has 'Bruxelles'
    assert address.complete({"street": "Dorp 1", "country": "Belgique"})[0] == {
        "street": "Dorp", "house_number": "1", "postal_code": None, "city": None, "country": "BE"}
    assert address.one_line({"street": "Damrak", "house_number": "1", "postal_code": "1012 LG", "city": "Amsterdam",
                             "country": "NL"}) == "Damrak 1, 1012 LG Amsterdam, Nederland"


def test_legacy_one_line_addresses_are_split(client):  # client: the app made the tables
    with session_scope() as s:
        org = Organisation(email="legacy-adres@gmail.com")
        s.add(org)
        s.commit()
        cust = Customer(org_id=org.id, name="Oud adres")
        s.add(cust)
        s.commit()
        cid = cust.id
    engine = get_engine()
    with engine.begin() as conn:
        cols = [r[1] for r in conn.execute(text("PRAGMA table_info(customer)")).all()]
        if "address" not in cols:
            conn.execute(text("ALTER TABLE customer ADD COLUMN address VARCHAR"))
        conn.execute(text("UPDATE customer SET address = 'Bondgenotenlaan 5 bus 2, 3000' WHERE id = :id"), {"id": cid})
    _split_legacy_addresses(engine)
    with session_scope() as s:
        c = s.get(Customer, cid)
        assert (c.street, c.house_number, c.postal_code, c.city, c.country) == (
            "Bondgenotenlaan", "5 bus 2", "3000", "Leuven", "BE")


# ---------- import ----------

CUSTOMER_CSV = (
    "Naam;Straat;Nr;Postcode;Gemeente;Land\n"
    "Fam. Janssens;Naamsestraat;10;3000;;\n"
    "Fam. Peeters;Meir 1;;;Antwerpen;\n"
    "Fam. Maes;Veldstraat 3;;3000;Gent;België\n"
    "Fam. Claes;Diestsestraat;4;;Leuven;\n"
).encode()


def test_customer_import_without_ai_splits_and_completes_addresses(client):
    _login(client, "adres-import@gmail.com")
    org_id = _org_id("adres-import@gmail.com")
    client.post("/contacten/klanten/import", files={"file": ("klanten.csv", CUSTOMER_CSV, "text/csv")})
    with session_scope() as s:
        rows = {c.name: c for c in s.exec(select(Customer).where(Customer.org_id == org_id)).all()}
    j = rows["Fam. Janssens"]
    assert (j.street, j.house_number, j.postal_code, j.city, j.country) == ("Naamsestraat", "10", "3000", "Leuven", "BE")
    assert not j.needs_review  # filling the town from the postcode is no guess
    p = rows["Fam. Peeters"]
    assert (p.street, p.house_number, p.city, p.postal_code) == ("Meir", "1", "Antwerpen", None)  # several postcodes
    m = rows["Fam. Maes"]
    assert m.needs_review and "past niet bij postcode 3000" in m.review_note and m.city == "Gent"
    c = rows["Fam. Claes"]
    assert (c.postal_code, c.country) == ("3000", "BE")
    page = client.get("/contacten/klanten").text
    assert "Naamsestraat 10, 3000 Leuven" in page


def test_ai_import_prompt_asks_for_parts_and_results_are_checked(monkeypatch):
    seen = {}

    def fake_ask_json(system, content, **kw):
        seen["system"] = system
        return {"records": [
            {"row": 1, "name": "Bouw nv", "kind": "b2b", "street": "Kerkstraat", "house_number": "1",
             "postal_code": "3000", "city": None, "country": None, "confidence": 0.9, "issues": [], "filled": []},
            {"row": 2, "name": "Fam. Vos", "address": "Dorp 2, 3000 Gent", "confidence": 0.9, "issues": []},
        ], "skipped": []}

    monkeypatch.setattr(import_mapping, "ask_json", fake_ask_json)
    sheets = [import_mapping.Sheet("klanten", [(1, ["Bouw nv", "Kerkstraat 1", "3000"]), (2, ["Fam. Vos", "Dorp 2"])])]
    records, _, method = import_mapping.map_contacts(sheets, "klanten")
    assert method == "ai" and '"house_number"' in seen["system"] and "verzin nooit een straat" in seen["system"]
    bouw, vos = records
    assert (bouw["city"], bouw["country"]) == ("Leuven", "BE") and bouw["filled"] and bouw["confidence"] == 0.9
    assert vos["street"] == "Dorp" and vos["confidence"] < import_mapping.CONFIDENT  # 3000 is not Gent


def test_supplier_form_and_api_use_parts(client):
    _login(client, "adres-form@gmail.com")
    org_id = _org_id("adres-form@gmail.com")
    page = client.get("/contacten/leveranciers").text
    assert 'name="street"' in page and 'name="postal_code"' in page and 'name="country"' in page
    client.post("/contacten/leveranciers", data={"name": "Verf nv", "street": "Vaartkom", "house_number": "2",
                                                 "city": "Leuven", "country": "BE", "lead_days": "2"})
    with session_scope() as s:
        sup = s.exec(select(Supplier).where(Supplier.org_id == org_id)).one()
        assert sup.address == "Vaartkom 2, 3000 Leuven"

    key = client.post("/api/v1/signup", json={"email": "adres-api@gmail.com", "lookup": False}).json()["api_key"]
    r = client.post("/api/v1/customers", headers={"X-API-Key": key},
                    json={"name": "Fam. Wouters", "address": "Tiensestraat 8, 3000 Leuven"})
    body = r.json()
    assert (body["street"], body["house_number"], body["postal_code"], body["city"]) == ("Tiensestraat", "8", "3000", "Leuven")


# ---------- kilometre allowance ----------


def test_travel_cost_in_the_engine():
    skill = load_skill("schilder")
    config = skill.default_price_config().model_copy(update={"km_rate": 0.5, "km_hours_per_trip": 8.0})
    geometry = Geometry.model_validate(FIXTURE)
    base = calculate_quote(geometry, skill, config.model_copy(update={"km_rate": 0.0}), scope=["muren", "plafonds"])
    result = calculate_quote(geometry, skill, config, scope=["muren", "plafonds"], distance_km=12.5)
    trips = -(-base.totals.hours_total // 8)
    assert result.travel.trips == trips and result.travel.km_total == pytest.approx(2 * 12.5 * trips)
    assert result.totals.travel_cost == pytest.approx(result.travel.km_total * 0.5)
    assert result.totals.subtotal_excl == pytest.approx(base.totals.subtotal_excl + result.totals.travel_cost)
    for incl in (True, False):  # the lines still add up to the headline total
        lines = offer_lines(result, incl)
        target = result.totals.total_incl if incl else result.totals.subtotal_excl
        assert sum(ln.amount for ln in lines) == pytest.approx(target, abs=0.001)
        assert any(ln.description.startswith("Verplaatsingskosten") and ln.unit == "km" for ln in lines)
    unknown = calculate_quote(geometry, skill, config, scope=["muren", "plafonds"], distance_km=None)
    assert unknown.travel is None and any("Kilometervergoeding" in w for w in unknown.warnings)
    assert not any("Kilometervergoeding" in w for w in base.warnings)


def test_distance_looked_up_once_and_typed_by_hand(client, monkeypatch):
    _login(client, "km@gmail.com")
    org_id = _org_id("km@gmail.com")
    with session_scope() as s:
        org = s.get(Organisation, org_id)
        org.address = "Kerkstraat 1, 3000 Leuven"
        s.add(org)
        s.commit()
    page = client.get("/config").text
    assert 'name="km_rate"' in page and 'name="km_hours_per_trip"' in page
    form = {"hourly_rate": "45", "km_rate": "0,40", "km_hours_per_trip": "8"}
    client.post("/config", data=form)
    calls = []
    monkeypatch.setattr(geo, "road_distance",
                        lambda a, b, country=None: calls.append((a, b)) or geo.Distance(23.4, "route", "berekend over de weg"))

    client.post("/contacten/klanten", data={"name": "Fam. Verstraete", "street": "Markt", "house_number": "1",
                                            "postal_code": "3290", "city": "Diest"})
    with session_scope() as s:
        cid = s.exec(select(Customer).where(Customer.org_id == org_id)).one().id
    r = client.post("/projects", data={"name": "Living Verstraete", "customer_id": str(cid), "scope": ["muren"]},
                    follow_redirects=False)
    pid = int(r.headers["location"].rsplit("/", 1)[-1])
    with session_scope() as s:
        api_key = s.get(Organisation, org_id).api_key
    client.put(f"/api/v1/projects/{pid}/geometry", headers={"X-API-Key": api_key}, json=FIXTURE)
    page = client.get(f"/projects/{pid}").text
    client.get(f"/projects/{pid}")  # second visit: no new lookup
    assert calls == [("Kerkstraat 1, 3000 Leuven", "Markt 1, 3290 Diest")]  # once per pair of addresses
    assert 'name="distance_km"' in page and "verplaatsing" in page and "berekend over de weg" in page

    client.post(f"/projects/{pid}/quote")
    with session_scope() as s:
        quote = services.latest_quote(s, s.get(Project, pid))
        assert services.quote_result(quote).travel.distance_km == 23.4
    assert "Verplaatsingskosten" in client.get(f"/projects/{pid}/offerte").text

    r = client.post(f"/projects/{pid}/afstand", data={"distance_km": "30,5"}, follow_redirects=True)
    assert "Afstand tot de werf: 30,5 km" in r.text
    client.get(f"/projects/{pid}")
    with session_scope() as s:
        p = s.get(Project, pid)
        assert (p.distance_km, p.distance_source) == (30.5, "manueel") and len(calls) == 1
    client.post(f"/projects/{pid}/afstand", data={"opnieuw": "1"})
    with session_scope() as s:
        assert s.get(Project, pid).distance_km == 23.4 and len(calls) == 2


# ---------- archive ----------


def test_reimport_brings_archived_customers_back_and_archive_can_be_emptied(client):
    _login(client, "archief@gmail.com")
    org_id = _org_id("archief@gmail.com")
    client.post("/contacten/klanten/import", files={"file": ("klanten.csv", CUSTOMER_CSV, "text/csv")})
    with session_scope() as s:
        rows = {c.name: c.id for c in s.exec(select(Customer).where(Customer.org_id == org_id)).all()}
    for cid in rows.values():
        client.post(f"/contacten/klanten/{cid}/actief")  # archive all four
    assert "Fam. Janssens" not in client.get("/contacten/klanten").text

    client.post("/contacten/klanten/import", files={"file": ("klanten.csv", CUSTOMER_CSV, "text/csv")})
    page = client.get("/contacten/klanten").text
    assert "Fam. Janssens" in page and "4 uit het archief teruggehaald" in page

    # archive again; one customer has a project and must stay
    r = client.post("/projects", data={"name": "Gevel Claes", "customer_id": str(rows["Fam. Claes"]),
                                       "scope": ["muren"]}, follow_redirects=False)
    assert r.status_code == 303
    for cid in rows.values():
        client.post(f"/contacten/klanten/{cid}/actief")
    assert "Archief leegmaken" in client.get("/contacten/klanten?inactief=1").text
    r = client.post("/contacten/klanten/archief-leegmaken", follow_redirects=True)
    assert "4 klanten verwijderd" not in r.text and "3 klanten verwijderd" in r.text and "1 bleven in het archief" in r.text
    with session_scope() as s:
        left = s.exec(select(Customer).where(Customer.org_id == org_id)).all()
        assert [(c.name, c.active) for c in left] == [("Fam. Claes", False)]
