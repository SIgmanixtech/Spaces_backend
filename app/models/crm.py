"""CRM core (Phase 3A): leads, their interests, notes and follow-up tasks (BR-006, BR-008).
Phase 4A: proposals made to a lead, and the lead's link to a tenant record.

A lead is a prospective tenant. Phase 4A links a lead to a tenant record (an existing one,
or one created from the lead); that gives no client-app access and creates no contract or
account. A lead with at least one proposal that is not withdrawn is a prospect (derived,
not a stage).
Duplicates are only warned about, so the normalised match keys below are indexed but not
unique. Stages stored here are the three an administrator sets by hand; the later ones are
read from contract and activation records when those phases exist.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    Sequence,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import (
    BillingPeriod,
    LeadImportFileType,
    LeadSource,
    LeadStage,
    ProposalStatus,
    SpaceType,
)
from app.models.mixins import CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin, enum_check

lead_reference_seq = Sequence("lead_reference_seq", metadata=Base.metadata)
proposal_reference_seq = Sequence("proposal_reference_seq", metadata=Base.metadata)


class Lead(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "leads"
    __table_args__ = (
        enum_check("source", LeadSource),
        enum_check("stage", LeadStage),
        enum_check("space_type", SpaceType),
        enum_check("budget_period", BillingPeriod),
        CheckConstraint("company_name IS NOT NULL OR contact_name IS NOT NULL", name="has_name"),
        CheckConstraint("capacity IS NULL OR capacity > 0", name="capacity_positive"),
        CheckConstraint("area_sqft IS NULL OR area_sqft > 0", name="area_positive"),
        CheckConstraint(
            "(budget_amount IS NULL) = (budget_period IS NULL)"
            " AND (budget_amount IS NULL OR budget_amount >= 0)",
            name="budget",
        ),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_code"),
    )

    reference: Mapped[str] = mapped_column(
        String(20),
        unique=True,
        nullable=False,
        server_default=text(
            "('LD-' || to_char(now(), 'YYYY') || '-' "
            "|| lpad(nextval('lead_reference_seq')::text, 6, '0'))"
        ),
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    stage: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=LeadStage.LEAD_CREATED.value, index=True
    )
    stage_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    company_name: Mapped[str | None] = mapped_column(String(200))
    contact_name: Mapped[str | None] = mapped_column(String(200))
    contact_email: Mapped[str | None] = mapped_column(String(320))
    contact_phone: Mapped[str | None] = mapped_column(String(32))
    # Normalised forms for duplicate warnings (BR-008); set by the service.
    email_key: Mapped[str | None] = mapped_column(String(320), index=True)
    phone_key: Mapped[str | None] = mapped_column(String(20), index=True)
    company_key: Mapped[str | None] = mapped_column(String(200), index=True)
    # Requirements.
    property_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("properties.id", ondelete="RESTRICT"), index=True
    )
    space_type: Mapped[str | None] = mapped_column(String(32))
    capacity: Mapped[int | None] = mapped_column(Integer)
    area_sqft: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    budget_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    budget_period: Mapped[str | None] = mapped_column(String(16))
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="INR")
    desired_start_date: Mapped[date | None] = mapped_column(Date)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL"), index=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )
    # Set when the lead came from a spreadsheet import (Phase 3B-1); never changed after.
    import_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("lead_import_batches.id", ondelete="RESTRICT"), index=True
    )
    # The tenant record this lead belongs to (Phase 4A). Several leads may share a tenant;
    # a lead has at most one. A linked tenant cannot be deleted (it is archived instead).
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), index=True
    )
    tenant_linked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tenant_linked_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )


class LeadInterest(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """A property, or one space of it, the lead is interested in. Holds no inventory."""

    __tablename__ = "lead_interests"
    __table_args__ = (
        # The space, when given, must belong to the same property.
        ForeignKeyConstraint(
            ["space_id", "property_id"], ["spaces.id", "spaces.property_id"], ondelete="RESTRICT"
        ),
        Index(
            "uq_lead_interests_property",
            "lead_id",
            "property_id",
            unique=True,
            postgresql_where=text("space_id IS NULL"),
        ),
        Index(
            "uq_lead_interests_space",
            "lead_id",
            "space_id",
            unique=True,
            postgresql_where=text("space_id IS NOT NULL"),
        ),
    )

    lead_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("leads.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    property_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("properties.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    space_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    note: Mapped[str | None] = mapped_column(String(500))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )


class LeadNote(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "lead_notes"

    lead_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("leads.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )


class LeadTask(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A follow-up. Due and overdue are worked out from due_date and the business date."""

    __tablename__ = "lead_tasks"
    __table_args__ = (
        CheckConstraint("done = (completed_at IS NOT NULL)", name="completed_when_done"),
        Index("ix_lead_tasks_done_due", "done", "due_date"),
    )

    lead_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("leads.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    details: Mapped[str | None] = mapped_column(Text)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL"), index=True
    )
    done: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )


