import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import ClassVar, Literal, Self

from pydantic import Field, model_validator

from app.models.enums import ContractCategory, ContractType, DocumentStatus
from app.schemas.common import ConflictDetail, InputSchema, ReadSchema, UpdateSchema

ContractState = Literal["PENDING", "ACTIVE", "EXPIRED", "CANCELLED", "TERMINATED"]


class CategoryFields(InputSchema):
    """Category and its details (owner decisions D8 and D10 of 2 Nov 2026). Required
    details are checked when the contract is approved, so a draft can be saved first."""

    category: ContractCategory | None = Field(
        default=None,
        description="SPACE, OFFICE, GOVERNMENT, PARKING or COMMERCIAL: what the agreement is "
        "for. Separate from the agreement type. Null on contracts made before categories.",
    )
    main_contract_id: uuid.UUID | None = Field(
        default=None,
        description="Parking only: the same tenant's main contract it belongs to (D8.4). "
        "Null for a standalone Parking contract (D8.5).",
    )
    expected_headcount: int | None = Field(
        default=None, gt=0, le=100_000, description="Commercial: expected headcount (D10.1)."
    )
    occupants: int | None = Field(
        default=None, gt=0, le=100_000, description="Office: number of occupants."
    )
    government_department: str | None = Field(default=None, max_length=200)
    government_order_reference: str | None = Field(default=None, max_length=100)
    vehicle_registration: str | None = Field(
        default=None, max_length=20, description="Parking: vehicle registration."
    )
    lock_in_period: str | None = Field(
        default=None, max_length=100, description="As printed on the agreement (D10.8)."
    )
    notice_period: str | None = Field(
        default=None, max_length=100, description="As printed on the agreement (D10.8)."
    )
    payment_due_day: int | None = Field(
        default=None, ge=1, le=28, description="Day of the month the fee is due (coworking)."
    )
    tds_percent: Decimal | None = Field(
        default=None,
        ge=0,
        le=100,
        max_digits=5,
        decimal_places=2,
        description="TDS the member deducts, per agreement. Empty: no TDS.",
    )


class ContractCreate(CategoryFields):
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
    contract_type: ContractType | None = Field(
        default=None,
        description="SPACE_LEASE or COWORKING_LICENCE: which agreement applies. Null falls "
        "back to the space's setting.",
    )
    category: ContractCategory | None = None
    main_contract_id: uuid.UUID | None = None
    expected_headcount: int | None = Field(default=None, gt=0, le=100_000)
    occupants: int | None = Field(default=None, gt=0, le=100_000)
    government_department: str | None = Field(default=None, max_length=200)
    government_order_reference: str | None = Field(default=None, max_length=100)
    vehicle_registration: str | None = Field(default=None, max_length=20)
    lock_in_period: str | None = Field(default=None, max_length=100)
    notice_period: str | None = Field(default=None, max_length=100)
    payment_due_day: int | None = Field(default=None, ge=1, le=28)
    tds_percent: Decimal | None = Field(default=None, ge=0, le=100, max_digits=5, decimal_places=2)


class ContractCancel(InputSchema):
    reason: str | None = Field(default=None, max_length=2000)
    include_parking: bool = Field(
        default=True, description="Also cancel Parking contracts linked to it (LC-5)."
    )


class ContractTerminate(InputSchema):
    termination_date: date = Field(
        description="Last day held, inclusive. The space is free from the day after."
    )
    reason: str | None = Field(default=None, max_length=2000)
    include_parking: bool = Field(
        default=True, description="Also end Parking contracts linked to it (LC-5)."
    )


class ContractExtend(InputSchema):
    end_date: date | None = Field(
        description="The new last day, inclusive, or null to make the contract ongoing."
    )
    include_parking: bool = Field(
        default=True,
        description="Also extend linked Parking contracts that end on the same day (LC-6).",
    )


class ContractHold(InputSchema):
    hold_until: date | None = Field(
        default=None, description="Last day held, inclusive. Default: 7 days from today."
    )
    reason: str | None = Field(default=None, max_length=2000)


class ContractReason(InputSchema):
    reason: str | None = Field(default=None, max_length=2000)


class ContractRenew(InputSchema):
    end_date: date | None = Field(
        default=None,
        description="Last day of the renewal. Omitted: the same length as the contract. "
        "Null: ongoing (not for Commercial or Government).",
    )
    monthly_rent: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    security_deposit: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    terms: str | None = Field(default=None, max_length=20_000)
    notes: str | None = Field(default=None, max_length=2000)
    include_parking: bool = Field(
        default=True, description="Also renew linked Parking contracts (LC-6)."
    )


class BookingConvert(CategoryFields):
    """Admin turns a monthly booking into a pending contract (D12.3). Omitted values are
    prefilled from the booking and the space's pricing."""

    tenant_id: uuid.UUID | None = Field(
        default=None, description="Required when the booking has no tenant."
    )
    start_date: date | None = None
    end_date: date | None = None
    monthly_rent: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    security_deposit: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    notes: str | None = Field(default=None, max_length=2000)


class PricingSuggestion(ReadSchema):
    monthly_rent: Decimal | None
    security_deposit: Decimal | None


class ContractRead(ReadSchema):
    id: uuid.UUID
    reference: str
    proposal_id: uuid.UUID | None = Field(
        default=None, description="The accepted proposal it was made from (Phase 4B)."
    )
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
    contract_type: ContractType | None = Field(
        default=None, description="Set on the contract, or null to use the space's setting."
    )
    activated_at: datetime | None
    cancelled_at: datetime | None
    cancellation_reason: str | None
    terminated_at: datetime | None
    termination_reason: str | None
    group_id: uuid.UUID | None = Field(
        default=None,
        description="Set when this row is one seat of a contract for several seats. "
        "Approving or rejecting one row acts on every pending row of the group.",
    )
    group_reference: str | None = Field(
        default=None, description="The reference the group is shown under (its first row)."
    )
    group_spaces: list[str] = Field(default_factory=list, description="Every seat of the group.")
    agreement_status: DocumentStatus | None = Field(
        default=None,
        description="SIGNED when an agreement document is recorded as signed; otherwise the "
        "latest document's status (superseded ones ignored); null when none was generated.",
    )
    completes_contract: bool = Field(
        default=False,
        description="Whether this contract counts as completed for client access (BR-025): "
        "approved (ACTIVE) and its agreement recorded as signed.",
    )
    created_at: datetime
    updated_at: datetime
    # Phase B2/B3
    category: ContractCategory | None = None
    main_contract_id: uuid.UUID | None = None
    main_contract_reference: str | None = None
    renews_contract_id: uuid.UUID | None = None
    renews_contract_reference: str | None = None
    renewed_by_reference: str | None = Field(
        default=None, description="The renewal made from this contract, if any."
    )
    hold_until: date | None = Field(
        default=None, description="Pending only: the last day the space is held."
    )
    hold_extension_requested_until: date | None = None
    hold_extension_reason: str | None = None
    expected_headcount: int | None = None
    occupants: int | None = None
    government_department: str | None = None
    government_order_reference: str | None = None
    vehicle_registration: str | None = None
    lock_in_period: str | None = None
    notice_period: str | None = None
    payment_due_day: int | None = None
    tds_percent: Decimal | None = None
    booking_id: uuid.UUID | None = None
    missing_for_approval: list[str] = Field(
        default_factory=list,
        description="What a categorised pending contract still needs before approval.",
    )


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
