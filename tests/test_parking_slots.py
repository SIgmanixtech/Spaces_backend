# ruff: noqa: F811  (pytest fixtures shared from other test modules)
"""Parking slots (BR-032, D18): an ordinary rentable space type, priced by the existing
pricing rules and held by the existing allocation engine. Needs the migrated test
database. Sample property "Sigma Hub" comes from test_client_api."""

from fastapi.testclient import TestClient

from app.models.enums import RateBasis, SpaceType
from app.services.lead_import_files import SPACE_TYPE_LABEL
from app.services.pricing import FIXED_BASIS_TYPES
from app.services.spaces import ALLOWED_PARENTS
from tests.test_bookings import book
from tests.test_client_api import hub  # noqa: F401
from tests.test_resources import V1, post


def slot(
    api: TestClient, hub: dict, name: str = "P-01", expected: int = 201, **extra: object
) -> dict:
    return post(
        api,
        "spaces",
        {
            "property_id": hub["property"]["id"],
            "name": name,
            "space_type": "PARKING_SLOT",
            "parent_space_id": hub["5B"]["id"],
            **extra,
        },
        expected,
    )


def test_parking_slot_rules_reuse_the_existing_maps() -> None:
    assert SpaceType.PARKING_SLOT in FIXED_BASIS_TYPES[RateBasis.PER_UNIT]
    assert ALLOWED_PARENTS[SpaceType.PARKING_SLOT] == {SpaceType.FLOOR, SpaceType.PORTION}
    assert SPACE_TYPE_LABEL[SpaceType.PARKING_SLOT] == "Parking slot"


def test_a_parking_slot_is_a_rentable_space_on_a_floor_or_portion(
    api: TestClient, hub: dict
) -> None:
    made = slot(api, hub)
    assert (made["space_type"], made["is_rentable"]) == ("PARKING_SLOT", True)
    # Not inside a cabin or other room: the same parent rules as any space.
    refused = api.post(
        f"{V1}/spaces",
        json={
            "property_id": hub["property"]["id"],
            "name": "P-03",
            "space_type": "PARKING_SLOT",
            "parent_space_id": hub["cabin"]["id"],
        },
    )
    assert refused.status_code in (400, 409, 422), refused.text


def test_a_parking_slot_is_priced_per_unit_and_booked_once_at_a_time(
    api: TestClient, hub: dict
) -> None:
    made = slot(api, hub)
    post(
        api,
        "pricing-rules",
        {
            "property_id": hub["property"]["id"],
            "rate_basis": "PER_UNIT",
            "billing_period": "MONTHLY",
            "amount": "1500",
            "applies_to": "PARKING_SLOT",
            "effective_from": "2026-01-01",
            "status": "ACTIVE",
        },
    )
    first = book(api, made, customer_name="Ravi")
    assert first["space_name"] == "P-01"
    # The allocation engine holds it: an overlapping booking is refused.
    clash = api.post(
        f"{V1}/bookings",
        json={
            "property_id": made["property_id"],
            "space_id": made["id"],
            "customer_name": "Asha",
            "start_at": first["start_at"],
            "end_at": first["end_at"],
        },
    )
    assert clash.status_code == 409, clash.text


def test_leads_can_ask_for_a_parking_slot(api: TestClient) -> None:
    made = api.post(
        f"{V1}/admin/crm/leads", json={"company_name": "Acme", "space_type": "PARKING_SLOT"}
    )
    assert made.status_code == 201 and made.json()["space_type"] == "PARKING_SLOT"
