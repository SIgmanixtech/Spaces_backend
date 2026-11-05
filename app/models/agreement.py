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
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import ContractType, DocumentStatus, TemplateStatus
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
        UniqueConstraint(
            "contract_type", "property_id", "version", name="uq_contract_templates_version"
        ),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint("(status = 'DRAFT') = (approved_at IS NULL)", name="approved_unless_draft"),
        # At most one ACTIVE template per contract type, for a property or as the default.
        Index(
            "uq_contract_templates_active",
            "contract_type",
            text("coalesce(property_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
    )

    contract_type: Mapped[str] = mapped_column(String(32), nullable=False)
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
