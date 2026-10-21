"""Bookings: time-bounded reservations of one space over ``[start_at, end_at)``.

Rules
-----
* PENDING, CONFIRMED and COMPLETED bookings hold their time range; CANCELLED
  releases it.
* Allowed transitions: PENDING -> CONFIRMED | CANCELLED, and
  CONFIRMED -> COMPLETED | CANCELLED. CANCELLED and COMPLETED are final.
* A space cannot be booked while another tenant occupies it (or its whole
  property) on any day the booking touches.
* Spaces nest (floor > portion > room > desk). A booking also conflicts with
  bookings of every space containing it and every space inside it, and an
  occupancy of any of those blocks it: booking portion 5A takes its desks, and
  a booked desk keeps 5A from being booked whole. A space inside one that is
  not ACTIVE cannot be booked either.

* Contracts (see services/contracts.py) hold spaces the same way. A tenant may
  book inside its own contracted space: such a booking adds no hold of its own.

Concurrency
-----------
A booking that holds a space owns an allocation in the shared allocation engine
(services/allocations.py): an exclusive claim on its space and shared claims on
every space containing it. The database's exclusion constraints on those claims
refuse a space held twice, and a space held together with anything inside it,
even between simultaneous requests; such a refusal is a 409 naming what is in the
way. The checks below run first only to explain refusals well. Writers share the
property row and lock spaces top down; structural changes take the property row
exclusively, so the hierarchy cannot change while a booking is checked.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models.booking import Booking
from app.models.enums import (
    AllocationSource,
    BookingStatus,
    PropertyStatus,
    SpaceOperationalStatus,
)
from app.models.property import Property
from app.models.space import Space
from app.models.tenant import Tenant, TenantOccupancy
from app.models.user import AppUser
from app.repositories.base import order_by_clause, paginate_rows, text_search
from app.schemas.booking import AvailabilityRead, BookingConflict, BookingRead
from app.schemas.common import ListQuery
from app.services.allocations import AllocationConflict, AllocationEngine, booking_period
from app.services.base import commit, flush, log_activity, require
from app.services.hierarchy import Family, family
from app.services.occupancy import OPEN_BOOKING_STATUSES, holds_inventory

MAX_DURATION = timedelta(days=366)
MIN_YEAR, MAX_YEAR = 2000, 2200
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
        self.engine = AllocationEngine(session)

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
        self._check_years(start_at, end_at)
        space = require(self.session, Space, space_id, "Space")
        prop = self.session.get(Property, space.property_id)
        kin = family(self.session, space)
        conflicts = self._overlapping([space.id], start_at, end_at, exclude_booking_id)
        related = self._overlapping(sorted(kin.related), start_at, end_at, exclude_booking_id)

        if prop is None or prop.archived_at is not None or prop.status != PropertyStatus.ACTIVE:
            message = "The property is not open for bookings."
        elif space.operational_status != SpaceOperationalStatus.ACTIVE:
            message = "The space is not open for bookings."
        elif closed := self._closed_container(kin):
            message = f'The space is inside "{closed.name}", which is not open for bookings.'
        elif conflicts:
            message = "The space is already booked for part of that time."
        elif related:
            message = self._related_message(kin, related[0])
        elif self._occupied_by_other(space, start_at, end_at, tenant_id=None, kin=kin):
            message = "The space is occupied by a tenant during that time."
        else:
            return AvailabilityRead(available=True, message="The space is available.", conflicts=[])
        found = [*conflicts, *related]
        names = self._names([booking.space_id for booking in found])
        listed = [
            BookingConflict.model_validate(booking).model_copy(
                update={
                    "space_name": names.get(booking.space_id),
                    "relation": "same"
                    if booking.space_id == space.id
                    else "contains"
                    if booking.space_id in kin.ancestors
                    else "is inside",
                }
            )
            for booking in found
        ]
        # Contracts in the way, from the allocation engine.
        own = self.engine.for_booking(exclude_booking_id) if exclude_booking_id else None
        listed += [
            BookingConflict(
                kind="CONTRACT",
                reference=c.reference,
                start_at=c.start_at,
                end_at=c.end_at,
                status=c.status,
                space_id=c.space_id,
                space_name=c.space_name,
                relation=c.relation,  # type: ignore[arg-type]
            )
            for c in self.engine.conflicts(
                space.id, booking_period(start_at, end_at), exclude=[own.id] if own else []
            )
            if c.kind == "CONTRACT"
        ]
        return AvailabilityRead(available=False, message=message, conflicts=listed)

    # --- writes ----------------------------------------------------------

    def create(self, values: dict[str, Any]) -> BookingRead:
        prop = require(self.session, Property, values["property_id"], "Property")
        self.engine.share_property(prop.id)
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
        self._hold(booking)
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
            self.engine.share_property(booking.property_id)
            space = self._lock_space(booking.space_id)
            self._check_window(start_at, end_at)
            self._ensure_free(space, start_at, end_at, tenant_id, exclude_id=booking.id)

        for field, value in values.items():
            setattr(booking, field, value)
        if rescheduled or "tenant_id" in values:
            flush(self.session)
            self._hold(booking)
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
        allocation = self.engine.for_booking(booking.id)
        if allocation is not None:
            self.engine.release(allocation, "BOOKING_CANCELLED")
        return self._finish("cancelled", booking)

    # --- helpers ---------------------------------------------------------

    def _get(self, booking_id: uuid.UUID) -> Booking:
        booking = self.session.get(Booking, booking_id)
        if booking is None:
            raise NotFoundError("Booking not found.")
        return booking

    def _hold(self, booking: Booking) -> None:
        """Writes, moves or releases the booking's allocation to match the booking."""
        allocation = self.engine.for_booking(booking.id)
        if booking.status == BookingStatus.CANCELLED or self._inside_own_contract(booking):
            if allocation is not None:
                self.engine.release(allocation, "INSIDE_OWN_CONTRACT")
            return
        period = booking_period(booking.start_at, booking.end_at)
        if allocation is None:
            self.engine.allocate(
                source=AllocationSource.BOOKING,
                record_id=booking.id,
                property_id=booking.property_id,
                space_id=booking.space_id,
                period=period,
            )
        else:
            self.engine.change_period(allocation, period)

    def _inside_own_contract(self, booking: Booking) -> bool:
        """The booking's tenant holds the space (or one containing it, or the whole
        property) under a contract for every day of the booking."""
        if booking.tenant_id is None:
            return False
        space = self.session.get(Space, booking.space_id)
        if space is None:
            return False
        kin = family(self.session, space)
        first_day, last_day = self._days(booking.start_at, booking.end_at)
        return bool(
            self.session.scalar(
                select(
                    exists().where(
                        TenantOccupancy.tenant_id == booking.tenant_id,
                        TenantOccupancy.property_id == booking.property_id,
                        holds_inventory(),
                        or_(
                            TenantOccupancy.space_id.in_([space.id, *kin.ancestors]),
                            TenantOccupancy.space_id.is_(None),
                        ),
                        TenantOccupancy.start_date <= first_day,
                        or_(
                            TenantOccupancy.end_date.is_(None),
                            TenantOccupancy.end_date >= last_day,
                        ),
                    )
                )
            )
        )

    def _own_allocations(self, booking_id: uuid.UUID | None) -> "list[uuid.UUID]":
        own = self.engine.for_booking(booking_id) if booking_id else None
        return [own.id] if own else []

    def _contract_tenant(self, reference: str) -> uuid.UUID | None:
        return self.session.scalar(
            select(TenantOccupancy.tenant_id).where(TenantOccupancy.reference == reference)
        )

    @staticmethod
    def _days(start_at: datetime, end_at: datetime) -> tuple[date, date]:
        zone = ZoneInfo(get_settings().business_timezone)
        # end_at is exclusive, so a booking ending at midnight does not touch the next day.
        return start_at.astimezone(zone).date(), (
            end_at.astimezone(zone) - timedelta(microseconds=1)
        ).date()

    def _closed_container(self, kin: Family) -> Space | None:
        """The nearest containing space that is not operationally ACTIVE."""
        for space_id in kin.ancestors:
            container = self.session.get(Space, space_id)
            if (
                container is not None
                and container.operational_status != SpaceOperationalStatus.ACTIVE
            ):
                return container
        return None

    def _related_message(self, kin: Family, booking: Booking) -> str:
        name = self._names([booking.space_id]).get(booking.space_id, "another space")
        return (
            f'Part of that time is already booked on "{name}", which '
            f"{kin.relation(booking.space_id)} this space."
        )

    def _names(self, space_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, str]:
        if not space_ids:
            return {}
        rows = self.session.execute(
            select(Space.id, Space.name).where(Space.id.in_(set(space_ids)))
        )
        return {space_id: name for space_id, name in rows}

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

    def _check_years(self, start_at: datetime, end_at: datetime) -> None:
        # Dates at the edge of what a datetime can hold overflow when converted
        # to the business time zone.
        if not (start_at.year >= MIN_YEAR and end_at.year <= MAX_YEAR):
            raise BadRequestError(f"Booking dates must be between {MIN_YEAR} and {MAX_YEAR}.")

    def _check_window(self, start_at: datetime, end_at: datetime) -> None:
        self._check_years(start_at, end_at)
        if start_at < datetime.now(UTC) - START_GRACE:
            raise BadRequestError("A booking cannot start in the past.")
        if end_at - start_at > MAX_DURATION:
            raise BadRequestError("A booking cannot be longer than 366 days.")

    def _overlapping(
        self,
        space_ids: Sequence[uuid.UUID],
        start_at: datetime,
        end_at: datetime,
        exclude_id: uuid.UUID | None,
    ) -> Sequence[Booking]:
        if not space_ids:
            return []
        statement = (
            select(Booking)
            .where(
                Booking.space_id.in_(space_ids),
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
        self,
        space: Space,
        start_at: datetime,
        end_at: datetime,
        tenant_id: uuid.UUID | None,
        kin: Family | None = None,
    ) -> bool:
        kin = kin or family(self.session, space)
        first_day, last_day = self._days(start_at, end_at)
        conditions = [
            TenantOccupancy.property_id == space.property_id,
            holds_inventory(),
            # The space, anything containing it or inside it, or the whole property.
            or_(
                TenantOccupancy.space_id.in_([space.id, *kin.related]),
                TenantOccupancy.space_id.is_(None),
            ),
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
        kin = family(self.session, space)
        if closed := self._closed_container(kin):
            self.session.rollback()
            raise ConflictError(
                f'The space is inside "{closed.name}", which is not open for bookings.'
            )
        if self._overlapping([space.id], start_at, end_at, exclude_id):
            self.session.rollback()
            raise ConflictError("The space is already booked for part of that time.")
        if related := self._overlapping(sorted(kin.related), start_at, end_at, exclude_id):
            message = self._related_message(kin, related[0])
            self.session.rollback()
            raise ConflictError(message)
        if self._occupied_by_other(space, start_at, end_at, tenant_id, kin=kin):
            # Name the contracts in the way (the tenant's own are no obstacle to it).
            found = [
                c
                for c in self.engine.conflicts(
                    space.id,
                    booking_period(start_at, end_at),
                    exclude=self._own_allocations(exclude_id),
                )
                if c.kind == "CONTRACT" and self._contract_tenant(c.reference) != tenant_id
            ]
            self.session.rollback()
            raise AllocationConflict(found, "The space is occupied by a tenant during that time.")

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
