"""Reimbursement Fee (business rule BR-029): expenses SIGMANIX pays on a tenant's behalf
(electricity, water, internet...) and recovers.

- ReimbursementExpenseType: the catalogue. Admins add types; names are unique ignoring case.
- ReimbursementTerm: how a type is charged. With no contract it is a property default;
  with a contract it is that contract's agreed term. Defaults never change a contract's
  terms by themselves; they are copied only when an admin applies them.
- ReimbursementExpense: an actual amount the admin enters for a contract and period. It
  stays PENDING_REVIEW until reviewed; only REVIEWED expenses may reach an invoice.

Tax is set per term or expense (treatment and rate), never copied from the Space Fee.
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
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import (
    BillingPeriod,
    ReimbursementMethod,
    ReimbursementRecurrence,
    ReimbursementStatus,
    TaxTreatment,
)
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin, enum_check

# Periods a recurring reimbursement can follow: the existing billing periods, except
# CUSTOM (which needs its own label and has no fixed length).
REIMBURSEMENT_PERIODS = tuple(p for p in BillingPeriod if p != BillingPeriod.CUSTOM)
_PERIODS = ", ".join(f"'{p.value}'" for p in REIMBURSEMENT_PERIODS)
_TAX = (
    "(tax_treatment = 'EXEMPT') = (tax_rate IS NULL)"
    " AND (tax_rate IS NULL OR (tax_rate >= 0 AND tax_rate <= 100))"
)


class ReimbursementExpenseType(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "reimbursement_expense_types"
    __table_args__ = (
        Index("uq_reimbursement_expense_types_name", func.lower(text("name")), unique=True),
    )

    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )


class ReimbursementTerm(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "reimbursement_terms"
    __table_args__ = (
        enum_check("method", ReimbursementMethod),
        enum_check("recurrence", ReimbursementRecurrence),
        enum_check("tax_treatment", TaxTreatment),
        CheckConstraint(
            f"billing_period IS NULL OR billing_period IN ({_PERIODS})", name="period_valid"
        ),
        CheckConstraint(
            "(recurrence = 'RECURRING') = (billing_period IS NOT NULL)",
            name="period_when_recurring",
        ),
        CheckConstraint("(method = 'FIXED') = (amount IS NOT NULL)", name="amount_when_fixed"),
        CheckConstraint("amount IS NULL OR amount > 0", name="amount_positive"),
        CheckConstraint(_TAX, name="tax"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_code"),
        # One default per type per property; one agreed term per type per contract.
        Index(
            "uq_reimbursement_terms_default",
            "property_id",
            "expense_type_id",
            unique=True,
            postgresql_where=text("contract_id IS NULL"),
        ),
        Index(
            "uq_reimbursement_terms_contract",
            "contract_id",
            "expense_type_id",
            unique=True,
            postgresql_where=text("contract_id IS NOT NULL"),
        ),
    )

    property_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("properties.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    contract_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenant_occupancies.id", ondelete="RESTRICT")
    )
    expense_type_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reimbursement_expense_types.id", ondelete="RESTRICT"), nullable=False
    )
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    recurrence: Mapped[str] = mapped_column(String(16), nullable=False)
    billing_period: Mapped[str | None] = mapped_column(String(16))
    tax_treatment: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=TaxTreatment.EXEMPT.value
    )
    tax_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="INR")
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )


class ReimbursementExpense(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "reimbursement_expenses"
    __table_args__ = (
        enum_check("status", ReimbursementStatus),
        enum_check("tax_treatment", TaxTreatment),
        CheckConstraint("period_end >= period_start", name="period_ordered"),
        CheckConstraint("amount > 0 AND original_amount > 0", name="amount_positive"),
        CheckConstraint(
            "(status = 'PENDING_REVIEW') = (reviewed_at IS NULL)", name="reviewed_when_decided"
        ),
        CheckConstraint(_TAX, name="tax"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_code"),
        Index("ix_reimbursement_expenses_contract_status", "contract_id", "status"),
    )

    property_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("properties.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    contract_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant_occupancies.id", ondelete="RESTRICT"), nullable=False
    )
    expense_type_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reimbursement_expense_types.id", ondelete="RESTRICT"), nullable=False
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    # As first entered; edits before review change `amount` only (and are logged).
    original_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="INR")
    description: Mapped[str] = mapped_column(Text, nullable=False)
    one_time: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    tax_treatment: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=TaxTreatment.EXEMPT.value
    )
    tax_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    receipt_file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("files.id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=ReimbursementStatus.PENDING_REVIEW.value
    )
    review_note: Mapped[str | None] = mapped_column(Text)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    entered_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )
