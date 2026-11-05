# ruff: noqa: F811  (pytest fixtures shared from other test modules)
"""Invoices, payments, balances, deposits, and spaces and bookings (BR-009, BR-018, BR-019,
BR-029, BR-030). Needs the migrated test database (like every API test here)."""

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.services.occupancy import business_today
from tests.test_agreements import make_docx, sign_with_copy, storage, upload  # noqa: F401
from tests.test_client_access import agreement, contract_for, new_tenant
from tests.test_client_api import hub, priya  # noqa: F401
from tests.test_resources import V1

ADMIN = f"{V1}/admin"
R = f"{V1}/reimbursements"


def account_with_contract(api: TestClient, hub: dict, **tenant: object) -> tuple[str, dict]:
    made = new_tenant(api, **tenant)
    contract = contract_for(api, made, hub)  # 12,000 a month, approved, Cabin 1
    agreement(api, contract, up_to="done")  # signed: completed contract, account opens
    account = api.get(f"{ADMIN}/accounts/by-tenant/{made['id']}").json()["account_id"]
    assert account
    return account, contract


def draft(api: TestClient, account: str, expected: int = 201, **body: object) -> dict:
    response = api.post(
        f"{ADMIN}/invoices",
        json={"account_id": account, "invoice_date": business_today().isoformat(), **body},
    )
    assert response.status_code == expected, response.text
    return response.json()


def reviewed_expense(api: TestClient, contract: dict, amount: int = 1500) -> dict:
    water = next(t["id"] for t in api.get(f"{R}/expense-types").json() if t["name"] == "Water")
    made = api.post(
        f"{R}/expenses",
        json={
            "contract_id": contract["id"],
            "expense_type_id": water,
            "period_start": "2026-10-01",
            "period_end": "2026-10-31",
            "description": "October water",
            "amount": amount,
            "one_time": True,
        },
    )
    assert made.status_code == 201, made.text
    done = api.post(f"{R}/expenses/{made.json()['id']}/review", json={"decision": "REVIEWED"})
    assert done.status_code == 200, done.text
    return done.json()


def pay(api: TestClient, invoice: dict, expected: int = 201, **body: object) -> dict:
    response = api.post(
        f"{ADMIN}/invoices/{invoice['id']}/payments",
        json={"paid_on": business_today().isoformat(), "method": "UPI", **body},
    )
    assert response.status_code == expected, response.text
    return response.json()


def test_only_admins_reach_billing(client: TestClient, priya: TestClient) -> None:
    for who, status in ((client, 401), (priya, 403)):
        for path in ("invoices", "payments", "billing/balances", "billing/deposits"):
            assert who.get(f"{ADMIN}/{path}").status_code == status
    assert priya.get(f"{V1}/client/invoices").status_code == 404


