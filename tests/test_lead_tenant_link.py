# ruff: noqa: F811  (pytest fixtures shared from test_client_api)
"""Linking a lead to a tenant record (Phase 4A, BR-007). Needs the migrated test database
(like every API test here)."""

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Account, Tenant, TenantOccupancy
from app.services.occupancy import business_today
from tests.test_client_api import hub, priya  # noqa: F401
from tests.test_crm import CRM, lead
from tests.test_resources import V1

TENANTS = f"{V1}/admin/tenants"


def tenant(api: TestClient, **body: object) -> dict:
    response = api.post(TENANTS, json={"name": "Acme Ltd", **body})
    assert response.status_code == 201, response.text
    return response.json()


def history(api: TestClient, entity_type: str, entity_id: str) -> list[dict]:
    return api.get(
        f"{V1}/admin/activity", params={"entity_type": entity_type, "entity_id": entity_id}
    ).json()["items"]


def test_only_admins_link_leads(client: TestClient, priya: TestClient) -> None:
    lead_id = "00000000-0000-0000-0000-000000000000"
    for who, status in ((client, 401), (priya, 403)):
        assert who.post(f"{CRM}/leads/{lead_id}/tenant", json={}).status_code == status
        assert who.post(f"{CRM}/leads/{lead_id}/tenant/new", json={}).status_code == status
        assert who.post(f"{CRM}/leads/{lead_id}/tenant/unlink", json={}).status_code == status


def test_link_a_lead_to_an_existing_tenant(api: TestClient, db_session: Session) -> None:
    made = lead(api, contact_email="ops@acme.in")
    existing = tenant(api, email="ops@acme.in")
    linked = api.post(f"{CRM}/leads/{made['id']}/tenant", json={"tenant_id": existing["id"]})
    assert linked.status_code == 200, linked.text
    body = linked.json()
    assert (body["tenant_id"], body["tenant_name"]) == (existing["id"], "Acme Ltd")
    assert body["tenant_linked_at"]
    # The lead itself is kept as it was.
    assert (body["reference"], body["stage"], body["source"]) == (
        made["reference"],
        made["stage"],
        made["source"],
    )
    # Several leads may share a tenant; the tenant filter finds them.
    again = lead(api, company_name="Acme Ltd (second enquiry)")
    api.post(f"{CRM}/leads/{again['id']}/tenant", json={"tenant_id": existing["id"]})
    found = api.get(f"{CRM}/leads", params={"tenant_id": existing["id"]}).json()["items"]
    assert {f["id"] for f in found} == {made["id"], again["id"]}
    # A second link is refused until unlinked; nothing changes.
    other = tenant(api, name="Beta LLP")
    refused = api.post(f"{CRM}/leads/{made['id']}/tenant", json={"tenant_id": other["id"]})
    assert refused.status_code == 409 and "Unlink it first" in refused.json()["detail"]
    assert api.get(f"{CRM}/leads/{made['id']}").json()["tenant_id"] == existing["id"]
    # No client access, contract or account comes with the link.
    assert api.get(f"{TENANTS}/{existing['id']}").json()["client_access"] == "NOT_INVITED"
    assert db_session.scalar(select(func.count()).select_from(TenantOccupancy)) == 0
    assert db_session.scalar(select(func.count()).select_from(Account)) == 0
    # Both histories record it.
    assert any(e["action"] == "tenant_linked" for e in history(api, "leads", made["id"]))
    assert any(e["action"] == "lead_linked" for e in history(api, "tenants", existing["id"]))


def test_link_refuses_unknown_and_archived_tenants(api: TestClient, db_session: Session) -> None:
    made = lead(api)
    missing = "00000000-0000-0000-0000-000000000000"
    assert (
        api.post(f"{CRM}/leads/{made['id']}/tenant", json={"tenant_id": missing}).status_code == 404
    )
    gone = tenant(api, name="Gone Ltd")
    db_session.get(Tenant, gone["id"]).archived_at = db_session.scalar(select(func.now()))
    db_session.commit()
    assert (
        api.post(f"{CRM}/leads/{made['id']}/tenant", json={"tenant_id": gone["id"]}).status_code
        == 400
    )
    assert (
        api.post(f"{CRM}/leads/{missing}/tenant", json={"tenant_id": gone["id"]}).status_code == 404
    )


