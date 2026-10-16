import uuid
from datetime import datetime
from typing import ClassVar

from pydantic import Field

from app.models.enums import FileKind
from app.schemas.common import ReadSchema, UpdateSchema


class FileRead(ReadSchema):
    id: uuid.UUID
    property_id: uuid.UUID
    building_id: uuid.UUID | None
    layout_id: uuid.UUID | None
    space_id: uuid.UUID | None
    asset_id: uuid.UUID | None = None
    kind: FileKind
    original_name: str
    content_type: str
    size_bytes: int
    width_px: int | None = Field(default=None, description="Read from the image; null for a PDF.")
    height_px: int | None = None
    caption: str | None = None
    description: str | None = None
    category: str | None = None
    sort_order: int = Field(default=0, description="Lists are ordered by this, then by upload.")
    is_primary: bool = Field(
        default=False,
        description="The owner's cover image, or a layout's main plan. One per owner.",
    )
    created_at: datetime
    url: str = Field(description="Short-lived signed link to the file. Do not store it.")
    url_expires_at: datetime


class FileUpdate(UpdateSchema):
    """Display details only. The bytes, owner and kind of a file never change:
    upload a replacement and delete the old file instead."""

    required_fields: ClassVar[frozenset[str]] = frozenset({"sort_order", "is_primary"})

    caption: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=2000)
    category: str | None = Field(default=None, max_length=80)
    sort_order: int | None = Field(default=None, ge=0, le=100_000)
    is_primary: bool | None = Field(
        default=None,
        description="true makes this the owner's primary file and clears the previous one.",
    )
