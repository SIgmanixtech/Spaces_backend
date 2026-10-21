"""Contracts: a tenant holding a whole property, or one space with everything inside it."""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import CurrentAdmin, DbSession, ListParams
from app.api.responses import NOT_FOUND, PROTECTED, _error
from app.schemas.common import Page
from app.schemas.contract import (
    ContractAvailability,
    ContractCancel,
    ContractCreate,
    ContractExtend,
    ContractRead,
    ContractScope,
    ContractState,
    ContractTerminate,
    ContractUpdate,
)
from app.schemas.dashboard import ActivityEntry
from app.services.contracts import ContractService

router = APIRouter(prefix="/contracts", tags=["Contracts"], responses=PROTECTED)

HELD = {
    409: _error(
        "The space, a space containing it, a space inside it, or the whole property is "
        "already booked or contracted for part of that time. `conflicts` names each hold "
        "in the way. Also returned when the requested status change is not allowed."
    )
}


@router.get("", response_model=Page[ContractRead], summary="List contracts")
def list_contracts(
    admin: CurrentAdmin,
    session: DbSession,
    query: ListParams,
    property_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    space_id: uuid.UUID | None = None,
    status: ContractState | None = None,
) -> Page[ContractRead]:
    """Latest start first. Search matches the contract reference."""
    items, total = ContractService(session, admin).list(
        query, property_id=property_id, tenant_id=tenant_id, space_id=space_id, status=status
    )
    return Page(items=items, total=total, page=query.page, page_size=query.page_size)


@router.get(
    "/availability",
    response_model=ContractAvailability,
    responses=NOT_FOUND,
    summary="Check whether a space can be contracted",
)
def contract_availability(
    admin: CurrentAdmin,
    session: DbSession,
    property_id: uuid.UUID,
    start_date: date,
    space_id: Annotated[
        uuid.UUID | None, Query(description="Omit to check the whole property.")
    ] = None,
    end_date: Annotated[date | None, Query(description="Last day, inclusive.")] = None,
    tenant_id: Annotated[
        uuid.UUID | None,
        Query(description="The tenant asking: their own whole-property contract is no obstacle."),
    ] = None,
    exclude_contract_id: Annotated[
        uuid.UUID | None,
        Query(description="Ignore this contract, e.g. when checking an extension."),
    ] = None,
) -> ContractAvailability:
    """Advisory: activating checks again inside its own transaction, and the database
    has the final word."""
    return ContractService(session, admin).availability(
        property_id=property_id,
        space_id=space_id,
        start_date=start_date,
        end_date=end_date,
        tenant_id=tenant_id,
        exclude_contract_id=exclude_contract_id,
    )


@router.post(
    "",
    response_model=ContractRead,
    status_code=201,
    responses={**NOT_FOUND, **HELD},
    summary="Create a contract",
)
def create_contract(admin: CurrentAdmin, session: DbSession, body: ContractCreate) -> ContractRead:
    """PENDING by default, holding nothing. With `activate: true` it is activated at
    once, which fails with 409 if anything is in the way (and then nothing is saved)."""
    return ContractService(session, admin).create(body.model_dump())


@router.get(
    "/{contract_id}", response_model=ContractRead, responses=NOT_FOUND, summary="Get a contract"
)
def get_contract(admin: CurrentAdmin, session: DbSession, contract_id: uuid.UUID) -> ContractRead:
    return ContractService(session, admin).get(contract_id)


@router.patch(
    "/{contract_id}",
    response_model=ContractRead,
    responses={**NOT_FOUND, **HELD},
    summary="Change a contract's terms",
)
def update_contract(
    admin: CurrentAdmin, session: DbSession, contract_id: uuid.UUID, body: ContractUpdate
) -> ContractRead:
    """Rent, deposit, currency, terms and notes at any time; dates only while PENDING.
    The status changes only through activate, cancel, terminate and extend."""
    return ContractService(session, admin).update(contract_id, body.model_dump(exclude_unset=True))


@router.post(
    "/{contract_id}/activate",
    response_model=ContractRead,
    responses={**NOT_FOUND, **HELD},
    summary="Activate a pending contract",
)
def activate_contract(
    admin: CurrentAdmin, session: DbSession, contract_id: uuid.UUID
) -> ContractRead:
    """Holds the space, and everything inside it, for the contract's dates."""
    return ContractService(session, admin).activate(contract_id)


@router.post(
    "/{contract_id}/cancel",
    response_model=ContractRead,
    responses={**NOT_FOUND, **HELD},
    summary="Cancel a contract",
)
def cancel_contract(
    admin: CurrentAdmin,
    session: DbSession,
    contract_id: uuid.UUID,
    body: ContractCancel | None = None,
) -> ContractRead:
    """For a pending contract, or an active one that has not started. Releases the
    space; the contract is kept as CANCELLED. A started contract is terminated instead."""
    return ContractService(session, admin).cancel(contract_id, body.reason if body else None)


@router.post(
    "/{contract_id}/terminate",
    response_model=ContractRead,
    responses={**NOT_FOUND, **HELD},
    summary="End an active contract early",
)
def terminate_contract(
    admin: CurrentAdmin, session: DbSession, contract_id: uuid.UUID, body: ContractTerminate
) -> ContractRead:
    """The contract holds through `termination_date` and the space is free from the
    next day. The agreed end date is kept as `original_end_date`."""
    return ContractService(session, admin).terminate(
        contract_id, body.termination_date, body.reason
    )


@router.post(
    "/{contract_id}/extend",
    response_model=ContractRead,
    responses={**NOT_FOUND, **HELD},
    summary="Change an active contract's end date",
)
def extend_contract(
    admin: CurrentAdmin, session: DbSession, contract_id: uuid.UUID, body: ContractExtend
) -> ContractRead:
    """Extends (or renews, or shortens) the contract. Refused with 409, naming what is
    in the way, if the longer period is held by someone else."""
    return ContractService(session, admin).extend(contract_id, body.end_date)


@router.get(
    "/{contract_id}/scope",
    response_model=ContractScope,
    responses=NOT_FOUND,
    summary="Spaces a contract holds",
)
def contract_scope(
    admin: CurrentAdmin, session: DbSession, contract_id: uuid.UUID
) -> ContractScope:
    """The contracted space and every space inside it: what no one else can book or
    contract while this contract holds."""
    return ContractService(session, admin).scope(contract_id)


@router.get(
    "/{contract_id}/history",
    response_model=list[ActivityEntry],
    responses=NOT_FOUND,
    summary="A contract's history",
)
def contract_history(
    admin: CurrentAdmin, session: DbSession, contract_id: uuid.UUID
) -> list[ActivityEntry]:
    """Every recorded change, oldest first. Not paginated."""
    return ContractService(session, admin).history(contract_id)
