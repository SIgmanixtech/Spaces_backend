"""billing profiles, building-level prices and derived rates

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-22 20:00:00.000000+00:00

Additive. No price changes and no profile is created.

1. billing_profiles: how one billing period converts into another for a property, or
   for one space in it (billable hours per day, days per week, days per month, weekly
   and monthly discounts, rounding). No column has a default.

2. pricing_rules:
   * target BUILDING with building_id: a default for the spaces in one building;
   * source CONFIGURED / DERIVED / OVERRIDE (every existing rule is CONFIGURED);
   * base_rule_id, billing_profile_id and derivation (the figures and formula) for
     DERIVED rules, which exist only once an administrator approves an estimate;
   * the active-overlap exclusion now also distinguishes buildings.

Hand-written; tests/test_migrations.py checks it against the models.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0008'
down_revision: str | None = '0007'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NO_SPACE = "00000000-0000-0000-0000-000000000000"
OLD_TARGET = (
    "(target_type = 'SPACE' AND space_id IS NOT NULL AND applies_to IS NULL)"
    " OR (target_type = 'PROPERTY' AND space_id IS NULL)"
)
NEW_TARGET = (
    "(target_type = 'SPACE' AND space_id IS NOT NULL AND building_id IS NULL"
    " AND applies_to IS NULL)"
    " OR (target_type = 'BUILDING' AND building_id IS NOT NULL AND space_id IS NULL)"
    " OR (target_type = 'PROPERTY' AND space_id IS NULL AND building_id IS NULL)"
)


def _overlap(with_building: bool) -> str:
    building = f"(coalesce(building_id, '{NO_SPACE}'::uuid)) WITH =, " if with_building else ""
    return (
        "ALTER TABLE pricing_rules ADD CONSTRAINT ex_pricing_rules_active_overlap "
        "EXCLUDE USING gist (property_id WITH =, "
        f"(coalesce(space_id, '{NO_SPACE}'::uuid)) WITH =, {building}"
        "(coalesce(applies_to, '')) WITH =, rate_basis WITH =, billing_period WITH =, "
        "(daterange(effective_from, effective_to, '[]')) WITH &&) "
        "WHERE (status = 'ACTIVE')"
    )


def upgrade() -> None:
    op.create_table(
        'billing_profiles',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('property_id', sa.Uuid(), nullable=False),
        sa.Column('space_id', sa.Uuid(), nullable=True),
        sa.Column('hours_per_day', sa.Numeric(precision=4, scale=2), nullable=False),
        sa.Column('days_per_week', sa.Numeric(precision=3, scale=1), nullable=False),
        sa.Column('days_per_month', sa.Numeric(precision=4, scale=1), nullable=False),
        sa.Column('weekly_discount_pct', sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column('monthly_discount_pct', sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column('rounding', sa.String(length=16), nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
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
            "rounding IN ('PAISA', 'RUPEE', 'TEN_RUPEES')",
            name=op.f('ck_billing_profiles_rounding_valid'),
        ),
        sa.CheckConstraint(
            'hours_per_day > 0 AND hours_per_day <= 24',
            name=op.f('ck_billing_profiles_hours_per_day'),
        ),
        sa.CheckConstraint(
            'days_per_week > 0 AND days_per_week <= 7',
            name=op.f('ck_billing_profiles_days_per_week'),
        ),
        sa.CheckConstraint(
            'days_per_month > 0 AND days_per_month <= 31',
            name=op.f('ck_billing_profiles_days_per_month'),
        ),
        sa.CheckConstraint(
            'weekly_discount_pct >= 0 AND weekly_discount_pct < 100'
            ' AND monthly_discount_pct >= 0 AND monthly_discount_pct < 100',
            name=op.f('ck_billing_profiles_discounts'),
        ),
        sa.ForeignKeyConstraint(
            ['property_id'], ['properties.id'],
            name=op.f('fk_billing_profiles_property_id_properties'), ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['space_id', 'property_id'], ['spaces.id', 'spaces.property_id'],
            name=op.f('fk_billing_profiles_space_id_spaces'), ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['updated_by'], ['app_users.id'],
            name=op.f('fk_billing_profiles_updated_by_app_users'), ondelete='SET NULL',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_billing_profiles')),
    )
    op.create_index(
        op.f('ix_billing_profiles_property_id'), 'billing_profiles', ['property_id'], unique=False
    )
    op.create_index(
        'uq_billing_profiles_property', 'billing_profiles', ['property_id'], unique=True,
        postgresql_where=sa.text('space_id IS NULL'),
    )
    op.create_index(
        'uq_billing_profiles_space', 'billing_profiles', ['space_id'], unique=True,
        postgresql_where=sa.text('space_id IS NOT NULL'),
    )

    rules = 'pricing_rules'
    op.add_column(rules, sa.Column('building_id', sa.Uuid(), nullable=True))
    op.add_column(
        rules,
        sa.Column('source', sa.String(length=16), server_default='CONFIGURED', nullable=False),
    )
    op.add_column(rules, sa.Column('base_rule_id', sa.Uuid(), nullable=True))
    op.add_column(rules, sa.Column('billing_profile_id', sa.Uuid(), nullable=True))
    op.add_column(
        rules, sa.Column('derivation', postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.create_index(op.f('ix_pricing_rules_building_id'), rules, ['building_id'], unique=False)
    op.create_foreign_key(
        op.f('fk_pricing_rules_building_id_buildings'), rules, 'buildings',
        ['building_id', 'property_id'], ['id', 'property_id'], ondelete='RESTRICT',
    )
    op.create_foreign_key(
        op.f('fk_pricing_rules_base_rule_id_pricing_rules'), rules, rules,
        ['base_rule_id'], ['id'], ondelete='SET NULL',
    )
    op.create_foreign_key(
        op.f('fk_pricing_rules_billing_profile_id_billing_profiles'), rules, 'billing_profiles',
        ['billing_profile_id'], ['id'], ondelete='SET NULL',
    )
    op.create_check_constraint(
        op.f('ck_pricing_rules_source_valid'), rules,
        "source IN ('CONFIGURED', 'DERIVED', 'OVERRIDE')",
    )
    op.create_check_constraint(
        op.f('ck_pricing_rules_derived_has_derivation'), rules,
        "(source = 'DERIVED') = (derivation IS NOT NULL)",
    )
    op.drop_constraint(op.f('ck_pricing_rules_target_type_valid'), rules, type_='check')
    op.create_check_constraint(
        op.f('ck_pricing_rules_target_type_valid'), rules,
        "target_type IN ('PROPERTY', 'BUILDING', 'SPACE')",
    )
    op.drop_constraint(op.f('ck_pricing_rules_target'), rules, type_='check')
    op.create_check_constraint(op.f('ck_pricing_rules_target'), rules, NEW_TARGET)
    op.execute("ALTER TABLE pricing_rules DROP CONSTRAINT ex_pricing_rules_active_overlap")
    op.execute(_overlap(with_building=True))


def downgrade() -> None:
    op.execute(
        """
        DO $$
        DECLARE profiles integer; building_rules integer; derived integer;
        BEGIN
          SELECT count(*) INTO profiles FROM billing_profiles;
          SELECT count(*) INTO building_rules FROM pricing_rules WHERE building_id IS NOT NULL;
          SELECT count(*) INTO derived FROM pricing_rules WHERE source <> 'CONFIGURED';
          IF profiles + building_rules + derived > 0 THEN
            RAISE EXCEPTION
              'Cannot downgrade past 0008: % billing profile(s), % building price(s) and '
              '% derived or override price(s) exist. Export or clear them deliberately '
              'first; this migration will not discard them.', profiles, building_rules, derived;
          END IF;
        END $$;
        """
    )
    rules = 'pricing_rules'
    op.execute("ALTER TABLE pricing_rules DROP CONSTRAINT ex_pricing_rules_active_overlap")
    op.execute(_overlap(with_building=False))
    op.drop_constraint(op.f('ck_pricing_rules_target'), rules, type_='check')
    op.create_check_constraint(op.f('ck_pricing_rules_target'), rules, OLD_TARGET)
    op.drop_constraint(op.f('ck_pricing_rules_target_type_valid'), rules, type_='check')
    op.create_check_constraint(
        op.f('ck_pricing_rules_target_type_valid'), rules, "target_type IN ('PROPERTY', 'SPACE')"
    )
    op.drop_constraint(op.f('ck_pricing_rules_derived_has_derivation'), rules, type_='check')
    op.drop_constraint(op.f('ck_pricing_rules_source_valid'), rules, type_='check')
    op.drop_constraint(
        op.f('fk_pricing_rules_billing_profile_id_billing_profiles'), rules, type_='foreignkey'
    )
    op.drop_constraint(op.f('fk_pricing_rules_base_rule_id_pricing_rules'), rules, type_='foreignkey')
    op.drop_constraint(op.f('fk_pricing_rules_building_id_buildings'), rules, type_='foreignkey')
    op.drop_index(op.f('ix_pricing_rules_building_id'), table_name=rules)
    for column in ('derivation', 'billing_profile_id', 'base_rule_id', 'source', 'building_id'):
        op.drop_column(rules, column)
    op.drop_table('billing_profiles')
