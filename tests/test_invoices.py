"""Invoices: made when the work is done, approved (number, OGM), sent over Peppol (B2B) or as PDF (B2C), paid."""

import json
import re
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import pytest
from sqlmodel import select

from app import invoicing
from app.db import session_scope
from app.models import Invoice, Organisation, Todo

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "sample_geometry.json").read_text())
UBL = {"cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
       "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"}


def _finished_project(client, h, customer: dict) -> int:
    pid = client.post("/api/v1/projects", headers=h, json={"name": "Binnenschilderwerk", **customer}).json()["project_id"]
    client.put(f"/api/v1/projects/{pid}/geometry", headers=h, json=FIXTURE)
    client.post(f"/api/v1/projects/{pid}/quote", headers=h)
    for to in ["gestuurd", "aanvaard", "in_uitvoering", "werk_afgerond"]:
        assert client.post(f"/api/v1/projects/{pid}/status", headers=h, json={"to": to}).status_code == 200, to
    return pid


@pytest.fixture(scope="module")
def h(client):
    key = client.post("/api/v1/signup", json={"email": "facturen@gmail.com", "lookup": False}).json()["api_key"]
    with session_scope() as s:
        org = s.exec(select(Organisation).where(Organisation.email == "facturen@gmail.com")).one()
        org.company_name, org.vat_number, org.iban = "Schilders Peeters bv", "BE0477472701", "BE68539007547034"
        org.address = "Kerkstraat 1, 2000 Antwerpen"
        s.add(org)
        s.commit()
    return {"X-API-Key": key}


def test_structured_reference_is_a_valid_ogm():
    ref = invoicing.structured_ref(2026, 1)
    digits = re.sub(r"\D", "", ref)
    assert re.fullmatch(r"\+\+\+\d{3}/\d{4}/\d{5}\+\+\+", ref) and len(digits) == 12
    assert int(digits[:10]) % 97 == int(digits[10:]) or (int(digits[:10]) % 97 == 0 and digits[10:] == "97")


