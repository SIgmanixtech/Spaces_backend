import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar

from pydantic import Field

from app.models.enums import PriceUnit, SpaceOperationalStatus, SpaceStatus, SpaceType
from app.schemas.common import InputSchema, ReadSchema, UpdateSchema

_Price = Field(default=None, ge=0, max_digits=12, decimal_places=2)


class SpaceCreate(InputSchema):
    property_id: uuid.UUID
    layout_id: uuid.UUID | None = None
    name: str = Field(min_length=1, max_length=200)
    code: str | None = Field(default=None, min_length=1, max_length=50)
    space_type: SpaceType
    capacity: int | None = Field(default=None, ge=1)
    price_amount: Decimal | None = _Price
    price_unit: PriceUnit | None = None
    currency: str = Field(default="INR", min_length=3, max_length=3, pattern="^[A-Z]{3}$")
    operational_status: SpaceOperationalStatus = SpaceOperationalStatus.ACTIVE
    x: int | None = Field(default=None, ge=0)
    y: int | None = Field(default=None, ge=0)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    attributes: dict[str, Any] = Field(default_factory=dict)


class SpaceUpdate(UpdateSchema):
    required_fields: ClassVar[frozenset[str]] = frozenset(
        {"name", "space_type", "currency", "operational_status", "attributes"}
    )

    layout_id: uuid.UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=200)
    code: str | None = Field(default=None, min_length=1, max_length=50)
    space_type: SpaceType | None = None
    capacity: int | None = Field(default=None, ge=1)
    price_amount: Decimal | None = _Price
    price_unit: PriceUnit | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3, pattern="^[A-Z]{3}$")
    operational_status: SpaceOperationalStatus | None = None
    x: int | None = Field(default=None, ge=0)
    y: int | None = Field(default=None, ge=0)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    attributes: dict[str, Any] | None = None


class SpaceRead(ReadSchema):
    id: uuid.UUID
    property_id: uuid.UUID
    layout_id: uuid.UUID | None
    name: str
    code: str | None
    space_type: SpaceType
    capacity: int | None
    price_amount: float | None
    price_unit: PriceUnit | None
    currency: str
    operational_status: SpaceOperationalStatus = Field(
        description="Whether the space can be used at all, set by an admin."
    )
    status: SpaceStatus = Field(
        description="Derived right now from occupancies and bookings. Read-only."
    )
    x: int | None
    y: int | None
    width: int | None
    height: int | None
    attributes: dict[str, Any]
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime
