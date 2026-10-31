# ruff: noqa: F811  (pytest fixtures shared from test_client_api)
"""Three charge types and the Reimbursement Fee (business rules BR-009, BR-029, BR-030).

Sample property "Sigma Hub" (from test_client_api): monthly rate 70/sq ft (no maintenance);
seats 100/hour and 500/day. Fixed and per-sq-ft maintenance are covered by test_pricing.
Reimbursement data is entered here; nothing comes from the application's own defaults
except the six seeded expense types."""

import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ActivityLog, StoredFile
from tests.test_client_api import CUSTOMER, MONDAY, PUBLIC, hub, iso, priya  # noqa: F401
from tests.test_properties import create_property
from tests.test_resources import V1, post

R = f"{V1}/reimbursements"
SEEDED = [
    "Diesel generator / power backup",
    "Electricity",
    "Internet",
    "Other shared operational expenses",
    "Waste management",
    "Water",
]


def types(api: TestClient) -> dict[str, str]:
    return {t["name"]: t["id"] for t in api.get(f"{R}/expense-types").json()}


def term(api: TestClient, expected: int = 201, **body: object) -> dict:
    response = api.post(f"{R}/terms", json=body)
    assert response.status_code == expected, response.text
    return response.json()


@pytest.fixture
def contract(api: TestClient, hub: dict) -> dict:
    tenant = post(api, "tenants", {"name": "Acme", "email": "ops@acme.example"})
    made = api.post(
        f"{V1}/contracts",
        json={
            "tenant_id": tenant["id"],
            "property_id": hub["property"]["id"],
            "space_id": hub["cabin"]["id"],
            "start_date": "2031-01-01",
            "monthly_rent": 15000,
            "activate": True,
        },
    )
    assert made.status_code == 201, made.text
    return made.json()


def test_the_expense_catalogue_is_seeded_and_never_duplicated(
    api: TestClient, db_session: Session
) -> None:
    assert sorted(types(api)) == SEEDED
    made = api.post(f"{R}/expense-types", json={"name": "  Pest   control "})
    assert made.status_code == 201 and made.json()["name"] == "Pest control"
    for name in ("electricity", "ELECTRICITY ", "pest control"):
        dup = api.post(f"{R}/expense-types", json={"name": name})
        assert dup.status_code == 409 and "already exists" in dup.json()["detail"]
    url = f"{R}/expense-types/{made.json()['id']}"
    assert api.patch(url, json={"name": "Water"}).status_code == 409
    assert api.patch(url, json={"name": "   "}).status_code == 422
    renamed = api.patch(url, json={"name": " Pest  control service"})
    assert renamed.status_code == 200 and renamed.json()["name"] == "Pest control service"
    # Renaming to itself in another case is not a duplicate of itself.
    assert api.patch(url, json={"name": "pest control service"}).status_code == 200
    logged = db_session.scalars(
        select(ActivityLog.message).where(ActivityLog.entity_id == uuid.UUID(made.json()["id"]))
    ).all()
    assert any("name Pest control -> Pest control service" in m for m in logged)
    retired = api.patch(f"{R}/expense-types/{made.json()['id']}", json={"is_active": False})
    assert retired.json()["is_active"] is False
    active = api.get(f"{R}/expense-types", params={"include_inactive": False}).json()
    assert "Pest control" not in [t["name"] for t in active]


