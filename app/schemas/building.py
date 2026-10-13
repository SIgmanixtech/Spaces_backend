import uuid
from datetime import datetime
from typing import ClassVar

from pydantic import Field

from app.schemas.common import InputSchema, ReadSchema, UpdateSchema


class BuildingCreate(InputSchema):
    property_id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None


class BuildingUpdate(UpdateSchema):
    required_fields: ClassVar[frozenset[str]] = frozenset({"name"})

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None


class BuildingRead(ReadSchema):
    id: uuid.UUID
    property_id: uuid.UUID
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime


class FloorCreate(InputSchema):
    building_id: uuid.UUID
    name: str = Field(min_length=1, max_length=100)
    level_number: int = Field(ge=-20, le=300, description="0 is ground; negatives are basements.")


class FloorUpdate(UpdateSchema):
    required_fields: ClassVar[frozenset[str]] = frozenset({"name", "level_number"})

    name: str | None = Field(default=None, min_length=1, max_length=100)
    level_number: int | None = Field(default=None, ge=-20, le=300)


class FloorRead(ReadSchema):
    id: uuid.UUID
    building_id: uuid.UUID
    name: str
    level_number: int
    created_at: datetime
    updated_at: datetime
