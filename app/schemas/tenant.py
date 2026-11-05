import uuid
from datetime import date, datetime
from typing import ClassVar, Literal, Self

from pydantic import EmailStr, Field, field_validator, model_validator

from app.models.enums import OccupancyStatus, TenantStatus
from app.schemas.common import InputSchema, ReadSchema, UpdateSchema

# Formats only (Phase 4A); no other legal rule is assumed. The GSTIN and PAN patterns are the
# ones the agreement form already uses (app/services/agreements.py); CIN is the standard
# 21-character company identification number.
GSTIN_PATTERN = r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$"
PAN_PATTERN = r"^[A-Z]{5}[0-9]{4}[A-Z]$"
CIN_PATTERN = r"^[LU][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6}$"
LEGAL_FIELDS = (
    "registered_address",
    "signatory_name",
    "signatory_designation",
    "signatory_address",
    "gstin",
    "pan",
    "cin",
)


class TenantLegal(InputSchema):
    """Legal and signatory details, all optional. Blank text is saved as empty (null);
    GSTIN, PAN and CIN are trimmed and uppercased before their format is checked."""

    registered_address: str | None = Field(default=None, max_length=500)
    signatory_name: str | None = Field(default=None, max_length=200)
    signatory_designation: str | None = Field(default=None, max_length=120)
    signatory_address: str | None = Field(
        default=None, max_length=500, description="Only if different from the registered one."
    )
    gstin: str | None = Field(
        default=None, pattern=GSTIN_PATTERN, description="e.g. 36ABCDE1234F1Z5"
    )
    pan: str | None = Field(default=None, pattern=PAN_PATTERN, description="e.g. ABCDE1234F")
    cin: str | None = Field(
        default=None, pattern=CIN_PATTERN, description="21 characters, e.g. U72200TG2015PTC123456"
    )

    @field_validator(*LEGAL_FIELDS, mode="before")
    @classmethod
    def _tidy(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("gstin", "pan", "cin", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value


class TenantCreate(TenantLegal):
    name: str = Field(min_length=1, max_length=200)
    company_name: str | None = Field(default=None, max_length=200)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, min_length=5, max_length=32)
    status: TenantStatus = TenantStatus.ACTIVE
    notes: str | None = None


class TenantUpdate(TenantLegal, UpdateSchema):
    required_fields: ClassVar[frozenset[str]] = frozenset({"name", "status"})

    name: str | None = Field(default=None, min_length=1, max_length=200)
    company_name: str | None = Field(default=None, max_length=200)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, min_length=5, max_length=32)
    status: TenantStatus | None = None
    notes: str | None = None


class TenantRead(ReadSchema):
    id: uuid.UUID
    name: str
    company_name: str | None
    email: str | None
    phone: str | None
    status: TenantStatus
    notes: str | None
    property_id: uuid.UUID | None = Field(description="Property of the current occupancy.")
    property_name: str | None
    space_id: uuid.UUID | None = Field(description="Space of the current occupancy, if any.")
    space_name: str | None
    occupancy_status: OccupancyStatus = Field(
        description="OCCUPIED with a current occupancy; RESERVED with an upcoming occupancy "
        "or open booking; otherwise VACANT. Read-only."
    )
    booking_refs: list[str] = Field(description="References of the tenant's bookings.")
    client_access: Literal["NOT_INVITED", "INVITED", "ACTIVE"] = Field(
        default="NOT_INVITED",
        description="Client app access: invited by the property team, active once the "
        "tenant has set a password.",
    )
    client_invited_at: datetime | None = None
    client_activated_at: datetime | None = None
    client_access_blocker: str | None = Field(
        default=None,
        description="Why the tenant cannot be invited yet (no email, no completed contract).",
    )
    registered_address: str | None = None
    signatory_name: str | None = None
    signatory_designation: str | None = None
    signatory_address: str | None = None
    gstin: str | None = None
    pan: str | None = None
    cin: str | None = None
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


class OccupancyCreate(InputSchema):
    property_id: uuid.UUID
    space_id: uuid.UUID | None = Field(
        default=None, description="Omit when the tenant occupies the whole property."
    )
    start_date: date
    end_date: date | None = Field(
        default=None, description="Last day of the occupancy, inclusive. Null means ongoing."
    )
    notes: str | None = None

    @model_validator(mode="after")
    def _dates_ordered(self) -> Self:
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("end_date cannot be before start_date")
        return self


class OccupancyUpdate(UpdateSchema):
    required_fields: ClassVar[frozenset[str]] = frozenset({"start_date"})

    start_date: date | None = None
    end_date: date | None = None
    notes: str | None = None


class OccupancyRead(ReadSchema):
    id: uuid.UUID
    reference: str | None = Field(default=None, description="The contract reference.")
    status: str | None = Field(
        default=None, description="Contract status; see GET /contracts/{id} for the full contract."
    )
    tenant_id: uuid.UUID
    property_id: uuid.UUID
    property_name: str
    space_id: uuid.UUID | None
    space_name: str | None
    start_date: date
    end_date: date | None
    notes: str | None
    created_at: datetime
    updated_at: datetime


class ClientAccessRequest(InputSchema):
    send_email: bool = Field(
        default=True,
        description="True: ask Supabase to email the invitation. False: only make a link to "
        "share (nothing is emailed).",
    )


class ClientAccessRead(ReadSchema):
    tenant_id: uuid.UUID
    status: Literal["NOT_INVITED", "INVITED", "ACTIVE"]
    email: str
    invited_at: datetime | None
    activated_at: datetime | None
    email_requested: bool = Field(
        description="Supabase accepted a request to email the invitation. Delivery is not "
        "confirmed."
    )
    invitation_link: str | None = Field(
        description="Only when no email was requested: the link to share with the tenant."
    )
    message: str


class TenantDuplicateCheck(InputSchema):
    """Details typed into the tenant form. Warning only: nothing is merged or refused."""

    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=32)
    company_name: str | None = Field(default=None, max_length=200)
    exclude_tenant_id: uuid.UUID | None = Field(
        default=None, description="The tenant being edited, so it does not match itself."
    )
