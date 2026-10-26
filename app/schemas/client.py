"""What the customer (client) application reads and sends.

Public schemas carry only what a published listing shows: no internal notes,
no other customers' names or references, no storage paths, no audit fields.
Customer schemas describe the signed-in customer's own records.
"""

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import EmailStr, Field, SecretStr, model_validator

from app.models.enums import (
    BillingPeriod,
    BookingStatus,
    DepositRule,
    FileKind,
    MaintenanceBasis,
    PaymentFrequency,
    RateBasis,
    SpaceType,
    TaxTreatment,
)
from app.schemas.auth import SessionRead
from app.schemas.common import InputSchema, ReadSchema, UpdateSchema
from app.schemas.layout import LayoutElement
from app.schemas.pricing import AdditionalChargeRead, EstimateRead

# --- public catalogue -----------------------------------------------------------------


class PublicImage(ReadSchema):
    id: uuid.UUID
    url: str = Field(description="Signed link; expires at url_expires_at.")
    url_expires_at: datetime
    kind: FileKind
    content_type: str
    caption: str | None
    description: str | None
    category: str | None
    width_px: int | None
    height_px: int | None
    is_primary: bool
    building_id: uuid.UUID | None
    layout_id: uuid.UUID | None
    space_id: uuid.UUID | None


class StartingPrice(ReadSchema):
    """The lowest one-period recurring total of any space, for one billing period."""

    billing_period: BillingPeriod
    amount: float
    currency: str
    space_type: SpaceType


class PublicPropertyCard(ReadSchema):
    id: uuid.UUID
    name: str
    code: str | None
    property_type: str
    category: str
    city: str
    state: str
    locality: str | None = Field(description="Not stored separately yet; always null.")
    summary: str | None = Field(description="The description, shortened to 240 characters.")
    amenities: list[str]
    cover: PublicImage | None
    space_types: list[SpaceType] = Field(description="Types of rentable space offered.")
    max_capacity: int | None
    starting_prices: list[StartingPrice]
    offer: Literal["BOOKABLE", "ENQUIRY"] = Field(
        description="BOOKABLE: open, with at least one rentable space priced for an online "
        "booking period. ENQUIRY: listed for enquiries only."
    )
    bookable_spaces: int


class PublicSpace(ReadSchema):
    id: uuid.UUID
    name: str
    code: str | None
    space_type: SpaceType
    parent_space_id: uuid.UUID | None
    depth: int
    layout_id: uuid.UUID | None
    area_sqft: float | None
    capacity: int | None
    is_rentable: bool
    bookable: bool = Field(description="Rentable, open, and priced for at least one period.")
    x: int | None
    y: int | None
    width: int | None
    height: int | None


class PublicFloor(ReadSchema):
    id: uuid.UUID
    name: str
    level_number: int
    spaces: int = Field(default=0, description="Portions and spaces on the floor.")
    rentable_spaces: int = 0
    bookable_spaces: int = 0
    space_types: list[SpaceType] = Field(default_factory=list)


class PublicBuilding(ReadSchema):
    id: uuid.UUID
    name: str
    description: str | None
    floors: list[PublicFloor]


class PublicPropertyDetail(PublicPropertyCard):
    description: str | None
    address: str
    postal_code: str
    latitude: float | None
    longitude: float | None
    area_sqft: float | None
    bookable: bool = Field(description="Whether the property is open for bookings now.")
    buildings: list[PublicBuilding]
    images: list[PublicImage] = Field(
        description="Property and building pictures. Floor plans and space photos come with "
        "each floor."
    )


class PublicLayout(ReadSchema):
    id: uuid.UUID
    name: str
    grid_columns: int | None
    grid_rows: int | None
    plans: list[PublicImage] = Field(description="Floor-plan drawings of this layout.")
    elements: list[LayoutElement] = Field(
        default_factory=list,
        description="Furniture and fixtures drawn on the plan (chairs, doors...). Not bookable.",
    )


