"""POST /properties/setup: a whole property in one request, all or nothing."""

import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.v1.endpoints.files import get_storage_factory
from app.main import app
from app.models import (
    Asset,
    Building,
    Floor,
    Layout,
    PricingRule,
    Property,
    PropertyAmenity,
    Space,
    StoredFile,
)
from app.storage import LocalStorage
from tests import images
from tests.test_bookings import START, booking_body
from tests.test_resources import V1, post

SETUP = f"{V1}/properties/setup"


@pytest.fixture
def storage(tmp_path: Path) -> Iterator[LocalStorage]:
    local = LocalStorage(tmp_path, "http://testserver")
    app.dependency_overrides[get_storage_factory] = lambda: lambda: local
    yield local
    app.dependency_overrides.pop(get_storage_factory, None)


def sigma_hub() -> dict:
    """Sigma Hub as the wizard sends it. Prices are the user's entries, not defaults."""
    return {
        "property": {
            "name": "Sigma Hub",
            "property_type": "COMMERCIAL",
            "category": "OFFICE",
            "address": "1 Tech Park",
            "city": "Hyderabad",
            "state": "Telangana",
            "postal_code": "500081",
            "area_sqft": 33000,
        },
        "amenities": ["Gym", "Cafeteria", "gym"],
        "buildings": [{"ref": "bA", "name": "Building A"}],
        "floors": [
            {"ref": "f5", "building_ref": "bA", "name": "Floor 5", "level_number": 5},
            {"ref": "f6", "building_ref": "bA", "name": "Floor 6", "level_number": 6},
        ],
        "layouts": [
            {
                "ref": "l5",
                "floor_ref": "f5",
                "name": "Floor 5 plan",
                "grid_columns": 60,
                "grid_rows": 41,
            },
        ],
        "spaces": [
            # Children listed before their parents on purpose: order must not matter.
            {
                "ref": "c1",
                "floor_ref": "f5",
                "parent_ref": "5A",
                "layout_ref": "l5",
                "name": "Cabin 1",
                "space_type": "CABIN",
                "area_sqft": 200,
                "capacity": 4,
                "x": 3,
                "y": 5,
                "width": 7,
                "height": 4,
            },
            {
                "ref": "5A",
                "floor_ref": "f5",
                "layout_ref": "l5",
                "name": "Portion 5A",
                "code": "5A",
                "space_type": "PORTION",
                "area_sqft": 1650,
                "x": 2,
                "y": 1,
                "width": 28,
                "height": 20,
            },
            {
                "ref": "5B",
                "floor_ref": "f5",
                "name": "Portion 5B",
                "code": "5B",
                "space_type": "PORTION",
                "area_sqft": 1650,
            },
            {
                "ref": "bay",
                "floor_ref": "f5",
                "parent_ref": "5B",
                "name": "Open Bay",
                "space_type": "COWORKING",
                "capacity": 20,
            },
            {
                "ref": "s1",
                "floor_ref": "f5",
                "parent_ref": "bay",
                "name": "Seat 1",
                "space_type": "SEAT",
                "capacity": 1,
            },
            {
                "ref": "s2",
                "floor_ref": "f5",
                "parent_ref": "bay",
                "name": "Seat 2",
                "space_type": "SEAT",
                "capacity": 1,
            },
            {
                "ref": "dw",
                "floor_ref": "f5",
                "parent_ref": "5B",
                "name": "Workstation D1",
                "space_type": "DEDICATED_WORKSTATION",
                "capacity": 1,
            },
            {
                "ref": "fw",
                "floor_ref": "f5",
                "parent_ref": "bay",
                "name": "Hot Desk H1",
                "space_type": "FLEXIBLE_WORKSTATION",
                "capacity": 1,
            },
            {
                "ref": "whole6",
                "floor_ref": "f6",
                "name": "Floor 6 (entire)",
                "space_type": "FLOOR",
                "area_sqft": 3300,
                "is_rentable": True,
            },
        ],
        "assets": [
            {"ref": "a1", "location_ref": "c1", "name": "Projector", "category": "IT_EQUIPMENT"},
            {"ref": "a2", "name": "Chiller", "category": "HVAC", "status": "UNDER_MAINTENANCE"},
        ],
        "pricing": [
            {
                "ref": "default",
                "rate_basis": "PER_SQFT",
                "billing_period": "MONTHLY",
                "amount": "70",
                "effective_from": "2026-01-01",
                "status": "ACTIVE",
                "maintenance_amount": "6",
                "maintenance_basis": "PER_SQFT",
            },
            {
                "target_ref": "s1",
                "rate_basis": "PER_SEAT",
                "billing_period": "DAILY",
                "amount": "500",
                "effective_from": "2026-01-01",
                "status": "ACTIVE",
                "payment_frequency": "PER_BOOKING",
            },
            {
                "target_ref": "whole6",
                "rate_basis": "PER_FLOOR",
                "billing_period": "MONTHLY",
                "amount": "220000",
                "effective_from": "2026-01-01",
                "status": "ACTIVE",
                "deposit_rule": "MONTHS_OF_RENT",
                "deposit_value": "3",
            },
        ],
        "images": [],
    }


