"""Invoices, payments, balances, deposits, and spaces and bookings across tenant accounts.
Administrators only. Amounts and dates are as entered; nothing is calculated except sums of
lines and of verified payments. Deposits and advances are never invoiced or paid here."""

import uuid

from fastapi import APIRouter

from app.api.dependencies import CurrentAdmin, DbSession, ListParams
from app.api.responses import NOT_FOUND, PROTECTED, _error
from app.models.enums import InvoiceStatus, PaymentStatus
from app.schemas.common import Page
from app.schemas.invoice import (
    BalanceRead,
    DepositRow,
    InvoiceCreate,
    InvoiceLines,
    InvoiceRead,
    InvoiceUpdate,
    LineSuggestion,
    PaymentCreate,
    PaymentRead,
    Reason,
    SpaceBookingRow,
)
from app.services.invoices import InvoiceService

router = APIRouter(tags=["Accounts billing"], responses=PROTECTED)
STEP = {409: _error("Not allowed in the current status; the message says why.")}
BAD = {400: _error("A value breaks a rule (account, contract, expense, dates).")}


@router.get("/invoices", response_model=Page[InvoiceRead], summary="Invoices")
def list_invoices(
    admin: CurrentAdmin,
    session: DbSession,
    query: ListParams,
    account_id: uuid.UUID | None = None,
    status: InvoiceStatus | None = None,
) -> Page[InvoiceRead]:
    """Newest first. `search` matches the reference, tenant and company."""
    items, total = InvoiceService(session, admin).invoices(
        query, account_id=account_id, status=status
    )
    return Page(items=items, total=total, page=query.page, page_size=query.page_size)


@router.post(
    "/invoices",
    response_model=InvoiceRead,
    status_code=201,
    responses={**NOT_FOUND, **BAD},
    summary="Draft an invoice",
)
def create_invoice(admin: CurrentAdmin, session: DbSession, body: InvoiceCreate) -> InvoiceRead:
    return InvoiceService(session, admin).create(body.model_dump())


@router.get(
    "/invoices/{invoice_id}", response_model=InvoiceRead, responses=NOT_FOUND, summary="An invoice"
)
def get_invoice(admin: CurrentAdmin, session: DbSession, invoice_id: uuid.UUID) -> InvoiceRead:
    return InvoiceService(session, admin).get(invoice_id)


@router.patch(
    "/invoices/{invoice_id}",
    response_model=InvoiceRead,
    responses={**NOT_FOUND, **BAD, **STEP},
    summary="Edit a draft invoice",
)
def update_invoice(
    admin: CurrentAdmin, session: DbSession, invoice_id: uuid.UUID, body: InvoiceUpdate
) -> InvoiceRead:
    return InvoiceService(session, admin).update(invoice_id, body.model_dump(exclude_unset=True))


@router.put(
    "/invoices/{invoice_id}/lines",
    response_model=InvoiceRead,
    responses={**NOT_FOUND, **BAD, **STEP},
    summary="Set a draft invoice's lines",
)
def set_lines(
    admin: CurrentAdmin, session: DbSession, invoice_id: uuid.UUID, body: InvoiceLines
) -> InvoiceRead:
    """Space Fee, Maintenance Fee and Reimbursement Fee lines, amounts as entered. A
    Reimbursement Fee line may bill a reviewed expense, once, at its reviewed amount."""
    return InvoiceService(session, admin).set_lines(
        invoice_id, [line.model_dump() for line in body.lines]
    )


@router.get(
    "/invoices/{invoice_id}/suggestions",
    response_model=list[LineSuggestion],
    responses=NOT_FOUND,
    summary="Lines suggested from the contract and reviewed expenses",
)
def suggestions(
    admin: CurrentAdmin, session: DbSession, invoice_id: uuid.UUID
) -> list[LineSuggestion]:
    """Read-only: the contract's agreed monthly rent and reviewed expenses not yet invoiced."""
    return InvoiceService(session, admin).suggestions(invoice_id)


