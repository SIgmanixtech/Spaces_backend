"""Bookings across nested spaces: a portion and the desks inside it share their time.

Floor 5 (whole floor) > Portion 5A > Desk 1, and Portion 5B beside 5A.
"""

import threading
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.errors import ConflictError
from app.db.session import get_session_factory
from app.models import Booking
from app.models.enums import BookingStatus
from app.services.bookings import BookingService
from tests.test_bookings import BOOKINGS, HOUR, START, booking_body, iso
from tests.test_properties import create_property
from tests.test_resources import V1, post


@pytest.fixture
def floor5(api: TestClient) -> dict[str, dict]:
    prop = create_property(api)
    building = post(api, "buildings", {"property_id": prop["id"], "name": "Tower A"})
    floor = post(api, "floors", {"building_id": building["id"], "name": "Fifth", "level_number": 5})

    def space(name: str, space_type: str, **where: str) -> dict:
        return post(
            api,
            "spaces",
            {"property_id": prop["id"], "name": name, "space_type": space_type, **where},
        )

    root = space("Floor 5", "FLOOR", floor_id=floor["id"])
    portion_a = space("Portion 5A", "PORTION", parent_space_id=root["id"])
    portion_b = space("Portion 5B", "PORTION", parent_space_id=root["id"])
    desk = space("Desk 1", "DESK", parent_space_id=portion_a["id"])
    return {"root": root, "5A": portion_a, "5B": portion_b, "desk": desk}


def availability(api: TestClient, space: dict, start: datetime = START) -> dict:
    response = api.get(
        f"{BOOKINGS}/availability",
        params={"space_id": space["id"], "start_at": iso(start), "end_at": iso(start + HOUR)},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_a_booked_portion_takes_the_desks_inside_it(api: TestClient, floor5: dict) -> None:
    portion = post(api, "bookings", booking_body(floor5["5A"]))

    refused = post(api, "bookings", booking_body(floor5["desk"]), 409)
    assert refused["detail"] == (
        'Part of that time is already booked on "Portion 5A", which contains this space.'
    )
    answer = availability(api, floor5["desk"])
    assert answer["available"] is False
    assert [(c["reference"], c["space_name"]) for c in answer["conflicts"]] == [
        (portion["reference"], "Portion 5A")
    ]

    # Outside that hour, and next door, nothing is in the way.
    post(api, "bookings", booking_body(floor5["desk"], START + HOUR))
    post(api, "bookings", booking_body(floor5["5B"]))


def test_a_booked_desk_keeps_its_portion_and_floor_from_being_booked_whole(
    api: TestClient, floor5: dict
) -> None:
    post(api, "bookings", booking_body(floor5["desk"]))

    for whole in ("5A", "root"):
        refused = post(api, "bookings", booking_body(floor5[whole]), 409)
        assert refused["detail"] == (
            'Part of that time is already booked on "Desk 1", which is inside this space.'
        )
    assert availability(api, floor5["5B"])["available"] is True


def test_cancelling_releases_the_family(api: TestClient, floor5: dict) -> None:
    portion = post(api, "bookings", booking_body(floor5["5A"]))
    post(api, "bookings", booking_body(floor5["desk"]), 409)
    assert api.post(f"{BOOKINGS}/{portion['id']}/cancel", json={}).status_code == 200
    post(api, "bookings", booking_body(floor5["desk"]))


def test_rescheduling_into_a_containers_booking_is_refused(api: TestClient, floor5: dict) -> None:
    post(api, "bookings", booking_body(floor5["5A"], START + 2 * HOUR))
    desk = post(api, "bookings", booking_body(floor5["desk"]))
    moved = api.patch(
        f"{BOOKINGS}/{desk['id']}",
        json={"start_at": iso(START + 2 * HOUR), "end_at": iso(START + 3 * HOUR)},
    )
    assert moved.status_code == 409
    assert "Portion 5A" in moved.json()["detail"]


def test_an_occupied_portion_blocks_bookings_of_its_desks(api: TestClient, floor5: dict) -> None:
    acme = post(api, "tenants", {"name": "Acme Ltd"})
    post(
        api,
        f"tenants/{acme['id']}/occupancies",
        {
            "property_id": floor5["5A"]["property_id"],
            "space_id": floor5["5A"]["id"],
            "start_date": date.today().isoformat(),
        },
    )
    refused = post(api, "bookings", booking_body(floor5["desk"]), 409)
    assert refused["detail"] == "The space is occupied by a tenant during that time."
    # The tenant itself may book a desk inside the portion it occupies.
    post(
        api,
        "bookings",
        booking_body(floor5["desk"], tenant_id=acme["id"], customer_name=None),
    )
    # And the desk shows the portion's occupancy.
    desk = api.get(f"{V1}/spaces/{floor5['desk']['id']}").json()
    assert desk["status"] == "OCCUPIED"


def test_a_closed_portion_closes_its_desks(api: TestClient, floor5: dict) -> None:
    closed = api.patch(
        f"{V1}/spaces/{floor5['5A']['id']}", json={"operational_status": "UNDER_MAINTENANCE"}
    )
    assert closed.status_code == 200, closed.text
    refused = post(api, "bookings", booking_body(floor5["desk"]), 409)
    assert refused["detail"] == 'The space is inside "Portion 5A", which is not open for bookings.'
    assert availability(api, floor5["desk"])["available"] is False
    assert api.get(f"{V1}/spaces/{floor5['desk']['id']}").json()["status"] == "UNAVAILABLE"


def test_statuses_flow_down_but_not_up(api: TestClient, floor5: dict) -> None:
    now = datetime.now(UTC)
    post(
        api, "bookings", booking_body(floor5["5A"], now - timedelta(minutes=1), status="CONFIRMED")
    )

    def status(name: str) -> str:
        return api.get(f"{V1}/spaces/{floor5[name]['id']}").json()["status"]

    assert status("5A") == "BOOKED"
    assert status("desk") == "BOOKED"  # inside a booked portion
    assert status("root") == "AVAILABLE"  # the floor is not booked as a whole
    assert status("5B") == "AVAILABLE"


def test_simultaneous_portion_and_desk_requests_never_both_succeed(
    floor5: dict, db_session: Session
) -> None:
    """Without the property lock each request would see no conflict and both would insert."""
    rounds = 6
    for number in range(rounds):
        start = START + timedelta(days=number + 1)
        barrier = threading.Barrier(2)
        outcomes: list[str] = []

        def attempt(
            space: dict,
            start: datetime = start,
            barrier: threading.Barrier = barrier,
            outcomes: list[str] = outcomes,
        ) -> None:
            session = get_session_factory()()
            try:
                session.connection()
                barrier.wait(timeout=15)
                BookingService(session).create(
                    {
                        "property_id": uuid.UUID(space["property_id"]),
                        "space_id": uuid.UUID(space["id"]),
                        "customer_name": "Racer",
                        "start_at": start,
                        "end_at": start + HOUR,
                        "status": BookingStatus.PENDING,
                    }
                )
                outcomes.append("booked")
            except ConflictError:
                outcomes.append("conflict")
            finally:
                session.close()

        threads = [
            threading.Thread(target=attempt, args=(floor5[name],)) for name in ("5A", "desk")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert sorted(outcomes) == ["booked", "conflict"], number

    assert db_session.query(Booking).count() == rounds
