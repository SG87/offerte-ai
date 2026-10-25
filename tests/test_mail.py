"""Mailing the quote to the customer: outbox without SMTP, PDF attachment, status follow-up, test mail."""

import json
from pathlib import Path

import pytest
from sqlmodel import select

from app.config import get_settings
from app.db import session_scope
from app.models import OutboxMessage, Quote

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "sample_geometry.json").read_text())


class FakeSMTP:
    sent: list = []
    ports: list = []

    def __init__(self, host, port, timeout):
        FakeSMTP.ports.append((type(self).__name__, port))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        pass

    def login(self, user, password):
        pass

    def send_message(self, email):
        FakeSMTP.sent.append(email)


class FakeSMTPSSL(FakeSMTP):
    def starttls(self):
        raise AssertionError("SMTPS needs no STARTTLS")


@pytest.fixture()
def smtp(monkeypatch):
    """A ready (fake) mail server, not in test mode."""
    FakeSMTP.sent, FakeSMTP.ports = [], []
    settings = get_settings()
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.test")
    monkeypatch.setattr(settings, "smtp_user", "app@example.test")
    monkeypatch.setattr(settings, "smtp_password", "pw")
    monkeypatch.setattr(settings, "smtp_from", "app@example.test")
    monkeypatch.setattr("app.integrations.mail.smtplib.SMTP", FakeSMTP)
    monkeypatch.setattr("app.integrations.mail.smtplib.SMTP_SSL", FakeSMTPSSL)
    return FakeSMTP.sent


@pytest.fixture()
def quoted(client, request):
    email = f"mail-{request.node.name[-20:]}@gmail.com".lower()
    h = {"X-API-Key": client.post("/api/v1/signup", json={"email": email, "lookup": False}).json()["api_key"]}
    pid = client.post("/api/v1/projects", headers=h, json={"name": "Mailproject", "client_name": "Fam. Janssens"}).json()["project_id"]
    client.put(f"/api/v1/projects/{pid}/geometry", headers=h, json=FIXTURE)
    client.post(f"/api/v1/projects/{pid}/quote", headers=h)
    return h, pid, email


def _project_status(client, h, pid):
    return {p["project_id"]: p["status"] for p in client.get("/api/v1/projects", headers=h).json()}[pid]


def test_without_mail_server_the_quote_waits_in_the_outbox(client, quoted):
    h, pid, _ = quoted
    r = client.post(f"/api/v1/projects/{pid}/quote/mail", headers=h, json={"to": "janssens@example.test"})
    assert r.status_code == 200 and r.json()["status"] == "wacht"
    assert _project_status(client, h, pid) == "gemaakt"  # not sent yet, so not 'gestuurd'
    history = client.get(f"/api/v1/projects/{pid}/history", headers=h).json()
    assert any(e["kind"] == "mail" and "janssens@example.test (in outbox)" in e["text"] for e in history)


def test_invalid_address_is_refused(client, quoted):
    h, pid, _ = quoted
    r = client.post(f"/api/v1/projects/{pid}/quote/mail", headers=h, json={"to": "geen-adres"})
    assert r.status_code == 422


def test_sent_quote_has_the_pdf_and_moves_the_project_on(client, quoted, smtp):
    h, pid, org_email = quoted
    r = client.post(f"/api/v1/projects/{pid}/quote/mail", headers=h, json={"to": "janssens@example.test"})
    assert r.json()["status"] == "verzonden" and r.json()["quote_status"] == "verzonden"
    assert _project_status(client, h, pid) == "gestuurd"
    email = smtp[0]
    assert email["To"] == "janssens@example.test" and email["Reply-To"] == org_email
    assert "Offerte" in email["Subject"] and "Mailproject" in email["Subject"]
    [pdf] = list(email.iter_attachments())
    assert pdf.get_content_type() == "application/pdf" and pdf.get_filename().endswith("-v1.pdf")
    assert pdf.get_content().startswith(b"%PDF")
    assert "Fam. Janssens" in email.get_body(("plain",)).get_content()


def test_outbox_mail_sent_later_gets_the_pdf_and_the_status(client, quoted, monkeypatch):
    h, pid, org_email = quoted
    mid = client.post(f"/api/v1/projects/{pid}/quote/mail", headers=h, json={"to": "later@example.test"}).json()["mail_id"]
    FakeSMTP.sent, FakeSMTP.ports = [], []
    settings = get_settings()
    for k, v in {"smtp_host": "smtp.example.test", "smtp_port": 465, "smtp_user": "u", "smtp_password": "pw"}.items():
        monkeypatch.setattr(settings, k, v)
    monkeypatch.setattr("app.integrations.mail.smtplib.SMTP_SSL", FakeSMTPSSL)
    client.cookies.clear()
    client.post("/login", data={"email": org_email})
    client.post("/onboarding", data={"sector": "schilder"})
    r = client.post(f"/outbox/{mid}/verstuur?terug=offerte", follow_redirects=False)
    assert r.headers["location"] == f"/projects/{pid}/offerte#mailen"
    assert FakeSMTP.ports == [("FakeSMTPSSL", 465)]
    assert len(list(FakeSMTP.sent[0].iter_attachments())) == 1
    assert _project_status(client, h, pid) == "gestuurd"
    with session_scope() as s:
        assert s.get(OutboxMessage, mid).status == "verzonden"
        assert s.exec(select(Quote).where(Quote.project_id == pid)).first().status == "verzonden"


