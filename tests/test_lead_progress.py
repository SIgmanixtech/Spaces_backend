# ruff: noqa: F811  (pytest fixtures shared from other test modules)
"""Automatic lead stages after Space Selected (owner decisions L-2 and L-4, 2 Nov 2026).
Needs the migrated test database (like every API test here)."""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Tenant
from app.services.occupancy import business_today
from tests.test_agreements import storage  # noqa: F401
from tests.test_client_access import agreement
from tests.test_client_api import hub, priya  # noqa: F401
from tests.test_crm import CRM, lead
from tests.test_resources import V1

ADMIN = f"{V1}/admin"


def stage_of(api: TestClient, lead_id: str) -> str:
    return api.get(f"{CRM}/leads/{lead_id}").json()["pipeline_stage"]


def auto_entries(api: TestClient, lead_id: str) -> list[str]:
    items = api.get(
        f"{ADMIN}/activity", params={"entity_type": "leads", "entity_id": lead_id}
    ).json()["items"]
    return [e["message"] for e in items if e["action"] == "stage_auto"]


def test_stages_after_space_selected_follow_the_records(
    api: TestClient, hub: dict, storage: object, db_session: Session
) -> None:
    made = lead(api)
    today = business_today()
    proposal = api.post(
        f"{CRM}/leads/{made['id']}/proposals",
        json={
            "property_id": hub["property"]["id"],
            "space_ids": [hub["cabin"]["id"]],
            "proposal_date": today.isoformat(),
            "start_date": (today + timedelta(days=10)).isoformat(),
            "monthly_rent": "12000",
        },
    ).json()
    api.post(f"{CRM}/proposals/{proposal['id']}/send")
    api.post(
        f"{CRM}/proposals/{proposal['id']}/accept",
        json={"accepted_on": today.isoformat(), "acceptance_note": "Email"},
    )
    tenant = api.post(f"{ADMIN}/tenants", json={"name": "Acme Ltd", "email": "a@acme.example"})
    api.post(f"{CRM}/leads/{made['id']}/tenant", json={"tenant_id": tenant.json()["id"]})
    for stage in ("LEAD_REVIEWED", "SPACE_SELECTED"):
        api.post(f"{CRM}/leads/{made['id']}/stage", json={"stage": stage})
    assert stage_of(api, made["id"]) == "SPACE_SELECTED"
    assert auto_entries(api, made["id"]) == []

    contract = api.post(f"{ADMIN}/contracts/from-proposal/{proposal['id']}").json()
    assert stage_of(api, made["id"]) == "CONTRACT_PREPARED"
    assert auto_entries(api, made["id"])[0].endswith(
        f"to Contract Prepared automatically (contract {contract['reference']} created from "
        f"proposal {proposal['reference']})."
    )
    # The stored stage is not changed and cannot be set by hand past Space Selected.
    lead_now = api.get(f"{CRM}/leads/{made['id']}").json()
    assert (lead_now["stage"], lead_now["next_stage"]) == ("SPACE_SELECTED", None)
    assert (
        api.post(f"{CRM}/leads/{made['id']}/stage", json={"stage": "CONTRACT_PREPARED"})
    ).status_code == 422

    # Approved but not signed: still prepared, no new entry (no duplicates).
    api.patch(f"{ADMIN}/contracts/{contract['id']}", json={"contract_type": "SPACE_LEASE"})
    assert api.post(f"{ADMIN}/contracts/{contract['id']}/activate").status_code == 200
    assert stage_of(api, made["id"]) == "CONTRACT_PREPARED"
    assert len(auto_entries(api, made["id"])) == 1

    # Signed by both parties (BR-025 complete): Contract Completed (L-4).
    agreement(api, contract, up_to="done")
    assert stage_of(api, made["id"]) == "CONTRACT_COMPLETED"
    entries = auto_entries(api, made["id"])
    assert len(entries) == 2 and "to Contract Completed automatically (signed copy of" in entries[0]

    # The tenant activates client-app access: Tenant Activated.
    row = db_session.get(Tenant, tenant.json()["id"])
    row.client_invited_at = row.client_activated_at = datetime.now(UTC)
    db_session.commit()
    assert stage_of(api, made["id"]) == "TENANT_ACTIVATED"

    # Terminating later keeps the history: the lead stays past Contract Completed.
    terminated = api.post(
        f"{ADMIN}/contracts/{contract['id']}/terminate",
        json={"termination_date": (today + timedelta(days=20)).isoformat()},
    )
    assert terminated.status_code == 200, terminated.text
    assert stage_of(api, made["id"]) == "TENANT_ACTIVATED"
    # The filter by stored stage still works as before.
    listed = api.get(f"{CRM}/leads", params={"stage": "SPACE_SELECTED"}).json()["items"]
    assert [x["pipeline_stage"] for x in listed] == ["TENANT_ACTIVATED"]


def test_cancelling_the_contract_moves_the_lead_back_and_says_so(
    api: TestClient, hub: dict
) -> None:
    made = lead(api, company_name="Beta LLP")
    today = business_today()
    proposal = api.post(
        f"{CRM}/leads/{made['id']}/proposals",
        json={
            "property_id": hub["property"]["id"],
            "proposal_date": today.isoformat(),
            "start_date": (today + timedelta(days=30)).isoformat(),
        },
    ).json()
    api.post(f"{CRM}/proposals/{proposal['id']}/send")
    api.post(
        f"{CRM}/proposals/{proposal['id']}/accept",
        json={"accepted_on": today.isoformat(), "acceptance_note": "Call"},
    )
    tenant = api.post(f"{ADMIN}/tenants", json={"name": "Beta LLP"}).json()
    api.post(f"{CRM}/leads/{made['id']}/tenant", json={"tenant_id": tenant["id"]})
    for stage in ("LEAD_REVIEWED", "SPACE_SELECTED"):
        api.post(f"{CRM}/leads/{made['id']}/stage", json={"stage": stage})
    contract = api.post(f"{ADMIN}/contracts/from-proposal/{proposal['id']}").json()
    assert stage_of(api, made["id"]) == "CONTRACT_PREPARED"
    cancelled = api.post(f"{ADMIN}/contracts/{contract['id']}/cancel", json={"reason": "Lost"})
    assert cancelled.status_code == 200, cancelled.text
    assert stage_of(api, made["id"]) == "SPACE_SELECTED"
    entries = auto_entries(api, made["id"])
    assert len(entries) == 2
    assert "from Contract Prepared to Space Selected automatically (contract" in entries[0]
