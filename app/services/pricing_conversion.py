"""Derived prices: one billing period's rate converted into another under a billing profile.

The conversion policy (documented and tested; nothing is assumed beyond the profile):

* Every rate is first turned into a *daily equivalent* D, then into the target period.
* With h = billable hours per day, w = billable days per week, m = billable days per
  month, dw and dm the weekly and monthly discounts (as fractions):

      from HOURLY     D = rate × h
      from HALF_DAY   D = rate × 2            (a half-day is half the billable day)
      from FULL_DAY   D = rate                (a full day is the billable day)
      from DAILY      D = rate
      from WEEKLY     D = rate ÷ (w × (1 − dw))
      from MONTHLY    D = rate ÷ (m × (1 − dm))

      to HOURLY       D ÷ h
      to HALF_DAY     D ÷ 2
      to FULL_DAY     D
      to DAILY        D
      to WEEKLY       D × w × (1 − dw)
      to MONTHLY      D × m × (1 − dm)

  So for an hourly rate H: daily = H × h, weekly = H × h × w × (1 − dw) and
  monthly = H × h × m × (1 − dm). Converting back undoes the same steps exactly.
* CUSTOM periods have no conversion.
* Amounts are worked in Decimal and rounded once, at the end, by the profile's rounding
  policy (half up). Maintenance and additional charges are converted with the same
  factor as the rent. A deposit in months of rent only exists on a monthly rule; other
  periods carry a fixed deposit unchanged and no months-of-rent deposit.

A derived amount is an *estimate* until an administrator approves it; only approval
creates a DERIVED pricing rule, which is then priced and booked like any other.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.core.errors import BadRequestError
from app.models.enums import BillingPeriod, DepositRule, PaymentFrequency, RoundingPolicy

CONVERTIBLE = (
    BillingPeriod.HOURLY,
    BillingPeriod.HALF_DAY,
    BillingPeriod.FULL_DAY,
    BillingPeriod.DAILY,
    BillingPeriod.WEEKLY,
    BillingPeriod.MONTHLY,
)
STEP = {
    RoundingPolicy.PAISA: Decimal("0.01"),
    RoundingPolicy.RUPEE: Decimal("1"),
    RoundingPolicy.TEN_RUPEES: Decimal("10"),
}


@dataclass(frozen=True)
class Profile:
    hours_per_day: Decimal
    days_per_week: Decimal
    days_per_month: Decimal
    weekly_discount_pct: Decimal
    monthly_discount_pct: Decimal
    rounding: RoundingPolicy

    @classmethod
    def of(cls, record: Any) -> Profile:
        profile = cls(
            hours_per_day=Decimal(str(record.hours_per_day)),
            days_per_week=Decimal(str(record.days_per_week)),
            days_per_month=Decimal(str(record.days_per_month)),
            weekly_discount_pct=Decimal(str(record.weekly_discount_pct)),
            monthly_discount_pct=Decimal(str(record.monthly_discount_pct)),
            rounding=RoundingPolicy(record.rounding),
        )
        profile.check()
        return profile

    def check(self) -> None:
        if not (0 < self.hours_per_day <= 24):
            raise BadRequestError("Billable hours per day must be more than 0 and at most 24.")
        if not (0 < self.days_per_week <= 7):
            raise BadRequestError("Billable days per week must be more than 0 and at most 7.")
        if not (0 < self.days_per_month <= 31):
            raise BadRequestError("Billable days per month must be more than 0 and at most 31.")
        for label, value in (
            ("weekly", self.weekly_discount_pct),
            ("monthly", self.monthly_discount_pct),
        ):
            if not (0 <= value < 100):
                raise BadRequestError(f"The {label} discount must be from 0 up to (not) 100%.")

    def as_dict(self) -> dict[str, str]:
        return {
            "hours_per_day": str(self.hours_per_day),
            "days_per_week": str(self.days_per_week),
            "days_per_month": str(self.days_per_month),
            "weekly_discount_pct": str(self.weekly_discount_pct),
            "monthly_discount_pct": str(self.monthly_discount_pct),
            "rounding": self.rounding.value,
        }


def _n(value: Decimal) -> str:
    """A number for a formula: 6.0 -> 6, 100.00 -> 100, 7.50 -> 7.5."""
    text = format(value.normalize(), "f")
    return text


def _keep(pct: Decimal) -> Decimal:
    return 1 - pct / 100


def _to_daily(period: BillingPeriod, rate: Decimal, p: Profile) -> tuple[Decimal, str]:
    if period == BillingPeriod.HOURLY:
        return rate * p.hours_per_day, f"{_n(rate)} × {_n(p.hours_per_day)} h"
    if period == BillingPeriod.HALF_DAY:
        return rate * 2, f"{_n(rate)} × 2 half-days"
    if period in (BillingPeriod.FULL_DAY, BillingPeriod.DAILY):
        return rate, f"{_n(rate)}"
    if period == BillingPeriod.WEEKLY:
        return rate / (p.days_per_week * _keep(p.weekly_discount_pct)), (
            f"{_n(rate)} ÷ ({_n(p.days_per_week)} days × (1 − {_n(p.weekly_discount_pct)}%))"
        )
    if period == BillingPeriod.MONTHLY:
        return rate / (p.days_per_month * _keep(p.monthly_discount_pct)), (
            f"{_n(rate)} ÷ ({_n(p.days_per_month)} days × (1 − {_n(p.monthly_discount_pct)}%))"
        )
    raise BadRequestError("A custom billing period cannot be converted.")


def _from_daily(period: BillingPeriod, daily: Decimal, p: Profile) -> tuple[Decimal, str]:
    if period == BillingPeriod.HOURLY:
        return daily / p.hours_per_day, f"÷ {_n(p.hours_per_day)} h"
    if period == BillingPeriod.HALF_DAY:
        return daily / 2, "÷ 2"
    if period in (BillingPeriod.FULL_DAY, BillingPeriod.DAILY):
        return daily, ""
    if period == BillingPeriod.WEEKLY:
        return daily * p.days_per_week * _keep(p.weekly_discount_pct), (
            f"× {_n(p.days_per_week)} days × (1 − {_n(p.weekly_discount_pct)}%)"
        )
    if period == BillingPeriod.MONTHLY:
        return daily * p.days_per_month * _keep(p.monthly_discount_pct), (
            f"× {_n(p.days_per_month)} days × (1 − {_n(p.monthly_discount_pct)}%)"
        )
    raise BadRequestError("A custom billing period cannot be converted.")


def round_amount(value: Decimal, policy: RoundingPolicy) -> Decimal:
    step = STEP[policy]
    return (value / step).quantize(Decimal(1), rounding=ROUND_HALF_UP) * step


def factor(source: BillingPeriod, target: BillingPeriod, p: Profile) -> Decimal:
    """What one unit of a `source`-period price is worth over a `target` period."""
    daily, _ = _to_daily(source, Decimal(1), p)
    value, _ = _from_daily(target, daily, p)
    return value


@dataclass(frozen=True)
class Derived:
    billing_period: BillingPeriod
    amount: Decimal
    maintenance_amount: Decimal | None
    additional_charges: list[dict[str, str]]
    deposit_rule: DepositRule
    deposit_value: Decimal | None
    payment_frequency: PaymentFrequency
    formula: str
    unrounded: Decimal


def derive(rule: Any, target: BillingPeriod, p: Profile) -> Derived:
    """The terms of `rule` (a PricingRule or alike) converted to `target`."""
    source = BillingPeriod(rule.billing_period)
    if source == BillingPeriod.CUSTOM or target == BillingPeriod.CUSTOM:
        raise BadRequestError("A custom billing period cannot be converted.")
    if source == target:
        raise BadRequestError("The rule is already priced for that period.")
    amount = Decimal(str(rule.amount))
    daily, into = _to_daily(source, amount, p)
    raw, out = _from_daily(target, daily, p)
    f = factor(source, target, p)
    maintenance = (
        round_amount(Decimal(str(rule.maintenance_amount)) * f, p.rounding)
        if rule.maintenance_amount is not None
        else None
    )
    charges = [
        {
            "label": c["label"] if isinstance(c, dict) else c.label,
            "amount": str(
                round_amount(
                    Decimal(str(c["amount"] if isinstance(c, dict) else c.amount)) * f, p.rounding
                )
            ),
        }
        for c in rule.additional_charges or []
    ]
    deposit_rule = DepositRule(rule.deposit_rule)
    deposit_value = Decimal(str(rule.deposit_value)) if rule.deposit_value is not None else None
    if deposit_rule == DepositRule.MONTHS_OF_RENT and target != BillingPeriod.MONTHLY:
        deposit_rule, deposit_value = DepositRule.NONE, None
    payment = (
        PaymentFrequency(rule.payment_frequency)
        if target == BillingPeriod.MONTHLY
        else PaymentFrequency.PER_BOOKING
    )
    formula = f"({into}) {out}".replace(" ) ", ") ").strip()
    return Derived(
        billing_period=target,
        amount=round_amount(raw, p.rounding),
        maintenance_amount=maintenance,
        additional_charges=charges,
        deposit_rule=deposit_rule,
        deposit_value=deposit_value,
        payment_frequency=payment,
        formula=f"{formula} = {raw.quantize(Decimal('0.0001'))}, rounded to "
        f"{p.rounding.value.lower().replace('_', ' ')}",
        unrounded=raw,
    )
