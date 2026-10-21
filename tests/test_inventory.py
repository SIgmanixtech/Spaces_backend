"""The inventory hierarchy: property -> building -> floor -> space, and spaces within spaces."""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Space
from app.models.enums import SpaceType
from tests.test_properties import create_property
from tests.test_resources import V1, post

SPACES = f"{V1}/spaces"


def make_floor(api: TestClient, prop: dict, building: str = "Block A", level: int = 1) -> dict:
    block = post(api, "buildings", {"property_id": prop["id"], "name": building})
    return post(
        api, "floors", {"building_id": block["id"], "name": f"Level {level}", "level_number": level}
    )


def space(api: TestClient, prop: dict, space_type: str, name: str, expected: int = 201, **extra):
    body = {"property_id": prop["id"], "name": name, "space_type": space_type, **extra}
    return post(api, "spaces", body, expected)


def listed(api: TestClient, **params: object) -> list[str]:
    response = api.get(SPACES, params={"page_size": 100, **params})
    assert response.status_code == 200, response.text
    return [item["name"] for item in response.json()["items"]]


@pytest.fixture
def prop(api: TestClient) -> dict:
    return create_property(api)


@pytest.fixture
def floor(api: TestClient, prop: dict) -> dict:
    return make_floor(api, prop)


@pytest.fixture
def root(api: TestClient, prop: dict, floor: dict) -> dict:
    return space(api, prop, "FLOOR", "Whole floor", floor_id=floor["id"])


# --- unassigned spaces: everything that existed before the hierarchy ----------


def test_space_without_a_floor_is_unassigned_and_otherwise_unchanged(
    api: TestClient, prop: dict
) -> None:
    created = space(api, prop, "MEETING_ROOM", "Boardroom", code="MR-1", capacity=8)
    assert created["floor_id"] is None
    assert created["building_id"] is None
    assert created["parent_space_id"] is None
    assert created["depth"] == 0
    assert created["is_rentable"] is True
    assert created["area_sqft"] is None
    # The fields the frontend already reads are all still there.
    for field in ("layout_id", "status", "operational_status", "x", "y", "width", "height"):
        assert field in created
    assert listed(api, property_id=prop["id"]) == ["Boardroom"]
    assert listed(api, unassigned="true") == ["Boardroom"]
    assert listed(api, unassigned="false") == []


# --- building the hierarchy -----------------------------------------------------


def test_spaces_nest_under_the_whole_floor_space(
    api: TestClient, prop: dict, floor: dict, root: dict
) -> None:
    assert (root["depth"], root["parent_space_id"]) == (0, None)
    assert root["floor_id"] == floor["id"]
    assert root["building_id"] == floor["building_id"]

    portion = space(api, prop, "PORTION", "East wing", floor_id=floor["id"], area_sqft=1450)
    office = space(api, prop, "PRIVATE_OFFICE", "Office 1", parent_space_id=portion["id"])
    desk = space(api, prop, "DESK", "Desk 1", parent_space_id=office["id"], floor_id=floor["id"])

    # With only a floor given, the space goes directly under the whole-floor space.
    assert (portion["parent_space_id"], portion["depth"]) == (root["id"], 1)
    assert portion["area_sqft"] == 1450
    assert (office["parent_space_id"], office["depth"]) == (portion["id"], 2)
    assert (desk["parent_space_id"], desk["depth"]) == (office["id"], 3)
    # Floor and building are inherited from the parent.
    assert {office["floor_id"], desk["floor_id"]} == {floor["id"]}
    assert {office["building_id"], desk["building_id"]} == {floor["building_id"]}


def test_a_floor_needs_its_whole_floor_space_before_other_spaces(
    api: TestClient, prop: dict, floor: dict
) -> None:
    refused = space(api, prop, "DESK", "Desk 1", 409, floor_id=floor["id"])
    assert "no whole-floor space yet" in refused["detail"]


