"""Property dashboard metrics, computed from current records on every request.

Property counts use the derived occupancy status (see ``app.services.occupancy``),
so the four buckets always add up to the total:

    total        properties that are not archived
    unavailable  not operationally ACTIVE
    occupied     ACTIVE with a tenant occupancy covering today
    reserved     ACTIVE, not occupied, with a PENDING/CONFIRMED booking not yet ended
    vacant       ACTIVE with none of the above

A booking alone never makes a property "occupied".
"""

import calendar
from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.activity import ActivityLog
from app.models.enums import OccupancyStatus, PropertyStatus, SpaceStatus
from app.models.property import Property
from app.models.space import Space
from app.models.tenant import TenantOccupancy
from app.schemas.dashboard import (
    ActivityEntry,
    DashboardSummary,
    OccupancyTrendPoint,
    PropertyAvailability,
    SpaceSummary,
)
from app.services.occupancy import business_today, property_occupancy, space_statuses

TREND_MONTHS = 6


def _live_spaces(session: Session) -> list[Space]:
    return list(
        session.scalars(
            select(Space)
            .join(Property, Property.id == Space.property_id)
            .where(Space.archived_at.is_(None), Property.archived_at.is_(None))
        )
    )


def summary(session: Session) -> DashboardSummary:
    now = datetime.now(UTC)
    properties = session.scalars(select(Property).where(Property.archived_at.is_(None))).all()
    by_property = Counter(
        item.status for item in property_occupancy(session, properties, now).values()
    )

    spaces = _live_spaces(session)
    by_space = Counter(space_statuses(session, spaces, now).values())
    return DashboardSummary(
        total=len(properties),
        vacant=by_property[OccupancyStatus.VACANT],
        reserved=by_property[OccupancyStatus.RESERVED],
        occupied=by_property[OccupancyStatus.OCCUPIED],
        unavailable=by_property[OccupancyStatus.UNAVAILABLE],
        spaces=SpaceSummary(
            total=len(spaces),
            available=by_space[SpaceStatus.AVAILABLE],
            booked=by_space[SpaceStatus.BOOKED],
            reserved=by_space[SpaceStatus.RESERVED],
            occupied=by_space[SpaceStatus.OCCUPIED],
            unavailable=by_space[SpaceStatus.UNAVAILABLE],
        ),
        occupancy_rate=(
            round(100 * by_space[SpaceStatus.OCCUPIED] / len(spaces), 1) if spaces else None
        ),
        occupancy_trend=_occupancy_trend(session, business_today(now)),
    )


def _month_ends(today: date) -> list[date]:
    """The last day of each of the previous months, then today for the current one."""
    days = []
    year, month = today.year, today.month
    for _ in range(TREND_MONTHS):
        last = date(year, month, calendar.monthrange(year, month)[1])
        days.append(min(last, today))
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    return list(reversed(days))


def _occupancy_trend(session: Session, today: date) -> list[OccupancyTrendPoint]:
    zone = ZoneInfo(get_settings().business_timezone)

    def local_day(moment: datetime | None) -> date | None:
        return moment.astimezone(zone).date() if moment else None

    spaces = [
        (space_id, property_id, local_day(created_at), local_day(archived_at))
        for space_id, property_id, created_at, archived_at in session.execute(
            select(Space.id, Space.property_id, Space.created_at, Space.archived_at)
        )
    ]
    occupancies = session.execute(
        select(
            TenantOccupancy.property_id,
            TenantOccupancy.space_id,
            TenantOccupancy.start_date,
            TenantOccupancy.end_date,
        )
    ).all()

    points = []
    for day in _month_ends(today):
        existing = [
            (space_id, property_id)
            for space_id, property_id, created, archived in spaces
            if created <= day and (archived is None or archived > day)
        ]
        if not existing:
            continue
        occupied_spaces = set()
        occupied_properties = set()
        for property_id, space_id, start, end in occupancies:
            if start <= day and (end is None or end >= day):
                if space_id is None:
                    occupied_properties.add(property_id)
                else:
                    occupied_spaces.add(space_id)
        occupied = sum(
            1
            for space_id, property_id in existing
            if space_id in occupied_spaces or property_id in occupied_properties
        )
        points.append(
            OccupancyTrendPoint(
                period=day.strftime("%b %Y"),
                occupancy_rate=round(100 * occupied / len(existing), 1),
            )
        )
    return points


def recent_activity(session: Session, limit: int) -> list[ActivityEntry]:
    rows = session.execute(
        select(ActivityLog, Property.name)
        .outerjoin(Property, Property.id == ActivityLog.property_id)
        .order_by(ActivityLog.created_at.desc(), ActivityLog.id)
        .limit(limit)
    ).all()
    return [
        ActivityEntry(
            id=entry.id,
            action=entry.action,
            entity_type=entry.entity_type,
            message=entry.message,
            property_id=entry.property_id,
            property_name=property_name,
            created_at=entry.created_at,
        )
        for entry, property_name in rows
    ]


def availability(session: Session) -> list[PropertyAvailability]:
    """Spaces free right now at each operational property that has spaces."""
    properties = session.scalars(
        select(Property)
        .where(Property.archived_at.is_(None), Property.status == PropertyStatus.ACTIVE)
        .order_by(Property.name)
    ).all()
    spaces = _live_spaces(session)
    statuses = space_statuses(session, spaces)

    totals: dict = defaultdict(int)
    free: dict = defaultdict(int)
    for space in spaces:
        totals[space.property_id] += 1
        if statuses[space.id] == SpaceStatus.AVAILABLE:
            free[space.property_id] += 1
    return [
        PropertyAvailability(
            property_id=prop.id,
            property_name=prop.name,
            available_spaces=free[prop.id],
            total_spaces=totals[prop.id],
        )
        for prop in properties
        if totals[prop.id]
    ]
