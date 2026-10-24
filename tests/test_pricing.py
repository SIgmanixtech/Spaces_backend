"""Pricing rules: the calculation, inheritance and overrides, overlap protection, and the API.

Sigma Hub's figures (₹70 per sq ft per month, ₹6 maintenance) are entered here as
test data only; nothing in the application creates them.
"""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.errors import BadRequestError
from app.services.pricing import calculate
from tests.test_properties import create_property
from tests.test_resources import V1, post

RULES = f"{V1}/pricing-rules"


def terms(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "rate_basis": "PER_SQFT",
        "billing_period": "MONTHLY",
        "amount": Decimal("70"),
        "currency": "INR",
        "maintenance_amount": None,
        "maintenance_basis": None,
        "tax_treatment": "EXEMPT",
        "tax_rate": None,
        "additional_charges": [],
        "deposit_rule": "NONE",
        "deposit_value": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


# --- the calculation, without a database ----------------------------------------------


def test_area_times_rate_only_for_a_per_sqft_basis() -> None:
    per_sqft = calculate(terms(), area=3300, persons=None)
    assert per_sqft.rent == Decimal("231000.00")
    assert per_sqft.measure == Decimal("3300")

    fixed = calculate(terms(rate_basis="PER_CABIN", amount=Decimal("25000")), area=3300, persons=4)
    assert fixed.rent == Decimal("25000.00")
    assert fixed.measure is None

    per_person = calculate(
        terms(rate_basis="PER_PERSON", amount=Decimal("6000")), area=None, persons=4
    )
    assert per_person.rent == Decimal("24000.00")


def test_maintenance_tax_and_deposit_are_separate_and_the_deposit_is_not_recurring() -> None:
    sigma = calculate(
        terms(
            maintenance_amount=Decimal("6"),
            maintenance_basis="PER_SQFT",
            tax_treatment="ADDITIONAL",
            tax_rate=Decimal("18"),
            additional_charges=[{"label": "Parking", "amount": "1500"}],
            deposit_rule="MONTHS_OF_RENT",
            deposit_value=Decimal("3"),
        ),
        area=1650,
        persons=None,
    )
    assert sigma.rent == Decimal("115500.00")
    assert sigma.maintenance == Decimal("9900.00")
    assert sigma.additional_charges == Decimal("1500.00")
    # 18% of rent + maintenance + charges, once.
    assert sigma.tax == Decimal("22842.00")
    assert sigma.recurring_total == Decimal("149742.00")
    # Three months of rent only, outside the recurring total.
    assert sigma.deposit == Decimal("346500.00")


def test_included_tax_is_shown_but_not_added_again() -> None:
    charges = calculate(
        terms(
            rate_basis="PER_ROOM",
            amount=Decimal("1180"),
            tax_treatment="INCLUDED",
            tax_rate=Decimal("18"),
            billing_period="HOURLY",
        ),
        area=None,
        persons=None,
    )
    assert charges.tax == Decimal("180.00")
    assert charges.tax_included is True
    assert charges.recurring_total == Decimal("1180.00")


def test_quantity_and_rounding_half_up_to_the_paisa() -> None:
    hourly = calculate(
        terms(rate_basis="PER_ROOM", amount=Decimal("333.335"), billing_period="HOURLY"),
        area=None,
        persons=None,
        quantity=1,
    )
    assert hourly.rent == Decimal("333.34")
    three = calculate(
        terms(rate_basis="PER_ROOM", amount=Decimal("500"), billing_period="HOURLY"),
        area=None,
        persons=None,
        quantity=3,
    )
    assert three.rent == Decimal("1500.00")
    assert three.quantity == Decimal("3")


def test_a_rate_that_needs_an_area_refuses_to_guess() -> None:
    with pytest.raises(BadRequestError, match="area"):
        calculate(terms(), area=None, persons=None)


# --- through the API ------------------------------------------------------------------


@pytest.fixture
def hub(api: TestClient) -> dict[str, dict]:
    """Sigma Hub > Tower A > Floor 5 > Portion 5A (1650 sq ft) > Cabin 1 (200 sq ft),
    a meeting room, a coworking area with two seats; and a non-rentable corridor."""
    prop = create_property(api, name="Sigma Hub")
    building = post(api, "buildings", {"property_id": prop["id"], "name": "Tower A"})
    floor = post(api, "floors", {"building_id": building["id"], "name": "Fifth", "level_number": 5})

    def space(name: str, space_type: str, **extra: object) -> dict:
        return post(
            api,
            "spaces",
            {"property_id": prop["id"], "name": name, "space_type": space_type, **extra},
        )

    root = space("Floor 5", "FLOOR", floor_id=floor["id"], area_sqft=3300, is_rentable=False)
    portion = space("Portion 5A", "PORTION", parent_space_id=root["id"], area_sqft=1650)
    cabin = space("Cabin 1", "CABIN", parent_space_id=portion["id"], area_sqft=200, capacity=4)
    meeting = space("Meeting Room", "MEETING_ROOM", parent_space_id=portion["id"], capacity=8)
    cowork = space("Open Bay", "COWORKING", parent_space_id=root["id"], capacity=20)
    seat1 = space("Seat 1", "SEAT", parent_space_id=cowork["id"], capacity=1)
    seat2 = space("Seat 2", "SEAT", parent_space_id=cowork["id"], capacity=1)
    corridor = space("Corridor", "NON_RENTABLE", parent_space_id=root["id"], area_sqft=300)
    return {
        "property": prop,
        "root": root,
        "5A": portion,
        "cabin": cabin,
        "meeting": meeting,
        "cowork": cowork,
        "seat1": seat1,
        "seat2": seat2,
        "corridor": corridor,
    }


def rule(api: TestClient, hub: dict, expected: int = 201, **values: object) -> dict:
    body: dict[str, object] = {
        "property_id": hub["property"]["id"],
        "rate_basis": "PER_SQFT",
        "billing_period": "MONTHLY",
        "amount": "70",
        "effective_from": "2026-01-01",
        "status": "ACTIVE",
    }
    body.update(values)
    return post(api, "pricing-rules", body, expected)


def pricing(api: TestClient, hub: dict, on: str = "2026-10-22") -> dict[str, dict]:
    response = api.get(f"{V1}/properties/{hub['property']['id']}/pricing", params={"on": on})
    assert response.status_code == 200, response.text
    return {s["space_name"]: s for s in response.json()["spaces"]}


def monthly(entry: dict) -> dict | None:
    return next((p for p in entry["prices"] if p["billing_period"] == "MONTHLY"), None)


def test_spaces_inherit_the_property_default_and_its_maintenance(
    api: TestClient, hub: dict
) -> None:
    rule(api, hub, maintenance_amount="6", maintenance_basis="PER_SQFT")

    prices = pricing(api, hub)
    cabin = monthly(prices["Cabin 1"])
    assert cabin["source"] == "PROPERTY_DEFAULT"
    assert cabin["estimate"]["rent"] == 14000.0
    assert cabin["estimate"]["maintenance"] == 1200.0
    assert cabin["estimate"]["recurring_total"] == 15200.0
    assert monthly(prices["Portion 5A"])["estimate"]["rent"] == 115500.0
    # No area, so a per-sq-ft default cannot price the meeting room or the seats.
    assert monthly(prices["Meeting Room"]) is None
    assert "Corridor" not in prices  # never priced


def test_a_portion_override_flows_to_spaces_inside_it_but_a_fixed_price_does_not(
    api: TestClient, hub: dict
) -> None:
    rule(api, hub)
    rule(api, hub, space_id=hub["5A"]["id"], amount="80")
    prices = pricing(api, hub)
    cabin = monthly(prices["Cabin 1"])
    assert (cabin["source"], cabin["source_space_name"]) == ("INHERITED", "Portion 5A")
    assert cabin["estimate"]["rent"] == 16000.0
    assert monthly(prices["Portion 5A"])["source"] == "OWN"

    # A fixed price for the whole portion is the portion's alone.
    fixed = rule(
        api,
        hub,
        space_id=hub["5A"]["id"],
        rate_basis="PER_PORTION",
        amount="120000",
        billing_period="MONTHLY",
    )
    prices = pricing(api, hub)
    assert monthly(prices["Portion 5A"])["rule"]["id"] == fixed["id"]
    assert monthly(prices["Cabin 1"])["estimate"]["rent"] == 16000.0  # still per sq ft


def test_a_space_override_wins_and_a_scoped_default_beats_a_general_one(
    api: TestClient, hub: dict
) -> None:
    rule(api, hub)
    rule(api, hub, rate_basis="PER_SEAT", applies_to="SEAT", amount="6500")
    own = rule(api, hub, space_id=hub["cabin"]["id"], rate_basis="PER_CABIN", amount="25000")

    prices = pricing(api, hub)
    cabin = monthly(prices["Cabin 1"])
    assert (cabin["source"], cabin["rule"]["id"]) == ("OWN", own["id"])
    assert cabin["estimate"]["rent"] == 25000.0
    seat = monthly(prices["Seat 1"])
    assert (seat["source"], seat["estimate"]["rent"]) == ("PROPERTY_DEFAULT", 6500.0)


def test_rate_basis_is_separate_from_billing_period(api: TestClient, hub: dict) -> None:
    rule(
        api,
        hub,
        space_id=hub["meeting"]["id"],
        rate_basis="PER_ROOM",
        billing_period="HOURLY",
        amount="800",
        payment_frequency="PER_BOOKING",
    )
    rule(
        api,
        hub,
        space_id=hub["meeting"]["id"],
        rate_basis="PER_ROOM",
        billing_period="FULL_DAY",
        amount="5000",
        payment_frequency="PER_BOOKING",
    )
    rule(
        api,
        hub,
        space_id=hub["meeting"]["id"],
        rate_basis="PER_PERSON",
        billing_period="HALF_DAY",
        amount="300",
        payment_frequency="PER_BOOKING",
    )

    periods = {p["billing_period"]: p for p in pricing(api, hub)["Meeting Room"]["prices"]}
    assert set(periods) == {"HOURLY", "FULL_DAY", "HALF_DAY"}
    assert periods["HALF_DAY"]["estimate"]["rent"] == 2400.0  # 300 x 8 persons

    response = api.post(
        f"{RULES}/estimate",
        json={
            "space_id": hub["meeting"]["id"],
            "billing_period": "HOURLY",
            "quantity": 3,
            "on": "2026-10-22",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["estimate"]["rent"] == 2400.0
    assert response.json()["price"]["source"] == "OWN"

    missing = api.post(
        f"{RULES}/estimate",
        json={"space_id": hub["meeting"]["id"], "billing_period": "WEEKLY"},
    )
    assert missing.status_code == 404


def test_overlapping_active_rates_are_refused_but_drafts_and_later_dates_are_not(
    api: TestClient, hub: dict
) -> None:
    first = rule(api, hub, effective_to="2026-12-31")
    refused = rule(api, hub, 409, amount="75", effective_from="2026-06-01")
    assert "already covers part of those dates" in refused["detail"]
    rule(api, hub, amount="75", effective_from="2026-06-01", status="DRAFT")
    later = rule(api, hub, amount="75", effective_from="2027-01-01")

    # Each applies only on its own dates.
    assert monthly(pricing(api, hub, "2026-10-22")["Cabin 1"])["rule"]["id"] == first["id"]
    assert monthly(pricing(api, hub, "2027-02-01")["Cabin 1"])["rule"]["id"] == later["id"]

    # Retiring the first frees its dates; a retired rule cannot be changed.
    retired = api.post(f"{RULES}/{first['id']}/retire")
    assert retired.json()["status"] == "RETIRED"
    rule(api, hub, amount="72", effective_from="2026-06-01", effective_to="2026-12-31")
    assert api.patch(f"{RULES}/{first['id']}", json={"amount": "1"}).status_code == 409


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"space_id": "cabin", "rate_basis": "PER_SEAT"}, "does not fit a cabin"),
        ({"space_id": "meeting"}, "has no area"),
        ({"space_id": "corridor"}, "not rentable"),
        (
            {"deposit_rule": "MONTHS_OF_RENT", "deposit_value": "2", "billing_period": "HOURLY"},
            "MONTHLY",
        ),
        ({"tax_treatment": "ADDITIONAL"}, "tax rate"),
        ({"tax_rate": "18"}, "tax rate"),
        ({"maintenance_amount": "6"}, "maintenance"),
        ({"billing_period": "CUSTOM"}, "custom_period_label"),
        ({"effective_to": "2025-01-01"}, "end date"),
        ({"applies_to": "SEAT", "rate_basis": "PER_CABIN"}, "cannot be the default"),
        ({"status": "RETIRED"}, "RETIRED"),
    ],
)
def test_invalid_rules_are_refused_with_a_reason(
    api: TestClient, hub: dict, values: dict, message: str
) -> None:
    if "space_id" in values:
        values = {**values, "space_id": hub[values["space_id"]]["id"]}
    refused = rule(api, hub, 400, **values)
    assert message in refused["detail"]