def test_whole_floor_space_rules(api: TestClient, prop: dict, floor: dict, root: dict) -> None:
    duplicate = space(api, prop, "FLOOR", "Again", 409, floor_id=floor["id"])
    assert duplicate["detail"] == "This floor already has a whole-floor space."
    assert "needs a floor_id" in space(api, prop, "FLOOR", "Nowhere", 400)["detail"]
    nested = space(api, prop, "FLOOR", "Nested", 400, parent_space_id=root["id"])
    assert "cannot have a parent" in nested["detail"]


@pytest.mark.parametrize(
    ("child", "parent", "allowed"),
    [
        ("PORTION", "FLOOR", True),
        ("PORTION", "PORTION", False),
        ("MEETING_ROOM", "PORTION", True),
        ("CONFERENCE_ROOM", "FLOOR", True),
        ("TRAINING_ROOM", "PRIVATE_OFFICE", False),
        ("NON_RENTABLE", "PORTION", True),
        ("DESK", "COWORKING", True),
        ("DESK", "PRIVATE_OFFICE", True),
        ("DESK", "MEETING_ROOM", False),
        ("DESK", "DESK", False),
        ("CABIN", "CABIN", False),
    ],
)
def test_only_sensible_parent_child_combinations_are_accepted(
    api: TestClient, prop: dict, floor: dict, root: dict, child: str, parent: str, allowed: bool
) -> None:
    containers = {"FLOOR": root}
    containers["PORTION"] = space(api, prop, "PORTION", "Portion", floor_id=floor["id"])
    if parent not in containers:
        containers[parent] = space(
            api, prop, parent, "Container", parent_space_id=containers["PORTION"]["id"]
        )
    result = space(
        api, prop, child, "Child", 201 if allowed else 400, parent_space_id=containers[parent]["id"]
    )
    if not allowed:
        assert "cannot be placed inside" in result["detail"]


# --- same-property enforcement --------------------------------------------------


def test_floor_and_parent_must_belong_to_the_spaces_property(
    api: TestClient, prop: dict, floor: dict, root: dict
) -> None:
    other = create_property(api, name="Other Tower")
    other_floor = make_floor(api, other)
    other_root = space(api, other, "FLOOR", "Other floor", floor_id=other_floor["id"])

    wrong_floor = space(api, prop, "FLOOR", "Stray", 400, floor_id=other_floor["id"])
    assert wrong_floor["detail"] == "The floor does not belong to this property."
    wrong_parent = space(api, prop, "PORTION", "Stray", 400, parent_space_id=other_root["id"])
    assert wrong_parent["detail"] == "The parent space does not belong to this property."

    second_floor = make_floor(api, prop, building="Block B")
    mismatch = space(
        api, prop, "PORTION", "Stray", 400, floor_id=second_floor["id"], parent_space_id=root["id"]
    )
    assert mismatch["detail"] == "The parent space is on a different floor."

    assert space(api, prop, "DESK", "X", 404, floor_id=str(uuid.uuid4()))["detail"] == (
        "Floor not found."
    )
    assert space(api, prop, "DESK", "X", 404, parent_space_id=str(uuid.uuid4()))["detail"] == (
        "Parent space not found."
    )


def test_an_unassigned_space_cannot_be_a_parent(api: TestClient, prop: dict) -> None:
    legacy = space(api, prop, "COWORKING", "Legacy zone")
    refused = space(api, prop, "DESK", "Desk", 400, parent_space_id=legacy["id"])
    assert refused["detail"] == "The parent space is not assigned to a floor yet."


# --- rentable flag and reclassification ------------------------------------------


def test_non_rentable_spaces(api: TestClient, prop: dict, floor: dict, root: dict) -> None:
    corridor = space(api, prop, "NON_RENTABLE", "Corridor", floor_id=floor["id"])
    assert corridor["is_rentable"] is False
    assert (
        "cannot be marked rentable"
        in (
            space(api, prop, "NON_RENTABLE", "Lobby", 400, floor_id=floor["id"], is_rentable=True)[
                "detail"
            ]
        )
    )
    patched = api.patch(f"{SPACES}/{corridor['id']}", json={"is_rentable": True})
    assert patched.status_code == 400

    cabin = space(api, prop, "CABIN", "Store", floor_id=floor["id"])
    reclassified = api.patch(f"{SPACES}/{cabin['id']}", json={"space_type": "NON_RENTABLE"}).json()
    assert (reclassified["space_type"], reclassified["is_rentable"]) == ("NON_RENTABLE", False)
    assert api.patch(f"{SPACES}/{cabin['id']}", json={"is_rentable": None}).status_code == 422


