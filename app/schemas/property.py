import uuid
from datetime import datetime
from typing import ClassVar

from pydantic import Field

from app.models.enums import OccupancyStatus, PropertyStatus
from app.schemas.common import InputSchema, ReadSchema, UpdateSchema


class PropertyCreate(InputSchema):
    name: str = Field(min_length=1, max_length=200)
    property_type: str = Field(min_length=1, max_length=50)
    category: str = Field(min_length=1, max_length=50)
    description: str | None = None
    address: str = Field(min_length=1, max_length=500)
    city: str = Field(min_length=1, max_length=120)
    state: str = Field(min_length=1, max_length=120)
    postal_code: str = Field(min_length=1, max_length=20)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    building_info: str | None = None
    floor_count: int | None = Field(default=None, ge=0)
    capacity: int | None = Field(default=None, ge=0)
    area_sqft: float | None = Field(default=None, ge=0)
    availability_notes: str | None = None
    status: PropertyStatus = PropertyStatus.ACTIVE


class PropertyUpdate(UpdateSchema):
    required_fields: ClassVar[frozenset[str]] = frozenset(
        {"name", "property_type", "category", "address", "city", "state", "postal_code", "status"}
    )

    name: str | None = Field(default=None, min_length=1, max_length=200)
    property_type: str | None = Field(default=None, min_length=1, max_length=50)
    category: str | None = Field(default=None, min_length=1, max_length=50)
    description: str | None = None
    address: str | None = Field(default=None, min_length=1, max_length=500)
    city: str | None = Field(default=None, min_length=1, max_length=120)
    state: str | None = Field(default=None, min_length=1, max_length=120)
    postal_code: str | None = Field(default=None, min_length=1, max_length=20)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    building_info: str | None = None
    floor_count: int | None = Field(default=None, ge=0)
    capacity: int | None = Field(default=None, ge=0)
    area_sqft: float | None = Field(default=None, ge=0)
    availability_notes: str | None = None
    status: PropertyStatus | None = None


class PropertyRead(ReadSchema):
    id: uuid.UUID
    name: str
    property_type: str
    category: str
    description: str | None
    address: str
    city: str
    state: str
    postal_code: str
    latitude: float | None
    longitude: float | None
    building_info: str | None
    floor_count: int | None
    capacity: int | None
    area_sqft: float | None
    availability_notes: str | None
    status: PropertyStatus = Field(description="Operational status, set by an admin.")
    occupancy_status: OccupancyStatus = Field(
        description="Derived from current tenant occupancies and open bookings. Read-only."
    )
    occupancy_rate: float | None = Field(
        description="Occupied spaces divided by all spaces (0-1). Null when no spaces exist."
    )
    tenant_name: str | None = Field(
        description="The current tenant when exactly one tenant occupies the property."
    )
    amenities: list[str] = Field(description="Names of active amenities at this property.")
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime
