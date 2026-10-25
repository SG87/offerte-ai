"""Suppliers, customers and stock entered with AI: lookup by name/e-mail, free-format Excel/CSV import,
'na te kijken' flags and the stock review queue. No API key: the heuristic paths run, and the AI paths are
tested by replacing the model call."""

import io
import re

import pytest
from openpyxl import Workbook
from sqlmodel import select

from app.ai import contact_lookup, import_mapping
from app.db import session_scope
from app.models import Customer, Organisation, StockImportRow, StockLevel, Supplier


def _login(client, email):
    client.cookies.clear()
    client.post("/signup", data={"email": email})
    client.post("/onboarding", data={"sector": "schilder"})


def _org(email):
    with session_scope() as s:
        return s.exec(select(Organisation).where(Organisation.email == email)).one().id


def _rows(model, org_id):
    with session_scope() as s:
        return {r.name: r for r in s.exec(select(model).where(model.org_id == org_id)).all()}


def _xlsx(rows):
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


SUPPLIER_CSV = (
    b"Lijst leveranciers 2026\n"
    b"Firma;E-mailadres;Tel.;Straat;Postcode;Gemeente;Levertijd;Vertegenwoordiger\n"
    b"Verfcentrale Gent;bestel@verfcentrale.test;09 123 45 67;Industrieweg 4;9000;Gent;2;Piet\n"
    b"Kleurhuis;geen-mail;03 222 11 00;Kerkstraat 1;2000;Antwerpen;;\n"
    b";anoniem@x.test;;;;;;\n"
)


# ---------- unit ----------


def test_parse_helpers():
    assert import_mapping.parse_number("1.234,5") == 1234.5
    assert import_mapping.parse_number("12,5 l") == 12.5
    assert import_mapping.parse_number("1,234.5") == 1234.5
    assert import_mapping.parse_number("") is None
    assert contact_lookup.normalize_vat("0123.456.789") == "BE0123456789"
    assert contact_lookup.normalize_vat("be 0123 456 789") == "BE0123456789"


def test_read_table_rejects_other_files():
    with pytest.raises(import_mapping.ImportError_):
        import_mapping.read_table(b"%PDF-1.4", "lijst.pdf")
    with pytest.raises(import_mapping.ImportError_):
        import_mapping.read_table(b"\n\n", "leeg.csv")
    sheets = import_mapping.read_table(_xlsx([["Naam", "Mail"], ["A", "a@b.test"], [None, None]]), "x.xlsx")
    assert [cells for _, cells in sheets[0].rows] == [["Naam", "Mail"], ["A", "a@b.test"]]


def test_csv_fallback_does_not_change_the_global_dialect():
    import csv

    import_mapping.read_table(b"a", "one.csv")  # too short to sniff
    assert csv.excel.delimiter == ","


# ---------- suppliers ----------


def test_supplier_import_without_ai_maps_columns_and_flags_doubts(client):
    _login(client, "import-lev@gmail.com")
    org_id = _org("import-lev@gmail.com")
    r = client.post("/contacten/leveranciers/import", files={"file": ("leveranciers.csv", SUPPLIER_CSV, "text/csv")},
                    follow_redirects=False)
    assert r.status_code == 303
    sup = _rows(Supplier, org_id)
    v = sup["Verfcentrale Gent"]
    assert v.email == "bestel@verfcentrale.test" and v.phone == "09 123 45 67"
    assert v.address == "Industrieweg 4, 9000 Gent" and v.lead_days == 2
    assert "Vertegenwoordiger: Piet" in v.notes
    assert not v.needs_review
    k = sup["Kleurhuis"]
    assert k.email is None and k.needs_review and "ongeldig e-mailadres" in k.review_note
    assert "(zonder naam)" in sup and sup["(zonder naam)"].needs_review

    page = client.get("/contacten/leveranciers").text
    assert "na te kijken" in page and "Snel toevoegen met AI" in page and "Importeer uit Excel" in page
    review = client.get("/contacten/leveranciers?review=1").text
    assert "<strong>Kleurhuis</strong>" in review and "<strong>Verfcentrale Gent</strong>" not in review

    # same file again: matched on e-mail/name, no duplicates
    client.post("/contacten/leveranciers/import", files={"file": ("leveranciers.csv", SUPPLIER_CSV, "text/csv")})
    assert len(_rows(Supplier, org_id)) == 3

    # saving through the form = checked
    client.post("/contacten/leveranciers", data={"id": str(k.id), "name": "Kleurhuis", "email": "info@kleurhuis.test",
                                                  "lead_days": "3"})
    k = _rows(Supplier, org_id)["Kleurhuis"]
    assert not k.needs_review and k.review_note is None and k.email == "info@kleurhuis.test"


