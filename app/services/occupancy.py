"""Derived occupancy: what is occupied, reserved or free right now.

Nothing here is stored. Each value is computed from tenant occupancies and
bookings at the moment of the request:

Property (``OccupancyStatus``)
    UNAVAILABLE  the property is not operationally ACTIVE
    OCCUPIED     at least one tenant occupancy covers today
    RESERVED     no occupancy today, but a PENDING/CONFIRMED booking has not ended yet
    VACANT       none of the above

Space (``SpaceStatus``)
    UNAVAILABLE  the space is not operationally ACTIVE
    OCCUPIED     a tenant occupancy of the space (or of its whole property) covers today
    BOOKED       a CONFIRMED booking is in progress
    RESERVED     a PENDING/CONFIRMED booking has not ended yet
    AVAILABLE    none of the above

    A space also takes the status of any space containing it, when that is the
    stronger of the two (in the order above): a desk inside an occupied portion
    is OCCUPIED, one inside a booked portion is BOOKED, one inside a closed
    portion is UNAVAILABLE. A container does not take its contents' status: a
    portion with one booked desk is not booked as a whole, though booking it
    for that time is refused.
"""

import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.booking import Booking
from app.models.enums import (
    BookingStatus,
    OccupancyStatus,
    PropertyStatus,
    SpaceOperationalStatus,
    SpaceStatus,
    SpaceType,
)
from app.models.property import Property
from app.models.space import Space
from app.models.tenant import Tenant, TenantOccupancy
from app.services.hierarchy import chain, parent_map

# Bookings that still hold a future or in-progress claim on a space.
OPEN_BOOKING_STATUSES = (BookingStatus.PENDING, BookingStatus.CONFIRMED)


def business_today(now: datetime | None = None) -> date:
    """Today's calendar date in the configured business time zone."""
    moment = now or datetime.now(UTC)
    return moment.astimezone(ZoneInfo(get_settings().business_timezone)).date()


def is_rentable_unit() -> ColumnElement[bool]:
    """Spaces that count as inventory to let: not the whole-floor root, not non-rentable ones.

    Every space that existed before the hierarchy satisfies this, so counts and
    lists that use it are unchanged for existing data.
    """
    return and_(Space.is_rentable, Space.space_type != SpaceType.FLOOR)


def occupancy_covers(day: date) -> ColumnElement[bool]:
    return and_(
        TenantOccupancy.start_date <= day,
        or_(TenantOccupancy.end_date.is_(None), TenantOccupancy.end_date >= day),
    )


@dataclass(frozen=True)
class PropertyOccupancy:
    status: OccupancyStatus
    rate: float | None
    tenant_name: str | None


def property_occupancy(
    session: Session, properties: Sequence[Property], now: datetime | None = None
) -> dict[uuid.UUID, PropertyOccupancy]:
    """Occupancy for a batch of properties in a fixed number of queries."""
    if not properties:
        return {}
    now = now or datetime.now(UTC)
    ids = [prop.id for prop in properties]

    occupied_spaces: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    whole_property: set[uuid.UUID] = set()
    tenants: dict[uuid.UUID, set[str]] = defaultdict(set)
    for property_id, space_id, tenant_name in session.execute(
        select(TenantOccupancy.property_id, TenantOccupancy.space_id, Tenant.name)
        .join(Tenant, Tenant.id == TenantOccupancy.tenant_id)
        .where(TenantOccupancy.property_id.in_(ids), occupancy_covers(business_today(now)))
    ):
        tenants[property_id].add(tenant_name)
        if space_id is None:
            whole_property.add(property_id)
        else:
            occupied_spaces[property_id].add(space_id)

    space_totals = dict(
        session.execute(
            select(Space.property_id, func.count())
            .where(Space.property_id.in_(ids), Space.archived_at.is_(None), is_rentable_unit())
            .group_by(Space.property_id)
        ).all()
    )
    reserved = set(
        session.scalars(
            select(Booking.property_id)
            .where(
                Booking.property_id.in_(ids),
                Booking.status.in_(OPEN_BOOKING_STATUSES),
                Booking.end_at > now,
            )
            .distinct()
        )
    )

    result: dict[uuid.UUID, PropertyOccupancy] = {}
    for prop in properties:
        occupied = prop.id in tenants
        if prop.status != PropertyStatus.ACTIVE:
            status = OccupancyStatus.UNAVAILABLE
        elif occupied:
            status = OccupancyStatus.OCCUPIED
        elif prop.id in reserved:
            status = OccupancyStatus.RESERVED
        else:
            status = OccupancyStatus.VACANT

        total = space_totals.get(prop.id, 0)
        if prop.id in whole_property:
            rate: float | None = 1.0
        elif total:
            rate = round(min(len(occupied_spaces[prop.id]), total) / total, 4)
        else:
            rate = None  # no spaces defined, so a rate would be meaningless

        names = tenants.get(prop.id, set())
        result[prop.id] = PropertyOccupancy(
            status=status,
            rate=rate,
            tenant_name=next(iter(names)) if len(names) == 1 else None,
        )
    return result


