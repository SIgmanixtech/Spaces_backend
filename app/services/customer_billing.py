"""The signed-in customer's invoices, payments and deposits (read-only, client app).

Only what the property team has issued is shown: draft invoices are internal and never
listed. Amounts, statuses and totals come from the same records and rules as the admin
Accounts screens (BR-018, BR-019): a payment counts as paid only once VERIFIED; a recorded
payment reads as awaiting verification; a rejected one is never counted. Internal notes and
reasons are not shown. Deposits are the amounts agreed in the customer's approved contracts
(held, never netted against invoices, BR-030); nothing here says a deposit was received.
No tax, due-date, late-fee or advance rule is applied or invented here.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.models.account import Account
from app.models.enums import InvoiceStatus
from app.models.invoice import Invoice, Payment
from app.models.tenant import Tenant
from app.models.user import AppUser
from app.schemas.client_billing import (
    CustomerDeposit,
    CustomerInvoice,
    CustomerInvoiceLine,
    CustomerPayment,
)
from app.services.invoices import InvoiceService

VISIBLE = (InvoiceStatus.ISSUED, InvoiceStatus.CANCELLED)


class CustomerBillingService:
    def __init__(self, session: Session, user: AppUser, tenant: Tenant) -> None:
        self.session = session
        self.user = user
        self.tenant = tenant
        self.invoices_service = InvoiceService(session, user)

    def _account_ids(self) -> list[uuid.UUID]:
        return list(
            self.session.scalars(select(Account.id).where(Account.tenant_id == self.tenant.id))
        )

    def invoices(self) -> list[CustomerInvoice]:
        accounts = self._account_ids()
        if not accounts:
            return []
        rows = list(
            self.session.scalars(
                select(Invoice)
                .where(Invoice.account_id.in_(accounts), Invoice.status.in_(VISIBLE))
                .order_by(Invoice.invoice_date.desc(), Invoice.reference.desc())
            )
        )
        return [self._invoice(r) for r in self.invoices_service._reads(rows)]

    def invoice(self, invoice_id: uuid.UUID) -> CustomerInvoice:
        row = self.session.scalar(
            select(Invoice).where(
                Invoice.id == invoice_id,
                Invoice.account_id.in_(self._account_ids()),
                Invoice.status.in_(VISIBLE),
            )
        )
        if row is None:
            raise NotFoundError("Invoice not found.")
        read = self._invoice(self.invoices_service._reads([row])[0])
        read.payments = self.payments(invoice_id=row.id)
        return read

    def payments(self, invoice_id: uuid.UUID | None = None) -> list[CustomerPayment]:
        accounts = self._account_ids()
        if not accounts:
            return []
        statement = (
            select(Payment, Invoice.reference)
            .join(Invoice, Invoice.id == Payment.invoice_id)
            .where(Payment.account_id.in_(accounts), Invoice.status.in_(VISIBLE))
            .order_by(Payment.paid_on.desc(), Payment.reference.desc())
        )
        if invoice_id is not None:
            statement = statement.where(Payment.invoice_id == invoice_id)
        return [
            CustomerPayment(
                id=p.id,
                reference=p.reference,
                invoice_id=p.invoice_id,
                invoice_reference=reference,
                amount=p.amount,
                currency=p.currency,
                paid_on=p.paid_on,
                method=p.method,
                external_reference=p.external_reference,
                status=p.status,
                verified_at=p.verified_at,
                rejected_at=p.rejected_at,
            )
            for p, reference in self.session.execute(statement)
        ]

    def deposits(self) -> list[CustomerDeposit]:
        out: list[CustomerDeposit] = []
        for account_id in self._account_ids():
            for d in self.invoices_service.deposits(account_id):
                out.append(
                    CustomerDeposit(
                        contract_id=d.contract_id,
                        contract_reference=d.contract_reference,
                        contract_status=d.contract_status,
                        property_name=d.property_name,
                        amount=d.amount,
                        currency=d.currency,
                    )
                )
        return out

    @staticmethod
    def _invoice(read) -> CustomerInvoice:  # noqa: ANN001
        return CustomerInvoice(
            id=read.id,
            reference=read.reference,
            contract_reference=read.contract_reference,
            status=read.status,
            payment_state=read.payment_state,
            invoice_date=read.invoice_date,
            due_date=read.due_date,
            period_start=read.period_start,
            period_end=read.period_end,
            currency=read.currency,
            lines=[
                CustomerInvoiceLine(
                    category=line.category, description=line.description, amount=line.amount
                )
                for line in read.lines
            ],
            total=read.total,
            paid=read.paid,
            awaiting_verification=read.awaiting_verification,
            outstanding=read.outstanding,
            issued_at=read.issued_at,
            cancelled_at=read.cancelled_at,
        )
