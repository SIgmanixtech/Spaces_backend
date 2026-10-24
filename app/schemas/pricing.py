import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, ClassVar, Literal

from pydantic import Field

from app.models.enums import (
    BillingPeriod,
    DepositRule,
    MaintenanceBasis,
    PaymentFrequency,
    PricingStatus,
    PricingTarget,
    RateBasis,
    RateSource,
    RoundingPolicy,
    SpaceType,
    TaxTreatment,
)
from app.schemas.common import InputSchema, ReadSchema, UpdateSchema

_Money = Field(ge=0, max_digits=12, decimal_places=2)
_OptionalMoney = Field(default=None, ge=0, max_digits=12, decimal_places=2)
_Rate = Field(default=None, ge=0, le=100, max_digits=5, decimal_places=2)
_Currency = Field(default="INR", min_length=3, max_length=3, pattern="^[A-Z]{3}$")


class AdditionalCharge(InputSchema):
    label: str = Field(min_length=1, max_length=80)
    amount: Decimal = _Money


class PricingTerms(InputSchema):
    """The commercial terms of one rule. Cross-field rules are checked by the service."""

    rate_basis: RateBasis = Field(
        description="What the amount is multiplied by: PER_SQFT (area), PER_PERSON "
        "(capacity), or a fixed price for the whole floor, portion, cabin, office, "
        "workstation, seat, room or hall."
    )
    billing_period: BillingPeriod = Field(description="How long one charge covers.")
    custom_period_label: str | None = Field(
        default=None, min_length=1, max_length=80, description="Required for CUSTOM."
    )
    amount: Decimal = _Money
    currency: str = _Currency
    effective_from: date
    effective_to: date | None = Field(default=None, description="Inclusive. Null: open-ended.")
    status: PricingStatus = Field(
        default=PricingStatus.DRAFT, description="Only ACTIVE rules are used for prices."
    )
    maintenance_amount: Decimal | None = _OptionalMoney
    maintenance_basis: MaintenanceBasis | None = None
    tax_treatment: TaxTreatment = TaxTreatment.EXEMPT
    tax_rate: Decimal | None = _Rate
    additional_charges: list[AdditionalCharge] = Field(default_factory=list, max_length=20)
    deposit_rule: DepositRule = DepositRule.NONE
    deposit_value: Decimal | None = _OptionalMoney
    payment_frequency: PaymentFrequency = PaymentFrequency.MONTHLY
    notes: str | None = Field(default=None, max_length=2000)


class PricingRuleCreate(PricingTerms):
    property_id: uuid.UUID
    space_id: uuid.UUID | None = Field(
        default=None, description="The space priced. Omit for a building or property default."
    )
    building_id: uuid.UUID | None = Field(
        default=None, description="A default for the spaces of one building (no space_id)."
    )
    applies_to: SpaceType | None = Field(
        default=None,
        description="Building and property defaults only: limit the default to one type.",
    )
    source: Literal["CONFIGURED", "OVERRIDE"] = Field(
        default="CONFIGURED",
        description="OVERRIDE replaces an active DERIVED rate for the same target, basis and "
        "period (that derived rule is retired). DERIVED rules come only from approving an "
        "estimate.",
    )


class PricingRuleUpdate(UpdateSchema):
    """Target and property cannot change; retire the rule and create another instead."""

    required_fields: ClassVar[frozenset[str]] = frozenset(
        {
            "rate_basis",
            "billing_period",
            "amount",
            "currency",
            "effective_from",
            "status",
            "tax_treatment",
            "additional_charges",
            "deposit_rule",
            "payment_frequency",
        }
    )

    rate_basis: RateBasis | None = None
    billing_period: BillingPeriod | None = None
    custom_period_label: str | None = Field(default=None, min_length=1, max_length=80)
    amount: Decimal | None = _OptionalMoney
    currency: str | None = Field(default=None, min_length=3, max_length=3, pattern="^[A-Z]{3}$")
    effective_from: date | None = None
    effective_to: date | None = None
    status: PricingStatus | None = None
    maintenance_amount: Decimal | None = _OptionalMoney
    maintenance_basis: MaintenanceBasis | None = None
    tax_treatment: TaxTreatment | None = None
    tax_rate: Decimal | None = _Rate
    additional_charges: list[AdditionalCharge] | None = Field(default=None, max_length=20)
    deposit_rule: DepositRule | None = None
    deposit_value: Decimal | None = _OptionalMoney
    payment_frequency: PaymentFrequency | None = None
    notes: str | None = Field(default=None, max_length=2000)