# Strongest first: a space shows the first of these that applies to it or a container.
_STATUS_ORDER = (
    SpaceStatus.UNAVAILABLE,
    SpaceStatus.OCCUPIED,
    SpaceStatus.BOOKED,
    SpaceStatus.RESERVED,
    SpaceStatus.AVAILABLE,
)


def space_statuses(
    session: Session, spaces: Sequence[Space], now: datetime | None = None
) -> dict[uuid.UUID, SpaceStatus]:
    if not spaces:
        return {}
    now = now or datetime.now(UTC)
    parents = parent_map(session, [space.id for space in spaces])
    # The spaces asked about and every space containing them.
    family_ids = list(parents)
    property_ids = list({space.property_id for space in spaces})

    inactive = set(
        session.scalars(
            select(Space.id).where(
                Space.id.in_(family_ids),
                Space.operational_status != SpaceOperationalStatus.ACTIVE,
            )
        )
    )

    occupied_spaces: set[uuid.UUID] = set()
    occupied_properties: set[uuid.UUID] = set()
    for property_id, space_id in session.execute(
        select(TenantOccupancy.property_id, TenantOccupancy.space_id).where(
            TenantOccupancy.property_id.in_(property_ids), occupancy_covers(business_today(now))
        )
    ):
        if space_id is None:
            occupied_properties.add(property_id)
        else:
            occupied_spaces.add(space_id)

    in_progress: set[uuid.UUID] = set()
    upcoming: set[uuid.UUID] = set()
    for space_id, status, start_at in session.execute(
        select(Booking.space_id, Booking.status, Booking.start_at).where(
            Booking.space_id.in_(family_ids),
            Booking.status.in_(OPEN_BOOKING_STATUSES),
            Booking.end_at > now,
        )
    ):
        if status == BookingStatus.CONFIRMED and start_at <= now:
            in_progress.add(space_id)
        else:
            upcoming.add(space_id)

    def own(space_id: uuid.UUID) -> SpaceStatus:
        if space_id in inactive:
            return SpaceStatus.UNAVAILABLE
        if space_id in occupied_spaces:
            return SpaceStatus.OCCUPIED
        if space_id in in_progress:
            return SpaceStatus.BOOKED
        if space_id in upcoming:
            return SpaceStatus.RESERVED
        return SpaceStatus.AVAILABLE

    result: dict[uuid.UUID, SpaceStatus] = {}
    for space in spaces:
        candidates = [own(space_id) for space_id in [space.id, *chain(parents, space.id)]]
        if space.operational_status != SpaceOperationalStatus.ACTIVE:
            candidates.append(SpaceStatus.UNAVAILABLE)
        if space.property_id in occupied_properties:
            candidates.append(SpaceStatus.OCCUPIED)
        result[space.id] = min(candidates, key=_STATUS_ORDER.index)
    return result
