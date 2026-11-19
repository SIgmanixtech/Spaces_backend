"""Agreements for one-time bookings (administrators only), e.g. the Conference Room Usage
Agreement. See app/services/booking_agreements.py for the rules."""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Form, Response, UploadFile
from pydantic import TypeAdapter, ValidationError

from app.api.dependencies import CurrentAdmin, DbSession
from app.api.responses import NOT_FOUND, PROTECTED, _error
from app.api.v1.endpoints.files import Storage
from app.core.errors import BadRequestError
from app.models.agreement import BookingDocument
from app.models.booking import Booking
from app.schemas.agreement import LinkRead, Signer
from app.schemas.booking_agreement import (
    BookingAgreementGenerate,
    BookingAgreementPlan,
    BookingAgreementRead,
    BookingDocumentRead,
)
from app.services.agreements import MAX_SIGNED_COPY_BYTES, MAX_TEMPLATE_BYTES
from app.services.base import require
from app.services.booking_agreements import (
    AGREEMENT_LABEL,
    LIFECYCLE,
    BookingAgreementService,
)

router = APIRouter(tags=["Booking agreements"], responses=PROTECTED)
CONFLICT = {409: _error("Not allowed in the current state; the message says why.")}
_SIGNERS = TypeAdapter(list[Signer])


def document_read(document: BookingDocument) -> BookingDocumentRead:
    read = BookingDocumentRead.model_validate(document)
    read.lifecycle = LIFECYCLE.get(document.status, document.status)
    return read


def agreement_read(service: BookingAgreementService, booking: Booking) -> BookingAgreementRead:
    documents = service.documents(booking.id)
    current = next((d for d in documents if d.status != "SUPERSEDED"), None)
    return BookingAgreementRead(
        booking_id=booking.id,
        booking_reference=booking.reference,
        booking_status=booking.status,
        agreement_label=AGREEMENT_LABEL,
        current=document_read(current) if current else None,
        history=[document_read(d) for d in documents],
    )


@router.get(
    "/bookings/{booking_id}/agreement",
    response_model=BookingAgreementRead,
    responses=NOT_FOUND,
    summary="The booking's agreement: current document, status and history",
)
def booking_agreement(
    admin: CurrentAdmin, session: DbSession, booking_id: uuid.UUID
) -> BookingAgreementRead:
    booking = require(session, Booking, booking_id, "Booking")
    return agreement_read(BookingAgreementService(session, admin), booking)


@router.post(
    "/bookings/{booking_id}/agreement",
    response_model=BookingDocumentRead,
    responses={
        **NOT_FOUND,
        **CONFLICT,
        200: {"description": "The same file was already stored: that document, unchanged."},
        400: _error("Not a Word document, too large, or a bad reference."),
    },
    status_code=201,
    summary="Store the booking's agreement (DOCX); the same file again returns the same document",
)
def store_booking_agreement(
    admin: CurrentAdmin,
    session: DbSession,
    storage: Storage,
    response: Response,
    booking_id: uuid.UUID,
    file: UploadFile,
    reference: Annotated[str | None, Form(max_length=40)] = None,
    generated_on: Annotated[date | None, Form()] = None,
    notes: Annotated[str | None, Form(max_length=2000)] = None,
) -> BookingDocumentRead:
    data = file.file.read(MAX_TEMPLATE_BYTES + 1)
    document, created = BookingAgreementService(session, admin, storage).store(
        booking_id,
        data=data,
        file_name=file.filename or "agreement.docx",
        reference=reference,
        generated_on=generated_on,
        notes=notes,
    )
    if not created:
        response.status_code = 200
    return document_read(document)


@router.get(
    "/bookings/{booking_id}/agreement/plan",
    response_model=BookingAgreementPlan,
    responses=NOT_FOUND,
    summary="Whether the booking's agreement can be generated, and what it needs",
)
def booking_agreement_plan(
    admin: CurrentAdmin, session: DbSession, booking_id: uuid.UUID
) -> BookingAgreementPlan:
    plan = BookingAgreementService(session, admin).plan(booking_id)
    return BookingAgreementPlan.model_validate({k: v for k, v in plan.items() if k != "booking"})


@router.post(
    "/bookings/{booking_id}/agreement/generate",
    response_model=BookingDocumentRead,
    status_code=201,
    responses={
        **NOT_FOUND,
        **CONFLICT,
        200: {"description": "The same file was already stored: that document, unchanged."},
        400: _error("A typed value is not valid, or a detail is missing from the records."),
    },
    summary="Generate the booking's agreement from the approved booking template",
)
def generate_booking_agreement(
    admin: CurrentAdmin,
    session: DbSession,
    storage: Storage,
    response: Response,
    booking_id: uuid.UUID,
    body: BookingAgreementGenerate,
) -> BookingDocumentRead:
    document, created = BookingAgreementService(session, admin, storage).generate(
        booking_id, **body.model_dump()
    )
    if not created:
        response.status_code = 200
    return document_read(document)


@router.get(
    "/booking-documents/{document_id}/download",
    response_model=LinkRead,
    responses=NOT_FOUND,
    summary="A short-lived link to the agreement (DOCX)",
)
def download_booking_document(
    admin: CurrentAdmin, session: DbSession, storage: Storage, document_id: uuid.UUID
) -> LinkRead:
    url = BookingAgreementService(session, admin, storage).link(document_id)
    return LinkRead(url=url, expires_in=300)


@router.get(
    "/booking-documents/{document_id}/signed-copy/download",
    response_model=LinkRead,
    responses=NOT_FOUND,
    summary="A short-lived link to the signed copy (PDF)",
)
def download_booking_signed_copy(
    admin: CurrentAdmin, session: DbSession, storage: Storage, document_id: uuid.UUID
) -> LinkRead:
    url = BookingAgreementService(session, admin, storage).link(document_id, signed=True)
    return LinkRead(url=url, expires_in=300)


@router.post(
    "/booking-documents/{document_id}/approve",
    response_model=BookingDocumentRead,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Approve a generated agreement; it is then ready for signing",
)
def approve_booking_document(
    admin: CurrentAdmin, session: DbSession, document_id: uuid.UUID
) -> BookingDocumentRead:
    return document_read(BookingAgreementService(session, admin).approve(document_id))


@router.post(
    "/booking-documents/{document_id}/signed-copy",
    response_model=BookingDocumentRead,
    status_code=201,
    responses={**NOT_FOUND, **CONFLICT, 400: _error("Not a PDF, too large, or a detail missing.")},
    summary="Upload the paper-signed agreement (PDF); the agreement becomes SIGNED",
)
def upload_booking_signed_copy(
    admin: CurrentAdmin,
    session: DbSession,
    storage: Storage,
    document_id: uuid.UUID,
    file: UploadFile,
    signed_on: Annotated[date, Form()],
    signers: Annotated[
        str, Form(description='JSON: [{"party": "TENANT", "name": "...", "designation": "..."}]')
    ],
) -> BookingDocumentRead:
    try:
        people = _SIGNERS.validate_json(signers or "[]")
    except ValidationError as exc:
        raise BadRequestError(
            "signers must be a JSON list of {party: TENANT or COMPANY, name, designation}."
        ) from exc
    data = file.file.read(MAX_SIGNED_COPY_BYTES + 1)
    return document_read(
        BookingAgreementService(session, admin, storage).upload_signed_copy(
            document_id,
            data=data,
            file_name=file.filename or "signed.pdf",
            signed_on=signed_on,
            signers=[p.model_dump() for p in people],
        )
    )
