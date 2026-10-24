"""Buildings, floors, layouts, spaces, amenities, tenants and assets."""

from datetime import date, timedelta

from fastapi.testclient import TestClient

from tests.test_properties import create_property

V1 = "/api/v1"


def post(api: TestClient, path: str, body: dict, expected: int = 201) -> dict:
    response = api.post(f"{V1}/{path}", json=body)
    assert response.status_code == expected, response.text
    return response.json()


# --- buildings and floors ----------------------------------------------------


def test_building_and_floor_lifecycle(api: TestClient) -> None:
    prop = create_property(api)
    building = post(api, "buildings", {"property_id": prop["id"], "name": "Block A"})
    floor = post(
        api, "floors", {"building_id": building["id"], "name": "Ground", "level_number": 0}
    )
    post(api, "floors", {"building_id": building["id"], "name": "First", "level_number": 1})

    floors = api.get(f"{V1}/floors", params={"building_id": building["id"]}).json()
    assert [item["name"] for item in floors["items"]] == ["Ground", "First"]

    duplicate_name = post(api, "buildings", {"property_id": prop["id"], "name": "Block A"}, 409)
    assert "already has a building" in duplicate_name["detail"]
    duplicate_level = post(
        api, "floors", {"building_id": building["id"], "name": "G2", "level_number": 0}, 409
    )
    assert "already has a floor" in duplicate_level["detail"]

    # A building with floors cannot be deleted; the error hides database details.
    blocked = api.delete(f"{V1}/buildings/{building['id']}")
    assert blocked.status_code == 409
    assert "fk_" not in blocked.text and "constraint" not in blocked.text.lower()

    renamed = api.patch(f"{V1}/floors/{floor['id']}", json={"name": "Lobby"})
    assert renamed.json()["name"] == "Lobby"


def test_building_requires_an_existing_property(api: TestClient) -> None:
    body = {"property_id": "00000000-0000-0000-0000-000000000000", "name": "Block A"}

    assert post(api, "buildings", body, 404)["detail"] == "Property not found."


def test_unreferenced_building_can_be_deleted(api: TestClient) -> None:
    prop = create_property(api)
    building = post(api, "buildings", {"property_id": prop["id"], "name": "Block A"})

    assert api.delete(f"{V1}/buildings/{building['id']}").status_code == 204
    assert api.get(f"{V1}/buildings/{building['id']}").status_code == 404


# --- layouts and spaces ------------------------------------------------------


def test_layout_names_its_building_and_floor(api: TestClient) -> None:
    prop = create_property(api)
    building = post(api, "buildings", {"property_id": prop["id"], "name": "Block A"})
    floor = post(api, "floors", {"building_id": building["id"], "name": "First", "level_number": 1})

    layout = post(
        api,
        "layouts",
        {
            "property_id": prop["id"],
            "building_id": building["id"],
            "floor_id": floor["id"],
            "name": "Open plan",
            "grid_columns": 12,
            "grid_rows": 8,
            "metadata": {"scale": "1:100"},
        },
    )

    assert layout["building_name"] == "Block A"
    assert layout["floor_name"] == "First"
    assert layout["metadata"] == {"scale": "1:100"}
    assert layout["status"] == "ACTIVE"


def test_layout_rejects_locations_from_elsewhere(api: TestClient) -> None:
    prop = create_property(api, name="Tower One")
    other = create_property(api, name="Tower Two")
    building = post(api, "buildings", {"property_id": prop["id"], "name": "Block A"})
    other_building = post(api, "buildings", {"property_id": other["id"], "name": "Block B"})
    other_floor = post(
        api, "floors", {"building_id": other_building["id"], "name": "G", "level_number": 0}
    )
    base = {"property_id": prop["id"], "name": "Plan"}

    wrong_building = post(api, "layouts", {**base, "building_id": other_building["id"]}, 400)
    assert "building does not belong" in wrong_building["detail"]
    wrong_floor = post(
        api, "layouts", {**base, "building_id": building["id"], "floor_id": other_floor["id"]}, 400
    )
    assert "floor does not belong" in wrong_floor["detail"]
    floor_only = post(api, "layouts", {**base, "floor_id": other_floor["id"]}, 400)
    assert "together with its building" in floor_only["detail"]


def test_archived_layout_is_hidden_from_the_default_list(api: TestClient) -> None:
    prop = create_property(api)
    layout = post(api, "layouts", {"property_id": prop["id"], "name": "Plan"})

    assert api.delete(f"{V1}/layouts/{layout['id']}").status_code == 204

    assert api.get(f"{V1}/layouts").json()["total"] == 0
    assert api.get(f"{V1}/layouts", params={"status": "ARCHIVED"}).json()["total"] == 1


def test_spaces_belong_to_a_layout_of_the_same_property(api: TestClient) -> None:
    prop = create_property(api, name="Tower One")
    other = create_property(api, name="Tower Two")
    layout = post(api, "layouts", {"property_id": prop["id"], "name": "Plan"})
    other_layout = post(api, "layouts", {"property_id": other["id"], "name": "Plan"})

    desk = post(
        api,
        "spaces",
        {
            "property_id": prop["id"],
            "layout_id": layout["id"],
            "name": "Desk 1",
            "code": "D-001",
            "space_type": "DESK",
            "capacity": 1,
            "price_amount": 450.50,
            "price_unit": "DAY",
            "x": 0,
            "y": 0,
            "width": 1,
            "height": 1,
        },
    )
    post(
        api,
        "spaces",
        {
            "property_id": prop["id"],
            "layout_id": layout["id"],
            "name": "Cabin",
            "space_type": "CABIN",
        },
    )

    assert desk["status"] == "AVAILABLE"
    assert desk["operational_status"] == "ACTIVE"
    assert desk["price_amount"] == 450.5
    assert desk["currency"] == "INR"

    on_layout = api.get(f"{V1}/layouts/{layout['id']}/spaces").json()
    assert [space["name"] for space in on_layout] == ["Cabin", "Desk 1"]

    mismatch = post(
        api,
        "spaces",
        {
            "property_id": prop["id"],
            "layout_id": other_layout["id"],
            "name": "Stray",
            "space_type": "DESK",
        },
        400,
    )
    assert "layout does not belong" in mismatch["detail"]

    duplicate_code = post(
        api,
        "spaces",
        {"property_id": prop["id"], "name": "Desk 2", "code": "D-001", "space_type": "DESK"},
        409,
    )
    assert "space with that code" in duplicate_code["detail"]


def test_space_rules_and_derived_status(api: TestClient) -> None:
    prop = create_property(api)
    base = {"property_id": prop["id"], "name": "Room", "space_type": "MEETING_ROOM"}

    assert "price_unit" in post(api, "spaces", {**base, "price_amount": 100}, 400)["detail"]
    assert api.post(f"{V1}/spaces", json={**base, "space_type": "HOT_TUB"}).status_code == 422
    assert api.post(f"{V1}/spaces", json={**base, "status": "OCCUPIED"}).status_code == 422

    space = post(api, "spaces", base)
    updated = api.patch(
        f"{V1}/spaces/{space['id']}", json={"operational_status": "UNDER_MAINTENANCE"}
    ).json()
    assert updated["status"] == "UNAVAILABLE"

    assert api.delete(f"{V1}/spaces/{space['id']}").json()["outcome"] == "DELETED"
    assert api.get(f"{V1}/spaces", params={"property_id": prop["id"]}).json()["total"] == 0


# --- amenities -----------------------------------------------------------------


