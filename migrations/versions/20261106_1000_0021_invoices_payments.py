"""Invoices, invoice lines and payments for tenant accounts (BR-009, BR-018, BR-019)

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-06 10:00:00.000000+00:00

Additive. Three new tables and two new sequences; no existing row, constraint or column
is changed.

- invoices: INV-YYYY-NNNNNN from invoice_reference_seq; account (RESTRICT), optional
  contract (RESTRICT); DRAFT -> ISSUED -> CANCELLED (issue and cancellation recorded);
  dates as entered (due date not before the invoice date). No tax or total is stored: the
  total is the sum of the lines.
- invoice_lines: the three charge categories (BR-009), amounts not negative; a
  Reimbursement Fee line may carry its reviewed expense, invoiced at most once (partial
  unique index, released only by cancelling the invoice).
- payments: PAY-YYYY-NNNNNN from payment_reference_seq; against one invoice (RESTRICT);
  RECORDED -> VERIFIED or REJECTED (recorded); amount positive; the same method and
  external reference refused twice per invoice (partial unique index).

The downgrade refuses while any invoice or payment exists, like the earlier revisions.
Hand-written; tests/test_migrations.py checks it against the models.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INVOICE_STATUSES = "'DRAFT', 'ISSUED', 'CANCELLED'"
CATEGORIES = "'SPACE_FEE', 'MAINTENANCE_FEE', 'REIMBURSEMENT_FEE'"
PAYMENT_STATUSES = "'RECORDED', 'VERIFIED', 'REJECTED'"
METHODS = "'BANK_TRANSFER', 'UPI', 'CHEQUE', 'CASH', 'CARD', 'OTHER'"


def _now(name: str) -> sa.Column:
    return sa.Column(
        name, sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def _user_fk(table: str, column: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column], ["app_users.id"], name=op.f(f"fk_{table}_{column}_app_users"), ondelete="SET NULL"
    )


def _reference(prefix: str, seq: str) -> sa.Column:
    return sa.Column(
        "reference",
        sa.String(length=20),
        server_default=sa.text(
            f"('{prefix}-' || to_char(now(), 'YYYY') || '-' "
            f"|| lpad(nextval('{seq}')::text, 6, '0'))"
        ),
        nullable=False,
    )


def upgrade() -> None:
    op.execute("CREATE SEQUENCE invoice_reference_seq")
    op.execute("CREATE SEQUENCE payment_reference_seq")

    t = "invoices"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        _reference("INV", "invoice_reference_seq"),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("contract_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="DRAFT", nullable=False),
        sa.Column("invoice_date", sa.Date(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("issued_by", sa.Uuid(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        _now("created_at"),
        _now("updated_at"),
        sa.CheckConstraint(f"status IN ({INVOICE_STATUSES})", name=op.f(f"ck_{t}_status_valid")),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=op.f(f"ck_{t}_currency_code")),
        sa.CheckConstraint(
            "due_date IS NULL OR due_date >= invoice_date", name=op.f(f"ck_{t}_due_after_date")
        ),
        sa.CheckConstraint(
            "period_end IS NULL OR period_start IS NULL OR period_end >= period_start",
            name=op.f(f"ck_{t}_period_ordered"),
        ),
        sa.CheckConstraint(
            "(status = 'DRAFT') = (issued_at IS NULL)", name=op.f(f"ck_{t}_issued_unless_draft")
        ),
        sa.CheckConstraint(
            "(status = 'CANCELLED') = (cancelled_at IS NOT NULL AND cancel_reason IS NOT NULL)",
            name=op.f(f"ck_{t}_cancellation_recorded"),
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f(f"fk_{t}_account_id_accounts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["contract_id"],
            ["tenant_occupancies.id"],
            name=op.f(f"fk_{t}_contract_id_tenant_occupancies"),
            ondelete="RESTRICT",
        ),
        _user_fk(t, "issued_by"),
        _user_fk(t, "created_by"),
        _user_fk(t, "updated_by"),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{t}")),
        sa.UniqueConstraint("reference", name=op.f(f"uq_{t}_reference")),
    )
    for column in ("account_id", "contract_id", "status"):
        op.create_index(op.f(f"ix_{t}_{column}"), t, [column])

    t = "invoice_lines"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("invoice_id", sa.Uuid(), nullable=False),
        sa.Column("category", sa.String(length=24), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("expense_id", sa.Uuid(), nullable=True),
        sa.Column(
            "expense_released", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        _now("created_at"),
        _now("updated_at"),
        sa.CheckConstraint(f"category IN ({CATEGORIES})", name=op.f(f"ck_{t}_category_valid")),
        sa.CheckConstraint("amount >= 0", name=op.f(f"ck_{t}_amount_not_negative")),
        sa.CheckConstraint(
            "expense_id IS NULL OR category = 'REIMBURSEMENT_FEE'",
            name=op.f(f"ck_{t}_expense_is_reimbursement"),
        ),
        sa.ForeignKeyConstraint(
            ["invoice_id"],
            ["invoices.id"],
            name=op.f(f"fk_{t}_invoice_id_invoices"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["expense_id"],
            ["reimbursement_expenses.id"],
            name=op.f(f"fk_{t}_expense_id_reimbursement_expenses"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{t}")),
    )
    op.create_index(op.f(f"ix_{t}_invoice_id"), t, ["invoice_id"])
    op.create_index(
        "uq_invoice_lines_expense",
        t,
        ["expense_id"],
        unique=True,
        postgresql_where=sa.text("expense_id IS NOT NULL AND NOT expense_released"),
    )

    t = "payments"
    op.create_table(
        t,
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        _reference("PAY", "payment_reference_seq"),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("invoice_id", sa.Uuid(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("paid_on", sa.Date(), nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("external_reference", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="RECORDED", nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("recorded_by", sa.Uuid(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_by", sa.Uuid(), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        _now("created_at"),
        _now("updated_at"),
        sa.CheckConstraint(f"status IN ({PAYMENT_STATUSES})", name=op.f(f"ck_{t}_status_valid")),
        sa.CheckConstraint(f"method IN ({METHODS})", name=op.f(f"ck_{t}_method_valid")),
        sa.CheckConstraint("amount > 0", name=op.f(f"ck_{t}_amount_positive")),
        sa.CheckConstraint(
            "(status = 'VERIFIED') = (verified_at IS NOT NULL)",
            name=op.f(f"ck_{t}_verification_recorded"),
        ),
        sa.CheckConstraint(
            "(status = 'REJECTED') = (rejected_at IS NOT NULL AND reject_reason IS NOT NULL)",
            name=op.f(f"ck_{t}_rejection_recorded"),
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f(f"fk_{t}_account_id_accounts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["invoice_id"],
            ["invoices.id"],
            name=op.f(f"fk_{t}_invoice_id_invoices"),
            ondelete="RESTRICT",
        ),
        _user_fk(t, "recorded_by"),
        _user_fk(t, "verified_by"),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{t}")),
        sa.UniqueConstraint("reference", name=op.f(f"uq_{t}_reference")),
    )
    for column in ("account_id", "invoice_id", "status"):
        op.create_index(op.f(f"ix_{t}_{column}"), t, [column])
    op.create_index(
        "uq_payments_external_reference",
        t,
        ["invoice_id", "method", "external_reference"],
        unique=True,
        postgresql_where=sa.text("external_reference IS NOT NULL AND status <> 'REJECTED'"),
    )


def downgrade() -> None:
    # Like the earlier revisions: refuse rather than silently discard billing records.
    op.execute(
        """
        DO $$
        DECLARE invoices integer; payments integer;
        BEGIN
          SELECT count(*) INTO invoices FROM invoices;
          SELECT count(*) INTO payments FROM payments;
          IF invoices + payments > 0 THEN
            RAISE EXCEPTION
              'Cannot downgrade past 0021: % invoice(s) and % payment(s) exist. Export or '
              'clear them deliberately first; this migration will not discard them.',
              invoices, payments;
          END IF;
        END $$;
        """
    )
    op.drop_table("payments")
    op.drop_table("invoice_lines")
    op.drop_table("invoices")
    op.execute("DROP SEQUENCE payment_reference_seq")
    op.execute("DROP SEQUENCE invoice_reference_seq")
