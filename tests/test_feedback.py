"""The 'Idee of bug melden' button and the feedback page that explains the planned flow."""


def _login(client, email):
    client.cookies.clear()
    client.post("/signup", data={"email": email})
    client.post("/onboarding", data={"sector": "schilder"})


def test_feedback_requires_login(client):
    client.cookies.clear()
    r = client.get("/feedback", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_feedback_button_and_page(client):
    _login(client, "feedback@gmail.com")
    page = client.get("/projects").text
    assert 'href="/feedback"' in page and "Idee of bug" in page

    r = client.get("/feedback")
    assert r.status_code == 200
    page = r.text
    assert "€ 20 korting" in page
    assert "flow-title" in page and "PM-agent" in page and "E2E-agent" in page
    assert "Goedkeuring" in page and "Release" in page
    assert "3 kortingen per bedrijf per maand" in page
    assert "Binnenkort beschikbaar" in page
