"""Hierarchical bookings and contracts through the allocation engine.

Floor 5 (whole floor) holds Portion 5A and Portion 5B. 5A holds a cabin, a desk,
a meeting room and a conference hall; 5B holds two cabins, a desk and a
conference hall. Scenarios follow the brief: A whole-floor contract, B portion
contract, C individual bookings, D conflicts in both directions, E expiry,
cancellation, termination and extension, F concurrency.
"""

import threading
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError
from app.db.session import get_session_factory
from app.models import Allocation, AllocationClaim, Booking, TenantOccupancy
from app.models.enums import BookingStatus
from app.services.bookings import BookingService
from app.services.contracts import ContractService
from app.services.spaces import SpaceService
from tests.test_bookings import BOOKINGS, HOUR, START, booking_body, iso
from tests.test_properties import create_property
from tests.test_resources import V1, post

CONTRACTS = f"{V1}/contracts"
ZONE = ZoneInfo(get_settings().business_timezone)
DAY = START.astimezone(ZONE).date()  # the local day of the standard booking slot


def day(offset: int) -> date:
    return DAY + timedelta(days=offset)


def at(offset_days: int, hour: int = 10) -> datetime:
    """A whole hour on a day relative to DAY, in the business time zone, as UTC."""
    return (
        datetime.combine(day(offset_days), datetime.min.time(), tzinfo=ZONE)
        .replace(hour=hour)
        .astimezone(UTC)
    )


@pytest.fixture
def floor5(api: TestClient) -> dict[str, dict]:
    prop = create_property(api, name="Sigma Hub")
    building = post(api, "buildings", {"property_id": prop["id"], "name": "Building A"})
    floor = post(
        api, "floors", {"building_id": building["id"], "name": "Floor 5", "level_number": 5}
    )
    spaces: dict[str, dict] = {"property": prop}

    def space(key: str, name: str, space_type: str, parent: str | None = None, **extra: object):
        where = {"parent_space_id": spaces[parent]["id"]} if parent else {"floor_id": floor["id"]}
        spaces[key] = post(
            api,
            "spaces",
            {"property_id": prop["id"], "name": name, "space_type": space_type, **where, **extra},
        )

    space("floor", "Floor 5", "FLOOR", area_sqft=3300)
    space("5A", "Portion 5A", "PORTION", "floor", area_sqft=1650)
    space("5B", "Portion 5B", "PORTION", "floor", area_sqft=1650)
    space("A-cabin", "5A-C01", "CABIN", "5A")
    space("A-desk", "5A-D01", "DESK", "5A")
    space("A-meeting", "5A-M01", "MEETING_ROOM", "5A")
    space("A-hall", "5A-CF01", "CONFERENCE_ROOM", "5A")
    space("B-cabin", "5B-C01", "CABIN", "5B")
    space("B-cabin2", "5B-C02", "CABIN", "5B")
    space("B-desk", "5B-D01", "DESK", "5B")
    space("B-hall", "5B-CF01", "CONFERENCE_ROOM", "5B")
    spaces["tenant"] = post(api, "tenants", {"name": "Acme Ltd"})
    spaces["other"] = post(api, "tenants", {"name": "Globex"})
    return spaces


def contract(
    api: TestClient,
    space: dict | None,
    start: date,
    end: date | None,
    expected: int = 201,
    tenant: dict | None = None,
    activate: bool = True,
    property_id: str | None = None,
) -> dict:
    body: dict[str, object] = {
        "tenant_id": tenant["id"] if tenant else post(api, "tenants", {"name": "Tenant"})["id"],
        "property_id": property_id or (space["property_id"] if space else None),
        "start_date": start.isoformat(),
        "end_date": end.isoformat() if end else None,
        "activate": activate,
    }
    if space is not None:
        body["space_id"] = space["id"]
    response = api.post(CONTRACTS, json=body)
    assert response.status_code == expected, response.text
    return response.json()


def book(api: TestClient, space: dict, start: datetime = START, expected: int = 201, **extra):
    response = api.post(BOOKINGS, json=booking_body(space, start, **extra))
    assert response.status_code == expected, response.text
    return response.json()


