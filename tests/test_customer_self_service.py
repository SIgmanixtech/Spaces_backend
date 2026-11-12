# ruff: noqa: F811  (pytest fixtures shared from other test modules)
"""Customers who registered themselves (12 Nov 2026): long-term enquiries without a
tenancy, sign-in without a tenancy, ownership, and linking when the property team later
invites a tenant with the same email."""

import uuid
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_token_verifier
from app.main import app
from app.models import Booking, Tenant
from app.models.crm import Lead
from app.models.enums import UserRole
from app.models.user import AppUser
from app.services.auth import SupabaseSession, get_auth_gateway, open_session
from app.services.occupancy import business_today
from tests.conftest import make_token
from tests.test_agreements import storage  # noqa: F401
from tests.test_client_access import agreement, contract_for
from tests.test_client_api import CUSTOMER, hub  # noqa: F401
from tests.test_resources import V1, post

ADMIN = f"{V1}/admin"


def customer(db_session: Session, signing_key, email: str) -> tuple[AppUser, TestClient]:
    user = AppUser(id=uuid.uuid4(), email=email, full_name="Self Registered", role="CUSTOMER")
    db_session.add(user)
    db_session.commit()
    token = make_token(signing_key, user.id)
    return user, TestClient(app, headers={"Authorization": f"Bearer {token}"})


def test_a_customer_without_a_tenancy_sends_a_long_term_enquiry(
    verifier: None, signing_key, hub: dict, db_session: Session, api: TestClient
) -> None:
    user, client = customer(db_session, signing_key, "walkin@example.com")
    start = business_today() + timedelta(days=20)
    made = client.post(
        f"{CUSTOMER}/rental-requests",
        json={"space_id": hub["cabin"]["id"], "duration": "YEARLY", "start_date": str(start)},
    )
    assert made.status_code == 201, made.text
    lead = db_session.get(Lead, made.json()["id"])
    assert (lead.source, lead.tenant_id, lead.created_by, lead.requested_term_months) == (
        "CLIENT_APP",
        None,  # no tenant is created or linked for a self-registered customer
        user.id,
        12,
    )
    assert (lead.contact_email, lead.contact_name) == ("walkin@example.com", "Self Registered")
    assert db_session.scalar(select(func.count()).select_from(Tenant)) == 0
    assert db_session.scalars(select(Booking)).all() == []
    assert [r["reference"] for r in client.get(f"{CUSTOMER}/rental-requests").json()] == [
        lead.reference
    ]
    _, other = customer(db_session, signing_key, "someone@example.com")
    assert other.get(f"{CUSTOMER}/rental-requests").json() == []
    # The property team sees it in CRM.
    assert api.get(f"{V1}/admin/crm/leads/{lead.id}").status_code == 200


def test_sign_in_needs_no_tenancy_but_admins_never_use_the_client_app(
    verifier: None, signing_key, db_session: Session, api: TestClient
) -> None:
    user, _ = customer(db_session, signing_key, "fresh@example.com")
    verifier_ = app.dependency_overrides.get(get_token_verifier, get_token_verifier)()
    read = open_session(
        db_session,
        verifier_,
        SupabaseSession(
            access_token=make_token(signing_key, user.id), expires_in=3600, refresh_token="r"
        ),
    )
    assert read.user.role == UserRole.CUSTOMER
    # Admin accounts are refused by every customer endpoint, tenancy or not.
    for path in ("me", "bookings", "rental-requests", "contracts", "invoices"):
        assert api.get(f"{CUSTOMER}/{path}").status_code == 403, path


class LinkGateway:
    def invite(self, email: str, full_name: str):  # noqa: ANN201
        raise AssertionError("no invitation is sent to an existing customer account")

    invitation_link = invite


def test_inviting_a_tenant_links_the_customer_who_already_registered(
    verifier: None, signing_key, hub: dict, db_session: Session, api: TestClient, storage: object
) -> None:
    user, client = customer(db_session, signing_key, "acme@example.com")
    booked = client.post(
        f"{CUSTOMER}/bookings",
        json={
            "space_id": hub["seat1"]["id"],
            "billing_period": "HOURLY",
            "start_at": "2030-01-07T09:00:00+05:30",
            "end_at": "2030-01-07T10:00:00+05:30",
        },
    )
    assert booked.status_code == 201, booked.text
    assert client.get(f"{CUSTOMER}/contracts").status_code == 403  # no tenancy yet

    tenant = post(api, "tenants", {"name": "Acme Pvt Ltd", "email": "acme@example.com"})
    agreement(api, contract_for(api, tenant, hub), up_to="done")  # completed contract
    app.dependency_overrides[get_auth_gateway] = lambda: LinkGateway()
    try:
        linked = api.post(
            f"{ADMIN}/tenants/{tenant['id']}/client-access", json={"send_email": False}
        )
    finally:
        app.dependency_overrides.pop(get_auth_gateway, None)
    assert linked.status_code == 200, linked.text
    assert linked.json()["status"] == "ACTIVE" and "linked" in linked.json()["message"]
    db_session.expire_all()
    assert db_session.get(Tenant, uuid.UUID(tenant["id"])).user_id == user.id
    # Now a tenant: contracts are visible, and the earlier booking is still theirs.
    assert client.get(f"{CUSTOMER}/contracts").status_code == 200
    assert client.get(f"{CUSTOMER}/me").json()["is_tenant"] is True
    mine = [b["id"] for b in client.get(f"{CUSTOMER}/bookings").json()["items"]]
    assert booked.json()["id"] in mine