def send(api: TestClient, setup: dict, files: list[tuple[str, bytes]] = (), expected: int = 201):
    response = api.post(
        SETUP,
        data={"setup": json.dumps(setup)},
        files=[("files", (name, data, "application/octet-stream")) for name, data in files],
    )
    assert response.status_code == expected, response.text
    return response.json()


def counts(db_session: Session) -> dict[str, int]:
    db_session.expire_all()
    return {
        model.__tablename__: db_session.scalar(select(func.count()).select_from(model))
        for model in (
            Property,
            Building,
            Floor,
            Layout,
            Space,
            Asset,
            PropertyAmenity,
            PricingRule,
            StoredFile,
        )
    }


def test_creates_the_whole_property_and_maps_every_ref(
    api: TestClient, db_session: Session, storage: LocalStorage
) -> None:
    setup = sigma_hub()
    setup["images"] = [
        {
            "file_index": 0,
            "owner_ref": "property",
            "kind": "MAIN",
            "is_primary": True,
            "caption": "Front",
        },
        {"file_index": 1, "owner_ref": "c1", "kind": "INTERIOR", "category": "Cabin"},
        {"file_index": 2, "owner_ref": "l5", "kind": "FLOOR_PLAN", "is_primary": True},
    ]
    result = send(
        api,
        setup,
        [("front.png", images.png()), ("cabin.jpg", images.jpeg()), ("plan.pdf", images.pdf())],
    )

    assert result["created"] == {
        "buildings": 1,
        "floors": 2,
        "layouts": 1,
        "spaces": 10,
        "assets": 2,
        "amenities": 2,
        "pricing_rules": 3,
        "derived_rules": 0,
        "files": 3,
    }
    assert result["added"] == [
        'Whole-floor space "Floor 5 (whole floor)", which every floor needs.'
    ]
    ids = result["ids"]
    assert {"bA", "f5", "l5", "5A", "c1", "s1", "a1", "default"} <= set(ids)
    assert result["property"]["amenities"] == ["Cafeteria", "Gym"]

    spaces = {s.name: s for s in db_session.scalars(select(Space))}
    root5 = spaces["Floor 5 (whole floor)"]
    assert (root5.space_type, root5.is_rentable) == ("FLOOR", False)
    assert spaces["Portion 5A"].parent_space_id == root5.id
    assert str(spaces["Cabin 1"].parent_space_id) == ids["5A"]
    assert str(spaces["Seat 1"].parent_space_id) == ids["bay"]
    assert (spaces["Cabin 1"].x, spaces["Cabin 1"].width) == (3, 7)  # sketch geometry kept
    assert str(spaces["Cabin 1"].layout_id) == ids["l5"]
    assert spaces["Floor 6 (entire)"].is_rentable is True

    projector = db_session.scalar(select(Asset).where(Asset.name == "Projector"))
    assert str(projector.space_id) == ids["c1"] and str(projector.floor_id) == ids["f5"]

    covers = {f["original_name"]: f for f in result["files"]}
    assert covers["front.png"]["is_primary"] and covers["front.png"]["caption"] == "Front"
    assert covers["cabin.jpg"]["space_id"] == ids["c1"]
    assert covers["plan.pdf"]["layout_id"] == ids["l5"]

    prices = api.get(f"{V1}/properties/{result['property']['id']}/pricing").json()
    by_name = {s["space_name"]: s for s in prices["spaces"]}
    cabin = next(p for p in by_name["Cabin 1"]["prices"] if p["billing_period"] == "MONTHLY")
    assert (cabin["source"], cabin["estimate"]["rent"], cabin["estimate"]["maintenance"]) == (
        "PROPERTY_DEFAULT",
        14000.0,
        1200.0,
    )
    whole6 = by_name["Floor 6 (entire)"]["prices"][0]
    assert whole6["estimate"]["rent"] == 220000.0 and whole6["estimate"]["deposit"] == 660000.0
    seat = by_name["Seat 1"]["prices"][0]
    assert (seat["billing_period"], seat["estimate"]["rent"]) == ("DAILY", 500.0)


def test_bad_references_are_all_reported_and_nothing_is_saved(
    api: TestClient, db_session: Session
) -> None:
    setup = sigma_hub()
    setup["floors"][1]["building_ref"] = "nowhere"
    setup["spaces"][0]["parent_ref"] = "5B-missing"
    setup["spaces"].append({**setup["spaces"][2], "name": "Copy"})  # ref "5B" twice
    setup["pricing"][1]["target_ref"] = "a1"  # an asset cannot be priced
    setup["images"] = [{"file_index": 4, "kind": "MAIN"}]

    body = send(api, setup, expected=400)
    fields = {e["field"] for e in body["errors"]}
    assert {
        "floors.1.building_ref",
        "spaces.0.parent_ref",
        "spaces.9.ref",
        "pricing.1.target_ref",
        "images.0.file_index",
    } <= fields
    assert "Nothing was saved" in body["detail"]
    assert set(counts(db_session).values()) == {0}


def test_a_refused_record_rolls_back_everything_before_it(
    api: TestClient, db_session: Session
) -> None:
    setup = sigma_hub()
    # Valid on its own, refused by the pricing rules: per seat does not fit a cabin.
    setup["pricing"].append(
        {
            "target_ref": "c1",
            "rate_basis": "PER_SEAT",
            "billing_period": "MONTHLY",
            "amount": "1",
            "effective_from": "2026-01-01",
        }
    )
    body = send(api, setup, expected=400)
    assert body["detail"] == (
        'Price of "Cabin 1": A per seat price does not fit a cabin. Nothing was saved.'
    )
    assert body["errors"] == [
        {"field": "pricing.3", "message": "A per seat price does not fit a cabin."}
    ]
    assert set(counts(db_session).values()) == {0}

    duplicate_level = sigma_hub()
    duplicate_level["floors"][1]["level_number"] = 5
    body = send(api, duplicate_level, expected=409)
    assert body["detail"].startswith('Floor "Floor 6": This building already has a floor at that')
    assert set(counts(db_session).values()) == {0}


def test_stored_image_bytes_are_deleted_when_a_later_image_is_refused(
    api: TestClient, db_session: Session, storage: LocalStorage, tmp_path: Path
) -> None:
    setup = sigma_hub()
    setup["images"] = [
        {"file_index": 0, "kind": "MAIN"},
        {"file_index": 1, "owner_ref": "c1", "kind": "INTERIOR"},
    ]
    body = send(
        api, setup, [("ok.png", images.png()), ("fake.png", b"\x89PNG not really")], expected=415
    )
    assert body["detail"].startswith('Image "fake.png":')
    assert set(counts(db_session).values()) == {0}
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


def test_a_setup_without_images_needs_no_file_storage(api: TestClient) -> None:
    def unavailable():
        raise AssertionError("storage was looked up")

    app.dependency_overrides[get_storage_factory] = lambda: unavailable
    try:
        send(api, sigma_hub())
    finally:
        app.dependency_overrides.pop(get_storage_factory, None)


def test_the_setup_field_is_validated_like_any_request(api: TestClient) -> None:
    setup = sigma_hub()
    setup["spaces"][0]["space_type"] = "SPACESHIP"
    body = send(api, setup, expected=422)
    assert body["errors"][0]["field"] == "setup.spaces.0.space_type"
    assert send(api, {**sigma_hub(), "surprise": 1}, expected=422)["errors"][0]["field"] == (
        "setup.surprise"
    )


def test_a_seat_booking_holds_only_that_seat(api: TestClient) -> None:
    result = send(api, sigma_hub())
    ids = result["ids"]
    prop = result["property"]["id"]

    def space(ref: str) -> dict:
        return {"id": ids[ref], "property_id": prop}

    post(api, "bookings", booking_body(space("s1")))
    # The next seat, and the hot desk in the same bay, stay free for the same hour.
    post(api, "bookings", booking_body(space("s2")))
    post(api, "bookings", booking_body(space("fw")))
    # The seat itself, and the bay or portion containing it, are taken.
    post(api, "bookings", booking_body(space("s1")), 409)
    refused = post(api, "bookings", booking_body(space("bay")), 409)
    assert "inside this space" in refused["detail"]
    post(api, "bookings", booking_body(space("5B")), 409)
    # Outside that hour the seat is free again.
    post(api, "bookings", booking_body(space("s1"), START + timedelta(hours=2)))
