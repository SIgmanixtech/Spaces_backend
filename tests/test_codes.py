# ruff: noqa: F811  (pytest fixtures shared from other test modules)
"""Codes are generated, never typed (owner request of 16 Nov 2026)."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.property import Property
from app.models.space import Space
from tests.test_client_api import hub  # noqa: F401
from tests.test_properties import create_property
from tests.test_resources import V1, post


def test_properties_get_the_next_code_and_a_given_code_is_kept(api: TestClient) -> None:
    first = create_property(api, name="Alpha")
    second = create_property(api, name="Beta")
    assert (first["code"], second["code"]) == ("PROP-0001", "PROP-0002")
    given = create_property(api, name="Gamma", code="PROP-0007")
    assert given["code"] == "PROP-0007"
    # Numbering continues after the highest code of the series: nothing is reused.
    assert create_property(api, name="Delta")["code"] == "PROP-0008"
    other = create_property(api, name="Epsilon", code="SH-1")
    assert other["code"] == "SH-1"
    assert create_property(api, name="Zeta")["code"] == "PROP-0009"


def test_spaces_are_numbered_per_type_within_their_property(
    api: TestClient, hub: dict, db_session: Session
) -> None:
    codes = {
        s.name: s.code
        for s in db_session.scalars(select(Space).where(Space.property_id == hub["property"]["id"]))
    }
    assert codes == {
        "Floor 5": "FL-001",
        "Portion 5A": "PT-001",
        "Portion 5B": "PT-002",
        "Cabin 1": "CB-001",
        "Open Bay": "CW-001",
        "Seat 1": "ST-001",
        "Seat 2": "ST-002",
    }
    # Another property starts its own series.
    other = create_property(api, name="Other Hub")
    cabin = post(
        api, "spaces", {"property_id": other["id"], "name": "Cabin A", "space_type": "CABIN"}
    )
    assert cabin["code"] == "CB-001"
    # The next cabin here continues the series; a code given by hand is kept.
    more = post(
        api,
        "spaces",
        {"property_id": hub["property"]["id"], "name": "Cabin 2", "space_type": "CABIN"},
    )
    assert more["code"] == "CB-002"
    named = post(
        api,
        "spaces",
        {
            "property_id": hub["property"]["id"],
            "name": "Cabin 3",
            "space_type": "CABIN",
            "code": "VIP-1",
        },
    )
    assert named["code"] == "VIP-1"


def test_assets_get_a_tag(api: TestClient, hub: dict) -> None:
    pid = hub["property"]["id"]
    first = post(api, "assets", {"property_id": pid, "name": "Projector", "category": "AV"})
    second = post(api, "assets", {"property_id": pid, "name": "Chair", "category": "FURNITURE"})
    assert (first["asset_tag"], second["asset_tag"]) == ("AST-00001", "AST-00002")


def test_several_new_records_in_one_save_get_different_codes_in_name_order(
    db_session: Session,
) -> None:
    prop = Property(
        name="Batch",
        property_type="OFFICE",
        category="COMMERCIAL",
        address="1 Road",
        city="Hyderabad",
        state="Telangana",
        postal_code="500001",
    )
    db_session.add(prop)
    db_session.flush()
    seats = [Space(property_id=prop.id, name=f"Seat {n}", space_type="SEAT") for n in (10, 2, 1)]
    db_session.add_all(seats)
    db_session.flush()
    assert {s.name: s.code for s in seats} == {
        "Seat 1": "ST-001",
        "Seat 2": "ST-002",
        "Seat 10": "ST-003",
    }
    assert prop.code == "PROP-0001"


def test_the_property_setup_gives_every_record_a_code(api: TestClient) -> None:
    from tests.test_property_setup import send, sigma_hub  # the wizard's request

    made = send(api, sigma_hub())
    prop = api.get(f"{V1}/properties", params={"search": "Sigma Hub"}).json()["items"][0]
    assert prop["code"].startswith("PROP-"), made
    spaces = api.get(f"{V1}/spaces", params={"property_id": prop["id"], "page_size": 100})
    codes = [s["code"] for s in spaces.json()["items"]]
    assert codes and all(codes) and len(set(codes)) == len(codes)
    assets = api.get(f"{V1}/assets", params={"property_id": prop["id"]}).json()["items"]
    assert all(a["asset_tag"] for a in assets)
