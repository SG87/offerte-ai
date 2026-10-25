"""Suppliers, customers and stock entered with AI: one name/e-mail looked up, or a free-format file imported.

Everything runs as a background AI job (app/ai_jobs.py). What the AI is not sure about is never silently
applied as truth:
- suppliers/customers are created or completed, but flagged `needs_review` with the reason in `review_note`;
- stock rows are applied only when the material and quantity are certain; the rest waits in StockImportRow
  until the user picks the material and applies it (or ignores it).

Existing contacts are matched on e-mail or name: an import only fills their empty fields and flags conflicts.
"""

from __future__ import annotations

import re
from typing import Any

from sqlmodel import Session, select

from app import ai_jobs, services
from app.ai import import_mapping as im
from app.ai.contact_lookup import lookup_contact
from app.engine import load_skill
from app.integrations import stock as stock_mod
from app.models import Customer, Organisation, StockImportRow, Supplier

MODELS: dict[str, type] = {"leveranciers": Supplier, "klanten": Customer}
SINGULAR = {"leveranciers": "leverancier", "klanten": "klant"}
CONTACT_FIELDS = {
    "leveranciers": ["name", "contact_name", "email", "phone", "address", "vat_number", "website",
                     "customer_number", "lead_days", "notes"],
    "klanten": ["name", "kind", "language", "contact_name", "email", "phone", "address", "vat_number", "website",
                "notes"],
}
LABELS = {"name": "naam", "contact_name": "contactpersoon", "email": "e-mail", "phone": "telefoon",
          "address": "adres", "vat_number": "btw-nummer", "website": "website", "customer_number": "klantnummer",
          "lead_days": "levertermijn", "kind": "type klant", "language": "taal", "notes": "notities"}
# a field still at its model default on an existing record may be replaced by imported data
DEFAULTED = {"lead_days": 3}


