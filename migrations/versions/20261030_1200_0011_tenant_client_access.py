"""client app access for tenants: invited and activated times

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-30 12:00:00.000000+00:00

Additive. Nothing existing changes meaning and no row is written.

tenants.client_invited_at: when the property team last invited the tenant to the client
app (only tenants with a completed contract and an email can be invited).
tenants.client_activated_at: when the tenant first set a password from the invitation.
Only tenants with client_activated_at can use the client app. Activation needs an
invitation first.

Hand-written; tests/test_migrations.py checks it against the models.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tenants", sa.Column("client_invited_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "tenants", sa.Column("client_activated_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        op.f("ck_tenants_activated_after_invited"),
        "tenants",
        "client_activated_at IS NULL OR client_invited_at IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_tenants_activated_after_invited"), "tenants", type_="check")
    op.drop_column("tenants", "client_activated_at")
    op.drop_column("tenants", "client_invited_at")
