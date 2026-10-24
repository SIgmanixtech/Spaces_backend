"""property code: the short code the property team gives a property

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-22 16:00:00.000000+00:00

Additive: one nullable column, unique when set. Existing properties get no code;
nothing is generated or guessed. The Create Property wizard has always asked for a
code and could not store it; from this revision it is saved and shown to customers.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0007'
down_revision: str | None = '0006'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('properties', sa.Column('code', sa.String(length=50), nullable=True))
    op.create_unique_constraint(op.f('uq_properties_code'), 'properties', ['code'])


def downgrade() -> None:
    op.execute(
        """
        DO $$
        DECLARE coded integer;
        BEGIN
          SELECT count(*) INTO coded FROM properties WHERE code IS NOT NULL;
          IF coded > 0 THEN
            RAISE EXCEPTION
              'Cannot downgrade past 0007: % propert(ies) have a code. Export or clear them '
              'deliberately first; this migration will not discard them.', coded;
          END IF;
        END $$;
        """
    )
    op.drop_constraint(op.f('uq_properties_code'), 'properties', type_='unique')
    op.drop_column('properties', 'code')
