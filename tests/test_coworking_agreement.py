# ruff: noqa: F811  (pytest fixtures shared from other test modules)
"""The Coworking License Agreement from CRM tenant details (owner decisions, 17 Nov 2026).

Teju's agreement with test data: the member and company details the manager provided, two
dedicated workstations priced by a TEST pricing rule (the real fee is the property team's
to set), and the licensor's bank details kept in the company profile.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import docx
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.invoice import Invoice, InvoiceLine
from app.models.tenant import TenantOccupancy
from app.storage import LocalStorage
from tests.test_agreements import SIGNED_PDF, storage  # noqa: F401
from tests.test_client_api import hub  # noqa: F401
from tests.test_customer_self_service import customer
from tests.test_resources import V1, post

TEMPLATE = Path(__file__).parent / "fixtures" / "coworking_licence_template_v1.docx"

TEJU = {
    "name": "Somisetty Venkata Teja Prakash",
    "company_name": "PEGORION SOFTWARE SOLUTIONS PRIVATE LIMITED",
    "email": "tejaprakash@pegorion.com",
    "phone": "+91 90637 63783",
    "personal_pan": "IDAPP7511Q",
    "pan": "AAOCP7964R",
    "cin": "U62099AP2024PTC114980",
    "registered_address": "C/O G. Venkata Krishnaiah, 12/653, Kota Street, Proddatur, "
    "Cuddapah - 516360, Andhra Pradesh",
}
SIGMANIX = {
    "legal_name": "Sigmanix Ventures Private Limited",
    "registered_address": "5B - Sigma Hub Building, Plot No.8A, Survey No.184P, Sai Prithvi "
    "Enclave, Masidbanda, Kondapur, Hyderabad 500084",
    "signatory_name": "Test Signatory",
    "signatory_designation": "Managing Director",
    "bank_name": "YES BANK",
    "bank_account_name": "SIGMANIX VENTURES PRIVATE LIMITED",
    "bank_account_number": "119626900000051",
    "bank_ifsc": "yesb0001196",  # stored upper-case
    "bank_branch": "GACHIBOWLI",
    "upi_id": "yespay.mabs1196269akit0051@yesbankltd",
}


def text_of(data: bytes) -> str:
    d = docx.Document(io.BytesIO(data))
    cells = [c.text for t in d.tables for r in t.rows for c in r.cells]
    return "\n".join([p.text for p in d.paragraphs] + cells)


def table_rows(data: bytes) -> list[list[str]]:
    d = docx.Document(io.BytesIO(data))
    return [[c.text for c in r.cells] for r in d.tables[0].rows]


def setup_coworking(api: TestClient, hub: dict, *, tax: bool = True) -> list[dict]:
    """A licensor profile, two dedicated workstations and a TEST monthly price for them."""
    profile = post(api, "company-profiles", SIGMANIX)
    assert profile["bank_ifsc"] == "YESB0001196"
    linked = api.put(
        f"{V1}/company-profiles/{profile['id']}/properties",
        json={"property_ids": [hub["property"]["id"]]},
    )
    assert linked.status_code == 200, linked.text
    desks = [
        post(
            api,
            "spaces",
            {
                "property_id": hub["property"]["id"],
                "name": f"WS-{n}",
                "space_type": "DEDICATED_WORKSTATION",
                "parent_space_id": hub["bay"]["id"],
                "capacity": 1,
            },
        )
        for n in (1, 2)
    ]
    post(
        api,
        "pricing-rules",
        {
            "property_id": hub["property"]["id"],
            "rate_basis": "PER_WORKSTATION",
            "billing_period": "MONTHLY",
            "amount": "5000",  # TEST price
            "applies_to": "DEDICATED_WORKSTATION",
            "tax_treatment": "ADDITIONAL" if tax else "EXEMPT",
            **({"tax_rate": "18"} if tax else {}),
            "deposit_rule": "MONTHS_OF_RENT",
            "deposit_value": "2",
            "effective_from": "2026-01-01",
            "status": "ACTIVE",
            "notes": "TEST price",
        },
    )
    return desks


def onboard(api: TestClient, hub: dict, desks: list[dict], **terms: object) -> dict:
    body = {
        "tenant": TEJU,
        "assignment": {
            "property_id": hub["property"]["id"],
            "space_ids": [d["id"] for d in desks],
            "start_date": "2030-01-01",
            "contract_type": "COWORKING_LICENCE",
            **terms,
        },
    }
    made = api.post(f"{V1}/admin/tenants/onboard", data={"data": json.dumps(body)})
    assert made.status_code == 201, made.text
    return made.json()


def approved_template(api: TestClient) -> dict:
    form = {"contract_type": "COWORKING_LICENCE", "name": "Coworking License Agreement"}
    made = api.post(
        f"{V1}/contract-templates",
        data=form,
        files={"file": ("template.docx", TEMPLATE.read_bytes(), "application/octet-stream")},
    )
    assert made.status_code == 201, made.text
    approved = api.post(f"{V1}/contract-templates/{made.json()['id']}/approve")
    assert approved.status_code == 200, approved.text
    return approved.json()


def document_bytes(api: TestClient, files: LocalStorage, document_id: str) -> bytes:
    link = api.get(f"{V1}/contract-documents/{document_id}/download").json()["url"]
    path = link.split("/files/local/")[1].split("?")[0]
    return files.get(path.replace("%2F", "/"))


TERMS = {
    "lock_in_period": "2 (Two) months",
    "notice_period": "2 (Two) months",
    "payment_due_day": 5,
    "tds_percent": "10",
}


def test_tejus_agreement_is_filled_from_records(
    api: TestClient, hub: dict, storage: LocalStorage, db_session: Session
) -> None:
    desks = setup_coworking(api, hub)
    made = onboard(api, hub, desks, **TERMS)
    tenant, contract = made["tenant"], made["contract"]
    # Personal and company PAN are kept apart.
    assert (tenant["personal_pan"], tenant["pan"]) == ("IDAPP7511Q", "AAOCP7964R")
    # Two workstations: two ACTIVE contracts in one group, terms on both.
    seats = db_session.scalars(
        select(TenantOccupancy).where(TenantOccupancy.tenant_id == tenant["id"])
    ).all()
    assert len(seats) == 2 and len({s.group_id for s in seats}) == 1
    assert {(s.payment_due_day, str(s.tds_percent), s.contract_type) for s in seats} == {
        (5, "10.00", "COWORKING_LICENCE")
    }
    # Draft invoice: rent of both seats, linked to the first contract; no duplicate.
    invoice = made["invoice"]
    assert invoice["status"] == "DRAFT" and invoice["contract_id"] == contract["id"]
    lines = db_session.scalars(
        select(InvoiceLine).where(InvoiceLine.invoice_id == invoice["id"])
    ).all()
    assert [(line.category, float(line.amount)) for line in lines] == [("SPACE_FEE", 10000.0)]

    template = approved_template(api)
    assert template["status"] == "ACTIVE"
    url = f"{V1}/contracts/{contract['id']}/agreement/documents"
    generated = api.post(url)
    assert generated.status_code == 201, generated.text
    document = generated.json()
    data = document_bytes(api, storage, document["id"])
    text = text_of(data)
    if sample := os.environ.get("SIGMANIX_SAMPLE_DIR"):  # a copy to review in Word
        Path(sample, "Teju - Coworking License Agreement (TEST DATA).docx").write_bytes(data)

    # Member and company, PANs separate, no S/O, no old member.
    assert "'Somisetty Venkata Teja Prakash' (PAN IDAPP7511Q)" in text
    assert (
        "representing 'PEGORION SOFTWARE SOLUTIONS PRIVATE LIMITED' "
        "(CIN U62099AP2024PTC114980, PAN AAOCP7964R)"
    ) in text
    assert "Kota Street, Proddatur" in text
    for old in ("Nanda", "Ramulu", "S/O", "40,000", "30,000.00", "XX0051", "29th May"):
        assert old not in text, old
    assert "{{" not in text and "{%" not in text
    # Dates, due day, deposit (two months of the TEST fee per seat), lock-in and notice.
    assert "commencing from 1st January 2030" in text
    assert "prepaid on 5th day of the month" in text
    assert (
        "security deposit amounting to Rs. 20,000.00 (Rupees Twenty Thousand Only) for "
        "2 (Two) months lock-in period, member can discontinue by providing a 2 (Two) months "
        "notice."
    ) in text
    # The licensor's bank details, from the company profile.
    assert "SIGMANIX VENTURES PRIVATE LIMITED, YES BANK, Account No. 119626900000051" in text
    assert "IFSC YESB0001196, GACHIBOWLI branch" in text
    assert "UPI ID: yespay.mabs1196269akit0051@yesbankltd" in text
    assert "additional 18% above the monthly price" in text
    # The membership table: one line, GST 18% and TDS 10% from the records.
    rows = table_rows(data)
    assert rows[0][4] == "GST 18%" and rows[0][6] == "TDS Hold 10%"
    assert rows[1] == [
        "1",
        "Dedicated Workstations",
        "2",
        "₹ 10,000.00",
        "₹ 1,800.00",
        "₹ 11,800.00",
        "₹ 1,000.00",
        "₹ 10,800.00",
    ]
    assert rows[2][1:] == [
        "Total",
        "",
        "₹ 10,000.00",
        "₹ 1,800.00",
        "₹ 11,800.00",
        "₹ 1,000.00",
        "₹ 10,800.00",
    ]
    assert len(rows) == 3  # the loop markers are gone, no empty line
    # Signatures.
    assert "Name of the Sigmanix Spaces Representative: Test Signatory" in text
    assert "Name of the Member Representative: Somisetty Venkata Teja Prakash" in text
    assert "Member Company/Individual Name: PEGORION SOFTWARE SOLUTIONS PRIVATE LIMITED" in text

    # Retrying returns the same document; nothing new is made.
    assert api.post(url).json()["id"] == document["id"]
    # Approve, send, record the signed copy: then it is never regenerated or replaced.
    for step in ("approve", "send"):
        moved = api.post(f"{V1}/contract-documents/{document['id']}/{step}")
        assert moved.status_code == 200, moved.text
    signed = api.post(
        f"{V1}/admin/contract-documents/{document['id']}/signed-copy",
        data={
            "signed_on": "2026-11-01",
            "signers": json.dumps(
                [
                    {"party": "TENANT", "name": TEJU["name"], "designation": "Director"},
                    {"party": "COMPANY", "name": "Test Signatory"},
                ]
            ),
        },
        files={"file": ("signed.pdf", SIGNED_PDF, "application/pdf")},
    )
    assert signed.status_code == 201, signed.text
    again = api.post(url)
    assert again.status_code == 409 and "signed" in again.json()["detail"]
    # Signed terms change only by addendum.
    changed = api.patch(f"{V1}/contracts/{contract['id']}", json={"tds_percent": "2"})
    assert changed.status_code == 409


def test_missing_details_are_listed_not_guessed(
    api: TestClient, hub: dict, storage: LocalStorage
) -> None:
    desks = setup_coworking(api, hub)
    made = onboard(api, hub, desks)  # no lock-in, notice or due day entered
    approved_template(api)
    url = f"{V1}/contracts/{made['contract']['id']}/agreement"
    plan = api.get(url).json()
    assert plan["can_generate"] is False
    for label in (
        "Fee due day of the month (entered on the contract)",
        "Lock-in period (entered on the contract)",
        "Notice period (entered on the contract)",
    ):
        assert label in plan["missing"], plan["missing"]
    refused = api.post(f"{url}/documents")
    assert refused.status_code == 400 and "Fee due day" in refused.json()["detail"]
    # Entered on the contract: then it generates.
    for seat in (made["contract"]["id"],):
        assert (
            api.patch(
                f"{V1}/contracts/{seat}",
                json={
                    "lock_in_period": "1 month",
                    "notice_period": "1 month",
                    "payment_due_day": 7,
                },
            ).status_code
            == 200
        )
    assert api.post(f"{url}/documents").status_code == 201


def test_without_tds_or_gst_the_table_says_so(
    api: TestClient, hub: dict, storage: LocalStorage
) -> None:
    desks = setup_coworking(api, hub, tax=False)
    made = onboard(api, hub, desks[:1], **{**TERMS, "tds_percent": None})
    approved_template(api)
    document = api.post(f"{V1}/contracts/{made['contract']['id']}/agreement/documents").json()
    rows = table_rows(document_bytes(api, storage, document["id"]))
    assert rows[0][4] == "GST 0%" and rows[0][6] == "TDS Hold 0%"
    assert rows[1][1:] == [
        "Dedicated Workstation",
        "1",
        "₹ 5,000.00",
        "₹ 0.00",
        "₹ 5,000.00",
        "₹ 0.00",
        "₹ 5,000.00",
    ]


def test_the_preview_shows_tds_and_net_payable(api: TestClient, hub: dict) -> None:
    desks = setup_coworking(api, hub)
    quote = api.post(
        f"{V1}/admin/tenants/assignment-quote",
        json={
            "property_id": hub["property"]["id"],
            "space_ids": [d["id"] for d in desks],
            "start_date": "2030-01-01",
            "tds_percent": "10",
        },
    ).json()
    p = quote["pricing"]
    assert quote["quantity"] == 2 and [s["space_name"] for s in quote["spaces"]] == ["WS-1", "WS-2"]
    assert [
        float(p[k]) for k in ("rent", "tax", "recurring_total", "tds_amount", "net_payable")
    ] == [
        10000,
        1800,
        11800,
        1000,
        10800,
    ]
    assert float(p["deposit"]) == 20000


def test_a_taken_workstation_stops_the_whole_assignment(
    api: TestClient, hub: dict, db_session: Session
) -> None:
    desks = setup_coworking(api, hub)
    onboard(api, hub, desks[1:], **TERMS)
    other = {
        **TEJU,
        "name": "Someone Else",
        "email": "else@example.com",
        "phone": "+91 99999 11111",
    }
    body = {
        "tenant": other,
        "assignment": {
            "property_id": hub["property"]["id"],
            "space_ids": [d["id"] for d in desks],
            "start_date": "2030-02-01",
        },
    }
    refused = api.post(f"{V1}/admin/tenants/onboard", data={"data": json.dumps(body)})
    assert refused.status_code == 409 and "WS-2" in refused.json()["detail"]
    assert db_session.scalar(select(func.count()).select_from(TenantOccupancy)) == 1
    assert db_session.scalar(select(func.count()).select_from(Invoice)) == 1


def test_bank_details_are_validated_and_admin_only(
    api: TestClient, db_session: Session, signing_key, verifier
) -> None:
    bad = api.post(
        f"{V1}/company-profiles",
        json={**SIGMANIX, "bank_ifsc": "YESB1196", "bank_account_number": "12AB"},
    )
    assert bad.status_code == 422
    _, someone = customer(db_session, signing_key, "member@example.com")
    assert someone.get(f"{V1}/company-profiles").status_code == 403
    assert someone.post(f"{V1}/contract-templates").status_code == 403
    personal = api.post(f"{V1}/tenants", json={"name": "X", "personal_pan": "not-a-pan"})
    assert personal.status_code == 422