def test_reclassifying_respects_parent_and_children(
    api: TestClient, prop: dict, floor: dict, root: dict
) -> None:
    office = space(api, prop, "PRIVATE_OFFICE", "Office", floor_id=floor["id"])
    space(api, prop, "DESK", "Desk", parent_space_id=office["id"])
    cabin = space(api, prop, "CABIN", "Cabin", floor_id=floor["id"])

    def retype(target: dict, space_type: str) -> tuple[int, str]:
        response = api.patch(f"{SPACES}/{target['id']}", json={"space_type": space_type})
        return response.status_code, response.json().get("detail", "")

    # The office holds a desk, and a desk cannot sit in a meeting room.
    assert retype(office, "MEETING_ROOM") == (
        400,
        "A desk cannot be placed inside a meeting room.",
    )
    assert retype(office, "COWORKING")[0] == 200
    assert retype(cabin, "MEETING_ROOM")[0] == 200
    assert retype(cabin, "FLOOR")[0] == 400
    assert retype(root, "PORTION")[0] == 400
    # PATCH cannot move a space.
    assert api.patch(f"{SPACES}/{cabin['id']}", json={"floor_id": None}).status_code == 422


# --- listing ----------------------------------------------------------------------


def test_listing_hides_structural_spaces_unless_asked(
    api: TestClient, prop: dict, floor: dict, root: dict
) -> None:
    portion = space(api, prop, "PORTION", "East wing", floor_id=floor["id"])
    space(api, prop, "DESK", "Desk 1", parent_space_id=portion["id"])
    space(api, prop, "NON_RENTABLE", "Corridor", floor_id=floor["id"])
    space(api, prop, "CABIN", "Legacy cabin")

    assert listed(api) == ["Desk 1", "East wing", "Legacy cabin"]
    assert listed(api, include_structural="true") == [
        "Corridor",
        "Desk 1",
        "East wing",
        "Legacy cabin",
        "Whole floor",
    ]
    assert listed(api, floor_id=floor["id"]) == ["Desk 1", "East wing"]
    assert listed(api, building_id=floor["building_id"], unassigned="false") == [
        "Desk 1",
        "East wing",
    ]
    assert listed(api, parent_space_id=portion["id"]) == ["Desk 1"]
    assert listed(api, parent_space_id=root["id"], include_structural="true") == [
        "Corridor",
        "East wing",
    ]
    assert listed(api, is_rentable="false", include_structural="true") == ["Corridor"]
    assert listed(api, space_type="FLOOR", include_structural="true") == ["Whole floor"]
    assert listed(api, unassigned="true") == ["Legacy cabin"]


def test_dashboard_counts_only_lettable_spaces(
    api: TestClient, prop: dict, floor: dict, root: dict
) -> None:
    space(api, prop, "DESK", "Desk 1", floor_id=floor["id"])
    space(api, prop, "NON_RENTABLE", "Corridor", floor_id=floor["id"])

    summary = api.get(f"{V1}/property/dashboard/summary").json()
    assert summary["spaces"]["total"] == 1
    availability = api.get(f"{V1}/property/dashboard/availability").json()
    assert [(row["available_spaces"], row["total_spaces"]) for row in availability] == [(1, 1)]


# --- moving and assigning ---------------------------------------------------------


def move(api: TestClient, target: dict, expected: int = 200, **body: object) -> dict:
    response = api.post(f"{SPACES}/{target['id']}/move", json=body)
    assert response.status_code == expected, response.text
    return response.json()


