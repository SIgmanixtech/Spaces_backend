"""Booking requests reserve nothing; client-app rental requests; 10-minute checkout holds

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-12 20:00:00.000000+00:00

Owner decisions of 12 Nov 2026.

1. A PENDING booking is a request and reserves nothing; only CONFIRMED (and COMPLETED)
   bookings hold. The overlap rule `ex_bookings_space_time` is recreated for those statuses
   only, and the allocations of existing PENDING bookings are released (kept as history,
   reason PENDING_REQUEST). **Data change:** existing pending requests stop holding their
   space; confirming one checks the space again.
2. Leads: source CLIENT_APP (a long-term rental request from the client app) and
   `requested_term_months` (1-120). Additive.
3. `payment_holds` (new) and allocation source PAYMENT_HOLD with `allocations.payment_hold_id`:
   a 10-minute checkout hold while a customer pays online. Additive; nothing uses it until
   online payments are switched on (ONLINE_PAYMENTS_ENABLED, off by default).

The downgrade refuses while any pending booking, client-app lead, requested term or checkout
hold exists (it could not restore what the upgrade changed without guessing).
Hand-written; tests/test_migrations.py checks it against the models.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_SOURCES = "'MANUAL', 'EXCEL', 'META_ADS', 'OTHER'"
NEW_SOURCES = "'MANUAL', 'EXCEL', 'META_ADS', 'OTHER', 'CLIENT_APP'"
PERIODS = "'HOURLY', 'HALF_DAY', 'FULL_DAY', 'DAILY', 'WEEKLY', 'MONTHLY', 'CUSTOM'"
HOLD_STATUSES = "'ACTIVE', 'CONVERTED', 'RELEASED', 'EXPIRED', 'NEEDS_REVIEW', 'RESOLVED'"
OLD_ALLOCATION_SOURCE = (
    "(source_type = 'BOOKING' AND booking_id IS NOT NULL AND contract_id IS NULL"
    " AND NOT upper_inf(period))"
    " OR (source_type = 'CONTRACT' AND contract_id IS NOT NULL AND booking_id IS NULL)"
)
NEW_ALLOCATION_SOURCE = (
    "(source_type = 'BOOKING' AND booking_id IS NOT NULL AND contract_id IS NULL"
    " AND payment_hold_id IS NULL AND NOT upper_inf(period))"
    " OR (source_type = 'CONTRACT' AND contract_id IS NOT NULL AND booking_id IS NULL"
    " AND payment_hold_id IS NULL)"
    " OR (source_type = 'PAYMENT_HOLD' AND payment_hold_id IS NOT NULL"
    " AND booking_id IS NULL AND contract_id IS NULL AND NOT upper_inf(period))"
)


def _now(name: str) -> sa.Column:
    return sa.Column(
        name, sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    # Earlier revisions in the same transaction may leave deferred trigger events on
    # allocations; fire them now, or the table cannot be altered below.
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")
    # 1. Booking requests reserve nothing.
    op.execute("ALTER TABLE bookings DROP CONSTRAINT ex_bookings_space_time")
    op.execute(
        "ALTER TABLE bookings ADD CONSTRAINT ex_bookings_space_time EXCLUDE USING gist "
        "(space_id WITH =, tstzrange(start_at, end_at, '[)') WITH &&) "
        "WHERE (status IN ('CONFIRMED', 'COMPLETED'))"
    )

    # 2. Client-app rental requests become leads.
    op.drop_constraint(op.f("ck_leads_source_valid"), "leads", type_="check")
    op.create_check_constraint(op.f("ck_leads_source_valid"), "leads", f"source IN ({NEW_SOURCES})")
    op.add_column("leads", sa.Column("requested_term_months", sa.SmallInteger(), nullable=True))
    op.create_check_constraint(
        op.f("ck_leads_term_months_range"),
        "leads",
        "requested_term_months IS NULL OR requested_term_months BETWEEN 1 AND 120",
    )

    # 3. Checkout holds.
    op.execute("CREATE SEQUENCE payment_hold_reference_seq")
    t = "payment_holds"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "reference",
            sa.String(length=20),
            server_default=sa.text(
                "('PH-' || to_char(now(), 'YYYY') || '-' "
                "|| lpad(nextval('payment_hold_reference_seq')::text, 6, '0'))"
            ),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), server_default="ACTIVE", nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("property_id", sa.Uuid(), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("billing_period", sa.String(length=16), nullable=False),
        sa.Column("pricing_rule_id", sa.Uuid(), nullable=True),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "quote",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("release_reason", sa.String(length=32), nullable=True),
        sa.Column("booking_id", sa.Uuid(), nullable=True),
        sa.Column("payment_reference", sa.String(length=100), nullable=True),
        sa.Column("payment_amount", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("payment_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_reason", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.Uuid(), nullable=True),
        sa.Column("resolution", sa.Text(), nullable=True),
        _now("created_at"),
        _now("updated_at"),
        sa.CheckConstraint(f"status IN ({HOLD_STATUSES})", name=op.f(f"ck_{t}_status_valid")),
        sa.CheckConstraint(
            f"billing_period IN ({PERIODS})", name=op.f(f"ck_{t}_billing_period_valid")
        ),
        sa.CheckConstraint("end_at > start_at", name=op.f(f"ck_{t}_time_range_ordered")),
        sa.CheckConstraint("expires_at > created_at", name=op.f(f"ck_{t}_expiry_after_creation")),
        sa.CheckConstraint(
            "(status = 'ACTIVE') = (released_at IS NULL)",
            name=op.f(f"ck_{t}_released_unless_active"),
        ),
        sa.CheckConstraint(
            "status <> 'CONVERTED' OR booking_id IS NOT NULL",
            name=op.f(f"ck_{t}_converted_booking"),
        ),
        sa.CheckConstraint(
            "status NOT IN ('NEEDS_REVIEW', 'RESOLVED') OR review_reason IS NOT NULL",
            name=op.f(f"ck_{t}_review_reason_given"),
        ),
        sa.ForeignKeyConstraint(
            ["space_id", "property_id"],
            ["spaces.id", "spaces.property_id"],
            name=op.f(f"fk_{t}_space_id_spaces"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f(f"fk_{t}_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_users.id"],
            name=op.f(f"fk_{t}_user_id_app_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["pricing_rule_id"],
            ["pricing_rules.id"],
            name=op.f(f"fk_{t}_pricing_rule_id_pricing_rules"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["booking_id"],
            ["bookings.id"],
            name=op.f(f"fk_{t}_booking_id_bookings"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by"],
            ["app_users.id"],
            name=op.f(f"fk_{t}_resolved_by_app_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{t}")),
        sa.UniqueConstraint("reference", name=op.f(f"uq_{t}_reference")),
        sa.UniqueConstraint("booking_id", name=op.f(f"uq_{t}_booking_id")),
    )
    op.create_index(op.f(f"ix_{t}_status"), t, ["status"])
    op.create_index(op.f(f"ix_{t}_tenant_id"), t, ["tenant_id"])
    op.create_index(op.f(f"ix_{t}_property_id"), t, ["property_id"])
    op.create_index(op.f(f"ix_{t}_space_id"), t, ["space_id"])
    op.create_index(
        "uq_payment_holds_payment_reference",
        t,
        ["payment_reference"],
        unique=True,
        postgresql_where=sa.text("payment_reference IS NOT NULL"),
    )
    op.create_index(
        "ix_payment_holds_active_expiry",
        t,
        ["expires_at"],
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )

    # Earlier revisions in the same transaction may leave deferred trigger events on
    # allocations; fire them now, or the table cannot be altered below.
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")
    a = "allocations"
    op.add_column(a, sa.Column("payment_hold_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f(f"fk_{a}_payment_hold_id_payment_holds"),
        a,
        "payment_holds",
        ["payment_hold_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(op.f("uq_allocations_payment_hold"), a, ["payment_hold_id"])
    op.drop_constraint(op.f("ck_allocations_source_type_valid"), a, type_="check")
    op.create_check_constraint(
        op.f("ck_allocations_source_type_valid"),
        a,
        "source_type IN ('BOOKING', 'CONTRACT', 'PAYMENT_HOLD')",
    )
    op.drop_constraint(op.f("ck_allocations_source"), a, type_="check")
    op.create_check_constraint(op.f("ck_allocations_source"), a, NEW_ALLOCATION_SOURCE)

    # Last: this update queues deferred trigger events, after which the table cannot be
    # altered in the same transaction.
    op.execute(
        "UPDATE allocations SET is_active = false, released_at = now(), "
        "release_reason = 'PENDING_REQUEST' "
        "WHERE is_active AND booking_id IN (SELECT id FROM bookings WHERE status = 'PENDING')"
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        DECLARE pending integer; leads integer; holds integer;
        BEGIN
          SELECT count(*) INTO pending FROM bookings WHERE status = 'PENDING';
          SELECT count(*) INTO leads FROM leads
          WHERE source = 'CLIENT_APP' OR requested_term_months IS NOT NULL;
          SELECT count(*) INTO holds FROM payment_holds;
          IF pending + leads + holds > 0 THEN
            RAISE EXCEPTION
              'Cannot downgrade past 0023: % pending booking request(s), % client-app '
              'lead(s) and % checkout hold(s) exist. Before 0023 a pending booking held its '
              'space; this downgrade will not guess how to restore that.',
              pending, leads, holds;
          END IF;
        END $$;
        """
    )
    # Earlier revisions in the same transaction may leave deferred trigger events on
    # allocations; fire them now, or the table cannot be altered below.
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")
    a = "allocations"
    op.drop_constraint(op.f("ck_allocations_source"), a, type_="check")
    op.create_check_constraint(op.f("ck_allocations_source"), a, OLD_ALLOCATION_SOURCE)
    op.drop_constraint(op.f("ck_allocations_source_type_valid"), a, type_="check")
    op.create_check_constraint(
        op.f("ck_allocations_source_type_valid"), a, "source_type IN ('BOOKING', 'CONTRACT')"
    )
    op.drop_constraint(op.f("uq_allocations_payment_hold"), a, type_="unique")
    op.drop_constraint(op.f(f"fk_{a}_payment_hold_id_payment_holds"), a, type_="foreignkey")
    op.drop_column(a, "payment_hold_id")
    op.drop_table("payment_holds")
    op.execute("DROP SEQUENCE payment_hold_reference_seq")

    op.drop_constraint(op.f("ck_leads_term_months_range"), "leads", type_="check")
    op.drop_column("leads", "requested_term_months")
    op.drop_constraint(op.f("ck_leads_source_valid"), "leads", type_="check")
    op.create_check_constraint(op.f("ck_leads_source_valid"), "leads", f"source IN ({OLD_SOURCES})")

    op.execute("ALTER TABLE bookings DROP CONSTRAINT ex_bookings_space_time")
    op.execute(
        "ALTER TABLE bookings ADD CONSTRAINT ex_bookings_space_time EXCLUDE USING gist "
        "(space_id WITH =, tstzrange(start_at, end_at, '[)') WITH &&) "
        "WHERE (status <> 'CANCELLED')"
    )
