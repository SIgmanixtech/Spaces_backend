"""Adding a tenant with the space they take (owner request of 14 Nov 2026)."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Self

from pydantic import Field, model_validator

from app.models.enums import (
    ChargeCategory,
    ContractType,
    MaintenanceBasis,
    RateBasis,
    SpaceType,
    TaxTreatment,
)
from app.schemas.common import InputSchema, ReadSchema
from app.schemas.contract import ContractRead
from app.schemas.crm import DuplicateMatch
from app.schemas.invoice import InvoiceRead
from app.schemas.tenant import TenantCreate, TenantRead


class AssignmentInput(InputSchema):
    property_id: uuid.UUID
    space_id: uuid.UUID | None = Field(default=None, description="One space (older clients).")
    space_ids: list[uuid.UUID] = Field(
        default_factory=list,
        max_length=50,
        description="The workstations or spaces taken: their number is the membership quantity.",
    )
    start_date: date
    end_date: date | None = Field(
        default=None, description="Last day, inclusive. Null means ongoing."
    )
    # Commercial terms entered by the administrator (17 Nov 2026); none is assumed.
    contract_type: ContractType | None = Field(
        default=None, description="COWORKING_LICENCE or SPACE_LEASE: which agreement applies."
    )
    lock_in_period: str | None = Field(default=None, max_length=100)
    notice_period: str | None = Field(default=None, max_length=100)
    payment_due_day: int | None = Field(default=None, ge=1, le=28)
    tds_percent: Decimal | None = Field(default=None, ge=0, le=100, max_digits=5, decimal_places=2)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("The end date cannot be before the start date.")
        spaces = list(dict.fromkeys([*self.space_ids, *([self.space_id] if self.space_id else [])]))
        if not spaces:
            raise ValueError("Choose at least one space.")
        self.space_ids = spaces
        self.space_id = spaces[0]
        return self


class PricingBreakdown(ReadSchema):
    """One month for every chosen space under its pricing rule, as the pricing engine works
    it out (amounts summed over the spaces; rate and basis of the first)."""

    quantity: int = 1
    source: str = Field(description="Where the rule comes from: OWN, INHERITED, *_DEFAULT.")
    rule_id: uuid.UUID
    currency: str
    rate_basis: RateBasis
    rate: Decimal = Field(description="The rule's amount per unit of the basis.")
    area_sqft: Decimal | None
    maintenance_rate: Decimal | None
    maintenance_basis: MaintenanceBasis | None
    rent: Decimal
    maintenance: Decimal
    additional_charges: Decimal
    tax_treatment: TaxTreatment
    tax_rate: Decimal | None
    tax: Decimal
    recurring_total: Decimal
    deposit: Decimal | None
    tds_percent: Decimal | None = None
    tds_amount: Decimal = Decimal(0)
    net_payable: Decimal | None = None


class InvoicePreviewLine(ReadSchema):
    category: ChargeCategory
    description: str
    amount: Decimal


class InvoicePreview(ReadSchema):
    period_start: date
    period_end: date
    currency: str
    lines: list[InvoicePreviewLine]
    total: Decimal
    note: str


class QuoteSpace(ReadSchema):
    space_id: uuid.UUID
    space_name: str
    space_type: SpaceType
    area_sqft: Decimal | None
    available: bool
    conflicts: list[str]


class AssignmentQuote(ReadSchema):
    property_id: uuid.UUID
    property_name: str
    building_name: str | None
    floor_name: str | None
    space_id: uuid.UUID
    space_name: str
    space_type: SpaceType
    area_sqft: Decimal | None
    capacity: int | None
    available: bool
    conflicts: list[str]
    pricing: PricingBreakdown | None
    pricing_missing: str | None = Field(
        description="Why there is no monthly price: no invoice can be made without one."
    )
    invoice: InvoicePreview | None
    quantity: int = 1
    spaces: list[QuoteSpace] = Field(default_factory=list)


class TenantOnboard(InputSchema):
    """Either a new tenant's details or an existing tenant to link, and the space."""

    tenant: TenantCreate | None = None
    existing_tenant_id: uuid.UUID | None = Field(
        default=None, description="Link this existing tenant instead of creating one."
    )
    confirm_not_duplicate: bool = Field(
        default=False,
        description="Create the new tenant although tenants with the same email or phone "
        "exist (the admin checked they are different).",
    )
    assignment: AssignmentInput

    @model_validator(mode="after")
    def _one_tenant(self) -> Self:
        if (self.tenant is None) == (self.existing_tenant_id is None):
            raise ValueError("Give either new tenant details or an existing tenant, not both.")
        return self


class OnboardResult(ReadSchema):
    tenant: TenantRead
    created_tenant: bool
    contract: ContractRead
    invoice: InvoiceRead | None
    invoice_skipped: str | None = Field(description="Why no invoice was made, if none was.")
    signature_saved: bool


class DuplicateTenants(ReadSchema):
    detail: str
    matches: list[DuplicateMatch]


class SignatureLink(ReadSchema):
    url: str
    expires_in: int
    content_type: str
    size_bytes: int
