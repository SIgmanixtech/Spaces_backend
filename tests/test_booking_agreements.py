# ruff: noqa: F811  (pytest fixtures shared from other test modules)
"""Booking agreements: the Conference Room Usage Agreement stored against a one-time booking
(17 Nov 2026, migration 0026)."""

from __future__ import annotations

import io
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import docx
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.tenant import Tenant, TenantOccupancy
from tests.test_agreements import SIGNED_PDF, storage  # noqa: F401
from tests.test_client_api import hub  # noqa: F401
from tests.test_resources import V1, post

IST = ZoneInfo("Asia/Kolkata")
SIGNERS = (
    '[{"party": "TENANT", "name": "Somisetty Venkata Teja Prakash"},'
    ' {"party": "COMPANY", "name": "Test Signatory", "designation": "Managing Director"}]'
)


def agreement_docx(text: str = "CONFERENCE ROOM USAGE AGREEMENT") -> bytes:
    d = docx.Document()
    d.add_heading(text)
    out = io.BytesIO()
    d.save(out)
    return out.getvalue()


def overnight_booking(api: TestClient, hub: dict, status: str = "CONFIRMED") -> dict:
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
    start = (datetime.now(IST) + timedelta(days=3)).replace(
        hour=23, minute=0, second=0, microsecond=0
    )
    return post(
        api,
        "bookings",
        {
            "property_id": hub["property"]["id"],
            "space_id": room["id"],
            "customer_name": "Somisetty Venkata Teja Prakash",
            "customer_email": "teju.booking@sigmanix-test.in",
            "start_at": start.isoformat(),
            "end_at": (start + timedelta(hours=3)).isoformat(),
            "status": status,
        },
    )


def store(api: TestClient, booking_id: str, data: bytes, expected: int = 201, **form: str) -> dict:
    response = api.post(
        f"{V1}/admin/bookings/{booking_id}/agreement",
        data=form,
        files={"file": ("CRA.docx", data, "application/octet-stream")},
    )
    assert response.status_code == expected, response.text
    return response.json()


def test_store_approve_sign_and_history(
    api: TestClient,
    hub: dict,
    storage,
    db_session: Session,  # noqa: ANN001
) -> None:
    booking = overnight_booking(api, hub)
    tenants = db_session.scalar(select(func.count()).select_from(Tenant))
    contracts = db_session.scalar(select(func.count()).select_from(TenantOccupancy))
    empty = api.get(f"{V1}/admin/bookings/{booking['id']}/agreement").json()
    assert empty["current"] is None and empty["history"] == []

    data = agreement_docx()
    doc = store(api, booking["id"], data, reference="CRA-BK-2026-000007", generated_on="2026-11-17")
    assert doc["reference"] == "CRA-BK-2026-000007"
    assert doc["status"] == "GENERATED" and doc["generated_on"] == "2026-11-17"

    # The same file again: the same document, no duplicate, reference unchanged.
    again = store(api, booking["id"], data, expected=200, reference="OTHER-REF")
    assert again["id"] == doc["id"] and again["reference"] == "CRA-BK-2026-000007"

    link = api.get(f"{V1}/admin/booking-documents/{doc['id']}/download")
    assert link.status_code == 200 and link.json()["url"]

    # Not signed until approved and a signed copy is uploaded.
    early = api.post(
        f"{V1}/admin/booking-documents/{doc['id']}/signed-copy",
        data={"signed_on": "2026-11-17", "signers": SIGNERS},
        files={"file": ("signed.pdf", SIGNED_PDF, "application/pdf")},
    )
    assert early.status_code == 409
    approved = api.post(f"{V1}/admin/booking-documents/{doc['id']}/approve").json()
    assert approved["status"] == "APPROVED"
    signed = api.post(
        f"{V1}/admin/booking-documents/{doc['id']}/signed-copy",
        data={"signed_on": "2026-11-17", "signers": SIGNERS},
        files={"file": ("signed.pdf", SIGNED_PDF, "application/pdf")},
    )
    assert signed.status_code == 201, signed.text
    assert signed.json()["status"] == "SIGNED"

    # A signed agreement is never replaced.
    store(api, booking["id"], agreement_docx("A different file"), expected=409)
    view = api.get(f"{V1}/admin/bookings/{booking['id']}/agreement").json()
    assert view["current"]["id"] == doc["id"] and len(view["history"]) == 1
    assert (
        api.get(f"{V1}/admin/booking-documents/{doc['id']}/signed-copy/download").status_code == 200
    )

    # The booking and customer records are untouched; no tenant or contract is made here.
    assert api.get(f"{V1}/admin/bookings/{booking['id']}").json()["status"] == "CONFIRMED"
    assert db_session.scalar(select(func.count()).select_from(Tenant)) == tenants
    assert db_session.scalar(select(func.count()).select_from(TenantOccupancy)) == contracts


def test_newer_unsigned_file_supersedes_and_keeps_history(
    api: TestClient,
    hub: dict,
    storage,  # noqa: ANN001
) -> None:
    booking = overnight_booking(api, hub)
    first = store(api, booking["id"], agreement_docx("v1"))
    assert first["reference"].startswith("CRA-BK-")
    second = store(api, booking["id"], agreement_docx("v2"))
    assert second["reference"] == f"{first['reference']}-R2"
    view = api.get(f"{V1}/admin/bookings/{booking['id']}/agreement").json()
    assert view["current"]["id"] == second["id"]
    assert [d["status"] for d in view["history"]] == ["GENERATED", "SUPERSEDED"]


def test_refusals(api: TestClient, client: TestClient, hub: dict, storage) -> None:  # noqa: ANN001
    pending = overnight_booking(api, hub, status="PENDING")
    store(api, pending["id"], agreement_docx(), expected=409)
    booking = api.post(f"{V1}/admin/bookings/{pending['id']}/confirm")
    assert booking.status_code == 200, booking.text
    store(api, pending["id"], b"%PDF-1.4 not a docx", expected=400)
    store(api, pending["id"], agreement_docx(), expected=400, reference="bad ref!")
    # Administrators only.
    assert client.get(f"{V1}/admin/bookings/{pending['id']}/agreement").status_code == 401
