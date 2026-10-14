"""The schema and API as they are today (revision 0001), for comparison with the proposal.

The ``test_gap_*`` tests assert behaviour the audit called a defect. They pass
while the defect exists and are meant to be inverted when it is fixed.
"""

import threading
from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.db.session import get_session_factory
from app.models import Booking
from app.models.enums import BookingStatus
from tests.test_bookings import START, book, room  # noqa: F401  (room is a fixture)
from tests.test_resources import post
from tests.test_schema_constraints import HOUR, make_property, make_space


def test_existing_exclusion_constraint_is_safe_under_raw_concurrency(db_session: Session) -> None:
    """No row lock first, unlike the booking service: the constraint is the only guard.

    Safety must hold in every round. Losers are normally rejected with 23P01,
    but concurrent conflicting inserts can also deadlock (40P01); the count is
    printed, not asserted, because it depends on timing.
    """
    space = make_space(db_session, make_property(db_session))
    db_session.commit()
    space_id, property_id = space.id, space.property_id
    attempts, rounds = 8, 30
    seen: dict[str, int] = {}

    for round_number in range(rounds):
        start = START + timedelta(days=round_number)
        barrier = threading.Barrier(attempts)
        outcomes: list[str] = []

        def attempt(start=start, barrier=barrier, outcomes=outcomes) -> None:  # type: ignore[no-untyped-def]
            session = get_session_factory()()
            try:
                session.connection()
                barrier.wait(timeout=15)
                session.add(
                    Booking(
                        property_id=property_id,
                        space_id=space_id,
                        customer_name="Racer",
                        start_at=start,
                        end_at=start + HOUR,
                        status=BookingStatus.CONFIRMED,
                    )
                )
                session.commit()
                outcomes.append("booked")
            except DBAPIError as exc:
                session.rollback()
                outcomes.append(exc.orig.sqlstate)
            finally:
                session.close()

        threads = [threading.Thread(target=attempt) for _ in range(attempts)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        assert len(outcomes) == attempts
        assert outcomes.count("booked") == 1, outcomes
        assert set(outcomes) <= {"booked", "23P01", "40P01"}, outcomes
        for result in outcomes:
            seen[result] = seen.get(result, 0) + 1

    assert db_session.scalar(select(func.count()).select_from(Booking)) == rounds
    print(f"existing ex_bookings_space_time, {rounds} rounds x {attempts}: {seen}")


def test_gap_an_occupancy_can_be_created_over_an_existing_booking(
    api: TestClient,
    room: dict,  # noqa: F811
) -> None:
    booking = book(api, room, status="CONFIRMED")
    tenant = post(api, "tenants", {"name": "Acme Ltd"})
    occupancy = post(
        api,
        f"tenants/{tenant['id']}/occupancies",
        {
            "property_id": room["property_id"],
            "space_id": room["id"],
            "start_date": date.today().isoformat(),
        },
    )
    # Both now hold the same room for the booking's hour.
    assert occupancy["space_id"] == booking["space_id"]
    assert api.get(f"/api/v1/bookings/{booking['id']}").json()["status"] == "CONFIRMED"


def test_gap_two_tenants_can_occupy_the_whole_property_at_once(
    api: TestClient,
    room: dict,  # noqa: F811
) -> None:
    body = {"property_id": room["property_id"], "start_date": date.today().isoformat()}
    for name in ("Acme Ltd", "Globex Ltd"):
        tenant = post(api, "tenants", {"name": name})
        assert post(api, f"tenants/{tenant['id']}/occupancies", body)["space_id"] is None


def test_gap_whole_property_and_single_space_occupancies_can_overlap(
    api: TestClient,
    room: dict,  # noqa: F811
) -> None:
    today = date.today().isoformat()
    whole = post(api, "tenants", {"name": "Acme Ltd"})
    post(
        api,
        f"tenants/{whole['id']}/occupancies",
        {"property_id": room["property_id"], "start_date": today},
    )
    single = post(api, "tenants", {"name": "Globex Ltd"})
    occupancy = post(
        api,
        f"tenants/{single['id']}/occupancies",
        {"property_id": room["property_id"], "space_id": room["id"], "start_date": today},
    )
    assert occupancy["space_id"] == room["id"]
