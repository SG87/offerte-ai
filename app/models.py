"""Database tables (SQLModel). JSON payloads are stored as text columns to keep SQLite simple."""

from __future__ import annotations

import json
import secrets
from datetime import UTC, date, datetime
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
    logo_file: str | None = None  # file name in <data_dir>/logos
    brand_primary: str | None = None  # "#rrggbb"
    brand_secondary: str | None = None
    purchase_contact_email: str | None = None  # receives purchase lists on stock shortage; falls back to email
    iban: str | None = None  # bank account on invoices (and in the Peppol UBL)
    bic: str | None = None
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
    customer_id: int | None = Field(default=None, index=True, foreign_key="customer.id")
    # proposed execution (suggested automatically, editable; goes on the quote and becomes the planning block)
    proposed_start: date | None = None
    proposed_end: date | None = None
    proposed_crew_json: str = "[]"  # employee ids
    proposal_note: str | None = None
    proposal_locked: bool = False  # True once the user edited it: no automatic recalculation
    work_started_on: date | None = None  # actual dates, set on 'in uitvoering' / 'werk afgerond' (never in the future)
    work_done_on: date | None = None
    status: str = "voorbereid"  # see app/workflow.py
    status_changed_at: datetime | None = None
    created_at: datetime = Field(default_factory=_now)

    @property
    def scope(self) -> list[str]:
        return json.loads(self.scope_json)

    @property
    def proposed_crew(self) -> list[int]:
        return json.loads(self.proposed_crew_json or "[]")


