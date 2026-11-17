"""Booking agreements: documents linked to a one-time booking

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-17 17:00:00.000000+00:00

Owner decision of 17 Nov 2026: a Conference Room Usage Agreement belongs to the booking it
was made for, not to a contract (a one-time booking never becomes a lease or membership).
booking_documents holds the stored DOCX, its reference (e.g. CRA-BK-2026-000007), status
GENERATED -> APPROVED -> SIGNED (SUPERSEDED when replaced before signing), and the signed
copy (PDF) that is the only way to SIGNED.

Additive: one new table. The downgrade refuses while the table holds a row.
Hand-written; tests/test_migrations.py checks it against the models.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "booking_documents",
        sa.Column("reference", sa.String(40), nullable=False),
        sa.Column("booking_id", sa.Uuid(), nullable=False),
        sa.Column(
            "agreement_type",
            sa.String(32),
            server_default="CONFERENCE_ROOM_USAGE",
            nullable=False,
        ),
        sa.Column("status", sa.String(16), server_default="GENERATED", nullable=False),
        sa.Column("bucket", sa.String(100), nullable=False),
        sa.Column("object_path", sa.String(500), nullable=False),
        sa.Column("file_name", sa.String(255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("generated_on", sa.Date(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("uploaded_by", sa.Uuid(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by", sa.Uuid(), nullable=True),
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("signed_object_path", sa.String(500), nullable=True),
        sa.Column("signed_file_name", sa.String(255), nullable=True),
        sa.Column("signed_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("signed_sha256", sa.String(64), nullable=True),
        sa.Column("signed_on", sa.Date(), nullable=True),
        sa.Column(
            "signers",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("signed_uploaded_by", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('GENERATED', 'APPROVED', 'SIGNED', 'SUPERSEDED')",
            name=op.f("ck_booking_documents_status_valid"),
        ),
        sa.CheckConstraint(
            "agreement_type IN ('CONFERENCE_ROOM_USAGE')",
            name=op.f("ck_booking_documents_agreement_type_valid"),
        ),
        sa.CheckConstraint("size_bytes > 0", name=op.f("ck_booking_documents_size_positive")),
        sa.CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_booking_documents_sha256_hex")
        ),
        sa.CheckConstraint(
            "status <> 'SIGNED' OR (signed_object_path IS NOT NULL AND signed_on IS NOT NULL"
            " AND jsonb_array_length(signers) > 0)",
            name=op.f("ck_booking_documents_signed_has_evidence"),
        ),
        sa.ForeignKeyConstraint(
            ["booking_id"],
            ["bookings.id"],
            name=op.f("fk_booking_documents_booking_id_bookings"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by"],
            ["app_users.id"],
            name=op.f("fk_booking_documents_uploaded_by_app_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["approved_by"],
            ["app_users.id"],
            name=op.f("fk_booking_documents_approved_by_app_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["signed_uploaded_by"],
            ["app_users.id"],
            name=op.f("fk_booking_documents_signed_uploaded_by_app_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_booking_documents")),
        sa.UniqueConstraint("reference", name=op.f("uq_booking_documents_reference")),
        sa.UniqueConstraint("object_path", name=op.f("uq_booking_documents_object_path")),
        sa.UniqueConstraint(
            "signed_object_path", name=op.f("uq_booking_documents_signed_object_path")
        ),
        sa.UniqueConstraint("booking_id", "sha256", name="uq_booking_documents_file"),
    )
    op.create_index(op.f("ix_booking_documents_booking_id"), "booking_documents", ["booking_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT EXISTS (SELECT 1 FROM booking_documents)")).scalar():
        raise RuntimeError(
            "booking_documents holds agreements; refusing to drop it. Export them first."
        )
    op.drop_index(op.f("ix_booking_documents_booking_id"), table_name="booking_documents")
    op.drop_table("booking_documents")
