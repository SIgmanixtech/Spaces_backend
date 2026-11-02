"""Tenant accounts (BR-017, BR-020, BR-025, BR-030; read-only Accounts phase).

Approved (2 Nov 2026): one account per tenant with a completed contract (BR-025: approved
and its agreement recorded as signed), opened the first time the Accounts pages are read,
idempotently and never twice; no formatted reference until D7 is confirmed. Accounts are
read-only for now: they gather the tenant's contracts (the same records as CRM, BR-020),
the spaces those contracts hold, the security deposits agreed in them (held for the
tenant, never revenue, BR-030) and the reimbursement terms and expenses (BR-029).

Not here, by decision: invoices (D6, D7), payments and their allocation (BR-019 waits for
invoices), advances (D5). Nothing in this module records money as received or earned.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from sqlalchemy import exists, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.models.account import Account
from app.models.enums import ContractStatus, ReimbursementStatus
from app.models.property import Property
from app.models.reimbursement import (
    ReimbursementExpense,
    ReimbursementExpenseType,
    ReimbursementTerm,
)
from app.models.tenant import Tenant, TenantOccupancy
from app.models.user import AppUser
from app.repositories.base import paginate_rows, text_search
from app.schemas.account import (
    AccountDeposit,
    AccountExpense,
    AccountRead,
    AccountReimbursementTerm,
    AccountSpace,
    AccountSummary,
    Money,
)
from app.schemas.common import ListQuery
from app.services.base import commit, log_activity
from app.services.client_access import access_status, completed_contract_condition
from app.services.contracts import ContractService

NOT_AVAILABLE_YET = [
    "Invoices: billing frequency, generation and due dates, GST, late fees and discounts (D6) "
    "and invoice numbers (D7) are not decided.",
    "Payments: recording and allocating payments waits for invoices (BR-019).",
    "Advances: collection, application to invoices, refunds and settlement (D5) are not decided.",
    "Account reference: the SGX-ACC format (D7) is only proposed.",
]


def _money(totals: dict[str, Decimal]) -> list[Money]:
    return [Money(currency=c, amount=a) for c, a in sorted(totals.items())]


class AccountService:
    def __init__(self, session: Session, actor: AppUser) -> None:
        self.session = session
        self.actor = actor

    def ensure(self) -> int:
        """Open an account for every tenant with a completed contract that has none yet.
        Safe to repeat and to run at the same time: one account per tenant (unique key)."""
        tenants = list(
            self.session.scalars(
                select(TenantOccupancy.tenant_id)
                .where(completed_contract_condition())
                .where(~exists().where(Account.tenant_id == TenantOccupancy.tenant_id))
                .distinct()
            )
        )
        opened = 0
        for tenant_id in tenants:
            new_id = self.session.scalar(
                insert(Account)
                .values(tenant_id=tenant_id, created_by=self.actor.id)
                .on_conflict_do_nothing(index_elements=["tenant_id"])
                .returning(Account.id)
            )
            if new_id is None:
                continue  # opened by someone else meanwhile
            opened += 1
            name = self.session.scalar(select(Tenant.name).where(Tenant.id == tenant_id))
            log_activity(
                self.session,
                self.actor,
                action="opened",
                entity_type="accounts",
                entity_id=new_id,
                property_id=None,
                message=f"Account opened for {name}: the tenant has a completed contract."[:500],
            )
        if tenants:
            commit(self.session)
        return opened

    # --- reading ------------------------------------------------------------------------

    def _summaries(self, rows: Iterable[tuple[Account, Tenant]]) -> list[dict[str, Any]]:
        rows = list(rows)
        tenant_ids = [t.id for _, t in rows]
        if not tenant_ids:
            return []
        contracts = list(
            self.session.scalars(
                select(TenantOccupancy).where(TenantOccupancy.tenant_id.in_(tenant_ids))
            )
        )
        completed = set(
            self.session.scalars(
                select(TenantOccupancy.id).where(
                    TenantOccupancy.tenant_id.in_(tenant_ids), completed_contract_condition()
                )
            )
        )
        reads = {c.id: c for c in ContractService(self.session, self.actor).to_read(contracts)}
        by_tenant: dict[uuid.UUID, list[TenantOccupancy]] = defaultdict(list)
        for c in contracts:
            by_tenant[c.tenant_id].append(c)
        expenses = self.session.execute(
            select(
                TenantOccupancy.tenant_id,
                ReimbursementExpense.status,
                ReimbursementExpense.currency,
                ReimbursementExpense.amount,
            )
            .join(TenantOccupancy, TenantOccupancy.id == ReimbursementExpense.contract_id)
            .where(TenantOccupancy.tenant_id.in_(tenant_ids))
        ).all()
        pending: dict[uuid.UUID, int] = defaultdict(int)
        reviewed: dict[uuid.UUID, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
        for tenant_id, status, currency, amount in expenses:
            if status == ReimbursementStatus.PENDING_REVIEW:
                pending[tenant_id] += 1
            elif status == ReimbursementStatus.REVIEWED:
                reviewed[tenant_id][currency] += amount
        out = []
        for account, tenant in rows:
            mine = by_tenant.get(tenant.id, [])
            deposits: dict[str, Decimal] = defaultdict(Decimal)
            for c in mine:
                # Agreed in an approved contract (stored ACTIVE: running or expired).
                if c.status == ContractStatus.ACTIVE and c.security_deposit is not None:
                    deposits[c.currency] += c.security_deposit
            out.append(
                {
                    "id": account.id,
                    "tenant_id": tenant.id,
                    "tenant_name": tenant.name,
                    "company_name": tenant.company_name,
                    "email": tenant.email,
                    "phone": tenant.phone,
                    "tenant_status": tenant.status,
                    "tenant_archived": tenant.archived_at is not None,
                    "client_access": access_status(tenant),
                    "contracts": len(mine),
                    "completed_contracts": sum(c.id in completed for c in mine),
                    "holding_contracts": sum(
                        reads[c.id].status == ContractStatus.ACTIVE and reads[c.id].holds_inventory
                        for c in mine
                    ),
                    "agreed_deposits": _money(deposits),
                    "reimbursements_pending_review": pending.get(tenant.id, 0),
                    "reimbursements_reviewed": _money(reviewed.get(tenant.id, {})),
                    "opened_at": account.created_at,
                    "lead_id": account.lead_id,
                }
            )
        return out

    def accounts(
        self, query: ListQuery, property_id: uuid.UUID | None = None
    ) -> tuple[list[AccountSummary], int]:
        self.ensure()
        statement = (
            select(Account, Tenant)
            .join(Tenant, Tenant.id == Account.tenant_id)
            .order_by(func.lower(Tenant.name), Account.id)
        )
        if query.search:
            statement = statement.where(
                text_search(
                    [Tenant.name, Tenant.company_name, Tenant.email, Tenant.phone], query.search
                )
            )
        if property_id is not None:
            statement = statement.where(
                exists().where(
                    TenantOccupancy.tenant_id == Tenant.id,
                    TenantOccupancy.property_id == property_id,
                )
            )
        rows, total = paginate_rows(self.session, statement, query)
        summaries = self._summaries((r[0], r[1]) for r in rows)
        return [
            AccountSummary(**{k: v for k, v in s.items() if k != "lead_id"}) for s in summaries
        ], total

    def get(self, account_id: uuid.UUID) -> AccountRead:
        self.ensure()
        found = self.session.execute(
            select(Account, Tenant)
            .join(Tenant, Tenant.id == Account.tenant_id)
            .where(Account.id == account_id)
        ).first()
        if found is None:
            raise NotFoundError("Account not found.")
        account, tenant = found
        summary = self._summaries([(account, tenant)])[0]
        contracts = list(
            self.session.scalars(
                select(TenantOccupancy)
                .where(TenantOccupancy.tenant_id == tenant.id)
                .order_by(TenantOccupancy.start_date.desc(), TenantOccupancy.id)
            )
        )
        reads = ContractService(self.session, self.actor).to_read(contracts)
        reference = {c.id: c.reference for c in contracts}
        ids = list(reference)
        types = dict(
            self.session.execute(
                select(ReimbursementExpenseType.id, ReimbursementExpenseType.name)
            ).all()
        )
        terms = (
            list(
                self.session.scalars(
                    select(ReimbursementTerm)
                    .where(ReimbursementTerm.contract_id.in_(ids))
                    .order_by(ReimbursementTerm.created_at)
                )
            )
            if ids
            else []
        )
        expenses = (
            list(
                self.session.scalars(
                    select(ReimbursementExpense)
                    .where(ReimbursementExpense.contract_id.in_(ids))
                    .order_by(ReimbursementExpense.period_start.desc(), ReimbursementExpense.id)
                )
            )
            if ids
            else []
        )
        properties = dict(
            self.session.execute(
                select(Property.id, Property.name).where(
                    Property.id.in_({c.property_id for c in contracts})
                )
            ).all()
        )
        return AccountRead(
            **summary,
            contract_list=reads,
            spaces=[
                AccountSpace(
                    contract_reference=r.reference,
                    property_id=r.property_id,
                    property_name=properties.get(r.property_id, r.property_name),
                    space_name=r.space_name,
                )
                for r in reads
                if r.status == ContractStatus.ACTIVE and r.holds_inventory
            ],
            deposits=[
                AccountDeposit(
                    contract_id=c.id,
                    contract_reference=c.reference,
                    contract_status=next(r.status for r in reads if r.id == c.id),
                    amount=c.security_deposit,
                    currency=c.currency,
                )
                for c in contracts
                if c.security_deposit is not None and c.status == ContractStatus.ACTIVE
            ],
            reimbursement_terms=[
                AccountReimbursementTerm(
                    id=t.id,
                    contract_id=t.contract_id,  # type: ignore[arg-type]
                    contract_reference=reference[t.contract_id],  # type: ignore[index]
                    expense_type=types.get(t.expense_type_id, "—"),
                    method=t.method,
                    amount=t.amount,
                    recurrence=t.recurrence,
                    billing_period=t.billing_period,
                    currency=t.currency,
                )
                for t in terms
            ],
            expenses=[
                AccountExpense(
                    id=e.id,
                    contract_id=e.contract_id,
                    contract_reference=reference[e.contract_id],
                    expense_type=types.get(e.expense_type_id, "—"),
                    period_start=e.period_start,
                    period_end=e.period_end,
                    amount=e.amount,
                    currency=e.currency,
                    description=e.description,
                    one_time=e.one_time,
                    status=e.status,
                )
                for e in expenses
            ],
            not_available_yet=NOT_AVAILABLE_YET,
        )

    def for_tenant(self, tenant_id: uuid.UUID) -> uuid.UUID | None:
        """The tenant's account id, once it has one."""
        self.ensure()
        return self.session.scalar(select(Account.id).where(Account.tenant_id == tenant_id))
