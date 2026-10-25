"""Stock: levels per price-list item, reservations per accepted project, and purchase orders.

Flow on an accepted quote (see app/workflow.py):
  reserve_for_project -> shortage? -> propose_orders (one proposal per supplier)
  -> the stock manager confirms in the app -> confirm_order mails the supplier -> receive_order books it in.
Everything is deterministic; a stock agent can drive the same functions through the API.
"""

from __future__ import annotations

import math
import secrets
from datetime import UTC, date, datetime

from sqlmodel import Session, select

from app.models import (
    Organisation,
    Project,
    PurchaseOrder,
    PurchaseOrderLine,
    StockLevel,
    StockReservation,
    Supplier,
)
from app.schemas import PriceConfig, QuoteResult


def tracked_codes(config: PriceConfig, skill_defaults: PriceConfig) -> set[str]:
    """Price items whose material is bought and stocked (paint, tiles, plants), not costs like 'voorbereiding'.
    The user's choice wins; items without one follow the sector skill."""
    default = {i.code: bool(i.stock) for i in skill_defaults.items}
    return {i.code for i in config.items if (i.stock if i.stock is not None else default.get(i.code, False))}


def requirements(result: QuoteResult, tracked: set[str]) -> list[dict]:
    """Material per stocked price-list item, summed over the meetstaat (waste included, as calculated)."""
    need: dict[str, dict] = {}
    for line in result.lines:
        if line.material_qty <= 0 or line.post_code not in tracked:
            continue
        row = need.setdefault(
            line.post_code,
            {"code": line.post_code, "label": line.post_label, "unit": line.material_unit or line.unit, "required": 0.0},
        )
        row["required"] += line.material_qty
    for row in need.values():
        row["required"] = round(row["required"], 2)
    return sorted(need.values(), key=lambda r: r["label"])


# ---------- levels ----------


def stock_levels(session: Session, org: Organisation) -> dict[str, StockLevel]:
    return {s.code: s for s in session.exec(select(StockLevel).where(StockLevel.org_id == org.id)).all()}


def level_for(session: Session, org: Organisation, code: str, unit: str | None = None) -> StockLevel:
    row = stock_levels(session, org).get(code)
    if row is None:
        row = StockLevel(org_id=org.id, code=code, unit=unit)
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


def set_stock(session: Session, org: Organisation, code: str, qty: float, unit: str | None = None, **fields) -> StockLevel:
    row = level_for(session, org, code, unit)
    row.qty, row.updated_at = max(qty, 0.0), datetime.now(UTC)
    if unit:
        row.unit = unit
    for k, v in fields.items():
        if hasattr(row, k):
            setattr(row, k, v)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def status_of(level: StockLevel, on_order: float = 0.0) -> str:
    """ok | bijbestellen | besteld | tekort — what the stock page shows per item."""
    if level.available < 0:
        return "besteld" if on_order >= -level.available else "tekort"
    if level.available < level.reorder_point:
        return "besteld" if on_order > 0 else "bijbestellen"
    return "ok"


# ---------- reservations ----------


def active_reservations(session: Session, org: Organisation, project_id: int | None = None) -> list[StockReservation]:
    q = select(StockReservation).where(StockReservation.org_id == org.id, StockReservation.status == "gereserveerd")
    if project_id is not None:
        q = q.where(StockReservation.project_id == project_id)
    return list(session.exec(q).all())


def reserve_for_project(
    session: Session, org: Organisation, project: Project, result: QuoteResult, tracked: set[str]
) -> list[dict]:
    """Reserve the material of the quote. Returns rows with required, available_before, shortage (after reserving).
    Idempotent: a project with active reservations is not reserved twice."""
    if active_reservations(session, org, project.id):
        return []
    rows = []
    for req in requirements(result, tracked):
        level = level_for(session, org, req["code"], req["unit"])
        before = level.available
        level.reserved = round(level.reserved + req["required"], 2)
        level.updated_at = datetime.now(UTC)
        session.add(level)
        session.add(
            StockReservation(
                org_id=org.id, project_id=project.id, code=req["code"], qty=req["required"], unit=req["unit"]
            )
        )
        rows.append({**req, "available_before": before, "shortage": round(max(-level.available, 0.0), 2)})
    session.commit()
    return rows


def _close_reservations(session: Session, org: Organisation, project: Project, status: str, consume: bool) -> int:
    n = 0
    for res in active_reservations(session, org, project.id):
        level = level_for(session, org, res.code, res.unit)
        level.reserved = round(max(level.reserved - res.qty, 0.0), 2)
        if consume:
            level.qty = round(max(level.qty - res.qty, 0.0), 2)
        level.updated_at = datetime.now(UTC)
        res.status, res.closed_at = status, datetime.now(UTC)
        session.add(level)
        session.add(res)
        n += 1
    session.commit()
    return n


