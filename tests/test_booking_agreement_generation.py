# ruff: noqa: F811  (pytest fixtures shared from other test modules)
"""A one-time booking's agreement generated in the app from an approved booking template
(owner request of 19 Nov 2026, migration 0027)."""

from __future__ import annotations

import io
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import docx
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.agreement import BookingDocument
from app.models.tenant import TenantOccupancy
from app.services.booking_terms import Fee, duration, fee_lines
from tests.test_agreements import make_docx, storage, upload  # noqa: F401
from tests.test_client_api import hub  # noqa: F401
from tests.test_contract_signing import profile_for
from tests.test_resources import V1, post

IST = ZoneInfo("Asia/Kolkata")
ADMIN = f"{V1}/admin"
BANK = {
    "bank_name": "Yes Bank",
    "bank_account_name": "SIGMANIX TEST PRIVATE LIMITED",
    "bank_account_number": "000111222333",
    "bank_ifsc": "YESB0001196",
    "bank_branch": "Gachibowli",
    "upi_id": "sigmanix@yesbank",
}
TEMPLATE = [
    "SPACE BOOKING AGREEMENT {{ agreement_number }}",
    "'You' {{ member_clause }}",
    "'Us' means {{ provider_company_name }}, {{ provider_address }}.",
    "The booking is for the period {{ booking_period }}.",
    "{{ fee_sentence }}",
    "Pay to {{ bank_account_name }}, account {{ bank_account_number }}, IFSC {{ bank_ifsc }}.",
    "Signed for {{ provider_company_name }} by {{ provider_signatory_name }}.",
]


