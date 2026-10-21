import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import ClassVar, Literal, Self

from pydantic import Field, model_validator

from app.schemas.common import ConflictDetail, InputSchema, ReadSchema, UpdateSchema

ContractState = Literal["PENDING", "ACTIVE", "EXPIRED", "CANCELLED", "TERMINATED"]


class ContractCreate(InputSchema):
    tenant_id: uuid.UUID
    property_id: uuid.UUID
    space_id: uuid.UUID | None = Field(
        default=None,
        description="The space held, with everything inside it (a whole floor, a portion, a "
        "cabin...). Omit for the whole property.",
    )
    start_date: date
    end_date: date | None = Field(
        default=None, description="Last day held, inclusive. Null means ongoing."
    )
    monthly_rent: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    security_deposit: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    currency: str = Field(default="INR", min_length=3, max_length=3, pattern="^[A-Z]{3}$")
    terms: str | None = Field(default=None, max_length=20_000)
    notes: str | None = None
    activate: bool = Field(
        default=False,
        description="true activates at once (holding the space); false leaves it PENDING, "
        "holding nothing until activated.",
    )

    @model_validator(mode="after")
    def _dates_ordered(self) -> Self:
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("end_date cannot be before start_date")
        return self


class ContractUpdate(UpdateSchema):
    """Commercial details. Dates change through extend or terminate once active."""

    required_fields: ClassVar[frozenset[str]] = frozenset({"currency", "start_date"})

    monthly_rent: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    security_deposit: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    currency: str | None = Field(default=None, min_length=3, max_length=3, pattern="^[A-Z]{3}$")
    terms: str | None = Field(default=None, max_length=20_000)
    notes: str | None = None
    start_date: date | None = Field(default=None, description="Only while PENDING.")
    end_date: date | None = Field(default=None, description="Only while PENDING.")


class ContractCancel(InputSchema):
    reason: str | None = Field(default=None, max_length=2000)


class ContractTerminate(InputSchema):
    termination_date: date = Field(
        description="Last day held, inclusive. The space is free from the day after."
    )
    reason: str | None = Field(default=None, max_length=2000)


class ContractExtend(InputSchema):
    end_date: date | None = Field(
        description="The new last day, inclusive, or null to make the contract ongoing."
    )


class ContractRead(ReadSchema):
    id: uuid.UUID
    reference: str
    tenant_id: uuid.UUID
    tenant_name: str
    property_id: uuid.UUID
    property_name: str
    space_id: uuid.UUID | None
    space_name: str | None
    space_type: str | None
    start_date: date
    end_date: date | None
    original_end_date: date | None = Field(
        description="The agreed last day, when termination brought end_date forward."
    )
    status: ContractState = Field(
        description="EXPIRED is an ACTIVE contract whose last day has passed."
    )
    holds_inventory: bool = Field(
        description="Whether the contract holds its dates (ACTIVE, EXPIRED or TERMINATED)."
    )
    monthly_rent: Decimal | None
    security_deposit: Decimal | None
    currency: str
    terms: str | None
    notes: str | None
    activated_at: datetime | None
    cancelled_at: datetime | None
    cancellation_reason: str | None
    terminated_at: datetime | None
    termination_reason: str | None
    created_at: datetime
    updated_at: datetime


class ContractScopeSpace(ReadSchema):
    id: uuid.UUID
    name: str
    code: str | None
    space_type: str
    depth: int
    parent_space_id: uuid.UUID | None
    archived: bool


class ContractScope(ReadSchema):
    contract: ContractRead
    spaces: list[ContractScopeSpace] = Field(
        description="The space held and every space inside it, which no one else can book "
        "or contract while the contract holds. Empty for a whole-property contract."
    )


class ContractAvailability(ReadSchema):
    available: bool
    message: str
    conflicts: list[ConflictDetail]
