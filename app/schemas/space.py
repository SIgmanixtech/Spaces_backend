import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar, Self

from pydantic import Field, model_validator

from app.models.enums import PriceUnit, SpaceOperationalStatus, SpaceStatus, SpaceType
from app.schemas.common import InputSchema, ReadSchema, UpdateSchema

_Price = Field(default=None, ge=0, max_digits=12, decimal_places=2)
_Area = Field(default=None, gt=0, max_digits=12, decimal_places=2)


class SpaceCreate(InputSchema):
    property_id: uuid.UUID
    floor_id: uuid.UUID | None = Field(
        default=None,
        description="The floor the space is on. Omit, with parent_space_id, for an "
        "unassigned space. Required for space_type FLOOR.",
    )
    parent_space_id: uuid.UUID | None = Field(
        default=None,
        description="The containing space. Defaults to the floor's whole-floor (FLOOR) space.",
    )
    layout_id: uuid.UUID | None = None
    name: str = Field(min_length=1, max_length=200)
    code: str | None = Field(default=None, min_length=1, max_length=50)
    space_type: SpaceType
    capacity: int | None = Field(default=None, ge=1)
    area_sqft: Decimal | None = _Area
    is_rentable: bool | None = Field(
        default=None, description="Defaults to true, or false for space_type NON_RENTABLE."
    )
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
    """The floor and parent are not changed here: use the move action."""

    required_fields: ClassVar[frozenset[str]] = frozenset(
        {"name", "space_type", "currency", "operational_status", "attributes", "is_rentable"}
    )

    layout_id: uuid.UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=200)
    code: str | None = Field(default=None, min_length=1, max_length=50)
    space_type: SpaceType | None = None
    capacity: int | None = Field(default=None, ge=1)
    area_sqft: Decimal | None = _Area
    is_rentable: bool | None = None
    price_amount: Decimal | None = _Price
    price_unit: PriceUnit | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3, pattern="^[A-Z]{3}$")
    operational_status: SpaceOperationalStatus | None = None
    x: int | None = Field(default=None, ge=0)
    y: int | None = Field(default=None, ge=0)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    attributes: dict[str, Any] | None = None


class SpaceMove(InputSchema):
    """Assign an unassigned space to a floor, or move a space (and everything in it)."""

    floor_id: uuid.UUID | None = Field(
        default=None, description="Target floor. The space goes under its whole-floor space."
    )
    parent_space_id: uuid.UUID | None = Field(
        default=None, description="Target containing space. Its floor becomes the space's floor."
    )

    @model_validator(mode="after")
    def _needs_a_target(self) -> Self:
        if self.floor_id is None and self.parent_space_id is None:
            raise ValueError("floor_id or parent_space_id is required")
        return self


class SpaceRead(ReadSchema):
    id: uuid.UUID
    property_id: uuid.UUID
    building_id: uuid.UUID | None
    floor_id: uuid.UUID | None = Field(description="Null for a space not yet assigned to a floor.")
    parent_space_id: uuid.UUID | None
    depth: int = Field(description="Levels below the floor's whole-floor space (which is 0).")
    layout_id: uuid.UUID | None
    name: str
    code: str | None
    space_type: SpaceType
    capacity: int | None
    area_sqft: float | None
    is_rentable: bool
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
