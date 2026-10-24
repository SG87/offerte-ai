"""Database tables (SQLModel). JSON payloads are stored as text columns to keep SQLite simple."""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(UTC)


class Organisation(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    company_name: str | None = None
    website: str | None = None
    sector: str = "schilder"  # schilder | tuinaanleg | other
    address: str | None = None
    vat_number: str | None = None
    phone: str | None = None
    country: str = "BE"
    language: str = "nl"
    api_key: str = Field(default_factory=lambda: secrets.token_urlsafe(24), index=True)
    onboarding_done: bool = False
    created_at: datetime = Field(default_factory=_now)


class ConfigVersion(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    org_id: int = Field(index=True, foreign_key="organisation.id")
    version: int = 1
    sector: str
    data_json: str  # PriceConfig
    created_at: datetime = Field(default_factory=_now)

    @property
    def data(self) -> dict[str, Any]:
        return json.loads(self.data_json)


class SkillOverride(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    org_id: int = Field(index=True, foreign_key="organisation.id")
    sector: str
    version: int = 1
    free_text: str = ""
    adjustments_json: str = "[]"  # list[Adjustment]
    translated_by: str = "none"  # ai | manual | none
    created_at: datetime = Field(default_factory=_now)

    @property
    def adjustments(self) -> list[dict[str, Any]]:
        return json.loads(self.adjustments_json)


class Project(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    org_id: int = Field(index=True, foreign_key="organisation.id")
    name: str
    client_name: str | None = None
    client_address: str | None = None
    work_type: str = "renovatie_woning_10j"  # drives VAT suggestion
    vat_pct: float = 6.0
    scope_json: str = '["muren", "plafonds"]'
    notes: str | None = None
    status: str = "nieuw"  # nieuw | gemeten | offerte | verzonden
    created_at: datetime = Field(default_factory=_now)

    @property
    def scope(self) -> list[str]:
        return json.loads(self.scope_json)


class Plan(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(index=True, foreign_key="project.id")
    filename: str | None = None
    media_type: str | None = None
    path: str | None = None
    geometry_json: str = "{}"  # Geometry
    created_at: datetime = Field(default_factory=_now)

    @property
    def geometry(self) -> dict[str, Any]:
        return json.loads(self.geometry_json)


class Quote(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(index=True, foreign_key="project.id")
    number: str
    version: int = 1
    config_version: int = 1
    skill_version: str = "1.0"
    result_json: str  # QuoteResult
    status: str = "concept"  # concept | verzonden | aanvaard
    created_at: datetime = Field(default_factory=_now)

    @property
    def result(self) -> dict[str, Any]:
        return json.loads(self.result_json)


class SectorRequest(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    org_id: int | None = Field(default=None, index=True)
    email: str
    description: str
    created_at: datetime = Field(default_factory=_now)
