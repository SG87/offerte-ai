"""Project management: statuses, transitions, triggers on 'aanvaard' (stock check + purchase mail, planning)."""

import hashlib
import hmac
import json
from pathlib import Path

import pytest
from sqlmodel import select

from app import workflow
from app.config import get_settings
from app.db import session_scope
from app.models import OutboxMessage

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "sample_geometry.json").read_text())


def test_transitions():
    assert workflow.allowed("voorbereid") == ["gemaakt", "geweigerd"]
    assert workflow.allowed("gestuurd") == ["aanvaard", "geweigerd", "gemaakt"]
    assert workflow.allowed("in_uitvoering") == ["werk_afgerond", "aanvaard"]  # no refusing once work started
    assert workflow.allowed("afgerond") == [] and workflow.allowed("geweigerd") == []
    assert workflow.PIPELINE[-1] == "afgerond" and "geweigerd" not in workflow.PIPELINE
    assert workflow.LEGACY["offerte"] == "gemaakt"


@pytest.fixture()
def project(client, request):
    email = f"flow-{request.node.name[-20:]}@gmail.com".lower()
    r = client.post("/api/v1/signup", json={"email": email, "lookup": False})
    h = {"X-API-Key": r.json()["api_key"]}
    pid = client.post("/api/v1/projects", headers=h, json={"name": "Flow", "client_name": "Fam. Peeters"}).json()[
        "project_id"
    ]
    client.put(f"/api/v1/projects/{pid}/geometry", headers=h, json=FIXTURE)
    return h, pid


def _status(client, h, pid, to, note=None):
    return client.post(f"/api/v1/projects/{pid}/status", headers=h, json={"to": to, "note": note})


