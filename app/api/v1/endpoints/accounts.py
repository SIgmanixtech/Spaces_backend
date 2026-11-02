"""Tenant accounts (BR-017, BR-020; read-only Accounts phase). Admin only, under /admin.

Reading these endpoints opens the account of any tenant that has a completed contract
(BR-025) and none yet: one per tenant, never two. Nothing here records or changes money;
invoices, payments and advances wait for D5, D6 and D7.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import CurrentAdmin, DbSession, ListParams
from app.api.responses import NOT_FOUND, PROTECTED
from app.schemas.account import AccountRead, AccountSummary
from app.schemas.common import Page, ReadSchema
from app.services.accounts import AccountService

router = APIRouter(prefix="/accounts", tags=["Accounts"], responses=PROTECTED)


class TenantAccount(ReadSchema):
    account_id: uuid.UUID | None


@router.get("", response_model=Page[AccountSummary], summary="Tenant accounts")
def list_accounts(
    admin: CurrentAdmin,
    session: DbSession,
    query: ListParams,
    property_id: Annotated[
        uuid.UUID | None, Query(description="Tenants with a contract at this property.")
    ] = None,
) -> Page[AccountSummary]:
    """By tenant name. Search matches the tenant's name, company, email and phone."""
    items, total = AccountService(session, admin).accounts(query, property_id)
    return Page(items=items, total=total, page=query.page, page_size=query.page_size)


@router.get(
    "/by-tenant/{tenant_id}",
    response_model=TenantAccount,
    summary="The account of one tenant, if it has one",
)
def account_of_tenant(
    admin: CurrentAdmin, session: DbSession, tenant_id: uuid.UUID
) -> TenantAccount:
    """Null until the tenant has a completed contract."""
    return TenantAccount(account_id=AccountService(session, admin).for_tenant(tenant_id))


@router.get("/{account_id}", response_model=AccountRead, responses=NOT_FOUND, summary="One account")
def get_account(admin: CurrentAdmin, session: DbSession, account_id: uuid.UUID) -> AccountRead:
    """Contracts (the same records as CRM), spaces held, agreed deposits (held, not
    revenue), reimbursement terms and expenses, and what is not available yet and why."""
    return AccountService(session, admin).get(account_id)