def test_an_unassigned_space_can_be_assigned_to_a_floor(
    api: TestClient, prop: dict, floor: dict, root: dict
) -> None:
    legacy = space(api, prop, "MEETING_ROOM", "Legacy room", code="MR-9")
    portion = space(api, prop, "PORTION", "East wing", floor_id=floor["id"])

    onto_floor = move(api, legacy, floor_id=floor["id"])
    assert (onto_floor["id"], onto_floor["code"]) == (legacy["id"], "MR-9")  # same record
    assert (onto_floor["parent_space_id"], onto_floor["depth"]) == (root["id"], 1)
    assert onto_floor["building_id"] == floor["building_id"]

    into_portion = move(api, legacy, parent_space_id=portion["id"])
    assert (into_portion["parent_space_id"], into_portion["depth"]) == (portion["id"], 2)
    assert listed(api, unassigned="true") == []


def test_assigning_keeps_existing_bookings(
    api: TestClient, prop: dict, floor: dict, root: dict
) -> None:
    legacy = space(api, prop, "MEETING_ROOM", "Legacy room")
    start = (datetime.now(UTC) + timedelta(days=3)).replace(minute=0, second=0, microsecond=0)
    booking = post(
        api,
        "bookings",
        {
            "property_id": prop["id"],
            "space_id": legacy["id"],
            "customer_name": "Priya Nair",
            "start_at": start.isoformat(),
            "end_at": (start + timedelta(hours=1)).isoformat(),
        },
    )
    # First assignment is allowed even with an open booking...
    move(api, legacy, floor_id=floor["id"])
    assert api.get(f"{V1}/bookings/{booking['id']}").json()["space_id"] == legacy["id"]
    # ...but once on a floor, the space stays put while the booking is open.
    portion = space(api, prop, "PORTION", "East wing", floor_id=floor["id"])
    blocked = move(api, legacy, 409, parent_space_id=portion["id"])
    assert "cannot be moved" in blocked["detail"]

    api.post(f"{V1}/bookings/{booking['id']}/cancel")
    move(api, legacy, parent_space_id=portion["id"])


def test_a_current_occupancy_inside_the_subtree_blocks_a_move(
    api: TestClient, prop: dict, floor: dict, root: dict
) -> None:
    portion = space(api, prop, "PORTION", "East wing", floor_id=floor["id"])
    office = space(api, prop, "PRIVATE_OFFICE", "Office", parent_space_id=portion["id"])
    tenant = post(api, "tenants", {"name": "Acme Ltd"})
    post(
        api,
        f"tenants/{tenant['id']}/occupancies",
        {
            "property_id": prop["id"],
            "space_id": office["id"],
            "start_date": date.today().isoformat(),
        },
    )
    second_floor = make_floor(api, prop, building="Block B")
    space(api, prop, "FLOOR", "Whole floor B", floor_id=second_floor["id"])
    assert "cannot be moved" in move(api, portion, 409, floor_id=second_floor["id"])["detail"]


def test_moving_takes_the_whole_subtree_to_the_new_floor(
    api: TestClient, prop: dict, floor: dict, root: dict
) -> None:
    portion = space(api, prop, "PORTION", "East wing", floor_id=floor["id"])
    office = space(api, prop, "PRIVATE_OFFICE", "Office", parent_space_id=portion["id"])
    desk = space(api, prop, "DESK", "Desk", parent_space_id=office["id"])
    second_floor = make_floor(api, prop, building="Block B")
    second_root = space(api, prop, "FLOOR", "Whole floor B", floor_id=second_floor["id"])

    moved = move(api, office, floor_id=second_floor["id"])
    assert (moved["parent_space_id"], moved["depth"]) == (second_root["id"], 1)
    moved_desk = api.get(f"{SPACES}/{desk['id']}").json()
    assert (moved_desk["parent_space_id"], moved_desk["depth"]) == (office["id"], 2)
    assert moved_desk["floor_id"] == second_floor["id"]
    assert moved_desk["building_id"] == second_floor["building_id"]
    assert listed(api, floor_id=floor["id"]) == ["East wing"]