def four_hour_booking(api: TestClient, hub: dict, status: str = "CONFIRMED") -> dict:
    tenant = post(
        api,
        "tenants",
        {
            "name": "Ravi Kumar",
            "company_name": "KT Demo Technologies Private Limited",
            "email": "ravi.booking@sigmanix-test.in",
            "registered_address": "Plot 1, Gachibowli, Hyderabad 500032",
            "pan": "AAKCK1234E",
            "personal_pan": "ABCPK1234D",
            "cin": "U62099TG2024PTC123456",
            "signatory_name": "Ravi Kumar",
            "signatory_designation": "Director",
        },
    )
    room = post(
        api,
        "spaces",
        {
            "property_id": hub["property"]["id"],
            "name": "Conference Room",
            "space_type": "CONFERENCE_ROOM",
            "parent_space_id": hub["5A"]["id"],
            "capacity": 8,
        },
    )
    start = (datetime.now(IST) + timedelta(days=4)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    return post(
        api,
        "bookings",
        {
            "property_id": hub["property"]["id"],
            "space_id": room["id"],
            "tenant_id": tenant["id"],
            "customer_name": "Ravi Kumar",
            "start_at": start.isoformat(),
            "end_at": (start + timedelta(hours=4)).isoformat(),
            "status": status,
        },
    )


def booking_template(api: TestClient, lines: list[str] = TEMPLATE) -> dict:
    made = upload(api, make_docx(lines), "BOOKING_AGREEMENT")
    approved = api.post(f"{V1}/contract-templates/{made['id']}/approve")
    assert approved.status_code == 200, approved.text
    return approved.json()


def plan(api: TestClient, booking_id: str) -> dict:
    response = api.get(f"{ADMIN}/bookings/{booking_id}/agreement/plan")
    assert response.status_code == 200, response.text
    return response.json()


def generate(api: TestClient, booking_id: str, expected: int = 201, **body: object) -> dict:
    payload = {"amount": "2000", "gst_treatment": "INCLUDED", "gst_rate": "18", **body}
    response = api.post(f"{ADMIN}/bookings/{booking_id}/agreement/generate", json=payload)
    assert response.status_code == expected, response.text
    return response.json()


def test_fee_lines_and_duration() -> None:
    included = fee_lines(Fee(Decimal("2000"), "INCLUDED", Decimal("18")))
    assert (str(included["nt"]), str(included["gst"]), str(included["invoice"])) == (
        "1694.92",
        "305.08",
        "2000.00",
    )
    added = fee_lines(Fee(Decimal("1000"), "ADDITIONAL", Decimal("18"), Decimal("10")))
    assert (added["gst"], added["invoice"], added["tds"], added["net"]) == (
        Decimal("180.00"),
        Decimal("1180.00"),
        Decimal("100.00"),
        Decimal("1080.00"),
    )
    exempt = fee_lines(Fee(Decimal("500"), "EXEMPT", Decimal("18")))
    assert exempt["gst"] == 0 and exempt["invoice"] == Decimal("500.00")
    start = datetime(2026, 11, 17, 23, 0, tzinfo=IST)
    assert duration(start, start + timedelta(hours=4)) == "4 hours"
    assert duration(start, start + timedelta(hours=3, minutes=30)) == "3 hours 30 minutes"


def test_generate_from_the_booking_template(
    api: TestClient,
    hub: dict,
    storage,  # noqa: ANN001
    db_session: Session,
) -> None:
    booking = four_hour_booking(api, hub)
    first = plan(api, booking["id"])
    assert "No approved booking agreement template" in first["problem"]

    template = booking_template(api)
    assert template["contract_type"] == "BOOKING_AGREEMENT"
    # Without a company profile the licensor's details are missing, by name.
    missing = plan(api, booking["id"])
    assert missing["problem"] is None
    assert "Licensor company name" in missing["missing"]
    assert "Bank account number" in missing["missing"]
    generate(api, booking["id"], expected=400)

    profile_for(api, hub["property"]["id"], **BANK)
    ready = plan(api, booking["id"])
    assert ready["problem"] is None and ready["missing"] == []
    assert ready["template"]["id"] == template["id"]

    contracts = db_session.scalar(select(func.count()).select_from(TenantOccupancy))
    doc = generate(api, booking["id"], aadhaar_last4="2639", residential_address="Proddatur")
    assert doc["status"] == "GENERATED"
    assert doc["reference"].startswith("CRA-")
    assert doc["template_id"] == template["id"] and doc["template_version"] == 1
    # A booking agreement never makes a contract.
    assert db_session.scalar(select(func.count()).select_from(TenantOccupancy)) == contracts

    row = db_session.get(BookingDocument, uuid.UUID(doc["id"]))
    stored = docx.Document(io.BytesIO(storage.get(row.object_path)))
    text = "\n".join(p.text for p in stored.paragraphs)
    assert "{{" not in text
    assert "'You' means 'Member' or 'KT Demo Technologies Private Limited' (CIN" in text
    assert "represented by Ravi Kumar (PAN ABCPK1234D, Aadhaar XXXX XXXX 2639)" in text
    assert "residing at Proddatur" in text
    assert "(4 hours), as one continuous booking" in text
    assert "Rs. 2,000.00 including GST (Rs. 1,694.92 plus GST 18% Rs. 305.08)" in text
    assert "000111222333" in text
    assert row.data["fee"]["gst_treatment"] == "INCLUDED"

    # Generating again supersedes the unsigned one and takes the next reference.
    again = generate(api, booking["id"], amount="2360", gst_treatment="ADDITIONAL")
    assert again["reference"] == f"{doc['reference']}-R2"
    history = api.get(f"{ADMIN}/bookings/{booking['id']}/agreement").json()["history"]
    assert [h["status"] for h in history] == ["GENERATED", "SUPERSEDED"]


def test_generation_refusals(
    api: TestClient,
    hub: dict,
    storage,  # noqa: ANN001
) -> None:
    booking_template(api)
    profile_for(api, hub["property"]["id"], **BANK)
    pending = four_hour_booking(api, hub, status="PENDING")
    assert "Confirm it first" in plan(api, pending["id"])["problem"]
    generate(api, pending["id"], expected=409)

    booking = four_hour_booking(api, hub)
    generate(api, booking["id"], expected=422, aadhaar_last4="12345")
    generate(api, booking["id"], expected=400, aadhaar_last4="12a4")
    generate(api, booking["id"], expected=400, gst_rate=None)
    generate(api, booking["id"], expected=422, gst_treatment="SOMETIMES")
    generate(api, booking["id"], expected=422, amount="0")
    exempt = generate(api, booking["id"], gst_treatment="EXEMPT", gst_rate=None)
    assert exempt["status"] == "GENERATED"

    # A booking template takes no category or customer fields.
    response = api.post(
        f"{V1}/contract-templates",
        data={"contract_type": "BOOKING_AGREEMENT", "name": "x", "category": "OFFICE"},
        files={"file": ("t.docx", make_docx(TEMPLATE), "application/octet-stream")},
    )
    assert response.status_code == 400, response.text
    # Contract-only names are not allowed in a booking template.
    lease_names = upload(api, make_docx(["{{ lease.rent }}"]), "BOOKING_AGREEMENT")
    refused = api.post(f"{V1}/contract-templates/{lease_names['id']}/approve")
    assert refused.status_code == 409 and "lease.rent" in refused.text
