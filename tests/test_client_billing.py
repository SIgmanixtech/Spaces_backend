# ruff: noqa: F811  (pytest fixtures shared from other test modules)
"""The customer's own invoices, payments and deposits in the client app (read-only).
Drafts and other customers' records are never shown; only verified payments count as paid."""

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Tenant
from tests.test_agreements import storage  # noqa: F401
from tests.test_billing import ADMIN, draft, pay
from tests.test_client_access import agreement, contract_for, new_tenant
from tests.test_client_api import hub, priya  # noqa: F401
from tests.test_resources import V1

CLIENT = f"{V1}/client"


def test_customers_see_their_issued_invoices_payments_and_deposits(
    api: TestClient, hub: dict, storage: object, priya: TestClient, db_session: Session
) -> None:
    mine = db_session.scalar(select(Tenant).where(Tenant.email == "priya@example.com"))
    contract = contract_for(api, {"id": str(mine.id)}, hub)  # 12,000 a month, Cabin 1
    api.patch(f"{ADMIN}/contracts/{contract['id']}", json={"security_deposit": "24000"})
    agreement(api, contract, up_to="done")
    account = api.get(f"{ADMIN}/accounts/by-tenant/{mine.id}").json()["account_id"]

    hidden = draft(api, account, contract_id=contract["id"], notes="internal note")
    issued = draft(api, account, contract_id=contract["id"], notes="internal note")
    lines = [{"category": "SPACE_FEE", "description": "Space Fee, November", "amount": "12000"}]
    api.put(f"{ADMIN}/invoices/{issued['id']}/lines", json={"lines": lines})
    issued = api.post(f"{ADMIN}/invoices/{issued['id']}/issue").json()
    verified = pay(api, issued, amount="5000", external_reference="UTR9")
    api.post(f"{ADMIN}/payments/{verified['id']}/verify")
    pay(api, issued, amount="1000", external_reference="UTR10")  # recorded only

    # Another customer's invoice is never shown.
    other = new_tenant(api, name="Other Ltd", email="o@other.example")
    other_contract = contract_for(api, other, {**hub, "cabin": hub["seat1"]})
    agreement(api, other_contract, up_to="done")
    other_account = api.get(f"{ADMIN}/accounts/by-tenant/{other['id']}").json()["account_id"]
    theirs = draft(api, other_account)
    api.put(f"{ADMIN}/invoices/{theirs['id']}/lines", json={"lines": lines})
    api.post(f"{ADMIN}/invoices/{theirs['id']}/issue")

    listed = priya.get(f"{CLIENT}/invoices")
    assert listed.status_code == 200, listed.text
    invoices = listed.json()
    assert [i["reference"] for i in invoices] == [issued["reference"]]
    shown = invoices[0]
    assert "notes" not in shown and "account_id" not in shown
    assert (float(shown["total"]), float(shown["paid"])) == (12000.0, 5000.0)
    assert float(shown["awaiting_verification"]) == 1000.0
    assert (shown["payment_state"], float(shown["outstanding"])) == ("PARTLY_PAID", 7000.0)

    detail = priya.get(f"{CLIENT}/invoices/{issued['id']}").json()
    assert {p["status"] for p in detail["payments"]} == {"VERIFIED", "RECORDED"}
    assert priya.get(f"{CLIENT}/invoices/{hidden['id']}").status_code == 404  # draft
    assert priya.get(f"{CLIENT}/invoices/{theirs['id']}").status_code == 404  # not mine

    payments = priya.get(f"{CLIENT}/payments").json()
    assert sorted(p["external_reference"] for p in payments) == ["UTR10", "UTR9"]
    assert all("notes" not in p and "reject_reason" not in p for p in payments)

    deposits = priya.get(f"{CLIENT}/deposits").json()
    assert [(d["contract_reference"], float(d["amount"])) for d in deposits] == [
        (contract["reference"], 24000.0)
    ]


def test_billing_needs_an_activated_customer(client: TestClient, api: TestClient) -> None:
    for path in ("invoices", "payments", "deposits"):
        assert client.get(f"{CLIENT}/{path}").status_code == 401
        assert api.get(f"{CLIENT}/{path}").status_code == 403