def test_amenity_definitions_are_shared_between_properties(api: TestClient, db_session) -> None:
    from app.models import Amenity

    one = create_property(api, name="Tower One")
    two = create_property(api, name="Tower Two")

    first = post(
        api,
        "amenities",
        {
            "property_id": one["id"],
            "name": "Parking",
            "description": "Covered",
            "opening_time": "08:00:00",
            "closing_time": "20:00:00",
        },
    )
    second = post(api, "amenities", {"property_id": two["id"], "name": "parking"})

    assert first["amenity_id"] == second["amenity_id"]
    assert first["property_name"] == "Tower One"
    assert first["opening_time"] == "08:00:00"
    assert second["description"] == "Covered"
    assert db_session.query(Amenity).count() == 1

    again = post(api, "amenities", {"property_id": one["id"], "name": "Parking"}, 409)
    assert "already assigned" in again["detail"]

    listed = api.get(f"{V1}/amenities", params={"property_id": two["id"]}).json()
    assert listed["total"] == 1

    assert api.get(f"{V1}/properties/{one['id']}").json()["amenities"] == ["Parking"]
    api.patch(f"{V1}/amenities/{first['id']}", json={"status": "INACTIVE"})
    assert api.get(f"{V1}/properties/{one['id']}").json()["amenities"] == []

    assert api.delete(f"{V1}/amenities/{first['id']}").status_code == 204
    assert api.get(f"{V1}/amenities").json()["total"] == 1
    assert db_session.query(Amenity).count() == 1


# --- tenants and occupancy -------------------------------------------------------


def test_tenant_crud_and_search(api: TestClient) -> None:
    tenant = post(
        api,
        "tenants",
        {"name": "Acme Ltd", "email": "ops@acme.example", "phone": "+91 98765 43210"},
    )
    post(api, "tenants", {"name": "Globex"})

    assert tenant["occupancy_status"] == "VACANT"
    assert tenant["property_id"] is None
    assert tenant["booking_refs"] == []

    assert api.post(f"{V1}/tenants", json={"name": "Bad", "email": "nope"}).status_code == 422

    found = api.get(f"{V1}/tenants", params={"search": "acme.example"}).json()
    assert [item["name"] for item in found["items"]] == ["Acme Ltd"]

    assert api.delete(f"{V1}/tenants/{tenant['id']}").json()["outcome"] == "DELETED"
    assert api.get(f"{V1}/tenants").json()["total"] == 1


def test_occupancy_drives_property_space_and_tenant_status(api: TestClient) -> None:
    prop = create_property(api)
    office = post(
        api,
        "spaces",
        {"property_id": prop["id"], "name": "Office 1", "space_type": "PRIVATE_OFFICE"},
    )
    post(
        api,
        "spaces",
        {"property_id": prop["id"], "name": "Office 2", "space_type": "PRIVATE_OFFICE"},
    )
    acme = post(api, "tenants", {"name": "Acme Ltd"})
    globex = post(api, "tenants", {"name": "Globex"})
    today = date.today()
    started = (today - timedelta(days=30)).isoformat()

    occupancy = post(
        api,
        f"tenants/{acme['id']}/occupancies",
        {"property_id": prop["id"], "space_id": office["id"], "start_date": started},
    )
    assert occupancy["space_name"] == "Office 1"
    assert occupancy["end_date"] is None

    current = api.get(f"{V1}/properties/{prop['id']}").json()
    assert current["occupancy_status"] == "OCCUPIED"
    assert current["occupancy_rate"] == 0.5
    assert current["tenant_name"] == "Acme Ltd"
    assert api.get(f"{V1}/spaces/{office['id']}").json()["status"] == "OCCUPIED"

    tenant = api.get(f"{V1}/tenants/{acme['id']}").json()
    assert tenant["occupancy_status"] == "OCCUPIED"
    assert tenant["property_name"] == "Sigma Tower"
    assert tenant["space_id"] == office["id"]

    # A second tenant cannot take the same space while it is occupied.
    clash = post(
        api,
        f"tenants/{globex['id']}/occupancies",
        {"property_id": prop["id"], "space_id": office["id"], "start_date": today.isoformat()},
        409,
    )
    assert "already occupied" in clash["detail"]

    # Nothing occupied may be archived out from under the tenant.
    assert api.delete(f"{V1}/properties/{prop['id']}").status_code == 409
    assert api.delete(f"{V1}/spaces/{office['id']}").status_code == 409
    assert api.delete(f"{V1}/tenants/{acme['id']}").status_code == 409

    # Ending the occupancy yesterday frees everything but keeps the history.
    ended = api.patch(
        f"{V1}/occupancies/{occupancy['id']}",
        json={"end_date": (today - timedelta(days=1)).isoformat()},
    )
    assert ended.status_code == 200
    assert api.get(f"{V1}/properties/{prop['id']}").json()["occupancy_status"] == "VACANT"
    assert api.get(f"{V1}/spaces/{office['id']}").json()["status"] == "AVAILABLE"
    assert api.get(f"{V1}/tenants/{acme['id']}").json()["occupancy_status"] == "VACANT"
    assert len(api.get(f"{V1}/tenants/{acme['id']}/occupancies").json()) == 1

    post(
        api,
        f"tenants/{globex['id']}/occupancies",
        {"property_id": prop["id"], "space_id": office["id"], "start_date": today.isoformat()},
    )


