# ruff: noqa: F811  (shared fixtures imported from test_client_api)
"""Billing profiles, derived rates, overrides and building / floor prices.

The figures (8 billable hours, 6 days a week, 26 days a month, 5% and 10% discounts) are
test data for an explicit profile, not defaults of the application.
"""

import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.errors import BadRequestError
from app.main import app
from app.models.enums import BillingPeriod as P
from app.models.enums import RoundingPolicy as R
from app.services.pricing_conversion import Profile, derive, factor, round_amount
from tests.test_client_api import CUSTOMER, MONDAY, PUBLIC, book, iso, priya  # noqa: F401
from tests.test_properties import create_property
from tests.test_resources import V1, post

PROFILE = Profile(Decimal(8), Decimal(6), Decimal(26), Decimal(5), Decimal(10), R.PAISA)


def rule(period: str, amount: str, **extra: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "billing_period": period,
        "amount": Decimal(amount),
        "maintenance_amount": None,
        "additional_charges": [],
        "deposit_rule": "NONE",
        "deposit_value": None,
        "payment_frequency": "PER_BOOKING",
    }
    values.update(extra)
    return SimpleNamespace(**values)


# --- the conversion policy --------------------------------------------------------------


def test_hourly_to_daily_weekly_and_monthly() -> None:
    hourly = rule("HOURLY", "100")
    assert derive(hourly, P.DAILY, PROFILE).amount == Decimal("800.00")  # 100 x 8
    assert derive(hourly, P.FULL_DAY, PROFILE).amount == Decimal("800.00")
    assert derive(hourly, P.HALF_DAY, PROFILE).amount == Decimal("400.00")
    assert derive(hourly, P.WEEKLY, PROFILE).amount == Decimal("4560.00")  # 800 x 6 x 0.95
    assert derive(hourly, P.MONTHLY, PROFILE).amount == Decimal("18720.00")  # 800 x 26 x 0.9


def test_monthly_converts_back_exactly_under_the_same_policy() -> None:
    monthly = rule(
        "MONTHLY",
        "18720",
        maintenance_amount=Decimal("520"),
        maintenance_basis="FIXED",
        additional_charges=[{"label": "Parking", "amount": "936"}],
        deposit_rule="MONTHS_OF_RENT",
        deposit_value=Decimal("2"),
        payment_frequency="MONTHLY",
    )
    hourly = derive(monthly, P.HOURLY, PROFILE)
    assert hourly.amount == Decimal("100.00")
    # Charges move with the rent; a months-of-rent deposit only exists monthly.
    assert hourly.maintenance_amount == Decimal("2.78")
    assert hourly.additional_charges == [{"label": "Parking", "amount": "5.00"}]
    assert (hourly.deposit_rule.value, hourly.deposit_value) == ("NONE", None)
    assert derive(monthly, P.WEEKLY, PROFILE).amount == Decimal("4560.00")
    assert factor(P.HOURLY, P.MONTHLY, PROFILE) * factor(P.MONTHLY, P.HOURLY, PROFILE) == 1


def test_rounding_policies_round_half_up_at_the_end() -> None:
    assert round_amount(Decimal("1234.565"), R.PAISA) == Decimal("1234.57")
    assert round_amount(Decimal("1234.5"), R.RUPEE) == Decimal("1235")
    assert round_amount(Decimal("1235"), R.TEN_RUPEES) == Decimal("1240")
    odd = Profile(Decimal("7.5"), Decimal(5), Decimal(22), Decimal(0), Decimal("12.5"), R.RUPEE)
    # 333 x 7.5 x 22 x 0.875 = 48,076.875 -> 48,077 (rupee), 48,080 (ten rupees)
    assert derive(rule("HOURLY", "333"), P.MONTHLY, odd).amount == Decimal("48077")
    tens = Profile(
        odd.hours_per_day,
        odd.days_per_week,
        odd.days_per_month,
        odd.weekly_discount_pct,
        odd.monthly_discount_pct,
        R.TEN_RUPEES,
    )
    assert derive(rule("HOURLY", "333"), P.MONTHLY, tens).amount == Decimal("48080")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("hours_per_day", 0),
        ("hours_per_day", 25),
        ("days_per_week", 8),
        ("days_per_month", 32),
        ("weekly_discount_pct", 100),
        ("monthly_discount_pct", -1),
    ],
)
def test_invalid_profiles_are_refused(field: str, value: int) -> None:
    figures = dict(
        hours_per_day=Decimal(8),
        days_per_week=Decimal(6),
        days_per_month=Decimal(26),
        weekly_discount_pct=Decimal(0),
        monthly_discount_pct=Decimal(0),
        rounding=R.PAISA,
    )
    figures[field] = Decimal(value)
    with pytest.raises(BadRequestError):
        Profile(**figures).check()


def test_custom_periods_do_not_convert() -> None:
    with pytest.raises(BadRequestError):
        derive(rule("CUSTOM", "1000"), P.MONTHLY, PROFILE)
    with pytest.raises(BadRequestError):
        derive(rule("HOURLY", "100"), P.HOURLY, PROFILE)


# --- through the API ---------------------------------------------------------------------


@pytest.fixture
def floor1(api: TestClient) -> dict[str, dict]:
    """Property > Building A > Floor 1 > Portion 1A > Cabin 1A and Seat 1."""
    prop = create_property(api, name="Pricing Hub")
    building = post(api, "buildings", {"property_id": prop["id"], "name": "Building A"})
    floor = post(
        api, "floors", {"building_id": building["id"], "name": "Floor 1", "level_number": 1}
    )

    def space(name: str, space_type: str, **extra: object) -> dict:
        return post(
            api,
            "spaces",
            {"property_id": prop["id"], "name": name, "space_type": space_type, **extra},
        )

    root = space("Floor 1", "FLOOR", floor_id=floor["id"], is_rentable=False)
    portion = space("Portion 1A", "PORTION", parent_space_id=root["id"], area_sqft=1000)
    cabin = space("Cabin 1A", "CABIN", parent_space_id=portion["id"], capacity=4)
    seat = space("Seat 1", "SEAT", parent_space_id=portion["id"], capacity=1)
    return {
        "property": prop,
        "building": building,
        "portion": portion,
        "cabin": cabin,
        "seat": seat,
    }


def price(api: TestClient, hub: dict, expected: int = 201, **values: object) -> dict:
    body = {
        "property_id": hub["property"]["id"],
        "effective_from": "2026-01-01",
        "status": "ACTIVE",
        **values,
    }
    return post(api, "pricing-rules", body, expected)


def profile(api: TestClient, hub: dict, expected: int = 201, **values: object) -> dict:
    body = {
        "property_id": hub["property"]["id"],
        "hours_per_day": 8,
        "days_per_week": 6,
        "days_per_month": 26,
        "weekly_discount_pct": 5,
        "monthly_discount_pct": 10,
        "rounding": "PAISA",
        **values,
    }
    return post(api, "billing-profiles", body, expected)


def test_profiles_require_every_figure_and_one_per_property(api: TestClient, floor1: dict) -> None:
    incomplete = api.post(
        f"{V1}/billing-profiles", json={"property_id": floor1["property"]["id"], "hours_per_day": 8}
    )
    assert incomplete.status_code == 422
    profile(api, floor1, 422, days_per_week=8)
    profile(api, floor1)
    assert profile(api, floor1, 409)["detail"].startswith("A billing profile already exists")
    override = profile(api, floor1, space_id=floor1["seat"]["id"], hours_per_day=10)
    listed = api.get(
        f"{V1}/billing-profiles", params={"property_id": floor1["property"]["id"]}
    ).json()
    assert [(p["space_name"], p["hours_per_day"]) for p in listed] == [
        (None, 8.0),
        ("Seat 1", 10.0),
    ]
    assert override["space_id"] == floor1["seat"]["id"]


def test_estimates_need_a_profile_and_are_only_previews(api: TestClient, floor1: dict) -> None:
    seat_hour = price(
        api,
        floor1,
        space_id=floor1["seat"]["id"],
        rate_basis="PER_SEAT",
        billing_period="HOURLY",
        amount="100",
        payment_frequency="PER_BOOKING",
    )
    no_profile = api.post(f"{V1}/pricing-rules/{seat_hour['id']}/derivations")
    assert no_profile.status_code == 409 and "no billing profile" in no_profile.json()["detail"]

    profile(api, floor1)
    preview = api.post(f"{V1}/pricing-rules/{seat_hour['id']}/derivations").json()
    by_period = {e["billing_period"]: e for e in preview["estimates"]}
    assert {p: e["amount"] for p, e in by_period.items()} == {
        "HALF_DAY": 400.0,
        "FULL_DAY": 800.0,
        "DAILY": 800.0,
        "WEEKLY": 4560.0,
        "MONTHLY": 18720.0,
    }
    assert by_period["WEEKLY"]["on_approval"] == "CREATE"
    assert "6 days" in by_period["WEEKLY"]["formula"]
    # A preview prices nothing.
    seat_prices = api.get(f"{V1}/properties/{floor1['property']['id']}/pricing").json()
    seat = next(s for s in seat_prices["spaces"] if s["space_name"] == "Seat 1")
    assert [p["billing_period"] for p in seat["prices"]] == ["HOURLY"]


