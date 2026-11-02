"""Tenant accounts (BR-017, BR-020, BR-030; read-only Accounts phase)."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import Field

from app.schemas.common import ReadSchema
from app.schemas.contract import ContractRead


class Money(ReadSchema):
    currency: str
    amount: Decimal


class AccountSummary(ReadSchema):
    id: uuid.UUID
    tenant_id: uuid.UUID
    tenant_name: str
    company_name: str | None
    email: str | None
    phone: str | None
    tenant_status: str
    tenant_archived: bool
    client_access: str
    contracts: int
    completed_contracts: int
    holding_contracts: int = Field(description="Contracts holding space now (active, in dates).")
    agreed_deposits: list[Money] = Field(
        description="Security deposits agreed in approved contracts. Held for the tenant, never "
        "revenue; whether they were received is not recorded yet (payments wait for D5-D7)."
    )
    reimbursements_pending_review: int
    reimbursements_reviewed: list[Money] = Field(
        description="Reviewed actual expenses. Not invoiced yet: invoices wait for D6."
    )
    opened_at: datetime


class AccountDeposit(ReadSchema):
    contract_id: uuid.UUID
    contract_reference: str
    contract_status: str
    amount: Decimal
    currency: str


class AccountReimbursementTerm(ReadSchema):
    id: uuid.UUID
    contract_id: uuid.UUID
    contract_reference: str
    expense_type: str
    method: str
    amount: Decimal | None
    recurrence: str
    billing_period: str | None
    currency: str


class AccountExpense(ReadSchema):
    id: uuid.UUID
    contract_id: uuid.UUID
    contract_reference: str
    expense_type: str
    period_start: date
    period_end: date
    amount: Decimal
    currency: str
    description: str
    one_time: bool
    status: str


class AccountSpace(ReadSchema):
    contract_reference: str
    property_id: uuid.UUID
    property_name: str
    space_name: str | None = Field(description="Null when the contract holds the whole property.")


class AccountRead(AccountSummary):
    lead_id: uuid.UUID | None = Field(description="Set once lead conversion exists (Phase 5).")
    contract_list: list[ContractRead]
    spaces: list[AccountSpace]
    deposits: list[AccountDeposit]
    reimbursement_terms: list[AccountReimbursementTerm]
    expenses: list[AccountExpense]
    not_available_yet: list[str] = Field(
        description="Accounts features waiting for business decisions, by decision."
    )
