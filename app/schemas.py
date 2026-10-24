"""Domain schemas (Pydantic). These are the contracts between AI layer, engine, API and MCP.

Design rule: the LLM only ever produces/edits *Geometry* and *adjustments*;
all arithmetic happens deterministically in app.engine on these types.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

OpeningKind = Literal["raam", "deur", "poort", "andere"]
Scope = Literal[
    "muren", "plafonds", "houtwerk", "buiten", "terras", "gazon", "beplanting", "graafwerk", "always"
]


class Opening(BaseModel):
    kind: OpeningKind = "raam"
    width_m: float = Field(gt=0)
    height_m: float = Field(gt=0)
    count: int = Field(default=1, ge=1)

    @property
    def area_m2(self) -> float:
        return self.width_m * self.height_m * self.count


class Zone(BaseModel):
    """A room (schilder) or a garden zone (tuinaanleg). Kind drives which posts apply."""

    id: str
    name: str
    kind: str = "ruimte"  # ruimte | terras | gazon | haag | border | oprit | vijver | andere
    area_m2: float | None = Field(default=None, ge=0)
    perimeter_m: float | None = Field(default=None, ge=0)
    length_m: float | None = Field(default=None, ge=0)  # linear zones (haag, afsluiting)
    height_m: float | None = Field(default=None, gt=0)  # ceiling height / hedge height
    depth_m: float | None = Field(default=None, ge=0)  # excavation depth
    count: int | None = Field(default=None, ge=0)  # countable zones (beplanting)
    openings: list[Opening] = Field(default_factory=list)
    in_scope: bool = True
    tags: list[str] = Field(default_factory=list)  # e.g. "vochtig", "nieuw_pleister"
    notes: str | None = None
    polygon: list[tuple[float, float]] | None = None  # metres, for the annotated plan


class Assumption(BaseModel):
    key: str
    text: str
    confirmed: bool = False
    source: Literal["ai", "config", "user", "skill"] = "ai"


class Geometry(BaseModel):
    """What the plan analysis produced, after/before user confirmation."""

    source: Literal["photo", "pdf", "manual", "mcp"] = "manual"
    scale_confirmed: bool = False
    scale_note: str | None = None
    default_height_m: float | None = None
    zones: list[Zone] = Field(default_factory=list)
    assumptions: list[Assumption] = Field(default_factory=list)

    def zone(self, zone_id: str) -> Zone | None:
        return next((z for z in self.zones if z.id == zone_id), None)


# ---------- pricing configuration ----------


class PriceItem(BaseModel):
    code: str
    label: str
    unit: str  # m2 | lm | stuk | ruimte | m3
    material_price: float = 0.0  # per material_unit (or per unit when material_unit is None)
    material_unit: str | None = None  # l | zak | stuk | ...
    coverage: float | None = None  # units covered per material_unit per layer (e.g. 8 m2 per litre)
    hours_per_unit: float = 0.0
    hours_scale_with_layers: bool = False


class PriceConfig(BaseModel):
    sector: str
    version: int = 1
    currency: str = "EUR"
    hourly_rate: float = 45.0
    margin_pct: float = 15.0  # on material
    waste_pct: float = 10.0  # on material quantity
    vat_pct: float = 21.0
    vat_label: str = "Btw 21 %"
    validity_days: int = 30
    round_hours_to: float = 0.5
    items: list[PriceItem] = Field(default_factory=list)

    def item(self, code: str) -> PriceItem | None:
        return next((i for i in self.items if i.code == code), None)


# ---------- skill overrides (structured adjustments) ----------


class Adjustment(BaseModel):
    """One structured rule derived from the user's free-text skill override.

    op:
      set_param      key, value
      tag_zones      match (regex on zone name/kind), tag
      exclude_zones  match
      set_layers     post, layers, match (optional)
      disable_post   post
      hours_factor   when ("height_gt"), value, factor
    """

    op: Literal["set_param", "tag_zones", "exclude_zones", "set_layers", "disable_post", "hours_factor"]
    key: str | None = None
    value: float | str | None = None
    match: str | None = None
    tag: str | None = None
    post: str | None = None
    layers: int | None = None
    when: str | None = None
    factor: float | None = None
    reason: str | None = None


# ---------- results ----------


class MeetstaatLine(BaseModel):
    zone_id: str
    zone_name: str
    post_code: str
    post_label: str
    unit: str
    gross_qty: float
    deduction_qty: float
    net_qty: float
    layers: int = 1
    material_qty: float = 0.0
    material_unit: str | None = None
    material_cost: float = 0.0
    hours: float = 0.0
    labour_cost: float = 0.0
    line_total: float = 0.0
    note: str | None = None


class QuoteTotals(BaseModel):
    area_total_m2: float
    hours_total: float
    material_cost: float
    margin: float
    labour_cost: float
    subtotal_excl: float
    vat_pct: float
    vat: float
    total_incl: float
    currency: str = "EUR"


class QuoteResult(BaseModel):
    sector: str
    lines: list[MeetstaatLine]
    totals: QuoteTotals
    assumptions: list[Assumption]
    warnings: list[str] = Field(default_factory=list)
    config_version: int
    skill_version: str
    override_version: int | None = None
    indicative: bool = True  # True until every assumption is confirmed and the scale is confirmed
    created_on: date = Field(default_factory=date.today)


class CompanyLookup(BaseModel):
    """Result of the e-mail domain lookup at sign-up."""

    email: str
    domain: str
    website: str | None = None
    company_name: str | None = None
    sector: str | None = None  # schilder | tuinaanleg | other
    sector_confidence: float = 0.0
    address: str | None = None
    vat_number: str | None = None
    phone: str | None = None
    language: str = "nl"
    method: Literal["ai", "heuristic", "none"] = "none"
    evidence: str | None = None