def test_approval_overrides_and_reapproval(api: TestClient, floor1: dict) -> None:
    profile(api, floor1)
    seat_hour = price(
        api,
        floor1,
        space_id=floor1["seat"]["id"],
        rate_basis="PER_SEAT",
        billing_period="HOURLY",
        amount="100",
        payment_frequency="PER_BOOKING",
    )
    # The admin set a weekly rate by hand: an estimate never replaces it.
    weekly = price(
        api,
        floor1,
        space_id=floor1["seat"]["id"],
        rate_basis="PER_SEAT",
        billing_period="WEEKLY",
        amount="4000",
        payment_frequency="PER_BOOKING",
    )
    approve = f"{V1}/pricing-rules/{seat_hour['id']}/derivations/approve"
    result = api.post(approve, json={"periods": ["DAILY", "WEEKLY", "MONTHLY"]}).json()
    assert sorted((r["billing_period"], r["amount"], r["source"]) for r in result["created"]) == [
        ("DAILY", 800.0, "DERIVED"),
        ("MONTHLY", 18720.0, "DERIVED"),
    ]
    assert [s["billing_period"] for s in result["skipped"]] == ["WEEKLY"]
    monthly = next(r for r in result["created"] if r["billing_period"] == "MONTHLY")
    assert monthly["base_rule_id"] == seat_hour["id"]
    assert monthly["derivation"]["profile"]["days_per_month"] == "26.0"
    assert api.get(f"{V1}/pricing-rules/{weekly['id']}").json()["status"] == "ACTIVE"

    # An override replaces the derived monthly rate for its period; the derived one is retired.
    override = price(
        api,
        floor1,
        space_id=floor1["seat"]["id"],
        rate_basis="PER_SEAT",
        billing_period="MONTHLY",
        amount="15000",
        source="OVERRIDE",
    )
    assert override["source"] == "OVERRIDE"
    assert api.get(f"{V1}/pricing-rules/{monthly['id']}").json()["status"] == "RETIRED"
    # Without OVERRIDE, a second rate for the same period is ambiguous and refused.
    price(
        api,
        floor1,
        409,
        space_id=floor1["seat"]["id"],
        rate_basis="PER_SEAT",
        billing_period="MONTHLY",
        amount="16000",
    )

    # A changed profile shows in a new preview; re-approving replaces the derived daily rate.
    profiles = api.get(
        f"{V1}/billing-profiles", params={"property_id": floor1["property"]["id"]}
    ).json()
    api.patch(f"{V1}/billing-profiles/{profiles[0]['id']}", json={"hours_per_day": 9})
    daily = api.post(
        f"{V1}/pricing-rules/{seat_hour['id']}/derivations", params={"periods": ["DAILY"]}
    ).json()["estimates"][0]
    assert (daily["amount"], daily["on_approval"]) == (900.0, "REPLACE_DERIVED")
    again = api.post(approve, json={"periods": ["DAILY", "MONTHLY"]}).json()
    assert [(r["billing_period"], r["amount"]) for r in again["created"]] == [("DAILY", 900.0)]
    assert [s["billing_period"] for s in again["skipped"]] == ["MONTHLY"]  # the override stays

    # Derived estimates cannot be the base of more estimates.
    assert (
        api.post(f"{V1}/pricing-rules/{again['created'][0]['id']}/derivations").status_code == 409
    )


def test_building_defaults_sit_between_own_prices_and_property_defaults(
    api: TestClient, floor1: dict
) -> None:
    price(api, floor1, rate_basis="PER_CABIN", billing_period="MONTHLY", amount="3000")
    price(
        api,
        floor1,
        building_id=floor1["building"]["id"],
        rate_basis="PER_CABIN",
        billing_period="MONTHLY",
        amount="3587",
    )
    prices = api.get(f"{V1}/properties/{floor1['property']['id']}/pricing").json()
    cabin = next(s for s in prices["spaces"] if s["space_name"] == "Cabin 1A")["prices"][0]
    assert (cabin["source"], cabin["estimate"]["rent"]) == ("BUILDING_DEFAULT", 3587.0)
    assert cabin["rule"]["building_name"] == "Building A"
    price(
        api,
        floor1,
        space_id=floor1["cabin"]["id"],
        rate_basis="PER_CABIN",
        billing_period="MONTHLY",
        amount="4000",
    )
    prices = api.get(f"{V1}/properties/{floor1['property']['id']}/pricing").json()
    cabin = next(s for s in prices["spaces"] if s["space_name"] == "Cabin 1A")["prices"][0]
    assert (cabin["source"], cabin["estimate"]["rent"]) == ("OWN", 4000.0)
    # Fixed cabin prices never use area; a building default must belong to the property.
    assert cabin["estimate"]["measure"] is None
    other = create_property(api, name="Elsewhere")
    stranger = post(api, "buildings", {"property_id": other["id"], "name": "Far"})
    price(
        api,
        floor1,
        400,
        building_id=stranger["id"],
        rate_basis="PER_CABIN",
        billing_period="MONTHLY",
        amount="1",
    )


