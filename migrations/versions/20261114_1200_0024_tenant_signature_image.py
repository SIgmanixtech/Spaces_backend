"""Tenant signature image (admin upload)

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-14 12:00:00.000000+00:00

Owner request of 14 Nov 2026: the Add Tenant dialog can take a PNG or JPEG image of the
tenant's signature. Stored privately in the property-files bucket; these columns keep its
reference on the tenant. It is only a signature asset: it never marks any contract or
agreement as signed (that stays with the contract-document workflow).

Additive: five nullable columns on tenants and two checks. The downgrade refuses while any
tenant has a signature image (dropping the columns would lose the reference).
Hand-written; tests/test_migrations.py checks it against the models.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("signature_path", sa.String(500), nullable=True))
    op.add_column("tenants", sa.Column("signature_content_type", sa.String(40), nullable=True))
    op.add_column("tenants", sa.Column("signature_size_bytes", sa.Integer(), nullable=True))
    op.add_column(
        "tenants",
        sa.Column("signature_uploaded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tenants",
        sa.Column(
            "signature_uploaded_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "app_users.id",
                name=op.f("fk_tenants_signature_uploaded_by_app_users"),
                ondelete="SET NULL",
            ),
            nullable=True,
        ),
    )
    op.create_unique_constraint(op.f("uq_tenants_signature_path"), "tenants", ["signature_path"])
    op.create_check_constraint(
        op.f("ck_tenants_signature_complete"),
        "tenants",
        "(signature_path IS NULL) = (signature_content_type IS NULL)"
        " AND (signature_path IS NULL) = (signature_size_bytes IS NULL)"
        " AND (signature_path IS NULL) = (signature_uploaded_at IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_tenants_signature_type"),
        "tenants",
        "signature_content_type IS NULL OR signature_content_type IN ('image/png', 'image/jpeg')",
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(
        sa.text("SELECT 1 FROM tenants WHERE signature_path IS NOT NULL LIMIT 1")
    ).first():
        raise RuntimeError(
            "Some tenants have a signature image; remove them before downgrading 0024."
        )
    op.drop_constraint(op.f("ck_tenants_signature_type"), "tenants", type_="check")
    op.drop_constraint(op.f("ck_tenants_signature_complete"), "tenants", type_="check")
    op.drop_constraint(op.f("uq_tenants_signature_path"), "tenants", type_="unique")
    op.drop_constraint(
        op.f("fk_tenants_signature_uploaded_by_app_users"), "tenants", type_="foreignkey"
    )
    for column in (
        "signature_uploaded_by",
        "signature_uploaded_at",
        "signature_size_bytes",
        "signature_content_type",
        "signature_path",
    ):
        op.drop_column("tenants", column)
