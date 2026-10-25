"""PDF plans: sent to Claude as a document block; stored and previewed in the werkruimte (no API key needed)."""

import io

import pytest
from reportlab.pdfgen import canvas

from app.ai import plan_analysis
from app.engine import load_skill


def _pdf() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.rect(100, 400, 300, 200)
    c.drawString(200, 500, "Living 6,00 x 4,00 m")
    c.save()
    return buf.getvalue()


def test_pdf_plan_goes_to_claude_as_document(monkeypatch):
    seen = {}

    def fake_ask_json(system, content, max_tokens=4000, effort=None):
        seen["content"], seen["max_tokens"], seen["effort"] = content, max_tokens, effort
        return {"zones": [{"id": "z1", "name": "Living", "kind": "living", "length_m": 6, "width_m": 4}]}

    monkeypatch.setattr(plan_analysis, "ask_json", fake_ask_json)
    # browsers sometimes send octet-stream: the %PDF- header decides
    geometry = plan_analysis.analyze_plan_image(_pdf(), "application/octet-stream", load_skill("schilder"))
    block = seen["content"][0]
    assert block["type"] == "document" and block["source"]["media_type"] == "application/pdf"
    assert "PDF" in seen["content"][-1]["text"]
    assert geometry.source == "pdf" and geometry.scale_confirmed is False and geometry.zones[0].name == "Living"
    # thinking counts against max_tokens: room for multi-page plans, and a bounded effort
    assert seen["max_tokens"] >= 64000 and seen["effort"] == "medium"


def test_oversized_pdf_is_refused_before_the_api_call(monkeypatch):
    monkeypatch.setattr(plan_analysis, "MAX_PDF_BYTES", 100)
    with pytest.raises(plan_analysis.PlanTooLarge):
        plan_analysis.analyze_plan_image(_pdf(), "application/pdf", load_skill("schilder"))


def test_pdf_upload_is_stored_and_previewed(client):
    client.cookies.clear()
    client.post("/signup", data={"email": "pdf@gmail.com"})
    client.post("/onboarding", data={"sector": "schilder"})
    r = client.post("/projects", data={"name": "Appartement", "customer_id": "new", "new_name": "Testklant", "scope": ["muren"]}, follow_redirects=False)
    pid = r.headers["location"].rsplit("/", 1)[-1]
    r = client.post(
        f"/projects/{pid}/plan",
        files={"file": ("plan.pdf", _pdf(), "application/octet-stream")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    page = client.get(f"/projects/{pid}").text
    assert "Plan (PDF)" in page and "<iframe" in page and "AI uitgeschakeld" in page
    r = client.get(f"/projects/{pid}/plan-image")
    assert r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF-")


def test_web_upload_analyses_in_background(client, monkeypatch):
    from app import services
    from app.config import get_settings
    from app.schemas import Geometry, Zone

    seen = {}

    def fake_analyze(data, media_type, skill, hints=None):
        # the page has already been answered: the plan is stored and marked running
        seen["status"] = services.plan_status(services.latest_plan(session, project))["status"]
        return Geometry(source="pdf", zones=[Zone(id="z1", name="Living", area_m2=24, perimeter_m=20)])

    monkeypatch.setattr(get_settings(), "anthropic_api_key", "test-key")
    monkeypatch.setattr(services, "analyze_plan_image", fake_analyze)
    client.cookies.clear()
    client.post("/signup", data={"email": "bg@gmail.com"})
    client.post("/onboarding", data={"sector": "schilder"})
    r = client.post("/projects", data={"name": "Achtergrond", "customer_id": "new", "new_name": "Testklant", "scope": ["muren"]}, follow_redirects=False)
    pid = int(r.headers["location"].rsplit("/", 1)[-1])
    from app.db import session_scope
    from app.models import Project

    with session_scope() as session:
        project = session.get(Project, pid)
        r = client.post(f"/projects/{pid}/plan", files={"file": ("plan.pdf", _pdf(), "application/pdf")})
    assert r.status_code == 200 and seen["status"] == "running"
    st = client.get(f"/projects/{pid}/plan-status").json()
    assert st["status"] == "done" and "1 zones" in st["message"]
    page = client.get(f"/projects/{pid}").text
    assert "Living" in page and "Claude leest je plan" not in page