def test_web_form_prefills_and_sends(client, smtp):
    client.cookies.clear()
    client.post("/signup", data={"email": "webmail@gmail.com"})
    client.post("/onboarding", data={"sector": "schilder"})
    r = client.post("/projects", data={"name": "Webmail", "customer_id": "new", "new_name": "Klant Web", "scope": ["muren"]}, follow_redirects=False)
    pid = r.headers["location"].rsplit("/", 1)[-1]
    with session_scope() as s:
        from app.models import Customer, Project

        p = s.get(Project, int(pid))
        s.get(Customer, p.customer_id).email = "klant@web.test"
        s.commit()
    key = client.post("/api/v1/signup", json={"email": "webmail@gmail.com", "lookup": False}).json()["api_key"]
    client.put(f"/api/v1/projects/{pid}/geometry", headers={"X-API-Key": key}, json=FIXTURE)
    client.post(f"/projects/{pid}/quote")
    page = client.get(f"/projects/{pid}/offerte").text
    assert 'id="mailen"' in page and 'value="klant@web.test"' in page and "Verstuur offerte" in page
    r = client.post(f"/projects/{pid}/offerte/mail", data={"to": "klant@web.test", "subject": "Uw offerte", "body": "Beste,\n\nzie bijlage."}, follow_redirects=False)
    assert r.status_code == 303 and smtp[0]["Subject"] == "Uw offerte"
    page = client.get(f"/projects/{pid}/offerte").text
    assert "Offerte verzonden naar klant@web.test" in page and "verzonden" in page


def test_account_testmail(client, smtp):
    client.cookies.clear()
    client.post("/signup", data={"email": "testmail@gmail.com"})
    client.post("/onboarding", data={"sector": "schilder"})
    assert "Stuur testmail" in client.get("/account").text
    r = client.post("/account/testmail", follow_redirects=False)
    assert r.status_code == 303 and smtp[0]["To"] == "testmail@gmail.com"


def _token(pid):
    with session_scope() as s:
        return s.exec(select(Quote).where(Quote.project_id == pid).order_by(Quote.id.desc())).first().public_token


def test_quote_mail_is_from_offer_ai_with_buttons_once_before_the_closing(client, quoted, smtp):
    h, pid, _ = quoted
    client.post(f"/api/v1/projects/{pid}/quote/mail", headers=h, json={"to": "janssens@example.test"})
    email = smtp[0]
    assert email["From"] == "Offer AI <app@example.test>"
    token = _token(pid)
    accept, refuse = f"/o/{token}?keuze=aanvaard", f"/o/{token}?keuze=geweigerd"
    html = email.get_body(("html",)).get_content()
    assert html.count(accept) == 1 and html.count(refuse) == 1
    assert html.index("Fam. Janssens") < html.index("Totaal incl. btw") < html.index("Offerte aanvaarden") < html.index("Met vriendelijke groeten")
    text = email.get_body(("plain",)).get_content()
    assert text.count(accept) == 1 and text.index(accept) < text.index("Met vriendelijke groeten")


def test_split_body_without_closing_puts_everything_before_the_buttons():
    from app.outputs.quote_mail import split_body

    assert split_body("Hallo,\n\nZie bijlage.") == ([["Hallo,"], ["Zie bijlage."]], [])
    assert split_body("Bonjour,\n\nBien cordialement,\nFirma") == ([["Bonjour,"]], [["Bien cordialement,", "Firma"]])


