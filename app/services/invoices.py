"""Invoices and payments for tenant accounts (BR-009, BR-018, BR-019, BR-029, BR-030).

Nothing financial is decided here (D5-D7 stay open):
- Lines are the three charge categories, amounts as entered. Suggestions come only from
  approved contract terms (the agreed monthly rent) and reviewed expenses; the admin
  chooses. No tax, frequency, due-date rule, late fee or discount. Total = sum of lines.
- A reviewed expense reaches at most one invoice, at its reviewed total (its amount plus
  the tax its agreed term gives, as on the Reimbursements screen); cancelling the invoice
  releases it.
- DRAFT -> ISSUED -> CANCELLED (reason; only while no payment is recorded or verified).
- Payments: against an issued invoice, RECORDED -> VERIFIED (counted) or REJECTED
  (reason). Never more than the invoice total; the same method and reference once.
- Deposits and advances are never invoiced, paid or netted here.
History goes to the activity log (entity types "invoices" and "payments").
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models.account import Account
from app.models.booking import Booking
from app.models.enums import (
    BookingStatus,
    ChargeCategory,
    ContractStatus,
    InvoiceStatus,
    PaymentStatus,
    ReimbursementStatus,
)
from app.models.invoice import Invoice, InvoiceLine, Payment
from app.models.property import Property
from app.models.reimbursement import ReimbursementExpense, ReimbursementExpenseType
from app.models.space import Space
from app.models.tenant import Tenant, TenantOccupancy
from app.models.user import AppUser
from app.repositories.base import paginate_rows, text_search
from app.schemas.common import ListQuery
from app.schemas.invoice import (
    BalanceRead,
    CategoryTotal,
    DepositRow,
    InvoiceLineRead,
    InvoiceRead,
    LineSuggestion,
    PaymentRead,
    SpaceBookingRow,
)
from app.services.accounts import AccountService
from app.services.base import commit, flush, log_activity
from app.services.contracts import ContractService, group_members
from app.services.occupancy import business_today
from app.services.reimbursements import tax_on

CATEGORY_LABEL = {
    ChargeCategory.SPACE_FEE: "Space Fee",
    ChargeCategory.MAINTENANCE_FEE: "Maintenance Fee",
    ChargeCategory.REIMBURSEMENT_FEE: "Reimbursement Fee",
}
ZERO = Decimal("0.00")
COUNTED = (PaymentStatus.RECORDED.value, PaymentStatus.VERIFIED.value)


def _expense_total(expense: ReimbursementExpense) -> Decimal:
    """The reviewed expense's total as the Reimbursements screen shows it."""
    return tax_on(expense.amount, expense.tax_treatment, expense.tax_rate)[1]


