# ruff: noqa: F811  (pytest fixtures shared from other test modules)
"""Client app access (BR-003, BR-005, BR-014, BR-025): only a tenant with a completed
contract (approved, agreement recorded as signed) and an email can be invited; inviting
again never makes a second account; the tenant becomes active by setting a password from
the link. Supabase is replaced by a stand-in; nothing goes over the network."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import ServiceUnavailableError, UnauthorizedError
from app.main import app
from app.models import AppUser, Tenant
from app.models.enums import UserRole
from app.services.auth import Invitation, SupabaseAuthGateway, get_auth_gateway
from tests.conftest import make_token
from tests.test_agreements import (  # noqa: F401
    make_docx,
    sign_with_copy,
    storage,
    upload,
)
from tests.test_client_api import CUSTOMER, MONDAY, hub  # noqa: F401
from tests.test_resources import V1, post

ADMIN = f"{V1}/admin"


class InviteAuth:
    """Stands in for Supabase's admin API: one sign-in per email, as Supabase keeps."""

    def __init__(self, key: ec.EllipticCurvePrivateKey, email_works: bool = True) -> None:
        self.key = key
        self.email_works = email_works
        self.users: dict[str, uuid.UUID] = {}
        self.emails: list[str] = []
        self.passwords: dict[uuid.UUID, str] = {}

    def _user(self, email: str) -> uuid.UUID:
        return self.users.setdefault(email, uuid.uuid4())

    def invite(self, email: str, full_name: str) -> Invitation:
        if not self.email_works:
            return self.invitation_link(email, full_name)
        self.emails.append(email)
        return Invitation(str(self._user(email)), email_requested=True, link=None)

    def invitation_link(self, email: str, full_name: str) -> Invitation:
        user_id = self._user(email)
        return Invitation(str(user_id), False, f"https://auth.test/verify?user={user_id}")

    def link_token(self, email: str) -> str:
        """What the link's redirect carries: a session for that user."""
        return make_token(self.key, self.users[email], email=email)

    def set_password(self, access_token: str, password: str) -> None:
        import jwt

        claims = jwt.decode(access_token, options={"verify_signature": False})
        self.passwords[uuid.UUID(claims["sub"])] = password


@pytest.fixture
def invites(verifier: None, signing_key: ec.EllipticCurvePrivateKey) -> Iterator[InviteAuth]:
    stand_in = InviteAuth(signing_key)
    app.dependency_overrides[get_auth_gateway] = lambda: stand_in
    yield stand_in
    app.dependency_overrides.pop(get_auth_gateway, None)


def new_tenant(api: TestClient, **extra: object) -> dict:
    return post(api, "tenants", {"name": "Acme Pvt Ltd", "email": "ops@acme.example", **extra})


def contract_for(api: TestClient, tenant: dict, hub: dict) -> dict:
    created = api.post(
        f"{ADMIN}/contracts",
        json={
            "tenant_id": tenant["id"],
            "property_id": hub["property"]["id"],
            "space_id": hub["cabin"]["id"],
            "start_date": str((MONDAY + timedelta(days=60)).date()),
            "monthly_rent": 12000,
            "activate": True,
        },
    )
    assert created.status_code == 201, created.text
    contract = created.json()
    api.patch(f"{ADMIN}/contracts/{contract['id']}", json={"contract_type": "SPACE_LEASE"})
    return contract


def agreement(api: TestClient, contract: dict, *, up_to: str) -> None:
    """Generate the agreement and move it forward up to the given step. "sign" uploads
    the signed copy (the plain sign step was retired in Phase 4B)."""
    template = upload(api, make_docx(["LEASE {{ contract.reference }}", "{{ lease.premises }}"]))
    api.post(f"{ADMIN}/contract-templates/{template['id']}/approve")
    made = api.post(f"{ADMIN}/contracts/{contract['id']}/agreement/documents")
    assert made.status_code == 201, made.text
    for step in ("approve", "send", "sign"):
        if step == up_to:
            break
        if step == "sign":
            sign_with_copy(api, made.json()["id"])
            continue
        moved = api.post(f"{ADMIN}/contract-documents/{made.json()['id']}/{step}")
        assert moved.status_code == 200, moved.text
    else:
        return


def completed_tenant(api: TestClient, hub: dict, **extra: object) -> dict:
    tenant = new_tenant(api, **extra)
    agreement(api, contract_for(api, tenant, hub), up_to="done")
    return tenant