@router.post(
    "/invoices/{invoice_id}/issue",
    response_model=InvoiceRead,
    responses={**NOT_FOUND, **STEP},
    summary="Issue a draft invoice",
)
def issue_invoice(admin: CurrentAdmin, session: DbSession, invoice_id: uuid.UUID) -> InvoiceRead:
    return InvoiceService(session, admin).issue(invoice_id)


@router.post(
    "/invoices/{invoice_id}/cancel",
    response_model=InvoiceRead,
    responses={**NOT_FOUND, **STEP},
    summary="Cancel an invoice (with a reason)",
)
def cancel_invoice(
    admin: CurrentAdmin, session: DbSession, invoice_id: uuid.UUID, body: Reason
) -> InvoiceRead:
    """Not while a payment is recorded or verified. Releases billed expenses."""
    return InvoiceService(session, admin).cancel(invoice_id, body.reason)


@router.post(
    "/invoices/{invoice_id}/payments",
    response_model=PaymentRead,
    status_code=201,
    responses={**NOT_FOUND, **BAD, **STEP},
    summary="Record a payment against an invoice",
)
def record_payment(
    admin: CurrentAdmin, session: DbSession, invoice_id: uuid.UUID, body: PaymentCreate
) -> PaymentRead:
    """It counts once verified. Never more than is left to pay; a reference only once."""
    return InvoiceService(session, admin).record_payment(invoice_id, body.model_dump())


@router.get("/payments", response_model=Page[PaymentRead], summary="Payments")
def list_payments(
    admin: CurrentAdmin,
    session: DbSession,
    query: ListParams,
    account_id: uuid.UUID | None = None,
    invoice_id: uuid.UUID | None = None,
    status: PaymentStatus | None = None,
) -> Page[PaymentRead]:
    items, total = InvoiceService(session, admin).payments(
        query, account_id=account_id, invoice_id=invoice_id, status=status
    )
    return Page(items=items, total=total, page=query.page, page_size=query.page_size)


@router.post(
    "/payments/{payment_id}/verify",
    response_model=PaymentRead,
    responses={**NOT_FOUND, **STEP},
    summary="Verify a recorded payment",
)
def verify_payment(admin: CurrentAdmin, session: DbSession, payment_id: uuid.UUID) -> PaymentRead:
    return InvoiceService(session, admin).verify(payment_id)


@router.post(
    "/payments/{payment_id}/reject",
    response_model=PaymentRead,
    responses={**NOT_FOUND, **STEP},
    summary="Reject a recorded payment (with a reason)",
)
def reject_payment(
    admin: CurrentAdmin, session: DbSession, payment_id: uuid.UUID, body: Reason
) -> PaymentRead:
    return InvoiceService(session, admin).reject(payment_id, body.reason)


@router.get("/billing/balances", response_model=list[BalanceRead], summary="Balances by account")
def balances(
    admin: CurrentAdmin, session: DbSession, account_id: uuid.UUID | None = None
) -> list[BalanceRead]:
    """Issued minus verified, per account and currency. Deposits are shown apart."""
    return InvoiceService(session, admin).balances(account_id)


@router.get("/billing/deposits", response_model=list[DepositRow], summary="Security deposits")
def deposits(
    admin: CurrentAdmin, session: DbSession, account_id: uuid.UUID | None = None
) -> list[DepositRow]:
    """Agreed in approved contracts; held for the tenant, never revenue (BR-030)."""
    return InvoiceService(session, admin).deposits(account_id)


@router.get(
    "/billing/spaces-bookings",
    response_model=list[SpaceBookingRow],
    summary="Spaces held and current bookings by account",
)
def spaces_and_bookings(
    admin: CurrentAdmin, session: DbSession, account_id: uuid.UUID | None = None
) -> list[SpaceBookingRow]:
    return InvoiceService(session, admin).spaces_and_bookings(account_id)
