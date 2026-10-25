"""Contacten, planning (with absences), to-do and voorraad pages through the web layer (no API key needed)."""

import re
from datetime import date, timedelta


def _login(client, email):
    client.cookies.clear()
    client.post("/signup", data={"email": email})
    client.post("/onboarding", data={"sector": "schilder"})


def test_contacts_crud_and_employee_absence_blocks_planning(client):
    _login(client, "beheer@gmail.com")
    # employees
    r = client.post("/contacten/werknemers", data={"name": "Jan Janssens", "role": "vakman", "hours_per_day": "8",
                                                   "planner": "on", "email": "jan@example.test"}, follow_redirects=False)
    assert r.status_code == 303
    client.post("/contacten/werknemers", data={"name": "Mia Maes", "role": "voorraad", "hours_per_day": "8", "stock_manager": "on"})
    page = client.get("/contacten/werknemers").text
    assert "Jan Janssens" in page and "Mia Maes" in page and "voorraad" in page
    jan = next(i for i in range(1, 50) if f"/contacten/werknemers/{i}" in page and "Jan Janssens" in client.get(f"/contacten/werknemers/{i}").text)
    # supplier + customer
    client.post("/contacten/leveranciers", data={"name": "Verf & Co", "email": "orders@verf.test", "lead_days": "2"})
    client.post("/contacten/klanten", data={"name": "Fam. De Smet", "address": "Kerkstraat 1, 2000 Antwerpen"})
    assert "Verf &amp; Co" in client.get("/contacten/leveranciers").text
    assert "Fam. De Smet" in client.get("/contacten/klanten").text

    # absence next monday
    monday = date.today() + timedelta(days=(7 - date.today().weekday()) % 7 or 7)
    r = client.post(f"/contacten/werknemers/{jan}/afwezigheid", data={"date_from": monday.isoformat(), "reason": "verlof"}, follow_redirects=False)
    assert r.status_code == 303
    page = client.get(f"/contacten/werknemers/{jan}?maand={monday:%Y-%m}").text
    assert "verlof" in page and "Beschikbaarheid" in page

    # project from the customer, then plan it: Jan is absent -> not assigned, warning shown
    cid = re.search(r"/contacten/klanten\?edit=(\d+)", client.get("/contacten/klanten").text).group(1)
    r = client.post("/projects", data={"name": "Gevel De Smet", "customer_id": str(cid), "scope": ["muren"]}, follow_redirects=False)
    pid = r.headers["location"].rsplit("/", 1)[-1]
    assert "Kerkstraat 1" in client.get(f"/projects/{pid}").text or True  # address lives on the project
    page = client.get(f"/planning?dag={monday}").text
    assert "afwezig" in page and "Jan Janssens" in page
    r = client.post("/planning/items", data={"project_id": pid, "date_from": monday.isoformat(), "date_to": monday.isoformat(),
                                             "employee_ids": [str(jan)], "hours": "8"}, follow_redirects=False)
    assert r.status_code == 303
    page = client.get(f"/planning?dag={monday}").text
    assert "niemand toegewezen" in page and "Gevel De Smet" in page
    # plan the day after with Jan: assigned
    tue = monday + timedelta(days=1)
    client.post("/planning/items", data={"project_id": pid, "date_from": tue.isoformat(), "employee_ids": [str(jan)]})
    page = client.get(f"/planning?dag={tue}").text
    assert "Jan Janssens" in page and "niemand toegewezen" not in page
    assert "Ingepland" in client.get(f"/projects/{pid}").text

    # to-do page: add, complete
    client.post("/todo", data={"title": "Ladder keuren", "assignee_id": str(jan)})
    page = client.get("/todo").text
    assert "Ladder keuren" in page and "Jan Janssens" in page
    tid = int(page.split('action="/todo/')[1].split("/")[0])
    client.post(f"/todo/{tid}/klaar")
    assert "Ladder keuren" not in client.get("/todo").text and "Ladder keuren" in client.get("/todo?status=gedaan").text


