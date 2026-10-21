import uuid
from datetime import date, datetime
from typing import ClassVar, Self

from pydantic import EmailStr, Field, model_validator

from app.models.enums import OccupancyStatus, TenantStatus
from app.schemas.common import InputSchema, ReadSchema, UpdateSchema


class TenantCreate(InputSchema):
    name: str = Field(min_length=1, max_length=200)
    company_name: str | None = Field(default=None, max_length=200)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, min_length=5, max_length=32)
    status: TenantStatus = TenantStatus.ACTIVE
    notes: str | None = None


class TenantUpdate(UpdateSchema):
    required_fields: ClassVar[frozenset[str]] = frozenset({"name", "status"})

    name: str | None = Field(default=None, min_length=1, max_length=200)
    company_name: str | None = Field(default=None, max_length=200)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, min_length=5, max_length=32)
    status: TenantStatus | None = None
    notes: str | None = None


class TenantRead(ReadSchema):
    id: uuid.UUID
    name: str
    company_name: str | None
    email: str | None
    phone: str | None
    status: TenantStatus
    notes: str | None
    property_id: uuid.UUID | None = Field(description="Property of the current occupancy.")
    property_name: str | None
    space_id: uuid.UUID | None = Field(description="Space of the current occupancy, if any.")
    space_name: str | None
    occupancy_status: OccupancyStatus = Field(
        description="OCCUPIED with a current occupancy; RESERVED with an upcoming occupancy "
        "or open booking; otherwise VACANT. Read-only."
    )
    booking_refs: list[str] = Field(description="References of the tenant's bookings.")
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


class OccupancyCreate(InputSchema):
    property_id: uuid.UUID
    space_id: uuid.UUID | None = Field(
        default=None, description="Omit when the tenant occupies the whole property."
    )
    start_date: date
    end_date: date | None = Field(
        default=None, description="Last day of the occupancy, inclusive. Null means ongoing."
    )
    notes: str | None = None

    @model_validator(mode="after")
    def _dates_ordered(self) -> Self:
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("end_date cannot be before start_date")
        return self


class OccupancyUpdate(UpdateSchema):
    required_fields: ClassVar[frozenset[str]] = frozenset({"start_date"})

    start_date: date | None = None
    end_date: date | None = None
    notes: str | None = None


class OccupancyRead(ReadSchema):
    id: uuid.UUID
    reference: str | None = Field(default=None, description="The contract reference.")
    status: str | None = Field(
        default=None, description="Contract status; see GET /contracts/{id} for the full contract."
    )
    tenant_id: uuid.UUID
    property_id: uuid.UUID
    property_name: str
    space_id: uuid.UUID | None
    space_name: str | None
    start_date: date
    end_date: date | None
    notes: str | None
    created_at: datetime
    updated_at: datetime