def test_create_a_tenant_from_a_lead(api: TestClient, db_session: Session) -> None:
    made = lead(
        api, company_name="Nair & Co", contact_name="Priya Nair", contact_phone="98765 43210"
    )
    created = api.post(
        f"{CRM}/leads/{made['id']}/tenant/new",
        json={
            "name": "Priya Nair",
            "company_name": "Nair & Co",
            "phone": "98765 43210",
            "gstin": "36abcde1234f1z5",
            "signatory_name": "Priya Nair",
        },
    )
    assert created.status_code == 201, created.text
    tenant_id = created.json()["tenant_id"]
    saved = api.get(f"{TENANTS}/{tenant_id}").json()
    assert (saved["name"], saved["gstin"], saved["signatory_name"]) == (
        "Priya Nair",
        "36ABCDE1234F1Z5",
        "Priya Nair",
    )
    # A retry (double click) is refused and makes no second tenant.
    retry = api.post(
        f"{CRM}/leads/{made['id']}/tenant/new",
        json={"name": "Priya Nair", "confirm_despite_matches": True},
    )
    assert retry.status_code == 409
    assert db_session.scalar(select(func.count()).select_from(Tenant)) == 1
    created_entries = [e for e in history(api, "tenants", tenant_id) if e["action"] == "created"]
    assert created_entries and made["reference"] in created_entries[0]["message"]
    # Legal values never appear in history.
    for entries in (history(api, "tenants", tenant_id), history(api, "leads", made["id"])):
        assert all("36ABCDE1234F1Z5" not in e["message"] for e in entries)


def test_possible_matches_need_explicit_confirmation(api: TestClient, db_session: Session) -> None:
    existing = tenant(api, email="ops@acme.in", phone="+91 98765 43210")
    made = lead(api, contact_email="OPS@acme.in")
    body = {"name": "Acme Limited", "email": "ops@acme.in"}
    refused = api.post(f"{CRM}/leads/{made['id']}/tenant/new", json=body)
    assert refused.status_code == 409
    conflicts = refused.json()["conflicts"]
    assert [(c["kind"], c["id"], c["matched_on"]) for c in conflicts] == [
        ("TENANT", existing["id"], ["email"])
    ]
    # Nothing was created or linked.
    assert db_session.scalar(select(func.count()).select_from(Tenant)) == 1
    assert api.get(f"{CRM}/leads/{made['id']}").json()["tenant_id"] is None

    confirmed = api.post(
        f"{CRM}/leads/{made['id']}/tenant/new", json={**body, "confirm_despite_matches": True}
    )
    assert confirmed.status_code == 201, confirmed.text
    assert confirmed.json()["tenant_id"] != existing["id"]
    entry = next(e for e in history(api, "leads", made["id"]) if e["action"] == "tenant_linked")
    assert "despite possible matches" in entry["message"]
    # The existing warning endpoints keep working as before (warning only).
    warned = api.post(f"{TENANTS}/check-duplicates", json={"email": "ops@acme.in"}).json()
    assert len(warned) == 2


def test_unlink_needs_a_reason_and_keeps_everything_else(api: TestClient) -> None:
    made = lead(api)
    api.post(f"{CRM}/leads/{made['id']}/notes", json={"body": "Wants two cabins."})
    api.post(
        f"{CRM}/leads/{made['id']}/tasks",
        json={"title": "Call back", "due_date": (business_today() + timedelta(days=2)).isoformat()},
    )
    existing = tenant(api)
    assert (
        api.post(f"{CRM}/leads/{made['id']}/tenant/unlink", json={"reason": "x"}).status_code == 409
    )
    api.post(f"{CRM}/leads/{made['id']}/tenant", json={"tenant_id": existing["id"]})
    assert api.post(f"{CRM}/leads/{made['id']}/tenant/unlink", json={}).status_code == 422
    assert (
        api.post(f"{CRM}/leads/{made['id']}/tenant/unlink", json={"reason": "   "}).status_code
        == 422
    )
    unlinked = api.post(
        f"{CRM}/leads/{made['id']}/tenant/unlink", json={"reason": "Linked to the wrong company"}
    )
    assert unlinked.status_code == 200, unlinked.text
    assert (unlinked.json()["tenant_id"], unlinked.json()["tenant_linked_at"]) == (None, None)
    # Notes, follow-ups and history are untouched; the tenant still exists.
    assert len(api.get(f"{CRM}/leads/{made['id']}/notes").json()) == 1
    assert len(api.get(f"{CRM}/leads/{made['id']}/tasks").json()["items"]) == 1
    assert api.get(f"{TENANTS}/{existing['id']}").status_code == 200
    for entity_type, entity_id, action in (
        ("leads", made["id"], "tenant_unlinked"),
        ("tenants", existing["id"], "lead_unlinked"),
    ):
        entry = next(e for e in history(api, entity_type, entity_id) if e["action"] == action)
        assert entry["message"].endswith("Reason: Linked to the wrong company")
    # It can be linked again afterwards.
    again = api.post(f"{CRM}/leads/{made['id']}/tenant", json={"tenant_id": existing["id"]})
    assert again.status_code == 200


def test_a_linked_tenant_is_archived_not_deleted(api: TestClient) -> None:
    made = lead(api)
    existing = tenant(api)
    api.post(f"{CRM}/leads/{made['id']}/tenant", json={"tenant_id": existing["id"]})
    removed = api.delete(f"{TENANTS}/{existing['id']}")
    assert removed.status_code == 200, removed.text
    assert removed.json()["outcome"] == "ARCHIVED"
    assert api.get(f"{CRM}/leads/{made['id']}").json()["tenant_id"] == existing["id"]
