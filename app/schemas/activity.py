"""The admin activity history (Phase 2e, BR-033): stored activity log entries with readable
labels and what can safely be looked up about them now.

Nothing here changes what was recorded. ``message`` is the stored text, unchanged. Anything
under ``current_context`` and ``record`` describes the records as they are *now*, not as
they were when the entry was written.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field

from app.schemas.common import ReadSchema


class ActivityActor(ReadSchema):
    id: uuid.UUID
    name: str | None
    email: str
    role: str


class CurrentContext(ReadSchema):
    """Where the record sits today. Not historical: it may have moved since."""

    building_id: uuid.UUID | None = None
    building_name: str | None = None
    floor_id: uuid.UUID | None = None
    floor_name: str | None = None
    space_id: uuid.UUID | None = None
    space_name: str | None = None


class ActivityRecord(ReadSchema):
    """Whether the entry's record still exists. ``state`` is UNKNOWN when the entry names
    no record, or a kind of record this view cannot look up."""

    state: Literal["EXISTS", "ARCHIVED", "DELETED", "UNKNOWN"]
    name: str | None = Field(default=None, description="Its current name, if it has one.")


class RecordedChange(ReadSchema):
    """A before/after value written into the stored message itself (Phase 2c reimbursement
    corrections only)."""

    field: str
    before: str
    after: str


class ActivityItem(ReadSchema):
    id: uuid.UUID
    created_at: datetime
    action: str
    action_label: str
    entity_type: str
    entity_label: str
    summary: str = Field(description='Readable label, e.g. "Space moved".')
    entity_id: uuid.UUID | None
    message: str = Field(description="The stored message, exactly as recorded.")
    actor: ActivityActor | None = Field(
        description="Null when no user was recorded (a system step, or the user was removed)."
    )
    property_id: uuid.UUID | None
    property_name: str | None = Field(description="The property's current name.")
    record: ActivityRecord
    current_context: CurrentContext | None
    changes: list[RecordedChange] = Field(
        default_factory=list,
        description="Only when the stored message recorded them in a known format; most "
        "entries did not record field-level changes.",
    )


class ActivityOption(ReadSchema):
    value: str
    label: str


class ActivityOptions(ReadSchema):
    """Filter choices taken from the stored entries."""

    entity_types: list[ActivityOption]
    actions: list[ActivityOption]
    actors: list[ActivityOption]
    properties: list[ActivityOption]
