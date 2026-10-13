"""Bookings: time-bounded reservations of one space over ``[start_at, end_at)``.

Rules
-----
* PENDING, CONFIRMED and COMPLETED bookings hold their time range; CANCELLED
  releases it.
* Allowed transitions: PENDING -> CONFIRMED | CANCELLED, and
  CONFIRMED -> COMPLETED | CANCELLED. CANCELLED and COMPLETED are final.
* A space cannot be booked while another tenant occupies it (or its whole
  property) on any day the booking touches.

Concurrency
-----------
Creating or rescheduling locks the space row, so requests for the same space
run one at a time and each sees the others' committed bookings. The database's
exclusion constraint remains the final guard: if anything slips past the
check, the insert fails and the caller gets 409, never a double booking.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models.booking import Booking
from app.models.enums import BookingStatus, PropertyStatus, SpaceOperationalStatus
from app.models.property import Property
from app.models.space import Space
from app.models.tenant import Tenant, TenantOccupancy
from app.models.user import AppUser
from app.repositories.base import order_by_clause, paginate_rows, text_search
from app.schemas.booking import AvailabilityRead, BookingConflict, BookingRead
from app.schemas.common import ListQuery
from app.services.base import commit, flush, log_activity, require
from app.services.occupancy import OPEN_BOOKING_STATUSES

MAX_DURATION = timedelta(days=366)
# Lets a booking "starting now" survive the time the request spends in transit.
START_GRACE = timedelta(minutes=5)

_SORT_FIELDS = {
    "start_at": Booking.start_at,
    "end_at": Booking.end_at,
    "created_at": Booking.created_at,
    "reference": Booking.reference,
    "status": Booking.status,
}

_TRANSITIONS: dict[str, set[BookingStatus]] = {
    BookingStatus.PENDING: {BookingStatus.CONFIRMED, BookingStatus.CANCELLED},
    BookingStatus.CONFIRMED: {BookingStatus.COMPLETED, BookingStatus.CANCELLED},
    BookingStatus.CANCELLED: set(),
    BookingStatus.COMPLETED: set(),
}


class BookingService:
    def __init__(self, session: Session, actor: AppUser | None = None) -> None:
        self.session = session
        self.actor = actor

    # --- reads -----------------------------------------------------------

    def list(
        self,
        query: ListQuery,
        *,
        property_id: uuid.UUID | None = None,
        space_id: uuid.UUID | None = None,
        tenant_id: uuid.UUID | None = None,
        status: BookingStatus | None = None,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
    ) -> tuple[list[BookingRead], int]:
        statement = select(Booking)
        for column, value in (
            (Booking.property_id, property_id),
            (Booking.space_id, space_id),
            (Booking.tenant_id, tenant_id),
            (Booking.status, status),
        ):
            if value is not None:
                statement = statement.where(column == value)
        if window_start is not None:
            statement = statement.where(Booking.end_at > window_start)
        if window_end is not None:
            statement = statement.where(Booking.start_at < window_end)
        if query.search:
            statement = statement.where(
                text_search(
                    [Booking.reference, Booking.customer_name, Booking.customer_email],
                    query.search,
                )
            )
        order = order_by_clause(query.sort, _SORT_FIELDS, "-start_at")
        rows, total = paginate_rows(self.session, statement.order_by(*order, Booking.id), query)
        return self.to_read([row[0] for row in rows]), total

    def get(self, booking_id: uuid.UUID) -> BookingRead:
        return self.to_read([self._get(booking_id)])[0]

    def availability(
        self,
        space_id: uuid.UUID,
        start_at: datetime,
        end_at: datetime,
        exclude_booking_id: uuid.UUID | None = None,
    ) -> AvailabilityRead:
        """Answer from committed records only. Creating a booking re-checks under a lock."""
        if end_at <= start_at:
            raise BadRequestError("end_at must be after start_at.")
        space = require(self.session, Space, space_id, "Space")
        prop = self.session.get(Property, space.property_id)
        conflicts = self._overlapping(space.id, start_at, end_at, exclude_booking_id)

        if prop is None or prop.archived_at is not None or prop.status != PropertyStatus.ACTIVE:
            message = "The property is not open for bookings."
        elif space.operational_status != SpaceOperationalStatus.ACTIVE:
            message = "The space is not open for bookings."
        elif conflicts:
            message = "The space is already booked for part of that time."
        elif self._occupied_by_other(space, start_at, end_at, tenant_id=None):
            message = "The space is occupied by a tenant during that time."
        else:
            return AvailabilityRead(available=True, message="The space is available.", conflicts=[])
        return AvailabilityRead(
            available=False,
            message=message,
            conflicts=[BookingConflict.model_validate(booking) for booking in conflicts],
        )

    # --- writes ----------------------------------------------------------

    def create(self, values: dict[str, Any]) -> BookingRead:
        prop = require(self.session, Property, values["property_id"], "Property")
        space = self._lock_space(values["space_id"])
        if space.property_id != prop.id:
            raise BadRequestError("The space does not belong to this property.")
        if prop.status != PropertyStatus.ACTIVE:
            raise ConflictError("The property is not open for bookings.")
        if space.operational_status != SpaceOperationalStatus.ACTIVE:
            raise ConflictError("The space is not open for bookings.")
        if values.get("tenant_id") is not None:
            require(self.session, Tenant, values["tenant_id"], "Tenant")
        self._check_window(values["start_at"], values["end_at"])
        self._ensure_free(space, values["start_at"], values["end_at"], values.get("tenant_id"))

        booking = Booking(**values, created_by=self.actor.id if self.actor else None)
        self.session.add(booking)
        flush(self.session)
        self._log("created", booking, prop.id, f"for {space.name}")
        commit(self.session)
        return self.get(booking.id)

    def update(self, booking_id: uuid.UUID, values: dict[str, Any]) -> BookingRead:
        booking = self._get(booking_id)
        if booking.status not in OPEN_BOOKING_STATUSES:
            raise ConflictError(f"A {booking.status.lower()} booking cannot be changed.")
        if not values:
            return self.to_read([booking])[0]

        start_at = values.get("start_at", booking.start_at)
        end_at = values.get("end_at", booking.end_at)
        tenant_id = values.get("tenant_id", booking.tenant_id)
        customer_name = values.get("customer_name", booking.customer_name)
        if tenant_id is None and customer_name is None:
            raise BadRequestError("A booking needs a tenant or a customer name.")
        if "tenant_id" in values and tenant_id is not None:
            require(self.session, Tenant, tenant_id, "Tenant")

        rescheduled = (start_at, end_at) != (booking.start_at, booking.end_at)
        if rescheduled:
            if end_at <= start_at:
                raise BadRequestError("end_at must be after start_at.")
            space = self._lock_space(booking.space_id)
            self._check_window(start_at, end_at)
            self._ensure_free(space, start_at, end_at, tenant_id, exclude_id=booking.id)

        for field, value in values.items():
            setattr(booking, field, value)
        self._log("rescheduled" if rescheduled else "updated", booking, booking.property_id)
        commit(self.session)
        return self.get(booking_id)

    def confirm(self, booking_id: uuid.UUID) -> BookingRead:
        booking = self._transition(booking_id, BookingStatus.CONFIRMED)
        if booking.end_at <= datetime.now(UTC):
            self.session.rollback()
            raise ConflictError("The booking's time has already passed.")
        return self._finish("confirmed", booking)

    def complete(self, booking_id: uuid.UUID) -> BookingRead:
        booking = self._transition(booking_id, BookingStatus.COMPLETED)
        if booking.start_at > datetime.now(UTC):
            self.session.rollback()
            raise ConflictError("A booking cannot be completed before it starts.")
        return self._finish("completed", booking)

    def cancel(self, booking_id: uuid.UUID, reason: str | None = None) -> BookingRead:
        booking = self._transition(booking_id, BookingStatus.CANCELLED)
        booking.cancelled_at = datetime.now(UTC)
        booking.cancellation_reason = reason
        return self._finish("cancelled", booking)

    # --- helpers ---------------------------------------------------------

    def _get(self, booking_id: uuid.UUID) -> Booking:
        booking = self.session.get(Booking, booking_id)
        if booking is None:
            raise NotFoundError("Booking not found.")
        return booking

    def _lock_space(self, space_id: uuid.UUID) -> Space:
        space = self.session.scalar(
            select(Space).where(Space.id == space_id, Space.archived_at.is_(None)).with_for_update()
        )
        if space is None:
            raise NotFoundError("Space not found.")
        return space

    def _transition(self, booking_id: uuid.UUID, target: BookingStatus) -> Booking:
        # Locked so two simultaneous transitions cannot both act on the old status.
        booking = self.session.scalar(
            select(Booking).where(Booking.id == booking_id).with_for_update()
        )
        if booking is None:
            raise NotFoundError("Booking not found.")
        if target not in _TRANSITIONS[booking.status]:
            current = booking.status.lower()
            self.session.rollback()
            raise ConflictError(f"A {current} booking cannot be {target.lower()}.")
        booking.status = target
        return booking

    def _finish(self, action: str, booking: Booking) -> BookingRead:
        self._log(action, booking, booking.property_id)
        commit(self.session)
        return self.get(booking.id)

    def _check_window(self, start_at: datetime, end_at: datetime) -> None:
        if start_at < datetime.now(UTC) - START_GRACE:
            raise BadRequestError("A booking cannot start in the past.")
        if end_at - start_at > MAX_DURATION:
            raise BadRequestError("A booking cannot be longer than 366 days.")

    def _overlapping(
        self,
        space_id: uuid.UUID,
        start_at: datetime,
        end_at: datetime,
        exclude_id: uuid.UUID | None,
    ) -> Sequence[Booking]:
        statement = (
            select(Booking)
            .where(
                Booking.space_id == space_id,
                Booking.status != BookingStatus.CANCELLED,
                Booking.start_at < end_at,
                Booking.end_at > start_at,
            )
            .order_by(Booking.start_at)
        )
        if exclude_id is not None:
            statement = statement.where(Booking.id != exclude_id)
        return self.session.scalars(statement).all()

    def _occupied_by_other(
        self, space: Space, start_at: datetime, end_at: datetime, tenant_id: uuid.UUID | None
    ) -> bool:
        zone = ZoneInfo(get_settings().business_timezone)
        first_day = start_at.astimezone(zone).date()
        # end_at is exclusive, so a booking ending at midnight does not touch the next day.
        last_day = (end_at.astimezone(zone) - timedelta(microseconds=1)).date()
        conditions = [
            TenantOccupancy.property_id == space.property_id,
            or_(TenantOccupancy.space_id == space.id, TenantOccupancy.space_id.is_(None)),
            TenantOccupancy.start_date <= last_day,
            or_(TenantOccupancy.end_date.is_(None), TenantOccupancy.end_date >= first_day),
        ]
        if tenant_id is not None:
            conditions.append(TenantOccupancy.tenant_id != tenant_id)
        return bool(self.session.scalar(select(exists().where(and_(*conditions)))))

    def _ensure_free(
        self,
        space: Space,
        start_at: datetime,
        end_at: datetime,
        tenant_id: uuid.UUID | None,
        exclude_id: uuid.UUID | None = None,
    ) -> None:
        if self._overlapping(space.id, start_at, end_at, exclude_id):
            self.session.rollback()
            raise ConflictError("The space is already booked for part of that time.")
        if self._occupied_by_other(space, start_at, end_at, tenant_id):
            self.session.rollback()
            raise ConflictError("The space is occupied by a tenant during that time.")

    def _log(self, action: str, booking: Booking, property_id: uuid.UUID, suffix: str = "") -> None:
        log_activity(
            self.session,
            self.actor,
            action=action,
            entity_type="bookings",
            entity_id=booking.id,
            property_id=property_id,
            message=f"Booking {booking.reference} was {action} {suffix}".strip() + ".",
        )

    # Annotated as Sequence because the method named "list" above shadows the builtin here.
    def to_read(self, bookings: Sequence[Booking]) -> Sequence[BookingRead]:
        def names(model: Any, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
            if not ids:
                return {}
            return dict(
                self.session.execute(select(model.id, model.name).where(model.id.in_(ids))).all()
            )

        properties = names(Property, {b.property_id for b in bookings})
        spaces = names(Space, {b.space_id for b in bookings})
        tenants = names(Tenant, {b.tenant_id for b in bookings if b.tenant_id})
        return [
            BookingRead.model_validate(
                {
                    **{
                        column.key: getattr(booking, column.key)
                        for column in Booking.__table__.columns
                    },
                    "property_name": properties[booking.property_id],
                    "space_name": spaces[booking.space_id],
                    "tenant_name": tenants.get(booking.tenant_id),
                }
            )
            for booking in bookings
        ]
