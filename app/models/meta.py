"""Meta Ads lead intake (Phase 3B-2, polling; BR-008).

A MetaLeadForm is one Meta lead form the property team imports from, with the field
mapping an admin set for it (Meta question name -> lead field). Each lead fetched from
Meta is stored once as a MetaLeadEvent, keyed by Meta's own lead id, so fetching it again
never creates a second CRM lead. The event keeps Meta's answers so a failed one can be
reprocessed after the mapping is corrected.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import MetaLeadStatus
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin, enum_check


class MetaLeadForm(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "meta_lead_forms"

    form_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Only enabled forms are fetched by the sync.
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # Meta question name -> lead field key (the Excel import's field keys, owner excluded).
    field_mapping: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Newest Meta "created_time" seen; the next sync asks only for later leads.
    last_lead_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )


class MetaLeadEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "meta_lead_events"
    __table_args__ = (
        enum_check("status", MetaLeadStatus),
        CheckConstraint("(status = 'IMPORTED') = (lead_id IS NOT NULL)", name="imported_has_lead"),
        CheckConstraint("attempts >= 0", name="attempts_not_negative"),
    )

    # Meta's lead id: unique, so the same lead is stored (and imported) once.
    leadgen_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    form_ref: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meta_lead_forms.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    ad_id: Mapped[str | None] = mapped_column(String(64))
    adset_id: Mapped[str | None] = mapped_column(String(64))
    campaign_id: Mapped[str | None] = mapped_column(String(64))
    meta_created_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The lead as Meta returned it (answers included), kept for reprocessing.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=MetaLeadStatus.RECEIVED.value, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)
    # Possible duplicates found when it was imported (a warning; nothing is merged).
    duplicates: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("leads.id", ondelete="RESTRICT"), unique=True
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