class InvoiceService:
    def __init__(self, session: Session, actor: AppUser) -> None:
        self.session = session
        self.actor = actor

    # --- helpers ----------------------------------------------------------------------------

    def _log(self, entity: str, action: str, entity_id: uuid.UUID, message: str) -> None:
        log_activity(
            self.session,
            self.actor,
            action=action,
            entity_type=entity,
            entity_id=entity_id,
            property_id=None,
            message=message[:500],
        )

    def _account(self, account_id: uuid.UUID) -> tuple[Account, Tenant]:
        AccountService(self.session, self.actor).ensure()
        found = self.session.execute(
            select(Account, Tenant)
            .join(Tenant, Tenant.id == Account.tenant_id)
            .where(Account.id == account_id)
        ).first()
        if found is None:
            raise NotFoundError("Account not found.")
        return found[0], found[1]

    def _invoice(self, invoice_id: uuid.UUID, lock: bool = False) -> Invoice:
        statement = select(Invoice).where(Invoice.id == invoice_id)
        if lock:
            statement = statement.with_for_update()
        invoice = self.session.scalar(statement)
        if invoice is None:
            raise NotFoundError("Invoice not found.")
        return invoice

    def _contract_of(self, tenant: Tenant, contract_id: uuid.UUID | None) -> None:
        if contract_id is None:
            return
        contract = self.session.get(TenantOccupancy, contract_id)
        if contract is None or contract.tenant_id != tenant.id:
            raise BadRequestError("The contract must be one of this account's contracts.")

    def _draft(self, invoice: Invoice) -> None:
        if invoice.status != InvoiceStatus.DRAFT:
            raise ConflictError(
                f"Invoice {invoice.reference} is {invoice.status.lower()}; only a draft changes."
            )

    def _sums(self, ids: list[uuid.UUID]) -> tuple[dict, dict, dict]:
        totals: dict[uuid.UUID, Decimal] = defaultdict(lambda: ZERO)
        paid: dict[uuid.UUID, Decimal] = defaultdict(lambda: ZERO)
        waiting: dict[uuid.UUID, Decimal] = defaultdict(lambda: ZERO)
        if not ids:
            return totals, paid, waiting
        for invoice_id, amount in self.session.execute(
            select(InvoiceLine.invoice_id, func.sum(InvoiceLine.amount))
            .where(InvoiceLine.invoice_id.in_(ids))
            .group_by(InvoiceLine.invoice_id)
        ):
            totals[invoice_id] = amount
        for invoice_id, status, amount in self.session.execute(
            select(Payment.invoice_id, Payment.status, func.sum(Payment.amount))
            .where(Payment.invoice_id.in_(ids), Payment.status.in_(COUNTED))
            .group_by(Payment.invoice_id, Payment.status)
        ):
            (paid if status == PaymentStatus.VERIFIED else waiting)[invoice_id] += amount
        return totals, paid, waiting

    def _reads(self, invoices: list[Invoice]) -> list[InvoiceRead]:
        if not invoices:
            return []
        ids = [i.id for i in invoices]
        totals, paid, waiting = self._sums(ids)
        lines: dict[uuid.UUID, list[InvoiceLine]] = defaultdict(list)
        for line in self.session.scalars(
            select(InvoiceLine)
            .where(InvoiceLine.invoice_id.in_(ids))
            .order_by(InvoiceLine.sort_order, InvoiceLine.created_at, InvoiceLine.id)
        ):
            lines[line.invoice_id].append(line)
        tenants = dict(
            self.session.execute(
                select(Account.id, Tenant.name)
                .join(Tenant, Tenant.id == Account.tenant_id)
                .where(Account.id.in_({i.account_id for i in invoices}))
            ).all()
        )
        contracts = dict(
            self.session.execute(
                select(TenantOccupancy.id, TenantOccupancy.reference).where(
                    TenantOccupancy.id.in_({i.contract_id for i in invoices if i.contract_id})
                )
            ).all()
        )
        out = []
        for i in invoices:
            total = totals[i.id]
            by_category: dict[str, Decimal] = defaultdict(lambda: ZERO)
            for line in lines[i.id]:
                by_category[line.category] += line.amount
            state = None
            if i.status == InvoiceStatus.ISSUED:
                state = (
                    "PAID" if paid[i.id] >= total else "PARTLY_PAID" if paid[i.id] > 0 else "UNPAID"
                )
            out.append(
                InvoiceRead(
                    id=i.id,
                    reference=i.reference,
                    account_id=i.account_id,
                    tenant_name=tenants.get(i.account_id, ""),
                    contract_id=i.contract_id,
                    contract_reference=contracts.get(i.contract_id) if i.contract_id else None,
                    status=i.status,
                    payment_state=state,
                    invoice_date=i.invoice_date,
                    due_date=i.due_date,
                    period_start=i.period_start,
                    period_end=i.period_end,
                    currency=i.currency,
                    notes=i.notes,
                    lines=[InvoiceLineRead.model_validate(line) for line in lines[i.id]],
                    category_totals=[
                        CategoryTotal(category=c, amount=by_category[c.value])
                        for c in ChargeCategory
                        if c.value in by_category
                    ],
                    total=total,
                    paid=paid[i.id],
                    awaiting_verification=waiting[i.id],
                    outstanding=(total - paid[i.id]) if i.status == InvoiceStatus.ISSUED else ZERO,
                    issued_at=i.issued_at,
                    cancelled_at=i.cancelled_at,
                    cancel_reason=i.cancel_reason,
                    created_at=i.created_at,
                    updated_at=i.updated_at,
                )
            )
        return out

    # --- invoices ---------------------------------------------------------------------------

    def invoices(
        self,
        query: ListQuery,
        *,
        account_id: uuid.UUID | None = None,
        status: InvoiceStatus | None = None,
    ) -> tuple[list[InvoiceRead], int]:
        AccountService(self.session, self.actor).ensure()
        statement = (
            select(Invoice)
            .join(Account, Account.id == Invoice.account_id)
            .join(Tenant, Tenant.id == Account.tenant_id)
        )
        if account_id is not None:
            statement = statement.where(Invoice.account_id == account_id)
        if status is not None:
            statement = statement.where(Invoice.status == status)
        if query.search:
            statement = statement.where(
                text_search([Invoice.reference, Tenant.name, Tenant.company_name], query.search)
            )
        statement = statement.order_by(Invoice.invoice_date.desc(), Invoice.created_at.desc())
        rows, total = paginate_rows(self.session, statement, query)
        return self._reads([r[0] for r in rows]), total

    def get(self, invoice_id: uuid.UUID) -> InvoiceRead:
        return self._reads([self._invoice(invoice_id)])[0]

    def create(self, values: dict[str, Any]) -> InvoiceRead:
        account, tenant = self._account(values["account_id"])
        self._contract_of(tenant, values.get("contract_id"))
        invoice = Invoice(**values, created_by=self.actor.id, updated_by=self.actor.id)
        self.session.add(invoice)
        flush(self.session)
        self.session.refresh(invoice, ["reference"])
        self._log(
            "invoices",
            "created",
            invoice.id,
            f"Invoice {invoice.reference} drafted for {tenant.name}.",
        )
        commit(self.session)
        return self.get(invoice.id)

    def update(self, invoice_id: uuid.UUID, values: dict[str, Any]) -> InvoiceRead:
        invoice = self._invoice(invoice_id, lock=True)
        self._draft(invoice)
        account, tenant = self._account(invoice.account_id)
        if "contract_id" in values:
            self._contract_of(tenant, values["contract_id"])
        changed = [k for k, v in values.items() if getattr(invoice, k) != v]
        for key in changed:
            setattr(invoice, key, values[key])
        if invoice.due_date and invoice.due_date < invoice.invoice_date:
            raise BadRequestError("The due date cannot be before the invoice date.")
        if (
            invoice.period_start
            and invoice.period_end
            and invoice.period_end < invoice.period_start
        ):
            raise BadRequestError("The period cannot end before it starts.")
        if changed:
            invoice.updated_by = self.actor.id
            self._log(
                "invoices",
                "updated",
                invoice.id,
                f"Invoice {invoice.reference} updated: {', '.join(changed)}.",
            )
        commit(self.session)
        return self.get(invoice.id)

    def set_lines(self, invoice_id: uuid.UUID, lines: list[dict[str, Any]]) -> InvoiceRead:
        invoice = self._invoice(invoice_id, lock=True)
        self._draft(invoice)
        account, tenant = self._account(invoice.account_id)
        contract_ids = set(
            self.session.scalars(
                select(TenantOccupancy.id).where(TenantOccupancy.tenant_id == tenant.id)
            )
        )
        expense_ids = [line["expense_id"] for line in lines if line.get("expense_id")]
        if len(set(expense_ids)) != len(expense_ids):
            raise BadRequestError("An expense is listed twice.")
        expenses = {
            e.id: e
            for e in self.session.scalars(
                select(ReimbursementExpense).where(ReimbursementExpense.id.in_(expense_ids))
            )
        }
        taken = set(
            self.session.scalars(
                select(InvoiceLine.expense_id).where(
                    InvoiceLine.expense_id.in_(expense_ids),
                    InvoiceLine.invoice_id != invoice.id,
                    InvoiceLine.expense_released.is_(False),
                )
            )
        )
        for line in lines:
            category = ChargeCategory(line["category"])
            expense_id = line.get("expense_id")
            if expense_id is None:
                continue
            if category != ChargeCategory.REIMBURSEMENT_FEE:
                raise BadRequestError("Only a Reimbursement Fee line can bill an expense.")
            expense = expenses.get(expense_id)
            if expense is None or expense.contract_id not in contract_ids:
                raise BadRequestError("The expense must belong to one of this account's contracts.")
            if expense.status != ReimbursementStatus.REVIEWED:
                raise BadRequestError("Only a reviewed expense can be invoiced.")
            if expense_id in taken:
                raise ConflictError("That expense is already on another invoice.")
            if expense.currency != invoice.currency:
                raise BadRequestError("The expense is in another currency than the invoice.")
            if Decimal(line["amount"]) != _expense_total(expense):
                raise BadRequestError(
                    "A reimbursed expense is billed at its reviewed total "
                    f"({expense.currency} {_expense_total(expense)})."
                )
        for old in self.session.scalars(
            select(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id)
        ):
            self.session.delete(old)
        flush(self.session)
        for position, line in enumerate(lines):
            self.session.add(
                InvoiceLine(
                    invoice_id=invoice.id,
                    category=line["category"],
                    description=line["description"].strip(),
                    amount=line["amount"],
                    expense_id=line.get("expense_id"),
                    sort_order=position,
                )
            )
        invoice.updated_by = self.actor.id
        self._log(
            "invoices",
            "lines_set",
            invoice.id,
            f"Invoice {invoice.reference}: {len(lines)} line(s) set.",
        )
        commit(self.session)
        return self.get(invoice.id)

    def suggestions(self, invoice_id: uuid.UUID) -> list[LineSuggestion]:
        """From approved contract terms and reviewed expenses only; nothing is added until
        the admin saves the lines."""
        invoice = self._invoice(invoice_id)
        account, tenant = self._account(invoice.account_id)
        out: list[LineSuggestion] = []
        if invoice.contract_id:
            contract = self.session.get(TenantOccupancy, invoice.contract_id)
            members = group_members(self.session, contract) if contract else []
            rents = [m.monthly_rent for m in members]
            if (
                contract
                and contract.status == ContractStatus.ACTIVE
                and all(r is not None for r in rents)
                and rents
            ):
                out.append(
                    LineSuggestion(
                        category=ChargeCategory.SPACE_FEE,
                        description=f"Space Fee, contract {contract.reference} (monthly rent "
                        "as agreed)",
                        amount=sum(rents),  # type: ignore[arg-type]
                        expense_id=None,
                        source=f"Contract {contract.reference}: agreed monthly rent",
                    )
                )
        contract_ids = (
            [invoice.contract_id]
            if invoice.contract_id
            else list(
                self.session.scalars(
                    select(TenantOccupancy.id).where(TenantOccupancy.tenant_id == tenant.id)
                )
            )
        )
        taken = select(InvoiceLine.expense_id).where(
            InvoiceLine.expense_id.is_not(None), InvoiceLine.expense_released.is_(False)
        )
        types = dict(
            self.session.execute(
                select(ReimbursementExpenseType.id, ReimbursementExpenseType.name)
            ).all()
        )
        for expense in self.session.scalars(
            select(ReimbursementExpense)
            .where(
                ReimbursementExpense.contract_id.in_(contract_ids),
                ReimbursementExpense.status == ReimbursementStatus.REVIEWED,
                ReimbursementExpense.currency == invoice.currency,
                ReimbursementExpense.id.not_in(taken),
            )
            .order_by(ReimbursementExpense.period_start, ReimbursementExpense.id)
        ):
            out.append(
                LineSuggestion(
                    category=ChargeCategory.REIMBURSEMENT_FEE,
                    description=f"{types.get(expense.expense_type_id, 'Reimbursement')}, "
                    f"{expense.period_start:%d %b %Y} to {expense.period_end:%d %b %Y}",
                    amount=_expense_total(expense),
                    expense_id=expense.id,
                    source="Reviewed expense (amount and its agreed tax)",
                )
            )
        return out

    def issue(self, invoice_id: uuid.UUID) -> InvoiceRead:
        invoice = self._invoice(invoice_id, lock=True)
        self._draft(invoice)
        totals, _, _ = self._sums([invoice.id])
        lines = self.session.scalar(
            select(func.count())
            .select_from(InvoiceLine)
            .where(InvoiceLine.invoice_id == invoice.id)
        )
        if not lines or totals[invoice.id] <= 0:
            raise ConflictError("Add at least one line with an amount before issuing.")
        invoice.status = InvoiceStatus.ISSUED.value
        invoice.issued_at = datetime.now(UTC)
        invoice.issued_by = self.actor.id
        self._log(
            "invoices",
            "issued",
            invoice.id,
            f"Invoice {invoice.reference} issued: {invoice.currency} {totals[invoice.id]}.",
        )
        commit(self.session)
        return self.get(invoice.id)

    def cancel(self, invoice_id: uuid.UUID, reason: str) -> InvoiceRead:
        invoice = self._invoice(invoice_id, lock=True)
        reason = reason.strip()
        if not reason:
            raise BadRequestError("Give a reason.")
        if invoice.status == InvoiceStatus.CANCELLED:
            raise ConflictError(f"Invoice {invoice.reference} is already cancelled.")
        live = self.session.scalar(
            select(func.count())
            .select_from(Payment)
            .where(Payment.invoice_id == invoice.id, Payment.status.in_(COUNTED))
        )
        if live:
            raise ConflictError(
                "The invoice has recorded or verified payments. Reject unverified ones first; "
                "a verified payment keeps the invoice."
            )
        invoice.status = InvoiceStatus.CANCELLED.value
        invoice.issued_at = invoice.issued_at or datetime.now(UTC)
        invoice.cancelled_at = datetime.now(UTC)
        invoice.cancel_reason = reason
        for line in self.session.scalars(
            select(InvoiceLine).where(
                InvoiceLine.invoice_id == invoice.id, InvoiceLine.expense_id.is_not(None)
            )
        ):
            line.expense_released = True  # the expense can be invoiced again
        self._log(
            "invoices",
            "cancelled",
            invoice.id,
            f"Invoice {invoice.reference} cancelled. Reason: {reason}",
        )
        commit(self.session)
        return self.get(invoice.id)

    # --- payments ---------------------------------------------------------------------------

    def _payment_reads(self, payments: list[Payment]) -> list[PaymentRead]:
        if not payments:
            return []
        tenants = dict(
            self.session.execute(
                select(Account.id, Tenant.name)
                .join(Tenant, Tenant.id == Account.tenant_id)
                .where(Account.id.in_({p.account_id for p in payments}))
            ).all()
        )
        invoices = dict(
            self.session.execute(
                select(Invoice.id, Invoice.reference).where(
                    Invoice.id.in_({p.invoice_id for p in payments})
                )
            ).all()
        )
        return [
            PaymentRead(
                id=p.id,
                reference=p.reference,
                account_id=p.account_id,
                tenant_name=tenants.get(p.account_id, ""),
                invoice_id=p.invoice_id,
                invoice_reference=invoices.get(p.invoice_id, ""),
                amount=p.amount,
                currency=p.currency,
                paid_on=p.paid_on,
                method=p.method,
                external_reference=p.external_reference,
                status=p.status,
                notes=p.notes,
                verified_at=p.verified_at,
                rejected_at=p.rejected_at,
                reject_reason=p.reject_reason,
                created_at=p.created_at,
            )
            for p in payments
        ]

    def payments(
        self,
        query: ListQuery,
        *,
        account_id: uuid.UUID | None = None,
        invoice_id: uuid.UUID | None = None,
        status: PaymentStatus | None = None,
    ) -> tuple[list[PaymentRead], int]:
        statement = (
            select(Payment)
            .join(Account, Account.id == Payment.account_id)
            .join(Tenant, Tenant.id == Account.tenant_id)
            .join(Invoice, Invoice.id == Payment.invoice_id)
        )
        for column, value in (
            (Payment.account_id, account_id),
            (Payment.invoice_id, invoice_id),
            (Payment.status, status),
        ):
            if value is not None:
                statement = statement.where(column == value)
        if query.search:
            statement = statement.where(
                text_search(
                    [Payment.reference, Payment.external_reference, Invoice.reference, Tenant.name],
                    query.search,
                )
            )
        statement = statement.order_by(Payment.paid_on.desc(), Payment.created_at.desc())
        rows, total = paginate_rows(self.session, statement, query)
        return self._payment_reads([r[0] for r in rows]), total

    def record_payment(self, invoice_id: uuid.UUID, values: dict[str, Any]) -> PaymentRead:
        invoice = self._invoice(invoice_id, lock=True)
        if invoice.status != InvoiceStatus.ISSUED:
            raise ConflictError("Payments are recorded only against an issued invoice.")
        if values["paid_on"] > business_today():
            raise BadRequestError("The payment date cannot be in the future.")
        reference = (values.get("external_reference") or "").strip() or None
        if reference:
            duplicate = self.session.scalar(
                select(Payment.reference).where(
                    Payment.invoice_id == invoice.id,
                    Payment.method == values["method"],
                    Payment.external_reference == reference,
                    Payment.status != PaymentStatus.REJECTED,
                )
            )
            if duplicate:
                raise ConflictError(
                    f"Payment {duplicate} already has this {values['method'].lower()} reference."
                )
        totals, paid, waiting = self._sums([invoice.id])
        room = totals[invoice.id] - paid[invoice.id] - waiting[invoice.id]
        if values["amount"] > room:
            raise ConflictError(
                f"That is more than is left to pay on {invoice.reference} "
                f"({invoice.currency} {room}, counting payments awaiting verification)."
            )
        payment = Payment(
            account_id=invoice.account_id,
            invoice_id=invoice.id,
            amount=values["amount"],
            currency=invoice.currency,
            paid_on=values["paid_on"],
            method=values["method"],
            external_reference=reference,
            notes=(values.get("notes") or "").strip() or None,
            recorded_by=self.actor.id,
        )
        self.session.add(payment)
        flush(self.session)
        self.session.refresh(payment, ["reference"])
        self._log(
            "payments",
            "recorded",
            payment.id,
            f"Payment {payment.reference} of {payment.currency} {payment.amount} recorded on "
            f"{invoice.reference}; it counts once verified.",
        )
        commit(self.session)
        return self._payment_reads([payment])[0]

    def _payment(self, payment_id: uuid.UUID) -> Payment:
        payment = self.session.scalar(
            select(Payment).where(Payment.id == payment_id).with_for_update()
        )
        if payment is None:
            raise NotFoundError("Payment not found.")
        if payment.status != PaymentStatus.RECORDED:
            raise ConflictError(f"Payment {payment.reference} is already {payment.status.lower()}.")
        return payment

    def verify(self, payment_id: uuid.UUID) -> PaymentRead:
        payment = self._payment(payment_id)
        payment.status = PaymentStatus.VERIFIED.value
        payment.verified_at = datetime.now(UTC)
        payment.verified_by = self.actor.id
        self._log("payments", "verified", payment.id, f"Payment {payment.reference} verified.")
        commit(self.session)
        return self._payment_reads([payment])[0]

    def reject(self, payment_id: uuid.UUID, reason: str) -> PaymentRead:
        payment = self._payment(payment_id)
        reason = reason.strip()
        if not reason:
            raise BadRequestError("Give a reason.")
        payment.status = PaymentStatus.REJECTED.value
        payment.rejected_at = datetime.now(UTC)
        payment.reject_reason = reason
        self._log(
            "payments",
            "rejected",
            payment.id,
            f"Payment {payment.reference} rejected. Reason: {reason}",
        )
        commit(self.session)
        return self._payment_reads([payment])[0]

    # --- across accounts --------------------------------------------------------------------

    def _accounts(self, account_id: uuid.UUID | None) -> list[tuple[Account, Tenant]]:
        AccountService(self.session, self.actor).ensure()
        statement = (
            select(Account, Tenant)
            .join(Tenant, Tenant.id == Account.tenant_id)
            .order_by(func.lower(Tenant.name), Account.id)
        )
        if account_id is not None:
            statement = statement.where(Account.id == account_id)
        return [(a, t) for a, t in self.session.execute(statement)]

    def balances(self, account_id: uuid.UUID | None = None) -> list[BalanceRead]:
        accounts = self._accounts(account_id)
        ids = [a.id for a, _ in accounts]
        if not ids:
            return []
        invoices = list(
            self.session.scalars(
                select(Invoice).where(
                    Invoice.account_id.in_(ids), Invoice.status == InvoiceStatus.ISSUED
                )
            )
        )
        totals, paid, waiting = self._sums([i.id for i in invoices])
        rows: dict[tuple[uuid.UUID, str], dict[str, Any]] = {}

        def row(account: uuid.UUID, currency: str) -> dict[str, Any]:
            return rows.setdefault(
                (account, currency),
                {"invoiced": ZERO, "paid": ZERO, "waiting": ZERO, "open": 0, "deposits": ZERO},
            )

        for i in invoices:
            r = row(i.account_id, i.currency)
            r["invoiced"] += totals[i.id]
            r["paid"] += paid[i.id]
            r["waiting"] += waiting[i.id]
            r["open"] += int(paid[i.id] < totals[i.id])
        tenant_of = {a.id: t for a, t in accounts}
        account_of = {t.id: a.id for a, t in accounts}
        for tenant_id, currency, deposit in self.session.execute(
            select(
                TenantOccupancy.tenant_id,
                TenantOccupancy.currency,
                TenantOccupancy.security_deposit,
            ).where(
                TenantOccupancy.tenant_id.in_(account_of),
                TenantOccupancy.status == ContractStatus.ACTIVE,
                TenantOccupancy.security_deposit.is_not(None),
            )
        ):
            row(account_of[tenant_id], currency)["deposits"] += deposit
        for a, _ in accounts:
            row(a.id, "INR")  # every account shows, even with nothing invoiced yet
        return [
            BalanceRead(
                account_id=account,
                tenant_name=tenant_of[account].name,
                currency=currency,
                invoiced=r["invoiced"],
                paid=r["paid"],
                awaiting_verification=r["waiting"],
                outstanding=r["invoiced"] - r["paid"],
                open_invoices=r["open"],
                deposits_held=r["deposits"],
            )
            for (account, currency), r in sorted(
                rows.items(), key=lambda kv: (tenant_of[kv[0][0]].name.lower(), kv[0][1])
            )
        ]

    def deposits(self, account_id: uuid.UUID | None = None) -> list[DepositRow]:
        accounts = self._accounts(account_id)
        account_of = {t.id: (a.id, t.name) for a, t in accounts}
        if not account_of:
            return []
        contracts = list(
            self.session.scalars(
                select(TenantOccupancy)
                .where(
                    TenantOccupancy.tenant_id.in_(account_of),
                    TenantOccupancy.status == ContractStatus.ACTIVE,
                    TenantOccupancy.security_deposit.is_not(None),
                )
                .order_by(TenantOccupancy.start_date.desc(), TenantOccupancy.id)
            )
        )
        reads = {r.id: r for r in ContractService(self.session, self.actor).to_read(contracts)}
        return [
            DepositRow(
                account_id=account_of[c.tenant_id][0],
                tenant_name=account_of[c.tenant_id][1],
                contract_id=c.id,
                contract_reference=c.reference,
                contract_status=reads[c.id].status,
                property_name=reads[c.id].property_name,
                amount=c.security_deposit,  # type: ignore[arg-type]
                currency=c.currency,
            )
            for c in contracts
        ]

    def spaces_and_bookings(self, account_id: uuid.UUID | None = None) -> list[SpaceBookingRow]:
        accounts = self._accounts(account_id)
        account_of = {t.id: (a.id, t.name) for a, t in accounts}
        if not account_of:
            return []
        contracts = list(
            self.session.scalars(
                select(TenantOccupancy).where(TenantOccupancy.tenant_id.in_(account_of))
            )
        )
        rows: list[SpaceBookingRow] = []
        for r in ContractService(self.session, self.actor).to_read(contracts):
            if r.status == ContractStatus.ACTIVE and r.holds_inventory:
                rows.append(
                    SpaceBookingRow(
                        account_id=account_of[r.tenant_id][0],
                        tenant_name=account_of[r.tenant_id][1],
                        kind="CONTRACT",
                        reference=r.reference,
                        property_name=r.property_name,
                        space_name=r.space_name,
                        starts=r.start_date,
                        ends=r.end_date,
                        status=r.status,
                    )
                )
        today = business_today()
        for booking, prop, space in self.session.execute(
            select(Booking, Property.name, Space.name)
            .join(Property, Property.id == Booking.property_id)
            .outerjoin(Space, Space.id == Booking.space_id)
            .where(
                Booking.tenant_id.in_(account_of),
                Booking.status != BookingStatus.CANCELLED,
                or_(Booking.end_at.is_(None), func.date(Booking.end_at) >= today),
            )
            .order_by(Booking.start_at)
        ):
            rows.append(
                SpaceBookingRow(
                    account_id=account_of[booking.tenant_id][0],
                    tenant_name=account_of[booking.tenant_id][1],
                    kind="BOOKING",
                    reference=booking.reference,
                    property_name=prop,
                    space_name=space,
                    starts=booking.start_at.date(),
                    ends=booking.end_at.date() if booking.end_at else None,
                    status=booking.status,
                )
            )
        rows.sort(key=lambda r: (r.tenant_name.lower(), r.starts))
        return rows