def test_create_property_prices_each_level_and_approved_estimates_are_bookable(
    api: TestClient, priya: TestClient
) -> None:
    setup = {
        "property": {
            "name": "Level Hub",
            "property_type": "COMMERCIAL",
            "category": "OFFICE",
            "address": "1 Road",
            "city": "Hyderabad",
            "state": "Telangana",
            "postal_code": "500081",
        },
        "buildings": [{"ref": "bA", "name": "Building A"}],
        "floors": [
            {"ref": "f1", "building_ref": "bA", "name": "Floor 1", "level_number": 1},
            {"ref": "f2", "building_ref": "bA", "name": "Floor 2", "level_number": 2},
        ],
        "spaces": [
            {"ref": "1A", "floor_ref": "f1", "name": "Portion 1A", "space_type": "PORTION"},
            {
                "ref": "c1A",
                "floor_ref": "f1",
                "parent_ref": "1A",
                "name": "Cabin 1A",
                "space_type": "CABIN",
            },
            {
                "ref": "s1",
                "floor_ref": "f1",
                "parent_ref": "1A",
                "name": "Seat 1",
                "space_type": "SEAT",
                "capacity": 1,
            },
        ],
        "billing_profile": {
            "hours_per_day": 8,
            "days_per_week": 6,
            "days_per_month": 26,
            "weekly_discount_pct": 5,
            "monthly_discount_pct": 10,
            "rounding": "PAISA",
        },
        "pricing": [
            # The administrator chose a basis and a period for every amount.
            {
                "target_ref": "f2",
                "rate_basis": "PER_FLOOR",
                "billing_period": "MONTHLY",
                "amount": "5000",
                "effective_from": "2026-01-01",
                "status": "ACTIVE",
            },
            {
                "target_ref": "1A",
                "rate_basis": "PER_PORTION",
                "billing_period": "DAILY",
                "amount": "600",
                "effective_from": "2026-01-01",
                "status": "ACTIVE",
                "payment_frequency": "PER_BOOKING",
            },
            {
                "target_ref": "c1A",
                "rate_basis": "PER_CABIN",
                "billing_period": "MONTHLY",
                "amount": "3587",
                "effective_from": "2026-01-01",
                "status": "ACTIVE",
            },
            {
                "target_ref": "s1",
                "rate_basis": "PER_SEAT",
                "billing_period": "HOURLY",
                "amount": "100",
                "effective_from": "2026-01-01",
                "status": "ACTIVE",
                "payment_frequency": "PER_BOOKING",
                "derive": ["DAILY", "WEEKLY"],
            },
        ],
    }
    response = api.post(f"{V1}/properties/setup", data={"setup": json.dumps(setup)})
    assert response.status_code == 201, response.text
    result = response.json()
    assert (result["created"]["pricing_rules"], result["created"]["derived_rules"]) == (4, 2)
    assert any("rentable: the floor is priced as a whole" in a for a in result["added"])

    plan_floors = TestClient(app).get(f"{PUBLIC}/properties/{result['property']['id']}").json()
    floor2 = next(f for f in plan_floors["buildings"][0]["floors"] if f["name"] == "Floor 2")
    assert floor2["bookable_spaces"] == 1  # the whole floor, priced at 5,000 a month

    seat_id = result["ids"]["s1"]
    weekly = book(priya, {"id": seat_id}, period="WEEKLY", hours=24 * 7)
    assert weekly["quoted_total"] == 4560.0  # approved derived rate
    # Without a profile in the request, derived periods are refused before anything is saved.
    setup["property"]["name"] = "No Profile Hub"
    del setup["billing_profile"]
    refused = api.post(f"{V1}/properties/setup", data={"setup": json.dumps(setup)})
    assert refused.status_code == 400
    assert any(e["field"] == "pricing.3.derive" for e in refused.json()["errors"])
    assert MONDAY - timedelta(days=1) < MONDAY and iso(MONDAY)
    assert CUSTOMER.endswith("/customer")