class LeadImportBatch(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One confirmed spreadsheet import (Phase 3B-1): who ran it, which file (name and
    SHA-256 fingerprint only; the file itself is not kept), the column mapping used, the
    counts and each row's outcome. Previews are not recorded. Imports are never undone."""

    __tablename__ = "lead_import_batches"
    __table_args__ = (
        enum_check("file_type", LeadImportFileType),
        CheckConstraint(
            "total_rows >= 0 AND imported_count >= 0 AND skipped_count >= 0"
            " AND error_count >= 0 AND duplicate_count >= 0",
            name="counts_not_negative",
        ),
        CheckConstraint(
            "imported_count + skipped_count + error_count = total_rows", name="counts_add_up"
        ),
        CheckConstraint("file_sha256 ~ '^[0-9a-f]{64}$'", name="file_sha256_hex"),
    )

    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    file_type: Mapped[str] = mapped_column(String(8), nullable=False)
    # Column index (as text) -> {"header": ..., "field": ... or null}.
    mapping: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    total_rows: Mapped[int] = mapped_column(Integer, nullable=False)
    imported_count: Mapped[int] = mapped_column(Integer, nullable=False)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # Rows (imported or skipped) that carried a duplicate warning.
    duplicate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # One entry per data row: row number, outcome, lead, errors and duplicate warnings.
    results: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )


class Proposal(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A proposal made to a lead (Phase 4A): the property and spaces offered, the period and
    the commercial terms as entered. Nothing is calculated. Accepting it records the lead's
    acceptance only: no contract, allocation, booking or tenant is created and no space is
    held. Every proposal is kept; a revision is a new proposal."""

    __tablename__ = "proposals"
    __table_args__ = (
        enum_check("status", ProposalStatus),
        CheckConstraint("end_date IS NULL OR end_date >= start_date", name="dates_ordered"),
        CheckConstraint(
            "valid_until IS NULL OR valid_until >= proposal_date", name="validity_ordered"
        ),
        CheckConstraint(
            "(monthly_rent IS NULL OR monthly_rent >= 0)"
            " AND (security_deposit IS NULL OR security_deposit >= 0)",
            name="amounts_not_negative",
        ),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_code"),
        CheckConstraint(
            "(status = 'ACCEPTED') = (accepted_on IS NOT NULL AND acceptance_note IS NOT NULL"
            " AND accepted_at IS NOT NULL)",
            name="acceptance_recorded",
        ),
        CheckConstraint(
            "(status = 'REJECTED') = (rejected_at IS NOT NULL)", name="rejection_recorded"
        ),
        CheckConstraint(
            "(status = 'WITHDRAWN') = (withdrawn_at IS NOT NULL)", name="withdrawal_recorded"
        ),
        # Lets proposal_spaces require spaces of the proposal's own property.
        UniqueConstraint("id", "property_id", name="uq_proposals_id_property"),
        # At most one accepted proposal per lead.
        Index(
            "uq_proposals_accepted_lead",
            "lead_id",
            unique=True,
            postgresql_where=text("status = 'ACCEPTED'"),
        ),
    )

    reference: Mapped[str] = mapped_column(
        String(20),
        unique=True,
        nullable=False,
        server_default=text(
            "('PR-' || to_char(now(), 'YYYY') || '-' "
            "|| lpad(nextval('proposal_reference_seq')::text, 6, '0'))"
        ),
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("leads.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    property_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("properties.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=ProposalStatus.DRAFT.value, index=True
    )
    proposal_date: Mapped[date] = mapped_column(Date, nullable=False)
    valid_until: Mapped[date | None] = mapped_column(Date)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date)
    monthly_rent: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    security_deposit: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="INR")
    other_charges: Mapped[str | None] = mapped_column(Text)
    commercial_terms: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_on: Mapped[date | None] = mapped_column(Date)
    acceptance_note: Mapped[str | None] = mapped_column(Text)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closing_reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )


class ProposalSpace(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """A space offered in a proposal; it must belong to the proposal's property. Building
    and floor are read from the space. Holds no inventory."""

    __tablename__ = "proposal_spaces"
    __table_args__ = (
        ForeignKeyConstraint(
            ["proposal_id", "property_id"],
            ["proposals.id", "proposals.property_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["space_id", "property_id"], ["spaces.id", "spaces.property_id"], ondelete="RESTRICT"
        ),
        UniqueConstraint("proposal_id", "space_id", name="uq_proposal_spaces_proposal_space"),
    )

    proposal_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    property_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    space_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
