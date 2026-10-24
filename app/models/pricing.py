"""Pricing rules: what a property, or one of its spaces, costs.

A rule prices one target: the property as a whole (a default that spaces inherit,
optionally only for one type of space) or a single space. What the amount is
multiplied by (``rate_basis``: per sq ft, per seat, per cabin...) is kept apart
from how often it is charged (``billing_period``: hourly, monthly...), so "₹70 per
sq ft per month" and "₹500 per seat per day" are both one rule each.

Only ACTIVE rules are used. Two ACTIVE rules for the same target, space type,
rate basis and billing period may not have overlapping effective dates; the
database refuses that, so a price is never counted twice.

The older ``spaces.price_amount`` / ``price_unit`` columns are a list price shown
on the space record. They are never read by pricing, estimates or contracts, and
these rules never write them.
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
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
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin, enum_check

# Stands in for "no space" in the overlap check: an exclusion constraint treats NULLs
# as never equal, which would let two property defaults overlap.
NO_SPACE = "00000000-0000-0000-0000-000000000000"


class PricingRule(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "pricing_rules"
    __table_args__ = (
        enum_check("target_type", PricingTarget),
        enum_check("rate_basis", RateBasis),
        enum_check("billing_period", BillingPeriod),
        enum_check("status", PricingStatus),
        enum_check("maintenance_basis", MaintenanceBasis),
        enum_check("tax_treatment", TaxTreatment),
        enum_check("deposit_rule", DepositRule),
        enum_check("payment_frequency", PaymentFrequency),
        enum_check("applies_to", SpaceType),
        # A space rule names its space; a property default names none and may be
        # limited to one type of space.
        enum_check("source", RateSource),
        # A space rule names its space; a building default its building; a property default
        # neither. Defaults may be limited to one type of space.
        CheckConstraint(
            "(target_type = 'SPACE' AND space_id IS NOT NULL AND building_id IS NULL"
            " AND applies_to IS NULL)"
            " OR (target_type = 'BUILDING' AND building_id IS NOT NULL AND space_id IS NULL)"
            " OR (target_type = 'PROPERTY' AND space_id IS NULL AND building_id IS NULL)",
            name="target",
        ),
        ForeignKeyConstraint(
            ["space_id", "property_id"], ["spaces.id", "spaces.property_id"], ondelete="RESTRICT"
        ),
        ForeignKeyConstraint(
            ["building_id", "property_id"],
            ["buildings.id", "buildings.property_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(source = 'DERIVED') = (derivation IS NOT NULL)", name="derived_has_derivation"
        ),
        CheckConstraint("amount >= 0", name="amount_not_negative"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from", name="dates_ordered"
        ),
        CheckConstraint(
            "(maintenance_amount IS NULL) = (maintenance_basis IS NULL)"
            " AND (maintenance_amount IS NULL OR maintenance_amount >= 0)",
            name="maintenance",
        ),
        CheckConstraint(
            "(tax_treatment = 'EXEMPT') = (tax_rate IS NULL)"
            " AND (tax_rate IS NULL OR tax_rate BETWEEN 0 AND 100)",
            name="tax",
        ),
        CheckConstraint(
            "(deposit_rule = 'NONE') = (deposit_value IS NULL)"
            " AND (deposit_value IS NULL OR deposit_value >= 0)"
            " AND (deposit_rule <> 'MONTHS_OF_RENT' OR billing_period = 'MONTHLY')",
            name="deposit",
        ),
        CheckConstraint(
            "(billing_period = 'CUSTOM') = (custom_period_label IS NOT NULL)",
            name="custom_period",
        ),
        CheckConstraint("jsonb_typeof(additional_charges) = 'array'", name="charges_array"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_code"),
        ExcludeConstraint(
            ("property_id", "="),
            (text(f"coalesce(space_id, '{NO_SPACE}'::uuid)"), "="),
            (text(f"coalesce(building_id, '{NO_SPACE}'::uuid)"), "="),
            (text("coalesce(applies_to, '')"), "="),
            ("rate_basis", "="),
            ("billing_period", "="),
            (text("daterange(effective_from, effective_to, '[]')"), "&&"),
            where=text("status = 'ACTIVE'"),
            using="gist",
            name="ex_pricing_rules_active_overlap",
        ),
        Index("ix_pricing_rules_property_status", "property_id", "status"),
    )

    property_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("properties.id", ondelete="RESTRICT"), nullable=False
    )
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)
    space_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    building_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    # For a property default: only spaces of this type inherit it. Null: any space it fits.
    applies_to: Mapped[str | None] = mapped_column(String(32))
    rate_basis: Mapped[str] = mapped_column(String(32), nullable=False)
    billing_period: Mapped[str] = mapped_column(String(16), nullable=False)
    custom_period_label: Mapped[str | None] = mapped_column(String(80))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="INR")
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=PricingStatus.DRAFT.value
    )
    maintenance_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    maintenance_basis: Mapped[str | None] = mapped_column(String(16))
    tax_treatment: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=TaxTreatment.EXEMPT.value
    )
    tax_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    # [{"label": "Parking", "amount": "1500.00"}], charged every billing period.
    additional_charges: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    deposit_rule: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=DepositRule.NONE.value
    )
    deposit_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    payment_frequency: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=PaymentFrequency.MONTHLY.value
    )
    notes: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=RateSource.CONFIGURED.value
    )
    # A DERIVED rule: the rule it was calculated from, the billing profile used, and the
    # figures and formula at the time it was approved.
    base_rule_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("pricing_rules.id", ondelete="SET NULL")
    )
    billing_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("billing_profiles.id", ondelete="SET NULL")
    )
    derivation: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )


class BillingProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """How one period converts into another for a property, or for one space in it.

    Nothing here has a default: an administrator enters every figure, so a derived price
    always rests on a stated policy. A space's own profile takes precedence over the
    property's."""

    __tablename__ = "billing_profiles"
    __table_args__ = (
        enum_check("rounding", RoundingPolicy),
        ForeignKeyConstraint(
            ["space_id", "property_id"], ["spaces.id", "spaces.property_id"], ondelete="CASCADE"
        ),
        CheckConstraint("hours_per_day > 0 AND hours_per_day <= 24", name="hours_per_day"),
        CheckConstraint("days_per_week > 0 AND days_per_week <= 7", name="days_per_week"),
        CheckConstraint("days_per_month > 0 AND days_per_month <= 31", name="days_per_month"),
        CheckConstraint(
            "weekly_discount_pct >= 0 AND weekly_discount_pct < 100"
            " AND monthly_discount_pct >= 0 AND monthly_discount_pct < 100",
            name="discounts",
        ),
        Index(
            "uq_billing_profiles_property",
            "property_id",
            unique=True,
            postgresql_where=text("space_id IS NULL"),
        ),
        Index(
            "uq_billing_profiles_space",
            "space_id",
            unique=True,
            postgresql_where=text("space_id IS NOT NULL"),
        ),
    )

    property_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("properties.id", ondelete="CASCADE"), nullable=False, index=True
    )
    space_id: Mapped[uuid.UUID | None] = mapped_column()
    hours_per_day: Mapped[Decimal] = mapped_column(Numeric(4, 2), nullable=False)
    days_per_week: Mapped[Decimal] = mapped_column(Numeric(3, 1), nullable=False)
    days_per_month: Mapped[Decimal] = mapped_column(Numeric(4, 1), nullable=False)
    weekly_discount_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    monthly_discount_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    rounding: Mapped[str] = mapped_column(String(16), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )
