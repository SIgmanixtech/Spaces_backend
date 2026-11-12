"""Contract categories, holds, renewals, parking links and addenda (Phases B2 and B3)

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-12 15:00:00.000000+00:00

Owner decisions of 2 Nov 2026: D8 (categories), D9.2 (templates by category), D10 (category
details), D12.3-D12.6 (conversion and holds), LC-2 (renewal), LC-3/LC-4 (addenda).

Additive. No existing value is changed: every new column is nullable or has a default, so
existing contracts and templates stay uncategorised (D8.6) and no hold dates are set.

- tenant_occupancies: category (five values), main_contract_id (Parking -> main contract),
  renews_contract_id, hold_until, the customer's hold-extension request, the D10 details
  (expected headcount, occupants, government department and order reference, vehicle
  registration) and lock-in / notice period as printed text. Checks: category valid, only
  Parking links to a main contract, no self links, counts positive. Indexes for links, holds,
  ending-soon and category; a booking becomes at most one live contract (partial unique).
- contract_templates: category; the version and one-active-template rules now include the
  category. They become unique indexes on expressions (coalesce), which every supported
  PostgreSQL accepts; NULLS NOT DISTINCT is not used.
- contract_addenda (new): AD-YYYY-NNNNNN from addendum_reference_seq; on one contract
  (RESTRICT); DRAFT -> SIGNED or CANCELLED (recorded); signed copy evidence inline.

Before the booking index is created the upgrade stops, changing nothing, if a booking is
already linked to two live contracts. The downgrade refuses while any B2/B3 value exists.
Hand-written; tests/test_migrations.py checks it against the models.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CATEGORIES = "'SPACE', 'OFFICE', 'GOVERNMENT', 'PARKING', 'COMMERCIAL'"
ADDENDUM_STATUSES = "'DRAFT', 'SIGNED', 'CANCELLED'"
NO_PROPERTY = "'00000000-0000-0000-0000-000000000000'::uuid"
CONTRACT_COLUMNS = (
    ("category", sa.String(length=16)),
    ("main_contract_id", sa.Uuid()),
    ("renews_contract_id", sa.Uuid()),
    ("hold_until", sa.Date()),
    ("hold_extension_requested_until", sa.Date()),
    ("hold_extension_reason", sa.Text()),
    ("expected_headcount", sa.Integer()),
    ("occupants", sa.Integer()),
    ("government_department", sa.String(length=200)),
    ("government_order_reference", sa.String(length=100)),
    ("vehicle_registration", sa.String(length=20)),
    ("lock_in_period", sa.String(length=100)),
    ("notice_period", sa.String(length=100)),
)


def _now(name: str) -> sa.Column:
    return sa.Column(
        name, sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    t = "tenant_occupancies"
    for name, type_ in CONTRACT_COLUMNS:
        op.add_column(t, sa.Column(name, type_, nullable=True))
    for column in ("main_contract_id", "renews_contract_id"):
        op.create_foreign_key(
            op.f(f"fk_{t}_{column}_{t}"), t, t, [column], ["id"], ondelete="RESTRICT"
        )
    op.create_check_constraint(
        op.f(f"ck_{t}_category_valid"), t, f"category IS NULL OR category IN ({CATEGORIES})"
    )
    op.create_check_constraint(
        op.f(f"ck_{t}_main_only_for_parking"),
        t,
        "main_contract_id IS NULL OR category = 'PARKING'",
    )
    op.create_check_constraint(
        op.f(f"ck_{t}_not_own_main"), t, "main_contract_id IS NULL OR main_contract_id <> id"
    )
    op.create_check_constraint(
        op.f(f"ck_{t}_not_own_renewal"),
        t,
        "renews_contract_id IS NULL OR renews_contract_id <> id",
    )
    op.create_check_constraint(
        op.f(f"ck_{t}_counts_positive"),
        t,
        "(expected_headcount IS NULL OR expected_headcount > 0)"
        " AND (occupants IS NULL OR occupants > 0)",
    )
    op.create_index(
        "ix_tenant_occupancies_main_contract_id",
        t,
        ["main_contract_id"],
        postgresql_where=sa.text("main_contract_id IS NOT NULL"),
    )
    op.create_index(
        "uq_tenant_occupancies_renews",
        t,
        ["renews_contract_id"],
        unique=True,
        postgresql_where=sa.text("renews_contract_id IS NOT NULL AND status <> 'CANCELLED'"),
    )
    op.create_index(
        "ix_tenant_occupancies_hold_until",
        t,
        ["hold_until"],
        postgresql_where=sa.text("status = 'PENDING' AND hold_until IS NOT NULL"),
    )
    op.create_index(
        "ix_tenant_occupancies_ending",
        t,
        ["end_date"],
        postgresql_where=sa.text("status = 'ACTIVE' AND end_date IS NOT NULL"),
    )
    op.create_index("ix_tenant_occupancies_category", t, ["category"])
    # Stop, changing nothing, rather than fail half-way on existing data.
    op.execute(
        """
        DO $$
        DECLARE twice integer;
        BEGIN
          SELECT count(*) INTO twice FROM (
            SELECT booking_id FROM tenant_occupancies
            WHERE booking_id IS NOT NULL AND status <> 'CANCELLED'
            GROUP BY booking_id HAVING count(*) > 1
          ) AS d;
          IF twice > 0 THEN
            RAISE EXCEPTION
              'Cannot apply 0022: % booking(s) are linked to more than one live contract. '
              'Cancel the extra contracts first.', twice;
          END IF;
        END $$;
        """
    )
    op.create_index(
        "uq_tenant_occupancies_booking",
        t,
        ["booking_id"],
        unique=True,
        postgresql_where=sa.text("booking_id IS NOT NULL AND status <> 'CANCELLED'"),
    )

    t = "contract_templates"
    op.add_column(t, sa.Column("category", sa.String(length=16), nullable=True))
    op.create_check_constraint(
        op.f(f"ck_{t}_category_valid"), t, f"category IS NULL OR category IN ({CATEGORIES})"
    )
    op.drop_constraint("uq_contract_templates_version", t, type_="unique")
    op.create_index(
        "uq_contract_templates_version",
        t,
        [
            "contract_type",
            sa.text("coalesce(category, '')"),
            sa.text(f"coalesce(property_id, {NO_PROPERTY})"),
            "version",
        ],
        unique=True,
    )
    op.drop_index("uq_contract_templates_active", table_name=t)
    op.create_index(
        "uq_contract_templates_active",
        t,
        [
            "contract_type",
            sa.text("coalesce(category, '')"),
            sa.text(f"coalesce(property_id, {NO_PROPERTY})"),
        ],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )

    op.execute("CREATE SEQUENCE addendum_reference_seq")
    t = "contract_addenda"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "reference",
            sa.String(length=20),
            server_default=sa.text(
                "('AD-' || to_char(now(), 'YYYY') || '-' "
                "|| lpad(nextval('addendum_reference_seq')::text, 6, '0'))"
            ),
            nullable=False,
        ),
        sa.Column("contract_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="DRAFT", nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "changes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "previous",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("bucket", sa.String(length=100), nullable=True),
        sa.Column("object_path", sa.String(length=500), nullable=True),
        sa.Column("file_name", sa.String(length=255), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("signed_on", sa.Date(), nullable=True),
        sa.Column(
            "signers",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancellation_reason", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        _now("created_at"),
        _now("updated_at"),
        sa.CheckConstraint(f"status IN ({ADDENDUM_STATUSES})", name=op.f(f"ck_{t}_status_valid")),
        sa.CheckConstraint(
            "(status = 'SIGNED') = (signed_at IS NOT NULL)",
            name=op.f(f"ck_{t}_signature_recorded"),
        ),
        sa.CheckConstraint(
            "(status = 'CANCELLED') = (cancelled_at IS NOT NULL)",
            name=op.f(f"ck_{t}_cancellation_recorded"),
        ),
        sa.CheckConstraint(
            "status <> 'SIGNED' OR (object_path IS NOT NULL AND signed_on IS NOT NULL"
            " AND jsonb_array_length(signers) > 0)",
            name=op.f(f"ck_{t}_signed_has_evidence"),
        ),
        sa.ForeignKeyConstraint(
            ["contract_id"],
            ["tenant_occupancies.id"],
            name=op.f(f"fk_{t}_contract_id_tenant_occupancies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["app_users.id"],
            name=op.f(f"fk_{t}_created_by_app_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{t}")),
        sa.UniqueConstraint("reference", name=op.f(f"uq_{t}_reference")),
        sa.UniqueConstraint("object_path", name=op.f(f"uq_{t}_object_path")),
    )
    op.create_index(op.f(f"ix_{t}_contract_id"), t, ["contract_id"])


def downgrade() -> None:
    # Like the earlier revisions: refuse rather than silently discard what 0022 recorded.
    op.execute(
        """
        DO $$
        DECLARE used integer; templates integer; addenda integer;
        BEGIN
          SELECT count(*) INTO used FROM tenant_occupancies
          WHERE category IS NOT NULL OR main_contract_id IS NOT NULL
             OR renews_contract_id IS NOT NULL OR hold_until IS NOT NULL
             OR hold_extension_requested_until IS NOT NULL
             OR expected_headcount IS NOT NULL OR occupants IS NOT NULL
             OR government_department IS NOT NULL OR government_order_reference IS NOT NULL
             OR vehicle_registration IS NOT NULL OR lock_in_period IS NOT NULL
             OR notice_period IS NOT NULL;
          SELECT count(*) INTO templates FROM contract_templates WHERE category IS NOT NULL;
          SELECT count(*) INTO addenda FROM contract_addenda;
          IF used + templates + addenda > 0 THEN
            RAISE EXCEPTION
              'Cannot downgrade past 0022: % contract(s) and % template(s) use categories, '
              'links, holds or category details, and % addend(a) exist. Clear them '
              'deliberately first; this migration will not discard them.',
              used, templates, addenda;
          END IF;
        END $$;
        """
    )
    op.drop_table("contract_addenda")
    op.execute("DROP SEQUENCE addendum_reference_seq")

    t = "contract_templates"
    op.drop_index("uq_contract_templates_active", table_name=t)
    op.drop_index("uq_contract_templates_version", table_name=t)
    op.create_unique_constraint(
        "uq_contract_templates_version", t, ["contract_type", "property_id", "version"]
    )
    op.create_index(
        "uq_contract_templates_active",
        t,
        ["contract_type", sa.text(f"coalesce(property_id, {NO_PROPERTY})")],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.drop_constraint(op.f(f"ck_{t}_category_valid"), t, type_="check")
    op.drop_column(t, "category")

    t = "tenant_occupancies"
    for index in (
        "uq_tenant_occupancies_booking",
        "ix_tenant_occupancies_category",
        "ix_tenant_occupancies_ending",
        "ix_tenant_occupancies_hold_until",
        "uq_tenant_occupancies_renews",
        "ix_tenant_occupancies_main_contract_id",
    ):
        op.drop_index(index, table_name=t)
    for check in (
        "counts_positive",
        "not_own_renewal",
        "not_own_main",
        "main_only_for_parking",
        "category_valid",
    ):
        op.drop_constraint(op.f(f"ck_{t}_{check}"), t, type_="check")
    for column in ("renews_contract_id", "main_contract_id"):
        op.drop_constraint(op.f(f"fk_{t}_{column}_{t}"), t, type_="foreignkey")
    for name, _ in reversed(CONTRACT_COLUMNS):
        op.drop_column(t, name)
