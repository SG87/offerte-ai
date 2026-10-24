"""REST API v1. Authentication: header `X-API-Key: <org api key>` (shown on the configuratie page)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, EmailStr, Field
from sqlmodel import Session

from app import services
from app.ai.client import AIUnavailable
from app.db import get_session
from app.engine import calculate_quote, load_skill
from app.models import Organisation
from app.schemas import Adjustment, CompanyLookup, Geometry, PriceConfig, QuoteResult

router = APIRouter(tags=["api-v1"])


def require_org(
    x_api_key: str | None = Header(default=None), session: Session = Depends(get_session)
) -> Organisation:
    if not x_api_key:
        raise HTTPException(401, "X-API-Key header ontbreekt")
    org = services.get_org_by_api_key(session, x_api_key)
    if not org:
        raise HTTPException(401, "Ongeldige API-key")
    return org


class SignupIn(BaseModel):
    email: EmailStr
    lookup: bool = True


class SignupOut(BaseModel):
    org_id: int
    email: str
    api_key: str
    sector: str
    created: bool
    lookup: CompanyLookup | None = None


@router.post("/signup", response_model=SignupOut)
def signup(body: SignupIn, session: Session = Depends(get_session)):
    org, lookup, created = services.signup(session, body.email, do_lookup=body.lookup)
    return SignupOut(
        org_id=org.id, email=org.email, api_key=org.api_key, sector=org.sector, created=created, lookup=lookup
    )


@router.get("/me")
def me(org: Organisation = Depends(require_org)):
    return {
        "org_id": org.id,
        "email": org.email,
        "company_name": org.company_name,
        "sector": org.sector,
        "address": org.address,
        "vat_number": org.vat_number,
    }


@router.get("/sectors")
def sectors():
    return services.available_sector_list()


@router.get("/config", response_model=PriceConfig)
def get_config(org: Organisation = Depends(require_org), session: Session = Depends(get_session)):
    return services.current_config(session, org)


@router.put("/config", response_model=PriceConfig)
def put_config(
    config: PriceConfig, org: Organisation = Depends(require_org), session: Session = Depends(get_session)
):
    services.save_config(session, org, config)
    return services.current_config(session, org)


class OverrideIn(BaseModel):
    free_text: str


@router.put("/skill-override")
def put_override(
    body: OverrideIn, org: Organisation = Depends(require_org), session: Session = Depends(get_session)
):
    row, unsupported = services.save_override(session, org, body.free_text)
    return {
        "version": row.version,
        "translated_by": row.translated_by,
        "adjustments": row.adjustments,
        "unsupported": unsupported,
    }


class ProjectIn(BaseModel):
    name: str
    client_name: str | None = None
    client_address: str | None = None
    work_type: str = "renovatie_woning_10j"
    scope: list[str] | None = None
    notes: str | None = None


@router.post("/projects")
def create_project(
    body: ProjectIn, org: Organisation = Depends(require_org), session: Session = Depends(get_session)
):
    p = services.create_project(session, org, **body.model_dump())
    return {"project_id": p.id, "name": p.name, "scope": p.scope, "vat_pct": p.vat_pct}


@router.get("/projects")
def list_projects(org: Organisation = Depends(require_org), session: Session = Depends(get_session)):
    return [
        {"project_id": p.id, "name": p.name, "client_name": p.client_name, "status": p.status}
        for p in services.list_projects(session, org)
    ]


def _project(session: Session, org: Organisation, project_id: int):
    p = services.get_project(session, org, project_id)
    if not p:
        raise HTTPException(404, "Project niet gevonden")
    return p


@router.post("/projects/{project_id}/plan", response_model=Geometry)
async def upload_plan(
    project_id: int,
    file: UploadFile = File(...),
    org: Organisation = Depends(require_org),
    session: Session = Depends(get_session),
):
    p = _project(session, org, project_id)
    data = await file.read()
    geometry, _ = services.analyze_upload(session, org, p, data, file.filename or "plan", file.content_type)
    return geometry


@router.get("/projects/{project_id}/geometry", response_model=Geometry)
def get_geometry(
    project_id: int, org: Organisation = Depends(require_org), session: Session = Depends(get_session)
):
    return services.current_geometry(session, _project(session, org, project_id))


@router.put("/projects/{project_id}/geometry", response_model=Geometry)
def put_geometry(
    project_id: int,
    geometry: Geometry,
    org: Organisation = Depends(require_org),
    session: Session = Depends(get_session),
):
    p = _project(session, org, project_id)
    services.save_geometry(session, p, geometry)
    return geometry


class AssistIn(BaseModel):
    instruction: str = Field(min_length=2)


@router.post("/projects/{project_id}/assist")
def assist(
    project_id: int,
    body: AssistIn,
    org: Organisation = Depends(require_org),
    session: Session = Depends(get_session),
):
    p = _project(session, org, project_id)
    try:
        geometry, summary = services.run_assist(session, org, p, body.instruction)
    except AIUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"summary": summary, "geometry": geometry}


@router.post("/projects/{project_id}/quote", response_model=QuoteResult)
def quote(project_id: int, org: Organisation = Depends(require_org), session: Session = Depends(get_session)):
    p = _project(session, org, project_id)
    _, result = services.run_quote(session, org, p)
    return result


@router.get("/projects/{project_id}/quote.pdf")
def quote_pdf(
    project_id: int, org: Organisation = Depends(require_org), session: Session = Depends(get_session)
):
    p = _project(session, org, project_id)
    q = services.latest_quote(session, p)
    if not q:
        raise HTTPException(404, "Nog geen offerte; roep eerst POST .../quote aan")
    return Response(services.offerte_pdf(org, p, q), media_type="application/pdf")


@router.get("/projects/{project_id}/meetstaat.xlsx")
def quote_xlsx(
    project_id: int, org: Organisation = Depends(require_org), session: Session = Depends(get_session)
):
    p = _project(session, org, project_id)
    q = services.latest_quote(session, p)
    if not q:
        raise HTTPException(404, "Nog geen offerte")
    return Response(
        services.meetstaat_xlsx(p, q),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@router.get("/projects/{project_id}/plan.svg")
def quote_svg(
    project_id: int, org: Organisation = Depends(require_org), session: Session = Depends(get_session)
):
    p = _project(session, org, project_id)
    return Response(
        services.plan_svg(session, p, services.latest_quote(session, p)), media_type="image/svg+xml"
    )


class PreviewIn(BaseModel):
    geometry: Geometry
    scope: list[str]
    sector: str | None = None
    vat_pct: float | None = None
    adjustments: list[Adjustment] = Field(default_factory=list)


@router.post("/quote/preview", response_model=QuoteResult)
def preview(
    body: PreviewIn, org: Organisation = Depends(require_org), session: Session = Depends(get_session)
):
    """Stateless calculation with the org's current config: handy for integrations and tests."""
    skill = load_skill(body.sector or org.sector)
    config = (
        services.current_config(session, org)
        if (body.sector or org.sector) == org.sector
        else skill.default_price_config()
    )
    adjustments, version = services.current_adjustments(session, org)
    return calculate_quote(
        body.geometry,
        skill,
        config,
        scope=body.scope,
        adjustments=adjustments + body.adjustments,
        vat_pct=body.vat_pct,
        override_version=version,
    )
