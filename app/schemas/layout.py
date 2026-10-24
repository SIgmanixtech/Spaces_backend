import uuid
from datetime import datetime
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import LayoutStatus
from app.schemas.common import InputSchema, ReadSchema, UpdateSchema

# Furniture and fixtures drawn on a floor plan. They are pictures, not inventory: nothing
# can be booked through them. Bookable things (cabins, desks, seats...) are spaces.
ElementKind = Literal[
    "CHAIR",
    "DESK",
    "TABLE",
    "MEETING_TABLE",
    "SOFA",
    "PLANT",
    "DOOR",
    "WINDOW",
    "WALL",
    "PILLAR",
    "WASHROOM",
    "PANTRY",
    "RECEPTION",
    "LIFT",
    "STAIRS",
    "FIRE_EXIT",
    "PRINTER",
    "STORAGE",
    "LOCKER",
    "WHITEBOARD",
    "SCREEN",
    "PHONE_BOOTH",
    "LABEL",
]
MAX_ELEMENTS = 2000
# Layouts are no longer drafted: a layout is in use (ACTIVE) or put away (ARCHIVED).
# Rows saved as DRAFT before this still read back, and count as in use.
LayoutChoice = Literal[LayoutStatus.ACTIVE, LayoutStatus.ARCHIVED]


class LayoutElement(BaseModel):
    """One drawn item, in grid cells like spaces."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1, max_length=64)
    kind: ElementKind
    x: int = Field(ge=0, le=500)
    y: int = Field(ge=0, le=500)
    width: int = Field(default=1, ge=1, le=500)
    height: int = Field(default=1, ge=1, le=500)
    rotation: Literal[0, 90, 180, 270] = 0
    label: str | None = Field(default=None, max_length=60)


def _checked_metadata(value: dict[str, Any] | None) -> dict[str, Any] | None:
    """Keeps other metadata as is; "elements", if given, must be valid drawn items."""
    if value is None or "elements" not in value:
        return value
    items = value["elements"]
    if not isinstance(items, list):
        raise ValueError("metadata.elements must be a list")
    if len(items) > MAX_ELEMENTS:
        raise ValueError(f"A layout can hold at most {MAX_ELEMENTS} drawn items.")
    elements = [LayoutElement.model_validate(item).model_dump() for item in items]
    if len({e["id"] for e in elements}) != len(elements):
        raise ValueError("Every drawn item needs its own id.")
    return {**value, "elements": elements}


class LayoutCreate(InputSchema):
    property_id: uuid.UUID
    building_id: uuid.UUID | None = None
    floor_id: uuid.UUID | None = None
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    grid_columns: int | None = Field(default=None, ge=1, le=500)
    grid_rows: int | None = Field(default=None, ge=1, le=500)
    metadata: dict[str, Any] = Field(default_factory=dict)
    status: LayoutChoice = LayoutStatus.ACTIVE

    @field_validator("metadata")
    @classmethod
    def _elements(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _checked_metadata(value)


class LayoutUpdate(UpdateSchema):
    required_fields: ClassVar[frozenset[str]] = frozenset({"name", "metadata", "status"})

    building_id: uuid.UUID | None = None
    floor_id: uuid.UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    grid_columns: int | None = Field(default=None, ge=1, le=500)
    grid_rows: int | None = Field(default=None, ge=1, le=500)
    metadata: dict[str, Any] | None = None
    status: LayoutChoice | None = None

    @field_validator("metadata")
    @classmethod
    def _elements(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _checked_metadata(value)


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