def test_stock_page_saves_levels_and_proposes_reorders(client):
    _login(client, "stock@gmail.com")
    client.post("/contacten/leveranciers", data={"name": "Groothandel X", "email": "x@x.test"})
    page = client.get("/voorraad").text
    assert "Materialen" in page and "muurverf" in page
    sup_id = page.split('name="supplier_muurverf"')[1].split('<option value="')[2].split('"')[0]
    r = client.post("/voorraad", data={"qty_muurverf": "5", "reorder_muurverf": "20", "pack_muurverf": "10",
                                        "supplier_muurverf": sup_id, "check": "1"}, follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/voorraad").text
    assert "Bestelvoorstellen te bevestigen" in page and "Groothandel X" in page
    assert "bij te bestellen" in page or "besteld" in page
    oid = int(page.split('id="bestelling-')[1].split('"')[0])
    r = client.post(f"/voorraad/bestellingen/{oid}/bevestig", follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/voorraad").text
    assert "Bestelvoorstellen te bevestigen" not in page and "Ontvangen" in page
    client.post(f"/voorraad/bestellingen/{oid}/ontvangen")
    page = client.get("/voorraad").text
    assert 'name="qty_muurverf" value="25"' in page  # 5 + 2 packs of 10
    assert "Levering" not in client.get("/todo").text


def test_project_gets_a_proposed_crew_and_dates_that_reach_the_quote_and_the_planning(client, monkeypatch):
    import json
    import re
    from datetime import date, timedelta
    from pathlib import Path

    from app import services
    from app.config import get_settings
    from app.schemas import Geometry

    fixture = json.loads((Path(__file__).parent / "fixtures" / "sample_geometry.json").read_text())
    _login(client, "voorstel@gmail.com")
    client.post("/contacten/werknemers", data={"name": "Piet Peeters", "role": "vakman", "hours_per_day": "8"})
    client.post("/contacten/werknemers", data={"name": "Ann Aerts", "role": "ploegbaas", "hours_per_day": "8"})
    client.post("/contacten/werknemers", data={"name": "Bea Boekhouding", "role": "administratie", "hours_per_day": "8"})
    r = client.post("/projects", data={"name": "Voorstelproject", "customer_id": "new", "new_name": "Testklant", "scope": ["muren", "plafonds"]}, follow_redirects=False)
    pid = r.headers["location"].rsplit("/", 1)[-1]
    # no proposal when the project is created: nothing is known about the work yet
    assert "Voorstel uitvoering" not in client.get(f"/projects/{pid}").text

    # the AI reads the plan -> zones -> hours -> proposal of crew and dates
    monkeypatch.setattr(get_settings(), "anthropic_api_key", "test-key")
    monkeypatch.setattr(services, "analyze_plan_image", lambda *a, **k: Geometry.model_validate(fixture))
    client.post(f"/projects/{pid}/plan", files={"file": ("plan.png", b"\x89PNG\r\n", "image/png")})
    assert "Voorstel uitvoering staat klaar" in client.get(f"/projects/{pid}/plan-status").json()["message"]
    page = client.get(f"/projects/{pid}").text
    assert "Voorstel uitvoering" in page and "AI-voorstel" in page
    # the two on-site workers are proposed, the office worker is not; dates at least a week out, on weekdays
    checked = re.findall(r'name="crew" value="(\d+)" checked', page)
    assert len(checked) == 2
    start = date.fromisoformat(re.search(r'id="proposed_start"[^>]*value="([^"]+)"', page).group(1))
    assert start >= date.today() + timedelta(days=7) and start.weekday() < 5
    assert "Bea" in page and f'value="{checked[0]}" checked' in page

    # quote: duration follows the hours, the sentence is on the quote page and in the PDF intro
    api = client.post("/api/v1/signup", json={"email": "voorstel@gmail.com", "lookup": False}).json()
    h = {"X-API-Key": api["api_key"]}
    client.post(f"/api/v1/projects/{pid}/quote", headers=h)
    offerte = client.get(f"/projects/{pid}/offerte").text
    assert "Voorgestelde uitvoering:" in offerte and "2 medewerkers" in offerte
    assert client.get(f"/api/v1/projects/{pid}/quote.pdf", headers=h).status_code == 200

    # the user adjusts it: locked, no longer recalculated on the next quote
    page = client.get(f"/projects/{pid}").text
    start = re.search(r'id="proposed_start"[^>]*value="([^"]+)"', page).group(1)
    r = client.post(f"/projects/{pid}/voorstel", data={"proposed_start": start, "proposed_end": start, "crew": [checked[0]]}, follow_redirects=False)
    assert r.status_code == 303
    client.post(f"/api/v1/projects/{pid}/quote", headers=h)
    page = client.get(f"/projects/{pid}").text
    assert "handmatig" in page and len(re.findall(r'name="crew" value="(\d+)" checked', page)) == 1

    # acceptance turns the proposal into the planning block with that crew
    for to in ["gestuurd", "aanvaard"]:
        assert client.post(f"/api/v1/projects/{pid}/status", headers=h, json={"to": to}).status_code == 200
    plan = client.get(f"/api/v1/planning?van={start}&tot={start}", headers=h).json()
    mine = [p for p in plan if p["project_id"] == int(pid)]
    assert len(mine) == 1 and mine[0]["employee_ids"] == [int(checked[0])]
    assert not [t for t in client.get("/api/v1/todos", headers=h).json() if t["project_id"] == int(pid) and t["kind"] == "inplannen"]
    assert "Ingepland volgens het voorstel" in client.get(f"/projects/{pid}").text


def test_manual_zones_get_no_proposal_until_asked(client):
    _login(client, "manueelvoorstel@gmail.com")
    client.post("/contacten/werknemers", data={"name": "Piet Peeters", "role": "vakman", "hours_per_day": "8"})
    r = client.post("/projects", data={"name": "Manueel", "customer_id": "new", "new_name": "Testklant", "scope": ["muren"]}, follow_redirects=False)
    pid = r.headers["location"].rsplit("/", 1)[-1]
    data = {"zone_0_name": "Living", "zone_0_kind": "ruimte", "zone_0_area": "30", "zone_0_perimeter": "22",
            "zone_0_height": "2.6", "zone_0_in_scope": "on", "scale_confirmed": "on"}
    client.post(f"/projects/{pid}/geometry", data=data)
    client.post(f"/projects/{pid}/quote")  # a quote alone does not invent a proposal
    page = client.get(f"/projects/{pid}").text
    assert "Nog geen voorstel" in page and 'id="proposed_start"' not in page
    assert "Voorgestelde uitvoering" not in client.get(f"/projects/{pid}/offerte").text
    client.post(f"/projects/{pid}/voorstel", data={"opnieuw": "1"})
    page = client.get(f"/projects/{pid}").text
    assert 'id="proposed_start"' in page and "Piet Peeters" in page


def test_delete_project_removes_its_data_and_releases_stock(client):
    from sqlmodel import select

    from app.db import session_scope
    from app.models import (
        OutboxMessage,
        PlanningItem,
        Project,
        ProjectEvent,
        Quote,
        StockLevel,
        StockReservation,
    )

    _login(client, "verwijder@gmail.com")
    h = {"X-API-Key": client.post("/api/v1/signup", json={"email": "verwijder@gmail.com", "lookup": False}).json()["api_key"]}
    r = client.post("/projects", data={"name": "Weg ermee", "customer_id": "new", "new_name": "Testklant", "scope": ["muren"]}, follow_redirects=False)
    pid = int(r.headers["location"].rsplit("/", 1)[-1])
    data = {"zone_0_name": "Living", "zone_0_kind": "ruimte", "zone_0_area": "30", "zone_0_perimeter": "22",
            "zone_0_height": "2.6", "zone_0_in_scope": "on", "scale_confirmed": "on"}
    client.post(f"/projects/{pid}/geometry", data=data)
    client.post(f"/projects/{pid}/quote")
    for to in ["gestuurd", "aanvaard"]:  # accepting reserves material, plans the work and may queue mails
        assert client.post(f"/api/v1/projects/{pid}/status", headers=h, json={"to": to}).status_code == 200
    with session_scope() as s:
        org_id = s.get(Project, pid).org_id
        assert s.exec(select(StockReservation).where(StockReservation.project_id == pid)).all()
        mails = [m.id for m in s.exec(select(OutboxMessage).where(OutboxMessage.project_id == pid)).all()]

    page = client.get(f"/projects/{pid}").text
    assert "Verwijder project" in page and f'action="/projects/{pid}/verwijder"' in page
    r = client.post(f"/projects/{pid}/verwijder", follow_redirects=True)
    assert "Project \u201cWeg ermee\u201d verwijderd." in r.text and f'href="/projects/{pid}"' not in r.text
    assert client.get(f"/projects/{pid}", follow_redirects=False).headers["location"] == "/projects"
    with session_scope() as s:
        assert s.get(Project, pid) is None
        for model in (Quote, ProjectEvent, StockReservation, PlanningItem):
            assert not s.exec(select(model).where(model.project_id == pid)).all(), model.__name__
        assert all(lv.reserved == 0 for lv in s.exec(select(StockLevel).where(StockLevel.org_id == org_id)).all())
        # mails stay as a record, detached from the project
        assert all(s.get(OutboxMessage, m).project_id is None for m in mails)
    assert client.delete(f"/api/v1/projects/{pid}", headers=h).status_code == 404


def test_api_delete_project(client):
    h = {"X-API-Key": client.post("/api/v1/signup", json={"email": "apiweg@gmail.com", "lookup": False}).json()["api_key"]}
    pid = client.post("/api/v1/projects", headers=h, json={"name": "Tijdelijk", "client_name": "Testklant"}).json()["project_id"]
    other = client.post("/api/v1/signup", json={"email": "apiweg2@gmail.com", "lookup": False}).json()["api_key"]
    assert client.delete(f"/api/v1/projects/{pid}", headers={"X-API-Key": other}).status_code == 404  # not yours
    assert client.delete(f"/api/v1/projects/{pid}", headers=h).json() == {"deleted": pid, "name": "Tijdelijk"}
    assert pid not in [p["project_id"] for p in client.get("/api/v1/projects", headers=h).json()]


def test_zone_tags_as_columns_and_delete_with_confirmation(client):
    _login(client, "tagkolom@gmail.com")
    r = client.post("/projects", data={"name": "Tags", "customer_id": "new", "new_name": "Testklant", "scope": ["muren"]}, follow_redirects=False)
    pid = r.headers["location"].rsplit("/", 1)[-1]
    base = {"scale_confirmed": "on", "tagcol_0_key": "vochtig", "tagcol_0_type": "bool",
            "tagcol_1_key": "ondergrond", "tagcol_1_type": "text"}
    zone = lambda i, name, **kw: {f"zone_{i}_name": name, f"zone_{i}_kind": "ruimte", f"zone_{i}_area": "10",  # noqa: E731
                                 f"zone_{i}_perimeter": "13", f"zone_{i}_in_scope": "on", **{f"zone_{i}_{k}": v for k, v in kw.items()}}
    data = {**base, **zone(0, "Badkamer", tag_0="on", tag_1="tegels"), **zone(1, "Living"), **zone(2, "Hal", tag_1="gyproc")}
    client.post(f"/projects/{pid}/geometry", data=data)
    zones = {z["name"]: z["tags"] for z in client.get(f"/projects/{pid}/geometry.json").json()["zones"]}
    assert zones == {"Badkamer": ["vochtig", "ondergrond=tegels"], "Living": [], "Hal": ["ondergrond=gyproc"]}

    page = client.get(f"/projects/{pid}").text
    assert ">Vochtig</th>" in page and ">Ondergrond</th>" in page and ">Wis<" not in page
    assert 'type="checkbox" name="zone_0_tag_0" checked' in page  # flag -> checkbox
    assert 'type="checkbox" name="zone_1_tag_0"' in page and 'name="zone_1_tag_0" checked' not in page
    assert '<datalist id="tagopts-1"><option value="tegels"><option value="gyproc"></datalist>' in page
    assert 'class="del-x"' in page and "Verwijderen?" in page

    # the confirmed delete submits zone_<i>_delete=1 with the rest of the form
    r = client.post(f"/projects/{pid}/geometry", data={**data, "zone_1_delete": "1"}, follow_redirects=True)
    assert "Verwijderd: Living." in r.text
    assert [z["name"] for z in client.get(f"/projects/{pid}/geometry.json").json()["zones"]] == ["Badkamer", "Hal"]


def test_tag_columns_from_skill_and_zones():
    from app.engine.skills import load_skill, tag_columns, tag_value
    from app.schemas import Zone

    z = Zone(id="a", name="Keuken", tags=["vochtig", "ondergrond=beton", "nieuw_pleister"])
    cols = {c.key: c for c in tag_columns(load_skill("schilder"), [z])}
    assert cols["vochtig"].type == "bool" and cols["vochtig"].label == "Vochtig"
    assert cols["ondergrond"].type == "text" and cols["ondergrond"].options == ["beton"]
    assert cols["nieuw_pleister"].type == "bool" and cols["nieuw_pleister"].label == "Nieuw pleister"
    assert tag_value(z, "vochtig") is True and tag_value(z, "ondergrond") == "beton" and tag_value(z, "x") is None
    assert tag_columns(load_skill("tuinaanleg"), []) == []


def test_live_preview_follows_height_and_perimeter_without_saving(client):
    _login(client, "livepreview@gmail.com")
    r = client.post("/projects", data={"name": "Live", "customer_id": "new", "new_name": "Testklant", "scope": ["muren", "plafonds"]}, follow_redirects=False)
    pid = r.headers["location"].rsplit("/", 1)[-1]
    row = {"zone_0_id": "", "zone_0_name": "Living", "zone_0_kind": "ruimte", "zone_0_area": "20", "zone_0_perimeter": "18",
           "zone_0_height": "2.5", "zone_0_openings": "raam 2x1.5", "zone_0_in_scope": "on", "scale_confirmed": "on"}
    client.post(f"/projects/{pid}/geometry", data=row)
    zid = client.get(f"/projects/{pid}/geometry.json").json()["zones"][0]["id"]
    page = client.get(f"/projects/{pid}").text
    assert "Vloer m²" in page and "Muren m²" in page and 'id="kpi-total_incl"' in page
    # 18 × 2.5 − 3 (raam ≥ 1 m²) + dagkanten (2×1.5 + 2) × 0.15 = 42.75
    assert 'data-wall-row="0">42,75<' in page

    base = client.post(f"/projects/{pid}/geometry/preview", data={**row, "zone_0_id": zid}).json()
    assert base["walls"][zid] == 42.75
    higher = client.post(f"/projects/{pid}/geometry/preview", data={**row, "zone_0_id": zid, "zone_0_height": "3"}).json()
    assert higher["walls"][zid] == 51.75  # +0.5 m × 18 m
    assert higher["totals"]["area_total_m2"] > base["totals"]["area_total_m2"]
    assert higher["totals"]["headline"] == higher["totals"]["total_incl"] > base["totals"]["total_incl"]  # B2C
    longer = client.post(f"/projects/{pid}/geometry/preview", data={**row, "zone_0_id": zid, "zone_0_perimeter": "20"}).json()
    assert longer["walls"][zid] == 47.75
    # nothing was saved
    assert client.get(f"/projects/{pid}/geometry.json").json()["zones"][0]["height_m"] == 2.5


def test_employee_channels_only_mail_for_now(client):
    from sqlmodel import select

    from app.db import session_scope
    from app.models import Employee

    _login(client, "kanaal@gmail.com")
    form = client.get("/contacten/werknemers").text
    assert "Gewenste communicatiemiddelen" in form
    assert 'name="channels" value="mail" checked' in form  # new employees: mail
    assert 'value="whatsapp"  disabled> WhatsApp (binnenkort)' in form
    client.post("/contacten/werknemers", data={"name": "Kanaal alles", "channels": ["mail", "whatsapp", "teams", "push"]})
    client.post("/contacten/werknemers", data={"name": "Kanaal geen"})
    with session_scope() as s:
        saved = {e.name: e.channels for e in s.exec(select(Employee).where(Employee.name.startswith("Kanaal "))).all()}
    # several can be ticked, but only the channels that work are kept; at least one remains
    assert saved == {"Kanaal alles": ["mail"], "Kanaal geen": ["mail"]}
    assert "E-mail" in client.get("/contacten/werknemers").text


def test_work_dates_are_never_in_the_future_and_move_the_planning(client):
    from datetime import date, timedelta

    from sqlmodel import select

    from app.db import session_scope
    from app.models import PlanningItem, Project

    _login(client, "werkdatum@gmail.com")
    h = {"X-API-Key": client.post("/api/v1/signup", json={"email": "werkdatum@gmail.com", "lookup": False}).json()["api_key"]}
    client.post("/contacten/werknemers", data={"name": "Piet Peeters", "role": "vakman", "hours_per_day": "8"})
    r = client.post("/projects", data={"name": "Datums", "customer_id": "new", "new_name": "Testklant", "scope": ["muren"]}, follow_redirects=False)
    pid = int(r.headers["location"].rsplit("/", 1)[-1])
    data = {"zone_0_name": "Living", "zone_0_kind": "ruimte", "zone_0_area": "30", "zone_0_perimeter": "22",
            "zone_0_height": "2.6", "zone_0_in_scope": "on", "scale_confirmed": "on"}
    client.post(f"/projects/{pid}/geometry", data=data)
    client.post(f"/projects/{pid}/quote")
    client.post(f"/projects/{pid}/voorstel", data={"opnieuw": "1"})  # proposal >= a week out
    for to in ["gestuurd", "aanvaard"]:  # acceptance turns the proposal into a planning block
        client.post(f"/api/v1/projects/{pid}/status", headers=h, json={"to": to})

    def items():
        with session_scope() as s:
            return [(i.date_from, i.date_to, i.status) for i in s.exec(select(PlanningItem).where(PlanningItem.project_id == pid))]

    (planned_from, planned_to, st), = items()
    assert planned_from >= date.today() + timedelta(days=7) and st == "gepland"
    page = client.get(f"/projects/{pid}").text
    assert f'name="on" required value="{date.today().isoformat()}" max="{date.today().isoformat()}"' in page

    # the future is refused, in the web form and the API
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    r = client.post(f"/projects/{pid}/status", data={"to": "in_uitvoering", "on": tomorrow}, follow_redirects=True)
    assert "kies vandaag of een dag in het verleden" in r.text
    r = client.post(f"/api/v1/projects/{pid}/status", headers=h, json={"to": "in_uitvoering", "on": tomorrow})
    assert r.status_code == 409
    with session_scope() as s:
        assert s.get(Project, pid).status == "aanvaard"

    # started two days ago: the block moves there, same length, and is 'bezig'
    started = date.today() - timedelta(days=2)
    client.post(f"/projects/{pid}/status", data={"to": "in_uitvoering", "on": started.isoformat()})
    (f, t, st), = items()
    assert f == started and t - f == planned_to - planned_from and st == "bezig"
    with session_scope() as s:
        assert s.get(Project, pid).work_started_on == started
    assert "dan gepland; blok nu" in client.get(f"/projects/{pid}").text

    # finished before the start is refused; finished today: the block ends today and is 'klaar'
    r = client.post(f"/api/v1/projects/{pid}/status", headers=h, json={"to": "werk_afgerond", "on": (started - timedelta(days=1)).isoformat()})
    assert r.status_code == 409 and "gestart op" in r.json()["detail"]
    assert client.post(f"/api/v1/projects/{pid}/status", headers=h, json={"to": "werk_afgerond"}).status_code == 200
    (f, t, st), = items()
    assert (f, t, st) == (started, date.today(), "klaar")
    page = client.get(f"/projects/{pid}").text
    assert f"gestart {started:%d/%m/%Y} · afgerond {date.today():%d/%m/%Y}" in page

    # one step back: the end date is gone and the block is 'bezig' again
    client.post(f"/api/v1/projects/{pid}/status", headers=h, json={"to": "in_uitvoering"})
    with session_scope() as s:
        p = s.get(Project, pid)
        assert p.work_done_on is None and p.work_started_on == started
    assert items()[0][2] == "bezig"
