import re
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
from tests.test_properties import create_property
from tests.test_resources import V1, post

BOOKINGS = f"{V1}/bookings"
# A fixed slot comfortably in the future, on the hour.
START = (datetime.now(UTC) + timedelta(days=30)).replace(minute=0, second=0, microsecond=0)
HOUR = timedelta(hours=1)


def iso(moment: datetime) -> str:
    return moment.isoformat()


@pytest.fixture
def room(api: TestClient) -> dict:
    prop = create_property(api)
    return post(
        api,
        "spaces",
        {"property_id": prop["id"], "name": "Boardroom", "space_type": "MEETING_ROOM"},
    )


def booking_body(room: dict, start: datetime = START, hours: float = 1, **extra: object) -> dict:
    body: dict[str, object] = {
        "property_id": room["property_id"],
        "space_id": room["id"],
        "customer_name": "Priya Nair",
        "start_at": iso(start),
        "end_at": iso(start + timedelta(hours=hours)),
    }
    body.update(extra)
    return body


def book(api: TestClient, room: dict, start: datetime = START, **extra: object) -> dict:
    return post(api, "bookings", booking_body(room, start, **extra))


# --- creation ------------------------------------------------------------------


def test_create_booking(api: TestClient, room: dict) -> None:
    booking = book(api, room, customer_email="priya@example.com")

    assert re.fullmatch(r"BK-\d{4}-\d{6}", booking["reference"])
    assert booking["status"] == "PENDING"
    assert booking["space_name"] == "Boardroom"
    assert booking["property_name"] == "Sigma Tower"
    assert booking["tenant_name"] is None
    assert datetime.fromisoformat(booking["start_at"]) == START
    assert booking["start_at"].endswith(("Z", "+00:00"))

    assert api.get(f"{BOOKINGS}/{booking['id']}").json() == booking


def test_times_are_normalised_to_utc(api: TestClient, room: dict) -> None:
    ist = START.astimezone(tz=__import__("zoneinfo").ZoneInfo("Asia/Kolkata"))

    booking = book(api, room, start=ist)

    assert datetime.fromisoformat(booking["start_at"]) == START
    assert booking["start_at"].endswith(("Z", "+00:00"))


def test_dates_beyond_the_supported_years_are_rejected(api: TestClient, room: dict) -> None:
    edge = {"start_at": "9999-12-31T23:00:00Z", "end_at": "9999-12-31T23:59:59Z"}

    created = api.post(BOOKINGS, json=booking_body(room, **edge))
    checked = api.get(f"{BOOKINGS}/availability", params={"space_id": room["id"], **edge})

    for response in (created, checked):
        assert response.status_code == 400
        assert "between 2000 and 2200" in response.json()["detail"]


def test_booking_can_be_created_confirmed_for_a_tenant(api: TestClient, room: dict) -> None:
    tenant = post(api, "tenants", {"name": "Acme Ltd"})

    booking = post(
        api,
        "bookings",
        booking_body(room, tenant_id=tenant["id"], customer_name=None, status="CONFIRMED"),
    )

    assert booking["status"] == "CONFIRMED"
    assert booking["tenant_name"] == "Acme Ltd"
    assert api.get(f"{V1}/tenants/{tenant['id']}").json()["booking_refs"] == [booking["reference"]]
    assert api.get(f"{V1}/tenants/{tenant['id']}").json()["occupancy_status"] == "RESERVED"


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"end_at": iso(START)}, "body"),
        ({"end_at": iso(START - HOUR)}, "body"),
        ({"start_at": "2031-01-01T09:00:00"}, "body.start_at"),
        ({"customer_name": None}, "body"),
        ({"status": "COMPLETED"}, "body.status"),
        ({"status": "CANCELLED"}, "body.status"),
        ({"reference": "BK-1"}, "body.reference"),
        ({"customer_email": "not-an-email"}, "body.customer_email"),
    ],
    ids=[
        "zero-length",
        "ends-before-start",
        "no-utc-offset",
        "no-customer",
        "born-completed",
        "born-cancelled",
        "client-reference",
        "bad-email",
    ],
)
def test_invalid_booking_requests_are_rejected(
    api: TestClient, room: dict, change: dict, field: str
) -> None:
    response = api.post(BOOKINGS, json=booking_body(room, **change))

    assert response.status_code == 422
    assert response.json()["errors"][0]["field"] == field


def test_booking_cannot_start_in_the_past_or_run_too_long(api: TestClient, room: dict) -> None:
    past = api.post(BOOKINGS, json=booking_body(room, datetime.now(UTC) - timedelta(days=1)))
    assert past.status_code == 400
    assert "in the past" in past.json()["detail"]

    endless = api.post(BOOKINGS, json=booking_body(room, hours=24 * 400))
    assert endless.status_code == 400


def test_booking_needs_existing_matching_open_targets(api: TestClient, room: dict) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    other = create_property(api, name="Tower Two")

    assert post(api, "bookings", booking_body(room, space_id=missing), 404)["detail"] == (
        "Space not found."
    )
    assert post(api, "bookings", booking_body(room, property_id=missing), 404)["detail"] == (
        "Property not found."
    )
    assert post(api, "bookings", booking_body(room, tenant_id=missing), 404)["detail"] == (
        "Tenant not found."
    )
    wrong_property = post(api, "bookings", booking_body(room, property_id=other["id"]), 400)
    assert "does not belong" in wrong_property["detail"]

    api.patch(f"{V1}/spaces/{room['id']}", json={"operational_status": "UNDER_MAINTENANCE"})
    assert "space is not open" in post(api, "bookings", booking_body(room), 409)["detail"]
    api.patch(f"{V1}/spaces/{room['id']}", json={"operational_status": "ACTIVE"})
    api.patch(f"{V1}/properties/{room['property_id']}", json={"status": "INACTIVE"})
    assert "property is not open" in post(api, "bookings", booking_body(room), 409)["detail"]

    assert api.get(BOOKINGS).json()["total"] == 0


# --- overlap and availability -----------------------------------------------------


def test_overlapping_booking_is_rejected(api: TestClient, room: dict) -> None:
    book(api, room, hours=2)

    for start, hours in ((START + HOUR, 2), (START - HOUR, 2), (START, 2), (START + HOUR / 2, 0.5)):
        clash = api.post(BOOKINGS, json=booking_body(room, start, hours))
        assert clash.status_code == 409
        assert clash.json()["detail"] == "The space is already booked for part of that time."

    assert api.get(BOOKINGS).json()["total"] == 1


def test_adjacent_bookings_and_other_spaces_are_fine(api: TestClient, room: dict) -> None:
    other_room = post(
        api, "spaces", {"property_id": room["property_id"], "name": "Huddle", "space_type": "CABIN"}
    )

    book(api, room)
    book(api, room, START + HOUR)
    book(api, room, START - HOUR)
    book(api, other_room)

    assert api.get(BOOKINGS).json()["total"] == 4


def test_availability_reflects_saved_bookings(api: TestClient, room: dict) -> None:
    def check(start: datetime, **extra: object) -> dict:
        params = {
            "space_id": room["id"],
            "start_at": iso(start),
            "end_at": iso(start + HOUR),
            **extra,
        }
        response = api.get(f"{BOOKINGS}/availability", params=params)
        assert response.status_code == 200, response.text
        return response.json()

    assert check(START) == {
        "available": True,
        "message": "The space is available.",
        "conflicts": [],
    }

    booking = book(api, room)

    taken = check(START)
    assert taken["available"] is False
    assert [conflict["reference"] for conflict in taken["conflicts"]] == [booking["reference"]]
    assert check(START + HOUR)["available"] is True
    assert check(START, exclude_booking_id=booking["id"])["available"] is True

    api.post(f"{BOOKINGS}/{booking['id']}/cancel")
    assert check(START)["available"] is True

    api.patch(f"{V1}/spaces/{room['id']}", json={"operational_status": "INACTIVE"})
    closed = check(START)
    assert closed["available"] is False
    assert closed["message"] == "The space is not open for bookings."


def test_availability_validates_its_input(api: TestClient, room: dict) -> None:
    backwards = api.get(
        f"{BOOKINGS}/availability",
        params={"space_id": room["id"], "start_at": iso(START), "end_at": iso(START - HOUR)},
    )
    assert backwards.status_code == 400
    assert api.get(f"{BOOKINGS}/availability").status_code == 422


