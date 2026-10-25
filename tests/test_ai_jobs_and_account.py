"""Background AI jobs (header indicator, popup, notices) and the account page."""

from app import services
from app.config import get_settings


def _login(client, email):
    client.cookies.clear()
    client.post("/signup", data={"email": email})
    client.post("/onboarding", data={"sector": "schilder"})


def test_signup_runs_the_company_lookup_as_a_background_job(client):
    client.cookies.clear()
    r = client.post("/signup", data={"email": "job@gmail.com"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/onboarding"
    # the test client runs background tasks before returning, so the job is already finished
    state = client.get("/ai/jobs").json()
    assert state["running"] == []
    job = state["finished"][0]
    assert job["kind"] == "bedrijf" and job["status"] == "klaar" and "Geen website" in job["message"]
    page = client.get("/onboarding").text
    assert "AI-analyse gestart" in page and "We zoeken je bedrijf op" in page  # popup on the first page
    assert '"kind": "bedrijf"' in page  # finished job handed to the page for its notice
    client.post("/ai/jobs/seen", json={})
    assert client.get("/ai/jobs").json()["finished"] == []
    assert "We zoeken je bedrijf op" not in client.get("/onboarding").text  # popup shown once


def test_ai_assist_runs_in_background_and_reports_back(client, monkeypatch):
    _login(client, "assistjob@gmail.com")
    r = client.post("/projects", data={"name": "Assistproject", "customer_id": "new", "new_name": "Testklant", "scope": ["muren"]}, follow_redirects=False)
    pid = int(r.headers["location"].rsplit("/", 1)[-1])
    calls = []

    def fake_assist(session, org, project, instruction):
        calls.append((project.id, instruction))
        return services.current_geometry(session, project), "Badkamer uit scope gezet."

    monkeypatch.setattr(get_settings(), "anthropic_api_key", "test-key")
    monkeypatch.setattr(services, "run_assist", fake_assist)
    r = client.post(f"/projects/{pid}/assist", data={"instruction": "badkamer niet meenemen"}, follow_redirects=False)
    assert r.status_code == 303 and calls == [(pid, "badkamer niet meenemen")]
    job = client.get("/ai/jobs").json()["finished"][-1]
    assert job["kind"] == "assist" and job["status"] == "klaar" and job["project_id"] == pid
    assert job["message"] == "Badkamer uit scope gezet." and job["link"] == f"/projects/{pid}"
    page = client.get(f"/projects/{pid}").text
    assert 'data-watch="project:' in page and "AI-assist past je zones aan" in page


def test_failed_job_is_reported_not_raised(client, monkeypatch):
    _login(client, "failjob@gmail.com")
    r = client.post("/projects", data={"name": "Faalproject", "customer_id": "new", "new_name": "Testklant"}, follow_redirects=False)
    pid = int(r.headers["location"].rsplit("/", 1)[-1])

    def boom(*a, **k):
        raise RuntimeError("model weg")

    monkeypatch.setattr(get_settings(), "anthropic_api_key", "test-key")
    monkeypatch.setattr(services, "run_assist", boom)
    client.post(f"/projects/{pid}/assist", data={"instruction": "x"})
    job = client.get("/ai/jobs").json()["finished"][-1]
    assert job["status"] == "mislukt" and "model weg" in job["message"]


def test_account_page_updates_settings(client):
    _login(client, "acct@gmail.com")
    page = client.get("/account").text
    assert "Bedrijfsgegevens" in page and "API-sleutel" in page and "Integraties" in page
    old_key = page.split('id="api-key" type="password" value="')[1].split('"')[0]

    r = client.post("/account/gegevens", data={"company_name": "Acct BV", "vat_number": "be 0123.456.789", "email": "acct@gmail.com",
                                               "language": "fr", "country": "NL", "purchase_contact_email": "aankoop@acct.test"}, follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/account").text
    assert 'value="Acct BV"' in page and 'value="BE0123456789"' in page and 'value="fr" selected' in page
    assert "aankoop@acct.test" in page

    # e-mail already used by another account
    client.post("/api/v1/signup", json={"email": "andere@gmail.com", "lookup": False})
    r = client.post("/account/gegevens", data={"email": "andere@gmail.com"})  # follows the redirect
    assert "Er bestaat al een account" in r.text and 'value="acct@gmail.com"' in r.text

    # sector change makes a new price-list version
    client.post("/account/sector", data={"sector": "tuinaanleg"})
    assert "Tuinaannemer" in client.get("/config").text

    # api key: needs confirmation
    client.post("/account/api-key", data={})
    assert old_key in client.get("/account").text
    client.post("/account/api-key", data={"confirm": "on"})
    assert old_key not in client.get("/account").text
    assert client.get("/api/v1/me", headers={"X-API-Key": old_key}).status_code == 401

    # logo upload and removal
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="4" height="4"/></svg>'
    client.post("/account/huisstijl", data={"brand_primary": "#aa3300"}, files={"logo": ("l.svg", svg, "image/svg+xml")})
    page = client.get("/account").text
    assert "/branding/logo?v=" in page and "--brand:#aa3300" in page
    client.post("/account/huisstijl", data={"remove_logo": "on", "brand_primary": "#aa3300"})
    assert "geen logo" in client.get("/account").text

    # re-run the lookup as a job
    client.post("/account/opzoeken", data={})
    assert any(j["kind"] == "bedrijf" for j in client.get("/ai/jobs").json()["finished"])
