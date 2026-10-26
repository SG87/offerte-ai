"""A project always has a customer; B2C quotes show prices incl. btw, B2B quotes excl. btw (btw added below)."""

import json
import re
from pathlib import Path

import pytest

from app.outputs.offer_lines import offer_lines, offer_totals
from app.schemas import QuoteResult

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "sample_geometry.json").read_text())


@pytest.fixture(scope="module")
def h(client):
    r = client.post("/api/v1/signup", json={"email": "klanten@gmail.com", "lookup": False})
    return {"X-API-Key": r.json()["api_key"]}


def _quote(client, h, pid) -> QuoteResult:
    client.put(f"/api/v1/projects/{pid}/geometry", headers=h, json=FIXTURE)
    r = client.post(f"/api/v1/projects/{pid}/quote", headers=h)
    assert r.status_code == 200, r.text
    return QuoteResult.model_validate(r.json())


def test_api_project_needs_a_customer(client, h):
    r = client.post("/api/v1/projects", headers=h, json={"name": "Zonder klant"})
    assert r.status_code == 422 and "klant" in r.json()["detail"]
    r = client.post("/api/v1/projects", headers=h, json={"name": "Onbekend", "customer_id": 99999})
    assert r.status_code == 422
    assert all(p["name"] not in ("Zonder klant", "Onbekend") for p in client.get("/api/v1/projects", headers=h).json())


def test_api_b2b_customer_defaults_to_21_pct_and_is_reused_by_name(client, h):
    r = client.post("/api/v1/customers", headers=h, json={"name": "Bouwbedrijf Vos bv", "kind": "b2b", "vat_number": "be 0123.456.789"})
    assert r.status_code == 200 and r.json()["kind"] == "b2b" and r.json()["vat_number"] == "BE0123456789"
    cid = r.json()["customer_id"]
    r = client.post("/api/v1/projects", headers=h, json={"name": "Kantoor", "customer_id": cid})
    assert r.json()["vat_pct"] == 21.0 and r.json()["customer"]["prices"] == "excl. btw"
    # same name, other case: the existing customer, not a duplicate
    r = client.post("/api/v1/projects", headers=h, json={"name": "Magazijn", "client_name": "bouwbedrijf vos BV"})
    assert r.json()["customer"]["customer_id"] == cid
    # new by name: b2c unless told otherwise
    r = client.post("/api/v1/projects", headers=h, json={"name": "Living", "client_name": "Fam. Maes"})
    assert r.json()["customer"]["kind"] == "b2c" and r.json()["vat_pct"] == 6.0
    names = [c["name"] for c in client.get("/api/v1/customers", headers=h).json()]
    assert names.count("Bouwbedrijf Vos bv") == 1 and "Fam. Maes" in names


def test_customer_of_another_org_is_refused(client, h):
    other = client.post("/api/v1/signup", json={"email": "ander@gmail.com", "lookup": False}).json()["api_key"]
    cid = client.post("/api/v1/customers", headers={"X-API-Key": other}, json={"name": "Niet van jou"}).json()["customer_id"]
    assert client.post("/api/v1/projects", headers=h, json={"name": "x", "customer_id": cid}).status_code == 422


@pytest.mark.parametrize("incl", [True, False])
def test_offer_lines_add_up_to_the_headline_total(client, h, incl):
    pid = client.post("/api/v1/projects", headers=h, json={"name": f"Som {incl}", "client_name": "Somklant"}).json()["project_id"]
    result = _quote(client, h, pid)
    lines = offer_lines(result, incl)
    label, headline, is_headline = offer_totals(result, incl)[0]
    assert is_headline and label == ("Totaal incl. btw" if incl else "Totaal excl. btw")
    assert round(sum(ln.amount for ln in lines), 2) == headline
    assert headline == (result.totals.total_incl if incl else result.totals.subtotal_excl)


