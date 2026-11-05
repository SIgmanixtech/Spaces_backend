# ruff: noqa: F811  (pytest fixtures shared from other test modules)
"""Tenant accounts (read-only Accounts phase; BR-017, BR-020, BR-025, BR-030). Needs the
migrated test database."""

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Account, ActivityLog
from tests.test_agreements import make_docx, sign_with_copy, storage, upload  # noqa: F401
from tests.test_client_access import agreement, contract_for, new_tenant
from tests.test_client_api import hub, priya  # noqa: F401
from tests.test_resources import V1

ADMIN = f"{V1}/admin"


def test_only_admins_reach_accounts(client: TestClient, priya: TestClient) -> None:
    for who, status in ((client, 401), (priya, 403)):
        assert who.get(f"{ADMIN}/accounts").status_code == status
        assert (
            who.get(f"{ADMIN}/accounts/00000000-0000-0000-0000-000000000000").status_code == status
        )
    assert priya.get(f"{V1}/client/accounts").status_code == 404


def test_one_account_per_tenant_with_a_completed_contract(
    api: TestClient, hub: dict, storage: object, db_session: Session
) -> None:
    tenant = new_tenant(api)
    contract = contract_for(api, tenant, hub)
    agreement(api, contract, up_to="sign")  # approved and sent, not signed: not completed
    assert api.get(f"{ADMIN}/accounts").json()["total"] == 0
    assert api.get(f"{ADMIN}/accounts/by-tenant/{tenant['id']}").json() == {"account_id": None}
    document = api.get(f"{ADMIN}/contracts/{contract['id']}/agreement").json()["documents"][0]
    sign_with_copy(api, document["id"])

    for _ in range(3):  # reading again never opens a second account
        listed = api.get(f"{ADMIN}/accounts").json()
    assert listed["total"] == 1
    assert db_session.scalar(select(func.count()).select_from(Account)) == 1
    opened = db_session.scalars(
        select(ActivityLog).where(ActivityLog.entity_type == "accounts")
    ).all()
    assert [e.action for e in opened] == ["opened"]
    account = listed["items"][0]
    assert (account["tenant_id"], account["completed_contracts"], account["contracts"]) == (
        tenant["id"],
        1,
        1,
    )
    assert api.get(f"{ADMIN}/accounts/by-tenant/{tenant['id']}").json() == {
        "account_id": account["id"]
    }


def test_an_account_reads_the_same_contract_records_and_holds_deposits_apart(
    api: TestClient, hub: dict, storage: object
) -> None:
    tenant = new_tenant(api)
    contract = contract_for(api, tenant, hub)
    api.patch(f"{ADMIN}/contracts/{contract['id']}", json={"security_deposit": 24000})
    agreement(api, contract, up_to="done")
    account_id = api.get(f"{ADMIN}/accounts").json()["items"][0]["id"]
    detail = api.get(f"{ADMIN}/accounts/{account_id}").json()

    same = api.get(f"{ADMIN}/contracts", params={"tenant_id": tenant["id"]}).json()["items"]
    assert [c["id"] for c in detail["contract_list"]] == [c["id"] for c in same]  # BR-020
    assert detail["contract_list"][0]["completes_contract"] is True
    assert detail["deposits"] == [
        {
            "contract_id": contract["id"],
            "contract_reference": contract["reference"],
            "contract_status": detail["contract_list"][0]["status"],
            "amount": "24000.00",
            "currency": "INR",
        }
    ]
    assert detail["agreed_deposits"] == [{"currency": "INR", "amount": "24000.00"}]
    assert detail["lead_id"] is None  # filled once lead conversion exists
    assert any("D6" in item for item in detail["not_available_yet"])
    assert any("D5" in item for item in detail["not_available_yet"])
    # Nothing financial is invented: no invoice, payment or revenue fields at all.
    for key in ("invoices", "payments", "balance", "revenue", "outstanding"):
        assert key not in detail


def test_contracts_filter_by_completion(api: TestClient, hub: dict, storage: object) -> None:
    done_tenant = new_tenant(api)
    done = contract_for(api, done_tenant, hub)
    agreement(api, done, up_to="done")
    completed = api.get(f"{ADMIN}/contracts", params={"completed": "true"}).json()["items"]
    assert [c["id"] for c in completed] == [done["id"]]
    assert all(c["completes_contract"] for c in completed)
    rest = api.get(f"{ADMIN}/contracts", params={"completed": "false"}).json()["items"]
    assert done["id"] not in [c["id"] for c in rest]