def invite(api: TestClient, tenant: dict, send_email: bool = True, expected: int = 200) -> dict:
    response = api.post(
        f"{ADMIN}/tenants/{tenant['id']}/client-access", json={"send_email": send_email}
    )
    assert response.status_code == expected, response.text
    return response.json()


def test_only_a_completed_contract_with_an_email_can_be_invited(
    api: TestClient, invites: InviteAuth, hub: dict, storage: object
) -> None:
    tenant = new_tenant(api)
    assert api.get(f"{ADMIN}/tenants/{tenant['id']}").json()["client_access"] == "NOT_INVITED"
    no_contract = invite(api, tenant, expected=409)
    assert "no completed contract" in no_contract["detail"]

    contract = contract_for(api, tenant, hub)
    agreement(api, contract, up_to="sign")  # generated, approved, sent: not signed
    assert "no completed contract" in invite(api, tenant, expected=409)["detail"]
    shown = api.get(f"{ADMIN}/tenants/{tenant['id']}").json()
    assert "no completed contract" in shown["client_access_blocker"]

    phone_only = new_tenant(api, name="Ravi", email=None, phone="+919876543210")
    agreement(api, contract_for(api, phone_only, {**hub, "cabin": hub["seat1"]}), up_to="done")
    assert "email" in invite(api, phone_only, expected=409)["detail"]
    assert invites.users == {}  # nothing was created at Supabase


def test_inviting_again_never_makes_a_second_account(
    api: TestClient, invites: InviteAuth, hub: dict, storage: object, db_session: Session
) -> None:
    tenant = completed_tenant(api, hub)
    first = invite(api, tenant)
    assert first["status"] == "INVITED" and first["email_requested"] is True
    assert first["invitation_link"] is None
    assert "Delivery is not confirmed" in first["message"]
    again = invite(api, tenant)
    link = invite(api, tenant, send_email=False)
    assert link["email_requested"] is False and link["invitation_link"].startswith("https://")
    assert "No email was sent" in link["message"]
    assert invites.emails == ["ops@acme.example", "ops@acme.example"]
    assert again["invited_at"] >= first["invited_at"]

    customers = db_session.scalars(select(AppUser).where(AppUser.role == UserRole.CUSTOMER)).all()
    assert [u.email for u in customers] == ["ops@acme.example"]
    stored = db_session.scalar(select(Tenant).where(Tenant.id == uuid.UUID(tenant["id"])))
    assert stored.user_id == customers[0].id and stored.client_activated_at is None
    listed = api.get(f"{ADMIN}/tenants/{tenant['id']}").json()
    assert listed["client_access"] == "INVITED" and listed["client_access_blocker"] is None


def test_without_an_email_service_a_link_is_given_not_a_claim(
    api: TestClient, invites: InviteAuth, hub: dict, storage: object
) -> None:
    invites.email_works = False
    answer = invite(api, completed_tenant(api, hub))
    assert answer["email_requested"] is False and answer["invitation_link"]
    assert invites.emails == []


def test_setting_a_password_activates_and_only_then_the_client_app_opens(
    api: TestClient, invites: InviteAuth, hub: dict, storage: object, db_session: Session
) -> None:
    tenant = completed_tenant(api, hub)
    invite(api, tenant)
    token = invites.link_token("ops@acme.example")
    waiting = TestClient(app, headers={"Authorization": f"Bearer {token}"})
    assert waiting.get(f"{CUSTOMER}/me").status_code == 403  # invited, not active yet

    short = TestClient(app).post(
        f"{V1}/client/auth/set-password",
        json={"access_token": token, "refresh_token": "r", "password": "short"},
    )
    assert short.status_code == 422
    done = TestClient(app).post(
        f"{V1}/client/auth/set-password",
        json={"access_token": token, "refresh_token": "r", "password": "a-good-password"},
    )
    assert done.status_code == 200, done.text
    session = done.json()
    assert session["user"]["role"] == "CUSTOMER"
    assert invites.passwords[invites.users["ops@acme.example"]] == "a-good-password"
    signed_in = TestClient(app, headers={"Authorization": f"Bearer {session['access_token']}"})
    me = signed_in.get(f"{CUSTOMER}/me")
    assert me.status_code == 200 and me.json()["email"] == "ops@acme.example"
    assert api.get(f"{ADMIN}/tenants/{tenant['id']}").json()["client_access"] == "ACTIVE"
    # Active now: no more invitations; a reset link still lets them choose a new password.
    assert "already has client access" in invite(api, tenant, expected=409)["detail"]
    reset = TestClient(app).post(
        f"{V1}/client/auth/set-password",
        json={"access_token": token, "refresh_token": "r", "password": "another-password"},
    )
    assert reset.status_code == 200
    assert db_session.scalar(select(func.count()).select_from(AppUser)) == 2  # admin + tenant


