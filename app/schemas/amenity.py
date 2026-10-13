import uuid
from datetime import datetime, time
from typing import ClassVar

from pydantic import Field

from app.models.enums import AmenityStatus
from app.schemas.common import InputSchema, ReadSchema, UpdateSchema


class AmenityCreate(InputSchema):
    property_id: uuid.UUID
    name: str = Field(
        min_length=1,
        max_length=120,
        description="Matched case-insensitively to a shared amenity definition; "
        "a new definition is created when none exists.",
    )
    description: str | None = Field(
        default=None, description="Stored on the shared definition, so it applies everywhere."
    )
    opening_time: time | None = None
    closing_time: time | None = None
    status: AmenityStatus = AmenityStatus.ACTIVE
    notes: str | None = None


class AmenityUpdate(UpdateSchema):
    required_fields: ClassVar[frozenset[str]] = frozenset({"name", "status"})

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    opening_time: time | None = None
    closing_time: time | None = None
    status: AmenityStatus | None = None
    notes: str | None = None


class AmenityRead(ReadSchema):
    id: uuid.UUID = Field(description="Id of this property's assignment of the amenity.")
    amenity_id: uuid.UUID = Field(description="Id of the shared amenity definition.")
    name: str
    description: str | None
    property_id: uuid.UUID
    property_name: str
    opening_time: time | None
    closing_time: time | None
    status: AmenityStatus
    notes: str | None
    created_at: datetime
    updated_at: datetime
