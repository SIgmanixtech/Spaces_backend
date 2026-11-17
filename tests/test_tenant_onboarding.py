# ruff: noqa: F811  (pytest fixtures shared from other test modules)
"""Adding a tenant with a space, pricing, account, first invoice and signature image;
admin bookings without a tenant field (owner request and decisions of 14 Nov 2026)."""

from __future__ import annotations

import json
import struct
import uuid
import zlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.v1.endpoints.files import get_storage, get_storage_factory
from app.main import app
from app.models import Booking, Tenant
from app.models.account import Account
from app.models.invoice import Invoice, InvoiceLine
from app.models.tenant import TenantOccupancy
from app.storage import LocalStorage
from tests.test_client_api import hub  # noqa: F401
from tests.test_customer_self_service import customer
from tests.test_resources import V1, post

ADMIN = f"{V1}/admin"
TENANTS = f"{ADMIN}/tenants"


def png(width: int = 4, height: int = 2) -> bytes:
    """A small, complete PNG."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + b"\xff\xff\xff" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


@pytest.fixture
def files(tmp_path: Path) -> Iterator[LocalStorage]:
    local = LocalStorage(tmp_path, "http://testserver")
    app.dependency_overrides[get_storage] = lambda: local
    app.dependency_overrides[get_storage_factory] = lambda: lambda: local
    yield local
    app.dependency_overrides.pop(get_storage, None)
    app.dependency_overrides.pop(get_storage_factory, None)


@pytest.fixture
def priced(api: TestClient, hub: dict) -> dict:
    """Portion 5A (1,650 sq ft) gets its own monthly rule: ₹70/sq ft rent and ₹6/sq ft
    maintenance, 18% GST on top and two months' rent as deposit."""
    post(
        api,
        "pricing-rules",
        {
            "property_id": hub["property"]["id"],
            "space_id": hub["5A"]["id"],
            "rate_basis": "PER_SQFT",
            "billing_period": "MONTHLY",
            "amount": "70",
            "maintenance_amount": "6",
            "maintenance_basis": "PER_SQFT",
            "tax_treatment": "ADDITIONAL",
            "tax_rate": "18",
            "deposit_rule": "MONTHS_OF_RENT",
            "deposit_value": "2",
            "effective_from": "2026-01-01",
            "status": "ACTIVE",
        },
    )
    return hub


def assignment(hub: dict, space: str = "5A", start: str = "2030-03-15", **extra: object) -> dict:
    return {
        "property_id": hub["property"]["id"],
        "space_id": hub[space]["id"],
        "start_date": start,
        **extra,
    }


def onboard(
    api: TestClient,
    body: dict,
    expected: int = 201,
    signature: tuple[str, bytes, str] | None = None,
) -> dict:
    response = api.post(
        f"{TENANTS}/onboard",
        data={"data": json.dumps(body)},
        files={"signature": signature} if signature else None,
    )
    assert response.status_code == expected, response.text
    return response.json()


NEW = {"name": "Acme Pvt Ltd", "email": "ops@acme.example", "phone": "+91 98765 43210"}


