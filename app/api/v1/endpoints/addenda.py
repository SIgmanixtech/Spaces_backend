"""Addenda: changes to an approved contract's terms, applied once signed (LC-3, LC-4)."""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Form, UploadFile
from pydantic import TypeAdapter, ValidationError

from app.api.dependencies import CurrentAdmin, DbSession
from app.api.responses import NOT_FOUND, PROTECTED, _error
from app.api.v1.endpoints.files import Storage
from app.core.errors import BadRequestError
from app.schemas.addendum import AddendumCancel, AddendumCreate, AddendumRead
from app.schemas.agreement import LinkRead, Signer
from app.services.addenda import AddendumService
from app.services.agreements import MAX_SIGNED_COPY_BYTES

router = APIRouter(tags=["Contracts"], responses=PROTECTED)
_SIGNERS = TypeAdapter(list[Signer])


@router.get(
    "/contracts/{contract_id}/addenda",
    response_model=list[AddendumRead],
    responses=NOT_FOUND,
    summary="A contract's addenda",
)
def list_addenda(
    admin: CurrentAdmin, session: DbSession, contract_id: uuid.UUID
) -> list[AddendumRead]:
    return [
        AddendumRead.model_validate(a) for a in AddendumService(session, admin).list(contract_id)
    ]


@router.post(
    "/contracts/{contract_id}/addenda",
    response_model=AddendumRead,
    status_code=201,
    responses={**NOT_FOUND, 409: _error("The contract is not approved.")},
    summary="Draft an addendum",
)
def create_addendum(
    admin: CurrentAdmin, session: DbSession, contract_id: uuid.UUID, body: AddendumCreate
) -> AddendumRead:
    """A draft changes nothing; the contract's terms stay until the addendum is signed."""
    addendum = AddendumService(session, admin).create(
        contract_id, body.effective_date, body.summary, body.changes
    )
    return AddendumRead.model_validate(addendum)


@router.post(
    "/addenda/{addendum_id}/cancel",
    response_model=AddendumRead,
    responses=NOT_FOUND,
    summary="Cancel a draft addendum",
)
def cancel_addendum(
    admin: CurrentAdmin, session: DbSession, addendum_id: uuid.UUID, body: AddendumCancel
) -> AddendumRead:
    return AddendumRead.model_validate(
        AddendumService(session, admin).cancel(addendum_id, body.reason)
    )


@router.post(
    "/addenda/{addendum_id}/signed-copy",
    response_model=AddendumRead,
    responses=NOT_FOUND,
    summary="Record the signed addendum and apply it",
)
def sign_addendum(
    admin: CurrentAdmin,
    session: DbSession,
    storage: Storage,
    addendum_id: uuid.UUID,
    file: UploadFile,
    signed_on: Annotated[date, Form()],
    signers: Annotated[
        str,
        Form(
            description='JSON: [{"party": "TENANT", "name": "...", "designation": "...", '
            '"company": "...", "signed_on": "YYYY-MM-DD"}]'
        ),
    ],
) -> AddendumRead:
    """PDF of the paper-signed addendum; both the tenant and SIGMANIX must have signed
    (D11.3). Its changes are then applied to the contract."""
    try:
        people = _SIGNERS.validate_json(signers or "[]")
    except ValidationError as exc:
        raise BadRequestError(
            "signers must be a JSON list of {party: TENANT or COMPANY, name, designation}."
        ) from exc
    data = file.file.read(MAX_SIGNED_COPY_BYTES + 1)
    if not data:
        raise BadRequestError("The file is empty.")
    addendum = AddendumService(session, admin, storage).sign(
        addendum_id,
        data=data,
        file_name=file.filename or "addendum.pdf",
        signed_on=signed_on,
        signers=[p.model_dump() for p in people],
    )
    return AddendumRead.model_validate(addendum)


@router.get(
    "/addenda/{addendum_id}/signed-copy/download",
    response_model=LinkRead,
    responses=NOT_FOUND,
    summary="A short-lived link to a signed addendum",
)
def download_addendum(
    admin: CurrentAdmin, session: DbSession, storage: Storage, addendum_id: uuid.UUID
) -> LinkRead:
    return LinkRead(url=AddendumService(session, admin, storage).link(addendum_id), expires_in=300)