def test_tenant_occupancy_blocks_other_peoples_bookings(api: TestClient, room: dict) -> None:
    acme = post(api, "tenants", {"name": "Acme Ltd"})
    post(
        api,
        f"tenants/{acme['id']}/occupancies",
        {
            "property_id": room["property_id"],
            "space_id": room["id"],
            "start_date": date.today().isoformat(),
        },
    )

    blocked = post(api, "bookings", booking_body(room), 409)
    assert blocked["detail"] == "The space is occupied by a tenant during that time."

    own = post(api, "bookings", booking_body(room, tenant_id=acme["id"], customer_name=None))
    assert own["tenant_name"] == "Acme Ltd"


# --- status transitions ----------------------------------------------------------


def test_cancelling_frees_the_slot_and_keeps_the_record(api: TestClient, room: dict) -> None:
    booking = book(api, room)

    cancelled = api.post(f"{BOOKINGS}/{booking['id']}/cancel", json={"reason": "Client request"})

    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"
    assert cancelled.json()["cancellation_reason"] == "Client request"
    assert cancelled.json()["cancelled_at"] is not None

    replacement = book(api, room)
    assert replacement["reference"] != booking["reference"]
    assert api.get(BOOKINGS).json()["total"] == 2


def test_delete_cancels_rather_than_deletes(api: TestClient, room: dict) -> None:
    booking = book(api, room)

    assert api.delete(f"{BOOKINGS}/{booking['id']}").status_code == 204

    assert api.get(f"{BOOKINGS}/{booking['id']}").json()["status"] == "CANCELLED"
    assert api.delete(f"{BOOKINGS}/{booking['id']}").status_code == 409


def test_status_transitions(api: TestClient, room: dict) -> None:
    booking = book(api, room)
    url = f"{BOOKINGS}/{booking['id']}"

    too_early = api.post(f"{url}/complete")
    assert too_early.status_code == 409
    assert too_early.json()["detail"] == "A pending booking cannot be completed."

    assert api.post(f"{url}/confirm").json()["status"] == "CONFIRMED"
    assert api.post(f"{url}/confirm").status_code == 409

    not_started = api.post(f"{url}/complete")
    assert not_started.status_code == 409
    assert "before it starts" in not_started.json()["detail"]
    assert api.get(url).json()["status"] == "CONFIRMED"

    assert api.post(f"{url}/cancel").json()["status"] == "CANCELLED"
    for action in ("confirm", "complete", "cancel"):
        assert api.post(f"{url}/{action}").status_code == 409
    assert api.patch(url, json={"notes": "late"}).status_code == 409


def test_in_progress_booking_can_be_completed_and_still_holds_its_slot(
    api: TestClient, room: dict
) -> None:
    now = datetime.now(UTC)
    booking = book(api, room, now - timedelta(minutes=1), status="CONFIRMED")

    assert api.get(f"{V1}/spaces/{room['id']}").json()["status"] == "BOOKED"

    completed = api.post(f"{BOOKINGS}/{booking['id']}/complete")
    assert completed.json()["status"] == "COMPLETED"
    assert (
        api.post(BOOKINGS, json=booking_body(room, now + timedelta(minutes=5))).status_code == 409
    )
    assert api.get(f"{V1}/spaces/{room['id']}").json()["status"] == "AVAILABLE"


def test_open_booking_marks_space_and_property_reserved(api: TestClient, room: dict) -> None:
    booking = book(api, room)

    assert api.get(f"{V1}/spaces/{room['id']}").json()["status"] == "RESERVED"
    prop = api.get(f"{V1}/properties/{room['property_id']}").json()
    assert prop["occupancy_status"] == "RESERVED"
    assert api.delete(f"{V1}/spaces/{room['id']}").status_code == 409
    assert api.delete(f"{V1}/properties/{room['property_id']}").status_code == 409

    api.post(f"{BOOKINGS}/{booking['id']}/cancel")
    assert api.get(f"{V1}/spaces/{room['id']}").json()["status"] == "AVAILABLE"
    assert api.get(f"{V1}/properties/{room['property_id']}").json()["occupancy_status"] == "VACANT"


# --- rescheduling ---------------------------------------------------------------