def test_the_preview_takes_the_spaces_own_pricing_and_works_out_the_month(
    api: TestClient, priced: dict
) -> None:
    quote = api.post(f"{TENANTS}/assignment-quote", json=assignment(priced))
    assert quote.status_code == 200, quote.text
    q = quote.json()
    assert (q["property_name"], q["building_name"], q["floor_name"], q["space_name"]) == (
        "Sigma Hub",
        "Tower A",
        "Floor 5",
        "Portion 5A",
    )
    assert q["space_type"] == "PORTION" and float(q["area_sqft"]) == 1650 and q["available"]
    numbers = ("rate", "maintenance_rate", "rent", "maintenance", "tax", "recurring_total")
    p = {k: float(v) if k in (*numbers, "deposit") else v for k, v in q["pricing"].items()}
    assert p["source"] == "OWN" and p["rate_basis"] == "PER_SQFT"
    assert (p["rate"], p["maintenance_rate"], p["maintenance_basis"]) == (70, 6, "PER_SQFT")
    assert p["rent"] == 115_500  # 1,650 x 70
    assert p["maintenance"] == 9_900  # 1,650 x 6
    assert p["tax"] == 22_572  # 18% of 125,400
    assert p["recurring_total"] == 147_972
    assert p["deposit"] == 231_000  # two months' rent
    invoice = q["invoice"]
    assert (invoice["period_start"], invoice["period_end"]) == ("2030-03-15", "2030-04-14")
    assert [(line["category"], float(line["amount"])) for line in invoice["lines"]] == [
        ("SPACE_FEE", 115_500),
        ("MAINTENANCE_FEE", 9_900),
    ]
    assert float(invoice["total"]) == 125_400


def test_adding_a_tenant_with_a_space_makes_the_contract_account_and_a_draft_invoice(
    api: TestClient, priced: dict, files: LocalStorage, db_session: Session
) -> None:
    made = onboard(api, {"tenant": NEW, "assignment": assignment(priced)})
    assert made["created_tenant"] is True and made["invoice_skipped"] is None
    tenant, contract, invoice = made["tenant"], made["contract"], made["invoice"]
    assert tenant["name"] == "Acme Pvt Ltd" and tenant["has_signature"] is False
    assert contract["status"] == "ACTIVE" and contract["space_id"] == priced["5A"]["id"]
    assert float(contract["monthly_rent"]) == 115_500
    assert float(contract["security_deposit"]) == 231_000
    assert invoice["status"] == "DRAFT" and invoice["contract_id"] == contract["id"]
    assert (invoice["period_start"], invoice["period_end"]) == ("2030-03-15", "2030-04-14")
    lines = db_session.scalars(
        select(InvoiceLine).where(InvoiceLine.invoice_id == uuid.UUID(invoice["id"]))
    ).all()
    assert sorted((line.category, float(line.amount)) for line in lines) == [
        ("MAINTENANCE_FEE", 9_900.0),
        ("SPACE_FEE", 115_500.0),
    ]
    assert float(invoice["total"]) == 125_400
    account = db_session.scalar(select(Account).where(Account.tenant_id == uuid.UUID(tenant["id"])))
    assert account is not None and str(account.id) == invoice["account_id"]
    # The space is the tenant's from the start date (an upcoming occupancy).
    held = api.get(f"{TENANTS}/{tenant['id']}/occupancies").json()
    assert [(o["space_id"], o["start_date"]) for o in held] == [(priced["5A"]["id"], "2030-03-15")]


def test_retrying_returns_the_same_records_and_never_a_second_invoice(
    api: TestClient, priced: dict, files: LocalStorage, db_session: Session
) -> None:
    first = onboard(api, {"tenant": NEW, "assignment": assignment(priced)})
    again = onboard(
        api,
        {"existing_tenant_id": first["tenant"]["id"], "assignment": assignment(priced)},
    )
    assert again["contract"]["id"] == first["contract"]["id"]
    assert again["invoice"]["id"] == first["invoice"]["id"]
    assert db_session.scalar(select(func.count()).select_from(Invoice)) == 1
    assert db_session.scalar(select(func.count()).select_from(TenantOccupancy)) == 1
    assert db_session.scalar(select(func.count()).select_from(Tenant)) == 1