def release_for_project(session: Session, org: Organisation, project: Project) -> int:
    """Quote no longer accepted: give the material back."""
    return _close_reservations(session, org, project, "vrijgegeven", consume=False)


def consume_for_project(session: Session, org: Organisation, project: Project) -> int:
    """Work done: the reserved material left the warehouse."""
    return _close_reservations(session, org, project, "verbruikt", consume=True)


# ---------- purchase orders ----------


def open_orders(session: Session, org: Organisation) -> list[PurchaseOrder]:
    return list(
        session.exec(
            select(PurchaseOrder)
            .where(PurchaseOrder.org_id == org.id, PurchaseOrder.status.in_(["voorstel", "besteld"]))  # type: ignore[attr-defined]
            .order_by(PurchaseOrder.id)
        ).all()
    )


def all_orders(session: Session, org: Organisation) -> list[PurchaseOrder]:
    return list(
        session.exec(select(PurchaseOrder).where(PurchaseOrder.org_id == org.id).order_by(PurchaseOrder.id.desc())).all()
    )


def order_lines(session: Session, order: PurchaseOrder) -> list[PurchaseOrderLine]:
    return list(session.exec(select(PurchaseOrderLine).where(PurchaseOrderLine.order_id == order.id)).all())


def on_order_by_code(session: Session, org: Organisation) -> dict[str, float]:
    """Quantity in open proposals/orders per code (so we don't propose the same shortage twice)."""
    out: dict[str, float] = {}
    for order in open_orders(session, org):
        for line in order_lines(session, order):
            out[line.code] = out.get(line.code, 0.0) + line.qty
    return out


def _next_number(session: Session, org: Organisation) -> str:
    year = datetime.now(UTC).year
    n = len(session.exec(select(PurchaseOrder).where(PurchaseOrder.org_id == org.id)).all()) + 1
    return f"B{year}-{n:04d}"


def propose_orders(
    session: Session, org: Organisation, labels: dict[str, str], project: Project | None = None
) -> list[PurchaseOrder]:
    """Create order proposals for every item whose available stock (minus what is already on order) is below
    zero or below its reorder point. One proposal per supplier; items without supplier share one proposal.
    Quantities are rounded up to the pack size. Returns the new proposals (possibly none)."""
    on_order = on_order_by_code(session, org)
    need_by_supplier: dict[int | None, list[PurchaseOrderLine]] = {}
    for code, level in stock_levels(session, org).items():
        covered = level.available + on_order.get(code, 0.0)  # what we have or already asked for
        missing = max(level.reorder_point, 0.0) - covered
        if missing <= 0:
            continue
        pack = level.pack_size if level.pack_size and level.pack_size > 0 else 1.0
        qty = math.ceil(missing / pack - 1e-9) * pack
        reason = "onder bestelpunt" if level.available >= 0 else f"tekort voor {project.name}" if project else "tekort"
        need_by_supplier.setdefault(level.supplier_id, []).append(
            PurchaseOrderLine(
                order_id=0,
                code=code,
                label=labels.get(code, code),
                qty=round(qty, 2),
                unit=level.unit,
                project_id=project.id if project and level.available < 0 else None,
                reason=reason,
            )
        )
    orders = []
    for supplier_id, lines in need_by_supplier.items():
        order = PurchaseOrder(org_id=org.id, supplier_id=supplier_id, number=_next_number(session, org))
        session.add(order)
        session.commit()
        session.refresh(order)
        for line in lines:
            line.order_id = order.id
            session.add(line)
        session.commit()
        orders.append(order)
    return orders