class PublicRule(ReadSchema):
    """A price as a customer sees it: the terms, without audit fields."""

    rate_basis: RateBasis
    billing_period: BillingPeriod
    custom_period_label: str | None
    amount: float
    currency: str
    effective_to: date | None
    maintenance_amount: float | None
    maintenance_basis: MaintenanceBasis | None
    tax_treatment: TaxTreatment
    tax_rate: float | None
    additional_charges: list[AdditionalChargeRead]
    deposit_rule: DepositRule
    deposit_value: float | None
    payment_frequency: PaymentFrequency
    notes: str | None = Field(description="Commercial terms entered with the price.")
    source: str = Field(
        description="CONFIGURED or OVERRIDE (entered), or DERIVED (calculated and approved)."
    )


class PublicPrice(ReadSchema):
    billing_period: BillingPeriod
    source: Literal["OWN", "INHERITED", "BUILDING_DEFAULT", "PROPERTY_DEFAULT"]
    rule: PublicRule
    estimate: EstimateRead | None


class PublicFloorPlan(ReadSchema):
    property_id: uuid.UUID
    building_id: uuid.UUID
    building_name: str
    floor: PublicFloor
    layouts: list[PublicLayout]
    spaces: list[PublicSpace]
    prices: dict[uuid.UUID, list[PublicPrice]] = Field(description="By space id.")
    images: dict[uuid.UUID, list[PublicImage]] = Field(description="Space photographs, by id.")


SpaceAvailabilityStatus = Literal["AVAILABLE", "BOOKED", "RESERVED", "OCCUPIED", "UNAVAILABLE"]


class SpaceAvailability(ReadSchema):
    space_id: uuid.UUID
    status: SpaceAvailabilityStatus
    available: bool
    message: str = Field(
        description="Why it is not available. Never names other customers or references."
    )


class FloorAvailability(ReadSchema):
    start_at: datetime
    end_at: datetime
    spaces: list[SpaceAvailability]


class QuoteRequest(InputSchema):
    space_id: uuid.UUID
    billing_period: BillingPeriod
    start_at: datetime
    end_at: datetime


class QuoteRead(ReadSchema):
    space_id: uuid.UUID
    billing_period: BillingPeriod
    quantity: float = Field(description="Billing periods the requested time covers.")
    price: PublicPrice
    estimate: EstimateRead
    availability: SpaceAvailability


# --- customer accounts ----------------------------------------------------------------


class CustomerRegister(InputSchema):
    email: EmailStr
    password: SecretStr = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=32)
    company_name: str | None = Field(default=None, max_length=200)


class RegistrationResult(ReadSchema):
    status: Literal["SIGNED_IN", "CONFIRMATION_REQUIRED"]
    message: str
    session: SessionRead | None


class PasswordRecovery(InputSchema):
    email: EmailStr


class OtpRequest(InputSchema):
    email: EmailStr


class OtpVerify(InputSchema):
    email: EmailStr
    code: str = Field(min_length=6, max_length=10, pattern=r"^[0-9]+$")
    full_name: str | None = Field(
        default=None, min_length=1, max_length=200, description="Used for a first sign-in only."
    )


class ContractFromBooking(InputSchema):
    """Turn a reservation into a contract request for the same space."""

    start_date: date
    months: int = Field(ge=1, le=120)
    notes: str | None = Field(default=None, max_length=1000)


class CustomerProfile(ReadSchema):
    id: uuid.UUID
    email: str
    full_name: str | None
    phone: str | None
    company_name: str | None


class CustomerProfileUpdate(UpdateSchema):
    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=32)
    company_name: str | None = Field(default=None, max_length=200)


# --- customer bookings and contracts -----------------------------------------------------


class CustomerBookingCreate(InputSchema):
    """No tenant, customer or property id: the backend takes those from the signed-in
    account and the space."""

    space_id: uuid.UUID
    billing_period: BillingPeriod
    start_at: datetime
    end_at: datetime
    notes: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _ordered(self) -> "CustomerBookingCreate":
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at")
        return self


