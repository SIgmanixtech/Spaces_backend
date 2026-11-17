"""Contract agreements: admin-approved Word templates and the documents generated from them.

There are two contract types and they never stand in for each other:

* SPACE_LEASE        a rental / lease of a defined space (floor, portion, office, cabin...)
* COWORKING_LICENCE  a licence to use coworking workspace (desk, seat, coworking cabin...)

A contract's type is set explicitly by an administrator, on the contract or as a default on
its space; it is never guessed from a name. A template is DRAFT until an administrator
approves it, which makes it ACTIVE; activating a newer version retires the older one. A
generated document keeps the template and version it came from, so retiring or replacing
a template never rewrites an existing agreement.

Documents move GENERATED -> APPROVED -> SENT_FOR_SIGNATURE -> SIGNED by explicit
administrator actions; nothing is ever marked signed automatically. A newer generation
for the same contract SUPERSEDES an unsigned one; a SIGNED document is never replaced.
"""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
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
    AddendumStatus,
    ContractCategory,
    ContractType,
    DocumentStatus,
    TemplateStatus,
)
from app.models.mixins import (
    CreatedAtMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    enum_check,
)


class ContractTemplate(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "contract_templates"
    __table_args__ = (
        enum_check("contract_type", ContractType),
        enum_check("status", TemplateStatus),
        enum_check("category", ContractCategory),
        # Versions count per agreement type, category and scope (migration 0022). The
        # expressions treat a missing category or property as one value, portably.
        Index(
            "uq_contract_templates_version",
            "contract_type",
            text("coalesce(category, '')"),
            text("coalesce(property_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
            "version",
            unique=True,
        ),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint("(status = 'DRAFT') = (approved_at IS NULL)", name="approved_unless_draft"),
        # At most one ACTIVE template per agreement type and category, for a property or as
        # the default (D9.2).
        Index(
            "uq_contract_templates_active",
            "contract_type",
            text("coalesce(category, '')"),
            text("coalesce(property_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
    )

    contract_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # D9.2: chosen by category + agreement type; NULL = made before categories (D8.6).
    category: Mapped[str | None] = mapped_column(String(16))
    # Null: the default for every property without its own template of this type.
    property_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("properties.id", ondelete="RESTRICT"), index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=TemplateStatus.DRAFT.value
    )
    bucket: Mapped[str] = mapped_column(String(100), nullable=False)
    object_path: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # Placeholders found in the document, and the customer fields this template needs.
    placeholders: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    required_fields: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    notes: Mapped[str | None] = mapped_column(Text)
    # Phase 4B: fields this template asks for, filled as {{ custom.<key> }}:
    # [{"key", "label", "type": text|number|date, "required": bool}].
    custom_fields: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ContractDocument(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "contract_documents"
    __table_args__ = (
        enum_check("status", DocumentStatus),
        enum_check("contract_type", ContractType),
        # Generating twice from the same inputs returns the same document.
        UniqueConstraint("contract_id", "input_hash", name="uq_contract_documents_inputs"),
    )

    contract_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant_occupancies.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    template_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("contract_templates.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    contract_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=DocumentStatus.GENERATED.value
    )
    bucket: Mapped[str] = mapped_column(String(100), nullable=False)
    object_path: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # The values put into the template, kept so the document can be explained later.
    data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    generated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ContractSignedCopy(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """A scan of the agreement signed on paper (Phase 4B): evidence of a paper signature,
    not a verified digital signature. Uploading it marks the document SIGNED; it never
    activates the contract. A copy is never overwritten: a replacement keeps the old copy
    with when and why it was replaced. At most one current copy per document.

    A future e-signature provider would add its own source here (`source`), leaving
    manual uploads as they are."""

    __tablename__ = "contract_signed_copies"
    __table_args__ = (
        CheckConstraint("source IN ('MANUAL_UPLOAD')", name="source_valid"),
        CheckConstraint("size_bytes > 0", name="size_positive"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_hex"),
        CheckConstraint(
            "(replaced_at IS NULL) = (replace_reason IS NULL)", name="replacement_recorded"
        ),
        CheckConstraint("jsonb_array_length(signers) > 0", name="has_signers"),
        Index(
            "uq_contract_signed_copies_current",
            "document_id",
            unique=True,
            postgresql_where=text("replaced_at IS NULL"),
        ),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("contract_documents.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    contract_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant_occupancies.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String(24), nullable=False, server_default="MANUAL_UPLOAD")
    bucket: Mapped[str] = mapped_column(String(100), nullable=False)
    object_path: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    signed_on: Mapped[date] = mapped_column(Date, nullable=False)
    # [{"party": "TENANT" | "COMPANY", "name": ..., "designation": ...}]
    signers: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )
    replaced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replace_reason: Mapped[str | None] = mapped_column(Text)


addendum_reference_seq = Sequence("addendum_reference_seq", metadata=Base.metadata)


class ContractAddendum(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A change to the terms of a contract, on the same contract (LC-3). Its new values
    (``changes``) apply only when it is recorded as signed by both parties (LC-4); until
    then the contract's terms are unchanged. The values it replaced are kept in
    ``previous``. Never deleted; a draft can be cancelled with a reason."""

    __tablename__ = "contract_addenda"
    __table_args__ = (
        enum_check("status", AddendumStatus),
        CheckConstraint("(status = 'SIGNED') = (signed_at IS NOT NULL)", name="signature_recorded"),
        CheckConstraint(
            "(status = 'CANCELLED') = (cancelled_at IS NOT NULL)", name="cancellation_recorded"
        ),
        CheckConstraint(
            "status <> 'SIGNED' OR (object_path IS NOT NULL AND signed_on IS NOT NULL"
            " AND jsonb_array_length(signers) > 0)",
            name="signed_has_evidence",
        ),
    )

    reference: Mapped[str] = mapped_column(
        String(20),
        unique=True,
        nullable=False,
        server_default=text(
            "('AD-' || to_char(now(), 'YYYY') || '-' "
            "|| lpad(nextval('addendum_reference_seq')::text, 6, '0'))"
        ),
    )
    contract_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant_occupancies.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=AddendumStatus.DRAFT.value
    )
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    changes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    previous: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # The signed copy (paper signature evidence), as for agreements.
    bucket: Mapped[str | None] = mapped_column(String(100))
    object_path: Mapped[str | None] = mapped_column(String(500), unique=True)
    file_name: Mapped[str | None] = mapped_column(String(255))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64))
    signed_on: Mapped[date | None] = mapped_column(Date)
    signers: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancellation_reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )


class BookingDocument(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An agreement for a one-time booking, such as a Conference Room Usage Agreement
    (17 Nov 2026, migration 0026). Kept apart from contract documents: a booking is not a
    contract and never becomes a lease or membership through this record.

    The DOCX is stored once and never rewritten. Status moves GENERATED -> APPROVED ->
    SIGNED by explicit administrator actions; SIGNED needs the uploaded signed copy (PDF).
    A signed document is never replaced. The same file uploaded twice for a booking is the
    same document (unique by booking and SHA-256)."""

    __tablename__ = "booking_documents"
    __table_args__ = (
        CheckConstraint(
            "status IN ('GENERATED', 'APPROVED', 'SIGNED', 'SUPERSEDED')", name="status_valid"
        ),
        CheckConstraint("agreement_type IN ('CONFERENCE_ROOM_USAGE')", name="agreement_type_valid"),
        CheckConstraint("size_bytes > 0", name="size_positive"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_hex"),
        CheckConstraint(
            "status <> 'SIGNED' OR (signed_object_path IS NOT NULL AND signed_on IS NOT NULL"
            " AND jsonb_array_length(signers) > 0)",
            name="signed_has_evidence",
        ),
        UniqueConstraint("booking_id", "sha256", name="uq_booking_documents_file"),
    )

    reference: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    booking_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bookings.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    agreement_type: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="CONFERENCE_ROOM_USAGE"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=DocumentStatus.GENERATED.value
    )
    bucket: Mapped[str] = mapped_column(String(100), nullable=False)
    object_path: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    generated_on: Mapped[date] = mapped_column(Date, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The signed copy (paper signature evidence, PDF).
    signed_object_path: Mapped[str | None] = mapped_column(String(500), unique=True)
    signed_file_name: Mapped[str | None] = mapped_column(String(255))
    signed_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    signed_sha256: Mapped[str | None] = mapped_column(String(64))
    signed_on: Mapped[date | None] = mapped_column(Date)
    signers: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    signed_uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )
