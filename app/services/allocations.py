"""The allocation engine: the one place that holds and releases spaces.

Every booking or contract that holds a space owns one allocation. The engine
writes the allocation and its claims (exclusive on the space, shared on every
space containing it); the database's exclusion constraints on the claims then
refuse any conflicting hold, even between concurrent requests. Callers check
for conflicts first only to explain a refusal well; the database decides.

Time: booking periods are the booking's ``[start_at, end_at)``. Contract
periods run from local midnight of ``start_date`` to local midnight after
``end_date`` (end inclusive), in the business time zone; an open-ended contract
has no upper bound. So a contract ending on the 31st holds the whole 31st, and a
booking on the 1st of the next month does not touch it.

Locks, always in this order: the property row (KEY SHARE for allocation writers,
UPDATE for whole-property contracts and structural changes), then spaces top
down as claims are written. A lock wait longer than ``LOCK_TIMEOUT`` gives up.
"""

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, literal, or_, select
from sqlalchemy.dialects.postgresql import TSTZRANGE, Range
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ConflictError
from app.models.allocation import EXCLUSIVE, SHARED, Allocation, AllocationClaim
from app.models.booking import Booking
from app.models.enums import AllocationSource
from app.models.property import Property
from app.models.space import Space
from app.models.tenant import TenantOccupancy
from app.services.hierarchy import chain, parent_map

LOCK_TIMEOUT = "10s"
CLAIM_CONSTRAINTS = {"ex_allocation_claims_exclusive", "ex_allocation_claims_mixed"}

Period = Range[datetime]


@dataclass(frozen=True)
class Conflict:
    """A hold that is in the way, described for the person asking."""

    kind: str  # "BOOKING" or "CONTRACT"
    reference: str
    status: str
    space_id: uuid.UUID
    space_name: str
    start_at: datetime
    end_at: datetime | None
    # "same" space, a space that "contains" the requested one, or one "inside" it.
    relation: str

    def describe(self) -> str:
        what = "booked" if self.kind == "BOOKING" else f"held by contract {self.reference}"
        if self.relation == "same":
            if self.kind == "CONTRACT":
                return (
                    f"The space is already occupied under contract {self.reference} "
                    "for part of that time."
                )
            return f"The space is already {what} for part of that time."
        return (
            f'Part of that time is already {what} on "{self.space_name}", which '
            f"{self.relation} this space."
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "reference": self.reference,
            "status": self.status,
            "space_id": str(self.space_id),
            "space_name": self.space_name,
            "start_at": self.start_at.isoformat(),
            "end_at": self.end_at.isoformat() if self.end_at else None,
            "relation": self.relation,
        }


def booking_period(start_at: datetime, end_at: datetime) -> Period:
    return Range(start_at, end_at, bounds="[)")


def contract_period(start_date: date, end_date: date | None) -> Period:
    zone = ZoneInfo(get_settings().business_timezone)

    def midnight(day: date) -> datetime:
        return datetime.combine(day, time.min, tzinfo=zone).astimezone(UTC)

    upper = midnight(end_date + timedelta(days=1)) if end_date is not None else None
    return Range(midnight(start_date), upper, bounds="[)")


def _overlaps(column: object, period: Period) -> object:
    return column.op("&&")(literal(period, TSTZRANGE))  # type: ignore[attr-defined]