class CustomerBookingReschedule(InputSchema):
    """A new time (and possibly a new billing period) for the same space."""

    billing_period: BillingPeriod
    start_at: datetime
    end_at: datetime

    @model_validator(mode="after")
    def _ordered(self) -> "CustomerBookingReschedule":
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at")
        return self


class CustomerBookingCancel(InputSchema):
    reason: str | None = Field(default=None, max_length=500)


class CustomerBooking(ReadSchema):
    id: uuid.UUID
    reference: str
    property_id: uuid.UUID
    property_name: str
    space_id: uuid.UUID
    space_name: str
    space_type: SpaceType
    start_at: datetime
    end_at: datetime
    status: BookingStatus
    billing_period: BillingPeriod | None
    quoted_total: float | None
    quoted_currency: str | None
    quote: dict[str, object] | None
    notes: str | None
    cancelled_at: datetime | None
    cancellation_reason: str | None
    can_cancel: bool
    created_at: datetime


class CustomerContract(ReadSchema):
    id: uuid.UUID
    reference: str
    property_id: uuid.UUID
    property_name: str
    space_name: str | None
    space_type: str | None
    scope: str = Field(description='"Whole property", "Whole floor", "Portion 5A"...')
    start_date: date
    end_date: date | None
    status: str
    monthly_rent: float | None
    security_deposit: float | None
    currency: str
    terms: str | None


class Capabilities(ReadSchema):
    """What this backend offers the client app, so it never promises the rest."""

    customer_registration: bool
    password_recovery: bool
    bookings: bool
    contracts: bool
    invoices: bool
    online_payments: bool
    qr_access: bool


class LeaseDue(ReadSchema):
    contract: CustomerContract
    next_due_date: date | None = Field(
        description="The next monthly rent date on or after today, within the lease."
    )


class CustomerActivity(ReadSchema):
    at: datetime
    kind: Literal["BOOKING", "CONTRACT"]
    reference: str
    action: str
    message: str
    by_property_team: bool = Field(description="False when the customer did it themselves.")
    booking_id: uuid.UUID | None = None


class CustomerDashboard(ReadSchema):
    """Everything on the customer's home page, in one call, straight from the records."""

    upcoming: list[CustomerBooking] = Field(description="Bookings not yet ended, soonest first.")
    reserved: int = Field(description="Upcoming bookings awaiting confirmation (PENDING).")
    contract_requests: int = Field(
        default=0, description="Contract requests waiting for the property team."
    )
    booked: int = Field(description="Upcoming confirmed bookings.")
    active_leases: list[LeaseDue]
    monthly_rent: dict[str, float] = Field(
        description="Total monthly rent of active leases, by currency."
    )
    activity: list[CustomerActivity] = Field(description="Newest first.")


class FreeSpacePrice(ReadSchema):
    billing_period: BillingPeriod
    amount: float
    currency: str


class FreeSpace(ReadSchema):
    id: uuid.UUID
    name: str
    space_type: SpaceType
    capacity: int | None
    area_sqft: float | None
    inside: str | None = Field(description="The portion or area it sits in, if not the floor.")
    bookable: bool = Field(description="Can be booked online now (priced for a period).")
    from_price: FreeSpacePrice | None = Field(description="Its lowest one-period total.")


class FreeFloor(ReadSchema):
    property_id: uuid.UUID
    property_name: str
    city: str
    building_id: uuid.UUID
    building_name: str
    floor_id: uuid.UUID
    floor_name: str
    level_number: int
    spaces: list[FreeSpace]


class FreeSpaces(ReadSchema):
    """Only what is free for the time asked: nothing booked, reserved or leased is listed."""

    start_at: datetime
    end_at: datetime
    floors: list[FreeFloor]
    total: int