def test_full_lifecycle_reserves_stock_and_orders_the_shortage(client, project):
    h, pid = project
    assert _status(client, h, pid, "gemaakt").status_code == 409  # no quote yet
    client.post(f"/api/v1/projects/{pid}/quote", headers=h)
    statuses = {p["project_id"]: p["status"] for p in client.get("/api/v1/projects", headers=h).json()}
    assert statuses[pid] == "gemaakt"  # calculating the quote moves it on by itself

    # muurverf fully in stock, plafondverf partly, the rest unknown (= 0)
    stock = client.put(
        "/api/v1/stock", headers=h, json=[{"code": "muurverf", "qty": 500}, {"code": "plafondverf", "qty": 1}]
    )
    assert stock.status_code == 200 and len(stock.json()) == 2
    assert client.put("/api/v1/stock", headers=h, json=[{"code": "bestaat_niet", "qty": 1}]).status_code == 422

    assert _status(client, h, pid, "aanvaard").status_code == 409  # must be sent first
    assert _status(client, h, pid, "gestuurd").status_code == 200
    r = _status(client, h, pid, "aanvaard", "akkoord per mail")
    assert r.status_code == 200 and r.json()["status"] == "aanvaard"

    # reservation: available = qty - reserved
    levels = {s["code"]: s for s in client.get("/api/v1/stock", headers=h).json()}
    assert levels["plafondverf"]["reserved"] > 1 and levels["plafondverf"]["available"] < 0
    assert levels["muurverf"]["available"] > 0

    # the shortage became an order proposal with a to-do and a mail to the stock manager (here: org mail)
    orders = client.get("/api/v1/orders", headers=h).json()
    proposal = next(o for o in orders if o["status"] == "voorstel")
    codes = {ln["code"]: ln for ln in proposal["lines"]}
    assert "plafondverf" in codes and codes["plafondverf"]["qty"] == 17  # 17,8 needed, 1 in stock -> 17 whole litres
    assert "muurverf_vochtwerend" in codes and "muurverf" not in codes
    assert all(ln["project_id"] == pid for ln in proposal["lines"])
    open_todos = client.get("/api/v1/todos", headers=h).json()
    kinds = {t["kind"] for t in open_todos if t["project_id"] == pid}
    assert kinds == {"bestelling", "inplannen"}
    with session_scope() as s:
        mails = s.exec(select(OutboxMessage).where(OutboxMessage.project_id == pid)).all()
    assert len(mails) == 1 and mails[0].kind == "bestelvoorstel" and mails[0].to.startswith("flow-")
    assert "/voorraad#bestelling-" in mails[0].body and "Plafondverf: 17 l" in mails[0].body

    # confirming needs a supplier with an e-mail address
    r = client.post(f"/api/v1/orders/{proposal['id']}/bevestig", headers=h)
    assert r.status_code == 409 and "leverancier" in r.json()["detail"]
    with session_scope() as s:
        from app.models import PurchaseOrder, Supplier

        sup = Supplier(org_id=mails[0].org_id, name="Verfgroothandel Peeters", email="orders@peeters.test", customer_number="K-77")
        s.add(sup)
        s.commit()
        s.refresh(sup)
        po = s.get(PurchaseOrder, proposal["id"])
        po.supplier_id = sup.id
        s.add(po)
        s.commit()
    r = client.post(f"/api/v1/orders/{proposal['id']}/bevestig", headers=h)
    assert r.status_code == 200 and r.json()["order"]["status"] == "besteld"
    with session_scope() as s:
        order_mail = s.get(OutboxMessage, r.json()["order"]["mail_id"])
    assert order_mail.to == "orders@peeters.test" and "K-77" in order_mail.body and "Plafondverf: 17 l" in order_mail.body
    kinds = {t["kind"] for t in client.get("/api/v1/todos", headers=h).json() if t["project_id"] == pid or t["ref_id"] == proposal["id"]}
    assert "bestelling" not in kinds and "levering" in kinds

    # back and forth: the reservation is released and re-made, nothing is ordered twice
    assert _status(client, h, pid, "gestuurd").status_code == 200
    levels = {s["code"]: s for s in client.get("/api/v1/stock", headers=h).json()}
    assert levels["plafondverf"]["reserved"] == 0
    assert _status(client, h, pid, "aanvaard").status_code == 200
    assert len([o for o in client.get("/api/v1/orders", headers=h).json() if o["status"] != "geannuleerd"]) == 1

    # delivery books the stock in; finished work consumes the reservation
    r = client.post(f"/api/v1/orders/{proposal['id']}/ontvangen", headers=h)
    assert r.status_code == 200
    levels = {s["code"]: s for s in client.get("/api/v1/stock", headers=h).json()}
    assert levels["plafondverf"]["qty"] == 18 and levels["plafondverf"]["available"] >= 0
    assert _status(client, h, pid, "geweigerd").status_code == 409  # too late to refuse
    assert _status(client, h, pid, "in_uitvoering").status_code == 200
    assert _status(client, h, pid, "werk_afgerond").status_code == 200
    levels = {s["code"]: s for s in client.get("/api/v1/stock", headers=h).json()}
    assert levels["plafondverf"]["reserved"] == 0 and 0 <= levels["plafondverf"]["qty"] < 1
    kinds = {t["kind"] for t in client.get("/api/v1/todos", headers=h).json() if t["project_id"] == pid}
    assert kinds == {"factuur_goedkeuren"}  # the concept invoice is waiting for approval
    assert _status(client, h, pid, "factuur_verstuurd").status_code == 200
    kinds = {t["kind"] for t in client.get("/api/v1/todos", headers=h).json() if t["project_id"] == pid}
    assert kinds == {"betaling"}
    for to in ["factuur_betaald", "afgerond"]:
        assert _status(client, h, pid, to).status_code == 200, to
    assert not [t for t in client.get("/api/v1/todos", headers=h).json() if t["project_id"] == pid]
    assert _status(client, h, pid, "factuur_betaald").status_code == 409  # afgerond is final

    texts = [e["text"] for e in client.get(f"/api/v1/projects/{pid}/history", headers=h).json()]
    assert any("akkoord per mail" in t for t in texts) and any("gereserveerd" in t.lower() for t in texts)
    assert any("afgeboekt" in t for t in texts) and any("vrijgegeven" in t for t in texts)