@pytest.mark.parametrize(
    "same",
    [
        {"email": "OPS@acme.example"},  # by email, whatever the case
        {"phone": "098765 43210"},  # by phone, however it is written
    ],
)
def test_a_new_tenant_matching_an_existing_one_needs_confirmation(
    api: TestClient, priced: dict, files: LocalStorage, db_session: Session, same: dict
) -> None:
    existing = post(api, "tenants", NEW)
    body = {"tenant": {"name": "Acme (again)", **same}, "assignment": assignment(priced)}
    refused = api.post(f"{TENANTS}/onboard", data={"data": json.dumps(body)})
    assert refused.status_code == 409, refused.text
    error = refused.json()
    assert error["code"] == "duplicate_tenant"
    assert [m["id"] for m in error["conflicts"]] == [existing["id"]]
    assert error["conflicts"][0]["email"] == "ops@acme.example"
    # Nothing was saved.
    assert db_session.scalar(select(func.count()).select_from(Tenant)) == 1
    assert db_session.scalar(select(func.count()).select_from(TenantOccupancy)) == 0
    # The admin confirms it is someone else: then it is created.
    made = onboard(api, {**body, "confirm_not_duplicate": True})
    assert made["created_tenant"] is True
    assert db_session.scalar(select(func.count()).select_from(Tenant)) == 2


def test_linking_an_existing_tenant_creates_no_tenant(
    api: TestClient, priced: dict, files: LocalStorage, db_session: Session
) -> None:
    existing = post(api, "tenants", NEW)
    made = onboard(api, {"existing_tenant_id": existing["id"], "assignment": assignment(priced)})
    assert made["created_tenant"] is False and made["tenant"]["id"] == existing["id"]
    assert db_session.scalar(select(func.count()).select_from(Tenant)) == 1
    both = {"tenant": NEW, "existing_tenant_id": existing["id"], "assignment": assignment(priced)}
    assert api.post(f"{TENANTS}/onboard", data={"data": json.dumps(both)}).status_code == 422


def test_a_taken_space_is_refused_and_nothing_is_saved(
    api: TestClient, priced: dict, files: LocalStorage, db_session: Session
) -> None:
    onboard(api, {"tenant": NEW, "assignment": assignment(priced, end_date="2030-12-31")})
    other = {"name": "Beta LLP", "email": "beta@example.com"}
    clash = api.post(
        f"{TENANTS}/onboard",
        data={
            "data": json.dumps(
                {"tenant": other, "assignment": assignment(priced, start="2030-06-01")}
            )
        },
    )
    assert clash.status_code == 409, clash.text
    assert "occupied" in clash.json()["detail"] or "contract" in clash.json()["detail"]
    assert db_session.scalar(select(func.count()).select_from(Tenant)) == 1  # Beta not created
    # A space inside the held portion is taken too (the allocation engine's hierarchy).
    inside = api.post(
        f"{TENANTS}/assignment-quote", json=assignment(priced, space="cabin", start="2030-06-01")
    ).json()
    assert inside["available"] is False and inside["conflicts"]


def test_without_a_monthly_price_the_tenant_is_added_but_no_invoice_is_made(
    api: TestClient, hub: dict, files: LocalStorage, db_session: Session
) -> None:
    # Seats have hourly and daily prices only; a per-sq-ft default needs an area.
    quote = api.post(f"{TENANTS}/assignment-quote", json=assignment(hub, space="seat1")).json()
    assert quote["pricing"] is None and quote["invoice"] is None and quote["pricing_missing"]
    made = onboard(api, {"tenant": NEW, "assignment": assignment(hub, space="seat1")})
    assert made["invoice"] is None and "No invoice was made" in made["invoice_skipped"]
    assert db_session.scalar(select(func.count()).select_from(Invoice)) == 0
    assert made["contract"]["status"] == "ACTIVE"


def test_unrentable_spaces_cannot_be_assigned(api: TestClient, hub: dict) -> None:
    corridor = post(
        api,
        "spaces",
        {
            "property_id": hub["property"]["id"],
            "name": "Corridor",
            "space_type": "NON_RENTABLE",
            "parent_space_id": hub["5A"]["id"],
            "is_rentable": False,
        },
    )
    hub = {**hub, "corridor": corridor}
    answer = api.post(f"{TENANTS}/assignment-quote", json=assignment(hub, space="corridor"))
    assert answer.status_code == 400 and "not rentable" in answer.json()["detail"]


