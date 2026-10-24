"""One request that creates a property with everything in it.

Records refer to each other by ``ref``: a short name chosen by the client, unique in
the request ("bA", "floor-5", "5A"...). The response maps each ref to the id it was
saved under. ``"property"`` is reserved for the property itself.
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import Field

from app.models.enums import (
    AssetStatus,
    BillingPeriod,
    FileKind,
    LayoutStatus,
    RoundingPolicy,
    SpaceOperationalStatus,
    SpaceType,
)
from app.schemas.common import InputSchema, ReadSchema
from app.schemas.file import FileRead
from app.schemas.pricing import PricingTerms
from app.schemas.property import PropertyCreate, PropertyRead

PROPERTY_REF = "property"
_Ref = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._:-]+$")
_OptionalRef = Field(default=None, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._:-]+$")


class SetupBuilding(InputSchema):
    ref: str = _Ref
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None


class SetupFloor(InputSchema):
    ref: str = _Ref
    building_ref: str = _Ref
    name: str = Field(min_length=1, max_length=100)
    level_number: int = Field(ge=-20, le=300)


class SetupLayout(InputSchema):
    ref: str = _Ref
    floor_ref: str = _Ref
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    grid_columns: int | None = Field(default=None, ge=1, le=500)
    grid_rows: int | None = Field(default=None, ge=1, le=500)
    metadata: dict[str, Any] = Field(default_factory=dict)
    status: Literal[LayoutStatus.ACTIVE, LayoutStatus.ARCHIVED] = LayoutStatus.ACTIVE


class SetupSpace(InputSchema):
    ref: str = _Ref
    floor_ref: str = _Ref
    parent_ref: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9._:-]+$",
        description="The containing space. Omit to place it directly on the floor.",
    )
    layout_ref: str | None = _OptionalRef
    name: str = Field(min_length=1, max_length=200)
    code: str | None = Field(default=None, min_length=1, max_length=50)
    space_type: SpaceType
    capacity: int | None = Field(default=None, ge=1)
    area_sqft: float | None = Field(default=None, gt=0)
    is_rentable: bool | None = None
    operational_status: SpaceOperationalStatus = SpaceOperationalStatus.ACTIVE
    x: int | None = Field(default=None, ge=0)
    y: int | None = Field(default=None, ge=0)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    attributes: dict[str, Any] = Field(default_factory=dict)


class SetupAsset(InputSchema):
    ref: str = _Ref
    location_ref: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9._:-]+$",
        description="A building, floor or space ref. Omit for the property as a whole.",
    )
    name: str = Field(min_length=1, max_length=200)
    category: str = Field(min_length=1, max_length=80)
    asset_tag: str | None = Field(default=None, min_length=1, max_length=80)
    location: str | None = Field(default=None, max_length=200)
    status: AssetStatus = AssetStatus.OPERATIONAL
    purchase_date: date | None = None
    notes: str | None = None
    maintenance_notes: str | None = None


class SetupPricing(PricingTerms):
    ref: str | None = _OptionalRef
    target_ref: str = Field(
        default=PROPERTY_REF,
        min_length=1,
        max_length=100,
        description='"property", a building ref (a building default), a floor ref (the '
        "whole floor) or a space ref.",
    )
    applies_to: SpaceType | None = None
    derive: list[BillingPeriod] = Field(
        default_factory=list,
        max_length=6,
        description="Periods to price from this rate with the billing profile, approved by the "
        "administrator in the wizard. Recalculated here; never applied to a period that has "
        "its own rate in this request.",
    )
    derive_status: Literal["ACTIVE", "DRAFT"] = "ACTIVE"


class SetupBillingProfile(InputSchema):
    """The property's billing profile. Every figure is entered by the administrator."""

    hours_per_day: Decimal = Field(gt=0, le=24, max_digits=4, decimal_places=2)
    days_per_week: Decimal = Field(gt=0, le=7, max_digits=3, decimal_places=1)
    days_per_month: Decimal = Field(gt=0, le=31, max_digits=4, decimal_places=1)
    weekly_discount_pct: Decimal = Field(ge=0, lt=100, max_digits=5, decimal_places=2)
    monthly_discount_pct: Decimal = Field(ge=0, lt=100, max_digits=5, decimal_places=2)
    rounding: RoundingPolicy


class SetupImage(InputSchema):
    file_index: int = Field(ge=0, description="Position of the file in the `files` parts.")
    owner_ref: str = Field(
        default=PROPERTY_REF,
        min_length=1,
        max_length=100,
        description='"property", or a building, layout, space or asset ref.',
    )
    kind: FileKind
    caption: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=2000)
    category: str | None = Field(default=None, max_length=80)
    sort_order: int | None = Field(default=None, ge=0, le=100_000)
    is_primary: bool | None = None


class PropertySetup(InputSchema):
    property: PropertyCreate
    amenities: list[str] = Field(default_factory=list, max_length=100)
    buildings: list[SetupBuilding] = Field(default_factory=list, max_length=50)
    floors: list[SetupFloor] = Field(default_factory=list, max_length=500)
    layouts: list[SetupLayout] = Field(default_factory=list, max_length=500)
    spaces: list[SetupSpace] = Field(default_factory=list, max_length=5000)
    assets: list[SetupAsset] = Field(default_factory=list, max_length=2000)
    billing_profile: SetupBillingProfile | None = None
    pricing: list[SetupPricing] = Field(default_factory=list, max_length=5000)
    images: list[SetupImage] = Field(default_factory=list, max_length=200)


class SetupCounts(ReadSchema):
    buildings: int
    floors: int
    layouts: int
    spaces: int
    assets: int
    amenities: int
    pricing_rules: int
    derived_rules: int = 0
    files: int


class PropertySetupResult(ReadSchema):
    property: PropertyRead
    ids: dict[str, uuid.UUID] = Field(description="Each ref, mapped to the id it was saved as.")
    created: SetupCounts
    added: list[str] = Field(
        description="Records the backend required that the request did not contain, such as "
        "a floor's whole-floor space."
    )
    files: list[FileRead]
