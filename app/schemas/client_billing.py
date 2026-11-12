"""What the client app shows a customer about their own invoices, payments and deposits.
Internal notes, reasons and draft invoices are never included."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field

from app.models.enums import ChargeCategory, InvoiceStatus, PaymentMethod, PaymentStatus
from app.schemas.common import ReadSchema


class CustomerInvoiceLine(ReadSchema):
    category: ChargeCategory
    description: str
    amount: Decimal


class CustomerPayment(ReadSchema):
    id: uuid.UUID
    reference: str
    invoice_id: uuid.UUID
    invoice_reference: str
    amount: Decimal
    currency: str
    paid_on: date
    method: PaymentMethod
    external_reference: str | None
    status: PaymentStatus = Field(
        description="VERIFIED: confirmed by the property team. RECORDED: received, awaiting "
        "verification (not counted as paid). REJECTED: not accepted."
    )
    verified_at: datetime | None
    rejected_at: datetime | None


class CustomerInvoice(ReadSchema):
    id: uuid.UUID
    reference: str
    contract_reference: str | None
    status: InvoiceStatus = Field(description="ISSUED or CANCELLED; drafts are never shown.")
    payment_state: Literal["UNPAID", "PARTLY_PAID", "PAID"] | None = Field(
        description="Issued invoices only, from verified payments."
    )
    invoice_date: date
    due_date: date | None = Field(description="As entered by the property team, if any.")
    period_start: date | None
    period_end: date | None
    currency: str
    lines: list[CustomerInvoiceLine]
    total: Decimal = Field(description="The sum of the lines.")
    paid: Decimal = Field(description="Verified payments only.")
    awaiting_verification: Decimal
    outstanding: Decimal = Field(description="Total minus verified payments.")
    issued_at: datetime | None
    cancelled_at: datetime | None
    payments: list[CustomerPayment] = Field(
        default_factory=list, description="On the detail view only."
    )


class CustomerDeposit(ReadSchema):
    """A security deposit agreed in an approved contract. Shows what was agreed, not whether
    it was received."""

    contract_id: uuid.UUID
    contract_reference: str
    contract_status: str
    property_name: str
    amount: Decimal
    currency: str