def _zones(client, pid):
    data = {"zone_0_name": "Living", "zone_0_kind": "ruimte", "zone_0_area": "30", "zone_0_perimeter": "22",
            "zone_0_height": "2.6", "zone_0_in_scope": "on", "scale_confirmed": "on"}
    assert client.post(f"/projects/{pid}/geometry", data=data, follow_redirects=False).status_code == 303


def _login(client, email):
    client.cookies.clear()
    client.post("/signup", data={"email": email})
    client.post("/onboarding", data={"sector": "schilder"})


def test_web_project_without_customer_is_refused(client):
    _login(client, "webklant@gmail.com")
    r = client.post("/projects", data={"name": "Los project", "scope": ["muren"]}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/projects?nieuw=1"
    page = client.get(r.headers["location"]).text
    assert "klant nodig" in page and "Los project" not in page


def test_web_b2b_and_b2c_quotes(client):
    _login(client, "webklant2@gmail.com")
    page = client.get("/projects").text
    assert 'name="customer_id" required' in page and "+ Nieuwe klant" in page

    # new B2B customer straight from the project form; work type follows the customer (21 %)
    r = client.post(
        "/projects",
        data={"name": "Kantoor Vos", "customer_id": "new", "new_name": "Vos bv", "new_kind": "b2b",
              "new_vat_number": "BE0999888777", "new_street": "Industrieweg 4", "new_city": "Gent", "scope": ["muren", "plafonds"]},
        follow_redirects=False,
    )
    pid = r.headers["location"].rsplit("/", 1)[-1]
    page = client.get(f"/projects/{pid}").text
    assert "Vos bv" in page and "B2B · prijzen excl. btw" in page and "btw 21 %" in page
    assert "Industrieweg 4" in page  # work site = customer address
    _zones(client, pid)
    client.post(f"/projects/{pid}/quote")
    offerte = client.get(f"/projects/{pid}/offerte").text
    assert "Totaal excl. btw</th>" in offerte
    rows = re.findall(r'<td colspan="3" class="num">([^<]+)</td>', offerte)
    assert rows == ["Totaal excl. btw", "Btw 21 %", "Totaal incl. btw"]
    assert client.get(f"/projects/{pid}/offerte.pdf").headers["content-type"] == "application/pdf"
    klanten = client.get("/contacten/klanten").text
    assert "Vos bv" in klanten and ">B2B</span>" in klanten

    # an existing B2C customer: lines incl. btw, headline incl. btw, btw shown as included
    client.post("/contacten/klanten", data={"name": "Fam. Peeters", "kind": "b2c", "street": "Dorp", "house_number": "1"})
    cid = re.search(r'<option value="(\d+)"[^>]*>Fam\. Peeters', client.get("/projects").text).group(1)
    r = client.post("/projects", data={"name": "Living Peeters", "customer_id": cid, "scope": ["muren"]}, follow_redirects=False)
    pid2 = r.headers["location"].rsplit("/", 1)[-1]
    assert "B2C · prijzen incl. btw" in client.get(f"/projects/{pid2}").text
    _zones(client, pid2)
    client.post(f"/projects/{pid2}/quote")
    offerte = client.get(f"/projects/{pid2}/offerte").text
    assert "Totaal incl. btw</th>" in offerte
    rows = re.findall(r'<td colspan="3" class="num">([^<]+)</td>', offerte)
    assert rows == ["Totaal incl. btw", "Waarvan btw 6 %", "Totaal excl. btw"]

    # switching the customer to B2B flips the quote to excl. btw
    vos = re.search(r'<option value="(\d+)"[^>]*>Vos bv', client.get("/projects").text).group(1)
    client.post(f"/projects/{pid2}/klant", data={"customer_id": vos})
    assert "Totaal excl. btw</th>" in client.get(f"/projects/{pid2}/offerte").text

    board = client.get("/projects").text
    assert "excl. btw</span>" in board and "B2B" in board


def test_customer_language_defaults_to_company_and_shows_in_contacts(client, h):
    c = client.post("/api/v1/customers", headers=h, json={"name": "Taalloos"}).json()
    assert c["language"] == "nl"
    c = client.post("/api/v1/customers", headers=h, json={"name": "Dupont sa", "kind": "b2b", "language": "fr"}).json()
    assert c["language"] == "fr"
    r = client.post("/api/v1/projects", headers=h, json={"name": "Nieuw", "client_name": "Smith Ltd", "client_language": "en"})
    assert r.json()["customer"]["language"] == "en"


def test_quote_in_customer_language_with_ai_translation(client, monkeypatch):
    from sqlmodel import select

    from app import services
    from app.config import get_settings
    from app.db import session_scope
    from app.models import AIJob, Quote

    _login(client, "taal@gmail.com")
    page = client.get("/projects").text
    assert 'name="new_language"' in page and '<option value="nl" selected>Nederlands</option>' in page
    r = client.post("/projects", data={"name": "Binnenschilderwerk", "customer_id": "new", "new_name": "Famille Dubois",
                                       "new_kind": "b2c", "new_language": "fr", "scope": ["muren"]}, follow_redirects=False)
    pid = r.headers["location"].rsplit("/", 1)[-1]
    assert '<span class="chip" title="Taal van de offerte">FR</span>' in client.get(f"/projects/{pid}").text
    _zones(client, pid)

    # without AI: fixed texts in French, free texts stay Dutch and the page says so
    client.post(f"/projects/{pid}/quote")
    offerte = client.get(f"/projects/{pid}/offerte").text
    assert ">Devis<" in offerte and "Total TVAC" in offerte and "Dont TVA 6 %" in offerte and "Description" in offerte
    assert "Living: Muurverf acryl mat" in offerte and "nog niet vertaald naar het frans" in offerte
    assert "AI staat uit" in offerte

    # with AI: one background job translates the free texts once per quote version
    calls = []

    def fake_translate(texts, target):
        calls.append((target, list(texts)))
        return {t: f"[{target}] {t}" for t in texts}

    monkeypatch.setattr(get_settings(), "anthropic_api_key", "test-key")
    monkeypatch.setattr(services, "translate_texts", fake_translate)
    offerte = client.get(f"/projects/{pid}/offerte").text  # starts the job; it runs after the response
    assert len(calls) == 1 and calls[0][0] == "fr" and "Living" in calls[0][1] and "Binnenschilderwerk" in calls[0][1]
    offerte = client.get(f"/projects/{pid}/offerte").text
    assert "[fr] Living: [fr] Muurverf acryl mat" in offerte and "[fr] Binnenschilderwerk" in offerte
    assert "nog niet vertaald" not in offerte and len(calls) == 1  # nothing left: no second job
    with session_scope() as s:
        q = s.exec(select(Quote).where(Quote.project_id == int(pid))).all()[-1]
        assert q.translations["fr"]["Living"] == "[fr] Living"
        job = s.exec(select(AIJob).where(AIJob.kind == "vertaling", AIJob.project_id == int(pid))).one()
        assert job.status == "klaar"
    pdf = client.get(f"/projects/{pid}/offerte.pdf")
    assert pdf.headers["content-type"] == "application/pdf" and len(pdf.content) > 1000


def test_i18n_fallbacks():
    from app.i18n import t, unit
    from app.planning import proposal_sentence

    assert t("de", "offerte") == "Offerte"  # unknown language -> Dutch
    assert t("en", "btw_pct", pct="21") == "VAT 21 %" and unit("fr", "stuk") == "pièce" and unit("fr", "m2") == "m2"

    class P:
        from datetime import date
        proposed_start = date(2026, 11, 2)
        proposed_end = date(2026, 11, 4)
        proposed_crew = [1, 2]

    assert proposal_sentence(P(), {}, "en").startswith("Proposed execution: 02/11/2026 – 04/11/2026 (3 working days, 2 workers)")
    assert "3 werkdagen, 2 medewerkers" in proposal_sentence(P(), {})