def available(api: TestClient, space: dict, start: datetime = START) -> dict:
    response = api.get(
        f"{BOOKINGS}/availability",
        params={"space_id": space["id"], "start_at": iso(start), "end_at": iso(start + HOUR)},
    )
    assert response.status_code == 200, response.text
    return response.json()


A_CHILDREN = ("A-cabin", "A-desk", "A-meeting", "A-hall")
B_SPACES = ("5B", "B-cabin", "B-cabin2", "B-desk", "B-hall")


# --- Scenario A: whole-floor contract -----------------------------------------------


def test_a_whole_floor_contract_blocks_every_space_on_the_floor(
    api: TestClient, floor5: dict
) -> None:
    held = contract(api, floor5["floor"], day(-1), day(1), tenant=floor5["tenant"])
    assert held["status"] == "ACTIVE" and held["reference"].startswith("CT-")

    for key in ("5A", "5B", *A_CHILDREN, "B-cabin", "B-desk", "B-hall"):
        answer = available(api, floor5[key])
        assert answer["available"] is False, key
        refused = book(api, floor5[key], expected=409)
        assert refused["conflicts"][0]["reference"] == held["reference"], key
        assert refused["conflicts"][0]["relation"] == "contains", key
        assert refused["conflicts"][0]["space_name"] == "Floor 5", key

    # One contract record protects the floor; nothing was written per child space.
    scope = api.get(f"{CONTRACTS}/{held['id']}/scope").json()
    assert {s["name"] for s in scope["spaces"]} >= {"Floor 5", "Portion 5A", "5B-CF01"}

    # Outside its dates the floor's spaces are free again.
    book(api, floor5["A-desk"], at(2))
    book(api, floor5["B-hall"], at(-2))


def test_a_a_floor_contract_running_today_shows_its_spaces_occupied(
    api: TestClient, floor5: dict
) -> None:
    today = datetime.now(UTC).astimezone(ZONE).date()
    contract(api, floor5["floor"], today, today + timedelta(days=30), tenant=floor5["tenant"])
    for key in ("floor", "5A", "A-desk", "B-hall"):
        assert api.get(f"{V1}/spaces/{floor5[key]['id']}").json()["status"] == "OCCUPIED", key


# --- Scenario B: portion contract -----------------------------------------------------


def test_b_a_portion_contract_blocks_its_spaces_but_not_the_other_portion(
    api: TestClient, floor5: dict
) -> None:
    held = contract(api, floor5["5A"], day(-1), day(1), tenant=floor5["tenant"])

    for key in A_CHILDREN:
        refused = book(api, floor5[key], expected=409)
        assert refused["conflicts"][0]["reference"] == held["reference"], key
    for key in B_SPACES:
        assert available(api, floor5[key])["available"] is True, key
    book(api, floor5["B-cabin"])

    # Competing contracts inside 5A, on 5A itself, or on the whole floor are refused.
    contract(api, floor5["A-cabin"], day(0), day(0), 409, tenant=floor5["other"])
    contract(api, floor5["5A"], day(1), day(3), 409, tenant=floor5["other"])
    contract(api, floor5["floor"], day(0), day(0), 409, tenant=floor5["other"])
    # 5B and a space in it stay contractable (the cabin is booked, so use the desk).
    contract(api, floor5["B-desk"], day(0), day(1), tenant=floor5["other"])


# --- Scenario C: individual bookings --------------------------------------------------


def test_c_booking_one_cabin_blocks_only_that_cabin(api: TestClient, floor5: dict) -> None:
    first = book(api, floor5["B-cabin"])
    refused = book(api, floor5["B-cabin"], START + HOUR / 2, expected=409)
    assert refused["detail"] == "The space is already booked for part of that time."

    for key in ("B-cabin2", "B-desk", "B-hall", "A-cabin", "A-hall"):
        book(api, floor5[key])
    book(api, floor5["B-cabin"], START + HOUR)  # touching, not overlapping
    assert first["status"] == "PENDING"


# --- Scenario D: conflicts in both directions -----------------------------------------


def test_d_a_child_booking_blocks_a_parent_contract(api: TestClient, floor5: dict) -> None:
    booking = book(api, floor5["B-desk"])
    for parent in ("5B", "floor"):
        refused = contract(api, floor5[parent], day(0), day(0), 409, tenant=floor5["tenant"])
        assert refused["conflicts"][0]["reference"] == booking["reference"]
        assert refused["conflicts"][0]["relation"] == "is inside"
    # The pending contract is kept, holding nothing: nothing was saved by the 409.
    assert api.get(CONTRACTS, params={"space_id": floor5["5B"]["id"]}).json()["total"] == 0


def test_d_a_parent_contract_blocks_a_child_booking_and_a_child_contract(
    api: TestClient, floor5: dict
) -> None:
    held = contract(api, floor5["5B"], day(0), day(0), tenant=floor5["tenant"])
    refused = book(api, floor5["B-desk"], expected=409)
    assert refused["conflicts"] == [
        {
            **refused["conflicts"][0],
            "kind": "CONTRACT",
            "reference": held["reference"],
            "relation": "contains",
            "space_name": "Portion 5B",
        }
    ]
    contract(api, floor5["B-desk"], day(0), day(0), 409, tenant=floor5["other"])


def test_d_the_contract_holder_may_still_book_inside_its_own_contract(
    api: TestClient, floor5: dict, db_session: Session
) -> None:
    contract(api, floor5["5A"], day(-1), day(1), tenant=floor5["tenant"])
    own = book(api, floor5["A-hall"], tenant_id=floor5["tenant"]["id"], customer_name=None)
    # Inside its own contract a booking needs no hold of its own.
    assert db_session.scalar(select(Allocation).where(Allocation.booking_id == own["id"])) is None
    book(api, floor5["A-hall"], START + HOUR / 2, 409, tenant_id=floor5["tenant"]["id"])


# --- Scenario E: expiry, cancellation, termination, extension ---------------------------


def test_e_cancelling_releases_the_space_and_keeps_the_record(
    api: TestClient, floor5: dict, db_session: Session
) -> None:
    held = contract(api, floor5["5A"], day(-1), day(1), tenant=floor5["tenant"])
    book(api, floor5["A-desk"], expected=409)

    cancelled = api.post(f"{CONTRACTS}/{held['id']}/cancel", json={"reason": "Deal fell through"})
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "CANCELLED"
    book(api, floor5["A-desk"])

    # History is kept: the contract, its released allocation and every step.
    allocation = db_session.scalar(select(Allocation).where(Allocation.contract_id == held["id"]))
    assert allocation is not None and not allocation.is_active
    assert allocation.release_reason == "CONTRACT_CANCELLED"
    actions = [e["action"] for e in api.get(f"{CONTRACTS}/{held['id']}/history").json()]
    assert actions == ["created", "activated", "cancelled"]
    assert api.post(f"{CONTRACTS}/{held['id']}/cancel").status_code == 409


def test_e_a_started_contract_is_terminated_not_cancelled(api: TestClient, floor5: dict) -> None:
    today = datetime.now(UTC).astimezone(ZONE).date()
    held = contract(api, floor5["5B"], today, None, tenant=floor5["tenant"])
    refused = api.post(f"{CONTRACTS}/{held['id']}/cancel")
    assert refused.status_code == 409 and "Terminate" in refused.json()["detail"]

    # Terminated with effect after DAY: DAY stays held, the day after is free.
    ended = api.post(
        f"{CONTRACTS}/{held['id']}/terminate",
        json={"termination_date": day(0).isoformat(), "reason": "Moved out"},
    )
    assert ended.status_code == 200, ended.text
    body = ended.json()
    assert (body["status"], body["end_date"], body["original_end_date"]) == (
        "TERMINATED",
        day(0).isoformat(),
        None,
    )
    book(api, floor5["B-desk"], at(0, 23), 409)  # the last held day, late evening
    book(api, floor5["B-desk"], at(1, 0))  # midnight after the last day