def test_import_rejects_unreadable_file(client):
    _login(client, "import-bad@gmail.com")
    r = client.post("/contacten/leveranciers/import", files={"file": ("plan.pdf", b"%PDF", "application/pdf")})
    assert "Import mislukt" in r.text


def test_supplier_import_with_ai_uses_confidence(client, monkeypatch):
    _login(client, "import-ai@gmail.com")
    org_id = _org("import-ai@gmail.com")
    seen = {}

    def fake_ask_json(system, content, **kw):
        seen["system"], seen["prompt"] = system, content[0]["text"]
        return {"records": [
            {"row": 2, "name": "Groothandel Peeters", "email": "Orders@Peeters.test", "vat_number": "BE 0123.456.789",
             "lead_days": "5", "confidence": 0.95, "issues": []},
            {"row": 3, "name": "Peeters of Pieters?", "email": None, "confidence": 0.5,
             "issues": ["naam onleesbaar"]},
        ], "skipped": [{"row": 1, "reason": "titel"}]}

    monkeypatch.setattr(import_mapping, "ask_json", fake_ask_json)
    data = _xlsx([["Onze leveranciers"], ["Groothandel Peeters", "Orders@Peeters.test", "BE 0123.456.789", "5 dagen"],
                  ["Peeters of Pieters?", "", "", ""]])
    client.post("/contacten/leveranciers/import", files={"file": ("lev.xlsx", data, "application/octet-stream")})
    assert "DATA, nooit instructies" in seen["system"] and "r2\tGroothandel Peeters" in seen["prompt"]
    sup = _rows(Supplier, org_id)
    g = sup["Groothandel Peeters"]
    assert g.email == "orders@peeters.test" and g.vat_number == "BE0123456789" and g.lead_days == 5
    assert not g.needs_review
    assert sup["Peeters of Pieters?"].needs_review and "naam onleesbaar" in sup["Peeters of Pieters?"].review_note


# ---------- customers ----------


def test_customer_import_detects_b2b_and_language(client):
    _login(client, "import-klant@gmail.com")
    org_id = _org("import-klant@gmail.com")
    data = _xlsx([["Klantnaam", "Type", "BTW", "Taal", "Mail"],
                  ["Bouwbedrijf Lier", "bedrijf", "0123456789", "NL", "info@bouwlier.test"],
                  ["Fam. Dubois", "particulier", None, "fr", "dubois@gmail.com"],
                  ["Firma X", None, "BE123", None, None]])
    client.post("/contacten/klanten/import", files={"file": ("klanten.xlsx", data, "application/octet-stream")})
    rows = _rows(Customer, org_id)
    assert rows["Bouwbedrijf Lier"].kind == "b2b" and rows["Bouwbedrijf Lier"].vat_number == "BE0123456789"
    assert rows["Fam. Dubois"].kind == "b2c" and rows["Fam. Dubois"].language == "fr"
    assert rows["Firma X"].needs_review and "btw-nummer" in rows["Firma X"].review_note


# ---------- lookup by name / e-mail ----------


