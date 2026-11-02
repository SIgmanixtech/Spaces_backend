"""CRM core request and response bodies (Phase 3A)."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import ClassVar, Literal, Self

from pydantic import EmailStr, Field, model_validator

from app.models.enums import BillingPeriod, LeadSource, LeadStage, SpaceType
from app.schemas.common import InputSchema, ReadSchema, UpdateSchema

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
