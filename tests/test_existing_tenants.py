# ruff: noqa: F811  (pytest fixtures shared from other test modules)
"""Existing tenants in admin (ET1-ET4): duplicate warnings on the tenant form, each
contract's agreement status and whether it completes the contract (BR-025). Needs the
migrated test database (like every API test here)."""

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Tenant
from app.services.client_access import completed_contract_ids, has_completed_contract
from tests.test_agreements import make_docx, sign_with_copy, storage, upload  # noqa: F401
from tests.test_client_access import agreement, contract_for, invites, new_tenant  # noqa: F401
from tests.test_client_api import hub, priya  # noqa: F401
from tests.test_resources import V1

ADMIN = f"{V1}/admin"
CHECK = f"{ADMIN}/tenants/check-duplicates"


def check(api: TestClient, **body: object) -> list[dict]:
    response = api.post(CHECK, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_only_admins_check_tenant_duplicates(client: TestClient, priya: TestClient) -> None:
    for who, status in ((client, 401), (priya, 403)):
        assert who.post(CHECK, json={"email": "a@b.example"}).status_code == status
    # Not reachable through the client prefix either.
    assert priya.post(f"{V1}/client/tenants/check-duplicates", json={}).status_code == 404


def test_tenant_duplicates_match_email_phone_and_company_never_name_alone(
    api: TestClient,
) -> None:
    acme = new_tenant(api, name="Acme Pvt Ltd", company_name="ACME Ltd.", phone="+91 98765 43210")
    api.post(f"{ADMIN}/crm/leads", json={"company_name": "Acme Ltd", "contact_email": "x@y.in"})

    assert check(api) == []  # nothing typed yet
    by_email = check(api, email=" OPS@Acme.Example ")
    assert [(m["kind"], m["id"], m["matched_on"]) for m in by_email] == [
        ("TENANT", acme["id"], ["email"])
    ]
    by_phone = check(api, phone="098765-43210")
    assert [m["matched_on"] for m in by_phone] == [["phone"]]
    by_company = check(api, company_name="acme  ltd")
    # Tenants only: the matching lead is CRM information and is not listed here.
    assert [(m["kind"], m["matched_on"]) for m in by_company] == [("TENANT", ["company"])]
    assert by_company[0]["name"] == "Acme Pvt Ltd" and by_company[0]["status"] == "ACTIVE"
    # The same name with nothing else in common is not a match.
    assert check(api, company_name="Acme Pvt Ltd Holdings", email="other@else.example") == []
    # Editing: the tenant does not match itself.
    assert check(api, email="ops@acme.example", exclude_tenant_id=acme["id"]) == []


def test_a_possible_duplicate_never_blocks_or_merges(api: TestClient, db_session: Session) -> None:
    first = new_tenant(api)
    second = api.post(f"{ADMIN}/tenants", json={"name": "Acme again", "email": "ops@acme.example"})
    assert second.status_code == 201, second.text
    edited = api.patch(f"{ADMIN}/tenants/{first['id']}", json={"phone": "98765 43210"})
    assert edited.status_code == 200, edited.text
    assert db_session.scalar(select(func.count()).select_from(Tenant)) == 2
    assert {m["id"] for m in check(api, email="ops@acme.example")} == {
        first["id"],
        second.json()["id"],
    }


def test_crm_duplicate_check_still_includes_leads_and_tenants(api: TestClient) -> None:
    new_tenant(api)
    api.post(
        f"{ADMIN}/crm/leads", json={"company_name": "Lead Co", "contact_email": "ops@acme.example"}
    )
    found = api.post(
        f"{ADMIN}/crm/leads/check-duplicates", json={"contact_email": "ops@acme.example"}
    )
    assert sorted(m["kind"] for m in found.json()) == ["LEAD", "TENANT"]


def test_tenant_contracts_show_agreement_status_and_completion(
    api: TestClient, hub: dict, storage: object, db_session: Session
) -> None:
    tenant = new_tenant(api)
    other = new_tenant(api, name="Other Ltd", email="other@else.example")
    contract = contract_for(api, tenant, hub)

    def mine() -> list[dict]:
        listed = api.get(f"{ADMIN}/contracts", params={"tenant_id": tenant["id"]})
        assert listed.status_code == 200, listed.text
        return listed.json()["items"]

    assert [(c["id"], c["agreement_status"], c["completes_contract"]) for c in mine()] == [
        (contract["id"], None, False)
    ]
    assert api.get(f"{ADMIN}/contracts", params={"tenant_id": other["id"]}).json()["items"] == []
    agreement(api, contract, up_to="sign")  # generated, approved and sent, not signed
    assert (mine()[0]["agreement_status"], mine()[0]["completes_contract"]) == (
        "SENT_FOR_SIGNATURE",
        False,
    )
    blocker = api.get(f"{ADMIN}/tenants/{tenant['id']}").json()["client_access_blocker"]
    assert "no completed contract" in blocker
    document = api.get(f"{ADMIN}/contracts/{contract['id']}/agreement").json()["documents"][0]
    sign_with_copy(api, document["id"])
    assert (mine()[0]["agreement_status"], mine()[0]["completes_contract"]) == ("SIGNED", True)
    assert api.get(f"{ADMIN}/tenants/{tenant['id']}").json()["client_access_blocker"] is None
    # The per-contract flag and the invitation rule are the same rule (BR-025).
    row = db_session.get(Tenant, uuid.UUID(tenant["id"]))
    assert row is not None and has_completed_contract(db_session, row)
    contract_id = uuid.UUID(contract["id"])
    assert completed_contract_ids(db_session, {contract_id}) == {contract_id}


def test_a_pending_contract_with_a_signed_agreement_does_not_complete(
    api: TestClient, hub: dict, storage: object
) -> None:
    tenant = new_tenant(api)
    pending = api.post(
        f"{ADMIN}/contracts",
        json={
            "tenant_id": tenant["id"],
            "property_id": hub["property"]["id"],
            "space_id": hub["cabin"]["id"],
            "start_date": "2027-03-01",
            "activate": False,
        },
    )
    assert pending.status_code == 201, pending.text
    read = api.get(f"{ADMIN}/contracts/{pending.json()['id']}").json()
    assert (read["status"], read["agreement_status"], read["completes_contract"]) == (
        "PENDING",
        None,
        False,
    )


def test_tenant_creation_and_edits_are_in_its_history(api: TestClient) -> None:
    tenant = new_tenant(api)
    edited = api.patch(
        f"{ADMIN}/tenants/{tenant['id']}", json={"phone": "98765 43210", "email": "new@acme.in"}
    )
    assert edited.status_code == 200, edited.text
    api.patch(f"{ADMIN}/tenants/{tenant['id']}", json={"phone": "98765 43210"})
    history = api.get(
        f"{ADMIN}/activity", params={"entity_type": "tenants", "entity_id": tenant["id"]}
    ).json()["items"]
    messages = [e["message"] for e in history]
    assert 'Tenant "Acme Pvt Ltd" was created.' in messages
    assert 'Tenant "Acme Pvt Ltd" was updated: email, phone.' in messages
    assert 'Tenant "Acme Pvt Ltd" was saved without changes.' in messages
    # Field names only: the new values are not copied into the history.
    assert all("new@acme.in" not in m for m in messages)
