"""Tenant accounts (BR-017, read-only Accounts phase)

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-02 11:00:00.000000+00:00

Additive. One new table; nothing existing is touched.

- accounts: one per tenant with a completed contract (BR-025), unique per tenant. No
  formatted reference until D7 is confirmed; the lead link is filled once lead conversion
  exists (Phase 5). Invoices, payments and advances are not part of this revision.

The downgrade refuses while any account exists. Hand-written; tests/test_migrations.py
checks it against the models.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    t = "accounts"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f(f"fk_{t}_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["lead_id"], ["leads.id"], name=op.f(f"fk_{t}_lead_id_leads"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["app_users.id"],
            name=op.f(f"fk_{t}_created_by_app_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{t}")),
        sa.UniqueConstraint("tenant_id", name=op.f(f"uq_{t}_tenant_id")),
    )
    op.create_index(op.f(f"ix_{t}_lead_id"), t, ["lead_id"])


def downgrade() -> None:
    op.execute(
        """
        DO $$
        DECLARE accounts integer;
        BEGIN
          SELECT count(*) INTO accounts FROM accounts;
          IF accounts > 0 THEN
            RAISE EXCEPTION
              'Cannot downgrade past 0017: % account(s) exist. Export or clear them '
              'deliberately first; this migration will not discard them.', accounts;
          END IF;
        END $$;
        """
    )
    op.drop_index(op.f("ix_accounts_lead_id"), table_name="accounts")
    op.drop_table("accounts")