def test_e_an_expired_contract_no_longer_holds(api: TestClient, floor5: dict) -> None:
    today = datetime.now(UTC).astimezone(ZONE).date()
    past = contract(
        api,
        floor5["5A"],
        today - timedelta(days=10),
        today - timedelta(days=1),
        tenant=floor5["tenant"],
    )
    assert api.get(f"{CONTRACTS}/{past['id']}").json()["status"] == "EXPIRED"
    assert api.get(f"{V1}/spaces/{floor5['A-desk']['id']}").json()["status"] == "AVAILABLE"
    book(api, floor5["A-desk"])
    listed = api.get(CONTRACTS, params={"status": "EXPIRED"}).json()
    assert [c["id"] for c in listed["items"]] == [past["id"]]


def test_e_extensions_are_checked_against_the_hierarchy(api: TestClient, floor5: dict) -> None:
    held = contract(api, floor5["5A"], day(-3), day(-1), tenant=floor5["tenant"])
    blocker = book(api, floor5["A-desk"], at(2))

    refused = api.post(f"{CONTRACTS}/{held['id']}/extend", json={"end_date": day(5).isoformat()})
    assert refused.status_code == 409
    assert refused.json()["conflicts"][0]["reference"] == blocker["reference"]
    assert api.get(f"{CONTRACTS}/{held['id']}").json()["end_date"] == day(-1).isoformat()

    extended = api.post(f"{CONTRACTS}/{held['id']}/extend", json={"end_date": day(1).isoformat()})
    assert extended.status_code == 200, extended.text
    book(api, floor5["A-desk"], at(0), 409)
    history = [e["action"] for e in api.get(f"{CONTRACTS}/{held['id']}/history").json()]
    assert history[-1] == "extended"


def test_e_pending_contracts_hold_nothing_until_activated(api: TestClient, floor5: dict) -> None:
    draft = contract(api, floor5["5B"], day(0), day(0), tenant=floor5["tenant"], activate=False)
    assert draft["status"] == "PENDING" and draft["holds_inventory"] is False
    booking = book(api, floor5["B-cabin"])
    refused = api.post(f"{CONTRACTS}/{draft['id']}/activate")
    assert refused.status_code == 409
    assert refused.json()["conflicts"][0]["reference"] == booking["reference"]
    assert api.get(f"{CONTRACTS}/{draft['id']}").json()["status"] == "PENDING"
    api.post(f"{BOOKINGS}/{booking['id']}/cancel", json={})
    assert api.post(f"{CONTRACTS}/{draft['id']}/activate").json()["status"] == "ACTIVE"


def test_e_rescheduling_a_booking_is_checked_too(api: TestClient, floor5: dict) -> None:
    contract(api, floor5["5A"], day(3), day(4), tenant=floor5["tenant"])
    booking = book(api, floor5["A-desk"])
    moved = api.patch(
        f"{BOOKINGS}/{booking['id']}", json={"start_at": iso(at(3)), "end_at": iso(at(3) + HOUR)}
    )
    assert moved.status_code == 409


def test_whole_property_contracts_hold_every_space(api: TestClient, floor5: dict) -> None:
    prop_id = floor5["property"]["id"]
    booking = book(api, floor5["B-hall"])
    refused = contract(api, None, day(0), day(0), 409, floor5["tenant"], property_id=prop_id)
    assert refused["conflicts"][0]["reference"] == booking["reference"]
    contract(api, None, day(5), day(6), tenant=floor5["tenant"], property_id=prop_id)
    book(api, floor5["A-desk"], at(5), 409)
    contract(api, floor5["5A"], day(6), day(7), 409, tenant=floor5["other"])


# --- moves and archiving ------------------------------------------------------------------


def test_spaces_with_holds_cannot_be_moved_or_archived(api: TestClient, floor5: dict) -> None:
    contract(api, floor5["A-cabin"], day(0), day(0), tenant=floor5["tenant"])
    moved = api.post(
        f"{V1}/spaces/{floor5['A-cabin']['id']}/move", json={"parent_space_id": floor5["5B"]["id"]}
    )
    assert moved.status_code == 409
    assert api.delete(f"{V1}/spaces/{floor5['A-cabin']['id']}").status_code == 409


