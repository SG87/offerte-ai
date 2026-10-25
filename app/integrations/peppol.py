"""Peppol access point: the UBL invoice is POSTed to OFFERTE_PEPPOL_API_URL (application/xml, Bearer key).
Most access points (Billit, Storecove, e-invoice.be, …) take a UBL document this way; a provider that needs
its own envelope gets a small adapter here."""

from __future__ import annotations

import httpx

from app.config import get_settings
from app.models import Invoice


def send(xml: bytes, inv: Invoice) -> tuple[bool, str | None, str | None]:
    """(ok, reference from the access point, error)."""
    settings = get_settings()
    headers = {"Content-Type": "application/xml"}
    if settings.peppol_api_key:
        headers["Authorization"] = f"Bearer {settings.peppol_api_key}"
    try:
        r = httpx.post(settings.peppol_api_url, content=xml, headers=headers, timeout=30)
    except httpx.HTTPError as exc:
        return False, None, f"{type(exc).__name__}: {exc}"
    if r.status_code >= 300:
        return False, None, f"HTTP {r.status_code}: {r.text[:300]}"
    ref = None
    try:
        data = r.json()
        ref = str(data.get("id") or data.get("documentId") or data.get("transmissionId") or "") or None
    except ValueError:
        ref = r.headers.get("Location")
    return True, ref or inv.number, None