def test_set_password_is_only_for_invited_tenants(
    api: TestClient,
    admin_user: AppUser,
    invites: InviteAuth,
    signing_key: ec.EllipticCurvePrivateKey,
    db_session: Session,
) -> None:
    def attempt(token: str) -> int:
        return (
            TestClient(app)
            .post(
                f"{V1}/client/auth/set-password",
                json={"access_token": token, "refresh_token": "r", "password": "a-good-password"},
            )
            .status_code
        )

    assert attempt(make_token(signing_key, uuid.uuid4())) == 403  # no such account here
    assert attempt(make_token(signing_key, admin_user.id)) == 403  # the administrator
    stranger = AppUser(id=uuid.uuid4(), email="s@example.com", role=UserRole.CUSTOMER)
    db_session.add(stranger)
    db_session.commit()
    assert attempt(make_token(signing_key, stranger.id)) == 403  # never invited
    forged = make_token(ec.generate_private_key(ec.SECP256R1()), stranger.id)
    assert attempt(forged) == 401
    expired = make_token(signing_key, stranger.id, exp=datetime.now(UTC) - timedelta(minutes=1))
    assert attempt(expired) == 401
    assert invites.passwords == {}


def test_an_email_used_by_another_account_cannot_be_invited(
    api: TestClient, admin_user: AppUser, invites: InviteAuth, hub: dict, storage: object
) -> None:
    tenant = completed_tenant(api, hub, email=admin_user.email)
    assert "Another account already uses this email" in invite(api, tenant, expected=409)["detail"]
    assert invites.users == {}


def test_the_supabase_admin_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, dict]] = []
    answers: dict[str, httpx.Response] = {}

    def request(method: str, url: str, **kwargs: object) -> httpx.Response:
        path = url.removeprefix("http://supabase.test/auth/v1")
        calls.append((method, path, kwargs))
        return answers.get(path, httpx.Response(500))

    monkeypatch.setattr(httpx, "request", request)
    gateway = SupabaseAuthGateway(
        "http://supabase.test/auth/v1",
        "publishable",
        "https://client.test/set-password",
        "sb_secret",
    )
    answers["/invite"] = httpx.Response(200, json={"id": str(uuid.uuid4())})
    sent = gateway.invite("a@example.com", "A")
    assert sent.email_requested and sent.link is None
    method, path, kwargs = calls[-1]
    assert (method, path) == ("POST", "/invite")
    assert kwargs["params"] == {"redirect_to": "https://client.test/set-password"}
    assert kwargs["headers"] == {"apikey": "sb_secret"}

    # Supabase cannot send (or the address already has a sign-in): a link, not a claim.
    user_id = str(uuid.uuid4())
    answers["/invite"] = httpx.Response(422, json={"msg": "already been registered"})
    answers["/admin/generate_link"] = httpx.Response(
        200, json={"id": user_id, "properties": {"action_link": "https://auth/verify?t=1"}}
    )
    fallback = gateway.invite("a@example.com", "A")
    assert (fallback.user_id, fallback.email_requested, fallback.link) == (
        user_id,
        False,
        "https://auth/verify?t=1",
    )
    assert calls[-1][2]["json"]["redirect_to"] == "https://client.test/set-password"

    without_secret = SupabaseAuthGateway("http://supabase.test/auth/v1", "publishable")
    with pytest.raises(ServiceUnavailableError, match="SUPABASE_SECRET_KEY"):
        without_secret.invite("a@example.com", "A")

    monkeypatch.setattr(httpx, "put", lambda *a, **k: httpx.Response(401))
    with pytest.raises(UnauthorizedError, match="expired"):
        gateway.set_password("token", "a-good-password")
    monkeypatch.setattr(httpx, "put", lambda *a, **k: httpx.Response(200, json={}))
    gateway.set_password("token", "a-good-password")
