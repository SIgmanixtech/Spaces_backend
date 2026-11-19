import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field

from app.schemas.agreement import Signer
from app.schemas.common import InputSchema, ReadSchema


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
    template_id: uuid.UUID | None = None
    template_version: int | None = None
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


class BookingTemplateSummary(ReadSchema):
    id: uuid.UUID
    name: str
    version: int


class BookingCustomField(ReadSchema):
    key: str
    label: str
    type: str
    required: bool = False


class BookingAgreementPlan(ReadSchema):
    """Whether the app can generate this booking's agreement, and what it needs."""

    template: BookingTemplateSummary | None
    problem: str | None = Field(description="What stops generation, in words; null when none.")
    missing: list[str] = Field(description="Details missing from the records (tenant, company).")
    custom_fields: list[BookingCustomField]
    suggested_amount: Decimal | None = Field(description="The booking's quoted total, if any.")
    suggested_gst_treatment: str | None
    suggested_gst_rate: Decimal | None


class BookingAgreementGenerate(InputSchema):
    """What the administrator types; everything else comes from the records."""

    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2, description="Agreed fee (INR).")
    gst_treatment: Literal["INCLUDED", "ADDITIONAL", "EXEMPT"] = Field(
        description="INCLUDED: the fee includes GST. ADDITIONAL: GST on top. EXEMPT: none."
    )
    gst_rate: Decimal | None = Field(default=None, ge=0, le=28, max_digits=5, decimal_places=2)
    tds_percent: Decimal | None = Field(default=None, ge=0, le=100, max_digits=5, decimal_places=2)
    aadhaar_last4: str | None = Field(default=None, max_length=4)
    residential_address: str | None = Field(default=None, max_length=500)
    fields: dict[str, str | None] = Field(default_factory=dict)
    notes: str | None = Field(default=None, max_length=2000)
