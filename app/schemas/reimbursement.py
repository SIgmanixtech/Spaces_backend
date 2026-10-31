"""Reimbursement Fee (business rule BR-029)."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import ClassVar, Literal, Self

from pydantic import Field, model_validator

from app.models.enums import (
    BillingPeriod,
    ReimbursementMethod,
    ReimbursementRecurrence,
    ReimbursementStatus,
    TaxTreatment,
)
from app.schemas.common import InputSchema, ReadSchema, UpdateSchema

_Money = Field(gt=0, max_digits=12, decimal_places=2)
_Rate = Field(default=None, ge=0, le=100, max_digits=5, decimal_places=2)


# --- expense types -----------------------------------------------------------------------


class ExpenseTypeCreate(InputSchema):
    name: str = Field(min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=1000)


class ExpenseTypeUpdate(UpdateSchema):
    required_fields: ClassVar[frozenset[str]] = frozenset({"name", "is_active"})

    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=1000)
    is_active: bool | None = None


class ExpenseTypeRead(ReadSchema):
    id: uuid.UUID
    name: str
    description: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


# --- terms (property defaults and contract terms) -----------------------------------------


class TermFields(InputSchema):
    expense_type_id: uuid.UUID
    method: ReimbursementMethod
    amount: Decimal | None = Field(
        default=None, gt=0, max_digits=12, decimal_places=2, description="Required for FIXED."
    )
    recurrence: ReimbursementRecurrence
    billing_period: BillingPeriod | None = Field(
        default=None, description="Required for RECURRING (any existing period except CUSTOM)."
    )
    tax_treatment: TaxTreatment = TaxTreatment.EXEMPT
    tax_rate: Decimal | None = _Rate
    currency: str = Field(default="INR", min_length=3, max_length=3, pattern="^[A-Z]{3}$")
    notes: str | None = Field(default=None, max_length=2000)


class TermCreate(TermFields):
    property_id: uuid.UUID
    contract_id: uuid.UUID | None = Field(
        default=None, description="Omit for a property default; set for a contract's term."
    )


class TermUpdate(UpdateSchema):
    required_fields: ClassVar[frozenset[str]] = frozenset(
        {"method", "recurrence", "tax_treatment", "currency"}
    )

    method: ReimbursementMethod | None = None
    amount: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    recurrence: ReimbursementRecurrence | None = None
    billing_period: BillingPeriod | None = None
    tax_treatment: TaxTreatment | None = None
    tax_rate: Decimal | None = _Rate
    currency: str | None = Field(default=None, min_length=3, max_length=3, pattern="^[A-Z]{3}$")
    notes: str | None = Field(default=None, max_length=2000)


class TermRead(ReadSchema):
    id: uuid.UUID
    property_id: uuid.UUID
    contract_id: uuid.UUID | None
    scope: Literal["PROPERTY_DEFAULT", "CONTRACT"]
    expense_type_id: uuid.UUID
    expense_type_name: str
    method: ReimbursementMethod
    amount: float | None
    recurrence: ReimbursementRecurrence
    billing_period: BillingPeriod | None
    tax_treatment: TaxTreatment
    tax_rate: float | None
    currency: str
    notes: str | None
    tax: float | None = Field(description="Tax on a FIXED amount; null for ACTUAL terms.")
    total: float | None = Field(description="FIXED amount plus ADDITIONAL tax; null for ACTUAL.")
    created_at: datetime
    updated_at: datetime


class ApplyDefaultsResult(ReadSchema):
    created: list[TermRead]
    skipped: list[str] = Field(description="Expense types the contract already has: unchanged.")


# --- actual expenses ---------------------------------------------------------------------


class ExpenseCreate(InputSchema):
    contract_id: uuid.UUID
    expense_type_id: uuid.UUID
    period_start: date
    period_end: date
    amount: Decimal = _Money
    description: str = Field(min_length=1, max_length=2000)
    one_time: bool = Field(
        default=False,
        description="An exceptional, one-time expense. A recurring one needs the contract to "
        "have an ACTUAL term for its type.",
    )
    tax_treatment: TaxTreatment | None = Field(
        default=None, description="Defaults to the contract term's tax for this type, else EXEMPT."
    )
    tax_rate: Decimal | None = _Rate
    currency: str = Field(default="INR", min_length=3, max_length=3, pattern="^[A-Z]{3}$")
    receipt_file_id: uuid.UUID | None = Field(
        default=None, description="An existing stored file of the same property."
    )

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.period_end < self.period_start:
            raise ValueError("period_end cannot be before period_start")
        return self


class ExpenseUpdate(UpdateSchema):
    """Only while PENDING_REVIEW. The first entered amount is kept as original_amount."""

    required_fields: ClassVar[frozenset[str]] = frozenset(
        {"amount", "description", "period_start", "period_end", "tax_treatment"}
    )

    period_start: date | None = None
    period_end: date | None = None
    amount: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    description: str | None = Field(default=None, min_length=1, max_length=2000)
    tax_treatment: TaxTreatment | None = None
    tax_rate: Decimal | None = _Rate
    receipt_file_id: uuid.UUID | None = None


class ExpenseReview(InputSchema):
    decision: Literal["REVIEWED", "REJECTED"]
    note: str | None = Field(default=None, max_length=2000)


class ExpenseRead(ReadSchema):
    id: uuid.UUID
    property_id: uuid.UUID
    contract_id: uuid.UUID
    contract_reference: str
    expense_type_id: uuid.UUID
    expense_type_name: str
    period_start: date
    period_end: date
    amount: float
    original_amount: float
    currency: str
    description: str
    one_time: bool
    tax_treatment: TaxTreatment
    tax_rate: float | None
    tax: float
    total: float = Field(description="Amount plus ADDITIONAL tax.")
    receipt_file_id: uuid.UUID | None
    status: ReimbursementStatus
    review_note: str | None
    reviewed_by: uuid.UUID | None
    reviewed_at: datetime | None
    entered_by: uuid.UUID | None
    updated_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


# --- existing additional charges (report only) --------------------------------------------


class AdditionalChargeReview(ReadSchema):
    rule_id: uuid.UUID
    property_id: uuid.UUID
    rule_status: str
    target: str = Field(description="What the rule prices: a space, a building or the property.")
    label: str
    amount: float
    classification: Literal["MATCHES_EXPENSE_TYPE", "UNCLASSIFIED"]
    suggested_expense_type: str | None
    note: str