def test_invoice_lifecycle_lines_suggestions_and_payments(
    api: TestClient, hub: dict, storage: object
) -> None:
    account, contract = account_with_contract(api, hub)
    expense = reviewed_expense(api, contract)
    invoice = draft(
        api,
        account,
        contract_id=contract["id"],
        due_date=(business_today() + timedelta(days=10)).isoformat(),
        period_start="2026-11-01",
        period_end="2026-11-30",
    )
    assert invoice["reference"].startswith(f"INV-{business_today().year}-")
    assert (invoice["status"], invoice["total"], invoice["payment_state"]) == (
        "DRAFT",
        "0",
        None,
    ) or (
        invoice["status"],
        float(invoice["total"]),
        invoice["payment_state"],
    ) == ("DRAFT", 0.0, None)

    # Suggestions: the contract's agreed rent and the reviewed expense, nothing invented.
    suggested = api.get(f"{ADMIN}/invoices/{invoice['id']}/suggestions").json()
    by_category = {s["category"]: s for s in suggested}
    assert float(by_category["SPACE_FEE"]["amount"]) == 12000.0
    assert by_category["REIMBURSEMENT_FEE"]["expense_id"] == expense["id"]
    assert float(by_category["REIMBURSEMENT_FEE"]["amount"]) == expense["total"]
    assert "MAINTENANCE_FEE" not in by_category

    # Issuing an empty draft is refused.
    assert api.post(f"{ADMIN}/invoices/{invoice['id']}/issue").status_code == 409
    lines = [
        {"category": "SPACE_FEE", "description": "Space Fee, November", "amount": "12000"},
        {
            "category": "MAINTENANCE_FEE",
            "description": "Maintenance, November",
            "amount": "1800.50",
        },
        {
            "category": "REIMBURSEMENT_FEE",
            "description": "October water",
            "amount": str(expense["total"]),
            "expense_id": expense["id"],
        },
    ]
    set_ = api.put(f"{ADMIN}/invoices/{invoice['id']}/lines", json={"lines": lines})
    assert set_.status_code == 200, set_.text
    body = set_.json()
    # Each category separately; the total is the sum of the lines (BR-009, BR-018).
    assert [c["category"] for c in body["category_totals"]] == [
        "SPACE_FEE",
        "MAINTENANCE_FEE",
        "REIMBURSEMENT_FEE",
    ]
    total = 12000 + 1800.50 + expense["total"]
    assert float(body["total"]) == total
    assert (
        api.put(
            f"{ADMIN}/invoices/{invoice['id']}/lines",
            json={"lines": [{"category": "ADVANCE", "description": "x", "amount": "1"}]},
        ).status_code
        == 422
    )

    issued = api.post(f"{ADMIN}/invoices/{invoice['id']}/issue").json()
    assert (issued["status"], issued["payment_state"], float(issued["outstanding"])) == (
        "ISSUED",
        "UNPAID",
        total,
    )
    # Issued: no more edits.
    assert api.patch(f"{ADMIN}/invoices/{invoice['id']}", json={"notes": "x"}).status_code == 409
    assert api.put(f"{ADMIN}/invoices/{invoice['id']}/lines", json={"lines": []}).status_code == 409

    # Payments: never more than is left; a reference only once; verified ones count.
    pay(api, issued, 409, amount=str(total + 1), external_reference="UTR1")
    pay(api, issued, 400, amount="10", paid_on=(business_today() + timedelta(days=1)).isoformat())
    first = pay(api, issued, amount="5000", external_reference="UTR1")
    assert first["reference"].startswith("PAY-") and first["status"] == "RECORDED"
    dup = pay(api, issued, 409, amount="10", external_reference="UTR1")
    assert first["reference"] in dup["detail"]
    shown = api.get(f"{ADMIN}/invoices/{invoice['id']}").json()
    assert (float(shown["paid"]), float(shown["awaiting_verification"])) == (0.0, 5000.0)
    assert shown["payment_state"] == "UNPAID"  # recorded is not verified (BR-019)
    assert (
        api.post(f"{ADMIN}/payments/{first['id']}/reject", json={"reason": ""}).status_code == 422
    )
    assert api.post(f"{ADMIN}/payments/{first['id']}/verify").json()["status"] == "VERIFIED"
    assert api.post(f"{ADMIN}/payments/{first['id']}/verify").status_code == 409
    shown = api.get(f"{ADMIN}/invoices/{invoice['id']}").json()
    assert (shown["payment_state"], float(shown["outstanding"])) == ("PARTLY_PAID", total - 5000)

    # A rejected payment frees its amount and its reference.
    wrong = pay(api, issued, amount="100", method="CHEQUE", external_reference="000123")
    rejected = api.post(
        f"{ADMIN}/payments/{wrong['id']}/reject", json={"reason": "Cheque bounced"}
    ).json()
    assert (rejected["status"], rejected["reject_reason"]) == ("REJECTED", "Cheque bounced")
    pay(api, issued, amount="100", method="CHEQUE", external_reference="000123")
    rest = pay(api, issued, amount=str(round(total - 5000 - 100, 2)), method="BANK_TRANSFER")
    for p in api.get(f"{ADMIN}/payments", params={"invoice_id": invoice["id"]}).json()["items"]:
        if p["status"] == "RECORDED":
            api.post(f"{ADMIN}/payments/{p['id']}/verify")
    final = api.get(f"{ADMIN}/invoices/{invoice['id']}").json()
    assert (final["payment_state"], float(final["outstanding"])) == ("PAID", 0.0)
    assert rest["status"] == "RECORDED"
    pay(api, issued, 409, amount="1")  # nothing left to pay
    # Verified payments keep the invoice.
    refused = api.post(f"{ADMIN}/invoices/{invoice['id']}/cancel", json={"reason": "x"})
    assert refused.status_code == 409

    # Every step is in the history.
    history = api.get(
        f"{ADMIN}/activity", params={"entity_type": "invoices", "entity_id": invoice["id"]}
    ).json()["items"]
    assert {"created", "lines_set", "issued"} <= {e["action"] for e in history}


