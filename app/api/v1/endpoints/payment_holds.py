"""Checkout holds that need the property team: late or mismatched payments."""

import uuid

from fastapi import APIRouter

from app.api.dependencies import CurrentAdmin, DbSession
from app.api.responses import NOT_FOUND, PROTECTED
from app.schemas.payment_hold import PaymentHoldAdminRead, PaymentHoldResolve
from app.services.payment_holds import PaymentHoldService

router = APIRouter(prefix="/payment-holds", tags=["Bookings"], responses=PROTECTED)


@router.get(
    "/needs-review",
    response_model=list[PaymentHoldAdminRead],
    summary="Payments that need a refund or a decision",
)
def needing_review(admin: CurrentAdmin, session: DbSession) -> list[PaymentHoldAdminRead]:
    """Paid after the 10-minute hold ended when the space was taken, or paid the wrong
    amount. No booking was made for these."""
    return [
        PaymentHoldAdminRead.model_validate(h)
        for h in PaymentHoldService(session, admin).needing_review()
    ]


@router.post(
    "/{hold_id}/resolve",
    response_model=PaymentHoldAdminRead,
    responses=NOT_FOUND,
    summary="Record what was done for a payment needing review",
)
def resolve(
    admin: CurrentAdmin, session: DbSession, hold_id: uuid.UUID, body: PaymentHoldResolve
) -> PaymentHoldAdminRead:
    return PaymentHoldAdminRead.model_validate(
        PaymentHoldService(session, admin).resolve(hold_id, body.resolution)
    )
