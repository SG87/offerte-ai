"""Tools of the general AI agent. Read tools answer from the organisation's own data; action tools never change
anything themselves: they become an AgentAction that runs only when the user clicks 'Uitvoeren'."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

from sqlmodel import Session, select

from app import invoicing, planning, services, todos, workflow
from app.models import Customer, Organisation, Project, Todo

Tool = Callable[..., Any]


def _project_row(p: Project, total: float | None = None) -> dict:
    return {
        "project_id": p.id,
        "naam": p.name,
        "klant": p.client_name,
        "status": p.status,
        "status_label": workflow.label(p.status),
        "link": f"/projects/{p.id}",
        **({"offerte_totaal_incl": total} if total is not None else {}),
    }


def _invoice_row(inv) -> dict:
    t = invoicing.totals(inv)
    due = invoicing.due_date(inv)
    return {
        "invoice_id": inv.id,
        "nummer": inv.number,
        "status": inv.status,
        "klant": inv.client_name,
        "b2b": inv.b2b,
        "totaal_incl": t["total"],
        "totaal_excl": t["subtotal"],
        "vervaldatum": due.isoformat() if due else None,
        "te_laat": bool(due and due < date.today() and inv.status in ("goedgekeurd", "verzonden")),
        "link": f"/facturen/{inv.id}",
    }


# ---------- read ----------


def overzicht(session: Session, org: Organisation) -> dict:
    rows = services.projects_overview(session, org)
    per_status: dict[str, int] = {}
    for r in rows:
        per_status[r["status_label"]] = per_status.get(r["status_label"], 0) + 1
    invs = invoicing.list_invoices(session, org)
    open_inv = [i for i in invs if i.status in ("goedgekeurd", "verzonden")]
    return {
        "vandaag": date.today().isoformat(),
        "projecten_per_status": per_status,
        "facturen_concept": sum(1 for i in invs if i.status == "concept"),
        "openstaand_incl": round(sum(invoicing.totals(i)["total"] for i in open_inv), 2),
        "facturen_te_laat": [_invoice_row(i) for i in open_inv if _invoice_row(i)["te_laat"]],
        "open_taken": todos.count_open(session, org),
    }


def zoek_projecten(session: Session, org: Organisation, zoek: str | None = None, status: str | None = None) -> list[dict]:
    out = []
    for r in services.projects_overview(session, org):
        p = r["project"]
        if status and p.status != status:
            continue
        if zoek and zoek.lower() not in f"{p.name} {p.client_name or ''} {p.client_address or ''}".lower():
            continue
        out.append(_project_row(p, r["quote"] and services.quote_result(r["quote"]).totals.total_incl))
    return out[:50]


def project_details(session: Session, org: Organisation, project_id: int) -> dict:
    p = services.get_project(session, org, project_id)
    if not p:
        return {"error": "Project niet gevonden"}
    quote = services.latest_quote(session, p)
    result = services.quote_result(quote) if quote else None
    customer = services.project_customer(session, p)
    items = planning.items_for_project(session, p)
    who = planning.assignees(session, items)
    emps = {e.id: e.name for e in planning.employees(session, org, active_only=False)}
    inv = invoicing.for_project(session, p)
    return {
        **_project_row(p),
        "werfadres": p.client_address,
        "klant": {"naam": customer.name, "type": customer.kind, "taal": customer.language} if customer else None,
        "scope": p.scope,
        "btw_pct": p.vat_pct,
        "offerte": {
            "nummer": f"{quote.number} v{quote.version}",
            "totaal_excl": result.totals.subtotal_excl,
            "totaal_incl": result.totals.total_incl,
            "uren": result.totals.hours_total,
            "oppervlakte_m2": result.totals.area_total_m2,
            "indicatief": result.indicative,
        } if quote else None,
        "voorstel_uitvoering": planning.proposal_sentence(p, {}),
        "werk_gestart": p.work_started_on.isoformat() if p.work_started_on else None,
        "werk_afgerond": p.work_done_on.isoformat() if p.work_done_on else None,
        "planning": [{"van": i.date_from.isoformat(), "tot": i.date_to.isoformat(), "status": i.status,
                      "wie": [emps.get(e, e) for e in who.get(i.id, [])]} for i in items],
        "factuur": _invoice_row(inv) if inv else None,
        "historiek": [e.text for e in workflow.history(session, p)[:10]],
    }


def facturen(session: Session, org: Organisation, status: str | None = None) -> list[dict]:
    return [_invoice_row(i) for i in invoicing.list_invoices(session, org, status)][:50]


def factuur_details(session: Session, org: Organisation, invoice_id: int) -> dict:
    inv = invoicing.get(session, org, invoice_id)
    if not inv:
        return {"error": "Factuur niet gevonden"}
    return {**_invoice_row(inv), "titel": inv.title, "taal": inv.language, "betaaltermijn_dagen": inv.due_days,
            "opmerkingen": inv.notes, "regels": invoicing.totals(inv)["lines"], "mededeling": inv.structured_ref,
            "verstuurd_via": inv.sent_via, "peppol": inv.peppol_status, "betaald_op": inv.paid_on.isoformat() if inv.paid_on else None}


def klanten(session: Session, org: Organisation, zoek: str | None = None) -> list[dict]:
    rows = session.exec(select(Customer).where(Customer.org_id == org.id).order_by(Customer.name)).all()
    return [
        {"customer_id": c.id, "naam": c.name, "type": c.kind, "taal": c.language, "email": c.email, "telefoon": c.phone,
         "adres": c.address, "btw": c.vat_number, "actief": c.active}
        for c in rows if not zoek or zoek.lower() in f"{c.name} {c.email or ''} {c.address or ''}".lower()
    ][:50]


def planning_tussen(session: Session, org: Organisation, van: str | None = None, tot: str | None = None) -> list[dict]:
    start = date.fromisoformat(van) if van else date.today()
    end = date.fromisoformat(tot) if tot else start + timedelta(days=14)
    items = planning.items_between(session, org, start, end)
    who = planning.assignees(session, items)
    emps = {e.id: e.name for e in planning.employees(session, org, active_only=False)}
    projects = {p.id: p.name for p in services.list_projects(session, org)}
    return [{"van": i.date_from.isoformat(), "tot": i.date_to.isoformat(), "project": projects.get(i.project_id, i.title),
             "project_id": i.project_id, "status": i.status, "wie": [emps.get(e, e) for e in who.get(i.id, [])]} for i in items]


def voorraad(session: Session, org: Organisation) -> list[dict]:
    from app.integrations import stock

    on_order = stock.on_order_by_code(session, org)
    return [
        {"code": lv.code, "in_voorraad": lv.qty, "gereserveerd": lv.reserved, "beschikbaar": lv.available,
         "eenheid": lv.unit, "bestelpunt": lv.reorder_point, "besteld": on_order.get(lv.code, 0.0),
         "status": stock.status_of(lv, on_order.get(lv.code, 0.0))}
        for lv in stock.stock_levels(session, org).values()
    ]


def taken(session: Session, org: Organisation, status: str = "open") -> list[dict]:
    emps = {e.id: e.name for e in planning.employees(session, org, active_only=False)}
    return [{"todo_id": t.id, "titel": t.title, "soort": t.kind, "voor": emps.get(t.assignee_id), "deadline": t.due.isoformat() if t.due else None,
             "link": t.link} for t in todos.list_todos(session, org, status)][:50]


READ: dict[str, tuple[Tool, dict]] = {
    "overzicht": (overzicht, {"type": "object", "properties": {}}),
    "zoek_projecten": (zoek_projecten, {"type": "object", "properties": {
        "zoek": {"type": "string", "description": "deel van projectnaam, klant of adres"},
        "status": {"type": "string", "description": "voorbereid|gemaakt|gestuurd|aanvaard|in_uitvoering|werk_afgerond|factuur_verstuurd|factuur_betaald|afgerond|geweigerd"}}}),
    "project_details": (project_details, {"type": "object", "properties": {"project_id": {"type": "integer"}}, "required": ["project_id"]}),
    "facturen": (facturen, {"type": "object", "properties": {"status": {"type": "string", "description": "concept|goedgekeurd|verzonden|betaald"}}}),
    "factuur_details": (factuur_details, {"type": "object", "properties": {"invoice_id": {"type": "integer"}}, "required": ["invoice_id"]}),
    "klanten": (klanten, {"type": "object", "properties": {"zoek": {"type": "string"}}}),
    "planning": (planning_tussen, {"type": "object", "properties": {
        "van": {"type": "string", "description": "JJJJ-MM-DD, standaard vandaag"}, "tot": {"type": "string", "description": "JJJJ-MM-DD, standaard +14 dagen"}}}),
    "voorraad": (voorraad, {"type": "object", "properties": {}}),
    "taken": (taken, {"type": "object", "properties": {"status": {"type": "string", "description": "open|gedaan"}}}),
}
READ_DESCRIPTIONS = {
    "overzicht": "Samenvatting van het bedrijf nu: projecten per status, openstaande en te late facturen, open taken.",
    "zoek_projecten": "Projecten zoeken op naam/klant/adres en/of status.",
    "project_details": "Alles over één project: klant, offerte, voorstel, planning, factuur, historiek.",
    "facturen": "Facturen, eventueel gefilterd op status.",
    "factuur_details": "Eén factuur met regels, bedragen, mededeling en verzendstatus.",
    "klanten": "Klanten zoeken.",
    "planning": "Planningsblokken in een periode, met wie er werkt.",
    "voorraad": "Voorraad per materiaal: in voorraad, gereserveerd, beschikbaar, besteld.",
    "taken": "To-do's (taken), standaard de open.",
}


# ---------- actions (proposed; run after the user's click) ----------


def _describe_status(session: Session, org: Organisation, a: dict) -> str:
    p = services.get_project(session, org, int(a["project_id"]))
    if not p:
        raise ValueError("Project niet gevonden")
    current = workflow.LEGACY.get(p.status, p.status)
    if a["status"] not in workflow.allowed(current):
        raise ValueError(f"Van '{workflow.label(current)}' kan niet naar '{workflow.label(a['status'])}'; "
                         f"wel naar: {', '.join(workflow.label(x) for x in workflow.allowed(current))}")
    if a["status"] in workflow.NEEDS_QUOTE and services.latest_quote(session, p) is None:
        raise ValueError(f"'{workflow.label(a['status'])}' kan pas als er een offerte berekend is")
    if a.get("datum") and date.fromisoformat(a["datum"]) > date.today():
        raise ValueError("Een werkdatum in de toekomst kan niet")
    when = f" op {a['datum']}" if a.get("datum") else ""
    return f"Project '{p.name}': {workflow.label(p.status)} → {workflow.label(a['status'])}{when}"


def _run_status(session: Session, org: Organisation, a: dict) -> str:
    p = services.get_project(session, org, int(a["project_id"]))
    on = date.fromisoformat(a["datum"]) if a.get("datum") else None
    workflow.change_status(session, org, p, a["status"], a.get("notitie"), actor="ai", on=on)
    return f"Status van '{p.name}' is nu {workflow.label(p.status)}."


def _describe_invoice_edit(session: Session, org: Organisation, a: dict) -> str:
    inv = invoicing.get(session, org, int(a["invoice_id"]))
    if not inv:
        raise ValueError("Factuur niet gevonden")
    if inv.status != "concept":
        raise ValueError("Alleen een conceptfactuur kan aangepast worden")
    return f"Factuur {inv.number or '(concept)'} van {inv.client_name}: {a.get('uitleg') or 'regels aanpassen'}"


def _run_invoice_edit(session: Session, org: Organisation, a: dict) -> str:
    inv = invoicing.get(session, org, int(a["invoice_id"]))
    before = invoicing.totals(inv)["total"]
    invoicing.save(session, inv, a.get("regels") or inv.lines, notes=a.get("opmerkingen", inv.notes),
                   due_days=a.get("betaaltermijn_dagen"))
    return f"Factuur aangepast: totaal {before:.2f} → {invoicing.totals(inv)['total']:.2f} EUR incl. btw."


def _describe_approve(session: Session, org: Organisation, a: dict) -> str:
    inv = invoicing.get(session, org, int(a["invoice_id"]))
    if not inv:
        raise ValueError("Factuur niet gevonden")
    return f"Factuur voor {inv.client_name} goedkeuren ({invoicing.totals(inv)['total']:.2f} EUR incl. btw)"


def _run_approve(session: Session, org: Organisation, a: dict) -> str:
    inv = invoicing.approve(session, org, invoicing.get(session, org, int(a["invoice_id"])), actor="ai")
    return f"Factuur {inv.number} goedgekeurd."


def _describe_todo(session: Session, org: Organisation, a: dict) -> str:
    t = session.get(Todo, int(a["todo_id"]))
    if not t or t.org_id != org.id:
        raise ValueError("Taak niet gevonden")
    return f"Taak afvinken: {t.title}"


def _run_todo(session: Session, org: Organisation, a: dict) -> str:
    t = session.get(Todo, int(a["todo_id"]))
    todos.complete(session, t)
    return f"Taak '{t.title}' afgevinkt."


LINE_SCHEMA = {"type": "object", "properties": {
    "description": {"type": "string"}, "qty": {"type": "number"}, "unit": {"type": "string"},
    "unit_price": {"type": "number", "description": "excl. btw, per eenheid; negatief voor korting"},
    "vat_pct": {"type": "number"}}, "required": ["description", "qty", "unit_price", "vat_pct"]}

ACTIONS: dict[str, tuple[Callable, Callable, dict, str]] = {
    "project_status_wijzigen": (_describe_status, _run_status, {"type": "object", "properties": {
        "project_id": {"type": "integer"}, "status": {"type": "string"},
        "datum": {"type": "string", "description": "JJJJ-MM-DD, enkel bij in_uitvoering/werk_afgerond; nooit in de toekomst"},
        "notitie": {"type": "string"}}, "required": ["project_id", "status"]},
        "Stel voor om de status van een project te wijzigen (één stap vooruit of terug, of 'geweigerd')."),
    "factuur_aanpassen": (_describe_invoice_edit, _run_invoice_edit, {"type": "object", "properties": {
        "invoice_id": {"type": "integer"},
        "regels": {"type": "array", "items": LINE_SCHEMA, "description": "de VOLLEDIGE nieuwe lijst regels"},
        "opmerkingen": {"type": "string"}, "betaaltermijn_dagen": {"type": "integer"},
        "uitleg": {"type": "string", "description": "korte uitleg van de wijziging voor de gebruiker"}},
        "required": ["invoice_id", "uitleg"]},
        "Stel een aanpassing voor aan een CONCEPTfactuur. Haal eerst factuur_details op en geef de volledige nieuwe regellijst."),
    "factuur_goedkeuren": (_describe_approve, _run_approve, {"type": "object", "properties": {"invoice_id": {"type": "integer"}},
        "required": ["invoice_id"]}, "Stel voor een conceptfactuur goed te keuren (krijgt nummer en mededeling)."),
    "taak_afvinken": (_describe_todo, _run_todo, {"type": "object", "properties": {"todo_id": {"type": "integer"}},
        "required": ["todo_id"]}, "Stel voor een to-do af te vinken."),
}


def tool_definitions() -> list[dict]:
    defs = [{"name": n, "description": READ_DESCRIPTIONS[n], "input_schema": schema} for n, (_, schema) in READ.items()]
    defs += [{"name": n, "description": desc + " Voert NIETS uit: de gebruiker moet bevestigen.", "input_schema": schema}
             for n, (_, _, schema, desc) in ACTIONS.items()]
    return defs