def test_a_signature_image_is_checked_stored_privately_and_marks_nothing_signed(
    api: TestClient, priced: dict, files: LocalStorage, db_session: Session, signing_key, verifier
) -> None:
    body = {"tenant": NEW, "assignment": assignment(priced)}
    # Not an image, a fake PNG, or too big: refused, and nothing is created.
    for bad in (
        ("sig.png", b"hello", "image/png"),
        ("sig.png", b"\x89PNG\r\n\x1a\n" + b"0" * 40, "image/png"),
        ("sig.png", png()[:-12] + b"0" * (2 * 1024 * 1024), "image/png"),
        ("sig.gif", b"GIF89a" + b"0" * 40, "image/gif"),
    ):
        refused = api.post(
            f"{TENANTS}/onboard", data={"data": json.dumps(body)}, files={"signature": bad}
        )
        assert refused.status_code == 400, refused.text
    assert db_session.scalar(select(func.count()).select_from(Tenant)) == 0
    assert list(files.root.rglob("*.*")) == []

    made = onboard(api, body, signature=("signature.png", png(), "image/png"))
    assert made["signature_saved"] is True and made["tenant"]["has_signature"] is True
    tenant = db_session.get(Tenant, uuid.UUID(made["tenant"]["id"]))
    assert tenant.signature_content_type == "image/png"
    assert tenant.signature_path.startswith(f"tenants/{tenant.id}/")
    assert "signature_path" not in made["tenant"]  # never in responses
    link = api.get(f"{TENANTS}/{tenant.id}/signature")
    assert link.status_code == 200 and link.json()["expires_in"] == 300
    # Only an asset: the contract has no signed document.
    docs = api.get(f"{ADMIN}/contracts/{made['contract']['id']}").json()
    assert docs["status"] == "ACTIVE"
    assert api.get(f"{TENANTS}/{tenant.id}").json()["client_access"] != "ACTIVE"

    # Replace, then remove.
    old = tenant.signature_path
    replaced = api.put(
        f"{TENANTS}/{tenant.id}/signature",
        files={"signature": ("s.png", png(8, 3), "image/png")},
    )
    assert replaced.status_code == 200, replaced.text
    db_session.expire_all()
    assert db_session.get(Tenant, tenant.id).signature_path != old
    assert not (files.root / old).exists()
    assert api.delete(f"{TENANTS}/{tenant.id}/signature").status_code == 204
    db_session.expire_all()
    assert db_session.get(Tenant, tenant.id).signature_path is None
    assert api.get(f"{TENANTS}/{tenant.id}/signature").status_code == 404

    # Customers never reach these endpoints.
    _, someone = customer(db_session, signing_key, "someone@example.com")
    for method in ("get", "delete"):
        assert getattr(someone, method)(f"{TENANTS}/{tenant.id}/signature").status_code == 403
    assert (
        someone.put(
            f"{TENANTS}/{tenant.id}/signature",
            files={"signature": ("s.png", png(), "image/png")},
        ).status_code
        == 403
    )
    assert TestClient(app).get(f"{TENANTS}/{tenant.id}/signature").status_code == 401


def test_a_failed_save_removes_the_uploaded_signature(
    api: TestClient, priced: dict, files: LocalStorage, db_session: Session
) -> None:
    onboard(api, {"tenant": NEW, "assignment": assignment(priced, end_date="2030-12-31")})
    clash = {
        "tenant": {"name": "Beta LLP", "email": "beta@example.com"},
        "assignment": assignment(priced, start="2030-06-01"),
    }
    refused = api.post(
        f"{TENANTS}/onboard",
        data={"data": json.dumps(clash)},
        files={"signature": ("s.png", png(), "image/png")},
    )
    assert refused.status_code == 409
    assert list(files.root.rglob("*.png")) == []


