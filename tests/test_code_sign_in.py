"""Sign-in with a one-time code by SMS (mobile number) or email.

Supabase Auth is replaced by a stand-in that issues genuinely signed tokens and accepts one
known code. Production verification is done by Supabase; nothing here accepts an arbitrary
code. A code never creates an account: only tenants the property team invited and who
have set a password (activated) can sign in."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import ServiceUnavailableError, UnauthorizedError
from app.main import app
from app.models import AppUser, Tenant
from app.models.enums import UserRole
from app.services.auth import SupabaseAuthGateway, SupabaseSession, get_auth_gateway
from app.services.customers import normalize_phone
from tests.conftest import make_token

CUSTOMER = "/api/v1/customer"
GOOD = "123456"


class CodeAuth:
    """Knows which contacts were sent a code; accepts only GOOD, and only once."""

    def __init__(self, key: ec.EllipticCurvePrivateKey, sms_enabled: bool = True) -> None:
        self.key, self.sms_enabled = key, sms_enabled
        self.users: dict[str, uuid.UUID] = {}
        self.sent: list[str] = []
        self.pending: set[str] = set()

    def _issue(self, contact: str) -> SupabaseSession:
        user_id = self.users.setdefault(contact, uuid.uuid4())
        # make_token adds a default email; a phone sign-in carries none.
        claims: dict[str, object] = (
            {"phone": contact.lstrip("+"), "email": None}
            if contact.startswith("+")
            else {"email": contact}
        )
        return SupabaseSession(make_token(self.key, user_id, **claims), f"refresh-{user_id}", 3600)

    def send_phone_otp(self, phone: str) -> None:
        if not self.sms_enabled:
            raise ServiceUnavailableError(
                "Sign-in by mobile number is not available yet. Use your email instead."
            )
        self.sent.append(phone)
        self.pending.add(phone)

    def verify_phone_otp(self, phone: str, code: str) -> SupabaseSession:
        return self._verify(phone, code)

    def send_otp(self, email: str) -> None:
        self.sent.append(email)
        self.pending.add(email)

    def verify_otp(self, email: str, code: str) -> SupabaseSession:
        return self._verify(email, code)

    def _verify(self, contact: str, code: str) -> SupabaseSession:
        if code != GOOD or contact not in self.pending:
            raise UnauthorizedError("That code is wrong or has expired. Ask for a new one.")
        self.pending.discard(contact)
        return self._issue(contact)


@pytest.fixture
def auth(verifier: None, signing_key: ec.EllipticCurvePrivateKey) -> Iterator[CodeAuth]:
    stand_in = CodeAuth(signing_key)
    app.dependency_overrides[get_auth_gateway] = lambda: stand_in
    yield stand_in
    app.dependency_overrides.pop(get_auth_gateway, None)


@pytest.fixture
def client(auth: CodeAuth) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.mark.parametrize(
    ("raw", "e164"),
    [
        ("9876543210", "+919876543210"),
        ("98765 43210", "+919876543210"),
        ("+91 98765-43210", "+919876543210"),
        ("09876543210", "+919876543210"),
        ("919876543210", "+919876543210"),
        ("+44 7911 123456", "+447911123456"),
    ],
)
def test_mobile_numbers_are_normalised(raw: str, e164: str) -> None:
    assert normalize_phone(raw) == e164


@pytest.mark.parametrize("raw", ["12345", "5876543210", "+91 12345 67890", "abcdefghij", "0000"])
def test_invalid_mobile_numbers_are_refused(raw: str) -> None:
    from app.core.errors import BadRequestError

    with pytest.raises(BadRequestError):
        normalize_phone(raw)


def activated_tenant(
    db_session: Session,
    auth: CodeAuth,
    *,
    email: str | None = None,
    phone: str | None = None,
    activated: bool = True,
) -> uuid.UUID:
    """A customer the property team invited; activated unless said otherwise."""
    user_id = uuid.uuid4()
    for contact in (email, phone):
        if contact:
            auth.users[contact] = user_id
    db_session.add(
        AppUser(id=user_id, email=email, phone=phone, full_name="Asha", role=UserRole.CUSTOMER)
    )
    db_session.flush()
    now = datetime.now(UTC)
    db_session.add(
        Tenant(
            name="Asha",
            email=email,
            phone=phone,
            user_id=user_id,
            client_invited_at=now,
            client_activated_at=now if activated else None,
        )
    )
    db_session.commit()
    return user_id


def test_unknown_contacts_are_refused_and_nothing_is_created(
    client: TestClient, db_session: Session
) -> None:
    users_before = db_session.scalar(select(func.count()).select_from(AppUser))
    for request, verify, body in (
        ("phone-otp/request", "phone-otp/verify", {"phone": "9876543210"}),
        ("otp/request", "otp/verify", {"email": "new@example.com"}),
    ):
        asked = client.post(f"{CUSTOMER}/{request}", json=body)
        assert asked.status_code == 202
        refused = client.post(f"{CUSTOMER}/{verify}", json={**body, "code": GOOD})
        assert refused.status_code == 403
        assert "set up by SIGMANIX when your contract is completed" in refused.json()["detail"]
    assert db_session.scalar(select(func.count()).select_from(AppUser)) == users_before
    assert db_session.scalar(select(func.count()).select_from(Tenant)) == 0


def test_an_activated_tenant_signs_in_by_mobile_or_email_code(
    client: TestClient, auth: CodeAuth, db_session: Session
) -> None:
    user_id = activated_tenant(db_session, auth, email="asha@example.com", phone="+919876543210")
    client.post(f"{CUSTOMER}/phone-otp/request", json={"phone": "98765 43210"})
    wrong = client.post(f"{CUSTOMER}/phone-otp/verify", json={"phone": "9876543210", "code": "1"})
    assert wrong.status_code == 422  # not a 6-digit code
    signed = client.post(f"{CUSTOMER}/phone-otp/verify", json={"phone": "9876543210", "code": GOOD})
    assert signed.status_code == 200, signed.text
    assert signed.json()["status"] == "SIGNED_IN"
    assert signed.json()["session"]["user"]["id"] == str(user_id)
    # The same code cannot be used twice.
    again = client.post(f"{CUSTOMER}/phone-otp/verify", json={"phone": "9876543210", "code": GOOD})
    assert again.status_code == 401
    client.post(f"{CUSTOMER}/otp/request", json={"email": "Asha@Example.com"})
    by_email = client.post(
        f"{CUSTOMER}/otp/verify", json={"email": "asha@example.com", "code": GOOD}
    )
    assert by_email.json()["session"]["user"]["id"] == str(user_id)


def test_an_invited_tenant_must_set_a_password_first(
    client: TestClient, auth: CodeAuth, db_session: Session
) -> None:
    activated_tenant(db_session, auth, email="asha@example.com", activated=False)
    client.post(f"{CUSTOMER}/otp/request", json={"email": "asha@example.com"})
    refused = client.post(
        f"{CUSTOMER}/otp/verify", json={"email": "asha@example.com", "code": GOOD}
    )
    assert refused.status_code == 403


def test_administrators_cannot_use_customer_codes(
    client: TestClient, auth: CodeAuth, db_session: Session
) -> None:
    admin_id = uuid.uuid4()
    auth.users["boss@example.com"] = admin_id
    db_session.add(AppUser(id=admin_id, email="boss@example.com", role=UserRole.ADMIN))
    db_session.commit()
    client.post(f"{CUSTOMER}/otp/request", json={"email": "boss@example.com"})
    refused = client.post(
        f"{CUSTOMER}/otp/verify", json={"email": "boss@example.com", "code": GOOD}
    )
    assert refused.status_code == 403


def test_code_requests_never_create_accounts_at_supabase(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    sent: list[dict] = []

    def post(url: str, **kwargs: object) -> httpx.Response:
        sent.append(kwargs["json"])  # type: ignore[arg-type]
        return httpx.Response(
            422, json={"error_code": "otp_disabled", "msg": "Signups not allowed for otp"}
        )

    monkeypatch.setattr(httpx, "post", post)
    gateway = SupabaseAuthGateway("http://supabase.test/auth/v1", "publishable")
    gateway.send_otp("unknown@example.com")  # same quiet answer as for a known address
    gateway.send_phone_otp("+919876543210")
    assert [body["create_user"] for body in sent] == [False, False]


def test_missing_sms_configuration_is_reported_not_faked(
    verifier: None, signing_key: ec.EllipticCurvePrivateKey
) -> None:
    app.dependency_overrides[get_auth_gateway] = lambda: CodeAuth(signing_key, sms_enabled=False)
    try:
        with TestClient(app) as client:
            response = client.post(f"{CUSTOMER}/phone-otp/request", json={"phone": "9876543210"})
        assert response.status_code == 503
        assert "not available yet" in response.json()["detail"]
    finally:
        app.dependency_overrides.pop(get_auth_gateway, None)


def test_the_supabase_gateway_maps_provider_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from app.core.errors import BadRequestError

    def answer(status: int, body: dict):  # type: ignore[no-untyped-def]
        monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(status, json=body))

    gateway = SupabaseAuthGateway("http://supabase.test/auth/v1", "publishable")
    answer(400, {"msg": "Unsupported phone provider"})
    with pytest.raises(ServiceUnavailableError, match="not available yet"):
        gateway.send_phone_otp("+919876543210")
    answer(429, {"msg": "rate limited"})
    with pytest.raises(ServiceUnavailableError, match="Too many codes"):
        gateway.send_phone_otp("+919876543210")
    answer(422, {"msg": "Invalid phone number format"})
    with pytest.raises(BadRequestError):
        gateway.send_phone_otp("+919876543210")
    answer(200, {})
    gateway.send_phone_otp("+919876543210")
    answer(403, {"error_code": "otp_expired", "msg": "Token has expired or is invalid"})
    with pytest.raises(UnauthorizedError, match="wrong or has expired"):
        gateway.verify_phone_otp("+919876543210", "123456")