def test_property_defaults_are_validated_and_taxed_on_their_own(api: TestClient, hub: dict) -> None:
    pid, t = hub["property"]["id"], types(api)
    base = {"property_id": pid, "expense_type_id": t["Electricity"]}
    for bad in (
        {"method": "FIXED", "recurrence": "RECURRING", "billing_period": "MONTHLY"},  # no amount
        {"method": "FIXED", "amount": 100, "recurrence": "RECURRING"},  # no period
        {"method": "FIXED", "amount": 100, "recurrence": "RECURRING", "billing_period": "CUSTOM"},
        {"method": "ACTUAL", "amount": 100, "recurrence": "RECURRING", "billing_period": "MONTHLY"},
        {
            "method": "FIXED",
            "amount": 100,
            "recurrence": "ONE_TIME",
            "tax_treatment": "ADDITIONAL",
        },  # no rate
    ):
        assert api.post(f"{R}/terms", json={**base, **bad}).status_code == 400, bad
    electricity = term(
        api,
        **base,
        method="FIXED",
        amount=1000,
        recurrence="RECURRING",
        billing_period="MONTHLY",
        tax_treatment="ADDITIONAL",
        tax_rate=18,
    )
    assert electricity["scope"] == "PROPERTY_DEFAULT"
    assert (electricity["tax"], electricity["total"]) == (180.0, 1180.0)
    term(api, 409, **base, method="ACTUAL", recurrence="RECURRING", billing_period="MONTHLY")
    water = term(
        api,
        property_id=pid,
        expense_type_id=t["Water"],
        method="ACTUAL",
        recurrence="RECURRING",
        billing_period="MONTHLY",
        tax_treatment="INCLUDED",
        tax_rate=5,
    )
    assert water["amount"] is None and water["total"] is None
    listed = api.get(f"{R}/terms", params={"property_id": pid}).json()
    assert [x["expense_type_name"] for x in listed] == ["Electricity", "Water"]


def test_estimates_show_three_charge_types_and_bookings_are_unchanged(
    api: TestClient, hub: dict
) -> None:
    pid = hub["property"]["id"]

    def monthly_cabin() -> dict:
        pricing = api.get(f"{V1}/properties/{pid}/pricing").json()
        cabin = next(s for s in pricing["spaces"] if s["space_id"] == hub["cabin"]["id"])
        return next(p for p in cabin["prices"] if p["billing_period"] == "MONTHLY")["estimate"]

    before = monthly_cabin()
    quote_before = TestClient(api.app).post(
        f"{PUBLIC}/quotes",
        json={
            "space_id": hub["seat1"]["id"],
            "billing_period": "DAILY",
            "start_at": iso(MONDAY),
            "end_at": iso(MONDAY.replace(day=MONDAY.day + 1)),
        },
    )
    term(
        api,
        property_id=pid,
        expense_type_id=types(api)["Electricity"],
        method="FIXED",
        amount=1000,
        recurrence="RECURRING",
        billing_period="MONTHLY",
        tax_treatment="ADDITIONAL",
        tax_rate=18,
    )
    term(
        api,
        property_id=pid,
        expense_type_id=types(api)["Internet"],
        method="FIXED",
        amount=500,
        recurrence="RECURRING",
        billing_period="MONTHLY",
    )
    after = monthly_cabin()
    # Space Fee (200 sq ft x 70) and Maintenance Fee (none on this rate) are as before.
    assert (
        (after["rent"], after["maintenance"])
        == (before["rent"], before["maintenance"])
        == (
            14000.0,
            0.0,
        )
    )
    assert after["recurring_total"] == before["recurring_total"]
    assert (after["reimbursement"], after["reimbursement_tax"]) == (1500.0, 180.0)
    assert [(x["expense_type"], x["total"]) for x in after["reimbursement_lines"]] == [
        ("Electricity", 1180.0),
        ("Internet", 500.0),
    ]
    assert after["total_with_reimbursement"] == before["recurring_total"] + 1680.0
    # A daily seat price has no monthly reimbursement; a customer's quote is unchanged.
    quote_after = TestClient(api.app).post(
        f"{PUBLIC}/quotes",
        json={
            "space_id": hub["seat1"]["id"],
            "billing_period": "DAILY",
            "start_at": iso(MONDAY),
            "end_at": iso(MONDAY.replace(day=MONDAY.day + 1)),
        },
    )
    assert quote_after.status_code == quote_before.status_code == 200
    assert quote_after.json()["estimate"] == quote_before.json()["estimate"]
    assert quote_after.json()["estimate"]["reimbursement"] == 0