def test_customer_accepts_online(client, quoted, smtp):
    h, pid, org_email = quoted
    client.post(f"/api/v1/projects/{pid}/quote/mail", headers=h, json={"to": "janssens@example.test"})
    token = _token(pid)
    client.cookies.clear()  # the customer has no session
    page = client.get(f"/o/{token}?keuze=aanvaard")
    assert page.status_code == 200 and "Bevestig: offerte aanvaarden" in page.text and "Mailproject" in page.text
    assert _project_status(client, h, pid) == "gestuurd"  # opening the link (or a link scanner) changes nothing

    r = client.post(f"/o/{token}", data={"keuze": "aanvaard", "naam": "Jan Janssens"}, follow_redirects=False)
    assert "fout=naam_verplicht" in r.headers["location"]  # the agreement box is required
    assert _project_status(client, h, pid) == "gestuurd"

    r = client.post(f"/o/{token}", data={"keuze": "aanvaard", "naam": "Jan Janssens", "akkoord": "on", "opmerking": "Graag in november"}, follow_redirects=False)
    assert r.status_code == 303
    assert _project_status(client, h, pid) == "aanvaard"
    with session_scope() as s:
        q = s.exec(select(Quote).where(Quote.project_id == pid)).first()
        assert (q.decision, q.decided_name, q.decision_note, q.status) == ("aanvaard", "Jan Janssens", "Graag in november", "aanvaard")
        note = s.exec(select(OutboxMessage).where(OutboxMessage.project_id == pid, OutboxMessage.kind == "klantreactie")).one()
    assert note.to == org_email and "Jan Janssens" in note.subject and "Graag in november" in note.body
    assert "U heeft deze offerte aanvaard" in client.get(f"/o/{token}").text
    # a second answer doesn't overwrite the first
    client.post(f"/o/{token}", data={"keuze": "geweigerd"})
    assert _project_status(client, h, pid) == "aanvaard"
    history = client.get(f"/api/v1/projects/{pid}/history", headers=h).json()
    assert any(e["actor"] == "klant" and e["to_status"] == "aanvaard" for e in history)


def test_customer_declines_online_and_old_versions_are_replaced(client, quoted):
    h, pid, _ = quoted
    client.post(f"/api/v1/projects/{pid}/quote/mail", headers=h, json={"to": "janssens@example.test"})  # waits in outbox
    old = _token(pid)
    client.post(f"/api/v1/projects/{pid}/quote", headers=h)  # v2
    assert "vervangen door een nieuwere" in client.get(f"/o/{old}").text
    client.post(f"/o/{old}", data={"keuze": "geweigerd"})
    assert _project_status(client, h, pid) == "gemaakt"

    client.post(f"/api/v1/projects/{pid}/quote/mail", headers=h, json={"to": "janssens@example.test"})
    new = _token(pid)
    assert new != old
    client.post(f"/o/{new}", data={"keuze": "geweigerd", "opmerking": "Te duur"})
    assert _project_status(client, h, pid) == "geweigerd"
    assert "U heeft deze offerte geweigerd" in client.get(f"/o/{new}").text


def test_unknown_link_and_public_pdf(client, quoted):
    h, pid, _ = quoted
    assert client.get("/o/bestaat-niet").status_code == 404
    client.post(f"/api/v1/projects/{pid}/quote/mail", headers=h, json={"to": "janssens@example.test"})
    client.cookies.clear()
    r = client.get(f"/o/{_token(pid)}/offerte.pdf")
    assert r.status_code == 200 and r.content.startswith(b"%PDF")


def test_answer_page_shows_the_buttons_once(client, quoted):
    h, pid, _ = quoted
    client.post(f"/api/v1/projects/{pid}/quote/mail", headers=h, json={"to": "janssens@example.test"})
    token = _token(pid)
    page = client.get(f"/o/{token}").text
    assert page.count("?keuze=aanvaard#antwoord") == 1  # only under the quote
    page = client.get(f"/o/{token}?keuze=aanvaard").text
    assert "?keuze=aanvaard#antwoord" not in page and "Bevestig: offerte aanvaarden" in page


def test_add_to_calendar_for_the_proposed_period(client, quoted):
    from datetime import date

    from app.models import Project

    h, pid, _ = quoted
    client.post(f"/api/v1/projects/{pid}/quote/mail", headers=h, json={"to": "janssens@example.test"})
    token = _token(pid)
    with session_scope() as s:
        p = s.get(Project, pid)
        p.proposed_start, p.proposed_end, p.client_address = None, None, "Kerkstraat 1, 2000 Antwerpen"
        s.commit()
    assert "Zet in agenda" not in client.get(f"/o/{token}").text  # no proposal, no button
    assert client.get(f"/o/{token}/agenda.ics").status_code == 404

    with session_scope() as s:
        p = s.get(Project, pid)
        p.proposed_start, p.proposed_end = date(2026, 11, 2), date(2026, 11, 16)
        s.commit()
    page = client.get(f"/o/{token}").text
    assert "Zet in agenda" in page and "calendar.google.com" in page and "dates=20261102%2F20261117" in page
    r = client.get(f"/o/{token}/agenda.ics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar")
    ics = r.text
    assert "DTSTART;VALUE=DATE:20261102" in ics and "DTEND;VALUE=DATE:20261117" in ics  # end is exclusive
    assert "LOCATION:Kerkstraat 1\\, 2000 Antwerpen" in ics and "(voorgesteld)" in ics
    assert all(len(line.encode()) <= 75 for line in ics.split("\r\n"))
