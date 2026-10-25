"""End-to-end through the HTTP layer: signup -> config -> project -> geometry -> quote -> PDF/XLSX/SVG."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "sample_geometry.json").read_text())


@pytest.fixture(scope="module")
def api_key(client: TestClient) -> str:
    r = client.post("/api/v1/signup", json={"email": "test@gmail.com", "lookup": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] is True and body["sector"] == "schilder"
    return body["api_key"]


def test_signup_is_idempotent(client, api_key):
    r = client.post("/api/v1/signup", json={"email": "test@gmail.com", "lookup": False})
    assert r.json()["created"] is False
    assert r.json()["api_key"] == api_key


def test_api_requires_key(client):
    assert client.get("/api/v1/config").status_code == 401
    assert client.get("/api/v1/config", headers={"X-API-Key": "nope"}).status_code == 401


def test_full_quote_flow_over_api(client, api_key):
    h = {"X-API-Key": api_key}
    cfg = client.get("/api/v1/config", headers=h).json()
    assert cfg["sector"] == "schilder" and cfg["version"] == 1 and len(cfg["items"]) >= 5

    # bump the hourly rate -> new version
    cfg["hourly_rate"] = 50
    r = client.put("/api/v1/config", headers=h, json=cfg)
    assert r.json()["version"] == 2 and r.json()["hourly_rate"] == 50

    # skill override (no API key -> heuristic translation)
    r = client.put(
        "/api/v1/skill-override",
        headers=h,
        json={"free_text": "Badkamer niet meenemen. Keuken altijd vochtwerend."},
    )
    assert r.status_code == 200
    ops = {a["op"] for a in r.json()["adjustments"]}
    assert "exclude_zones" in ops and "tag_zones" in ops

    r = client.post(
        "/api/v1/projects",
        headers=h,
        json={"name": "Test klus", "client_name": "Jan", "scope": ["muren", "plafonds", "houtwerk"]},
    )
    pid = r.json()["project_id"]
    assert r.json()["vat_pct"] == 6.0

    r = client.put(f"/api/v1/projects/{pid}/geometry", headers=h, json=FIXTURE)
    assert r.status_code == 200 and len(r.json()["zones"]) == 5

    r = client.post(f"/api/v1/projects/{pid}/quote", headers=h)
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["config_version"] == 2 and result["override_version"] == 1
    assert result["totals"]["total_incl"] > 0
    assert not any(line["zone_id"] == "badkamer" for line in result["lines"])

    assert (
        client.get(f"/api/v1/projects/{pid}/quote.pdf", headers=h).headers["content-type"]
        == "application/pdf"
    )
    assert client.get(f"/api/v1/projects/{pid}/meetstaat.xlsx", headers=h).status_code == 200
    svg = client.get(f"/api/v1/projects/{pid}/plan.svg", headers=h)
    assert svg.status_code == 200 and b"<svg" in svg.content and b"Living" in svg.content

    # stateless preview with the tuin skill
    tuin = {
        "geometry": {
            "scale_confirmed": True,
            "zones": [{"id": "t", "name": "Terras", "kind": "terras", "area_m2": 20}],
        },
        "scope": ["terras", "graafwerk"],
        "sector": "tuinaanleg",
    }
    r = client.post("/api/v1/quote/preview", headers=h, json=tuin)
    assert r.status_code == 200 and r.json()["sector"] == "tuinaanleg"


def test_assist_without_key_returns_503(client, api_key):
    h = {"X-API-Key": api_key}
    pid = client.post("/api/v1/projects", headers=h, json={"name": "x", "client_name": "Testklant"}).json()["project_id"]
    r = client.post(f"/api/v1/projects/{pid}/assist", headers=h, json={"instruction": "keuken 3 lagen"})
    assert r.status_code == 503


def test_web_flow(client):
    client.cookies.clear()
    r = client.get("/")
    assert r.status_code == 200 and "professionele" in r.text
    r = client.post("/login", data={"email": "web@gmail.com"}, follow_redirects=False)
    assert r.status_code == 200 and "geen account" in r.text  # login is for existing accounts only
    r = client.post("/signup", data={"email": "web@gmail.com"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/onboarding"
    client.cookies.clear()
    r = client.post("/login", data={"email": "web@gmail.com"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/onboarding"
    r = client.get("/onboarding")
    assert r.status_code == 200 and "In welke sector" in r.text
    logo = b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><script>x()</script><rect/></svg>'
    r = client.post(
        "/onboarding",
        data={"sector": "tuinaanleg", "company_name": "Groen BV", "brand_primary": "#2E7D32"},
        files={"logo": ("logo.svg", logo, "image/svg+xml")},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/config"
    r = client.get("/branding/logo")
    assert r.status_code == 200 and "script" not in r.text and "onload" not in r.text
    assert "default-src 'none'" in r.headers["content-security-policy"]
    r = client.get("/config")
    assert r.status_code == 200 and "Tuinaannemer" in r.text and "terras_tegels" in r.text
    assert "--brand:#2e7d32" in r.text and "/branding/logo" in r.text
    r = client.post(
        "/projects",
        data={"name": "Tuin Jan", "customer_id": "new", "new_name": "Jan", "work_type": "nieuwbouw", "scope": ["terras", "gazon"]},
        follow_redirects=False,
    )
    pid = r.headers["location"].rsplit("/", 1)[-1]
    r = client.post(
        f"/projects/{pid}/geometry",
        data={
            "zone_0_name": "Terras",
            "zone_0_kind": "terras",
            "zone_0_area": "24",
            "zone_0_in_scope": "on",
            "zone_1_name": "Gazon",
            "zone_1_kind": "gazon",
            "zone_1_area": "120",
            "zone_1_in_scope": "on",
            "scale_confirmed": "on",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    r = client.post(f"/projects/{pid}/quote", follow_redirects=True)
    assert r.status_code == 200 and "Totaal incl. btw" in r.text and "definitief" in r.text
    assert client.get(f"/projects/{pid}/offerte.pdf").headers["content-type"] == "application/pdf"
    assert client.get(f"/projects/{pid}").status_code == 200


def test_mcp_endpoint_answers_initialize(client):
    # POST /mcp must answer directly (no redirect): a JSON-RPC initialize gets the server info back
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    }
    r = client.post(
        "/mcp",
        json=body,
        headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
        follow_redirects=False,
    )
    assert r.status_code == 200, r.text
    assert r.json()["result"]["serverInfo"]["name"] == "Offerte-AI"


def test_meetstaat_follows_zone_changes_and_flags_the_saved_quote(client):
    client.cookies.clear()
    client.post("/signup", data={"email": "hoogte@gmail.com"})
    client.post("/onboarding", data={"sector": "schilder"})
    key = client.post("/api/v1/signup", json={"email": "hoogte@gmail.com", "lookup": False}).json()["api_key"]
    h = {"X-API-Key": key}
    pid = client.post("/api/v1/projects", headers=h, json={"name": "Hoogte", "client_name": "Testklant", "scope": ["muren", "plafonds"]}).json()["project_id"]
    client.put(f"/api/v1/projects/{pid}/geometry", headers=h, json=FIXTURE)
    saved = client.post(f"/api/v1/projects/{pid}/quote", headers=h).json()
    page = client.get(f"/projects/{pid}").text
    assert "Gewijzigd sinds offerte" not in page

    # raise the living's ceiling: walls grow, the saved quote does not change by itself
    geo = json.loads(json.dumps(FIXTURE))
    living = next(z for z in geo["zones"] if z["id"] == "living")
    living["height_m"] = 3.0
    client.put(f"/api/v1/projects/{pid}/geometry", headers=h, json=geo)
    page = client.get(f"/projects/{pid}").text
    assert "Gewijzigd sinds offerte" in page and "oppervlakte +" in page and "Herbereken → offerte v2" in page
    old_area = f"{saved['totals']['area_total_m2']:g}".replace(".", ",")
    assert f"{old_area} m²" not in page.split("Meetstaat")[1].split("meetstaatregels")[0]  # tiles show the live area

    new = client.post(f"/api/v1/projects/{pid}/quote", headers=h).json()
    assert new["totals"]["area_total_m2"] > saved["totals"]["area_total_m2"]
    assert "Gewijzigd sinds offerte" not in client.get(f"/projects/{pid}").text
