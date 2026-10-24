"""customer access: customer accounts, published properties, booking quotes

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-22 15:00:00.000000+00:00

Additive. Nothing existing is rewritten and nothing becomes visible to customers
until an administrator publishes it.

1. app_users.role may be CUSTOMER as well as ADMIN.
2. tenants.user_id links a tenant record to the customer account that owns it
   (unique: one account, one tenant). Ownership checks start from it.
3. properties.is_published: shown in the client app only when true. Every
   existing property starts unpublished.
4. bookings.channel (ADMIN for every existing booking) and the quote the backend
   calculated for a customer booking: billing period, pricing rule, total,
   currency and the itemised lines.

Hand-written; tests/test_migrations.py checks it against the models.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0006'
down_revision: str | None = '0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERIODS = "'HOURLY', 'HALF_DAY', 'FULL_DAY', 'DAILY', 'WEEKLY', 'MONTHLY', 'CUSTOM'"


def upgrade() -> None:
    op.drop_constraint(op.f('ck_app_users_role_valid'), 'app_users', type_='check')
    op.create_check_constraint(
        op.f('ck_app_users_role_valid'), 'app_users', "role IN ('ADMIN', 'CUSTOMER')"
    )

    op.add_column('tenants', sa.Column('user_id', sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f('fk_tenants_user_id_app_users'), 'tenants', 'app_users', ['user_id'], ['id'],
        ondelete='SET NULL',
    )
    op.create_unique_constraint(op.f('uq_tenants_user_id'), 'tenants', ['user_id'])

    op.add_column(
        'properties',
        sa.Column('is_published', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    )

    op.add_column(
        'bookings',
        sa.Column('channel', sa.String(length=16), server_default='ADMIN', nullable=False),
    )
    op.add_column('bookings', sa.Column('billing_period', sa.String(length=16), nullable=True))
    op.add_column('bookings', sa.Column('pricing_rule_id', sa.Uuid(), nullable=True))
    op.add_column(
        'bookings', sa.Column('quoted_total', sa.Numeric(precision=12, scale=2), nullable=True)
    )
    op.add_column('bookings', sa.Column('quoted_currency', sa.String(length=3), nullable=True))
    op.add_column(
        'bookings', sa.Column('quote', postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.create_check_constraint(
        op.f('ck_bookings_channel_valid'), 'bookings', "channel IN ('ADMIN', 'CUSTOMER')"
    )
    op.create_check_constraint(
        op.f('ck_bookings_billing_period_valid'), 'bookings', f"billing_period IN ({PERIODS})"
    )
    op.create_foreign_key(
        op.f('fk_bookings_pricing_rule_id_pricing_rules'), 'bookings', 'pricing_rules',
        ['pricing_rule_id'], ['id'], ondelete='SET NULL',
    )


def downgrade() -> None:
    # Customer accounts, their tenant links, publication choices and customer quotes
    # exist only from this revision on. Refuse rather than discard them.
    op.execute(
        """
        DO $$
        DECLARE customers integer; linked integer; published integer; quoted integer;
        BEGIN
          SELECT count(*) INTO customers FROM app_users WHERE role = 'CUSTOMER';
          SELECT count(*) INTO linked FROM tenants WHERE user_id IS NOT NULL;
          SELECT count(*) INTO published FROM properties WHERE is_published;
          SELECT count(*) INTO quoted FROM bookings
           WHERE channel <> 'ADMIN' OR quoted_total IS NOT NULL OR quote IS NOT NULL;
          IF customers + linked + published + quoted > 0 THEN
            RAISE EXCEPTION
              'Cannot downgrade past 0006: % customer account(s), % linked tenant(s), '
              '% published propert(ies) and % customer booking(s) exist. Export or clear '
              'them deliberately first; this migration will not discard them.',
              customers, linked, published, quoted;
          END IF;
        END $$;
        """
    )
    op.drop_constraint(
        op.f('fk_bookings_pricing_rule_id_pricing_rules'), 'bookings', type_='foreignkey'
    )
    op.drop_constraint(op.f('ck_bookings_billing_period_valid'), 'bookings', type_='check')
    op.drop_constraint(op.f('ck_bookings_channel_valid'), 'bookings', type_='check')
    for column in (
        'quote', 'quoted_currency', 'quoted_total', 'pricing_rule_id', 'billing_period', 'channel'
    ):
        op.drop_column('bookings', column)
    op.drop_column('properties', 'is_published')
    op.drop_constraint(op.f('uq_tenants_user_id'), 'tenants', type_='unique')
    op.drop_constraint(op.f('fk_tenants_user_id_app_users'), 'tenants', type_='foreignkey')
    op.drop_column('tenants', 'user_id')
    op.drop_constraint(op.f('ck_app_users_role_valid'), 'app_users', type_='check')
    op.create_check_constraint(op.f('ck_app_users_role_valid'), 'app_users', "role IN ('ADMIN')")