def confirm_order(session: Session, org: Organisation, order: PurchaseOrder, by: str = "gebruiker") -> tuple[bool, str]:
    """Stock manager approved: mail the supplier and mark the order as placed. Returns (ok, message)."""
    from app.integrations import mail  # local import: mail knows nothing about stock

    if order.status != "voorstel":
        return False, f"Bestelling {order.number} is al {order.status}."
    supplier = session.get(Supplier, order.supplier_id) if order.supplier_id else None
    if supplier is None or not supplier.email:
        return False, (
            f"Bestelling {order.number}: geen leverancier met e-mailadres gekoppeld. "
            "Kies een leverancier bij de materialen op de voorraadpagina."
        )
    lines = order_lines(session, order)
    if not order.public_token:  # the supplier's confirm button in the mail
        order.public_token = secrets.token_urlsafe(24)
    body = (
        f"Beste {supplier.contact_name or supplier.name},\n\n"
        f"Graag bestellen wij{' onder klantnummer ' + supplier.customer_number if supplier.customer_number else ''}:\n\n"
        + "\n".join(f"- {ln.label}: {ln.qty:g} {ln.unit or ''}".rstrip() for ln in lines)
        + f"\n\nLeveradres: {org.address or '[adres]'}\n"
        f"Referentie: {order.number}\n\n"
        f"Met vriendelijke groeten,\n{org.company_name or org.email}\n{org.phone or ''}".rstrip()
        + "\n"
    )
    msg = mail.queue_mail(
        session, org, supplier.email, f"Bestelling {order.number} — {org.company_name or org.email}", body, kind="bestelling",
        order_id=order.id,
    )
    order.status, order.confirmed_at, order.confirmed_by, order.mail_id = "besteld", datetime.now(UTC), by, msg.id
    session.add(order)
    session.commit()
    state = "verzonden" if msg.status == "verzonden" else "klaargezet in de outbox (geen mailserver ingesteld)"
    return True, f"Bestelling {order.number} bevestigd; bestelmail naar {supplier.email} {state}."


def receive_order(session: Session, org: Organisation, order: PurchaseOrder) -> tuple[bool, str]:
    if order.status != "besteld":
        return False, f"Bestelling {order.number} is {order.status}, niet 'besteld'."
    for line in order_lines(session, order):
        level = level_for(session, org, line.code, line.unit)
        level.qty = round(level.qty + line.qty, 2)
        level.updated_at = datetime.now(UTC)
        session.add(level)
    order.status, order.received_at = "ontvangen", datetime.now(UTC)
    session.add(order)
    session.commit()
    return True, f"Bestelling {order.number} ontvangen en ingeboekt."


def cancel_order(session: Session, org: Organisation, order: PurchaseOrder) -> tuple[bool, str]:
    if order.status not in ("voorstel", "besteld"):
        return False, f"Bestelling {order.number} is al {order.status}."
    order.status = "geannuleerd"
    session.add(order)
    session.commit()
    return True, f"Bestelling {order.number} geannuleerd."


DELIVERY_STATUSES = {"leverbaar": "Leverbaar", "nalevering": "Nalevering", "niet_leverbaar": "Niet leverbaar"}


def supplier_response(
    session: Session,
    order: PurchaseOrder,
    lines: dict[int, tuple[str, date | None, str | None]],
    contact: str | None = None,
    note: str | None = None,
) -> tuple[bool, str]:
    """The supplier confirms the order and gives per line (status, expected date, note). Can be updated while the
    order is placed (e.g. a new date). Every line needs a status; a date unless it is not deliverable."""
    if order.status != "besteld":
        return False, f"Bestelling {order.number} is {order.status}; aanpassen kan niet meer."
    rows = order_lines(session, order)
    for ln in rows:
        status, when, _ = lines.get(ln.id, (None, None, None))
        if status not in DELIVERY_STATUSES:
            return False, f"Kies een status voor '{ln.label}'."
        if status != "niet_leverbaar" and when is None:
            return False, f"Geef een leverdatum voor '{ln.label}'."
    for ln in rows:
        status, when, line_note = lines[ln.id]
        ln.delivery_status = status
        ln.delivery_date = None if status == "niet_leverbaar" else when
        ln.delivery_note = (line_note or "").strip()[:500] or None
        session.add(ln)
    now = datetime.now(UTC)
    order.supplier_confirmed_at = order.supplier_confirmed_at or now
    order.supplier_updated_at = now
    order.supplier_contact = (contact or "").strip()[:120] or None
    order.supplier_note = (note or "").strip()[:2000] or None
    session.add(order)
    session.commit()
    return True, f"Bestelling {order.number} bevestigd door de leverancier."


# ---------- overview for the stock page ----------


def overview(session: Session, org: Organisation, config: PriceConfig, tracked: set[str]) -> list[dict]:
    levels = stock_levels(session, org)
    on_order = on_order_by_code(session, org)
    rows = []
    for item in config.items:
        if item.code not in tracked:
            continue
        level = levels.get(item.code) or StockLevel(org_id=org.id, code=item.code, unit=item.material_unit or item.unit)
        rows.append(
            {
                "item": item,
                "level": level,
                "unit": level.unit or item.material_unit or item.unit,
                "on_order": on_order.get(item.code, 0.0),
                "status": status_of(level, on_order.get(item.code, 0.0)),
            }
        )
    return rows
