# ruff: noqa: F811  (pytest fixtures shared from test_client_api)
"""Proposals (Phase 4A, BR-006). Needs the migrated test database (like every API test here).

Sample property "Sigma Hub" (from test_client_api): Tower A > Floor 5 > Portion 5A (Cabin 1),
Portion 5B (Open Bay > Seat 1, Seat 2)."""

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Account, Allocation, Booking, Lead, Tenant, TenantOccupancy
from app.services.occupancy import business_today
from tests.test_client_api import hub, priya  # noqa: F401
from tests.test_crm import CRM, lead
from tests.test_properties import create_property
from tests.test_resources import V1


def propose(api: TestClient, lead_id: str, expected: int = 201, **body: object) -> dict:
    today = business_today()
    response = api.post(
        f"{CRM}/leads/{lead_id}/proposals",
        json={
            "proposal_date": today.isoformat(),
            "start_date": (today + timedelta(days=30)).isoformat(),
            **body,
        },
    )
    assert response.status_code == expected, response.text
    return response.json()


def step(api: TestClient, proposal: dict, action: str, expected: int = 200, **body: object):
    response = api.post(f"{CRM}/proposals/{proposal['id']}/{action}", json=body)
    assert response.status_code == expected, response.text
    return response.json()


def counts(session: Session) -> tuple[int, ...]:
    session.expire_all()
    return tuple(
        session.scalar(select(func.count()).select_from(model))
        for model in (TenantOccupancy, Allocation, Booking, Tenant, Account)
    )


def test_only_admins_reach_proposals(client: TestClient, priya: TestClient) -> None:
    for who, status in ((client, 401), (priya, 403)):
        assert who.get(f"{CRM}/proposals/00000000-0000-0000-0000-000000000000").status_code == (
            status
        )


def test_a_proposal_links_to_its_lead_with_spaces_and_terms(api: TestClient, hub: dict) -> None:
    made_lead = lead(api)
    today = business_today()
    made = propose(
        api,
        made_lead["id"],
        property_id=hub["property"]["id"],
        space_ids=[hub["seat1"]["id"], hub["cabin"]["id"]],
        valid_until=(today + timedelta(days=14)).isoformat(),
        end_date=(today + timedelta(days=395)).isoformat(),
        monthly_rent="85000",
        security_deposit="255000",
        other_charges="Maintenance as per the building's notice.",
        commercial_terms="Lock-in 11 months; 5% escalation after 12 months.",
        notes="Prefers the corner cabin.",
    )
    assert made["reference"].startswith(f"PR-{today.year}-")
    assert (made["lead_id"], made["lead_reference"]) == (made_lead["id"], made_lead["reference"])
    assert (made["status"], made["property_name"], made["expired"]) == (
        "DRAFT",
        "Sigma Hub",
        False,
    )
    assert (made["monthly_rent"], made["security_deposit"], made["currency"]) == (
        85000.0,
        255000.0,
        "INR",
    )
    assert made["commercial_terms"].startswith("Lock-in")
    spaces = {s["space_name"]: s for s in made["spaces"]}
    assert set(spaces) == {"Seat 1", "Cabin 1"}
    assert (spaces["Seat 1"]["building_name"], spaces["Seat 1"]["floor_name"]) == (
        "Tower A",
        "Floor 5",
    )
    listed = api.get(f"{CRM}/leads/{made_lead['id']}/proposals").json()
    assert [p["id"] for p in listed] == [made["id"]]
    assert api.get(f"{CRM}/proposals/{made['id']}").json()["notes"] == "Prefers the corner cabin."


def test_proposal_validation(api: TestClient, hub: dict) -> None:
    lead_id = lead(api)["id"]
    other = create_property(api, name="Other Park")
    today = business_today()
    base = {"property_id": hub["property"]["id"]}
    # A space of another property, a non-rentable space, a space twice.
    propose(api, lead_id, 400, property_id=other["id"], space_ids=[hub["seat1"]["id"]])
    store = api.post(
        f"{V1}/admin/spaces",
        json={
            "property_id": hub["property"]["id"],
            "name": "Store room",
            "space_type": "NON_RENTABLE",
            "parent_space_id": hub["5A"]["id"],
        },
    )
    assert store.status_code == 201, store.text
    propose(api, lead_id, 400, **base, space_ids=[store.json()["id"]])
    propose(api, lead_id, 422, **base, space_ids=[hub["seat1"]["id"]] * 2)
    # Dates and amounts.
    propose(
        api,
        lead_id,
        422,
        **base,
        end_date=today.isoformat(),
        start_date=(today + timedelta(days=1)).isoformat(),
    )
    propose(
        api,
        lead_id,
        422,
        **base,
        valid_until=(today - timedelta(days=1)).isoformat(),
    )
    propose(api, lead_id, 422, **base, monthly_rent="-1")
    propose(api, lead_id, 422, **base, currency="inr")
    assert api.get(f"{CRM}/leads/{lead_id}/proposals").json() == []
    unknown = api.post(
        f"{CRM}/leads/00000000-0000-0000-0000-000000000000/proposals",
        json={**base, "proposal_date": today.isoformat(), "start_date": today.isoformat()},
    )
    assert unknown.status_code == 404


