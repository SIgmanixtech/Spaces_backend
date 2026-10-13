"""Database-level integrity rules. These hold even if application code has a bug."""

import re
import threading
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import get_session_factory
from app.models import Booking, Property, Space, Tenant, TenantOccupancy
from app.models.enums import BookingStatus, SpaceType

START = datetime(2030, 1, 10, 9, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


def make_property(session: Session, name: str = "Test Tower") -> Property:
    prop = Property(
        name=name,
        property_type="OFFICE",
        category="COMMERCIAL",
        address="1 Test Street",
        city="Hyderabad",
        state="Telangana",
        postal_code="500001",
    )
    session.add(prop)
    session.flush()
    return prop


def make_space(session: Session, prop: Property, name: str = "Room A") -> Space:
    space = Space(property_id=prop.id, name=name, space_type=SpaceType.MEETING_ROOM)
    session.add(space)
    session.flush()
    return space


def make_booking(space: Space, start: datetime, end: datetime, **overrides: object) -> Booking:
    values: dict[str, object] = {
        "property_id": space.property_id,
        "space_id": space.id,
        "customer_name": "Test Customer",
        "start_at": start,
        "end_at": end,
        "status": BookingStatus.CONFIRMED,
    }
    values.update(overrides)
    return Booking(**values)


def test_booking_reference_is_generated(db_session: Session) -> None:
    space = make_space(db_session, make_property(db_session))
    booking = make_booking(space, START, START + HOUR)
    db_session.add(booking)
    db_session.commit()
    db_session.refresh(booking)

    assert re.fullmatch(r"BK-\d{4}-\d{6}", booking.reference)


def test_overlapping_bookings_are_rejected(db_session: Session) -> None:
    space = make_space(db_session, make_property(db_session))
    db_session.add(make_booking(space, START, START + 2 * HOUR))
    db_session.commit()

    db_session.add(make_booking(space, START + HOUR, START + 3 * HOUR))
    with pytest.raises(IntegrityError, match="ex_bookings_space_time"):
        db_session.commit()


def test_pending_booking_also_blocks(db_session: Session) -> None:
    space = make_space(db_session, make_property(db_session))
    db_session.add(make_booking(space, START, START + HOUR, status=BookingStatus.PENDING))
    db_session.commit()

    db_session.add(make_booking(space, START, START + HOUR))
    with pytest.raises(IntegrityError, match="ex_bookings_space_time"):
        db_session.commit()


def test_back_to_back_bookings_are_allowed(db_session: Session) -> None:
    space = make_space(db_session, make_property(db_session))
    db_session.add(make_booking(space, START, START + HOUR))
    db_session.add(make_booking(space, START + HOUR, START + 2 * HOUR))

    db_session.commit()


def test_same_time_on_different_spaces_is_allowed(db_session: Session) -> None:
    prop = make_property(db_session)
    db_session.add(make_booking(make_space(db_session, prop, "Room A"), START, START + HOUR))
    db_session.add(make_booking(make_space(db_session, prop, "Room B"), START, START + HOUR))

    db_session.commit()


def test_cancelling_releases_the_time_range(db_session: Session) -> None:
    space = make_space(db_session, make_property(db_session))
    first = make_booking(space, START, START + HOUR)
    db_session.add(first)
    db_session.commit()

    first.status = BookingStatus.CANCELLED
    db_session.add(make_booking(space, START, START + HOUR))

    db_session.commit()


def test_booking_must_end_after_it_starts(db_session: Session) -> None:
    space = make_space(db_session, make_property(db_session))
    db_session.add(make_booking(space, START, START))

    with pytest.raises(IntegrityError, match="time_range_ordered"):
        db_session.commit()


def test_booking_needs_a_tenant_or_customer(db_session: Session) -> None:
    space = make_space(db_session, make_property(db_session))
    db_session.add(make_booking(space, START, START + HOUR, customer_name=None))

    with pytest.raises(IntegrityError, match="has_customer"):
        db_session.commit()


def test_booking_space_must_belong_to_its_property(db_session: Session) -> None:
    space = make_space(db_session, make_property(db_session, "Tower One"))
    other = make_property(db_session, "Tower Two")
    db_session.add(make_booking(space, START, START + HOUR, property_id=other.id))

    with pytest.raises(IntegrityError, match="fk_bookings_space_id_spaces"):
        db_session.commit()


def test_invalid_status_is_rejected(db_session: Session) -> None:
    space = make_space(db_session, make_property(db_session))
    db_session.add(make_booking(space, START, START + HOUR, status="MAYBE"))

    with pytest.raises(IntegrityError, match="status_valid"):
        db_session.commit()


def test_concurrent_bookings_for_one_slot_yield_exactly_one(db_session: Session) -> None:
    space = make_space(db_session, make_property(db_session))
    db_session.commit()
    space_id, property_id = space.id, space.property_id

    attempts = 8
    barrier = threading.Barrier(attempts)
    outcomes: list[str] = []

    def attempt() -> None:
        session = get_session_factory()()
        try:
            session.connection()  # connect first so the race is over the insert itself
            barrier.wait(timeout=15)
            session.add(
                Booking(
                    property_id=property_id,
                    space_id=space_id,
                    customer_name=f"Racer {uuid.uuid4().hex[:6]}",
                    start_at=START,
                    end_at=START + HOUR,
                    status=BookingStatus.CONFIRMED,
                )
            )
            session.commit()
            outcomes.append("booked")
        except IntegrityError:
            session.rollback()
            outcomes.append("rejected")
        finally:
            session.close()

    threads = [threading.Thread(target=attempt) for _ in range(attempts)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert outcomes.count("booked") == 1
    assert len(outcomes) == attempts
    assert db_session.query(Booking).filter_by(space_id=space_id).count() == 1


def test_overlapping_occupancies_of_a_space_are_rejected(db_session: Session) -> None:
    space = make_space(db_session, make_property(db_session))
    first, second = Tenant(name="Acme Ltd"), Tenant(name="Globex Ltd")
    db_session.add_all([first, second])
    db_session.flush()
    db_session.add(
        TenantOccupancy(
            tenant_id=first.id,
            property_id=space.property_id,
            space_id=space.id,
            start_date=date(2030, 1, 1),
        )
    )
    db_session.commit()

    db_session.add(
        TenantOccupancy(
            tenant_id=second.id,
            property_id=space.property_id,
            space_id=space.id,
            start_date=date(2030, 6, 1),
        )
    )
    with pytest.raises(IntegrityError, match="ex_tenant_occupancies_space_dates"):
        db_session.commit()


def test_occupancy_can_follow_an_ended_one(db_session: Session) -> None:
    space = make_space(db_session, make_property(db_session))
    first, second = Tenant(name="Acme Ltd"), Tenant(name="Globex Ltd")
    db_session.add_all([first, second])
    db_session.flush()
    for tenant, start, end in (
        (first, date(2030, 1, 1), date(2030, 5, 31)),
        (second, date(2030, 6, 1), None),
    ):
        db_session.add(
            TenantOccupancy(
                tenant_id=tenant.id,
                property_id=space.property_id,
                space_id=space.id,
                start_date=start,
                end_date=end,
            )
        )

    db_session.commit()
