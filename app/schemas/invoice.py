"""Invoices, payments, balances, deposits, and spaces and bookings across accounts.
Admin only. Amounts are as entered; nothing is calculated except sums of lines and of
verified payments."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import ClassVar, Literal, Self

from pydantic import Field, model_validator

from app.models.enums import ChargeCategory, InvoiceStatus, PaymentMethod, PaymentStatus
from app.schemas.common import InputSchema, ReadSchema, UpdateSchema

_Amount = Field(ge=0, max_digits=12, decimal_places=2)


class InvoiceCreate(InputSchema):
    account_id: uuid.UUID
    contract_id: uuid.UUID | None = Field(default=None, description="One of the account's.")
    invoice_date: date
    due_date: date | None = Field(default=None, description="As agreed; never calculated.")
    period_start: date | None = None
    period_end: date | None = None
    currency: str = Field(default="INR", pattern=r"^[A-Z]{3}$")
    notes: str | None = Field(default=None, max_length=5000)

    @model_validator(mode="after")
    def _dates(self) -> Self:
        if self.due_date and self.due_date < self.invoice_date:
            raise ValueError("The due date cannot be before the invoice date.")
        if self.period_start and self.period_end and self.period_end < self.period_start:
            raise ValueError("The period cannot end before it starts.")
        return self


class InvoiceUpdate(UpdateSchema):
    """Only a draft."""

    required_fields: ClassVar[frozenset[str]] = frozenset({"invoice_date", "currency"})

    contract_id: uuid.UUID | None = None
    invoice_date: date | None = None
    due_date: date | None = None
    period_start: date | None = None
    period_end: date | None = None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    notes: str | None = Field(default=None, max_length=5000)


class InvoiceLineInput(InputSchema):
    category: ChargeCategory
    description: str = Field(min_length=1, max_length=500)
    amount: Decimal = _Amount
    expense_id: uuid.UUID | None = Field(
        default=None, description="A reviewed expense (Reimbursement Fee lines only)."
    )


class InvoiceLines(InputSchema):
    lines: list[InvoiceLineInput] = Field(max_length=100)


class Reason(InputSchema):
    reason: str = Field(min_length=1, max_length=1000)


class InvoiceLineRead(ReadSchema):
    id: uuid.UUID
    category: ChargeCategory
    description: str
    amount: Decimal
    expense_id: uuid.UUID | None


class CategoryTotal(ReadSchema):
    category: ChargeCategory
    amount: Decimal


class InvoiceRead(ReadSchema):
    id: uuid.UUID
    reference: str
    account_id: uuid.UUID
    tenant_name: str
    contract_id: uuid.UUID | None
    contract_reference: str | None
    status: InvoiceStatus
    payment_state: Literal["UNPAID", "PARTLY_PAID", "PAID"] | None = Field(
        description="Issued invoices only; from verified payments."
    )
    invoice_date: date
    due_date: date | None
    period_start: date | None
    period_end: date | None
    currency: str
    notes: str | None
    lines: list[InvoiceLineRead]
    category_totals: list[CategoryTotal] = Field(description="Each category separately.")
    total: Decimal = Field(description="The sum of the lines.")
    paid: Decimal = Field(description="Verified payments.")
    awaiting_verification: Decimal
    outstanding: Decimal = Field(description="Total minus verified payments.")
    issued_at: datetime | None
    cancelled_at: datetime | None
    cancel_reason: str | None
    created_at: datetime
    updated_at: datetime


class LineSuggestion(ReadSchema):
    category: ChargeCategory
    description: str
    amount: Decimal
    expense_id: uuid.UUID | None
    source: str = Field(description="Where it comes from (the contract, a reviewed expense).")


class PaymentCreate(InputSchema):
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    paid_on: date
    method: PaymentMethod
    external_reference: str | None = Field(
        default=None, max_length=100, description="Bank / UPI reference or cheque number."
    )
    notes: str | None = Field(default=None, max_length=2000)


class PaymentRead(ReadSchema):
    id: uuid.UUID
    reference: str
    account_id: uuid.UUID
    tenant_name: str
    invoice_id: uuid.UUID
    invoice_reference: str
    amount: Decimal
    currency: str
    paid_on: date
    method: PaymentMethod
    external_reference: str | None
    status: PaymentStatus
    notes: str | None
    verified_at: datetime | None
    rejected_at: datetime | None
    reject_reason: str | None
    created_at: datetime


class BalanceRead(ReadSchema):
    """Per account and currency. Deposits are shown apart: held, never netted (BR-030)."""

    account_id: uuid.UUID
    tenant_name: str
    currency: str
    invoiced: Decimal = Field(description="Issued invoices.")
    paid: Decimal = Field(description="Verified payments.")
    awaiting_verification: Decimal
    outstanding: Decimal
    open_invoices: int
    deposits_held: Decimal = Field(description="Agreed in approved contracts; not netted.")


class DepositRow(ReadSchema):
    account_id: uuid.UUID
    tenant_name: str
    contract_id: uuid.UUID
    contract_reference: str
    contract_status: str
    property_name: str
    amount: Decimal
    currency: str


class SpaceBookingRow(ReadSchema):
    account_id: uuid.UUID
    tenant_name: str
    kind: Literal["CONTRACT", "BOOKING"]
    reference: str
    property_name: str
    space_name: str | None
    starts: date
    ends: date | None
    status: str
