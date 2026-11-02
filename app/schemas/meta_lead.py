"""Meta Ads lead intake (Phase 3B-2, polling)."""

import uuid
from datetime import datetime
from typing import ClassVar, Literal

from pydantic import Field

from app.models.enums import MetaLeadStatus
from app.schemas.common import InputSchema, ReadSchema, UpdateSchema
from app.schemas.lead_import import ImportField


class MetaStatus(ReadSchema):
    """Whether the server can reach Meta. Never carries the token or secret."""

    configured: bool = Field(description="Graph API version, page token and app secret are set.")
    missing_settings: list[str] = Field(description="Names of unset settings, never values.")
    forms: int
    enabled_forms: int
    last_synced_at: datetime | None
    received: int
    imported: int
    failed: int


class MetaFormCreate(InputSchema):
    form_id: str = Field(min_length=1, max_length=64, pattern=r"^[0-9]+$")
    name: str = Field(min_length=1, max_length=200)


class MetaFormUpdate(UpdateSchema):
    required_fields: ClassVar[frozenset[str]] = frozenset({"name", "enabled", "field_mapping"})

    name: str | None = Field(default=None, min_length=1, max_length=200)
    enabled: bool | None = None
    field_mapping: dict[str, str | None] | None = Field(
        default=None, description="Meta question name -> lead field key (null: not mapped)."
    )


class MetaFormRead(ReadSchema):
    id: uuid.UUID
    form_id: str
    name: str
    enabled: bool
    field_mapping: dict[str, str]
    seen_fields: list[str] = Field(description="Question names seen in leads received so far.")
    last_synced_at: datetime | None
    last_lead_time: datetime | None
    last_sync_error: str | None
    received: int
    imported: int
    failed: int
    created_at: datetime
    updated_at: datetime


class MetaFieldOptions(ReadSchema):
    fields: list[ImportField] = Field(description="Lead fields a Meta question can fill.")
    suggested: dict[str, str] = Field(description="Meta's standard questions and their fields.")


class MetaAnswer(ReadSchema):
    name: str
    value: str
    field: str | None = Field(description="The lead field it filled; null: kept in the note.")


class MetaDuplicate(ReadSchema):
    kind: Literal["LEAD", "TENANT"]
    id: uuid.UUID
    reference: str | None
    name: str
    matched_on: list[str]


class MetaEventRead(ReadSchema):
    id: uuid.UUID
    leadgen_id: str
    form_ref: uuid.UUID
    form_name: str
    ad_id: str | None
    adset_id: str | None
    campaign_id: str | None
    meta_created_time: datetime | None
    status: MetaLeadStatus
    attempts: int
    last_error: str | None
    duplicates: list[MetaDuplicate]
    lead_id: uuid.UUID | None
    lead_reference: str | None
    answers: list[MetaAnswer]
    processed_at: datetime | None
    created_at: datetime


class MetaSyncFormResult(ReadSchema):
    form_id: str
    name: str
    received: int
    already_known: int
    imported: int
    failed: int
    error: str | None


class MetaSyncResult(ReadSchema):
    forms: list[MetaSyncFormResult]
    received: int
    imported: int
    failed: int