def test_proposals_can_be_made_at_any_stage_and_make_a_prospect(api: TestClient, hub: dict) -> None:
    fresh = lead(api)
    assert (fresh["stage"], fresh["is_prospect"]) == ("LEAD_CREATED", False)
    made = propose(api, fresh["id"], property_id=hub["property"]["id"])
    assert api.get(f"{CRM}/leads/{fresh['id']}").json()["is_prospect"] is True
    # The stage is untouched: a prospect is derived, not a stage.
    assert api.get(f"{CRM}/leads/{fresh['id']}").json()["stage"] == "LEAD_CREATED"

    later = lead(api, company_name="Beta LLP")
    for stage in ("LEAD_REVIEWED", "SPACE_SELECTED"):
        api.post(f"{CRM}/leads/{later['id']}/stage", json={"stage": stage})
    propose(api, later["id"], property_id=hub["property"]["id"])

    none = lead(api, company_name="Gamma Co")
    prospects = api.get(f"{CRM}/leads", params={"prospect": "true"}).json()["items"]
    assert {p["id"] for p in prospects} == {fresh["id"], later["id"]}
    others = api.get(f"{CRM}/leads", params={"prospect": "false"}).json()["items"]
    assert {p["id"] for p in others} == {none["id"]}

    # A withdrawn proposal no longer counts.
    step(api, made, "withdraw", reason="Replaced")
    assert api.get(f"{CRM}/leads/{fresh['id']}").json()["is_prospect"] is False


def test_lifecycle_edit_send_accept_and_the_one_accepted_rule(
    api: TestClient, hub: dict, db_session: Session
) -> None:
    lead_id = lead(api)["id"]
    first = propose(api, lead_id, property_id=hub["property"]["id"], monthly_rent="80000")
    # A draft can be edited, including its property and spaces.
    edited = api.patch(
        f"{CRM}/proposals/{first['id']}",
        json={"monthly_rent": "82000", "space_ids": [hub["seat2"]["id"]]},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["monthly_rent"] == 82000.0
    assert [s["space_name"] for s in edited.json()["spaces"]] == ["Seat 2"]
    other = create_property(api, name="Other Park")
    moved = api.patch(f"{CRM}/proposals/{first['id']}", json={"property_id": other["id"]})
    assert moved.status_code == 200, moved.text
    assert (moved.json()["property_name"], moved.json()["spaces"]) == ("Other Park", [])
    api.patch(
        f"{CRM}/proposals/{first['id']}",
        json={"property_id": hub["property"]["id"], "space_ids": [hub["seat1"]["id"]]},
    )

    # Acceptance needs a sent proposal and the acceptance recorded.
    step(api, first, "accept", 409, accepted_on=business_today().isoformat(), acceptance_note="x")
    sent = step(api, first, "send")
    assert sent["status"] == "SENT" and sent["sent_at"]
    assert api.patch(f"{CRM}/proposals/{first['id']}", json={"notes": "late"}).status_code == 409
    step(api, first, "accept", 422, accepted_on=business_today().isoformat())
    step(api, first, "accept", 422, accepted_on=business_today().isoformat(), acceptance_note="")

    before = counts(db_session)
    accepted = step(
        api,
        first,
        "accept",
        accepted_on=business_today().isoformat(),
        acceptance_note="Email from the director, 5 Nov",
    )
    assert accepted["status"] == "ACCEPTED"
    assert accepted["acceptance_note"] == "Email from the director, 5 Nov"
    assert accepted["accepted_by_email"]
    # Acceptance creates no contract, allocation, booking, tenant or account.
    assert counts(db_session) == before
    db_session.expire_all()
    assert db_session.get(Lead, accepted["lead_id"]).tenant_id is None

    # At most one accepted proposal per lead.
    second = propose(api, lead_id, property_id=hub["property"]["id"])
    step(api, second, "send")
    refused = step(
        api,
        second,
        "accept",
        409,
        accepted_on=business_today().isoformat(),
        acceptance_note="Phone call",
    )
    assert first["reference"] in refused["detail"]
    # Closed proposals stay closed.
    step(api, first, "withdraw", 409)
    step(api, first, "reject", 409)
    rejected = step(api, second, "reject", reason="Budget too low")
    assert (rejected["status"], rejected["closing_reason"]) == ("REJECTED", "Budget too low")
    step(api, second, "send", 409)

    # Every proposal is kept, newest first.
    listed = api.get(f"{CRM}/leads/{lead_id}/proposals").json()
    assert [p["id"] for p in listed] == [second["id"], first["id"]]


def test_a_sent_proposal_past_its_validity_shows_as_expired(
    api: TestClient, hub: dict, db_session: Session
) -> None:
    lead_id = lead(api)["id"]
    today = business_today()
    made = propose(
        api,
        lead_id,
        property_id=hub["property"]["id"],
        proposal_date=(today - timedelta(days=20)).isoformat(),
        valid_until=(today - timedelta(days=1)).isoformat(),
    )
    assert made["expired"] is False  # a draft never expires
    sent = step(api, made, "send")
    assert (sent["status"], sent["expired"]) == ("SENT", True)


def test_every_proposal_step_is_in_the_lead_history(api: TestClient, hub: dict) -> None:
    made_lead = lead(api)
    made = propose(api, made_lead["id"], property_id=hub["property"]["id"])
    api.patch(f"{CRM}/proposals/{made['id']}", json={"notes": "Call back"})
    step(api, made, "send")
    step(
        api,
        made,
        "accept",
        accepted_on=business_today().isoformat(),
        acceptance_note="Signed offer letter",
    )
    history = api.get(
        f"{V1}/admin/activity", params={"entity_type": "leads", "entity_id": made_lead["id"]}
    ).json()["items"]
    actions = {e["action"] for e in history}
    assert {
        "proposal_created",
        "proposal_updated",
        "proposal_sent",
        "proposal_accepted",
    } <= actions
    accepted = next(e for e in history if e["action"] == "proposal_accepted")
    assert "no contract or allocation was created" in accepted["message"]
