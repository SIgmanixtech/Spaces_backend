"""Pricing rules, the prices they give each space, and charge estimates."""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.api.dependencies import CurrentAdmin, DbSession, ListParams
from app.api.responses import CONFLICT, NOT_FOUND, PROTECTED, _error
from app.models.enums import BillingPeriod, PricingStatus, PricingTarget
from app.models.pricing import BillingProfile, PricingRule
from app.schemas.common import Page
from app.schemas.pricing import (
    BillingProfileCreate,
    BillingProfileRead,
    BillingProfileUpdate,
    DerivationApprove,
    DerivationPreview,
    DerivationResult,
    EstimateRequest,
    EstimateResult,
    PricingRuleCreate,
    PricingRuleRead,
    PricingRuleUpdate,
    PropertyPricing,
)
from app.services.billing import BillingProfileService, DerivationService
from app.services.pricing import PricingService

router = APIRouter(prefix="/pricing-rules", tags=["Pricing"], responses=PROTECTED)
property_router = APIRouter(prefix="/properties", tags=["Pricing"], responses=PROTECTED)

OVERLAP = {
    409: _error(
        "An ACTIVE rule for the same target, space type, basis and billing period already "
        "covers part of the dates; or the rule is retired and cannot change."
    )
}


@router.get("", response_model=Page[PricingRuleRead], summary="List pricing rules")
def list_rules(
    admin: CurrentAdmin,
    session: DbSession,
    query: ListParams,
    property_id: uuid.UUID | None = None,
    space_id: uuid.UUID | None = None,
    target_type: PricingTarget | None = None,
    status: PricingStatus | None = None,
    billing_period: BillingPeriod | None = None,
) -> Page[PricingRuleRead]:
    """Latest effective date first. Sort by `effective_from`, `billing_period`, `status`,
    `created_at` or `updated_at`."""
    service = PricingService(session, admin)
    conditions = []
    if property_id is not None:
        conditions.append(PricingRule.property_id == property_id)
    items, total = service.list(
        query,
        {
            "space_id": space_id,
            "target_type": target_type,
            "status": status,
            "billing_period": billing_period,
        },
        conditions=conditions,
    )
    return Page(
        items=service.to_read(items), total=total, page=query.page, page_size=query.page_size
    )


@router.post(
    "",
    response_model=PricingRuleRead,
    status_code=201,
    responses={**NOT_FOUND, **OVERLAP},
    summary="Create a pricing rule",
)
def create_rule(
    admin: CurrentAdmin, session: DbSession, body: PricingRuleCreate
) -> PricingRuleRead:
    """Omit `space_id` for a property default (optionally only for `applies_to` spaces).
    The amount is multiplied by `rate_basis` and charged every `billing_period`. A new
    rule is DRAFT unless `status` says ACTIVE; only ACTIVE rules price anything."""
    service = PricingService(session, admin)
    return service.to_read([service.create(body.model_dump())])[0]


@router.post(
    "/estimate",
    response_model=EstimateResult,
    responses=NOT_FOUND,
    summary="Estimate a space's charges",
)
def estimate(admin: CurrentAdmin, session: DbSession, body: EstimateRequest) -> EstimateResult:
    """The rule that prices the space for the billing period on the day (its own, one
    inherited from a space containing it, or the property default), and the charges
    for `quantity` periods. 404 when no active rule applies."""
    price, charges = PricingService(session, admin).estimate(
        body.space_id,
        body.billing_period,
        on=body.on,
        quantity=body.quantity,
        persons=body.persons,
    )
    return EstimateResult(price=price, estimate=charges)


@router.get("/{rule_id}", response_model=PricingRuleRead, responses=NOT_FOUND, summary="Get a rule")
def get_rule(admin: CurrentAdmin, session: DbSession, rule_id: uuid.UUID) -> PricingRuleRead:
    service = PricingService(session, admin)
    return service.to_read([service.get(rule_id)])[0]


@router.patch(
    "/{rule_id}",
    response_model=PricingRuleRead,
    responses={**NOT_FOUND, **OVERLAP},
    summary="Update a pricing rule",
)
def update_rule(
    admin: CurrentAdmin, session: DbSession, rule_id: uuid.UUID, body: PricingRuleUpdate
) -> PricingRuleRead:
    service = PricingService(session, admin)
    return service.to_read([service.update(rule_id, body.model_dump(exclude_unset=True))])[0]


