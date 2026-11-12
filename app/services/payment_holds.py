"""10-minute checkout holds while a customer pays online (owner decision, 12 Nov 2026).

Separate from booking requests (which reserve nothing) and from the 7-day holds on pending
contracts. A hold takes an allocation (source PAYMENT_HOLD), so the database itself refuses
any overlapping confirmed booking, contract or other hold. Server time is the authority:
``expires_at`` is set here and expired holds are released before every availability check
(AllocationEngine.release_expired_holds), even if the customer has closed the page.

Payment: no gateway is integrated. ``payment_verified`` and ``payment_failed`` are the
entry points a verified gateway notification must call (see app/services/payment_gateway.py);
they are idempotent by the gateway's payment reference, so duplicate or delayed
notifications never create a second booking. Starting a payment changes nothing.

Late payment: if a payment arrives after the hold ended, a booking is made only when the
space is still free; otherwise the hold goes to NEEDS_REVIEW (refund or another space,
decided by the property team) and no overlapping booking is ever created.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models.allocation import Allocation
from app.models.enums import (
    AllocationSource,
    BillingPeriod,
    BookingChannel,
    BookingStatus,
    PaymentHoldStatus,
)
from app.models.payment_hold import PaymentHold
from app.models.tenant import Tenant
from app.models.user import AppUser
from app.services.allocations import AllocationEngine, booking_period
from app.services.base import commit, flush, log_activity, one_transaction
from app.services.bookings import BookingService
from app.services.catalogue import CatalogueService
from app.services.pricing import PricingService, periods_in
from app.services.rental_policy import can_book

HOLD_MINUTES = 10
ENTITY = "payment_holds"


def payments_enabled() -> bool:
    return get_settings().online_payments_enabled


class PaymentHoldService:
    def __init__(self, session: Session, actor: AppUser | None) -> None:
        self.session = session
        self.actor = actor
        self.engine = AllocationEngine(session)

    # --- customer ---------------------------------------------------------------------

    def start(
        self,
        tenant: Tenant,
        *,
        space_id: uuid.UUID,
        billing_period: BillingPeriod,
        start_at: datetime,
        end_at: datetime,
        notes: str | None,
    ) -> PaymentHold:
        """Checkout begins: hold this exact space and period for 10 minutes, atomically."""
        if not payments_enabled():
            raise ConflictError("Paying online is not available yet. Send a booking request.")
        if self.actor is None:
            raise ConflictError("Sign in to pay.")
        catalogue = CatalogueService(self.session)
        space, prop = catalogue.get_space(space_id)
        ok, reason = can_book(space.space_type, billing_period)
        if not ok:
            raise ConflictError(reason or "Not offered.")
        if start_at.tzinfo is None or end_at.tzinfo is None:
            raise BadRequestError("Give times with a time zone.")
        quantity = periods_in(billing_period, start_at, end_at)
        try:
            price, estimate = PricingService(self.session).estimate(
                space.id, billing_period, on=catalogue._business_day(start_at), quantity=quantity
            )
        except NotFoundError:
            raise ConflictError(f"{space.name} has no price for that booking.") from None
        bookings = BookingService(self.session, self.actor)
        bookings._check_window(start_at, end_at)
        # A new checkout replaces this customer's earlier unpaid one.
        for old in self.session.scalars(
            select(PaymentHold)
            .where(
                PaymentHold.tenant_id == tenant.id,
                PaymentHold.status == PaymentHoldStatus.ACTIVE,
            )
            .with_for_update()
        ):
            self._release(old, "REPLACED")
        self.engine.share_property(prop.id)
        locked = bookings._lock_space(space.id)
        bookings._ensure_free(locked, start_at, end_at, tenant.id)
        now = datetime.now(UTC)
        hold = PaymentHold(
            id=uuid.uuid4(),
            status=PaymentHoldStatus.ACTIVE,
            tenant_id=tenant.id,
            user_id=self.actor.id,
            property_id=prop.id,
            space_id=space.id,
            start_at=start_at,
            end_at=end_at,
            billing_period=billing_period,
            pricing_rule_id=price.rule.id,
            amount=Decimal(str(estimate.recurring_total)),
            currency=estimate.currency,
            quote=estimate.model_dump(mode="json"),
            notes=notes,
            created_at=now,
            expires_at=now + timedelta(minutes=HOLD_MINUTES),
        )
        self.session.add(hold)
        flush(self.session)
        self.session.refresh(hold, ["reference"])
        # The database refuses an overlapping hold or confirmed booking here (claims).
        self.engine.allocate(
            source=AllocationSource.PAYMENT_HOLD,
            record_id=hold.id,
            property_id=prop.id,
            space_id=space.id,
            period=booking_period(start_at, end_at),
        )
        self._log(
            hold,
            f"Checkout hold {hold.reference} for {space.name} until {hold.expires_at:%H:%M} UTC.",
        )
        commit(self.session)
        return hold

    def get(self, tenant: Tenant, hold_id: uuid.UUID) -> PaymentHold:
        self.engine.release_expired_holds()
        hold = self.session.scalar(
            select(PaymentHold).where(PaymentHold.id == hold_id, PaymentHold.tenant_id == tenant.id)
        )
        if hold is None:
            raise NotFoundError("Checkout not found.")
        commit(self.session)
        return hold

    def cancel(self, tenant: Tenant, hold_id: uuid.UUID) -> PaymentHold:
        hold = self._lock(hold_id)
        if hold.tenant_id != tenant.id:
            raise NotFoundError("Checkout not found.")
        if hold.status == PaymentHoldStatus.ACTIVE:
            self._release(hold, "CANCELLED")
            self._log(hold, f"Checkout hold {hold.reference} cancelled by the customer.")
        commit(self.session)
        return hold

    # --- payment notifications (verified by the gateway adapter before calling) ----------

    def payment_verified(
        self, hold_id: uuid.UUID, *, payment_reference: str, amount: Decimal, currency: str
    ) -> PaymentHold:
        """A verified successful payment. Idempotent by ``payment_reference``."""
        reference = (payment_reference or "").strip()
        if not reference:
            raise BadRequestError("A payment reference is required.")
        hold = self._lock(hold_id)
        if hold.payment_reference is not None:
            if hold.payment_reference == reference:
                return hold  # duplicate or delayed notification: already handled
            raise ConflictError("This checkout was already paid with another payment.")
        other = self.session.scalar(
            select(PaymentHold.reference).where(PaymentHold.payment_reference == reference)
        )
        if other:
            raise ConflictError(f"That payment was already applied to {other}.")
        now = datetime.now(UTC)
        hold.payment_reference = reference[:100]
        hold.payment_amount = amount
        hold.payment_verified_at = now
        if currency != hold.currency or Decimal(amount) != hold.amount:
            self._review(
                hold,
                f"Paid {currency} {amount}, expected {hold.currency} {hold.amount}. "
                "Check the payment and refund or confirm by hand.",
            )
            commit(self.session)
            return hold
        if hold.status == PaymentHoldStatus.ACTIVE and hold.expires_at > now:
            self._convert(hold)
        else:
            # Paid after the hold ended: book only if the space is still free.
            if hold.status == PaymentHoldStatus.ACTIVE:
                self._release(hold, "EXPIRED", status=PaymentHoldStatus.EXPIRED)
            # Checked through the engine (every hold, booking and contract), under the
            # property's lock, without rolling anything back.
            self.engine.share_property(hold.property_id)
            taken = self.engine.conflicts(hold.space_id, booking_period(hold.start_at, hold.end_at))
            if not taken and hold.start_at > now:
                self._convert(hold)
            else:
                self._review(
                    hold,
                    "Paid after the 10-minute hold ended and the space was taken meanwhile "
                    "(or the time has started). No booking was made: refund the payment or "
                    "offer another space.",
                )
        commit(self.session)
        return hold

    def payment_failed(self, hold_id: uuid.UUID, *, payment_reference: str | None) -> PaymentHold:
        """A verified failed or abandoned payment: release the hold. Idempotent."""
        hold = self._lock(hold_id)
        if hold.status == PaymentHoldStatus.ACTIVE:
            self._release(hold, "PAYMENT_FAILED")
            self._log(hold, f"Payment for {hold.reference} failed; the space was released.")
        commit(self.session)
        return hold

    # --- property team ----------------------------------------------------------------

    def needing_review(self) -> list[PaymentHold]:
        return list(
            self.session.scalars(
                select(PaymentHold)
                .where(PaymentHold.status == PaymentHoldStatus.NEEDS_REVIEW)
                .order_by(PaymentHold.payment_verified_at)
            )
        )

    def resolve(self, hold_id: uuid.UUID, resolution: str) -> PaymentHold:
        """Record what was done for a late or mismatched payment (refund, another space...)."""
        hold = self._lock(hold_id)
        if hold.status != PaymentHoldStatus.NEEDS_REVIEW:
            raise ConflictError("Only a checkout needing review can be resolved.")
        text = " ".join((resolution or "").split())
        if not text:
            raise BadRequestError("Say what was done (e.g. refunded on a date).")
        hold.status = PaymentHoldStatus.RESOLVED
        hold.resolution = text[:2000]
        hold.resolved_at = datetime.now(UTC)
        hold.resolved_by = self.actor.id if self.actor else None
        self._log(hold, f"Checkout {hold.reference} resolved: {text[:200]}")
        commit(self.session)
        return hold

    # --- internals --------------------------------------------------------------------

    def _lock(self, hold_id: uuid.UUID) -> PaymentHold:
        hold = self.session.scalar(
            select(PaymentHold).where(PaymentHold.id == hold_id).with_for_update()
        )
        if hold is None:
            raise NotFoundError("Checkout not found.")
        return hold

    def _release(
        self, hold: PaymentHold, reason: str, *, status: str = PaymentHoldStatus.RELEASED
    ) -> None:
        allocation = self.session.scalar(
            select(Allocation).where(Allocation.payment_hold_id == hold.id)
        )
        if allocation is not None:
            self.engine.release(allocation, f"PAYMENT_HOLD_{reason}"[:32])
        hold.status = status
        hold.released_at = hold.released_at or datetime.now(UTC)
        hold.release_reason = reason[:32]
        flush(self.session)

    def _convert(self, hold: PaymentHold) -> None:
        """The hold becomes a CONFIRMED booking in one transaction: the hold's allocation is
        released and the booking's taken at once, so nobody can slip in between."""
        if hold.status == PaymentHoldStatus.ACTIVE:
            self._release(hold, "PAID")
        tenant = self.session.get(Tenant, hold.tenant_id)
        values: dict[str, Any] = {
            "property_id": hold.property_id,
            "space_id": hold.space_id,
            "tenant_id": hold.tenant_id,
            "customer_name": tenant.name if tenant else "Customer",
            "start_at": hold.start_at,
            "end_at": hold.end_at,
            "notes": hold.notes,
            "status": BookingStatus.CONFIRMED,
            "channel": BookingChannel.CUSTOMER,
            "billing_period": hold.billing_period,
            "pricing_rule_id": hold.pricing_rule_id,
            "quoted_total": hold.amount,
            "quoted_currency": hold.currency,
            "quote": hold.quote,
        }
        with one_transaction(self.session):
            booking = BookingService(self.session, self.actor).create(values)
        hold.booking_id = booking.id
        hold.status = PaymentHoldStatus.CONVERTED
        self._log(
            hold,
            f"Checkout {hold.reference} paid ({hold.payment_reference}); booking "
            f"{booking.reference} confirmed.",
        )
        flush(self.session)

    def _review(self, hold: PaymentHold, reason: str) -> None:
        if hold.status == PaymentHoldStatus.ACTIVE:
            self._release(hold, "REVIEW")
        hold.status = PaymentHoldStatus.NEEDS_REVIEW
        hold.released_at = hold.released_at or datetime.now(UTC)
        hold.review_reason = reason
        self._log(hold, f"Checkout {hold.reference} needs review: {reason}")
        flush(self.session)

    def _log(self, hold: PaymentHold, message: str) -> None:
        log_activity(
            self.session,
            self.actor,
            action="payment_hold",
            entity_type=ENTITY,
            entity_id=hold.id,
            property_id=hold.property_id,
            message=message[:500],
        )
