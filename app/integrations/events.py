"""Integration events: triggers for systems outside the app (stock agent, planning tool).

Each event is stored, then POSTed to OFFERTE_WEBHOOK_URL when configured. Without a webhook it stays 'open'
and an agent can pick it up with GET /api/v1/events and mark it done with POST /api/v1/events/{id}/ack.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlmodel import Session, select

from app.config import get_settings
from app.models import IntegrationEvent, Organisation

USER_AGENT = "OfferteAI/0.1 (+webhook)"


def emit(
    session: Session, org: Organisation, event_type: str, payload: dict[str, Any], project_id: int | None = None
) -> IntegrationEvent:
    event = IntegrationEvent(
        org_id=org.id, project_id=project_id, type=event_type, payload_json=json.dumps(payload, ensure_ascii=False)
    )
    session.add(event)
    session.commit()
    session.refresh(event)
    deliver(session, event)
    return event


def envelope(event: IntegrationEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "type": event.type,
        "org_id": event.org_id,
        "project_id": event.project_id,
        "status": event.status,
        "created_at": event.created_at.isoformat(),
        "payload": event.payload,
    }


def deliver(session: Session, event: IntegrationEvent) -> None:
    settings = get_settings()
    if not settings.webhook_url:
        return
    body = json.dumps(envelope(event), ensure_ascii=False).encode()
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT, "X-Offerte-Event": event.type}
    if settings.webhook_secret:
        sig = hmac.new(settings.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        headers["X-Offerte-Signature"] = f"sha256={sig}"
    try:
        r = httpx.post(settings.webhook_url, content=body, headers=headers, timeout=5)
        r.raise_for_status()
        event.status, event.delivered_at, event.error = "afgeleverd", datetime.now(UTC), None
    except httpx.HTTPError as exc:
        event.status, event.error = "mislukt", f"{type(exc).__name__}: {exc}"
    session.add(event)
    session.commit()


def already_emitted(session: Session, project_id: int, event_type: str) -> IntegrationEvent | None:
    return session.exec(
        select(IntegrationEvent).where(IntegrationEvent.project_id == project_id, IntegrationEvent.type == event_type)
    ).first()


def list_events(
    session: Session, org: Organisation, event_type: str | None = None, status: str | None = None
) -> list[IntegrationEvent]:
    q = select(IntegrationEvent).where(IntegrationEvent.org_id == org.id)
    if event_type:
        q = q.where(IntegrationEvent.type == event_type)
    if status:
        q = q.where(IntegrationEvent.status == status)
    return list(session.exec(q.order_by(IntegrationEvent.id)).all())