def test_occupancy_validation(api: TestClient) -> None:
    prop = create_property(api, name="Tower One")
    other = create_property(api, name="Tower Two")
    space = post(api, "spaces", {"property_id": other["id"], "name": "Desk", "space_type": "DESK"})
    tenant = post(api, "tenants", {"name": "Acme Ltd"})
    path = f"tenants/{tenant['id']}/occupancies"

    backwards = api.post(
        f"{V1}/{path}",
        json={"property_id": prop["id"], "start_date": "2030-02-01", "end_date": "2030-01-01"},
    )
    assert backwards.status_code == 422

    wrong_space = post(
        api,
        path,
        {"property_id": prop["id"], "space_id": space["id"], "start_date": "2030-01-01"},
        400,
    )
    assert "space does not belong" in wrong_space["detail"]

    future = post(api, path, {"property_id": prop["id"], "start_date": "2999-01-01"})
    assert api.get(f"{V1}/tenants/{tenant['id']}").json()["occupancy_status"] == "RESERVED"
    assert api.delete(f"{V1}/occupancies/{future['id']}").status_code == 204
    assert api.delete(f"{V1}/occupancies/{future['id']}").status_code == 404


# --- assets -------------------------------------------------------------------


def test_asset_crud_and_location_rules(api: TestClient) -> None:
    prop = create_property(api, name="Tower One")
    other = create_property(api, name="Tower Two")
    building = post(api, "buildings", {"property_id": prop["id"], "name": "Block A"})
    other_building = post(api, "buildings", {"property_id": other["id"], "name": "Block B"})

    asset = post(
        api,
        "assets",
        {
            "name": "Projector",
            "category": "AV",
            "asset_tag": "AV-0001",
            "property_id": prop["id"],
            "building_id": building["id"],
            "location": "Boardroom",
            "purchase_date": "2025-04-01",
        },
    )
    assert asset["status"] == "OPERATIONAL"
    assert asset["property_name"] == "Tower One"

    wrong_building = post(
        api,
        "assets",
        {
            "name": "Chair",
            "category": "FURNITURE",
            "property_id": prop["id"],
            "building_id": other_building["id"],
        },
        400,
    )
    assert "building does not belong" in wrong_building["detail"]

    duplicate_tag = post(
        api,
        "assets",
        {"name": "Screen", "category": "AV", "asset_tag": "AV-0001", "property_id": prop["id"]},
        409,
    )
    assert "asset tag" in duplicate_tag["detail"]

    retired = api.patch(f"{V1}/assets/{asset['id']}", json={"status": "RETIRED"}).json()
    assert retired["status"] == "RETIRED"
    by_status = api.get(f"{V1}/assets", params={"status": "RETIRED", "search": "proj"}).json()
    assert by_status["total"] == 1

    assert api.delete(f"{V1}/assets/{asset['id']}").status_code == 204
    assert api.get(f"{V1}/assets/{asset['id']}").status_code == 404
