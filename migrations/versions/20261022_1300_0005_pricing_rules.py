"""pricing rules, and workstation and seat space types

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-22 13:00:00.000000+00:00

Additive. Nothing existing is rewritten.

1. Space types gain DEDICATED_WORKSTATION, FLEXIBLE_WORKSTATION, SEAT and OTHER.
   The allowed list only grows, so every existing row still passes.

2. pricing_rules holds what a property (as a default) or one space costs: an
   amount, what it is multiplied by (rate_basis) and how often it is charged
   (billing_period), with effective dates, a DRAFT/ACTIVE/RETIRED status,
   maintenance, tax, recurring additional charges, a deposit rule and a payment
   frequency. An exclusion constraint refuses two ACTIVE rules for the same
   target, space type, basis and period whose dates overlap.

   No rule is created: existing spaces keep their spaces.price_amount /
   price_unit list price, which pricing rules neither read nor write. No example
   prices are inserted.

Hand-written; tests/test_migrations.py checks it against the models.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0005'
down_revision: str | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_SPACE_TYPES = (
    "'DESK', 'PRIVATE_OFFICE', 'CABIN', 'MEETING_ROOM', 'COWORKING', 'FLOOR', 'PORTION', "
    "'CONFERENCE_ROOM', 'TRAINING_ROOM', 'NON_RENTABLE'"
)
NEW_SPACE_TYPES = "'DEDICATED_WORKSTATION', 'FLEXIBLE_WORKSTATION', 'SEAT', 'OTHER'"
ALL_SPACE_TYPES = f"{OLD_SPACE_TYPES}, {NEW_SPACE_TYPES}"
NO_SPACE = "00000000-0000-0000-0000-000000000000"


def upgrade() -> None:
    # --- 1. space types --------------------------------------------------------------
    op.drop_constraint(op.f('ck_spaces_space_type_valid'), 'spaces', type_='check')
    op.create_check_constraint(
        op.f('ck_spaces_space_type_valid'), 'spaces', f"space_type IN ({ALL_SPACE_TYPES})"
    )

    # --- 2. pricing rules ------------------------------------------------------------
    op.create_table(
        'pricing_rules',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('property_id', sa.Uuid(), nullable=False),
        sa.Column('target_type', sa.String(length=16), nullable=False),
        sa.Column('space_id', sa.Uuid(), nullable=True),
        sa.Column('applies_to', sa.String(length=32), nullable=True),
        sa.Column('rate_basis', sa.String(length=32), nullable=False),
        sa.Column('billing_period', sa.String(length=16), nullable=False),
        sa.Column('custom_period_label', sa.String(length=80), nullable=True),
        sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column('currency', sa.String(length=3), server_default='INR', nullable=False),
        sa.Column('effective_from', sa.Date(), nullable=False),
        sa.Column('effective_to', sa.Date(), nullable=True),
        sa.Column('status', sa.String(length=16), server_default='DRAFT', nullable=False),
        sa.Column('maintenance_amount', sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column('maintenance_basis', sa.String(length=16), nullable=True),
        sa.Column('tax_treatment', sa.String(length=16), server_default='EXEMPT', nullable=False),
        sa.Column('tax_rate', sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column(
            'additional_charges', postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"), nullable=False,
        ),
        sa.Column('deposit_rule', sa.String(length=16), server_default='NONE', nullable=False),
        sa.Column('deposit_value', sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column(
            'payment_frequency', sa.String(length=16), server_default='MONTHLY', nullable=False
        ),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_by', sa.Uuid(), nullable=True),
        sa.Column('updated_by', sa.Uuid(), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.CheckConstraint(
            "target_type IN ('PROPERTY', 'SPACE')", name=op.f('ck_pricing_rules_target_type_valid')
        ),
        sa.CheckConstraint(
            "rate_basis IN ('PER_SQFT', 'PER_PERSON', 'PER_FLOOR', 'PER_PORTION', 'PER_CABIN', "
            "'PER_OFFICE', 'PER_WORKSTATION', 'PER_SEAT', 'PER_ROOM', 'PER_HALL', 'PER_UNIT')",
            name=op.f('ck_pricing_rules_rate_basis_valid'),
        ),
        sa.CheckConstraint(
            "billing_period IN ('HOURLY', 'HALF_DAY', 'FULL_DAY', 'DAILY', 'WEEKLY', 'MONTHLY', "
            "'CUSTOM')",
            name=op.f('ck_pricing_rules_billing_period_valid'),
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'ACTIVE', 'RETIRED')", name=op.f('ck_pricing_rules_status_valid')
        ),
        sa.CheckConstraint(
            "maintenance_basis IN ('PER_SQFT', 'FIXED')",
            name=op.f('ck_pricing_rules_maintenance_basis_valid'),
        ),
        sa.CheckConstraint(
            "tax_treatment IN ('EXEMPT', 'INCLUDED', 'ADDITIONAL')",
            name=op.f('ck_pricing_rules_tax_treatment_valid'),
        ),
        sa.CheckConstraint(
            "deposit_rule IN ('NONE', 'FIXED', 'MONTHS_OF_RENT')",
            name=op.f('ck_pricing_rules_deposit_rule_valid'),
        ),
        sa.CheckConstraint(
            "payment_frequency IN ('MONTHLY', 'QUARTERLY', 'HALF_YEARLY', 'ANNUALLY', "
            "'PER_BOOKING', 'CUSTOM')",
            name=op.f('ck_pricing_rules_payment_frequency_valid'),
        ),
        sa.CheckConstraint(
            f"applies_to IN ({ALL_SPACE_TYPES})", name=op.f('ck_pricing_rules_applies_to_valid')
        ),
        sa.CheckConstraint(
            "(target_type = 'SPACE' AND space_id IS NOT NULL AND applies_to IS NULL)"
            " OR (target_type = 'PROPERTY' AND space_id IS NULL)",
            name=op.f('ck_pricing_rules_target'),
        ),
        sa.CheckConstraint('amount >= 0', name=op.f('ck_pricing_rules_amount_not_negative')),
        sa.CheckConstraint(
            'effective_to IS NULL OR effective_to >= effective_from',
            name=op.f('ck_pricing_rules_dates_ordered'),
        ),
        sa.CheckConstraint(
            "(maintenance_amount IS NULL) = (maintenance_basis IS NULL)"
            " AND (maintenance_amount IS NULL OR maintenance_amount >= 0)",
            name=op.f('ck_pricing_rules_maintenance'),
        ),
        sa.CheckConstraint(
            "(tax_treatment = 'EXEMPT') = (tax_rate IS NULL)"
            " AND (tax_rate IS NULL OR tax_rate BETWEEN 0 AND 100)",
            name=op.f('ck_pricing_rules_tax'),
        ),
        sa.CheckConstraint(
            "(deposit_rule = 'NONE') = (deposit_value IS NULL)"
            " AND (deposit_value IS NULL OR deposit_value >= 0)"
            " AND (deposit_rule <> 'MONTHS_OF_RENT' OR billing_period = 'MONTHLY')",
            name=op.f('ck_pricing_rules_deposit'),
        ),
        sa.CheckConstraint(
            "(billing_period = 'CUSTOM') = (custom_period_label IS NOT NULL)",
            name=op.f('ck_pricing_rules_custom_period'),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(additional_charges) = 'array'",
            name=op.f('ck_pricing_rules_charges_array'),
        ),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=op.f('ck_pricing_rules_currency_code')),
        sa.ForeignKeyConstraint(
            ['property_id'], ['properties.id'],
            name=op.f('fk_pricing_rules_property_id_properties'), ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['space_id', 'property_id'], ['spaces.id', 'spaces.property_id'],
            name=op.f('fk_pricing_rules_space_id_spaces'), ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['created_by'], ['app_users.id'],
            name=op.f('fk_pricing_rules_created_by_app_users'), ondelete='SET NULL',
        ),
        sa.ForeignKeyConstraint(
            ['updated_by'], ['app_users.id'],
            name=op.f('fk_pricing_rules_updated_by_app_users'), ondelete='SET NULL',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_pricing_rules')),
    )
    op.create_index(
        'ix_pricing_rules_property_status', 'pricing_rules', ['property_id', 'status'],
        unique=False,
    )
    op.create_index(op.f('ix_pricing_rules_space_id'), 'pricing_rules', ['space_id'], unique=False)
    op.execute(
        "ALTER TABLE pricing_rules ADD CONSTRAINT ex_pricing_rules_active_overlap "
        "EXCLUDE USING gist (property_id WITH =, "
        f"(coalesce(space_id, '{NO_SPACE}'::uuid)) WITH =, "
        "(coalesce(applies_to, '')) WITH =, rate_basis WITH =, billing_period WITH =, "
        "(daterange(effective_from, effective_to, '[]')) WITH &&) "
        "WHERE (status = 'ACTIVE')"
    )


def downgrade() -> None:
    # Dropping the table would discard every price entered since the upgrade, and the
    # old type list cannot hold a workstation or seat. Refuse rather than lose them.
    op.execute(
        f"""
        DO $$
        DECLARE rules integer; spaces_using integer;
        BEGIN
          SELECT count(*) INTO rules FROM pricing_rules;
          SELECT count(*) INTO spaces_using FROM spaces WHERE space_type IN ({NEW_SPACE_TYPES});
          IF rules > 0 OR spaces_using > 0 THEN
            RAISE EXCEPTION
              'Cannot downgrade past 0005: % pricing rule(s) and % space(s) of a new type '
              '(workstation, seat or other) exist. Export or clear them deliberately first; '
              'this migration will not discard them.', rules, spaces_using;
          END IF;
        END $$;
        """
    )
    op.drop_table('pricing_rules')
    op.drop_constraint(op.f('ck_spaces_space_type_valid'), 'spaces', type_='check')
    op.create_check_constraint(
        op.f('ck_spaces_space_type_valid'), 'spaces', f"space_type IN ({OLD_SPACE_TYPES})"
    )