@router.post(
    "/{rule_id}/retire",
    response_model=PricingRuleRead,
    responses=NOT_FOUND,
    summary="Retire a pricing rule",
)
def retire_rule(admin: CurrentAdmin, session: DbSession, rule_id: uuid.UUID) -> PricingRuleRead:
    """The rule stops pricing anything but is kept for history."""
    service = PricingService(session, admin)
    return service.to_read([service.retire(rule_id)])[0]


@router.delete(
    "/{rule_id}",
    status_code=204,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Delete a draft pricing rule",
)
def delete_rule(admin: CurrentAdmin, session: DbSession, rule_id: uuid.UUID) -> None:
    """Drafts only. Active rules are retired instead, which keeps their history."""
    PricingService(session, admin).delete(rule_id)


@property_router.get(
    "/{property_id}/pricing",
    response_model=PropertyPricing,
    responses=NOT_FOUND,
    summary="Effective prices of a property's spaces",
)
def property_pricing(
    admin: CurrentAdmin,
    session: DbSession,
    property_id: uuid.UUID,
    on: Annotated[date | None, Query(description="Defaults to today.")] = None,
) -> PropertyPricing:
    """Every rentable space with the price that applies to it on the day for each billing
    period, where it comes from (its own rule, inherited, or the property default), and a
    one-period estimate. The space's legacy list price is shown separately, never mixed in."""
    return PricingService(session, admin).effective(property_id, on)


# --- billing profiles and derived rates ----------------------------------------------------

profile_router = APIRouter(prefix="/billing-profiles", tags=["Pricing"], responses=PROTECTED)


@profile_router.get("", response_model=list[BillingProfileRead], summary="Billing profiles")
def list_profiles(
    admin: CurrentAdmin, session: DbSession, property_id: uuid.UUID
) -> list[BillingProfileRead]:
    """The property's profile and any space overrides."""
    service = BillingProfileService(session, admin)
    rows = session.scalars(
        select(BillingProfile)
        .where(BillingProfile.property_id == property_id)
        .order_by(BillingProfile.space_id.is_not(None), BillingProfile.created_at)
    ).all()
    return service.to_read(rows)


@profile_router.post(
    "",
    response_model=BillingProfileRead,
    status_code=201,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Create a billing profile",
)
def create_profile(
    admin: CurrentAdmin, session: DbSession, body: BillingProfileCreate
) -> BillingProfileRead:
    """Every figure is required: billable hours per day, days per week and per month,
    weekly and monthly discounts, and the rounding policy. Omit `space_id` for the
    property's profile; set it to override the profile for one space."""
    service = BillingProfileService(session, admin)
    return service.to_read([service.create(body.model_dump())])[0]


@profile_router.patch(
    "/{profile_id}",
    response_model=BillingProfileRead,
    responses=NOT_FOUND,
    summary="Update a billing profile",
)
def update_profile(
    admin: CurrentAdmin, session: DbSession, profile_id: uuid.UUID, body: BillingProfileUpdate
) -> BillingProfileRead:
    """Approved derived rates keep the figures they were approved with; preview and approve
    again to use the new profile."""
    service = BillingProfileService(session, admin)
    return service.to_read([service.update(profile_id, body.model_dump(exclude_unset=True))])[0]


@profile_router.delete(
    "/{profile_id}", status_code=204, responses=NOT_FOUND, summary="Delete a billing profile"
)
def delete_profile(admin: CurrentAdmin, session: DbSession, profile_id: uuid.UUID) -> None:
    BillingProfileService(session, admin).delete(profile_id)


@router.post(
    "/{rule_id}/derivations",
    response_model=DerivationPreview,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Preview the rule in other billing periods",
)
def preview_derivations(
    admin: CurrentAdmin,
    session: DbSession,
    rule_id: uuid.UUID,
    periods: Annotated[list[BillingPeriod] | None, Query()] = None,
) -> DerivationPreview:
    """Estimates from the billing profile (the space's own, else the property's). Nothing
    is saved or priced. 409 when there is no profile."""
    return DerivationService(session, admin).preview(rule_id, periods)


@router.post(
    "/{rule_id}/derivations/approve",
    response_model=DerivationResult,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Approve estimates as prices",
)
def approve_derivations(
    admin: CurrentAdmin, session: DbSession, rule_id: uuid.UUID, body: DerivationApprove
) -> DerivationResult:
    """Creates a DERIVED rule per approved period, recalculated now. A configured or
    override rate for a period is kept and that period is skipped; an earlier derived rate
    is replaced when approving as ACTIVE."""
    return DerivationService(session, admin).approve(rule_id, body.periods, body.status)