def test_invalid_moves_are_rejected(api: TestClient, prop: dict, floor: dict, root: dict) -> None:
    portion = space(api, prop, "PORTION", "East wing", floor_id=floor["id"])
    office = space(api, prop, "PRIVATE_OFFICE", "Office", parent_space_id=portion["id"])
    desk = space(api, prop, "DESK", "Desk", parent_space_id=office["id"])
    other = create_property(api, name="Other Tower")
    other_floor = make_floor(api, other)
    space(api, other, "FLOOR", "Other floor", floor_id=other_floor["id"])

    # Cycles: into itself, or into something inside itself.
    assert "inside itself" in move(api, office, 400, parent_space_id=office["id"])["detail"]
    assert "inside itself" in move(api, portion, 400, parent_space_id=desk["id"])["detail"]
    # Another property.
    assert move(api, office, 400, floor_id=other_floor["id"])["detail"] == (
        "The floor does not belong to this property."
    )
    # Type rules still apply at the destination.
    cabin = space(api, prop, "CABIN", "Cabin", floor_id=floor["id"])
    assert move(api, office, 400, parent_space_id=cabin["id"])["detail"] == (
        "A private office cannot be placed inside a cabin."
    )
    assert "cannot be moved" in move(api, root, 400, floor_id=floor["id"])["detail"]
    assert api.post(f"{SPACES}/{office['id']}/move", json={}).status_code == 422
    missing = api.post(f"{SPACES}/{uuid.uuid4()}/move", json={"floor_id": floor["id"]})
    assert missing.status_code == 404
    # Nothing moved.
    assert api.get(f"{SPACES}/{office['id']}").json()["parent_space_id"] == portion["id"]


# --- archive ----------------------------------------------------------------------


def test_archive_respects_the_hierarchy(
    api: TestClient, prop: dict, floor: dict, root: dict
) -> None:
    portion = space(api, prop, "PORTION", "East wing", floor_id=floor["id"])
    desk = space(api, prop, "DESK", "Desk", parent_space_id=portion["id"])

    blocked = api.delete(f"{SPACES}/{portion['id']}")
    assert blocked.status_code == 409
    assert "contains other spaces" in blocked.json()["detail"]

    for target in (desk, portion):
        assert api.delete(f"{SPACES}/{target['id']}").status_code == 204
    assert listed(api, floor_id=floor["id"]) == []
    assert listed(api, floor_id=floor["id"], include_archived="true") == ["Desk", "East wing"]
    # An archived space is no longer a valid parent.
    assert space(api, prop, "DESK", "Late", 404, parent_space_id=portion["id"])["detail"] == (
        "Parent space not found."
    )

    # The whole-floor space can be archived once empty, and then replaced.
    assert api.delete(f"{SPACES}/{root['id']}").status_code == 204
    replacement = space(api, prop, "FLOOR", "Whole floor (new)", floor_id=floor["id"])
    assert replacement["id"] != root["id"]


def test_floor_and_building_with_spaces_cannot_be_deleted(
    api: TestClient, prop: dict, floor: dict, root: dict
) -> None:
    assert api.delete(f"{V1}/floors/{floor['id']}").status_code == 409
    assert api.delete(f"{V1}/buildings/{floor['building_id']}").status_code == 409


def test_layout_must_be_on_the_spaces_floor(
    api: TestClient, prop: dict, floor: dict, root: dict
) -> None:
    second_floor = make_floor(api, prop, building="Block B")
    layout = post(
        api,
        "layouts",
        {
            "property_id": prop["id"],
            "name": "Plan B",
            "building_id": second_floor["building_id"],
            "floor_id": second_floor["id"],
        },
    )
    refused = space(api, prop, "DESK", "Desk", 400, floor_id=floor["id"], layout_id=layout["id"])
    assert refused["detail"] == "The layout belongs to a different floor than the space."
    # An unassigned space may still sit on any layout of its property, as before.
    assert space(api, prop, "DESK", "Legacy desk", layout_id=layout["id"])["layout_id"]


# --- database rules: these hold even if the service is bypassed -----------------


def _ids(api: TestClient) -> dict[str, uuid.UUID]:
    prop = create_property(api)
    floor = make_floor(api, prop)
    root = space(api, prop, "FLOOR", "Whole floor", floor_id=floor["id"])
    portion = space(api, prop, "PORTION", "P1", floor_id=floor["id"])
    desk = space(api, prop, "DESK", "D1", parent_space_id=portion["id"])
    other = create_property(api, name="Other Tower")
    other_floor = make_floor(api, other)
    return {
        "property": uuid.UUID(prop["id"]),
        "building": uuid.UUID(floor["building_id"]),
        "floor": uuid.UUID(floor["id"]),
        "root": uuid.UUID(root["id"]),
        "portion": uuid.UUID(portion["id"]),
        "desk": uuid.UUID(desk["id"]),
        "other_building": uuid.UUID(other_floor["building_id"]),
        "other_floor": uuid.UUID(other_floor["id"]),
    }


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        # A floor of another property: the building does not belong to this property.
        (
            {"building_id": "other_building", "floor_id": "other_floor"},
            "fk_spaces_building_id_buildings",
        ),
        # A floor that is not in the stated building.
        ({"floor_id": "other_floor"}, "fk_spaces_floor_id_floors"),
        ({"building_id": None}, "ck_spaces_floor_building_pair"),
        ({"parent_space_id": None, "parent_depth": None}, "ck_spaces_hierarchy_shape"),
        ({"space_type": SpaceType.FLOOR}, "ck_spaces_hierarchy_shape"),
        ({"depth": 3}, "ck_spaces_depth_valid"),
        ({"depth": 5, "parent_depth": 4}, "ck_spaces_depth_valid"),
        ({"parent_depth": None}, "ck_spaces_parent_depth_pair"),
        # Claims a parent depth the parent does not have.
        ({"depth": 3, "parent_depth": 2}, "fk_spaces_parent_space_id_spaces"),
        ({"space_type": SpaceType.NON_RENTABLE}, "ck_spaces_non_rentable"),
        ({"area_sqft": 0}, "ck_spaces_area_sqft_positive"),
        (
            {"space_type": SpaceType.FLOOR, "parent_space_id": None, "parent_depth": None}
            | {"depth": 0},
            "uq_spaces_floor_root",
        ),
    ],
)
def test_database_rejects_invalid_rows(
    api: TestClient, db_session: Session, overrides: dict, constraint: str
) -> None:
    ids = _ids(api)
    values: dict[str, object] = {
        "property_id": ids["property"],
        "building_id": ids["building"],
        "floor_id": ids["floor"],
        "parent_space_id": ids["root"],
        "depth": 1,
        "parent_depth": 0,
        "name": "Direct insert",
        "space_type": SpaceType.DESK,
    }
    for key, value in overrides.items():
        values[key] = ids.get(value, value) if isinstance(value, str) else value
    db_session.add(Space(**values))
    with pytest.raises(IntegrityError) as raised:
        db_session.commit()
    assert raised.value.orig.diag.constraint_name == constraint


def test_database_rejects_a_cycle(api: TestClient, db_session: Session) -> None:
    ids = _ids(api)
    # Portion under its own desk: whichever depth it claims, one link is broken.
    for depth, parent_depth in [(3, 2), (1, 0)]:
        db_session.execute(
            text(
                "UPDATE spaces SET parent_space_id = :desk, depth = :d, parent_depth = :pd"
                " WHERE id = :portion"
            ),
            {"desk": ids["desk"], "d": depth, "pd": parent_depth, "portion": ids["portion"]},
        )
        with pytest.raises(IntegrityError) as raised:
            db_session.commit()
        assert raised.value.orig.diag.constraint_name == "fk_spaces_parent_space_id_spaces"
        db_session.rollback()


def test_database_rejects_moving_a_space_to_another_property(
    api: TestClient, db_session: Session
) -> None:
    ids = _ids(api)
    other_property = db_session.execute(
        text("SELECT property_id FROM buildings WHERE id = :b"), {"b": ids["other_building"]}
    ).scalar_one()
    with pytest.raises(IntegrityError) as raised:
        db_session.execute(
            text("UPDATE spaces SET property_id = :p WHERE id = :desk"),
            {"p": other_property, "desk": ids["desk"]},
        )
    # Since 0004 a trigger refuses this first; the composite foreign keys still back it up.
    assert raised.value.orig.diag.constraint_name == "ck_spaces_property_immutable"
