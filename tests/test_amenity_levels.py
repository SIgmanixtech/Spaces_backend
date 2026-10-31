# ruff: noqa: F811  (pytest fixtures shared from test_client_api)
"""Amenities at property, building, floor and space level (BR-031, D16 option C).

Sample property "Sigma Hub" (from test_client_api): Tower A > Floor 5 > Portion 5A
(Cabin 1) and Portion 5B (Open Bay > Seat 1, Seat 2). The amenity names below are test
data only; the application has no built-in amenity list.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from app.models import ActivityLog
from app.models.amenity import PropertyAmenity
from app.services.amenities import effective_names_by_space
from tests.test_client_api import hub, priya  # noqa: F401
from tests.test_properties import create_property
from tests.test_resources import V1

A = f"{V1}/admin/amenities"
ADMIN = f"{V1}/admin"
PUBLIC = f"{V1}/client/public"


def assign(api: TestClient, expected: int = 201, **body: object) -> dict:
    response = api.post(A, json=body)
    assert response.status_code == expected, response.text
    return response.json()


@pytest.fixture
def placed(api: TestClient, hub: dict) -> dict[str, dict]:
    """Amenities at every level of Sigma Hub."""
    p = hub["property"]["id"]
    return {
        "reception": assign(api, property_id=p, name="Reception"),
        "wifi": assign(api, property_id=p, name="Wi-Fi"),
        "lift": assign(api, property_id=p, building_id=hub["building"]["id"], name="Lift"),
        "generator": assign(
            api, property_id=p, building_id=hub["building"]["id"], name="Generator"
        ),
        "pantry": assign(api, property_id=p, floor_id=hub["floor"]["id"], name="Pantry"),
        "floor_wifi": assign(api, property_id=p, floor_id=hub["floor"]["id"], name="wi-fi"),
        "ac": assign(api, property_id=p, space_id=hub["5B"]["id"], name="Air conditioning"),
        "backup": assign(api, property_id=p, space_id=hub["bay"]["id"], name="Power backup"),
    }


def effective(client: TestClient, **place: str) -> dict:
    response = client.get(f"{A}/effective", params=place)
    assert response.status_code == 200, response.text
    return response.json()


def by_name(found: dict) -> dict[str, dict]:
    return {a["name"]: a for a in found["amenities"]}


def sources(amenity: dict) -> list[tuple[str, str]]:
    return [(s["level"], s["place_name"]) for s in amenity["sources"]]


def test_each_level_is_assigned_with_its_place(api: TestClient, hub: dict, placed: dict) -> None:
    assert (placed["reception"]["level"], placed["reception"]["building_id"]) == (
        "PROPERTY",
        None,
    )
    assert (placed["lift"]["level"], placed["lift"]["building_name"]) == ("BUILDING", "Tower A")
    # A floor assignment records its building too, so the floor key can check both.
    assert placed["pantry"]["level"] == "FLOOR"
    assert (placed["pantry"]["floor_name"], placed["pantry"]["building_id"]) == (
        "Floor 5",
        hub["building"]["id"],
    )
    assert (placed["ac"]["level"], placed["ac"]["space_name"]) == ("SPACE", "Portion 5B")
    # The shared definition is reused case-insensitively: "wi-fi" is the same amenity.
    assert placed["floor_wifi"]["amenity_id"] == placed["wifi"]["amenity_id"]
    assert placed["floor_wifi"]["name"] == "Wi-Fi"
    p = hub["property"]["id"]
    # The property tab asks for property level only; other levels can be listed too.
    own = api.get(A, params={"property_id": p, "level": "PROPERTY"}).json()
    assert sorted(i["name"] for i in own["items"]) == ["Reception", "Wi-Fi"] and own["total"] == 2
    every = api.get(A, params={"property_id": p}).json()
    assert every["total"] == 8
    floor = api.get(A, params={"floor_id": hub["floor"]["id"]}).json()
    assert sorted(i["name"] for i in floor["items"]) == ["Pantry", "Wi-Fi"]
    spaces = api.get(A, params={"property_id": p, "level": "SPACE"}).json()
    assert sorted(i["space_name"] for i in spaces["items"]) == ["Open Bay", "Portion 5B"]
    # The admin's property view and the public card still list property-level amenities.
    assert api.get(f"{ADMIN}/properties/{p}").json()["amenities"] == ["Reception", "Wi-Fi"]


def test_duplicates_are_refused_per_level_only(api: TestClient, hub: dict, placed: dict) -> None:
    p = hub["property"]["id"]
    cases = [
        ({"property_id": p}, "already assigned to the property"),
        ({"property_id": p, "building_id": hub["building"]["id"]}, "assigned to the building"),
        ({"property_id": p, "floor_id": hub["floor"]["id"]}, "assigned to the floor"),
        ({"property_id": p, "space_id": hub["5B"]["id"]}, "assigned to the space"),
    ]
    names = ["RECEPTION", "lift", "Pantry", "air conditioning"]
    for (place, message), name in zip(cases, names, strict=True):
        dup = assign(api, 409, name=name, **place)
        assert message in dup["detail"]
    # The same amenity at another level is not a duplicate.
    assign(api, property_id=p, space_id=hub["seat1"]["id"], name="Air conditioning")
    assign(api, property_id=p, building_id=hub["building"]["id"], name="Pantry")


def test_places_must_belong_to_the_property_and_be_usable(
    api: TestClient, hub: dict, placed: dict
) -> None:
    p = hub["property"]["id"]
    other = create_property(api, name="Other Park")
    ob = api.post(f"{ADMIN}/buildings", json={"property_id": other["id"], "name": "B"}).json()
    of = api.post(
        f"{ADMIN}/floors", json={"building_id": ob["id"], "name": "F1", "level_number": 1}
    ).json()
    os_ = api.post(
        f"{ADMIN}/spaces", json={"property_id": other["id"], "name": "Desk", "space_type": "DESK"}
    ).json()
    for place in ({"building_id": ob["id"]}, {"floor_id": of["id"]}, {"space_id": os_["id"]}):
        refused = assign(api, 400, property_id=p, name="Lift", **place)
        assert "is not part of" in refused["detail"]
    # A floor with a building it is not in, or a space together with a floor.
    second = api.post(f"{ADMIN}/buildings", json={"property_id": p, "name": "Tower B"}).json()
    wrong = assign(
        api, 400, property_id=p, building_id=second["id"], floor_id=hub["floor"]["id"], name="X"
    )
    assert "not in that building" in wrong["detail"]
    both = assign(
        api, 400, property_id=p, floor_id=hub["floor"]["id"], space_id=hub["bay"]["id"], name="X"
    )
    assert "one place" in both["detail"]
    # Records that do not exist.
    for place in ("building_id", "floor_id", "space_id"):
        assign(api, 404, property_id=p, name="X", **{place: str(uuid.uuid4())})
    # The whole-floor space: the floor itself takes its amenities.
    root = {"id": hub["5A"]["parent_space_id"]}
    refused = assign(api, 400, property_id=p, space_id=root["id"], name="Pantry")
    assert "whole-floor space" in refused["detail"] and "floor itself" in refused["detail"]


def test_a_space_inherits_from_parent_spaces_floor_building_and_property(
    api: TestClient, hub: dict, placed: dict
) -> None:
    seat = effective(api, space_id=hub["seat1"]["id"])
    assert (seat["level"], seat["place_name"], seat["property_id"]) == (
        "SPACE",
        "Seat 1",
        hub["property"]["id"],
    )
    found = by_name(seat)
    assert sorted(found) == [
        "Air conditioning",
        "Generator",
        "Lift",
        "Pantry",
        "Power backup",
        "Reception",
        "Wi-Fi",
    ]
    assert not any(a["is_direct"] for a in found.values())
    # Recursively through the parent spaces, each with the actual source space's name.
    assert sources(found["Power backup"]) == [("SPACE", "Open Bay")]
    assert sources(found["Air conditioning"]) == [("SPACE", "Portion 5B")]
    assert sources(found["Lift"]) == [("BUILDING", "Tower A")]
    # Listed once, with every source, nearest first.
    assert sources(found["Wi-Fi"]) == [("FLOOR", "Floor 5"), ("PROPERTY", "Sigma Hub")]
    assert [s["assignment_id"] for s in found["Wi-Fi"]["sources"]] == [
        placed["floor_wifi"]["id"],
        placed["wifi"]["id"],
    ]

    bay = by_name(effective(api, space_id=hub["bay"]["id"]))
    assert bay["Power backup"]["is_direct"] is True
    assert bay["Air conditioning"]["is_direct"] is False
    # A sibling branch inherits nothing from Portion 5B.
    cabin = by_name(effective(api, space_id=hub["cabin"]["id"]))
    assert "Air conditioning" not in cabin and "Power backup" not in cabin
    assert sorted(cabin) == ["Generator", "Lift", "Pantry", "Reception", "Wi-Fi"]

    floor = by_name(effective(api, floor_id=hub["floor"]["id"]))
    assert {n: a["is_direct"] for n, a in floor.items()} == {
        "Generator": False,
        "Lift": False,
        "Pantry": True,
        "Reception": False,
        "Wi-Fi": True,
    }
    building = by_name(effective(api, building_id=hub["building"]["id"]))
    assert sorted(building) == ["Generator", "Lift", "Reception", "Wi-Fi"]
    assert building["Wi-Fi"]["is_direct"] is False
    prop = by_name(effective(api, property_id=hub["property"]["id"]))
    assert {n: a["is_direct"] for n, a in prop.items()} == {"Reception": True, "Wi-Fi": True}
    # The whole-floor space answers for its floor.
    root = hub["5A"]["parent_space_id"]
    assert by_name(effective(api, space_id=root)) == floor
    # One place exactly.
    assert api.get(f"{A}/effective").status_code == 400
    two = {"floor_id": hub["floor"]["id"], "space_id": hub["bay"]["id"]}
    assert api.get(f"{A}/effective", params=two).status_code == 400


def test_inheritance_is_computed_not_copied_and_levels_are_independent(
    api: TestClient, hub: dict, placed: dict, db_session: Session
) -> None:
    count = db_session.scalar(select(func.count()).select_from(PropertyAmenity))
    assert count == len(placed)  # nothing was copied down the hierarchy
    seat = hub["seat1"]["id"]
    # An INACTIVE assignment is ignored and never hides an active one elsewhere.
    api.patch(f"{A}/{placed['floor_wifi']['id']}", json={"status": "INACTIVE"})
    assert sources(by_name(effective(api, space_id=seat))["Wi-Fi"]) == [("PROPERTY", "Sigma Hub")]
    api.patch(f"{A}/{placed['pantry']['id']}", json={"status": "INACTIVE"})
    assert "Pantry" not in by_name(effective(api, space_id=seat))
    api.patch(f"{A}/{placed['floor_wifi']['id']}", json={"status": "ACTIVE"})
    # Removing one level's assignment leaves the same amenity at other levels.
    assert api.delete(f"{A}/{placed['floor_wifi']['id']}").status_code == 204
    found = by_name(effective(api, space_id=seat))
    assert sources(found["Wi-Fi"]) == [("PROPERTY", "Sigma Hub")]
    assert api.get(f"{A}/{placed['wifi']['id']}").status_code == 200
    assert api.delete(f"{A}/{placed['ac']['id']}").status_code == 204
    found = by_name(effective(api, space_id=seat))
    assert "Air conditioning" not in found and "Power backup" in found
    logged = db_session.scalars(
        select(ActivityLog.message).where(ActivityLog.entity_type == "property_amenities")
    ).all()
    assert 'Amenity "Pantry" was added to floor "Floor 5" (Tower A) at "Sigma Hub".' in logged
    assert 'Amenity "Wi-Fi" was removed from floor "Floor 5" at "Sigma Hub".' in logged


def test_archived_spaces_keep_their_assignments_but_offer_nothing(
    api: TestClient, hub: dict, placed: dict
) -> None:
    p = hub["property"]["id"]
    desk = api.post(
        f"{ADMIN}/spaces",
        json={
            "property_id": p,
            "name": "Old Desk",
            "space_type": "DESK",
            "parent_space_id": hub["5A"]["id"],
        },
    ).json()
    kept = assign(api, property_id=p, space_id=desk["id"], name="Locker")
    # Referenced by an amenity, so deleting it archives it and keeps the record.
    removed = api.delete(f"{ADMIN}/spaces/{desk['id']}")
    assert removed.status_code == 200 and "ARCHIVED" in removed.text
    assert api.get(f"{A}/{kept['id']}").json()["space_name"] == "Old Desk"
    refused = assign(api, 400, property_id=p, space_id=desk["id"], name="Shelf")
    assert "archived" in refused["detail"]
    assert api.get(f"{A}/effective", params={"space_id": desk["id"]}).status_code == 400
    found = api.get(f"{PUBLIC}/properties", params={"amenity": "Locker"}).json()
    assert found["total"] == 0


def test_buildings_and_floors_with_amenities_cannot_be_deleted(api: TestClient, hub: dict) -> None:
    p = hub["property"]["id"]
    building = api.post(f"{ADMIN}/buildings", json={"property_id": p, "name": "Annexe"}).json()
    floor = api.post(
        f"{ADMIN}/floors", json={"building_id": building["id"], "name": "G", "level_number": 0}
    ).json()
    on_floor = assign(api, property_id=p, floor_id=floor["id"], name="Pantry")
    refused = api.delete(f"{ADMIN}/floors/{floor['id']}")
    assert refused.status_code == 409
    assert refused.json()["detail"] == (
        "This floor has amenities assigned to it. Remove those amenity assignments first."
    )
    assert api.delete(f"{A}/{on_floor['id']}").status_code == 204
    assert api.delete(f"{ADMIN}/floors/{floor['id']}").status_code == 204
    on_building = assign(api, property_id=p, building_id=building["id"], name="Lift")
    refused = api.delete(f"{ADMIN}/buildings/{building['id']}")
    assert refused.status_code == 409 and "Remove those amenity assignments" in refused.text
    assert api.delete(f"{A}/{on_building['id']}").status_code == 204
    assert api.delete(f"{ADMIN}/buildings/{building['id']}").status_code == 204


def test_only_admins_manage_amenities_and_anyone_may_read_a_space(
    client: TestClient, priya: TestClient, api: TestClient, hub: dict, placed: dict
) -> None:
    p = hub["property"]["id"]
    body = {"property_id": p, "name": "Gym"}
    for who, status in ((client, 401), (priya, 403)):
        assert who.get(A).status_code == status
        assert who.post(A, json=body).status_code == status
        assert who.get(f"{A}/effective", params={"property_id": p}).status_code == status
        assert who.patch(f"{A}/{placed['lift']['id']}", json={"notes": "x"}).status_code == status
        assert who.delete(f"{A}/{placed['lift']['id']}").status_code == status
    url = f"{PUBLIC}/spaces/{hub['seat1']['id']}/amenities"
    api.patch(f"{A}/{placed['lift']['id']}", json={"notes": "Internal: service contract #12"})
    for who in (client, priya):  # a visitor and an activated tenant
        response = who.get(url)
        assert response.status_code == 200, response.text
        found = response.json()
        assert (found["space_name"], found["property_id"]) == ("Seat 1", p)
        lift = next(a for a in found["amenities"] if a["name"] == "Lift")
        assert lift["is_direct"] is False
        assert lift["sources"] == [
            {
                "level": "BUILDING",
                "place_name": "Tower A",
                "opening_time": None,
                "closing_time": None,
            }
        ]
        # Admin notes and assignment ids stay in the admin API.
        assert "Internal" not in response.text and "assignment_id" not in response.text
        # No mutation through the public prefix.
        assert who.post(url, json={}).status_code == 405
        assert who.delete(url).status_code == 405
    # Unpublished properties stay private.
    api.patch(f"{ADMIN}/properties/{p}", json={"is_published": False})
    assert client.get(url).status_code == 404


def test_search_matches_amenities_at_every_level(
    client: TestClient, api: TestClient, hub: dict, placed: dict
) -> None:
    other = create_property(api, name="Plain Park")
    api.patch(f"{ADMIN}/properties/{other['id']}", json={"is_published": True})
    api.patch(f"{A}/{placed['generator']['id']}", json={"status": "INACTIVE"})

    def listed(amenity: str) -> tuple[list[str], int]:
        page = client.get(f"{PUBLIC}/properties", params={"amenity": amenity}).json()
        return [c["name"] for c in page["items"]], page["total"]

    for amenity in ("Reception", "Lift", "Pantry", "Air conditioning", "Power backup"):
        assert listed(amenity) == (["Sigma Hub"], 1), amenity  # once, whatever the level
    assert listed("Generator") == ([], 0)  # inactive only
    assert listed("Sauna") == ([], 0)
    every = client.get(f"{PUBLIC}/properties").json()
    assert every["total"] == 2
    # Cards keep listing property-level amenities only.
    card = next(c for c in every["items"] if c["name"] == "Sigma Hub")
    assert card["amenities"] == ["Reception", "Wi-Fi"]
    # Pages and totals stay right with the filter.
    page = client.get(
        f"{PUBLIC}/properties", params={"amenity": "Lift", "page": 2, "page_size": 1}
    ).json()
    assert (page["items"], page["total"]) == ([], 1)

    def spaces(query: str) -> list[str]:
        response = client.post(f"{PUBLIC}/space-search", json={"query": query})
        assert response.status_code == 200, response.text
        found = response.json()
        assert found["total"] == len({r["space_id"] for r in found["results"]})
        return sorted(r["space_name"] for r in found["results"])

    assert spaces("seat with power backup") == ["Seat 1", "Seat 2"]  # from Open Bay
    assert spaces("seat with air conditioning") == ["Seat 1", "Seat 2"]  # from Portion 5B
    assert spaces("cabin with air conditioning") == []  # Cabin 1 is in 5A
    assert spaces("cabin with lift") == ["Cabin 1"]  # building
    assert spaces("cabin with pantry and reception") == ["Cabin 1"]  # floor and property
    seat = client.post(f"{PUBLIC}/space-search", json={"query": "seat with lift"}).json()
    one = next(r for r in seat["results"] if r["space_name"] == "Seat 1")
    assert {"Lift", "Power backup", "Air conditioning", "Wi-Fi"} <= set(one["amenities"])
    assert "Generator" not in one["amenities"]


def test_effective_amenities_for_search_take_two_queries_however_many_spaces(
    api: TestClient, hub: dict, placed: dict, db_session: Session
) -> None:
    p = hub["property"]["id"]
    for i in range(8):
        api.post(
            f"{ADMIN}/spaces",
            json={
                "property_id": p,
                "name": f"Desk {i}",
                "space_type": "DESK",
                "parent_space_id": hub["bay"]["id"],
            },
        )
    statements: list[str] = []
    engine = db_session.get_bind()

    def count(*args: object) -> None:
        statements.append(str(args[2]))

    event.listen(engine, "before_cursor_execute", count)
    try:
        found = effective_names_by_space(db_session, [uuid.UUID(p)])
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert len(statements) == 2
    desks = [names for space_id, names in found.items() if space_id != uuid.UUID(hub["bay"]["id"])]
    assert len(found) >= 15
    assert {"Power backup", "Air conditioning", "Lift", "Reception"} <= found[
        uuid.UUID(hub["seat1"]["id"])
    ]
    assert all("Reception" in names for names in desks)
