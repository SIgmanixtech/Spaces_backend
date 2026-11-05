"""CRM core request and response bodies (Phase 3A; proposals and tenant links, Phase 4A)."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import ClassVar, Literal, Self

from pydantic import EmailStr, Field, model_validator

from app.models.enums import BillingPeriod, LeadSource, LeadStage, ProposalStatus, SpaceType
from app.schemas.common import InputSchema, ReadSchema, UpdateSchema
from app.schemas.tenant import TenantCreate

_Money = Field(default=None, ge=0, max_digits=12, decimal_places=2)
_Area = Field(default=None, gt=0, max_digits=10, decimal_places=2)
_Phone = Field(default=None, min_length=5, max_length=32)
_Name = Field(default=None, min_length=1, max_length=200)


class _LeadDetails(InputSchema):
    company_name: str | None = _Name
    contact_name: str | None = _Name
    contact_email: EmailStr | None = None
    contact_phone: str | None = _Phone
    property_id: uuid.UUID | None = Field(default=None, description="Preferred property.")
    space_type: SpaceType | None = None
    capacity: int | None = Field(default=None, gt=0, le=100000)
    area_sqft: Decimal | None = _Area
    budget_amount: Decimal | None = _Money
    budget_period: BillingPeriod | None = None
    currency: str = Field(default="INR", pattern=r"^[A-Z]{3}$")
    desired_start_date: date | None = None
    owner_id: uuid.UUID | None = Field(default=None, description="An active administrator.")


class LeadCreate(_LeadDetails):
    source: LeadSource = Field(
        default=LeadSource.MANUAL,
        description="Where the lead came from (BR-008). An admin may correct it later; "
        "the change is recorded in the lead's history.",
    )

    @model_validator(mode="after")
    def _checks(self) -> Self:
        if not (self.company_name or self.contact_name):
            raise ValueError("Give a company name or a contact name.")
        if (self.budget_amount is None) != (self.budget_period is None):
            raise ValueError("Give the budget amount together with its billing period.")
        return self


class LeadUpdate(UpdateSchema):
    """The stage has its own endpoint. The source can be corrected (approved 2 Nov 2026);
    the change is recorded in the lead's history with the old and new source, and a lead's
    link to its Excel import or Meta Ads record is kept."""

    required_fields: ClassVar[frozenset[str]] = frozenset({"currency", "source"})

    source: LeadSource | None = None

    company_name: str | None = _Name
    contact_name: str | None = _Name
    contact_email: EmailStr | None = None
    contact_phone: str | None = _Phone
    property_id: uuid.UUID | None = None
    space_type: SpaceType | None = None
    capacity: int | None = Field(default=None, gt=0, le=100000)
    area_sqft: Decimal | None = _Area
    budget_amount: Decimal | None = _Money
    budget_period: BillingPeriod | None = None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    desired_start_date: date | None = None
    owner_id: uuid.UUID | None = None


class LeadRead(ReadSchema):
    id: uuid.UUID
    reference: str
    source: LeadSource
    stage: LeadStage
    stage_changed_at: datetime
    company_name: str | None
    contact_name: str | None
    contact_email: str | None
    contact_phone: str | None
    property_id: uuid.UUID | None
    property_name: str | None
    space_type: SpaceType | None
    capacity: int | None
    area_sqft: float | None
    budget_amount: float | None
    budget_period: BillingPeriod | None
    currency: str
    desired_start_date: date | None
    owner_id: uuid.UUID | None
    owner_name: str | None
    owner_email: str | None
    next_stage: LeadStage | None = Field(description="The only stage it can move to now.")
    open_tasks: int
    overdue_tasks: int
    tenant_id: uuid.UUID | None = Field(
        default=None, description="The tenant record this lead is linked to (Phase 4A)."
    )
    tenant_name: str | None = None
    tenant_linked_at: datetime | None = None
    is_prospect: bool = Field(
        default=False,
        description="True when the lead has at least one proposal that is not withdrawn. "
        "Derived; not a stage.",
    )
    created_at: datetime
    updated_at: datetime


class StageChange(InputSchema):
    stage: LeadStage


class InterestCreate(InputSchema):
    property_id: uuid.UUID
    space_id: uuid.UUID | None = Field(default=None, description="A space of that property.")
    note: str | None = Field(default=None, max_length=500)


class InterestRead(ReadSchema):
    id: uuid.UUID
    lead_id: uuid.UUID
    property_id: uuid.UUID
    property_name: str
    space_id: uuid.UUID | None
    space_name: str | None
    space_archived: bool
    note: str | None
    created_at: datetime


class NoteCreate(InputSchema):
    body: str = Field(min_length=1, max_length=5000)


class NoteRead(ReadSchema):
    id: uuid.UUID
    lead_id: uuid.UUID
    body: str
    author_email: str | None
    created_at: datetime
    updated_at: datetime


TaskState = Literal["OVERDUE", "DUE_TODAY", "UPCOMING", "DONE"]


class TaskCreate(InputSchema):
    title: str = Field(min_length=1, max_length=200)
    details: str | None = Field(default=None, max_length=5000)
    due_date: date
    owner_id: uuid.UUID | None = Field(default=None, description="An active administrator.")


class TaskUpdate(UpdateSchema):
    required_fields: ClassVar[frozenset[str]] = frozenset({"title", "due_date", "done"})

    title: str | None = Field(default=None, min_length=1, max_length=200)
    details: str | None = Field(default=None, max_length=5000)
    due_date: date | None = None
    owner_id: uuid.UUID | None = None
    done: bool | None = None


class TaskRead(ReadSchema):
    id: uuid.UUID
    lead_id: uuid.UUID
    lead_reference: str
    lead_label: str
    title: str
    details: str | None
    due_date: date
    state: TaskState = Field(description="From due_date and today's business date.")
    owner_id: uuid.UUID | None
    owner_name: str | None
    done: bool
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class DuplicateCheck(InputSchema):
    contact_email: str | None = Field(default=None, max_length=320)
    contact_phone: str | None = Field(default=None, max_length=32)
    company_name: str | None = Field(default=None, max_length=200)
    exclude_lead_id: uuid.UUID | None = None


class DuplicateMatch(ReadSchema):
    """A possible duplicate. Warning only: nothing is merged, refused or changed."""

    kind: Literal["LEAD", "TENANT"]
    id: uuid.UUID
    reference: str | None
    name: str
    company_name: str | None
    email: str | None
    phone: str | None
    status: str = Field(description="The lead's stage, or the tenant's status.")
    archived: bool
    matched_on: list[Literal["email", "phone", "company"]]


class OwnerRead(ReadSchema):
    id: uuid.UUID
    name: str | None
    email: str | None


# --- proposals (Phase 4A) ------------------------------------------------------------------


class _ProposalTerms(InputSchema):
    proposal_date: date
    valid_until: date | None = Field(default=None, description="Optional; not before the date.")
    start_date: date
    end_date: date | None = Field(default=None, description="Last day, inclusive; optional.")
    monthly_rent: Decimal | None = _Money
    security_deposit: Decimal | None = _Money
    currency: str = Field(default="INR", pattern=r"^[A-Z]{3}$")
    other_charges: str | None = Field(
        default=None, max_length=5000, description="As entered; nothing is calculated."
    )
    commercial_terms: str | None = Field(
        default=None, max_length=10000, description="As entered; nothing is calculated."
    )
    notes: str | None = Field(default=None, max_length=5000)


class ProposalCreate(_ProposalTerms):
    property_id: uuid.UUID
    space_ids: list[uuid.UUID] = Field(
        default_factory=list, max_length=200, description="Spaces of that property, if any."
    )

    @model_validator(mode="after")
    def _checks(self) -> Self:
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("The end date cannot be before the start date.")
        if self.valid_until is not None and self.valid_until < self.proposal_date:
            raise ValueError("Valid until cannot be before the proposal date.")
        if len(set(self.space_ids)) != len(self.space_ids):
            raise ValueError("A space is listed twice.")
        return self


class ProposalUpdate(UpdateSchema):
    """Only a draft can be edited. A sent proposal is revised by making a new one."""

    required_fields: ClassVar[frozenset[str]] = frozenset(
        {"property_id", "space_ids", "proposal_date", "start_date", "currency"}
    )

    property_id: uuid.UUID | None = None
    space_ids: list[uuid.UUID] | None = Field(default=None, max_length=200)
    proposal_date: date | None = None
    valid_until: date | None = None
    start_date: date | None = None
    end_date: date | None = None
    monthly_rent: Decimal | None = _Money
    security_deposit: Decimal | None = _Money
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    other_charges: str | None = Field(default=None, max_length=5000)
    commercial_terms: str | None = Field(default=None, max_length=10000)
    notes: str | None = Field(default=None, max_length=5000)


class ProposalAccept(InputSchema):
    accepted_on: date = Field(description="The day the lead accepted.")
    acceptance_note: str = Field(
        min_length=1,
        max_length=2000,
        description="How the acceptance was given, e.g. 'Email from the director'.",
    )


class ProposalClose(InputSchema):
    reason: str | None = Field(default=None, max_length=2000)


class ProposalSpaceRead(ReadSchema):
    space_id: uuid.UUID
    space_name: str
    space_type: str | None
    building_name: str | None
    floor_name: str | None
    archived: bool


class ProposalRead(ReadSchema):
    id: uuid.UUID
    reference: str
    lead_id: uuid.UUID
    lead_reference: str
    property_id: uuid.UUID
    property_name: str
    spaces: list[ProposalSpaceRead]
    status: ProposalStatus
    expired: bool = Field(description="Sent and past valid_until. Shown, never stored.")
    proposal_date: date
    valid_until: date | None
    start_date: date
    end_date: date | None
    monthly_rent: float | None
    security_deposit: float | None
    currency: str
    other_charges: str | None
    commercial_terms: str | None
    notes: str | None
    sent_at: datetime | None
    accepted_on: date | None
    acceptance_note: str | None
    accepted_at: datetime | None
    accepted_by_email: str | None
    rejected_at: datetime | None
    withdrawn_at: datetime | None
    closing_reason: str | None
    created_at: datetime
    updated_at: datetime


# --- lead-tenant link (Phase 4A) -------------------------------------------------------------


class TenantLink(InputSchema):
    tenant_id: uuid.UUID


class TenantFromLead(TenantCreate):
    """A new tenant from the lead's details, reviewed by the admin. When the details match
    existing tenants, set confirm_despite_matches after checking they are different."""

    confirm_despite_matches: bool = Field(
        default=False,
        description="The admin checked the possible matches and none is the same tenant.",
    )


class TenantUnlink(InputSchema):
    reason: str = Field(min_length=1, max_length=1000)