def test_an_expense_is_invoiced_once_and_only_when_reviewed(
    api: TestClient, hub: dict, storage: object
) -> None:
    account, contract = account_with_contract(api, hub)
    expense = reviewed_expense(api, contract)
    line = {
        "category": "REIMBURSEMENT_FEE",
        "description": "Water",
        "amount": str(expense["total"]),
        "expense_id": expense["id"],
    }
    first = draft(api, account, contract_id=contract["id"])
    second = draft(api, account, contract_id=contract["id"])
    assert (
        api.put(f"{ADMIN}/invoices/{first['id']}/lines", json={"lines": [line]}).status_code == 200
    )
    taken = api.put(f"{ADMIN}/invoices/{second['id']}/lines", json={"lines": [line]})
    assert taken.status_code == 409
    # Not on another category, and only at its reviewed total.
    wrong = {**line, "category": "MAINTENANCE_FEE"}
    assert (
        api.put(f"{ADMIN}/invoices/{second['id']}/lines", json={"lines": [wrong]}).status_code
        == 400
    )
    off = {**line, "amount": "1"}
    assert (
        api.put(f"{ADMIN}/invoices/{first['id']}/lines", json={"lines": [off]}).status_code == 400
    )
    # The suggestion no longer offers it.
    assert all(
        s["expense_id"] != expense["id"]
        for s in api.get(f"{ADMIN}/invoices/{second['id']}/suggestions").json()
    )
    # Cancelling the first (no payments) releases it.
    cancelled = api.post(f"{ADMIN}/invoices/{first['id']}/cancel", json={"reason": "Wrong month"})
    assert cancelled.json()["status"] == "CANCELLED"
    assert (
        api.put(f"{ADMIN}/invoices/{second['id']}/lines", json={"lines": [line]}).status_code == 200
    )
    assert (
        api.post(f"{ADMIN}/invoices/{first['id']}/cancel", json={"reason": "again"}).status_code
        == 409
    )


def test_invoice_validation(api: TestClient, hub: dict, storage: object) -> None:
    account, contract = account_with_contract(api, hub)
    other_account, other_contract = account_with_contract(
        api, {**hub, "cabin": hub["seat1"]}, name="Beta LLP", email="b@beta.example"
    )
    draft(api, account, 400, contract_id=other_contract["id"])
    draft(
        api,
        account,
        422,
        due_date=(business_today() - timedelta(days=1)).isoformat(),
    )
    draft(api, "00000000-0000-0000-0000-000000000000", 404)
    fresh = draft(api, account)
    pay(api, fresh, 409, amount="10")  # not issued
    assert (
        api.post(f"{ADMIN}/invoices/{fresh['id']}/cancel", json={"reason": " "}).status_code == 422
    )
    listed = api.get(f"{ADMIN}/invoices", params={"account_id": account}).json()
    assert [i["id"] for i in listed["items"]] == [fresh["id"]]
    assert other_account != account


def test_balances_deposits_and_spaces(
    api: TestClient, hub: dict, storage: object, db_session: Session
) -> None:
    account, contract = account_with_contract(api, hub)
    api.patch(f"{ADMIN}/contracts/{contract['id']}", json={"security_deposit": "36000"})
    empty = api.get(f"{ADMIN}/billing/balances").json()
    assert [(b["account_id"], float(b["invoiced"]), float(b["deposits_held"])) for b in empty] == [
        (account, 0.0, 36000.0)
    ]
    invoice = draft(api, account, contract_id=contract["id"])
    api.put(
        f"{ADMIN}/invoices/{invoice['id']}/lines",
        json={"lines": [{"category": "SPACE_FEE", "description": "Rent", "amount": "12000"}]},
    )
    issued = api.post(f"{ADMIN}/invoices/{invoice['id']}/issue").json()
    p = pay(api, issued, amount="2000")
    api.post(f"{ADMIN}/payments/{p['id']}/verify")
    pay(api, issued, amount="1000")
    row = api.get(f"{ADMIN}/billing/balances", params={"account_id": account}).json()[0]
    assert (
        float(row["invoiced"]),
        float(row["paid"]),
        float(row["awaiting_verification"]),
        float(row["outstanding"]),
        row["open_invoices"],
    ) == (12000.0, 2000.0, 1000.0, 10000.0, 1)
    # The deposit is shown apart and never reduces what is owed (BR-030).
    assert float(row["deposits_held"]) == 36000.0

    deposits = api.get(f"{ADMIN}/billing/deposits").json()
    assert [(d["contract_reference"], float(d["amount"])) for d in deposits] == [
        (contract["reference"], 36000.0)
    ]
    spaces = api.get(f"{ADMIN}/billing/spaces-bookings").json()
    assert [(s["kind"], s["reference"], s["space_name"]) for s in spaces] == [
        ("CONTRACT", contract["reference"], "Cabin 1")
    ]
