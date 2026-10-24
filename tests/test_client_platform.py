# ruff: noqa: F811  (shared fixtures imported from test_client_api)
"""Admin and customer apps on one backend: publication, offer, codes, rescheduling, and
whole-floor conflicts in both directions. Fixtures come from test_client_api."""

from datetime import date, timedelta

from fastapi.testclient import TestClient

from app.main import app
from tests.test_client_api import CUSTOMER, MONDAY, PUBLIC, book, hub, iso, priya, ravi  # noqa: F401
from tests.test_properties import create_property
from tests.test_resources import V1, post


def test_a_property_saved_by_the_admin_is_what_customers_see(api: TestClient, hub: dict) -> None:
    api.patch(f"{V1}/properties/{hub['property']['id']}", json={"code": "SH"})
    anonymous = TestClient(app)
    card = anonymous.get(f"{PUBLIC}/properties").json()["items"][0]
    assert (card["id"], card["code"], card["offer"], card["bookable_spaces"]) == (
        hub["property"]["id"],
        "SH",
        "BOOKABLE",
        5,
    )
    detail = anonymous.get(f"{PUBLIC}/properties/{hub['property']['id']}").json()
    floor = detail["buildings"][0]["floors"][0]
    assert (floor["spaces"], floor["rentable_spaces"], floor["bookable_spaces"]) == (6, 6, 5)
    assert "SEAT" in floor["space_types"]
    # The admin's own view of the same record.
    assert api.get(f"{V1}/properties/{hub['property']['id']}").json()["code"] == "SH"
    assert api.get(f"{V1}/properties", params={"search": "SH"}).json()["total"] == 1
    assert anonymous.get(f"{PUBLIC}/properties", params={"search": "SH"}).json()["total"] == 1


def test_new_properties_are_listed_at_once_unless_hidden(api: TestClient) -> None:
    shown = create_property(api, name="Listed Tower")
    hidden = create_property(api, name="Hidden Tower", is_published=False)
    assert (shown["is_published"], hidden["is_published"]) == (True, False)
    names = [c["name"] for c in TestClient(app).get(f"{PUBLIC}/properties").json()["items"]]
    assert names == ["Listed Tower"]


def test_codes_are_unique(api: TestClient) -> None:
    create_property(api, name="One", code="N1")
    refused = api.post(
        f"{V1}/properties",
        json={
            "name": "Two",
            "code": "N1",
            "property_type": "OFFICE",
            "category": "COMMERCIAL",
            "address": "x",
            "city": "Pune",
            "state": "MH",
            "postal_code": "411001",
        },
    )
    assert refused.status_code == 409
    assert refused.json()["detail"] == "Another property already uses that code."


def test_unpriced_inventory_is_for_enquiries_only(api: TestClient) -> None:
    prop = create_property(api, name="Nexus One", is_published=True)
    building = post(api, "buildings", {"property_id": prop["id"], "name": "Main"})
    floor = post(
        api, "floors", {"building_id": building["id"], "name": "Ground", "level_number": 0}
    )
    root = post(
        api,
        "spaces",
        {
            "property_id": prop["id"],
            "floor_id": floor["id"],
            "name": "Ground",
            "space_type": "FLOOR",
            "is_rentable": False,
        },
    )
    post(
        api,
        "spaces",
        {
            "property_id": prop["id"],
            "parent_space_id": root["id"],
            "name": "Cabin",
            "space_type": "CABIN",
        },
    )
    card = TestClient(app).get(f"{PUBLIC}/properties").json()["items"][0]
    assert (card["offer"], card["bookable_spaces"], card["starting_prices"]) == ("ENQUIRY", 0, [])


def test_sorting(api: TestClient, hub: dict) -> None:
    second = create_property(api, name="Alpha House", is_published=True)
    anonymous = TestClient(app)

    def names(**params: str) -> list[str]:
        listed = anonymous.get(f"{PUBLIC}/properties", params=params).json()["items"]
        return [c["name"] for c in listed]

    assert names() == ["Alpha House", "Sigma Hub"]
    assert names(sort="newest") == ["Alpha House", "Sigma Hub"]
    assert names(sort="price", billing_period="DAILY") == ["Sigma Hub"]
    assert anonymous.get(f"{PUBLIC}/properties", params={"sort": "price"}).status_code == 400
    assert second["is_published"] is True


def test_rescheduling_reprices_rechecks_and_needs_confirmation_again(
    api: TestClient, priya: TestClient, ravi: TestClient, hub: dict
) -> None:
    mine = book(priya, hub["seat1"], hours=2)
    api.post(f"{V1}/bookings/{mine['id']}/confirm")
    moved = priya.post(
        f"{CUSTOMER}/bookings/{mine['id']}/reschedule",
        json={
            "billing_period": "DAILY",
            "start_at": iso(MONDAY + timedelta(days=1)),
            "end_at": iso(MONDAY + timedelta(days=2)),
        },
    )
    assert moved.status_code == 200, moved.text
    body = moved.json()
    assert (body["status"], body["billing_period"], body["quoted_total"]) == (
        "PENDING",
        "DAILY",
        500.0,
    )
    # The old time is free again; the new one is held.
    book(ravi, hub["seat1"])
    taken = book(ravi, hub["seat1"], start=MONDAY + timedelta(days=1), expected=409)
    assert taken["detail"].startswith("Reserved")
    # Onto someone else's time: refused without naming them.
    other = book(ravi, hub["seat2"], start=MONDAY + timedelta(days=3))
    clash = priya.post(
        f"{CUSTOMER}/bookings/{mine['id']}/reschedule",
        json={
            "billing_period": "HOURLY",
            "start_at": iso(MONDAY),
            "end_at": iso(MONDAY + timedelta(hours=1)),
        },
    )
    assert clash.status_code == 409 and "ravi" not in clash.text.lower()
    assert (
        ravi.post(
            f"{CUSTOMER}/bookings/{mine['id']}/reschedule",
            json={
                "billing_period": "HOURLY",
                "start_at": iso(MONDAY),
                "end_at": iso(MONDAY + timedelta(hours=1)),
            },
        ).status_code
        == 404
    )
    assert other["status"] == "PENDING"


def test_whole_floor_contract_and_child_bookings_exclude_each_other(
    api: TestClient, priya: TestClient, hub: dict
) -> None:
    floor_root = next(
        s
        for s in TestClient(app)
        .get(f"{PUBLIC}/properties/{hub['property']['id']}/floors/{hub['floor']['id']}")
        .json()["spaces"]
        if s["space_type"] == "FLOOR"
    )
    tenant = post(api, "tenants", {"name": "Whole Floor Ltd"})
    contract = {
        "tenant_id": tenant["id"],
        "property_id": hub["property"]["id"],
        "space_id": floor_root["id"],
        "start_date": str(date.today()),
        "activate": True,
    }
    # An existing seat booking blocks a new whole-floor lease...
    seat = book(priya, hub["seat1"])
    assert api.post(f"{V1}/contracts", json=contract).status_code == 409
    priya.post(f"{CUSTOMER}/bookings/{seat['id']}/cancel")
    # ...and once the floor is leased, nothing inside it can be booked.
    post(api, "contracts", contract)
    for space in ("seat2", "cabin", "5B"):
        period = "HOURLY" if space.startswith("seat") else "MONTHLY"
        hours = 1 if period == "HOURLY" else 24 * 30
        refused = book(priya, hub[space], period=period, hours=hours, expected=409)
        assert "Occupied under a lease" in refused["detail"]
