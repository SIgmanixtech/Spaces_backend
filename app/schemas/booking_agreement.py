import uuid
from datetime import date, datetime

from pydantic import Field

from app.schemas.agreement import Signer
from app.schemas.common import ReadSchema


class BookingDocumentRead(ReadSchema):
    id: uuid.UUID
    reference: str
    booking_id: uuid.UUID
    agreement_type: str
    status: str = Field(description="GENERATED, APPROVED, SIGNED or SUPERSEDED.")
    lifecycle: str = ""
    file_name: str
    size_bytes: int
    sha256: str
    generated_on: date
    notes: str | None
    uploaded_by: uuid.UUID | None
    approved_at: datetime | None
    signed_at: datetime | None
    superseded_at: datetime | None
    signed_file_name: str | None
    signed_on: date | None
    signers: list[Signer]
    created_at: datetime


class BookingAgreementRead(ReadSchema):
    booking_id: uuid.UUID
    booking_reference: str
    booking_status: str
    agreement_label: str
    current: BookingDocumentRead | None = Field(
        description="The agreement in force: the newest one not superseded."
    )
    history: list[BookingDocumentRead] = Field(description="Every version, newest first.")