class AdditionalChargeRead(ReadSchema):
    label: str
    amount: float


class PricingRuleRead(ReadSchema):
    id: uuid.UUID
    property_id: uuid.UUID
    target_type: PricingTarget
    space_id: uuid.UUID | None
    space_name: str | None
    space_type: SpaceType | None
    building_id: uuid.UUID | None
    building_name: str | None
    source: RateSource
    base_rule_id: uuid.UUID | None
    billing_profile_id: uuid.UUID | None
    derivation: dict[str, Any] | None
    applies_to: SpaceType | None
    rate_basis: RateBasis
    billing_period: BillingPeriod
    custom_period_label: str | None
    amount: float
    currency: str
    effective_from: date
    effective_to: date | None
    status: PricingStatus
    maintenance_amount: float | None
    maintenance_basis: MaintenanceBasis | None
    tax_treatment: TaxTreatment
    tax_rate: float | None
    additional_charges: list[AdditionalChargeRead]
    deposit_rule: DepositRule
    deposit_value: float | None
    payment_frequency: PaymentFrequency
    notes: str | None
    created_by: uuid.UUID | None
    updated_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class EstimateRead(ReadSchema):
    """One billing period's charges (times ``quantity``), each rounded to the paisa/cent.

    ``recurring_total`` = rent + maintenance + additional charges, plus tax when tax is
    ADDITIONAL. INCLUDED tax is shown but already inside the other lines. The deposit is
    a one-time amount and is never part of the recurring total."""

    currency: str
    billing_period: BillingPeriod
    rate_basis: RateBasis
    quantity: float = Field(description="Billing periods charged, e.g. 3 for three hours.")
    measure: float | None = Field(description="Sq ft or persons the rate was multiplied by.")
    rent: float
    maintenance: float
    additional_charges: float
    tax: float
    tax_included: bool
    recurring_total: float
    deposit: float | None


PriceSource = Literal["OWN", "INHERITED", "BUILDING_DEFAULT", "PROPERTY_DEFAULT"]


class LegacyPrice(ReadSchema):
    amount: float
    unit: str
    currency: str


class EffectivePrice(ReadSchema):
    space_id: uuid.UUID
    space_name: str
    space_type: SpaceType
    area_sqft: float | None
    capacity: int | None
    billing_period: BillingPeriod
    source: PriceSource = Field(
        description="OWN: the space's own rule. INHERITED: a per-sq-ft or per-person rate "
        "of a space containing it. PROPERTY_DEFAULT: the property's default."
    )
    source_space_id: uuid.UUID | None
    source_space_name: str | None
    rule: PricingRuleRead
    estimate: EstimateRead | None = Field(
        description="Null when the space lacks the area or capacity the rate needs."
    )


class SpacePricing(ReadSchema):
    space_id: uuid.UUID
    space_name: str
    space_type: SpaceType
    prices: list[EffectivePrice]
    legacy_price: LegacyPrice | None = Field(
        description="The space record's own list price (price_amount/price_unit). Shown for "
        "reference only; it is not a pricing rule and is never added to these prices."
    )


class PropertyPricing(ReadSchema):
    property_id: uuid.UUID
    on: date
    spaces: list[SpacePricing]
    unpriced: list[uuid.UUID] = Field(description="Rentable spaces no active rule prices.")


class EstimateRequest(InputSchema):
    space_id: uuid.UUID
    billing_period: BillingPeriod
    on: date | None = Field(default=None, description="Defaults to today (business time zone).")
    quantity: Decimal = Field(default=Decimal(1), gt=0, max_digits=8, decimal_places=2)
    persons: int | None = Field(
        default=None, ge=1, description="For PER_PERSON rates; defaults to the capacity."
    )


