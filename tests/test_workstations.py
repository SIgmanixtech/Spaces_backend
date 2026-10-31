# ruff: noqa: F811  (pytest fixtures shared from test_client_api)
"""Business rule BR-028 (D3, option B): a workstation is one rentable space; its chairs are
named, non-rentable child spaces created with it. Booking the workstation holds its chairs;
a chair is never booked on its own (admin, customer or contract). Allocation unchanged.

Uses the client-API sample floor: Floor 5 > Portion 5A (Cabin 1) and Portion 5B (Open Bay
with Seat 1 and Seat 2)."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from tests.test_client_api import CUSTOMER, MONDAY, PUBLIC, book, hub, iso, priya  # noqa: F401
from tests.test_resources import V1, post

WS = f"{V1}/spaces/workstations"


@pytest.fixture
def layout(api: TestClient, hub: dict) -> dict:
    return post(
        api,
        "layouts",
        {
            "property_id": hub["property"]["id"],
            "building_id": hub["building"]["id"],
            "floor_id": hub["floor"]["id"],
            "name": "Floor 5 plan",
            "grid_columns": 40,
            "grid_rows": 28,
        },
    )


def workstation(
    api: TestClient, hub: dict, layout: dict, chairs: int, name: str = "WS1", **extra: object
) -> dict:
    response = api.post(
        WS,
        json={
            "property_id": hub["property"]["id"],
            "floor_id": hub["floor"]["id"],
            "parent_space_id": hub["5B"]["id"],
            "layout_id": layout["id"],
            "name": name,
            "chairs": chairs,
            "x": 20,
            "y": 2,
            **extra,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def spaces_of(api: TestClient, hub: dict) -> list[dict]:
    return api.get(
        f"{V1}/spaces",
        params={"property_id": hub["property"]["id"], "page_size": 100, "include_structural": True},
    ).json()["items"]


@pytest.mark.parametrize("chairs", [1, 4, 20])
def test_a_workstation_and_its_chairs_are_created_together(
    api: TestClient, hub: dict, layout: dict, chairs: int
) -> None:
    made = workstation(api, hub, layout, chairs)
    ws = made["workstation"]
    assert ws["space_type"] == "DEDICATED_WORKSTATION" and ws["is_rentable"] is True
    assert ws["capacity"] == chairs and ws["parent_space_id"] == hub["5B"]["id"]
    assert [c["name"] for c in made["chairs"]] == [f"WS1-Chair {n}" for n in range(1, chairs + 1)]
    for chair in made["chairs"]:
        assert chair["space_type"] == "SEAT" and chair["is_rentable"] is False
        assert chair["parent_space_id"] == ws["id"] and chair["layout_id"] == layout["id"]
        assert chair["depth"] == ws["depth"] + 1 and chair["floor_id"] == ws["floor_id"]
        # Each chair has its own cell inside the workstation's outline.
        assert ws["x"] <= chair["x"] < ws["x"] + ws["width"]
        assert ws["y"] <= chair["y"] < ws["y"] + ws["height"]
    cells = {(c["x"], c["y"]) for c in made["chairs"]}
    assert len(cells) == chairs
    assert (ws["width"], ws["height"]) == ((1, 1) if chairs == 1 else (-(-chairs // 2), 2))


def test_limits_and_all_or_nothing(api: TestClient, hub: dict, layout: dict) -> None:
    before = len(spaces_of(api, hub))
    for chairs in (0, 21):
        assert (
            api.post(
                WS, json={"property_id": hub["property"]["id"], "name": "X", "chairs": chairs}
            ).status_code
            == 422
        )
    # A seat cannot hold a workstation: refused, and no chair is left behind.
    bad = api.post(
        WS,
        json={
            "property_id": hub["property"]["id"],
            "floor_id": hub["floor"]["id"],
            "parent_space_id": hub["seat1"]["id"],
            "layout_id": layout["id"],
            "name": "WS9",
            "chairs": 3,
        },
    )
    assert bad.status_code == 400, bad.text
    assert len(spaces_of(api, hub)) == before


def test_repeating_a_request_or_saving_the_layout_never_duplicates(
    api: TestClient, hub: dict, layout: dict
) -> None:
    made = workstation(api, hub, layout, 4)
    count = len(spaces_of(api, hub))
    again = api.post(
        WS,
        json={
            "property_id": hub["property"]["id"],
            "floor_id": hub["floor"]["id"],
            "parent_space_id": hub["5B"]["id"],
            "layout_id": layout["id"],
            "name": "ws1",
            "chairs": 4,
        },
    )
    assert again.status_code == 409 and "already exists" in again.json()["detail"]
    # Saving the layout (moving the block and its chairs) twice, then reopening it.
    group = [made["workstation"], *made["chairs"]]
    for _ in range(2):
        for space in group:
            moved = api.patch(f"{V1}/spaces/{space['id']}", json={"x": space["x"] + 3})
            assert moved.status_code == 200, moved.text
    reopened = api.get(f"{V1}/layouts/{layout['id']}/spaces").json()
    on_plan = (
        {s["id"]: s for s in reopened}
        if isinstance(reopened, list)
        else {s["id"]: s for s in reopened["items"]}
    )
    for space in group:
        assert on_plan[space["id"]]["x"] == space["x"] + 3
        assert on_plan[space["id"]]["y"] == space["y"]
    assert len(spaces_of(api, hub)) == count


def test_moving_the_workstation_moves_its_chairs(api: TestClient, hub: dict, layout: dict) -> None:
    made = workstation(api, hub, layout, 3)
    moved = api.post(
        f"{V1}/spaces/{made['workstation']['id']}/move", json={"parent_space_id": hub["5A"]["id"]}
    )
    assert moved.status_code == 200, moved.text
    by_id = {s["id"]: s for s in spaces_of(api, hub)}
    assert by_id[made["workstation"]["id"]]["parent_space_id"] == hub["5A"]["id"]
    for chair in made["chairs"]:
        assert by_id[chair["id"]]["parent_space_id"] == made["workstation"]["id"]
        assert by_id[chair["id"]]["depth"] == by_id[made["workstation"]["id"]]["depth"] + 1


def test_a_chair_stays_non_rentable(api: TestClient, hub: dict, layout: dict) -> None:
    chair = workstation(api, hub, layout, 2)["chairs"][0]
    refused = api.patch(f"{V1}/spaces/{chair['id']}", json={"is_rentable": True})
    assert refused.status_code == 400 and "cannot be made rentable" in refused.json()["detail"]
    # Its marker survives other edits, so it can never be switched later.
    api.patch(f"{V1}/spaces/{chair['id']}", json={"attributes": {"colour": "blue"}})
    assert api.patch(f"{V1}/spaces/{chair['id']}", json={"is_rentable": True}).status_code == 400
    renamed = api.patch(f"{V1}/spaces/{chair['id']}", json={"name": "WS1-Chair A"})
    assert renamed.status_code == 200 and renamed.json()["is_rentable"] is False


def test_booking_the_workstation_holds_its_chairs_and_chairs_are_never_booked_alone(
    api: TestClient, priya: TestClient, hub: dict, layout: dict
) -> None:
    made = workstation(api, hub, layout, 4)
    ws, chair = made["workstation"], made["chairs"][0]
    window = {"start_at": iso(MONDAY), "end_at": iso(MONDAY + timedelta(hours=2))}

    def admin_booking(space: dict) -> object:
        return api.post(
            f"{V1}/bookings",
            json={
                "property_id": hub["property"]["id"],
                "space_id": space["id"],
                "customer_name": "Walk-in",
                **window,
            },
        )

    # Chairs and other non-rentable spaces are refused for admins.
    for space in (chair,):
        refused = admin_booking(space)
        assert refused.status_code == 409 and "not rentable" in refused.json()["detail"]
    corridor = post(
        api,
        "spaces",
        {
            "property_id": hub["property"]["id"],
            "parent_space_id": hub["5A"]["id"],
            "name": "Corridor",
            "space_type": "NON_RENTABLE",
        },
    )
    washroom = post(
        api,
        "spaces",
        {
            "property_id": hub["property"]["id"],
            "parent_space_id": hub["5A"]["id"],
            "name": "Washroom",
            "space_type": "OTHER",
            "is_rentable": False,
        },
    )
    for space in (corridor, washroom):
        assert admin_booking(space).status_code == 409
    # Customers and contracts are refused too.
    # (The customer path already refused it before this phase: a non-rentable space has no price.)
    assert "not rentable" in book(priya, chair, expected=400)["detail"]
    contract = api.post(
        f"{V1}/contracts",
        json={
            "tenant_id": post(api, "tenants", {"name": "Acme"})["id"],
            "property_id": hub["property"]["id"],
            "space_id": chair["id"],
            "start_date": str((MONDAY + timedelta(days=30)).date()),
            "activate": True,
        },
    )
    assert contract.status_code == 409 and "not rentable" in contract.json()["detail"]

    # The workstation itself books normally, and holds its chairs for that time.
    booked = admin_booking(ws)
    assert booked.status_code == 201, booked.text
    states = {
        s["space_id"]: s["status"]
        for s in TestClient(api.app)
        .get(
            f"{PUBLIC}/properties/{hub['property']['id']}/floors/{hub['floor']['id']}/availability",
            params=window,
        )
        .json()["spaces"]
    }
    assert states[ws["id"]] != "AVAILABLE"
    assert all(states[c["id"]] != "AVAILABLE" for c in made["chairs"])
    # A second booking of the workstation, or of the portion around it, conflicts.
    assert admin_booking(ws).status_code == 409
    assert admin_booking(hub["5B"]).status_code == 409
