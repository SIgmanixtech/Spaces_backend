"""Several seats booked together, then one contract for all of them.

Sample building "Sigmanix Tower" > Floor 5, split into two portions: 5A with two cabins,
5B with an open bay of six seats. A customer books 5 seats at once from the client app,
asks for a contract, and the property team approves or rejects it as one contract.
Prices are test data entered here."""

# ruff: noqa: F811  (pytest fixtures shared from test_client_api)
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from tests.test_client_api import MONDAY, iso, priya, ravi  # noqa: F401
from tests.test_properties import create_property
from tests.test_resources import V1, post

CLIENT = f"{V1}/client"
ADMIN = f"{V1}/admin"


@pytest.fixture
def tower(api: TestClient) -> dict:
    prop = create_property(api, name="Sigmanix Tower", description="Offices and coworking.")
    building = post(api, "buildings", {"property_id": prop["id"], "name": "Main block"})
    floor = post(
        api, "floors", {"building_id": building["id"], "name": "Floor 5", "level_number": 5}
    )

    def space(name: str, space_type: str, **extra: object) -> dict:
        return post(
            api,
            "spaces",
            {"property_id": prop["id"], "name": name, "space_type": space_type, **extra},
        )

    root = space("Floor 5", "FLOOR", floor_id=floor["id"], is_rentable=False)
    a = space("Portion 5A", "PORTION", parent_space_id=root["id"], area_sqft=1500)
    b = space("Portion 5B", "PORTION", parent_space_id=root["id"], area_sqft=1500)
    cabins = [
        space(f"Cabin {n}", "CABIN", parent_space_id=a["id"], capacity=4, area_sqft=180)
        for n in (1, 2)
    ]
    bay = space("Open Bay", "COWORKING", parent_space_id=b["id"], capacity=6)
    seats = [space(f"Seat {n}", "SEAT", parent_space_id=bay["id"], capacity=1) for n in range(1, 7)]
    for body in (
        {"rate_basis": "PER_SEAT", "billing_period": "DAILY", "amount": "500"},
        {"rate_basis": "PER_SEAT", "billing_period": "MONTHLY", "amount": "6000"},
    ):
        post(
            api,
            "pricing-rules",
            {
                "property_id": prop["id"],
                "effective_from": "2026-01-01",
                "status": "ACTIVE",
                "applies_to": "SEAT",
                **body,
            },
        )
    api.patch(f"{V1}/properties/{prop['id']}", json={"is_published": True})
    return {"property": prop, "floor": floor, "5A": a, "5B": b, "cabins": cabins, "seats": seats}


def book_seats(client: TestClient, seats: list[dict], expected: int = 201):  # type: ignore[no-untyped-def]
    response = client.post(
        f"{CLIENT}/bookings/group",
        json={
            "space_ids": [s["id"] for s in seats],
            "billing_period": "DAILY",
            "start_at": iso(MONDAY),
            "end_at": iso(MONDAY + timedelta(days=1)),
        },
    )
    assert response.status_code == expected, response.text
    return response.json()


def states(client: TestClient, tower: dict, start: datetime) -> dict[str, str]:
    base = f"{CLIENT}/public/properties/{tower['property']['id']}/floors/{tower['floor']['id']}"
    names = {s["id"]: s["name"] for s in client.get(base).json()["spaces"]}
    found = client.get(
        f"{base}/availability",
        params={"start_at": iso(start), "end_at": iso(start + timedelta(hours=1))},
    ).json()["spaces"]
    return {names[s["space_id"]]: s["status"] for s in found}


def test_the_floor_has_two_portions_with_cabins_and_seats(priya: TestClient, tower: dict) -> None:
    base = f"{CLIENT}/public/properties/{tower['property']['id']}/floors/{tower['floor']['id']}"
    plan = priya.get(base).json()["spaces"]
    by_name = {s["name"]: s for s in plan}
    assert {"Portion 5A", "Portion 5B", "Cabin 1", "Cabin 2", "Seat 1", "Seat 6"} <= set(by_name)
    assert by_name["Cabin 1"]["parent_space_id"] == tower["5A"]["id"]
    assert by_name["Seat 1"]["parent_space_id"] != tower["5A"]["id"]