def test_contract_terms_copy_defaults_without_ever_overwriting(
    api: TestClient, hub: dict, contract: dict
) -> None:
    pid, t = hub["property"]["id"], types(api)
    default = term(
        api,
        property_id=pid,
        expense_type_id=t["Electricity"],
        method="FIXED",
        amount=1000,
        recurrence="RECURRING",
        billing_period="MONTHLY",
    )
    term(
        api,
        property_id=pid,
        expense_type_id=t["Water"],
        method="ACTUAL",
        recurrence="RECURRING",
        billing_period="MONTHLY",
    )
    # The contract already agreed its own electricity amount.
    own = term(
        api,
        property_id=pid,
        contract_id=contract["id"],
        expense_type_id=t["Electricity"],
        method="FIXED",
        amount=1500,
        recurrence="RECURRING",
        billing_period="MONTHLY",
    )
    applied = api.post(f"{R}/contracts/{contract['id']}/apply-defaults").json()
    assert [x["expense_type_name"] for x in applied["created"]] == ["Water"]
    assert applied["skipped"] == ["Electricity"]
    again = api.post(f"{R}/contracts/{contract['id']}/apply-defaults").json()
    assert again["created"] == [] and again["skipped"] == ["Electricity", "Water"]
    # Changing the default never touches the contract, and the other way round.
    api.patch(f"{R}/terms/{default['id']}", json={"amount": 2000})
    terms = api.get(f"{R}/terms", params={"property_id": pid, "contract_id": contract["id"]}).json()
    assert {x["expense_type_name"]: x["amount"] for x in terms} == {
        "Electricity": 1500.0,
        "Water": None,
    }
    assert own["scope"] == "CONTRACT"
    other = create_property(api, name="Elsewhere")
    stray = api.post(
        f"{R}/terms",
        json={
            "property_id": other["id"],
            "contract_id": contract["id"],
            "expense_type_id": t["Water"],
            "method": "ACTUAL",
            "recurrence": "ONE_TIME",
        },
    )
    assert stray.status_code == 400 and "does not belong" in stray.json()["detail"]


def test_actual_expenses_wait_for_review_and_keep_their_history(
    api: TestClient, hub: dict, contract: dict, db_session: Session
) -> None:
    pid, t = hub["property"]["id"], types(api)
    term(
        api,
        property_id=pid,
        contract_id=contract["id"],
        expense_type_id=t["Water"],
        method="ACTUAL",
        recurrence="RECURRING",
        billing_period="MONTHLY",
        tax_treatment="ADDITIONAL",
        tax_rate=5,
    )
    entry = {
        "contract_id": contract["id"],
        "period_start": "2031-01-01",
        "period_end": "2031-01-31",
        "description": "January water bill",
    }
    # Recurring entries need an ACTUAL term for the type; one-time ones do not.
    no_term = api.post(
        f"{R}/expenses", json={**entry, "expense_type_id": t["Internet"], "amount": 900}
    )
    assert no_term.status_code == 400 and "one-time" in no_term.json()["detail"]
    one_off = api.post(
        f"{R}/expenses",
        json={**entry, "expense_type_id": t["Internet"], "amount": 900, "one_time": True},
    )
    assert one_off.status_code == 201 and one_off.json()["tax"] == 0.0
    water = api.post(f"{R}/expenses", json={**entry, "expense_type_id": t["Water"], "amount": 2000})
    assert water.status_code == 201, water.text
    w = water.json()
    assert (w["status"], w["tax_treatment"], w["tax"], w["total"]) == (
        "PENDING_REVIEW",
        "ADDITIONAL",
        100.0,
        2100.0,
    )
    bad = api.post(
        f"{R}/expenses",
        json={**entry, "expense_type_id": t["Water"], "amount": 5, "period_end": "2030-12-01"},
    )
    assert bad.status_code == 422
    # Corrected before review: the first entered amount is kept, the change is logged.
    fixed = api.patch(f"{R}/expenses/{w['id']}", json={"amount": 2200})
    assert (fixed.json()["amount"], fixed.json()["original_amount"]) == (2200.0, 2000.0)
    period = {"start": "2031-01-01", "end": "2031-01-31"}
    assert api.get(f"{R}/contracts/{contract['id']}/billable", params=period).json() == []
    assert (
        api.post(f"{R}/expenses/{w['id']}/review", json={"decision": "REJECTED"}).status_code == 400
    )
    reviewed = api.post(f"{R}/expenses/{w['id']}/review", json={"decision": "REVIEWED"})
    assert reviewed.json()["status"] == "REVIEWED" and reviewed.json()["reviewed_at"]
    assert api.patch(f"{R}/expenses/{w['id']}", json={"amount": 1}).status_code == 409
    assert (
        api.post(f"{R}/expenses/{w['id']}/review", json={"decision": "REVIEWED"}).status_code == 409
    )
    rejected = api.post(
        f"{R}/expenses/{one_off.json()['id']}/review",
        json={"decision": "REJECTED", "note": "Not a shared expense"},
    )
    assert rejected.json()["status"] == "REJECTED"
    billable = api.get(f"{R}/contracts/{contract['id']}/billable", params=period).json()
    assert [(b["expense_type_name"], b["total"]) for b in billable] == [("Water", 2310.0)]
    assert (
        api.get(
            f"{R}/contracts/{contract['id']}/billable",
            params={"start": "2031-02-01", "end": "2031-02-28"},
        ).json()
        == []
    )
    logged = db_session.scalars(
        select(ActivityLog.message).where(ActivityLog.entity_id == uuid.UUID(w["id"]))
    ).all()
    assert any("amount 2000.00 -> 2200" in m for m in logged)
    assert any("reviewed" in m.lower() for m in logged)


