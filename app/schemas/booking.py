import uuid
from datetime import datetime
from typing import Any, ClassVar, Literal, Self

from pydantic import AwareDatetime, EmailStr, Field, model_validator

from app.models.enums import BookingStatus
from app.schemas.common import InputSchema, ReadSchema, UpdateSchema

_START = Field(description="ISO 8601 with a UTC offset, e.g. 2026-11-03T09:00:00+05:30.")
_END = Field(description="Exclusive end. Another booking may start at exactly this time.")


class BookingCreate(InputSchema):
    property_id: uuid.UUID
    space_id: uuid.UUID
    tenant_id: uuid.UUID | None = None
    customer_name: str | None = Field(default=None, min_length=1, max_length=200)
    customer_email: EmailStr | None = None
    customer_phone: str | None = Field(default=None, min_length=5, max_length=32)
    start_at: AwareDatetime = _START
    end_at: AwareDatetime = _END
    status: Literal[BookingStatus.PENDING, BookingStatus.CONFIRMED] = Field(
        default=BookingStatus.PENDING,
        description="PENDING holds the slot until confirmed; CONFIRMED books it outright.",
    )
    notes: str | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at")
        if self.tenant_id is None and self.customer_name is None:
            raise ValueError("either tenant_id or customer_name is required")
        return self


class BookingUpdate(UpdateSchema):
    """Reschedule or correct an open booking. The space cannot be changed."""

    required_fields: ClassVar[frozenset[str]] = frozenset({"start_at", "end_at"})

    tenant_id: uuid.UUID | None = None
    customer_name: str | None = Field(default=None, min_length=1, max_length=200)
    customer_email: EmailStr | None = None
    customer_phone: str | None = Field(default=None, min_length=5, max_length=32)
    start_at: AwareDatetime | None = None
    end_at: AwareDatetime | None = None
    notes: str | None = None


class BookingCancel(InputSchema):
    reason: str | None = Field(default=None, max_length=500)


class BookingRead(ReadSchema):
    id: uuid.UUID
    reference: str
    property_id: uuid.UUID
    property_name: str
    space_id: uuid.UUID
    space_name: str
    tenant_id: uuid.UUID | None
    tenant_name: str | None
    customer_name: str | None
    customer_email: str | None
    customer_phone: str | None
    start_at: datetime
    end_at: datetime
    status: BookingStatus
    notes: str | None
    cancelled_at: datetime | None
    cancellation_reason: str | None
    channel: str = Field(
        default="ADMIN", description="ADMIN for the admin portal, CUSTOMER for the customer app."
    )
    billing_period: str | None = Field(
        default=None, description="The period the customer was quoted for; null for admin bookings."
    )
    quoted_total: float | None = Field(
        default=None, description="Estimate shown to the customer when booking, not a payment."
    )
    quoted_currency: str | None = None
    quote: dict[str, Any] | None = Field(
        default=None, description="The full quote the customer saw (rate, periods, rule)."
    )
    created_at: datetime
    updated_at: datetime


class BookingConflict(ReadSchema):
    kind: Literal["BOOKING", "CONTRACT"] = "BOOKING"
    reference: str
    start_at: datetime
    end_at: datetime | None = Field(description="Null for an open-ended contract.")
    status: str = Field(description="A booking status, or for a contract its contract status.")
    space_id: uuid.UUID | None = None
    # The space the conflicting hold is on: this one, or one containing or inside it.
    space_name: str | None = None
    relation: Literal["same", "contains", "is inside"] | None = Field(
        default=None,
        description="Whether the hold is on this space, a space containing it, or one inside it.",
    )


class AvailabilityRead(ReadSchema):
    available: bool
    message: str
    conflicts: list[BookingConflict] = Field(
        description="Existing bookings that overlap the requested time."
    )
