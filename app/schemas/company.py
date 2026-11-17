"""Company profile request and response bodies (Phase 4B). Admin only."""

import uuid
from datetime import datetime
from typing import ClassVar

from pydantic import Field, field_validator

from app.schemas.common import InputSchema, ReadSchema, UpdateSchema
from app.schemas.tenant import CIN_PATTERN, GSTIN_PATTERN, PAN_PATTERN

PROFILE_TEXT = (
    "legal_name",
    "registered_address",
    "signatory_name",
    "signatory_designation",
    "signatory_address",
    "gstin",
    "pan",
    "cin",
    "notes",
    "phone",
    "email",
    "bank_name",
    "bank_account_name",
    "bank_account_number",
    "bank_ifsc",
    "bank_branch",
    "upi_id",
)


class _Tidy(InputSchema):
    @field_validator(*PROFILE_TEXT, mode="before", check_fields=False)
    @classmethod
    def _tidy(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("gstin", "pan", "cin", "bank_ifsc", mode="before", check_fields=False)
    @classmethod
    def _upper(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value


class CompanyProfileCreate(_Tidy):
    legal_name: str = Field(min_length=1, max_length=200)
    registered_address: str | None = Field(default=None, max_length=500)
    signatory_name: str | None = Field(default=None, max_length=200)
    signatory_designation: str | None = Field(default=None, max_length=120)
    signatory_address: str | None = Field(default=None, max_length=500)
    gstin: str | None = Field(default=None, pattern=GSTIN_PATTERN)
    pan: str | None = Field(default=None, pattern=PAN_PATTERN)
    cin: str | None = Field(default=None, pattern=CIN_PATTERN)
    notes: str | None = Field(default=None, max_length=2000)
    phone: str | None = Field(default=None, pattern=r"^\+?[0-9 ()-]{7,20}$")
    email: str | None = Field(default=None, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    bank_name: str | None = Field(default=None, max_length=120)
    bank_account_name: str | None = Field(default=None, max_length=200)
    bank_account_number: str | None = Field(default=None, pattern=r"^[0-9]{6,34}$")
    bank_ifsc: str | None = Field(default=None, pattern=r"^[A-Z]{4}0[A-Z0-9]{6}$")
    bank_branch: str | None = Field(default=None, max_length=120)
    upi_id: str | None = Field(
        default=None, max_length=100, pattern=r"^[A-Za-z0-9._-]{2,}@[A-Za-z0-9.-]{2,}$"
    )


class CompanyProfileUpdate(_Tidy, UpdateSchema):
    required_fields: ClassVar[frozenset[str]] = frozenset({"legal_name"})

    legal_name: str | None = Field(default=None, min_length=1, max_length=200)
    registered_address: str | None = Field(default=None, max_length=500)
    signatory_name: str | None = Field(default=None, max_length=200)
    signatory_designation: str | None = Field(default=None, max_length=120)
    signatory_address: str | None = Field(default=None, max_length=500)
    gstin: str | None = Field(default=None, pattern=GSTIN_PATTERN)
    pan: str | None = Field(default=None, pattern=PAN_PATTERN)
    cin: str | None = Field(default=None, pattern=CIN_PATTERN)
    notes: str | None = Field(default=None, max_length=2000)
    phone: str | None = Field(default=None, pattern=r"^\+?[0-9 ()-]{7,20}$")
    email: str | None = Field(default=None, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    bank_name: str | None = Field(default=None, max_length=120)
    bank_account_name: str | None = Field(default=None, max_length=200)
    bank_account_number: str | None = Field(default=None, pattern=r"^[0-9]{6,34}$")
    bank_ifsc: str | None = Field(default=None, pattern=r"^[A-Z]{4}0[A-Z0-9]{6}$")
    bank_branch: str | None = Field(default=None, max_length=120)
    upi_id: str | None = Field(
        default=None, max_length=100, pattern=r"^[A-Za-z0-9._-]{2,}@[A-Za-z0-9.-]{2,}$"
    )


class ProfileProperty(ReadSchema):
    id: uuid.UUID
    name: str
    city: str | None


class CompanyProfileRead(ReadSchema):
    id: uuid.UUID
    legal_name: str
    registered_address: str | None
    signatory_name: str | None
    signatory_designation: str | None
    signatory_address: str | None
    gstin: str | None
    pan: str | None
    cin: str | None
    notes: str | None
    phone: str | None = None
    email: str | None = None
    bank_name: str | None = None
    bank_account_name: str | None = None
    bank_account_number: str | None = None
    bank_ifsc: str | None = None
    bank_branch: str | None = None
    upi_id: str | None = None
    properties: list[ProfileProperty]
    created_at: datetime
    updated_at: datetime


class ProfileProperties(InputSchema):
    property_ids: list[uuid.UUID] = Field(
        max_length=500,
        description="Every property this profile owns. A property listed here moves from "
        "any other profile; properties not listed are unlinked from this one.",
    )