def test_receipts_must_belong_to_the_same_property(
    api: TestClient, hub: dict, contract: dict, db_session: Session
) -> None:
    def stored(property_id: str) -> str:
        f = StoredFile(
            property_id=uuid.UUID(property_id),
            kind="DOCUMENT",
            bucket="b",
            object_path=f"{property_id}/{uuid.uuid4().hex}.pdf",
            original_name="bill.pdf",
            content_type="application/pdf",
            size_bytes=100,
        )
        db_session.add(f)
        db_session.commit()
        return str(f.id)

    t = types(api)
    entry = {
        "contract_id": contract["id"],
        "expense_type_id": t["Electricity"],
        "period_start": "2031-01-01",
        "period_end": "2031-01-31",
        "amount": 700,
        "description": "Meter reading",
        "one_time": True,
    }
    other = create_property(api, name="Other Place")
    wrong = api.post(f"{R}/expenses", json={**entry, "receipt_file_id": stored(other["id"])})
    assert wrong.status_code == 400 and "same property" in wrong.json()["detail"]
    right = api.post(
        f"{R}/expenses", json={**entry, "receipt_file_id": stored(hub["property"]["id"])}
    )
    assert right.status_code == 201 and right.json()["receipt_file_id"]


def test_existing_additional_charges_are_reported_not_changed(api: TestClient, hub: dict) -> None:
    rule = post(
        api,
        "pricing-rules",
        {
            "property_id": hub["property"]["id"],
            "space_id": hub["cabin"]["id"],
            "rate_basis": "PER_CABIN",
            "billing_period": "WEEKLY",
            "amount": "3000",
            "effective_from": "2026-01-01",
            "status": "ACTIVE",
            "additional_charges": [
                {"label": "Electricity charges", "amount": "500"},
                {"label": "Housekeeping", "amount": "200"},
            ],
        },
    )
    report = api.get(
        f"{R}/additional-charges-review", params={"property_id": hub["property"]["id"]}
    ).json()
    assert [(r["label"], r["classification"], r["suggested_expense_type"]) for r in report] == [
        ("Electricity charges", "MATCHES_EXPENSE_TYPE", "Electricity"),
        ("Housekeeping", "UNCLASSIFIED", None),
    ]
    assert all("nothing was changed" in r["note"] or "stays part" in r["note"] for r in report)
    unchanged = api.get(f"{V1}/pricing-rules/{rule['id']}").json()
    assert [c["label"] for c in unchanged["additional_charges"]] == [
        "Electricity charges",
        "Housekeeping",
    ]


def test_reimbursements_are_for_administrators_only(priya: TestClient, hub: dict) -> None:
    for method, path in (
        ("get", f"{R}/expense-types"),
        ("get", f"{R}/terms?property_id={hub['property']['id']}"),
        ("get", f"{R}/expenses"),
        ("post", f"{R}/expense-types"),
    ):
        assert getattr(priya, method)(path).status_code == 403
    assert date(2031, 1, 1)  # keep the date import meaningful for readers