class EstimateResult(ReadSchema):
    price: EffectivePrice
    estimate: EstimateRead


# --- billing profiles and derived rates ---------------------------------------------------

_Hours = Field(gt=0, le=24, max_digits=4, decimal_places=2)
_Week = Field(gt=0, le=7, max_digits=3, decimal_places=1)
_Month = Field(gt=0, le=31, max_digits=4, decimal_places=1)
_Discount = Field(ge=0, lt=100, max_digits=5, decimal_places=2)


class BillingProfileCreate(InputSchema):
    """How periods convert for a property, or one space in it. Every figure is required:
    there are no assumed working hours or discounts."""

    property_id: uuid.UUID
    space_id: uuid.UUID | None = Field(
        default=None, description="Omit for the property's profile; set for a space override."
    )
    hours_per_day: Decimal = _Hours
    days_per_week: Decimal = _Week
    days_per_month: Decimal = _Month
    weekly_discount_pct: Decimal = _Discount
    monthly_discount_pct: Decimal = _Discount
    rounding: RoundingPolicy
    notes: str | None = Field(default=None, max_length=2000)


class BillingProfileUpdate(UpdateSchema):
    required_fields: ClassVar[frozenset[str]] = frozenset(
        {
            "hours_per_day",
            "days_per_week",
            "days_per_month",
            "weekly_discount_pct",
            "monthly_discount_pct",
            "rounding",
        }
    )

    hours_per_day: Decimal | None = Field(default=None, gt=0, le=24, max_digits=4, decimal_places=2)
    days_per_week: Decimal | None = Field(default=None, gt=0, le=7, max_digits=3, decimal_places=1)
    days_per_month: Decimal | None = Field(
        default=None, gt=0, le=31, max_digits=4, decimal_places=1
    )
    weekly_discount_pct: Decimal | None = Field(
        default=None, ge=0, lt=100, max_digits=5, decimal_places=2
    )
    monthly_discount_pct: Decimal | None = Field(
        default=None, ge=0, lt=100, max_digits=5, decimal_places=2
    )
    rounding: RoundingPolicy | None = None
    notes: str | None = Field(default=None, max_length=2000)


class BillingProfileRead(ReadSchema):
    id: uuid.UUID
    property_id: uuid.UUID
    space_id: uuid.UUID | None
    space_name: str | None
    hours_per_day: float
    days_per_week: float
    days_per_month: float
    weekly_discount_pct: float
    monthly_discount_pct: float
    rounding: RoundingPolicy
    notes: str | None
    created_at: datetime
    updated_at: datetime


class ExistingRate(ReadSchema):
    id: uuid.UUID
    source: RateSource
    status: PricingStatus
    amount: float


class DerivedEstimate(ReadSchema):
    billing_period: BillingPeriod
    amount: float
    maintenance_amount: float | None
    additional_charges: list[AdditionalChargeRead]
    deposit_rule: DepositRule
    deposit_value: float | None
    formula: str
    existing: ExistingRate | None = Field(
        description="An active or draft rate already set for this period, if any."
    )
    on_approval: Literal["CREATE", "REPLACE_DERIVED", "KEEP_CONFIGURED"] = Field(
        description="What approving this period would do. A configured or override rate is "
        "never replaced by an estimate."
    )


class DerivationPreview(ReadSchema):
    """Estimates only. Nothing is priced until an administrator approves periods."""

    base: PricingRuleRead
    profile: BillingProfileRead
    estimates: list[DerivedEstimate]


class DerivationApprove(InputSchema):
    periods: list[BillingPeriod] = Field(min_length=1, max_length=6)
    status: Literal["ACTIVE", "DRAFT"] = "ACTIVE"


class DerivationSkip(ReadSchema):
    billing_period: BillingPeriod
    reason: str


class DerivationResult(ReadSchema):
    created: list[PricingRuleRead]
    skipped: list[DerivationSkip]