def test_enough_stock_sends_no_mail_and_webhook_is_signed(client, project, monkeypatch):
    h, pid = project
    client.post(f"/api/v1/projects/{pid}/quote", headers=h)
    codes = [i["code"] for i in client.get("/api/v1/config", headers=h).json()["items"]]
    client.put("/api/v1/stock", headers=h, json=[{"code": c, "qty": 10_000} for c in codes])

    sent = []

    class Ok:
        def raise_for_status(self):
            return None

    def fake_post(url, content, headers, timeout):
        sent.append((url, content, headers))
        return Ok()

    monkeypatch.setattr(get_settings(), "webhook_url", "https://hooks.example.test/offerte")
    monkeypatch.setattr(get_settings(), "webhook_secret", "s3cret")
    monkeypatch.setattr("app.integrations.events.httpx.post", fake_post)
    _status(client, h, pid, "gestuurd")
    _status(client, h, pid, "aanvaard")

    with session_scope() as s:
        assert s.exec(select(OutboxMessage).where(OutboxMessage.project_id == pid)).all() == []
    assert [hd["X-Offerte-Event"] for _, _, hd in sent] == ["voorraad.controle", "werk.inplannen"]
    _, body, headers = sent[0]
    expected = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert headers["X-Offerte-Signature"] == f"sha256={expected}"
    assert json.loads(body)["payload"]["shortage"] is False
    evs = client.get("/api/v1/events?type=werk.inplannen", headers=h).json()
    assert any(e["project_id"] == pid and e["status"] == "afgeleverd" for e in evs)


def test_web_board_and_status_buttons(client):
    client.cookies.clear()
    client.post("/signup", data={"email": "board@gmail.com"})
    client.post("/onboarding", data={"sector": "schilder"})
    r = client.post("/projects", data={"name": "Bordproject", "customer_id": "new", "new_name": "Testklant", "scope": ["muren"]}, follow_redirects=False)
    pid = r.headers["location"].rsplit("/", 1)[-1]
    board = client.get("/projects").text
    assert "Bordproject" in board and "Offerte voorbereid" in board and "Facturatie" in board
    page = client.get(f"/projects/{pid}").text
    assert "Projectstatus" in page and "Project aangemaakt." in page and "Offerte geweigerd…" in page
    r = client.post(f"/projects/{pid}/status", data={"to": "geweigerd", "note": "te duur"}, follow_redirects=False)
    assert r.status_code == 303
    page = client.get(f"/projects/{pid}").text
    assert "Dit project is afgesloten" in page and "te duur" in page


def test_mail_test_mode_redirects_everything_and_keeps_the_real_recipient(client, project, monkeypatch):
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            assert (host, port) == ("smtp.example.test", 587)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            pass

        def login(self, user, password):
            assert (user, password) == ("stijn@recommendit.net", "app-pw")

        def send_message(self, email):
            sent.append(email)

    h, pid = project
    settings = get_settings()
    # first without password: the mail waits in the outbox
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.test")
    monkeypatch.setattr(settings, "smtp_user", "stijn@recommendit.net")
    monkeypatch.setattr(settings, "smtp_from", "stijn@recommendit.net")
    monkeypatch.setattr(settings, "mail_redirect_to", "stijn@recommendit.net")
    monkeypatch.setattr("app.integrations.mail.smtplib.SMTP", FakeSMTP)
    client.post(f"/api/v1/projects/{pid}/quote", headers=h)
    _status(client, h, pid, "gestuurd")
    _status(client, h, pid, "aanvaard")  # nothing in stock -> order proposal mail to the stock manager
    with session_scope() as s:
        m = s.exec(select(OutboxMessage).where(OutboxMessage.project_id == pid)).first()
        mid, real_to = m.id, m.to
    assert m.status == "wacht" and sent == []

    # password set: "Verstuur nu" sends it, to the test address, marked with the intended recipient
    monkeypatch.setattr(settings, "smtp_password", "app-pw")
    client.cookies.clear()
    client.post("/login", data={"email": real_to})
    client.post("/onboarding", data={"sector": "schilder"})  # account was made via the API
    r = client.post(f"/outbox/{mid}/verstuur", follow_redirects=False)
    assert r.status_code == 303
    assert len(sent) == 1
    email = sent[0]
    assert email["To"] == "stijn@recommendit.net" and email["From"] == "Offer AI <stijn@recommendit.net>"
    assert email["Subject"].startswith(f"[TEST → {real_to}]") and f"bedoeld voor {real_to}" in email.get_content()
    with session_scope() as s:
        m = s.get(OutboxMessage, mid)
        assert m.status == "verzonden" and m.to == real_to and m.sent_to == "stijn@recommendit.net"
