"""Invoices and payments for tenant accounts (BR-009, BR-018, BR-019, BR-029).

Built without the open billing decisions (D5-D7), so nothing is invented:
- An admin creates each invoice for an account (optionally one contract). Lines are the
  three charge categories (BR-009); amounts are as entered. A Space Fee line can be
  suggested from the contract's agreed rent, and Reimbursement Fee lines come only from
  reviewed expenses, each invoiced at most once (BR-029). No tax, frequency, due-date rule,
  late fee or discount is calculated; the total is the sum of the lines (BR-018).
- Payments are recorded against one issued invoice and count only once verified (BR-019);
  the same reference is refused twice, and payments never exceed the invoice total.
- Security deposits and advances are not invoiced or paid here (BR-010, BR-030).
References: INV-YYYY-NNNNNN and PAY-YYYY-NNNNNN (D7's proposed format).
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Sequence,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import ChargeCategory, InvoiceStatus, PaymentMethod, PaymentStatus
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin, enum_check

invoice_reference_seq = Sequence("invoice_reference_seq", metadata=Base.metadata)
payment_reference_seq = Sequence("payment_reference_seq", metadata=Base.metadata)


class Invoice(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "invoices"
    __table_args__ = (
        enum_check("status", InvoiceStatus),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_code"),
        CheckConstraint("due_date IS NULL OR due_date >= invoice_date", name="due_after_date"),
        CheckConstraint(
            "period_end IS NULL OR period_start IS NULL OR period_end >= period_start",
            name="period_ordered",
        ),
        CheckConstraint("(status = 'DRAFT') = (issued_at IS NULL)", name="issued_unless_draft"),
        CheckConstraint(
            "(status = 'CANCELLED') = (cancelled_at IS NOT NULL AND cancel_reason IS NOT NULL)",
            name="cancellation_recorded",
        ),
    )

    reference: Mapped[str] = mapped_column(
        String(20),
        unique=True,
        nullable=False,
        server_default=text(
            "('INV-' || to_char(now(), 'YYYY') || '-' "
            "|| lpad(nextval('invoice_reference_seq')::text, 6, '0'))"
        ),
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    contract_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenant_occupancies.id", ondelete="RESTRICT"), index=True
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=InvoiceStatus.DRAFT.value, index=True
    )
    invoice_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date | None] = mapped_column(Date)
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="INR")
    notes: Mapped[str | None] = mapped_column(Text)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    issued_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )


class InvoiceLine(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One charge. A Reimbursement Fee line may carry the reviewed expense it bills; that
    expense is invoiced at most once (released again only when the invoice is cancelled)."""

    __tablename__ = "invoice_lines"
    __table_args__ = (
        enum_check("category", ChargeCategory),
        CheckConstraint("amount >= 0", name="amount_not_negative"),
        CheckConstraint(
            "expense_id IS NULL OR category = 'REIMBURSEMENT_FEE'", name="expense_is_reimbursement"
        ),
        Index(
            "uq_invoice_lines_expense",
            "expense_id",
            unique=True,
            postgresql_where=text("expense_id IS NOT NULL AND NOT expense_released"),
        ),
    )

    invoice_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    category: Mapped[str] = mapped_column(String(24), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    expense_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("reimbursement_expenses.id", ondelete="RESTRICT")
    )
    expense_released: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class Payment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "payments"
    __table_args__ = (
        enum_check("status", PaymentStatus),
        enum_check("method", PaymentMethod),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint(
            "(status = 'VERIFIED') = (verified_at IS NOT NULL)", name="verification_recorded"
        ),
        CheckConstraint(
            "(status = 'REJECTED') = (rejected_at IS NOT NULL AND reject_reason IS NOT NULL)",
            name="rejection_recorded",
        ),
        # The same bank / UPI / cheque reference is refused twice for one invoice (BR-019).
        Index(
            "uq_payments_external_reference",
            "invoice_id",
            "method",
            "external_reference",
            unique=True,
            postgresql_where=text("external_reference IS NOT NULL AND status <> 'REJECTED'"),
        ),
    )

    reference: Mapped[str] = mapped_column(
        String(20),
        unique=True,
        nullable=False,
        server_default=text(
            "('PAY-' || to_char(now(), 'YYYY') || '-' "
            "|| lpad(nextval('payment_reference_seq')::text, 6, '0'))"
        ),
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("invoices.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="INR")
    paid_on: Mapped[date] = mapped_column(Date, nullable=False)
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    external_reference: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=PaymentStatus.RECORDED.value, index=True
    )
    notes: Mapped[str | None] = mapped_column(Text)
    recorded_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reject_reason: Mapped[str | None] = mapped_column(Text)