def test_b2b_invoice_from_finished_work_to_peppol_and_paid(client, h, monkeypatch):
    pid = _finished_project(client, h, {"client_name": "Vos bv", "client_type": "b2b", "client_vat_number": "BE0123.456.749",
                                        "client_address": "Industrieweg 4, 9000 Gent"})
    invs = [i for i in client.get("/api/v1/invoices", headers=h).json() if i["project_id"] == pid]
    assert len(invs) == 1
    inv = invs[0]
    quote = client.post(f"/api/v1/projects/{pid}/quote", headers=h).json()  # same zones -> same totals
    assert inv["status"] == "concept" and inv["number"] is None and inv["b2b"]
    assert inv["total_incl"] == quote["totals"]["total_incl"]  # the invoice is the quote, to the cent
    with session_scope() as s:
        assert s.exec(select(Todo).where(Todo.kind == "factuur_goedkeuren", Todo.ref_id == inv["invoice_id"])).one().status == "open"

    # Peppol needs approval first
    assert client.post(f"/api/v1/invoices/{inv['invoice_id']}/peppol", headers=h).status_code == 409
    inv = client.post(f"/api/v1/invoices/{inv['invoice_id']}/approve", headers=h).json()
    year = date.today().year
    assert re.fullmatch(rf"F{year}-\d{{4}}", inv["number"]) and inv["structured_ref"].startswith("+++")
    with session_scope() as s:
        assert s.exec(select(Todo).where(Todo.kind == "factuur_goedkeuren", Todo.ref_id == inv["invoice_id"])).one().status == "gedaan"

    # UBL: Peppol BIS 3.0, endpoints from the enterprise numbers, amounts that add up
    xml = client.get(f"/api/v1/invoices/{inv['invoice_id']}/ubl.xml", headers=h).content
    root = ET.fromstring(xml)
    assert root.find("cbc:CustomizationID", UBL).text.endswith("billing:3.0")
    assert root.find("cbc:ID", UBL).text == inv["number"]
    sup = root.find("cac:AccountingSupplierParty/cac:Party/cbc:EndpointID", UBL)
    cus = root.find("cac:AccountingCustomerParty/cac:Party/cbc:EndpointID", UBL)
    assert (sup.get("schemeID"), sup.text) == ("0208", "0477472701") and cus.text == "0123456749"
    assert root.find("cac:AccountingCustomerParty/cac:Party/cac:PostalAddress/cbc:CityName", UBL).text == "Gent"
    assert root.find("cac:PaymentMeans/cbc:PaymentID", UBL).text == inv["structured_ref"]
    lines = root.findall("cac:InvoiceLine", UBL)
    line_sum = round(sum(float(ln.find("cbc:LineExtensionAmount", UBL).text) for ln in lines), 2)
    assert line_sum == float(root.find("cac:LegalMonetaryTotal/cbc:LineExtensionAmount", UBL).text) == inv["subtotal_excl"]
    for ln in lines:  # quantity × price = line amount, price never negative
        qty, price = float(ln.find("cbc:InvoicedQuantity", UBL).text), float(ln.find("cac:Price/cbc:PriceAmount", UBL).text)
        assert price >= 0 and abs(qty * price - float(ln.find("cbc:LineExtensionAmount", UBL).text)) < 0.005
    assert float(root.find("cac:LegalMonetaryTotal/cbc:PayableAmount", UBL).text) == inv["total_incl"]

    # no access point configured: ready for Peppol, project not yet 'factuur verstuurd'
    inv = client.post(f"/api/v1/invoices/{inv['invoice_id']}/peppol", headers=h).json()
    assert inv["peppol_status"] == "klaar" and inv["status"] == "goedgekeurd"

    # with an access point: sent, and the project moves on
    from app.config import get_settings
    from app.integrations import peppol

    sent = {}

    class R:
        status_code = 201
        text = "{}"
        headers = {}

        def json(self):
            return {"id": "AP-123"}

    def fake_post(url, content, headers, timeout):
        sent.update(url=url, xml=content, auth=headers.get("Authorization"))
        return R()

    monkeypatch.setattr(get_settings(), "peppol_api_url", "https://ap.example/invoices")
    monkeypatch.setattr(get_settings(), "peppol_api_key", "secret")
    monkeypatch.setattr(peppol.httpx, "post", fake_post)
    inv = client.post(f"/api/v1/invoices/{inv['invoice_id']}/peppol", headers=h).json()
    assert inv["status"] == "verzonden" and inv["sent_via"] == "peppol" and sent["auth"] == "Bearer secret"
    assert b"<cbc:CustomizationID>" in sent["xml"] or b"CustomizationID" in sent["xml"]
    statuses = {p["project_id"]: p["status"] for p in client.get("/api/v1/projects", headers=h).json()}
    assert statuses[pid] == "factuur_verstuurd"

    # paid: never in the future
    assert client.post(f"/api/v1/invoices/{inv['invoice_id']}/paid", headers=h, json={"on": "2999-01-01"}).status_code == 409
    inv = client.post(f"/api/v1/invoices/{inv['invoice_id']}/paid", headers=h, json={}).json()
    assert inv["status"] == "betaald" and inv["paid_on"] == date.today().isoformat()
    statuses = {p["project_id"]: p["status"] for p in client.get("/api/v1/projects", headers=h).json()}
    assert statuses[pid] == "factuur_betaald"


def _login(client, email):
    client.cookies.clear()
    client.post("/signup", data={"email": email})
    client.post("/onboarding", data={"sector": "schilder"})


