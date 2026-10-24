"""Pricing rules: validation, inheritance and the charge calculation.

Which rule prices a space for a billing period, nearest first:

1. OWN: an ACTIVE rule on the space itself.
2. INHERITED: a per-sq-ft or per-person rule on a space containing it (its portion,
   then its whole floor). Fixed prices never flow down: a portion's fixed monthly
   price is the portion's, not each cabin's inside it.
3. BUILDING_DEFAULT: a default of the space's building, for its type, then for any space.
4. PROPERTY_DEFAULT: a property default limited to this type of space, then one
   for any space.

A rule only prices a space it fits: per sq ft needs an area, per person a
capacity, and a fixed basis the matching type (PER_CABIN fits a cabin, PER_SEAT a
seat...). When several rules on one level fit, a fixed price for the type wins
over per person, which wins over per sq ft.

The calculation keeps every line apart and rounds each to 0.01, half up: rent,
maintenance, additional charges and tax; the recurring total is their sum (tax
only when ADDITIONAL); the deposit is one-time and never in the total.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import or_, select

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models.building import Building
from app.models.enums import (
    BillingPeriod,
    DepositRule,
    MaintenanceBasis,
    PricingStatus,
    PricingTarget,
    RateBasis,
    RateSource,
    SpaceType,
    TaxTreatment,
)
from app.models.pricing import PricingRule
from app.models.property import Property
from app.models.space import Space
from app.schemas.pricing import (
    EffectivePrice,
    EstimateRead,
    LegacyPrice,
    PricingRuleRead,
    PropertyPricing,
    SpacePricing,
)
from app.services.base import CrudService, require
from app.services.occupancy import business_today

CENT = Decimal("0.01")

# The space types each fixed basis prices.
FIXED_BASIS_TYPES: dict[RateBasis, frozenset[SpaceType]] = {
    RateBasis.PER_FLOOR: frozenset({SpaceType.FLOOR}),
    RateBasis.PER_PORTION: frozenset({SpaceType.PORTION}),
    RateBasis.PER_CABIN: frozenset({SpaceType.CABIN}),
    RateBasis.PER_OFFICE: frozenset({SpaceType.PRIVATE_OFFICE}),
    RateBasis.PER_WORKSTATION: frozenset(
        {SpaceType.DEDICATED_WORKSTATION, SpaceType.FLEXIBLE_WORKSTATION, SpaceType.DESK}
    ),
    RateBasis.PER_SEAT: frozenset({SpaceType.SEAT}),
    RateBasis.PER_ROOM: frozenset({SpaceType.MEETING_ROOM}),
    RateBasis.PER_HALL: frozenset({SpaceType.CONFERENCE_ROOM, SpaceType.TRAINING_ROOM}),
    RateBasis.PER_UNIT: frozenset({SpaceType.COWORKING, SpaceType.OTHER}),
}
# Bases measured by the space, which a space inherits from the spaces containing it.
MEASURED = (RateBasis.PER_SQFT, RateBasis.PER_PERSON)
# When several rules on one level fit a space: fixed first, then per person, then per sq ft.
_PREFERENCE = {RateBasis.PER_SQFT: 2, RateBasis.PER_PERSON: 1}

BASIS_LABEL = {
    RateBasis.PER_SQFT: "per sq ft",
    RateBasis.PER_PERSON: "per person",
    RateBasis.PER_FLOOR: "per floor",
    RateBasis.PER_PORTION: "per portion",
    RateBasis.PER_CABIN: "per cabin",
    RateBasis.PER_OFFICE: "per office",
    RateBasis.PER_WORKSTATION: "per workstation",
    RateBasis.PER_SEAT: "per seat",
    RateBasis.PER_ROOM: "per room",
    RateBasis.PER_HALL: "per hall",
    RateBasis.PER_UNIT: "per unit",
}


def _round(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _dec(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def is_priceable(space: Space) -> bool:
    return space.is_rentable and space.space_type != SpaceType.NON_RENTABLE


def fits(basis: RateBasis | str, space: Space) -> bool:
    """Whether a rule with this basis can price the space."""
    basis = RateBasis(basis)
    if basis == RateBasis.PER_SQFT:
        return space.area_sqft is not None
    if basis == RateBasis.PER_PERSON:
        return space.capacity is not None
    return SpaceType(space.space_type) in FIXED_BASIS_TYPES[basis]


@dataclass(frozen=True)
class Charges:
    currency: str
    billing_period: str
    rate_basis: str
    quantity: Decimal
    measure: Decimal | None
    rent: Decimal
    maintenance: Decimal
    additional_charges: Decimal
    tax: Decimal
    tax_included: bool
    recurring_total: Decimal
    deposit: Decimal | None

    def read(self) -> EstimateRead:
        return EstimateRead(
            currency=self.currency,
            billing_period=BillingPeriod(self.billing_period),
            rate_basis=RateBasis(self.rate_basis),
            quantity=float(self.quantity),
            measure=float(self.measure) if self.measure is not None else None,
            rent=float(self.rent),
            maintenance=float(self.maintenance),
            additional_charges=float(self.additional_charges),
            tax=float(self.tax),
            tax_included=self.tax_included,
            recurring_total=float(self.recurring_total),
            deposit=float(self.deposit) if self.deposit is not None else None,
        )


def calculate(
    terms: Any,
    *,
    area: Decimal | float | None,
    persons: int | None,
    quantity: Decimal | int = 1,
) -> Charges:
    """The charges of ``quantity`` billing periods under a rule's terms.

    ``terms`` is a PricingRule or anything with the same attributes. Raises
    BadRequestError when the rate needs an area or head-count that is missing."""
    basis = RateBasis(terms.rate_basis)
    quantity = _dec(quantity)
    area_value = _dec(area) if area is not None else None
    measure: Decimal | None = None
    if basis == RateBasis.PER_SQFT:
        if area_value is None:
            raise BadRequestError("A per-sq-ft rate needs the space's area.")
        measure = area_value
    elif basis == RateBasis.PER_PERSON:
        if persons is None:
            raise BadRequestError("A per-person rate needs a number of persons.")
        measure = Decimal(persons)
    per_period = _dec(terms.amount) * (measure if measure is not None else 1)
    rent = _round(per_period * quantity)

    maintenance = Decimal(0)
    if terms.maintenance_amount is not None:
        m = _dec(terms.maintenance_amount)
        if MaintenanceBasis(terms.maintenance_basis) == MaintenanceBasis.PER_SQFT:
            if area_value is None:
                raise BadRequestError("Per-sq-ft maintenance needs the space's area.")
            m *= area_value
        maintenance = _round(m * quantity)

    extra = sum((_dec(_charge_amount(c)) for c in terms.additional_charges or []), Decimal(0))
    additional = _round(extra * quantity)

    taxable = rent + maintenance + additional
    treatment = TaxTreatment(terms.tax_treatment)
    rate = _dec(terms.tax_rate) / 100 if terms.tax_rate is not None else Decimal(0)
    if treatment == TaxTreatment.ADDITIONAL:
        tax = _round(taxable * rate)
        total = taxable + tax
    elif treatment == TaxTreatment.INCLUDED:
        tax = _round(taxable - taxable / (1 + rate))
        total = taxable
    else:
        tax = Decimal(0)
        total = taxable

    deposit: Decimal | None = None
    rule = DepositRule(terms.deposit_rule)
    if rule == DepositRule.FIXED:
        deposit = _round(_dec(terms.deposit_value))
    elif rule == DepositRule.MONTHS_OF_RENT:
        # Months of one period's rent, however many periods this estimate covers.
        deposit = _round(_dec(terms.deposit_value) * per_period)

    return Charges(
        currency=terms.currency,
        billing_period=terms.billing_period,
        rate_basis=basis,
        quantity=quantity,
        measure=measure,
        rent=rent,
        maintenance=maintenance,
        additional_charges=additional,
        tax=tax,
        tax_included=treatment == TaxTreatment.INCLUDED,
        recurring_total=total,
        deposit=deposit,
    )


def _charge_amount(charge: Any) -> Any:
    return charge["amount"] if isinstance(charge, dict) else charge.amount


@dataclass(frozen=True)
class Resolved:
    rule: PricingRule
    source: str
    source_space: Space | None


class PricingService(CrudService[PricingRule]):
    model = PricingRule
    label = "Pricing rule"
    sort_fields = ("effective_from", "billing_period", "status", "created_at", "updated_at")
    default_sort = "-effective_from"

    def describe(self, record: PricingRule) -> str:
        target = "property default" if record.space_id is None else "space"
        return (
            f"{record.currency} {record.amount} {BASIS_LABEL[RateBasis(record.rate_basis)]}, "
            f"{record.billing_period.lower().replace('_', '-')} ({target})"
        )

    # --- validation ------------------------------------------------------

    def validate(self, values: dict[str, Any], existing: PricingRule | None) -> None:
        if existing is None:
            require(self.session, Property, values["property_id"], "Property")
            space_id, building_id = values.get("space_id"), values.get("building_id")
            if space_id is not None and building_id is not None:
                raise BadRequestError("Price a space or a building, not both at once.")
            values["target_type"] = (
                PricingTarget.SPACE
                if space_id is not None
                else PricingTarget.BUILDING
                if building_id is not None
                else PricingTarget.PROPERTY
            )
            if values.get("status") == PricingStatus.RETIRED:
                raise BadRequestError("A new pricing rule cannot start out RETIRED.")
        elif existing.status == PricingStatus.RETIRED:
            raise ConflictError("A retired pricing rule cannot be changed. Create a new one.")
        space = None
        if values.get("space_id") is not None:
            space = require(self.session, Space, values["space_id"], "Space")
            if space.property_id != values["property_id"]:
                raise BadRequestError("The space does not belong to this property.")
            if not is_priceable(space):
                raise BadRequestError(f'"{space.name}" is not rentable, so it cannot be priced.')
            if values.get("applies_to") is not None:
                raise BadRequestError("applies_to is only for building and property defaults.")
        if values.get("building_id") is not None:
            building = require(self.session, Building, values["building_id"], "Building")
            if building.property_id != values["property_id"]:
                raise BadRequestError("The building does not belong to this property.")
        check_terms(values, space)

    # --- writes ----------------------------------------------------------

    def create(self, values: dict[str, Any]) -> PricingRule:
        values = _plain(values)
        if self.actor is not None:
            values["updated_by"] = self.actor.id
        if values.get("source") == RateSource.OVERRIDE and values.get("status") == (
            PricingStatus.ACTIVE
        ):
            self.retire_derived_like(values)
        return super().create(values)

    def retire_derived_like(self, values: dict[str, Any]) -> list[PricingRule]:
        """Retire ACTIVE DERIVED rules an override (or a re-approval) replaces: same
        property, target, type, basis and period, with overlapping dates."""
        found = self.session.scalars(
            select(PricingRule).where(
                PricingRule.property_id == values["property_id"],
                PricingRule.space_id.is_(None)
                if values.get("space_id") is None
                else PricingRule.space_id == values["space_id"],
                PricingRule.building_id.is_(None)
                if values.get("building_id") is None
                else PricingRule.building_id == values["building_id"],
                PricingRule.applies_to.is_(None)
                if values.get("applies_to") is None
                else PricingRule.applies_to == values["applies_to"],
                PricingRule.rate_basis == values["rate_basis"],
                PricingRule.billing_period == values["billing_period"],
                PricingRule.status == PricingStatus.ACTIVE,
                PricingRule.source == RateSource.DERIVED,
                or_(
                    PricingRule.effective_to.is_(None),
                    PricingRule.effective_to >= values["effective_from"],
                ),
                *(
                    [PricingRule.effective_from <= values["effective_to"]]
                    if values.get("effective_to")
                    else []
                ),
            )
        ).all()
        for rule in found:
            rule.status = PricingStatus.RETIRED
            self._log("retired", rule)
        if found:
            self.session.flush()
        return list(found)

    def update(self, record_id: uuid.UUID, values: dict[str, Any]) -> PricingRule:
        values = _plain(values)
        if values and self.actor is not None:
            values["updated_by"] = self.actor.id
        return super().update(record_id, values)

    def retire(self, record_id: uuid.UUID) -> PricingRule:
        record = self.get(record_id)
        if record.status == PricingStatus.RETIRED:
            return record
        return self.update(record_id, {"status": PricingStatus.RETIRED})

    def before_delete(self, record: PricingRule) -> None:
        if record.status != PricingStatus.DRAFT:
            raise ConflictError(
                "Only draft pricing rules can be deleted. Retire an active one instead, "
                "which keeps its history."
            )

    # --- reads -----------------------------------------------------------

    def to_read(self, rules: Sequence[PricingRule]) -> list[PricingRuleRead]:
        space_ids = {rule.space_id for rule in rules if rule.space_id}
        spaces = (
            {s.id: s for s in self.session.scalars(select(Space).where(Space.id.in_(space_ids)))}
            if space_ids
            else {}
        )
        building_ids = {rule.building_id for rule in rules if rule.building_id}
        buildings = (
            dict(
                self.session.execute(
                    select(Building.id, Building.name).where(Building.id.in_(building_ids))
                ).all()
            )
            if building_ids
            else {}
        )
        out = []
        for rule in rules:
            space = spaces.get(rule.space_id) if rule.space_id else None
            out.append(
                PricingRuleRead.model_validate(
                    {
                        **{c.key: getattr(rule, c.key) for c in PricingRule.__table__.columns},
                        "space_name": space.name if space else None,
                        "space_type": space.space_type if space else None,
                        "building_name": buildings.get(rule.building_id),
                    }
                )
            )
        return out

    def effective(self, property_id: uuid.UUID, on: date | None = None) -> PropertyPricing:
        """What each rentable space of the property costs on a day, per billing period."""
        require(self.session, Property, property_id, "Property")
        on = on or business_today()
        spaces = list(
            self.session.scalars(
                select(Space).where(Space.property_id == property_id, Space.archived_at.is_(None))
            )
        )
        rules = self._active_rules(property_id, on)
        by_id = {s.id: s for s in spaces}
        reads = {r.id: r for r in self.to_read(rules)}
        out: list[SpacePricing] = []
        unpriced: list[uuid.UUID] = []
        for space in sorted(spaces, key=lambda s: (s.depth, s.name)):
            if not is_priceable(space):
                continue
            prices = []
            for period in BillingPeriod:
                found = resolve(space, period, rules, by_id)
                if found is not None:
                    prices.append(self._effective(space, period, found, reads))
            if not prices:
                unpriced.append(space.id)
            out.append(
                SpacePricing(
                    space_id=space.id,
                    space_name=space.name,
                    space_type=SpaceType(space.space_type),
                    prices=prices,
                    legacy_price=_legacy(space),
                )
            )
        return PropertyPricing(property_id=property_id, on=on, spaces=out, unpriced=unpriced)

    def estimate(
        self,
        space_id: uuid.UUID,
        period: BillingPeriod,
        *,
        on: date | None = None,
        quantity: Decimal = Decimal(1),
        persons: int | None = None,
    ) -> tuple[EffectivePrice, EstimateRead]:
        space = require(self.session, Space, space_id, "Space")
        if not is_priceable(space):
            raise BadRequestError(f'"{space.name}" is not rentable, so it has no price.')
        on = on or business_today()
        rules = self._active_rules(space.property_id, on)
        by_id = {
            s.id: s
            for s in self.session.scalars(
                select(Space).where(Space.property_id == space.property_id)
            )
        }
        found = resolve(space, period, rules, by_id)
        if found is None:
            raise NotFoundError(
                f'No active {period.lower().replace("_", "-")} price applies to "{space.name}" '
                f"on {on.isoformat()}."
            )
        reads = {r.id: r for r in self.to_read([found.rule])}
        price = self._effective(space, period, found, reads)
        charges = calculate(
            found.rule,
            area=space.area_sqft,
            persons=persons if persons is not None else space.capacity,
            quantity=quantity,
        )
        return price, charges.read()

    # --- helpers ---------------------------------------------------------

    def _active_rules(self, property_id: uuid.UUID, on: date) -> list[PricingRule]:
        return list(
            self.session.scalars(
                select(PricingRule).where(
                    PricingRule.property_id == property_id,
                    PricingRule.status == PricingStatus.ACTIVE,
                    PricingRule.effective_from <= on,
                    or_(PricingRule.effective_to.is_(None), PricingRule.effective_to >= on),
                )
            )
        )

    def _effective(
        self,
        space: Space,
        period: BillingPeriod,
        found: Resolved,
        reads: dict[uuid.UUID, PricingRuleRead],
    ) -> EffectivePrice:
        try:
            estimate = calculate(found.rule, area=space.area_sqft, persons=space.capacity).read()
        except BadRequestError:
            estimate = None  # e.g. per-sq-ft maintenance on a space without an area
        return EffectivePrice(
            space_id=space.id,
            space_name=space.name,
            space_type=SpaceType(space.space_type),
            area_sqft=float(space.area_sqft) if space.area_sqft is not None else None,
            capacity=space.capacity,
            billing_period=period,
            source=found.source,  # type: ignore[arg-type]
            source_space_id=found.source_space.id if found.source_space else None,
            source_space_name=found.source_space.name if found.source_space else None,
            rule=reads[found.rule.id],
            estimate=estimate,
        )


def resolve(
    space: Space,
    period: BillingPeriod | str,
    rules: Iterable[PricingRule],
    spaces: dict[uuid.UUID, Space],
) -> Resolved | None:
    """The rule that prices ``space`` for ``period`` among ``rules`` (already filtered to
    ACTIVE on the day), or None. See the module docstring for the order."""
    on_space: dict[uuid.UUID, list[PricingRule]] = defaultdict(list)
    building_scoped: list[PricingRule] = []
    building_general: list[PricingRule] = []
    scoped: list[PricingRule] = []
    general: list[PricingRule] = []
    for rule in rules:
        if rule.billing_period != period:
            continue
        if rule.space_id is not None:
            on_space[rule.space_id].append(rule)
        elif rule.building_id is not None:
            if rule.building_id != space.building_id:
                continue
            if rule.applies_to is None:
                building_general.append(rule)
            elif rule.applies_to == space.space_type:
                building_scoped.append(rule)
        elif rule.applies_to is not None:
            if rule.applies_to == space.space_type:
                scoped.append(rule)
        else:
            general.append(rule)

    best = _best(on_space.get(space.id, []), space)
    if best is not None:
        return Resolved(best, "OWN", None)
    seen = {space.id}
    parent_id = space.parent_space_id
    while parent_id is not None and parent_id not in seen:
        seen.add(parent_id)
        parent = spaces.get(parent_id)
        if parent is None:
            break
        measured = [r for r in on_space.get(parent.id, []) if r.rate_basis in MEASURED]
        best = _best(measured, space)
        if best is not None:
            return Resolved(best, "INHERITED", parent)
        parent_id = parent.parent_space_id
    for level in (building_scoped, building_general):
        best = _best(level, space)
        if best is not None:
            return Resolved(best, "BUILDING_DEFAULT", None)
    for level in (scoped, general):
        best = _best(level, space)
        if best is not None:
            return Resolved(best, "PROPERTY_DEFAULT", None)
    return None


def _best(rules: list[PricingRule], space: Space) -> PricingRule | None:
    fitting = [r for r in rules if fits(r.rate_basis, space)]
    if not fitting:
        return None
    return min(fitting, key=lambda r: (_PREFERENCE.get(RateBasis(r.rate_basis), 0), r.created_at))


def check_terms(values: dict[str, Any], space: Space | None) -> None:
    """Cross-field rules of a pricing rule, with messages an admin can act on."""
    basis = RateBasis(values["rate_basis"])
    period = BillingPeriod(values["billing_period"])
    if values.get("effective_to") is not None and values["effective_to"] < values["effective_from"]:
        raise BadRequestError("The price's end date must be on or after its start date.")
    if (period == BillingPeriod.CUSTOM) != bool(values.get("custom_period_label")):
        raise BadRequestError(
            "A CUSTOM billing period needs a custom_period_label, and only CUSTOM has one."
        )
    if (values.get("maintenance_amount") is None) != (values.get("maintenance_basis") is None):
        raise BadRequestError("Give maintenance as an amount and a basis together, or neither.")
    treatment = TaxTreatment(values.get("tax_treatment") or TaxTreatment.EXEMPT)
    if (treatment == TaxTreatment.EXEMPT) != (values.get("tax_rate") is None):
        raise BadRequestError(
            "A tax rate is needed when tax is INCLUDED or ADDITIONAL, and only then."
        )
    deposit = DepositRule(values.get("deposit_rule") or DepositRule.NONE)
    if (deposit == DepositRule.NONE) != (values.get("deposit_value") is None):
        raise BadRequestError(
            "A deposit rule other than NONE needs a deposit_value, and only then."
        )
    if deposit == DepositRule.MONTHS_OF_RENT and period != BillingPeriod.MONTHLY:
        raise BadRequestError("A deposit in months of rent needs a MONTHLY billing period.")
    applies_to = values.get("applies_to")
    if applies_to is not None:
        target_type = SpaceType(applies_to)
        if target_type == SpaceType.NON_RENTABLE:
            raise BadRequestError("Non-rentable spaces cannot have a default price.")
        if basis not in MEASURED and target_type not in FIXED_BASIS_TYPES[basis]:
            raise BadRequestError(
                f"A {BASIS_LABEL[basis]} price cannot be the default for "
                f"{target_type.lower().replace('_', ' ')} spaces."
            )
    if space is not None:
        kind = space.space_type.lower().replace("_", " ")
        if basis == RateBasis.PER_SQFT and space.area_sqft is None:
            raise BadRequestError(
                f'"{space.name}" has no area, so a per-sq-ft price cannot be calculated. '
                "Enter its area first."
            )
        if basis == RateBasis.PER_PERSON and space.capacity is None:
            raise BadRequestError(
                f'"{space.name}" has no capacity, so a per-person price cannot be calculated.'
            )
        if basis not in MEASURED and not fits(basis, space):
            raise BadRequestError(f"A {BASIS_LABEL[basis]} price does not fit a {kind}.")
        if values.get("maintenance_basis") == MaintenanceBasis.PER_SQFT and space.area_sqft is None:
            raise BadRequestError(f'"{space.name}" has no area for per-sq-ft maintenance.')


def _plain(values: dict[str, Any]) -> dict[str, Any]:
    """JSON-ready additional charges: amounts as strings so no precision is lost."""
    charges = values.get("additional_charges")
    if charges is not None:
        values = {
            **values,
            "additional_charges": [
                {"label": c["label"], "amount": str(_round(_dec(c["amount"])))}
                if isinstance(c, dict)
                else {"label": c.label, "amount": str(_round(_dec(c.amount)))}
                for c in charges
            ],
        }
    return values


def _legacy(space: Space) -> LegacyPrice | None:
    if space.price_amount is None or space.price_unit is None:
        return None
    return LegacyPrice(
        amount=float(space.price_amount), unit=space.price_unit, currency=space.currency
    )


__all__ = [
    "BASIS_LABEL",
    "FIXED_BASIS_TYPES",
    "PricingService",
    "calculate",
    "check_terms",
    "fits",
    "resolve",
]


# How long one billing period lasts when a customer books by the clock. Bookings are
# exact time ranges, so the requested range must be a whole number of these.
PERIOD_LENGTH: dict[BillingPeriod, timedelta] = {
    BillingPeriod.HOURLY: timedelta(hours=1),
    BillingPeriod.HALF_DAY: timedelta(hours=4),
    BillingPeriod.FULL_DAY: timedelta(hours=8),
    BillingPeriod.DAILY: timedelta(days=1),
    BillingPeriod.WEEKLY: timedelta(days=7),
    BillingPeriod.MONTHLY: timedelta(days=30),
}


def periods_in(period: BillingPeriod | str, start_at: datetime, end_at: datetime) -> Decimal:
    """Billing periods in ``[start_at, end_at)``: a whole number, or BadRequestError."""
    period = BillingPeriod(period)
    length = PERIOD_LENGTH.get(period)
    if length is None:
        raise BadRequestError(
            "Custom billing periods are arranged with the property team, not booked online."
        )
    span = end_at - start_at
    if span <= timedelta(0):
        raise BadRequestError("The end must be after the start.")
    count, rest = divmod(span, length)
    if rest or count < 1:
        unit = {
            BillingPeriod.HOURLY: "whole hours",
            BillingPeriod.HALF_DAY: "whole half-days of 4 hours",
            BillingPeriod.FULL_DAY: "whole days of 8 hours",
            BillingPeriod.DAILY: "whole days of 24 hours",
            BillingPeriod.WEEKLY: "whole weeks",
            BillingPeriod.MONTHLY: "whole months of 30 days",
        }[period]
        raise BadRequestError(f"For a {period.lower().replace('_', '-')} price, book {unit}.")
    return Decimal(count)
