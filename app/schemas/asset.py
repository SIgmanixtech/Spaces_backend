import uuid
from datetime import date, datetime
from typing import ClassVar

from pydantic import Field

from app.models.enums import AssetStatus
from app.schemas.common import InputSchema, ReadSchema, UpdateSchema


class AssetCreate(InputSchema):
    name: str = Field(min_length=1, max_length=200)
    category: str = Field(min_length=1, max_length=80)
    asset_tag: str | None = Field(default=None, min_length=1, max_length=80)
    property_id: uuid.UUID
    building_id: uuid.UUID | None = None
    floor_id: uuid.UUID | None = None
    space_id: uuid.UUID | None = None
    location: str | None = Field(default=None, max_length=200)
    status: AssetStatus = AssetStatus.OPERATIONAL
    purchase_date: date | None = None
    notes: str | None = None
    maintenance_notes: str | None = None


class AssetUpdate(UpdateSchema):
    required_fields: ClassVar[frozenset[str]] = frozenset({"name", "category", "status"})

    name: str | None = Field(default=None, min_length=1, max_length=200)
    category: str | None = Field(default=None, min_length=1, max_length=80)
    asset_tag: str | None = Field(default=None, min_length=1, max_length=80)
    building_id: uuid.UUID | None = None
    floor_id: uuid.UUID | None = None
    space_id: uuid.UUID | None = None
    location: str | None = Field(default=None, max_length=200)
    status: AssetStatus | None = None
    purchase_date: date | None = None
    notes: str | None = None
    maintenance_notes: str | None = None


class AssetRead(ReadSchema):
    id: uuid.UUID
    name: str
    category: str
    asset_tag: str | None
    property_id: uuid.UUID
    property_name: str
    building_id: uuid.UUID | None
    floor_id: uuid.UUID | None
    space_id: uuid.UUID | None
    location: str | None
    status: AssetStatus
    purchase_date: date | None
    notes: str | None
    maintenance_notes: str | None
    created_at: datetime
    updated_at: datetime
