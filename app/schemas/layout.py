import uuid
from datetime import datetime
from typing import Any, ClassVar

from pydantic import Field

from app.models.enums import LayoutStatus
from app.schemas.common import InputSchema, ReadSchema, UpdateSchema


class LayoutCreate(InputSchema):
    property_id: uuid.UUID
    building_id: uuid.UUID | None = None
    floor_id: uuid.UUID | None = None
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    grid_columns: int | None = Field(default=None, ge=1, le=500)
    grid_rows: int | None = Field(default=None, ge=1, le=500)
    metadata: dict[str, Any] = Field(default_factory=dict)
    status: LayoutStatus = LayoutStatus.ACTIVE


class LayoutUpdate(UpdateSchema):
    required_fields: ClassVar[frozenset[str]] = frozenset({"name", "metadata", "status"})

    building_id: uuid.UUID | None = None
    floor_id: uuid.UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    grid_columns: int | None = Field(default=None, ge=1, le=500)
    grid_rows: int | None = Field(default=None, ge=1, le=500)
    metadata: dict[str, Any] | None = None
    status: LayoutStatus | None = None


class LayoutRead(ReadSchema):
    id: uuid.UUID
    property_id: uuid.UUID
    building_id: uuid.UUID | None
    building_name: str | None
    floor_id: uuid.UUID | None
    floor_name: str | None
    name: str
    description: str | None
    grid_columns: int | None
    grid_rows: int | None
    metadata: dict[str, Any]
    status: LayoutStatus
    created_at: datetime
    updated_at: datetime