class Plan(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(index=True, foreign_key="project.id")
    filename: str | None = None
    media_type: str | None = None
    path: str | None = None
    geometry_json: str = "{}"  # Geometry
    analysis_status: str | None = None  # running | done | failed; None = no AI analysis (manual)
    analysis_message: str | None = None
    analysis_started_at: datetime | None = None
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
    status: str = "concept"  # concept | verzonden | aanvaard | geweigerd
    translations_json: str = "{}"  # {lang: {dutch text: translation}} for the customer's language, made once
    # online answer of the customer on /o/{public_token}
    public_token: str | None = Field(default=None, index=True)
    decision: str | None = None  # aanvaard | geweigerd
    decided_at: datetime | None = None
    decided_name: str | None = None
    decision_note: str | None = None
    decision_ip: str | None = None
    created_at: datetime = Field(default_factory=_now)

    @property
    def result(self) -> dict[str, Any]:
        return json.loads(self.result_json)

    @property
    def translations(self) -> dict[str, dict[str, str]]:
        return json.loads(self.translations_json or "{}")


class SectorRequest(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    org_id: int | None = Field(default=None, index=True)
    email: str
    description: str
    created_at: datetime = Field(default_factory=_now)


class ProjectEvent(SQLModel, table=True):
    """Project history: status changes, triggers that ran, mails queued, notes."""

    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(index=True, foreign_key="project.id")
    kind: str = "status"  # status | info | trigger | mail
    from_status: str | None = None
    to_status: str | None = None
    text: str = ""
    actor: str = "gebruiker"  # gebruiker | systeem | api | mcp | klant | leverancier
    ref_id: int | None = None  # OutboxMessage.id or IntegrationEvent.id, depending on kind
    link: str | None = None  # page in the app this entry points to
    created_at: datetime = Field(default_factory=_now)


class OutboxMessage(SQLModel, table=True):
    """E-mail the platform wants to send. Sent via SMTP when configured, otherwise it waits here (placeholder)."""

    id: int | None = Field(default=None, primary_key=True)
    org_id: int = Field(index=True, foreign_key="organisation.id")
    project_id: int | None = Field(default=None, index=True)
    kind: str  # e.g. aankooplijst | bestelvoorstel | bestelling | offerte | test
    quote_id: int | None = None  # kind 'offerte': this quote's PDF is attached (rebuilt at send time)
    invoice_id: int | None = None  # kind 'factuur': this invoice's PDF is attached (rebuilt at send time)
    order_id: int | None = None  # kind 'bestelling': the supplier gets a confirm button for this order
    to: str
    subject: str
    body: str
    status: str = "wacht"  # wacht (no mail server) | verzonden | mislukt
    sent_to: str | None = None  # where it really went (differs from `to` in test mode)
    error: str | None = None
    created_at: datetime = Field(default_factory=_now)
    sent_at: datetime | None = None


class IntegrationEvent(SQLModel, table=True):
    """Trigger for outside systems (stock agent, planning tool): pushed to the webhook, or pulled via the API."""

    id: int | None = Field(default=None, primary_key=True)
    org_id: int = Field(index=True, foreign_key="organisation.id")
    project_id: int | None = Field(default=None, index=True)
    type: str = Field(index=True)  # voorraad.controle | werk.inplannen
    payload_json: str = "{}"
    status: str = "open"  # open (nobody took it yet) | afgeleverd | mislukt | afgehandeld
    error: str | None = None
    created_at: datetime = Field(default_factory=_now)
    delivered_at: datetime | None = None

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self.payload_json)


class Customer(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    org_id: int = Field(index=True, foreign_key="organisation.id")
    name: str
    kind: str = "b2c"  # b2c: prices incl. btw on the quote | b2b: prices excl. btw (btw still added at the bottom)
    language: str = "nl"  # nl | fr | en: language of the quote this customer receives (see app/i18n.py)
    contact_name: str | None = None
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    vat_number: str | None = None
    website: str | None = None
    notes: str | None = None
    active: bool = True
    needs_review: bool = False  # filled in by AI (lookup or import) and not yet checked by the user
    review_note: str | None = None  # why it needs checking / where the data came from
    created_at: datetime = Field(default_factory=_now)

    @property
    def is_b2b(self) -> bool:
        return self.kind == "b2b"


class Supplier(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    org_id: int = Field(index=True, foreign_key="organisation.id")
    name: str
    contact_name: str | None = None
    email: str | None = None  # orders go here
    phone: str | None = None
    address: str | None = None
    customer_number: str | None = None  # our account number at the supplier
    vat_number: str | None = None
    website: str | None = None
    lead_days: int = 3
    notes: str | None = None
    active: bool = True
    needs_review: bool = False  # filled in by AI (lookup or import) and not yet checked by the user
    review_note: str | None = None
    created_at: datetime = Field(default_factory=_now)


class Employee(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    org_id: int = Field(index=True, foreign_key="organisation.id")
    name: str
    role: str = "vakman"  # vakman | ploegbaas | voorraad | administratie | zaakvoerder
    email: str | None = None
    phone: str | None = None
    channels_json: str = '["mail"]'  # preferred ways to reach them, see app.todos.CHANNELS (only mail works for now)
    stock_manager: bool = False  # receives order proposals to confirm
    planner: bool = False  # receives 'werk inplannen' to-dos
    invoicer: bool = False  # approves the invoices made when the work is done
    hours_per_day: float = 8.0
    color: str | None = None  # calendar colour "#rrggbb"
    notes: str | None = None
    active: bool = True
    created_at: datetime = Field(default_factory=_now)

    @property
    def channels(self) -> list[str]:
        return json.loads(self.channels_json or "[]")


class Absence(SQLModel, table=True):
    """Employee not available for work: holiday, sick, training…"""

    id: int | None = Field(default=None, primary_key=True)
    employee_id: int = Field(index=True, foreign_key="employee.id")
    date_from: date
    date_to: date
    reason: str = "verlof"  # verlof | ziek | opleiding | andere
    note: str | None = None
    created_at: datetime = Field(default_factory=_now)


class StockLevel(SQLModel, table=True):
    """Material in stock per price-list item (code). qty = physically present, reserved = promised to projects."""

    id: int | None = Field(default=None, primary_key=True)
    org_id: int = Field(index=True, foreign_key="organisation.id")
    code: str = Field(index=True)
    qty: float = 0.0
    reserved: float = 0.0
    unit: str | None = None
    reorder_point: float = 0.0  # below this available quantity we propose an order
    pack_size: float = 1.0  # order in multiples of this (e.g. 10 l buckets)
    supplier_id: int | None = Field(default=None, foreign_key="supplier.id")
    location: str | None = None
    updated_at: datetime = Field(default_factory=_now)

    @property
    def available(self) -> float:
        return round(self.qty - self.reserved, 2)


class StockImportRow(SQLModel, table=True):
    """A row from a stock import (Excel or one line of text) that could not be applied with certainty:
    unknown material, or values the AI was unsure about. The user picks the material and applies it, or ignores it."""

    id: int | None = Field(default=None, primary_key=True)
    org_id: int = Field(index=True, foreign_key="organisation.id")
    source: str  # file name, or "snelle invoer"
    raw: str  # the original row as text
    code: str | None = None  # proposed price-list item code
    qty: float | None = None
    unit: str | None = None
    reorder_point: float | None = None
    pack_size: float | None = None
    supplier_name: str | None = None
    location: str | None = None
    note: str | None = None  # why it needs checking
    status: str = "open"  # open | toegepast | genegeerd
    created_at: datetime = Field(default_factory=_now)


class StockReservation(SQLModel, table=True):
    """Material promised to an accepted project; consumed when the work is done, released when it is undone."""

    id: int | None = Field(default=None, primary_key=True)
    org_id: int = Field(index=True, foreign_key="organisation.id")
    project_id: int = Field(index=True, foreign_key="project.id")
    code: str = Field(index=True)
    qty: float
    unit: str | None = None
    status: str = "gereserveerd"  # gereserveerd | verbruikt | vrijgegeven
    created_at: datetime = Field(default_factory=_now)
    closed_at: datetime | None = None


class PurchaseOrder(SQLModel, table=True):
    """Order proposal per supplier. The stock manager confirms it in the app, then the order mail goes out."""

    id: int | None = Field(default=None, primary_key=True)
    org_id: int = Field(index=True, foreign_key="organisation.id")
    supplier_id: int | None = Field(default=None, foreign_key="supplier.id")
    number: str
    status: str = "voorstel"  # voorstel | besteld | ontvangen | geannuleerd
    note: str | None = None
    created_at: datetime = Field(default_factory=_now)
    confirmed_at: datetime | None = None
    confirmed_by: str | None = None
    received_at: datetime | None = None
    mail_id: int | None = None  # OutboxMessage to the supplier
    # the supplier's answer on /l/{public_token}: order received and handled, delivery per line
    public_token: str | None = Field(default=None, index=True)
    supplier_confirmed_at: datetime | None = None  # first answer
    supplier_updated_at: datetime | None = None  # latest answer
    supplier_contact: str | None = None
    supplier_note: str | None = None


class PurchaseOrderLine(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    order_id: int = Field(index=True, foreign_key="purchaseorder.id")
    code: str
    label: str
    qty: float
    unit: str | None = None
    project_id: int | None = Field(default=None, index=True)
    reason: str | None = None  # e.g. "tekort voor project X" | "onder bestelpunt"
    delivery_status: str | None = None  # leverbaar | nalevering | niet_leverbaar (filled in by the supplier)
    delivery_date: date | None = None  # expected delivery
    delivery_note: str | None = None


class Todo(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    org_id: int = Field(index=True, foreign_key="organisation.id")
    title: str
    detail: str | None = None
    kind: str = "manueel"  # manueel | bestelling | levering | inplannen | factuur | betaling | voorraad
    link: str | None = None  # page in the app where it is handled
    project_id: int | None = Field(default=None, index=True)
    ref_id: int | None = None  # PurchaseOrder.id etc., with `kind` to tell what it is
    assignee_id: int | None = Field(default=None, foreign_key="employee.id")
    due: date | None = None
    status: str = "open"  # open | gedaan
    created_at: datetime = Field(default_factory=_now)
    done_at: datetime | None = None


class PlanningItem(SQLModel, table=True):
    """A block of work on the calendar: which project, which day(s), who."""

    id: int | None = Field(default=None, primary_key=True)
    org_id: int = Field(index=True, foreign_key="organisation.id")
    project_id: int | None = Field(default=None, index=True, foreign_key="project.id")
    title: str | None = None  # free text when not tied to a project
    date_from: date = Field(index=True)
    date_to: date
    start_time: str | None = None  # "08:00"
    hours: float | None = None  # planned hours per day
    note: str | None = None
    status: str = "gepland"  # gepland | bezig | klaar
    created_at: datetime = Field(default_factory=_now)


class PlanningAssignee(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    item_id: int = Field(index=True, foreign_key="planningitem.id")
    employee_id: int = Field(index=True, foreign_key="employee.id")


class AIJob(SQLModel, table=True):
    """A Claude call running in the background, so the user can keep working. Shown in the header until seen."""

    id: int | None = Field(default=None, primary_key=True)
    org_id: int = Field(index=True, foreign_key="organisation.id")
    kind: str  # bedrijf | plan | assist | skill | contact | import
    title: str  # "Plan lezen: Definitief plan.pdf"
    status: str = Field(default="bezig", index=True)  # bezig | klaar | mislukt
    message: str | None = None
    link: str | None = None
    project_id: int | None = Field(default=None, index=True)
    ref_id: int | None = None  # Plan.id for kind 'plan'
    seen: bool = False
    created_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = None


class Invoice(SQLModel, table=True):
    """An invoice. Made as 'concept' when the work is done (from the latest quote), edited by hand or by the AI,
    then approved: only then it gets its number (no gaps), date and structured payment reference."""

    id: int | None = Field(default=None, primary_key=True)
    org_id: int = Field(index=True, foreign_key="organisation.id")
    project_id: int | None = Field(default=None, index=True, foreign_key="project.id")
    customer_id: int | None = Field(default=None, index=True)
    quote_id: int | None = None
    number: str | None = Field(default=None, index=True)  # F2026-0001, on approval
    status: str = "concept"  # concept | goedgekeurd | verzonden | betaald
    b2b: bool = False  # snapshot of the customer type: Peppol for B2B, PDF/mail for private customers
    language: str = "nl"
    client_name: str | None = None
    client_address: str | None = None
    client_vat: str | None = None
    client_email: str | None = None
    title: str | None = None
    invoice_date: date | None = None
    due_days: int = 30
    lines_json: str = "[]"  # [{description, qty, unit, unit_price (excl. btw), vat_pct}]
    notes: str | None = None
    structured_ref: str | None = None  # Belgian OGM +++123/4567/89012+++
    sent_via: str | None = None  # peppol | email | manueel
    sent_at: datetime | None = None
    peppol_status: str | None = None  # klaar (no access point configured) | verzonden | mislukt
    peppol_ref: str | None = None
    peppol_error: str | None = None
    paid_on: date | None = None
    approved_at: datetime | None = None
    created_at: datetime = Field(default_factory=_now)

    @property
    def lines(self) -> list[dict[str, Any]]:
        return json.loads(self.lines_json or "[]")


class AgentConversation(SQLModel, table=True):
    """A chat with the general AI agent. Messages are stored exactly as sent to and received from the API
    (thinking blocks included) and only ever appended to."""

    id: int | None = Field(default=None, primary_key=True)
    org_id: int = Field(index=True, foreign_key="organisation.id")
    title: str | None = None
    system: str = ""
    messages_json: str = "[]"
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    @property
    def messages(self) -> list[dict[str, Any]]:
        return json.loads(self.messages_json or "[]")


class AgentAction(SQLModel, table=True):
    """A change the agent proposed; it only runs when the user clicks 'Uitvoeren'."""

    id: int | None = Field(default=None, primary_key=True)
    org_id: int = Field(index=True, foreign_key="organisation.id")
    conversation_id: int = Field(index=True, foreign_key="agentconversation.id")
    name: str
    input_json: str = "{}"
    summary: str = ""
    status: str = "voorgesteld"  # voorgesteld | uitgevoerd | geannuleerd | mislukt
    result: str | None = None
    reported: bool = False  # the agent was told the outcome (in the next question)
    created_at: datetime = Field(default_factory=_now)

    @property
    def input(self) -> dict[str, Any]:
        return json.loads(self.input_json or "{}")