def _key(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def find_existing(session: Session, org: Organisation, model: type, email: str | None, name: str | None):
    rows = session.exec(select(model).where(model.org_id == org.id)).all()
    if email:
        for r in rows:
            if r.email and r.email.lower() == email.lower():
                return r
    if name and _key(name):
        for r in rows:
            if _key(r.name) == _key(name):
                return r
    return None


def _flag(row, note: str) -> None:
    row.needs_review = True
    row.review_note = "; ".join(filter(None, [row.review_note, note]))[:1000]


def apply_contact(
    session: Session, org: Organisation, kind_key: str, data: dict, source: str, row=None
) -> tuple[Any, str, bool]:
    """Create or complete one supplier/customer.
    Returns (row, 'nieuw' | 'bijgewerkt' | 'ongewijzigd', flagged now as na te kijken)."""
    model = MODELS[kind_key]
    fields = CONTACT_FIELDS[kind_key]
    row = row or find_existing(session, org, model, data.get("email"), data.get("name"))
    issues = list(data.get("issues") or [])
    confident = data.get("confidence", 0) >= im.CONFIDENT and not issues
    if row is None:
        row = model(org_id=org.id, name=data.get("name") or "(zonder naam)")
        if kind_key == "klanten":
            row.language = org.language
        for f in fields:
            if data.get(f) not in (None, ""):
                setattr(row, f, data[f])
        state = "nieuw"
    else:
        changed, conflicts = False, []
        for f in fields:
            new = data.get(f)
            if new in (None, ""):
                continue
            old = getattr(row, f)
            if old in (None, "") or (f in DEFAULTED and old == DEFAULTED[f]):
                setattr(row, f, new)
                changed = True
            elif f == "notes":
                if str(new) not in str(old):
                    row.notes = f"{old}\n{new}"
                    changed = True
            elif str(old).strip().lower() != str(new).strip().lower():
                conflicts.append(f"{LABELS.get(f, f)} '{new}' (bewaard: '{old}')")
        if conflicts:
            issues.append("wijkt af van wat al bewaard was: " + ", ".join(conflicts))
            confident = False
        state = "bijgewerkt" if changed else "ongewijzigd"
    if not confident:
        _flag(row, f"{source}: " + ("; ".join(issues) if issues else "AI was niet zeker"))
    session.add(row)
    session.commit()
    session.refresh(row)
    return row, state, not confident


# ---------- one contact by name or e-mail ----------


def start_lookup(session: Session, org: Organisation, kind_key: str, query: str, row_id: int | None = None):
    """Background job: look the contact up and create it (or complete row_id). Returns (job, work)."""
    label = SINGULAR[kind_key]
    job = ai_jobs.start(session, org, "contact", f"{label.capitalize()} opzoeken: {query[:60]}",
                        link=f"/contacten/{kind_key}")

    def work(session: Session, org: Organisation, job) -> tuple[str, str | None]:
        found = lookup_contact(query, "supplier" if kind_key == "leveranciers" else "customer", org.country)
        existing = session.get(MODELS[kind_key], row_id) if row_id else None
        if existing is not None and existing.org_id != org.id:
            existing = None
        issues = []
        if found["method"] != "ai":
            issues.append("niets opgezocht met AI; enkel de invoer" if found["method"] == "input"
                          else "enkel de website gelezen, zonder AI")
        if found.get("is_private_person"):
            issues.append("particulier: niet opgezocht, vul zelf aan")
        if found["confidence"] < im.CONFIDENT and found.get("evidence"):
            issues.append(found["evidence"])
        data = {**found, "issues": issues}
        if kind_key == "leveranciers":
            data.pop("kind", None)
            data.pop("language", None)
        row, state, _ = apply_contact(session, org, kind_key, data, "AI-opzoeking", row=existing)
        verb = {"nieuw": "toegevoegd", "bijgewerkt": "aangevuld", "ongewijzigd": "niets nieuws gevonden"}[state]
        return (f"{row.name}: {verb}" + (" — na te kijken." if row.needs_review else "."),
                f"/contacten/{kind_key}?edit={row.id}")

    return job, work


# ---------- bulk import of suppliers / customers ----------


def start_contact_import(session: Session, org: Organisation, kind_key: str, filename: str, sheets: list[im.Sheet]):
    job = ai_jobs.start(session, org, "import", f"Import {kind_key}: {filename}", link=f"/contacten/{kind_key}")

    def work(session: Session, org: Organisation, job) -> tuple[str, str | None]:
        records, skipped, method = im.map_contacts(sheets, kind_key)
        counts = {"nieuw": 0, "bijgewerkt": 0, "ongewijzigd": 0}
        review = 0
        for rec in records:
            src = f"import {filename}" + (f" rij {rec['row']}" if rec.get("row") else "")
            if method != "ai":
                src += " (zonder AI)"
            _, state, flagged = apply_contact(session, org, kind_key, rec, src)
            counts[state] += 1
            review += flagged
        parts = [f"{counts['nieuw']} nieuw", f"{counts['bijgewerkt']} aangevuld"]
        if counts["ongewijzigd"]:
            parts.append(f"{counts['ongewijzigd']} al aanwezig")
        if review:
            parts.append(f"{review} na te kijken")
        if skipped:
            parts.append(f"{len(skipped)} rij{'' if len(skipped) == 1 else 'en'} overgeslagen")
        return f"{filename}: " + ", ".join(parts) + ".", f"/contacten/{kind_key}" + ("?review=1" if review else "")

    return job, work


# ---------- stock ----------


def stocked_items(session: Session, org: Organisation) -> list[dict]:
    cfg = services.current_config(session, org)
    tracked = stock_mod.tracked_codes(cfg, load_skill(org.sector).default_price_config())
    return [{"code": i.code, "label": i.label, "unit": i.material_unit or i.unit} for i in cfg.items if i.code in tracked]


def resolve_supplier(session: Session, org: Organisation, name: str | None, source: str) -> int | None:
    if not name or not _key(name):
        return None
    rows = session.exec(select(Supplier).where(Supplier.org_id == org.id)).all()
    key = _key(name)
    for s in rows:
        if _key(s.name) == key:
            return s.id
    for s in rows:
        if len(key) >= 4 and (key in _key(s.name) or _key(s.name) in key):
            return s.id
    sup = Supplier(org_id=org.id, name=name.strip(), needs_review=True,
                   review_note=f"Aangemaakt uit {source}; vul het e-mailadres voor bestellingen aan.")
    session.add(sup)
    session.commit()
    session.refresh(sup)
    return sup.id


def _unit_ok(row_unit: str | None, item_unit: str | None) -> bool:
    if not row_unit or not item_unit:
        return True
    norm = {"liter": "l", "litre": "l", "ltr": "l", "stuk": "st", "stuks": "st", "pcs": "st", "m2": "m²",
            "kilo": "kg", "kgs": "kg"}
    a, b = row_unit.strip().lower().rstrip("."), item_unit.strip().lower()
    return norm.get(a, a) == norm.get(b, b)


def apply_stock_records(session: Session, org: Organisation, records: list[dict], source: str) -> tuple[int, int]:
    """Apply the certain rows, queue the rest. Returns (applied, queued)."""
    items = {i["code"]: i for i in stocked_items(session, org)}
    seen: set[str] = set()
    applied = queued = 0
    for rec in records:
        code = rec.get("code")
        item = items.get(code) if code else None
        blocking = []  # our own checks; the AI's issues on a confident row are informative (e.g. a conversion)
        if item and code in seen:
            blocking.append("deze post staat meermaals in de import")
        if item and not _unit_ok(rec.get("unit"), item["unit"]):
            blocking.append(f"eenheid '{rec.get('unit')}' wijkt af van '{item['unit']}'")
        issues = list(rec.get("issues") or []) + blocking
        certain = bool(item) and rec.get("qty") is not None and rec.get("confidence", 0) >= im.CONFIDENT and not blocking
        if certain:
            level = stock_mod.level_for(session, org, code, item["unit"])
            stock_mod.set_stock(
                session, org, code, rec["qty"], item["unit"],
                reorder_point=rec["reorder_point"] if rec.get("reorder_point") is not None else level.reorder_point,
                pack_size=max(rec["pack_size"], 0.01) if rec.get("pack_size") else level.pack_size,
                supplier_id=resolve_supplier(session, org, rec.get("supplier"), source) or level.supplier_id,
                location=rec.get("location") or level.location,
            )
            seen.add(code)
            applied += 1
        else:
            session.add(StockImportRow(
                org_id=org.id, source=source, raw=(rec.get("raw") or "")[:2000], code=code if item else None,
                qty=rec.get("qty"), unit=rec.get("unit"), reorder_point=rec.get("reorder_point"),
                pack_size=rec.get("pack_size"), supplier_name=rec.get("supplier"), location=rec.get("location"),
                note="; ".join(issues) or "AI was niet zeker",
            ))
            session.commit()
            queued += 1
    return applied, queued


def _stock_message(source: str, applied: int, queued: int, skipped: int) -> str:
    parts = [f"{applied} regel{'' if applied == 1 else 's'} bijgewerkt"]
    if queued:
        parts.append(f"{queued} na te kijken")
    if skipped:
        parts.append(f"{skipped} overgeslagen")
    return f"{source}: " + ", ".join(parts) + "."


def start_stock_import(session: Session, org: Organisation, source: str, sheets: list[im.Sheet]):
    job = ai_jobs.start(session, org, "import", f"Voorraad importeren: {source}", link="/voorraad")

    def work(session: Session, org: Organisation, job) -> tuple[str, str | None]:
        suppliers = [s.name for s in session.exec(select(Supplier).where(Supplier.org_id == org.id)).all()]
        records, skipped, _ = im.map_stock(sheets, stocked_items(session, org), suppliers)
        applied, queued = apply_stock_records(session, org, records, source)
        return _stock_message(source, applied, queued, len(skipped)), "/voorraad" + ("#na-te-kijken" if queued else "")

    return job, work


def text_as_sheet(text: str) -> list[im.Sheet]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()][: im.MAX_ROWS]
    return [im.Sheet("snelle invoer", [(i, [ln]) for i, ln in enumerate(lines, start=1)])]