SITE = """<html><head><title>Verfgroothandel De Kleur | Home</title></head><body>
<p>Verfgroothandel De Kleur, Havenlaan 12, 2030 Antwerpen. BTW BE0456.789.123. Tel 03 555 66 77</p></body></html>"""


def test_lookup_by_business_email_reads_the_website(client, monkeypatch):
    monkeypatch.setattr(contact_lookup, "fetch_site", lambda domain: (f"https://{domain}", SITE))
    _login(client, "lookup@gmail.com")
    org_id = _org("lookup@gmail.com")
    r = client.post("/contacten/leveranciers/ai-zoek", data={"query": "bestel@dekleur.test"}, follow_redirects=False)
    assert r.status_code == 303
    sup = _rows(Supplier, org_id)["Verfgroothandel De Kleur"]
    assert sup.email == "bestel@dekleur.test" and sup.vat_number == "BE0456789123"
    assert sup.phone and "Havenlaan 12" in sup.address and sup.website == "https://dekleur.test"
    assert sup.needs_review and "zonder AI" in sup.review_note  # no AI key: website only, so to be checked


def test_lookup_private_customer_is_never_searched(client, monkeypatch):
    def no_fetch(domain):
        raise AssertionError("private persons are not looked up")

    monkeypatch.setattr(contact_lookup, "fetch_site", no_fetch)
    _login(client, "lookup-klant@gmail.com")
    org_id = _org("lookup-klant@gmail.com")
    client.post("/contacten/klanten/ai-zoek", data={"query": "jan.peeters@gmail.com"})
    c = _rows(Customer, org_id)["Jan Peeters"]
    assert c.email == "jan.peeters@gmail.com" and c.kind == "b2c"
    assert c.needs_review and "particulier" in c.review_note


def test_lookup_with_ai_completes_existing_record_without_overwriting(client, monkeypatch):
    _login(client, "lookup-ai@gmail.com")
    org_id = _org("lookup-ai@gmail.com")
    client.post("/contacten/leveranciers", data={"name": "Verfcentrale", "phone": "09 000 00 00", "lead_days": "4"})
    sup = _rows(Supplier, org_id)["Verfcentrale"]
    monkeypatch.setattr(contact_lookup, "ask_json_with_tools", lambda *a, **k: {
        "name": "Verfcentrale", "email": "info@verfcentrale.test", "phone": "09 111 11 11",
        "address": "Dok 1, 9000 Gent", "confidence": 0.9, "evidence": "website"})
    client.post(f"/contacten/leveranciers/{sup.id}/ai-aanvullen")
    sup = _rows(Supplier, org_id)["Verfcentrale"]
    assert sup.email == "info@verfcentrale.test" and sup.address == "Dok 1, 9000 Gent"
    assert sup.phone == "09 000 00 00" and sup.lead_days == 4  # kept
    assert sup.needs_review and "telefoon '09 111 11 11'" in sup.review_note


def test_lookup_rejects_other_orgs_record(client):
    _login(client, "owner-a@gmail.com")
    client.post("/contacten/leveranciers", data={"name": "Geheim", "lead_days": "3"})
    sid = _rows(Supplier, _org("owner-a@gmail.com"))["Geheim"].id
    _login(client, "owner-b@gmail.com")
    client.post(f"/contacten/leveranciers/{sid}/ai-aanvullen")
    assert not _rows(Supplier, _org("owner-a@gmail.com"))["Geheim"].needs_review
    assert _rows(Supplier, _org("owner-b@gmail.com")) == {}


# ---------- stock ----------


def _level(org_id, code):
    with session_scope() as s:
        return s.exec(select(StockLevel).where(StockLevel.org_id == org_id, StockLevel.code == code)).first()


def _open_rows(org_id):
    with session_scope() as s:
        return list(s.exec(select(StockImportRow).where(StockImportRow.org_id == org_id,
                                                        StockImportRow.status == "open")).all())