def test_b2c_invoice_page_edit_by_hand_and_by_ai_then_pdf_and_mail(client, monkeypatch):
    _login(client, "facturenweb@gmail.com")
    client.post("/contacten/werknemers", data={"name": "Fien Factuur", "role": "administratie", "invoicer": "on"})
    h = {"X-API-Key": client.post("/api/v1/signup", json={"email": "facturenweb@gmail.com", "lookup": False}).json()["api_key"]}
    pid = _finished_project(client, h, {"client_name": "Fam. Janssens", "client_email": "jan@example.com"})
    with session_scope() as s:
        inv = s.exec(select(Invoice).where(Invoice.project_id == pid)).one()
        iid = inv.id
        todo = s.exec(select(Todo).where(Todo.kind == "factuur_goedkeuren", Todo.ref_id == iid)).one()
        assert todo.assignee_id is not None  # the employee with 'Facturatie'
    assert ">Facturen<" in client.get("/projects").text
    lst = client.get("/facturen").text
    assert f'href="/facturen/{iid}"' in lst and "concept" in lst

    page = client.get(f"/facturen/{iid}").text
    assert "Factuur goedkeuren" in page and "Toon factuur" not in page and "Verstuur via Peppol" not in page
    assert "B2C · incl. btw · PDF" in page and "Laat AI aanpassen" in page

    # by hand: change the first line's quantity and add a line
    n = len(invoicing.totals(inv)["lines"])
    data = {"title": "Binnenschilderwerk", "due_days": "14", "notes": "Bedankt!"}
    for i, ln in enumerate(invoicing.totals(inv)["lines"]):
        data.update({f"line_{i}_description": ln["description"], f"line_{i}_qty": str(ln["qty"]), f"line_{i}_unit": ln["unit"],
                     f"line_{i}_unit_price": str(ln["unit_price"]), f"line_{i}_vat_pct": str(ln["vat_pct"])})
    data.update({f"line_{n}_description": "Extra: kader trapzaal", f"line_{n}_qty": "2", f"line_{n}_unit": "u",
                 f"line_{n}_unit_price": "45", f"line_{n}_vat_pct": "6"})
    client.post(f"/facturen/{iid}", data=data)
    with session_scope() as s:
        inv = s.get(Invoice, iid)
        tot = invoicing.totals(inv)
        assert len(tot["lines"]) == n + 1 and tot["lines"][-1]["amount"] == 90.0 and inv.due_days == 14 and inv.notes == "Bedankt!"

    # by the AI (in the background): a discount line; amounts are computed by the app
    from app.ai import invoice_assist
    from app.config import get_settings

    def fake_edit(invoice, instruction):
        lines = invoice["lines"] + [{"description": "Korting 5 %", "qty": 1, "unit": "", "unit_price": -100, "vat_pct": 6}]
        return {"title": invoice["title"], "lines": lines, "notes": invoice["notes"], "due_days": invoice["due_days"],
                "summary": "Korting toegevoegd."}

    monkeypatch.setattr(get_settings(), "anthropic_api_key", "test-key")
    monkeypatch.setattr(invoice_assist, "edit_invoice", fake_edit)
    client.post(f"/facturen/{iid}/ai", data={"instruction": "geef 100 euro korting"})
    with session_scope() as s:
        new = invoicing.totals(s.get(Invoice, iid))
    assert new["lines"][-1]["amount"] == -100.0 and new["subtotal"] == round(tot["subtotal"] - 100, 2)

    # approve, then the private customer's PDF and mail (no mail server: it waits in the outbox)
    client.post(f"/facturen/{iid}/goedkeuren")
    page = client.get(f"/facturen/{iid}").text
    assert "Toon factuur (PDF)" in page and "Verstuur via Peppol" not in page and "+++" in page
    pdf = client.get(f"/facturen/{iid}/factuur.pdf")
    assert pdf.headers["content-type"] == "application/pdf" and len(pdf.content) > 1500
    r = client.post(f"/facturen/{iid}/mail", data={"to": "jan@example.com", "subject": "Factuur", "body": "Zie bijlage."}, follow_redirects=True)
    assert "outbox" in r.text
    with session_scope() as s:
        assert s.get(Invoice, iid).status == "goedgekeurd"  # not sent yet: the mail is waiting
    client.post(f"/facturen/{iid}/verstuurd")
    statuses = {p["project_id"]: p["status"] for p in client.get("/api/v1/projects", headers=h).json()}
    assert statuses[pid] == "factuur_verstuurd"


def test_numbers_have_no_gaps_and_numbered_invoices_stay(client, h):
    a = _finished_project(client, h, {"client_name": "Klant A"})
    b = _finished_project(client, h, {"client_name": "Klant B"})
    by_project = {i["project_id"]: i for i in client.get("/api/v1/invoices", headers=h).json()}
    ia, ib = by_project[a]["invoice_id"], by_project[b]["invoice_id"]
    na = client.post(f"/api/v1/invoices/{ia}/approve", headers=h).json()["number"]
    nb = client.post(f"/api/v1/invoices/{ib}/approve", headers=h).json()["number"]
    assert int(nb.split("-")[1]) == int(na.split("-")[1]) + 1
    with session_scope() as s:
        org = s.exec(select(Organisation).where(Organisation.email == "facturen@gmail.com")).one()
        inv = s.get(Invoice, ia)
        invoicing.back_to_concept(s, org, inv)
        with pytest.raises(invoicing.InvoiceError):
            invoicing.delete_concept(s, org, inv)  # it has a number: it stays
        invoicing.approve(s, org, inv)
        assert inv.number == na  # same number again