def open_stock_rows(session: Session, org: Organisation) -> list[StockImportRow]:
    return list(session.exec(
        select(StockImportRow).where(StockImportRow.org_id == org.id, StockImportRow.status == "open")
        .order_by(StockImportRow.id)
    ).all())


def resolve_stock_row(
    session: Session, org: Organisation, row: StockImportRow, action: str, code: str | None = None,
    qty: float | None = None,
) -> tuple[bool, str]:
    if action == "negeer":
        row.status = "genegeerd"
        session.add(row)
        session.commit()
        return True, "Regel genegeerd."
    items = {i["code"]: i for i in stocked_items(session, org)}
    item = items.get(code or "")
    qty = row.qty if qty is None else qty
    if not item:
        return False, "Kies het materiaal waar deze regel bij hoort."
    if qty is None or qty < 0:
        return False, "Vul een hoeveelheid in."
    level = stock_mod.level_for(session, org, item["code"], item["unit"])
    stock_mod.set_stock(
        session, org, item["code"], qty, item["unit"],
        reorder_point=row.reorder_point if row.reorder_point is not None else level.reorder_point,
        pack_size=max(row.pack_size, 0.01) if row.pack_size else level.pack_size,
        supplier_id=resolve_supplier(session, org, row.supplier_name, row.source) or level.supplier_id,
        location=row.location or level.location,
    )
    row.status, row.code, row.qty = "toegepast", item["code"], qty
    session.add(row)
    session.commit()
    return True, f"{item['label']}: voorraad op {qty:g} {item['unit'] or ''} gezet."