def test_assigning_a_held_space_into_a_contracted_portion_is_refused(
    api: TestClient, floor5: dict
) -> None:
    loose = post(
        api,
        "spaces",
        {"property_id": floor5["property"]["id"], "name": "Pop-up", "space_type": "CABIN"},
    )
    book(api, loose)
    contract(api, floor5["5A"], day(-1), day(1), tenant=floor5["tenant"])
    refused = api.post(
        f"{V1}/spaces/{loose['id']}/move", json={"parent_space_id": floor5["5A"]["id"]}
    )
    assert refused.status_code == 409, refused.text
    # Into the free portion it goes, and its booking now also holds 5B.
    moved = api.post(
        f"{V1}/spaces/{loose['id']}/move", json={"parent_space_id": floor5["5B"]["id"]}
    )
    assert moved.status_code == 200, moved.text
    contract(api, floor5["5B"], day(0), day(0), 409, tenant=floor5["other"])


# --- the database is the final guard ----------------------------------------------------


def test_the_database_refuses_conflicting_claims_written_directly(
    floor5: dict, db_session: Session
) -> None:
    """No service involved: the exclusion constraints alone refuse a floor held while a
    desk on it is held, in either order, and allow two desks side by side."""
    prop_id = uuid.UUID(floor5["property"]["id"])
    ids = {k: uuid.UUID(floor5[k]["id"]) for k in ("floor", "5A", "A-desk", "B-desk")}
    period = "[2031-01-06 09:00+00, 2031-01-06 10:00+00)"

    def hold(space: str, chain: list[str]) -> None:
        allocation_id = uuid.uuid4()
        booking_id = db_session.scalar(
            text(
                "INSERT INTO bookings (property_id, space_id, customer_name, start_at, end_at)"
                " VALUES (:p, :s, 'Raw', '2031-01-06 09:00+00', '2031-01-06 10:00+00')"
                " RETURNING id"
            ),
            {"p": prop_id, "s": ids[space]},
        )
        db_session.execute(
            text(
                "INSERT INTO allocations (id, source_type, booking_id, property_id, space_id,"
                " period, original_period) VALUES (:id, 'BOOKING', :b, :p, :s, :r, :r)"
            ),
            {"id": allocation_id, "b": booking_id, "p": prop_id, "s": ids[space], "r": period},
        )
        for member in chain:
            db_session.execute(
                text(
                    "INSERT INTO allocation_claims (allocation_id, period, is_active, space_id,"
                    " claim_mode) VALUES (:a, :r, true, :s, :m)"
                ),
                {
                    "a": allocation_id,
                    "r": period,
                    "s": ids[member],
                    "m": 1 if member == space else 2,
                },
            )
        db_session.flush()

    hold("A-desk", ["floor", "5A", "A-desk"])
    hold("B-desk", ["floor", "B-desk"])  # siblings share the floor
    with pytest.raises(IntegrityError, match="ex_allocation_claims_mixed"):
        hold("floor", ["floor"])
    db_session.rollback()


# --- Scenario F: concurrency -------------------------------------------------------------