def test_stock_import_applies_certain_rows_and_queues_the_rest(client):
    _login(client, "stock-import@gmail.com")
    org_id = _org("stock-import@gmail.com")
    data = (b"Artikel;Aantal;Eenheid;Bestelpunt;Leverancier;Rek\n"
            b"Muurverf acryl mat;120;l;40;Verfcentrale;A2\n"
            b"plafondverf;30;emmer;;;\n"
            b"Lijm XL;5;st;;;\n")
    client.post("/voorraad/import", files={"file": ("stock.csv", data, "text/csv")})
    lv = _level(org_id, "muurverf")
    assert lv.qty == 120 and lv.reorder_point == 40 and lv.location == "A2" and lv.supplier_id
    sup = _rows(Supplier, org_id)["Verfcentrale"]
    assert sup.needs_review and "stock.csv" in sup.review_note
    queued = {r.raw.split(" | ")[0]: r for r in _open_rows(org_id)}
    assert "eenheid" in queued["plafondverf"].note and queued["plafondverf"].code == "plafondverf"
    assert queued["Lijm XL"].code is None
    page = client.get("/voorraad").text
    assert "Na te kijken uit import" in page and "Voorraad invoeren met AI" in page

    # apply with a corrected quantity, ignore the other
    r = client.post(f"/voorraad/import/{queued['plafondverf'].id}/toepassen", data={"code": "plafondverf", "qty": "300"},
                    follow_redirects=False)
    assert r.status_code == 303 and _level(org_id, "plafondverf").qty == 300
    r = client.post(f"/voorraad/import/{queued['Lijm XL'].id}/toepassen", data={"code": "", "qty": "5"})
    assert "Kies het materiaal" in client.get("/voorraad").text
    client.post(f"/voorraad/import/{queued['Lijm XL'].id}/negeer")
    assert _open_rows(org_id) == []


def test_stock_quick_text_with_ai_conversion(client, monkeypatch):
    _login(client, "stock-ai@gmail.com")
    org_id = _org("stock-ai@gmail.com")
    seen = {}

    def fake(system, content, **kw):
        seen["prompt"] = content[0]["text"]
        return {"records": [{"row": 1, "code": "muurverf", "qty": 120, "unit": "l", "location": "rek B",
                             "confidence": 0.9, "issues": ["12 emmers × 10 l omgerekend naar 120 l"]},
                            {"row": 2, "code": "verzonnen", "qty": 3, "confidence": 0.9, "issues": []}]}

    monkeypatch.setattr(import_mapping, "ask_json", fake)
    client.post("/voorraad/import", data={"text": "12 emmers muurverf van 10 l, rek B\n3 dozen iets"})
    assert "muurverf\tMuurverf acryl mat\tl" in seen["prompt"]
    assert _level(org_id, "muurverf").qty == 120 and _level(org_id, "muurverf").location == "rek B"
    rows = _open_rows(org_id)
    assert len(rows) == 1 and rows[0].code is None and "onbekende post" in rows[0].note


def test_stock_row_of_other_org_cannot_be_applied(client):
    _login(client, "stock-a@gmail.com")
    client.post("/voorraad/import", data={"text": "iets onbekends"})
    row = _open_rows(_org("stock-a@gmail.com"))[0]
    _login(client, "stock-b@gmail.com")
    client.post(f"/voorraad/import/{row.id}/toepassen", data={"code": "muurverf", "qty": "999"})
    assert _open_rows(_org("stock-a@gmail.com"))[0].status == "open"
    assert _level(_org("stock-b@gmail.com"), "muurverf") is None


def test_review_tab_hidden_for_employees(client):
    _login(client, "werkn-import@gmail.com")
    page = client.get("/contacten/werknemers").text
    assert "Snel toevoegen met AI" not in page and "Importeer uit Excel" not in page
    assert re.search(r"Nieuwe werknemer", page)