def test_five_seats_book_together_and_become_one_contract(
    api: TestClient, priya: TestClient, ravi: TestClient, tower: dict
) -> None:
    five = tower["seats"][:5]
    booked = book_seats(priya, five)
    assert [b["space_name"] for b in booked] == [f"Seat {n}" for n in range(1, 6)]
    assert len({b["group_id"] for b in booked}) == 1 and booked[0]["group_size"] == 5
    assert all(b["status"] == "PENDING" for b in booked)

    # All or nothing: Seat 5 is taken now, so a second customer booking Seats 5 and 6 gets
    # neither, and Seat 6 stays free.
    clash = book_seats(ravi, tower["seats"][4:6], expected=409)
    assert "Seat 5" in clash["detail"]
    assert states(ravi, tower, MONDAY + timedelta(hours=2))["Seat 6"] == "AVAILABLE"

    # One request turns all five into one contract.
    start = (MONDAY + timedelta(days=3)).date()
    made = priya.post(
        f"{CLIENT}/bookings/{booked[2]['id']}/contract",
        json={"start_date": str(start), "months": 6},
    )
    assert made.status_code == 201, made.text
    contract = made.json()
    assert contract["status"] == "PENDING"
    assert contract["spaces"] == [f"Seat {n}" for n in range(1, 6)]
    assert contract["scope"] == "5 spaces: Seat 1, Seat 2, Seat 3, Seat 4, Seat 5"
    assert contract["monthly_rent"] == 5 * 6000
    mine = priya.get(f"{CLIENT}/contracts").json()
    assert [c["reference"] for c in mine] == [contract["reference"]]  # listed once
    dashboard = priya.get(f"{CLIENT}/dashboard").json()
    assert dashboard["contract_requests"] == 1
    requested = [a for a in dashboard["activity"] if a["kind"] == "CONTRACT"]
    assert len(requested) == 1  # said once, not five times

    # Every booking reads as converted to that contract (not cancelled).
    for b in booked:
        now = priya.get(f"{CLIENT}/bookings/{b['id']}").json()
        assert now["status"] == "CONVERTED" and now["contract_reference"] is not None

    # While it waits, the five seats are held for everyone else; Seat 6 is not.
    during = datetime.combine(start, datetime.min.time(), tzinfo=UTC) + timedelta(days=10, hours=5)
    seen = states(ravi, tower, during)
    assert [seen[f"Seat {n}"] for n in range(1, 7)] == ["RESERVED"] * 5 + ["AVAILABLE"]

    # The property team sees five rows of one group; approving one approves all five.
    rows = api.get(
        f"{ADMIN}/contracts", params={"property_id": tower["property"]["id"], "page_size": 50}
    ).json()["items"]
    assert len(rows) == 5 and len({r["group_id"] for r in rows}) == 1
    assert rows[0]["group_reference"] == contract["reference"]
    assert rows[0]["group_spaces"] == [f"Seat {n}" for n in range(1, 6)]
    last = sorted(rows, key=lambda r: r["reference"])[-1]
    assert api.post(f"{ADMIN}/contracts/{last['id']}/activate").status_code == 200
    statuses = {
        r["status"]
        for r in api.get(
            f"{ADMIN}/contracts", params={"property_id": tower["property"]["id"]}
        ).json()["items"]
    }
    assert statuses == {"ACTIVE"}
    assert [states(ravi, tower, during)[f"Seat {n}"] for n in range(1, 6)] == ["OCCUPIED"] * 5
    assert priya.get(f"{CLIENT}/contracts").json()[0]["status"] == "ACTIVE"


def test_rejecting_or_withdrawing_frees_every_seat(
    api: TestClient, priya: TestClient, ravi: TestClient, tower: dict
) -> None:
    start = (MONDAY + timedelta(days=3)).date()
    during = datetime.combine(start, datetime.min.time(), tzinfo=UTC) + timedelta(days=5, hours=5)
    first = book_seats(priya, tower["seats"][:3])
    request = priya.post(
        f"{CLIENT}/bookings/{first[0]['id']}/contract",
        json={"start_date": str(start), "months": 1},
    ).json()
    rejected = api.post(f"{ADMIN}/contracts/{request['id']}/cancel", json={"reason": "Full"})
    assert rejected.status_code == 200, rejected.text
    assert [states(ravi, tower, during)[f"Seat {n}"] for n in (1, 2, 3)] == ["AVAILABLE"] * 3

    second = book_seats(priya, tower["seats"][3:5])
    waiting = priya.post(
        f"{CLIENT}/bookings/{second[1]['id']}/contract",
        json={"start_date": str(start), "months": 1},
    ).json()
    assert waiting["spaces"] == ["Seat 4", "Seat 5"]
    withdrawn = priya.post(f"{CLIENT}/contracts/{waiting['id']}/withdraw")
    assert withdrawn.status_code == 200 and withdrawn.json()["status"] == "CANCELLED"
    assert [states(ravi, tower, during)[f"Seat {n}"] for n in (4, 5)] == ["AVAILABLE"] * 2


def test_a_group_needs_two_different_spaces(priya: TestClient, tower: dict) -> None:
    seat = tower["seats"][0]
    book_seats(priya, [seat], expected=422)
    book_seats(priya, [seat, seat], expected=422)