def race(*attempts: Callable[[Session], object]) -> list[str]:
    """Runs each attempt in its own session at the same moment; reports ok or the error."""
    barrier = threading.Barrier(len(attempts))
    outcomes: list[str] = [""] * len(attempts)

    def run(index: int, attempt: Callable[[Session], object]) -> None:
        session = get_session_factory()()
        try:
            session.connection()
            barrier.wait(timeout=15)
            attempt(session)
            outcomes[index] = "ok"
        except AppError as exc:
            outcomes[index] = f"{exc.status_code}"
        except Exception as exc:  # noqa: BLE001 - a surprise must fail the test, not hang it
            outcomes[index] = type(exc).__name__
        finally:
            session.close()

    threads = [threading.Thread(target=run, args=(i, a)) for i, a in enumerate(attempts)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return outcomes


def _booking(space: dict, start: datetime) -> Callable[[Session], object]:
    return lambda s: BookingService(s).create(
        {
            "property_id": uuid.UUID(space["property_id"]),
            "space_id": uuid.UUID(space["id"]),
            "customer_name": "Racer",
            "start_at": start,
            "end_at": start + HOUR,
            "status": BookingStatus.PENDING,
        }
    )


def _contract(space: dict, tenant: dict, start: date, end: date) -> Callable[[Session], object]:
    return lambda s: ContractService(s).create(
        {
            "tenant_id": uuid.UUID(tenant["id"]),
            "property_id": uuid.UUID(space["property_id"]),
            "space_id": uuid.UUID(space["id"]),
            "start_date": start,
            "end_date": end,
            "activate": True,
        }
    )


def test_f_a_parent_contract_and_a_child_booking_never_both_succeed(
    floor5: dict, db_session: Session
) -> None:
    for round_number in range(6):
        offset = 10 + 3 * round_number
        outcomes = race(
            _contract(floor5["5A"], floor5["tenant"], day(offset), day(offset)),
            _booking(floor5["A-desk"], at(offset)),
        )
        assert sorted(outcomes) == ["409", "ok"], (round_number, outcomes)


def test_f_simultaneous_whole_floor_and_portion_contracts(floor5: dict) -> None:
    for round_number in range(4):
        offset = 40 + 3 * round_number
        outcomes = race(
            _contract(floor5["floor"], floor5["tenant"], day(offset), day(offset)),
            _contract(floor5["5B"], floor5["other"], day(offset), day(offset)),
            _booking(floor5["B-cabin"], at(offset)),
        )
        assert outcomes.count("ok") >= 1 and outcomes.count("ok") <= 2, outcomes
        if outcomes[0] == "ok":  # the floor won: nothing else on it may hold
            assert outcomes[1:] == ["409", "409"], outcomes


def test_f_the_same_space_is_never_held_twice(floor5: dict, db_session: Session) -> None:
    outcomes = race(*[_booking(floor5["B-cabin2"], at(60)) for _ in range(6)])
    assert sorted(outcomes) == ["409"] * 5 + ["ok"], outcomes


def test_f_an_extension_races_a_booking_it_would_cover(api: TestClient, floor5: dict) -> None:
    held = contract(api, floor5["5A"], day(70), day(70), tenant=floor5["tenant"])
    outcomes = race(
        lambda s: ContractService(s).extend(uuid.UUID(held["id"]), day(75)),
        _booking(floor5["A-meeting"], at(73)),
    )
    assert sorted(outcomes) == ["409", "ok"], outcomes


def test_f_a_move_races_a_contract_on_the_destination(api: TestClient, floor5: dict) -> None:
    for round_number in range(4):
        offset = 80 + 2 * round_number
        loose = post(
            api,
            "spaces",
            {
                "property_id": floor5["property"]["id"],
                "name": f"Loose {round_number}",
                "space_type": "CABIN",
            },
        )
        book(api, loose, at(offset))
        outcomes = race(
            lambda s, loose=loose: SpaceService(s).move(
                uuid.UUID(loose["id"]), None, uuid.UUID(floor5["5B"]["id"])
            ),
            _contract(floor5["5B"], floor5["other"], day(offset), day(offset)),
        )
        # Either the cabin moved into 5B (and the contract on 5B was refused), or the
        # contract won (and the move into a held portion was refused). Never both.
        assert sorted(outcomes) == ["409", "ok"], (round_number, outcomes)


def test_every_live_allocation_holds_its_full_claim_chain(
    api: TestClient, floor5: dict, db_session: Session
) -> None:
    contract(api, floor5["5A"], day(0), day(1), tenant=floor5["tenant"])
    book(api, floor5["B-desk"])
    claims = db_session.execute(
        select(AllocationClaim.space_id, AllocationClaim.claim_mode)
        .join(Allocation, Allocation.id == AllocationClaim.allocation_id)
        .join(Booking, Booking.id == Allocation.booking_id)
    ).all()
    names = {uuid.UUID(floor5[k]["id"]): k for k in ("floor", "5B", "B-desk")}
    assert sorted((names[s], m) for s, m in claims) == [("5B", 2), ("B-desk", 1), ("floor", 2)]
    assert db_session.scalar(select(TenantOccupancy.status)) == "ACTIVE"
