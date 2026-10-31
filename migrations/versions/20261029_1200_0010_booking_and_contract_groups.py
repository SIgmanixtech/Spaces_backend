"""several seats booked and contracted together

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-29 12:00:00.000000+00:00

Additive. Nothing existing changes meaning and no row is written.

1. bookings.group_id: bookings a customer made together (for example 5 seats at once)
   share one id. Each seat is still its own booking and holds only itself.
2. tenant_occupancies.group_id: a contract request for several seats is one row per seat
   sharing one id; they are approved, rejected or withdrawn together.

Hand-written; tests/test_migrations.py checks it against the models.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("bookings", sa.Column("group_id", sa.Uuid(), nullable=True))
    op.create_index("ix_bookings_group_id", "bookings", ["group_id"])
    op.add_column("tenant_occupancies", sa.Column("group_id", sa.Uuid(), nullable=True))
    op.create_index("ix_tenant_occupancies_group_id", "tenant_occupancies", ["group_id"])


def downgrade() -> None:
    op.drop_index("ix_tenant_occupancies_group_id", table_name="tenant_occupancies")
    op.drop_column("tenant_occupancies", "group_id")
    op.drop_index("ix_bookings_group_id", table_name="bookings")
    op.drop_column("bookings", "group_id")
