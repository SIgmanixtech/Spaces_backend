"""Billing profiles and derived rates.

A billing profile states how a property (or one space) converts one billing period into
another. With it, any configured rate can be *previewed* in other periods; nothing is
priced until an administrator approves chosen periods, which creates DERIVED pricing
rules. Approval never replaces a CONFIGURED or OVERRIDE rate; it does refresh an earlier
DERIVED rate for the same period (that one is retired).

See app/services/pricing_conversion.py for the conversion policy.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import or_, select

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models.enums import BillingPeriod, PricingStatus, RateSource
from app.models.pricing import BillingProfile, PricingRule
from app.models.property import Property
from app.models.space import Space
from app.schemas.pricing import (
    AdditionalChargeRead,
    BillingProfileRead,
    DerivationPreview,
    DerivationResult,
    DerivationSkip,
    DerivedEstimate,
    ExistingRate,
)
from app.services.base import CrudService, commit, flush, require
from app.services.pricing import PricingService
from app.services.pricing_conversion import CONVERTIBLE, Profile, derive


class BillingProfileService(CrudService[BillingProfile]):
    model = BillingProfile
    label = "Billing profile"
    sort_fields = ("created_at",)
    default_sort = "created_at"

    def describe(self, record: BillingProfile) -> str:
        return "space override" if record.space_id else "property profile"

    def validate(self, values: dict[str, Any], existing: BillingProfile | None) -> None:
        if existing is None:
            require(self.session, Property, values["property_id"], "Property")
            if values.get("space_id") is not None:
                space = require(self.session, Space, values["space_id"], "Space")
                if space.property_id != values["property_id"]:
                    raise BadRequestError("The space does not belong to this property.")
            taken = self.session.scalar(
                select(BillingProfile.id).where(
                    BillingProfile.property_id == values["property_id"],
                    BillingProfile.space_id.is_(None)
                    if values.get("space_id") is None
                    else BillingProfile.space_id == values["space_id"],
                )
            )
            if taken:
                raise ConflictError(
                    "A billing profile already exists for this "
                    + ("space" if values.get("space_id") else "property")
                    + ". Edit it instead."
                )
        Profile(
            hours_per_day=values["hours_per_day"],
            days_per_week=values["days_per_week"],
            days_per_month=values["days_per_month"],
            weekly_discount_pct=values["weekly_discount_pct"],
            monthly_discount_pct=values["monthly_discount_pct"],
            rounding=values["rounding"],
        ).check()

    def create(self, values: dict[str, Any]) -> BillingProfile:
        if self.actor is not None:
            values = {**values, "updated_by": self.actor.id}
        return super().create(values)

    def update(self, record_id: uuid.UUID, values: dict[str, Any]) -> BillingProfile:
        if values and self.actor is not None:
            values = {**values, "updated_by": self.actor.id}
        return super().update(record_id, values)

    def for_rule(self, rule: PricingRule) -> BillingProfile:
        """The space's own profile, else the property's. There is no fallback."""
        if rule.space_id is not None:
            own = self.session.scalar(
                select(BillingProfile).where(BillingProfile.space_id == rule.space_id)
            )
            if own is not None:
                return own
        profile = self.session.scalar(
            select(BillingProfile).where(
                BillingProfile.property_id == rule.property_id, BillingProfile.space_id.is_(None)
            )
        )
        if profile is None:
            raise ConflictError(
                "This property has no billing profile yet. Set billable hours, days and "
                "discounts first; estimates are never made from assumed figures."
            )
        return profile

    def to_read(self, profiles: Sequence[BillingProfile]) -> list[BillingProfileRead]:
        ids = {p.space_id for p in profiles if p.space_id}
        names = (
            dict(self.session.execute(select(Space.id, Space.name).where(Space.id.in_(ids))).all())
            if ids
            else {}
        )
        return [
            BillingProfileRead.model_validate(
                {
                    **{c.key: getattr(p, c.key) for c in BillingProfile.__table__.columns},
                    "space_name": names.get(p.space_id),
                }
            )
            for p in profiles
        ]


class DerivationService:
    def __init__(self, session, actor=None) -> None:  # type: ignore[no-untyped-def]
        self.session = session
        self.actor = actor
        self.pricing = PricingService(session, actor)
        self.profiles = BillingProfileService(session, actor)

    def preview(
        self, rule_id: uuid.UUID, periods: Sequence[BillingPeriod] | None = None
    ) -> DerivationPreview:
        base = self._base(rule_id)
        record = self.profiles.for_rule(base)
        profile = Profile.of(record)
        targets = [
            p for p in (periods or CONVERTIBLE) if p in CONVERTIBLE and p != base.billing_period
        ]
        estimates = []
        for period in targets:
            d = derive(base, period, profile)
            existing = self._existing(base, period)
            estimates.append(
                DerivedEstimate(
                    billing_period=period,
                    amount=float(d.amount),
                    maintenance_amount=float(d.maintenance_amount)
                    if d.maintenance_amount is not None
                    else None,
                    additional_charges=[
                        AdditionalChargeRead(label=c["label"], amount=float(c["amount"]))
                        for c in d.additional_charges
                    ],
                    deposit_rule=d.deposit_rule,
                    deposit_value=float(d.deposit_value) if d.deposit_value is not None else None,
                    formula=d.formula,
                    existing=ExistingRate(
                        id=existing.id,
                        source=RateSource(existing.source),
                        status=PricingStatus(existing.status),
                        amount=float(existing.amount),
                    )
                    if existing
                    else None,
                    on_approval="CREATE"
                    if existing is None
                    else "REPLACE_DERIVED"
                    if existing.source == RateSource.DERIVED
                    else "KEEP_CONFIGURED",
                )
            )
        return DerivationPreview(
            base=self.pricing.to_read([base])[0],
            profile=self.profiles.to_read([record])[0],
            estimates=estimates,
        )

    def approve(
        self, rule_id: uuid.UUID, periods: Sequence[BillingPeriod], status: str
    ) -> DerivationResult:
        base = self._base(rule_id)
        record = self.profiles.for_rule(base)
        profile = Profile.of(record)
        created: list[PricingRule] = []
        skipped: list[DerivationSkip] = []
        for period in dict.fromkeys(periods):
            if period not in CONVERTIBLE or period == base.billing_period:
                skipped.append(
                    DerivationSkip(billing_period=period, reason="Not a period it converts to.")
                )
                continue
            existing = self._existing(base, period)
            if existing is not None and existing.source != RateSource.DERIVED:
                skipped.append(
                    DerivationSkip(
                        billing_period=period,
                        reason="A configured rate already exists for this period; it is kept.",
                    )
                )
                continue
            d = derive(base, period, profile)
            values = {
                "property_id": base.property_id,
                "space_id": base.space_id,
                "building_id": base.building_id,
                "applies_to": base.applies_to,
                "rate_basis": base.rate_basis,
                "billing_period": period,
                "custom_period_label": None,
                "amount": d.amount,
                "currency": base.currency,
                "effective_from": base.effective_from,
                "effective_to": base.effective_to,
                "status": PricingStatus(status),
                "maintenance_amount": d.maintenance_amount,
                "maintenance_basis": base.maintenance_basis
                if d.maintenance_amount is not None
                else None,
                "tax_treatment": base.tax_treatment,
                "tax_rate": base.tax_rate,
                "additional_charges": d.additional_charges,
                "deposit_rule": d.deposit_rule,
                "deposit_value": d.deposit_value,
                "payment_frequency": d.payment_frequency,
                "notes": base.notes,
                "source": RateSource.DERIVED,
                "base_rule_id": base.id,
                "billing_profile_id": record.id,
                "derivation": {
                    "from_rule": str(base.id),
                    "from_period": base.billing_period,
                    "from_amount": str(base.amount),
                    "profile": profile.as_dict(),
                    "formula": d.formula,
                    "unrounded": str(d.unrounded),
                },
            }
            if existing is not None and status == PricingStatus.ACTIVE:
                self.pricing.retire_derived_like(values)
            elif existing is not None:
                skipped.append(
                    DerivationSkip(
                        billing_period=period,
                        reason="An active derived rate exists; approve as ACTIVE to replace it.",
                    )
                )
                continue
            self.pricing.validate(values, None)
            rule = PricingRule(**values, created_by=self.actor.id if self.actor else None)
            if self.actor is not None:
                rule.updated_by = self.actor.id
            self.session.add(rule)
            flush(self.session)
            self.pricing._log("created", rule)
            created.append(rule)
        commit(self.session)
        return DerivationResult(created=self.pricing.to_read(created), skipped=skipped)

    def _base(self, rule_id: uuid.UUID) -> PricingRule:
        base = self.session.get(PricingRule, rule_id)
        if base is None:
            raise NotFoundError("Pricing rule not found.")
        if base.status == PricingStatus.RETIRED:
            raise ConflictError("A retired rule cannot be the base of new prices.")
        if base.source == RateSource.DERIVED:
            raise ConflictError(
                "Derive from the configured rate this one came from, not from an estimate."
            )
        return base

    def _existing(self, base: PricingRule, period: BillingPeriod) -> PricingRule | None:
        """A live (draft or active) rate for the same target, type, basis and period."""
        return self.session.scalar(
            select(PricingRule)
            .where(
                PricingRule.property_id == base.property_id,
                PricingRule.space_id.is_(None)
                if base.space_id is None
                else PricingRule.space_id == base.space_id,
                PricingRule.building_id.is_(None)
                if base.building_id is None
                else PricingRule.building_id == base.building_id,
                PricingRule.applies_to.is_(None)
                if base.applies_to is None
                else PricingRule.applies_to == base.applies_to,
                PricingRule.rate_basis == base.rate_basis,
                PricingRule.billing_period == period,
                PricingRule.status.in_([PricingStatus.ACTIVE, PricingStatus.DRAFT]),
                or_(
                    PricingRule.effective_to.is_(None),
                    PricingRule.effective_to >= base.effective_from,
                ),
            )
            .order_by(PricingRule.status, PricingRule.created_at.desc())
        )
