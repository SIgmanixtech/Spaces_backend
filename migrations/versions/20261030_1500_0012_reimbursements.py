"""Reimbursement Fee: expense types, terms (property defaults and contract terms), expenses

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-30 15:00:00.000000+00:00

Additive. Nothing existing changes meaning; existing pricing rules, contracts and their
additional charges are untouched.

1. reimbursement_expense_types: the catalogue (names unique ignoring case). Seeded with
   the six approved starting types; admins add more.
2. reimbursement_terms: how an expense type is charged. contract_id NULL = a property
   default; otherwise that contract's agreed term. FIXED terms carry an amount; RECURRING
   terms a billing period (the existing ones except CUSTOM). Own tax treatment and rate.
3. reimbursement_expenses: actual amounts entered per contract and period, with the
   original amount, optional receipt (an existing stored file) and review status; only
   REVIEWED ones may later reach an invoice.

Hand-written; tests/test_migrations.py checks it against the models.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STARTING_TYPES = (
    ("Electricity", None),
    ("Water", None),
    ("Internet", None),
    ("Diesel generator / power backup", None),
    ("Waste management", None),
    (
        "Other shared operational expenses",
        "Other shared operational expenses paid by SIGMANIX on behalf of tenants.",
    ),
)
PERIODS = "'HOURLY', 'HALF_DAY', 'FULL_DAY', 'DAILY', 'WEEKLY', 'MONTHLY'"
TAX = (
    "(tax_treatment = 'EXEMPT') = (tax_rate IS NULL)"
    " AND (tax_rate IS NULL OR (tax_rate >= 0 AND tax_rate <= 100))"
)
TAX_VALID = "tax_treatment IN ('EXEMPT', 'INCLUDED', 'ADDITIONAL')"


def _timestamps() -> list[sa.Column]:
    return [
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
    ]


def _fk(table: str, column: str, target: str, ondelete: str) -> sa.ForeignKeyConstraint:
    referred = target.split(".")[0]
    return sa.ForeignKeyConstraint(
        [column], [target], name=op.f(f"fk_{table}_{column}_{referred}"), ondelete=ondelete
    )


def upgrade() -> None:
    types = op.create_table(
        "reimbursement_expense_types",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        *_timestamps(),
        _fk("reimbursement_expense_types", "created_by", "app_users.id", "SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reimbursement_expense_types")),
    )
    op.create_index(
        "uq_reimbursement_expense_types_name",
        "reimbursement_expense_types",
        [sa.text("lower(name)")],
        unique=True,
    )

    t = "reimbursement_terms"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("property_id", sa.Uuid(), nullable=False),
        sa.Column("contract_id", sa.Uuid(), nullable=True),
        sa.Column("expense_type_id", sa.Uuid(), nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("recurrence", sa.String(length=16), nullable=False),
        sa.Column("billing_period", sa.String(length=16), nullable=True),
        sa.Column("tax_treatment", sa.String(length=16), server_default="EXEMPT", nullable=False),
        sa.Column("tax_rate", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("method IN ('FIXED', 'ACTUAL')", name=op.f(f"ck_{t}_method_valid")),
        sa.CheckConstraint(
            "recurrence IN ('RECURRING', 'ONE_TIME')", name=op.f(f"ck_{t}_recurrence_valid")
        ),
        sa.CheckConstraint(TAX_VALID, name=op.f(f"ck_{t}_tax_treatment_valid")),
        sa.CheckConstraint(
            f"billing_period IS NULL OR billing_period IN ({PERIODS})",
            name=op.f(f"ck_{t}_period_valid"),
        ),
        sa.CheckConstraint(
            "(recurrence = 'RECURRING') = (billing_period IS NOT NULL)",
            name=op.f(f"ck_{t}_period_when_recurring"),
        ),
        sa.CheckConstraint(
            "(method = 'FIXED') = (amount IS NOT NULL)", name=op.f(f"ck_{t}_amount_when_fixed")
        ),
        sa.CheckConstraint("amount IS NULL OR amount > 0", name=op.f(f"ck_{t}_amount_positive")),
        sa.CheckConstraint(TAX, name=op.f(f"ck_{t}_tax")),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=op.f(f"ck_{t}_currency_code")),
        _fk(t, "property_id", "properties.id", "RESTRICT"),
        _fk(t, "contract_id", "tenant_occupancies.id", "RESTRICT"),
        _fk(t, "expense_type_id", "reimbursement_expense_types.id", "RESTRICT"),
        _fk(t, "created_by", "app_users.id", "SET NULL"),
        _fk(t, "updated_by", "app_users.id", "SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{t}")),
    )
    op.create_index(op.f(f"ix_{t}_property_id"), t, ["property_id"])
    op.create_index(
        "uq_reimbursement_terms_default",
        t,
        ["property_id", "expense_type_id"],
        unique=True,
        postgresql_where=sa.text("contract_id IS NULL"),
    )
    op.create_index(
        "uq_reimbursement_terms_contract",
        t,
        ["contract_id", "expense_type_id"],
        unique=True,
        postgresql_where=sa.text("contract_id IS NOT NULL"),
    )

    e = "reimbursement_expenses"
    op.create_table(
        e,
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("property_id", sa.Uuid(), nullable=False),
        sa.Column("contract_id", sa.Uuid(), nullable=False),
        sa.Column("expense_type_id", sa.Uuid(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("original_amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("one_time", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("tax_treatment", sa.String(length=16), server_default="EXEMPT", nullable=False),
        sa.Column("tax_rate", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("receipt_file_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="PENDING_REVIEW", nullable=False),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("reviewed_by", sa.Uuid(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("entered_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('PENDING_REVIEW', 'REVIEWED', 'REJECTED')",
            name=op.f(f"ck_{e}_status_valid"),
        ),
        sa.CheckConstraint(TAX_VALID, name=op.f(f"ck_{e}_tax_treatment_valid")),
        sa.CheckConstraint("period_end >= period_start", name=op.f(f"ck_{e}_period_ordered")),
        sa.CheckConstraint(
            "amount > 0 AND original_amount > 0", name=op.f(f"ck_{e}_amount_positive")
        ),
        sa.CheckConstraint(
            "(status = 'PENDING_REVIEW') = (reviewed_at IS NULL)",
            name=op.f(f"ck_{e}_reviewed_when_decided"),
        ),
        sa.CheckConstraint(TAX, name=op.f(f"ck_{e}_tax")),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=op.f(f"ck_{e}_currency_code")),
        _fk(e, "property_id", "properties.id", "RESTRICT"),
        _fk(e, "contract_id", "tenant_occupancies.id", "RESTRICT"),
        _fk(e, "expense_type_id", "reimbursement_expense_types.id", "RESTRICT"),
        _fk(e, "receipt_file_id", "files.id", "RESTRICT"),
        _fk(e, "reviewed_by", "app_users.id", "SET NULL"),
        _fk(e, "entered_by", "app_users.id", "SET NULL"),
        _fk(e, "updated_by", "app_users.id", "SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{e}")),
    )
    op.create_index(op.f(f"ix_{e}_property_id"), e, ["property_id"])
    op.create_index("ix_reimbursement_expenses_contract_status", e, ["contract_id", "status"])

    op.bulk_insert(
        types, [{"name": name, "description": description} for name, description in STARTING_TYPES]
    )


def downgrade() -> None:
    # Like 0002-0009: refuse rather than silently discard what admins have entered. Only
    # the six untouched starting types (seeded above) may be dropped without asking.
    starting = ", ".join("'" + name.lower().replace("'", "''") + "'" for name, _ in STARTING_TYPES)
    op.execute(
        f"""
        DO $$
        DECLARE terms integer; expenses integer; added integer;
        BEGIN
          SELECT count(*) INTO terms FROM reimbursement_terms;
          SELECT count(*) INTO expenses FROM reimbursement_expenses;
          SELECT count(*) INTO added FROM reimbursement_expense_types
            WHERE lower(name) NOT IN ({starting});
          IF terms + expenses + added > 0 THEN
            RAISE EXCEPTION
              'Cannot downgrade past 0012: % reimbursement term(s), % expense(s) and % '
              'added or renamed expense type(s) exist. Export or clear them deliberately '
              'first; this migration will not discard them.', terms, expenses, added;
          END IF;
        END $$;
        """
    )
    op.drop_table("reimbursement_expenses")
    op.drop_table("reimbursement_terms")
    op.drop_table("reimbursement_expense_types")
