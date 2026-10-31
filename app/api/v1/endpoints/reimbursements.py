"""Reimbursement Fee (business rule BR-029): expense types, property defaults and contract
terms, actual expenses and their review. Administrators only."""

import uuid
from datetime import date

from fastapi import APIRouter, Query

from app.api.dependencies import CurrentAdmin, DbSession
from app.api.responses import CONFLICT, NOT_FOUND, PROTECTED
from app.models.enums import ReimbursementStatus
from app.schemas.reimbursement import (
    AdditionalChargeReview,
    ApplyDefaultsResult,
    ExpenseCreate,
    ExpenseRead,
    ExpenseReview,
    ExpenseTypeCreate,
    ExpenseTypeRead,
    ExpenseTypeUpdate,
    ExpenseUpdate,
    TermCreate,
    TermRead,
    TermUpdate,
)
from app.services.reimbursements import ReimbursementService

router = APIRouter(prefix="/reimbursements", tags=["Reimbursements"], responses=PROTECTED)


@router.get("/expense-types", response_model=list[ExpenseTypeRead], summary="List expense types")
def list_types(
    admin: CurrentAdmin, session: DbSession, include_inactive: bool = True
) -> list[ExpenseTypeRead]:
    return ReimbursementService(session, admin).types(include_inactive)


@router.post(
    "/expense-types",
    response_model=ExpenseTypeRead,
    status_code=201,
    responses=CONFLICT,
    summary="Add an expense type",
)
def create_type(
    admin: CurrentAdmin, session: DbSession, body: ExpenseTypeCreate
) -> ExpenseTypeRead:
    """409 when a type with the same name (ignoring case and extra spaces) exists."""
    return ReimbursementService(session, admin).create_type(body.name, body.description)


@router.patch(
    "/expense-types/{type_id}",
    response_model=ExpenseTypeRead,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Rename or retire an expense type",
)
def update_type(
    admin: CurrentAdmin, session: DbSession, type_id: uuid.UUID, body: ExpenseTypeUpdate
) -> ExpenseTypeRead:
    return ReimbursementService(session, admin).update_type(
        type_id, body.model_dump(exclude_unset=True)
    )


@router.get("/terms", response_model=list[TermRead], summary="Property defaults or contract terms")
def list_terms(
    admin: CurrentAdmin,
    session: DbSession,
    property_id: uuid.UUID,
    contract_id: uuid.UUID | None = Query(
        default=None, description="Omit for the property defaults."
    ),
) -> list[TermRead]:
    return ReimbursementService(session, admin).terms(property_id, contract_id)


@router.post(
    "/terms",
    response_model=TermRead,
    status_code=201,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Add a property default or a contract term",
)
def create_term(admin: CurrentAdmin, session: DbSession, body: TermCreate) -> TermRead:
    return ReimbursementService(session, admin).create_term(body.model_dump())


@router.patch(
    "/terms/{term_id}", response_model=TermRead, responses=NOT_FOUND, summary="Change a term"
)
def update_term(
    admin: CurrentAdmin, session: DbSession, term_id: uuid.UUID, body: TermUpdate
) -> TermRead:
    return ReimbursementService(session, admin).update_term(
        term_id, body.model_dump(exclude_unset=True)
    )


@router.delete("/terms/{term_id}", status_code=204, responses=NOT_FOUND, summary="Remove a term")
def delete_term(admin: CurrentAdmin, session: DbSession, term_id: uuid.UUID) -> None:
    ReimbursementService(session, admin).delete_term(term_id)


@router.post(
    "/contracts/{contract_id}/apply-defaults",
    response_model=ApplyDefaultsResult,
    responses=NOT_FOUND,
    summary="Copy the property's defaults onto a contract (types it lacks only)",
)
def apply_defaults(
    admin: CurrentAdmin, session: DbSession, contract_id: uuid.UUID
) -> ApplyDefaultsResult:
    """Existing contract terms are never changed; their types are listed as skipped."""
    created, skipped = ReimbursementService(session, admin).apply_defaults(contract_id)
    return ApplyDefaultsResult(created=created, skipped=skipped)


@router.get("/expenses", response_model=list[ExpenseRead], summary="List entered expenses")
def list_expenses(
    admin: CurrentAdmin,
    session: DbSession,
    contract_id: uuid.UUID | None = None,
    property_id: uuid.UUID | None = None,
    status: ReimbursementStatus | None = None,
) -> list[ExpenseRead]:
    return ReimbursementService(session, admin).expenses(
        contract_id=contract_id, property_id=property_id, status=status
    )


@router.post(
    "/expenses",
    response_model=ExpenseRead,
    status_code=201,
    responses=NOT_FOUND,
    summary="Enter an actual expense for a contract and period",
)
def create_expense(admin: CurrentAdmin, session: DbSession, body: ExpenseCreate) -> ExpenseRead:
    """It waits for review; only reviewed expenses can reach an invoice."""
    return ReimbursementService(session, admin).create_expense(body.model_dump())


@router.patch(
    "/expenses/{expense_id}",
    response_model=ExpenseRead,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Correct an expense before review",
)
def update_expense(
    admin: CurrentAdmin, session: DbSession, expense_id: uuid.UUID, body: ExpenseUpdate
) -> ExpenseRead:
    return ReimbursementService(session, admin).update_expense(
        expense_id, body.model_dump(exclude_unset=True)
    )


@router.post(
    "/expenses/{expense_id}/review",
    response_model=ExpenseRead,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Mark an expense reviewed (billable) or rejected",
)
def review_expense(
    admin: CurrentAdmin, session: DbSession, expense_id: uuid.UUID, body: ExpenseReview
) -> ExpenseRead:
    return ReimbursementService(session, admin).review(expense_id, body.decision, body.note)


@router.get(
    "/contracts/{contract_id}/billable",
    response_model=list[ExpenseRead],
    summary="Reviewed expenses an invoice for this period may include",
)
def billable(
    admin: CurrentAdmin, session: DbSession, contract_id: uuid.UUID, start: date, end: date
) -> list[ExpenseRead]:
    return ReimbursementService(session, admin).billable(contract_id, start, end)


@router.get(
    "/additional-charges-review",
    response_model=list[AdditionalChargeReview],
    summary="Existing additional charges, and whether they look like reimbursements",
)
def additional_charges_review(
    admin: CurrentAdmin, session: DbSession, property_id: uuid.UUID | None = None
) -> list[AdditionalChargeReview]:
    """Read-only: nothing is reclassified or changed."""
    return ReimbursementService(session, admin).additional_charges_review(property_id)