# --- admin bookings without a tenant field ---------------------------------------------


def admin_booking(api: TestClient, hub: dict, hour: int, **customer_fields: object) -> dict:
    return post(
        api,
        "bookings",
        {
            "property_id": hub["property"]["id"],
            "space_id": hub["seat1"]["id"],
            "start_at": f"2030-01-07T{hour:02d}:00:00+05:30",
            "end_at": f"2030-01-07T{hour + 1:02d}:00:00+05:30",
            **customer_fields,
        },
    )


def test_an_admin_booking_needs_no_tenant_and_confirming_it_makes_one(
    api: TestClient, hub: dict, db_session: Session
) -> None:
    walk_in = {"customer_name": "Ravi Kumar", "customer_email": "ravi@example.com"}
    first = admin_booking(api, hub, 9, **walk_in)
    assert first["tenant_id"] is None
    assert db_session.scalar(select(func.count()).select_from(Tenant)) == 0
    assert api.post(f"{V1}/bookings/{first['id']}/confirm").status_code == 200
    tenant = db_session.scalar(select(Tenant).where(Tenant.email == "ravi@example.com"))
    assert tenant is not None and tenant.name == "Ravi Kumar"
    db_session.expire_all()
    assert db_session.get(Booking, uuid.UUID(first["id"])).tenant_id == tenant.id
    # The same customer books again: linked at once, confirmed, still one tenant.
    second = admin_booking(
        api,
        hub,
        11,
        customer_name="Ravi",
        customer_phone="99999 00000",
        customer_email="RAVI@example.com",
    )
    assert second["tenant_id"] == str(tenant.id)
    assert api.post(f"{V1}/bookings/{second['id']}/confirm").status_code == 200
    third = admin_booking(api, hub, 13, customer_name="Ravi K", customer_email="ravi@example.com")
    assert api.post(f"{V1}/bookings/{third['id']}/confirm").status_code == 200
    assert db_session.scalar(select(func.count()).select_from(Tenant)) == 1


def test_an_admin_booking_for_an_existing_tenant_links_by_phone(
    api: TestClient, hub: dict, db_session: Session
) -> None:
    existing = post(api, "tenants", NEW)
    made = admin_booking(api, hub, 9, customer_name="Acme front desk", customer_phone="09876543210")
    assert made["tenant_id"] == existing["id"]
    assert api.post(f"{V1}/bookings/{made['id']}/confirm").status_code == 200
    assert db_session.scalar(select(func.count()).select_from(Tenant)) == 1


def test_confirming_an_admin_booking_for_a_client_app_customer_uses_their_account(
    api: TestClient, hub: dict, db_session: Session, signing_key, verifier
) -> None:
    user, client = customer(db_session, signing_key, "app@example.com")
    made = admin_booking(
        api, hub, 9, customer_name="By phone call", customer_email="app@example.com"
    )
    assert api.post(f"{V1}/bookings/{made['id']}/confirm").status_code == 200
    tenant = db_session.scalar(select(Tenant).where(Tenant.user_id == user.id))
    assert tenant is not None and tenant.client_activated_at is not None
    assert client.get(f"{V1}/client/me").json()["is_tenant"] is True


def test_an_admin_booking_conflict_is_still_refused(api: TestClient, hub: dict) -> None:
    first = admin_booking(api, hub, 9, customer_name="One")
    assert api.post(f"{V1}/bookings/{first['id']}/confirm").status_code == 200
    clash = api.post(
        f"{V1}/bookings",
        json={
            "property_id": hub["property"]["id"],
            "space_id": hub["seat1"]["id"],
            "start_at": "2030-01-07T09:30:00+05:30",
            "end_at": "2030-01-07T10:30:00+05:30",
            "customer_name": "Two",
        },
    )
    assert clash.status_code == 409 and "already booked" in clash.json()["detail"]