def test_only_drafts_can_be_deleted(api: TestClient, hub: dict) -> None:
    active = rule(api, hub)
    draft = rule(api, hub, status="DRAFT", amount="90")
    assert api.delete(f"{RULES}/{active['id']}").status_code == 409
    assert api.delete(f"{RULES}/{draft['id']}").status_code == 204
    listed = api.get(RULES, params={"property_id": hub["property"]["id"]}).json()
    assert [r["id"] for r in listed["items"]] == [active["id"]]
    assert listed["items"][0]["created_by"] is not None


def test_the_legacy_list_price_is_shown_apart_and_never_added(api: TestClient, hub: dict) -> None:
    api.patch(
        f"{V1}/spaces/{hub['cabin']['id']}", json={"price_amount": "999", "price_unit": "MONTH"}
    )
    rule(api, hub)
    cabin = pricing(api, hub)["Cabin 1"]
    assert cabin["legacy_price"] == {"amount": 999.0, "unit": "MONTH", "currency": "INR"}
    assert monthly(cabin)["estimate"]["recurring_total"] == 14000.0


def test_nothing_is_priced_until_a_rule_is_entered(api: TestClient, hub: dict) -> None:
    response = api.get(f"{V1}/properties/{hub['property']['id']}/pricing")
    body = response.json()
    assert all(not s["prices"] for s in body["spaces"])
    assert len(body["unpriced"]) == len(body["spaces"])
    assert api.get(RULES, params={"property_id": hub["property"]["id"]}).json()["total"] == 0