class AllocationEngine:
    def __init__(self, session: Session) -> None:
        self.session = session

    # --- locks -----------------------------------------------------------------

    def _bound_waits(self) -> None:
        self.session.execute(select(literal(1)))  # make sure a transaction is open
        self.session.connection().exec_driver_sql(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")

    def share_property(self, property_id: uuid.UUID) -> None:
        """Before checking or writing allocations: structural changes wait for us."""
        self._bound_waits()
        self.session.execute(
            select(Property.id).where(Property.id == property_id).with_for_update(key_share=True)
        )

    def own_property(self, property_id: uuid.UUID) -> None:
        """For holds on the whole property: every allocation writer waits for us."""
        self._bound_waits()
        self.session.execute(
            select(Property.id).where(Property.id == property_id).with_for_update()
        )

    # --- reads -----------------------------------------------------------------

    def for_booking(self, booking_id: uuid.UUID) -> Allocation | None:
        return self.session.scalar(select(Allocation).where(Allocation.booking_id == booking_id))

    def for_contract(self, contract_id: uuid.UUID) -> Allocation | None:
        return self.session.scalar(select(Allocation).where(Allocation.contract_id == contract_id))

    def conflicts(
        self,
        space_id: uuid.UUID,
        period: Period,
        *,
        exclude: Iterable[uuid.UUID] = (),
    ) -> list[Conflict]:
        """Live holds that a hold of ``space_id`` over ``period`` would collide with:
        any hold of the space itself or of something inside it (they all claim the
        space), and exclusive holds of any space containing it."""
        ancestors = chain(parent_map(self.session, [space_id]), space_id)
        statement = (
            select(Allocation)
            .join(AllocationClaim, AllocationClaim.allocation_id == Allocation.id)
            .where(
                AllocationClaim.is_active,
                _overlaps(AllocationClaim.period, period),
                or_(
                    AllocationClaim.space_id == space_id,
                    and_(
                        AllocationClaim.space_id.in_(ancestors),
                        AllocationClaim.claim_mode == EXCLUSIVE,
                    ),
                ),
            )
            .distinct()
        )
        excluded = set(exclude)
        if excluded:
            statement = statement.where(Allocation.id.not_in(excluded))
        found = self.session.scalars(statement).all()
        return self.describe(found, space_id, set(ancestors))

    def describe(
        self, allocations: Sequence[Allocation], space_id: uuid.UUID, ancestors: set[uuid.UUID]
    ) -> list[Conflict]:
        if not allocations:
            return []
        names = dict(
            self.session.execute(
                select(Space.id, Space.name).where(Space.id.in_({a.space_id for a in allocations}))
            ).all()
        )
        bookings = {
            b.id: b
            for b in self.session.scalars(
                select(Booking).where(
                    Booking.id.in_({a.booking_id for a in allocations if a.booking_id})
                )
            )
        }
        contracts = {
            c.id: c
            for c in self.session.scalars(
                select(TenantOccupancy).where(
                    TenantOccupancy.id.in_({a.contract_id for a in allocations if a.contract_id})
                )
            )
        }
        out = []
        for a in sorted(allocations, key=lambda a: a.period.lower):
            record: Booking | TenantOccupancy | None = (
                bookings.get(a.booking_id) if a.booking_id else contracts.get(a.contract_id)  # type: ignore[arg-type]
            )
            relation = (
                "same"
                if a.space_id == space_id
                else "contains"
                if a.space_id in ancestors
                else "is inside"
            )
            out.append(
                Conflict(
                    kind=a.source_type,
                    reference=record.reference if record else "unknown",
                    status=record.status if record else "unknown",
                    space_id=a.space_id,
                    space_name=names.get(a.space_id, "a space"),
                    start_at=a.period.lower,  # type: ignore[arg-type]
                    end_at=a.period.upper,
                    relation=relation,
                )
            )
        return out

    # --- writes ----------------------------------------------------------------

    def allocate(
        self,
        *,
        source: AllocationSource,
        record_id: uuid.UUID,
        property_id: uuid.UUID,
        space_id: uuid.UUID,
        period: Period,
    ) -> Allocation:
        """Holds the space and claims its chain. Raises ConflictError, naming what is in
        the way, when the database refuses the hold."""
        allocation = Allocation(
            source_type=source,
            booking_id=record_id if source == AllocationSource.BOOKING else None,
            contract_id=record_id if source == AllocationSource.CONTRACT else None,
            property_id=property_id,
            space_id=space_id,
            period=period,
            original_period=period,
        )
        self.session.add(allocation)
        self._flush(space_id, period, set())
        self._claim(allocation)
        self._flush(space_id, period, {allocation.id})
        return allocation

    def change_period(self, allocation: Allocation, period: Period) -> None:
        """Moves the hold to a new period; its claims follow (ON UPDATE CASCADE)."""
        allocation.period = period
        if not allocation.is_active:
            allocation.is_active, allocation.released_at, allocation.release_reason = (
                True,
                None,
                None,
            )
        self._flush(allocation.space_id, period, {allocation.id})

    def release(self, allocation: Allocation, reason: str) -> None:
        """Stops the hold; the allocation and its claims stay as history."""
        if not allocation.is_active:
            return
        allocation.is_active = False
        allocation.released_at = datetime.now(UTC)
        allocation.release_reason = reason
        self.session.flush()

    def rebuild_claims(self, allocation_ids: Iterable[uuid.UUID]) -> None:
        """Rewrites live allocations' claims for the current hierarchy (after a space
        that was never on a floor is assigned to one)."""
        for allocation in self.session.scalars(
            select(Allocation).where(Allocation.id.in_(set(allocation_ids)), Allocation.is_active)
        ):
            self.session.execute(
                AllocationClaim.__table__.delete().where(
                    AllocationClaim.allocation_id == allocation.id
                )
            )
            self._claim(allocation)
            self._flush(allocation.space_id, allocation.period, {allocation.id})

    def live_allocation_ids(self, space_ids: Iterable[uuid.UUID]) -> list[uuid.UUID]:
        """Active allocations of these spaces that have not ended yet."""
        return list(
            self.session.scalars(
                select(Allocation.id).where(
                    Allocation.space_id.in_(set(space_ids)),
                    Allocation.is_active,
                    or_(
                        func.upper_inf(Allocation.period),
                        func.upper(Allocation.period) > datetime.now(UTC),
                    ),
                )
            )
        )

    # --- helpers ---------------------------------------------------------------

    def _claim(self, allocation: Allocation) -> None:
        parents = parent_map(self.session, [allocation.space_id])
        ancestors = chain(parents, allocation.space_id)
        # Top down, so every writer locks spaces in the same order.
        for space_id in reversed(ancestors):
            self.session.add(self._claim_row(allocation, space_id, SHARED))
        self.session.add(self._claim_row(allocation, allocation.space_id, EXCLUSIVE))

    @staticmethod
    def _claim_row(allocation: Allocation, space_id: uuid.UUID, mode: int) -> AllocationClaim:
        return AllocationClaim(
            allocation_id=allocation.id,
            period=allocation.period,
            is_active=allocation.is_active,
            space_id=space_id,
            claim_mode=mode,
        )

    def _flush(self, space_id: uuid.UUID, period: Period, own: set[uuid.UUID]) -> None:
        try:
            self.session.flush()
        except IntegrityError as exc:
            constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
            self.session.rollback()
            if constraint not in CLAIM_CONSTRAINTS:
                raise
            # Committed by a concurrent request after our own check: describe it now.
            found = self.conflicts(space_id, period, exclude=own)
            self.session.rollback()
            raise AllocationConflict(found) from None


class AllocationConflict(ConflictError):
    """409 naming the holds in the way."""

    def __init__(self, conflicts: Sequence[Conflict], message: str | None = None) -> None:
        detail = message or (
            conflicts[0].describe()
            if conflicts
            else "The space, or a space containing it or inside it, is already booked or "
            "contracted for part of that time."
        )
        super().__init__(detail, conflicts=[c.as_dict() for c in conflicts])
