"""The supplier's confirm button in the order mail and the page behind it (/l/{token})."""

import json
from pathlib import Path

import pytest
from sqlmodel import select

from app.config import get_settings
from app.db import session_scope
from app.models import OutboxMessage, PurchaseOrder, PurchaseOrderLine, Supplier, Todo

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "sample_geometry.json").read_text())


@pytest.fixture()
def placed_order(client, request, monkeypatch):
    """An accepted project with a shortage, ordered at a supplier through a (fake) mail server."""
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            pass

        def login(self, user, password):
            pass

        def send_message(self, email):
            sent.append(email)

    settings = get_settings()
    for k, v in {"smtp_host": "smtp.example.test", "smtp_user": "u", "smtp_password": "pw", "smtp_from": "app@example.test"}.items():
        monkeypatch.setattr(settings, k, v)
    monkeypatch.setattr("app.integrations.mail.smtplib.SMTP", FakeSMTP)

    email = f"lev-{request.node.name[-20:]}@gmail.com".lower()
    h = {"X-API-Key": client.post("/api/v1/signup", json={"email": email, "lookup": False}).json()["api_key"]}
    pid = client.post("/api/v1/projects", headers=h, json={"name": "Levering", "client_name": "Fam. Peeters"}).json()["project_id"]
    client.put(f"/api/v1/projects/{pid}/geometry", headers=h, json=FIXTURE)
    client.post(f"/api/v1/projects/{pid}/quote", headers=h)
    for to in ("gestuurd", "aanvaard"):
        client.post(f"/api/v1/projects/{pid}/status", headers=h, json={"to": to})
    order = next(o for o in client.get("/api/v1/orders", headers=h).json() if o["status"] == "voorstel")
    with session_scope() as s:
        po = s.get(PurchaseOrder, order["id"])
        sup = Supplier(org_id=po.org_id, name="Verfgroothandel Peeters", email="orders@peeters.test", lead_days=4)
        s.add(sup)
        s.commit()
        po.supplier_id = sup.id
        s.commit()
    sent.clear()
    assert client.post(f"/api/v1/orders/{order['id']}/bevestig", headers=h).status_code == 200
    return h, pid, order["id"], sent, email


def _order(oid):
    with session_scope() as s:
        po = s.get(PurchaseOrder, oid)
        lines = s.exec(select(PurchaseOrderLine).where(PurchaseOrderLine.order_id == oid)).all()
        s.expunge_all()
        return po, lines


def test_order_mail_has_one_confirm_button_before_the_closing(placed_order):
    _, _, oid, sent, _ = placed_order
    po, _ = _order(oid)
    [email] = [e for e in sent if e["To"] == "orders@peeters.test"]
    html = email.get_body(("html",)).get_content()
    assert html.count(f"/l/{po.public_token}") == 1
    assert html.index("Graag bestellen wij") < html.index("Bevestig ontvangst &amp; levering") < html.index("Met vriendelijke groeten")
    text = email.get_body(("plain",)).get_content()
    assert text.count(f"/l/{po.public_token}") == 1


def test_supplier_confirms_delivery_per_line(client, placed_order):
    h, pid, oid, sent, org_email = placed_order
    po, lines = _order(oid)
    client.cookies.clear()  # the supplier has no session
    page = client.get(f"/l/{po.public_token}")
    assert page.status_code == 200 and po.number in page.text and "Bevestig ontvangst en levering" in page.text
    assert all(ln.label in page.text for ln in lines)

    # a deliverable line without a date is refused, nothing is stored
    form = {f"status_{ln.id}": "leverbaar" for ln in lines}
    r = client.post(f"/l/{po.public_token}", data=form)
    assert r.status_code == 422 and "leverdatum" in r.text
    assert _order(oid)[0].supplier_confirmed_at is None

    first, rest = lines[0], lines[1:]
    form = {f"status_{first.id}": "niet_leverbaar", f"opmerking_{first.id}": "uit assortiment", "naam": "Piet", "opmerking": "Levering vóór 10u"}
    for i, ln in enumerate(rest):
        form |= {f"status_{ln.id}": "nalevering" if i else "leverbaar", f"datum_{ln.id}": f"2026-11-0{3 + i}"}
    sent.clear()
    r = client.post(f"/l/{po.public_token}", data=form, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("?ok=1")
    po, lines = _order(oid)
    assert po.supplier_contact == "Piet" and po.supplier_note == "Levering vóór 10u" and po.supplier_confirmed_at
    by_id = {ln.id: ln for ln in lines}
    assert by_id[first.id].delivery_status == "niet_leverbaar" and by_id[first.id].delivery_date is None
    assert by_id[first.id].delivery_note == "uit assortiment"
    latest = max(by_id[ln.id].delivery_date for ln in rest) if rest else None

    with session_scope() as s:
        todos = s.exec(select(Todo).where(Todo.ref_id == oid, Todo.status == "open")).all()
        notes = s.exec(select(OutboxMessage).where(OutboxMessage.kind == "leverancierreactie", OutboxMessage.org_id == po.org_id)).all()
    kinds = {t.kind: t for t in todos}
    assert "voorraad" in kinds and first.label in kinds["voorraad"].detail  # find an alternative
    if latest:
        assert kinds["levering"].due == latest  # the delivery check moves to the latest date
    assert len(notes) == 1 and "uit assortiment" in notes[0].body and "Levering vóór 10u" in notes[0].body
    history = client.get(f"/api/v1/projects/{pid}/history", headers=h).json()
    assert any(e["actor"] == "leverancier" for e in history)

    # the supplier can update; the stock page shows the answer
    assert "Bevestiging bijwerken" in client.get(f"/l/{po.public_token}?ok=1").text
    client.post("/login", data={"email": org_email})
    client.post("/onboarding", data={"sector": "schilder"})
    stock_page = client.get("/voorraad").text
    assert "bevestigd door leverancier" in stock_page and "niet leverbaar" in stock_page


def test_received_order_can_no_longer_be_changed(client, placed_order):
    h, _, oid, _, _ = placed_order
    client.post(f"/api/v1/orders/{oid}/ontvangen", headers=h)
    po, lines = _order(oid)
    page = client.get(f"/l/{po.public_token}").text
    assert "aanpassen kan niet meer" in page and "Bevestig ontvangst en levering" not in page
    r = client.post(f"/l/{po.public_token}", data={f"status_{ln.id}": "niet_leverbaar" for ln in lines})
    assert r.status_code == 422
    assert client.get("/l/bestaat-niet").status_code == 404