def test_reschedule_checks_for_conflicts(api: TestClient, room: dict) -> None:
    first = book(api, room)
    second = book(api, room, START + 2 * HOUR)
    url = f"{BOOKINGS}/{second['id']}"

    clash = api.patch(
        url, json={"start_at": iso(START + HOUR / 2), "end_at": iso(START + 2 * HOUR)}
    )
    assert clash.status_code == 409
    assert datetime.fromisoformat(api.get(url).json()["start_at"]) == START + 2 * HOUR

    # Extending a booking over its own old time is not a conflict with itself.
    extended = api.patch(url, json={"end_at": iso(START + 4 * HOUR)})
    assert extended.status_code == 200
    moved = api.patch(url, json={"start_at": iso(START + HOUR), "end_at": iso(START + 2 * HOUR)})
    assert moved.status_code == 200
    assert moved.json()["reference"] == second["reference"]

    backwards = api.patch(url, json={"end_at": iso(START)})
    assert backwards.status_code == 400
    renamed = api.patch(f"{BOOKINGS}/{first['id']}", json={"customer_name": "P. Nair"})
    assert renamed.json()["customer_name"] == "P. Nair"
    assert api.patch(url, json={"space_id": room["id"]}).status_code == 422


# --- listing ------------------------------------------------------------------


def test_list_filters_search_and_window(api: TestClient, room: dict) -> None:
    first = book(api, room, customer_name="Priya Nair")
    second = book(api, room, START + 24 * HOUR, customer_name="Arjun Rao")
    api.post(f"{BOOKINGS}/{second['id']}/confirm")

    assert api.get(BOOKINGS, params={"status": "CONFIRMED"}).json()["total"] == 1
    assert api.get(BOOKINGS, params={"search": "arjun"}).json()["items"][0]["id"] == second["id"]
    assert api.get(BOOKINGS, params={"search": first["reference"]}).json()["total"] == 1
    assert api.get(BOOKINGS, params={"space_id": room["id"]}).json()["total"] == 2

    newest_first = api.get(BOOKINGS).json()["items"]
    assert [item["id"] for item in newest_first] == [second["id"], first["id"]]
    oldest_first = api.get(BOOKINGS, params={"sort": "start_at"}).json()["items"]
    assert [item["id"] for item in oldest_first] == [first["id"], second["id"]]

    window = api.get(
        BOOKINGS, params={"window_start": iso(START - HOUR), "window_end": iso(START + 2 * HOUR)}
    ).json()
    assert [item["id"] for item in window["items"]] == [first["id"]]


# --- concurrency ----------------------------------------------------------------


def test_concurrent_requests_for_one_slot_create_exactly_one_booking(
    api: TestClient, room: dict, db_session: Session
) -> None:
    attempts = 10
    barrier = threading.Barrier(attempts)
    outcomes: list[str] = []
    values = {
        "property_id": uuid.UUID(room["property_id"]),
        "space_id": uuid.UUID(room["id"]),
        "start_at": START,
        "end_at": START + HOUR,
        "status": BookingStatus.CONFIRMED,
    }

    def attempt(number: int) -> None:
        session = get_session_factory()()
        try:
            session.connection()
            barrier.wait(timeout=15)
            BookingService(session).create({**values, "customer_name": f"Racer {number}"})
            outcomes.append("booked")
        except ConflictError:
            outcomes.append("conflict")
        finally:
            session.close()

    threads = [threading.Thread(target=attempt, args=(number,)) for number in range(attempts)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert sorted(outcomes) == ["booked"] + ["conflict"] * (attempts - 1)
    assert db_session.query(Booking).filter_by(space_id=room["id"]).count() == 1


def test_concurrent_staggered_requests_never_overlap(
    api: TestClient, room: dict, db_session: Session
) -> None:
    """Different but overlapping slots, so a naive check-then-insert would double-book."""
    attempts = 10
    barrier = threading.Barrier(attempts)

    def attempt(number: int) -> None:
        session = get_session_factory()()
        start = START + timedelta(minutes=20 * number)
        try:
            session.connection()
            barrier.wait(timeout=15)
            BookingService(session).create(
                {
                    "property_id": uuid.UUID(room["property_id"]),
                    "space_id": uuid.UUID(room["id"]),
                    "customer_name": f"Racer {number}",
                    "start_at": start,
                    "end_at": start + HOUR,
                    "status": BookingStatus.PENDING,
                }
            )
        except ConflictError:
            pass
        finally:
            session.close()

    threads = [threading.Thread(target=attempt, args=(number,)) for number in range(attempts)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    saved = db_session.query(Booking).order_by(Booking.start_at).all()
    assert saved, "at least one booking should succeed"
    for earlier, later in zip(saved, saved[1:], strict=False):
        assert earlier.end_at <= later.start_at
