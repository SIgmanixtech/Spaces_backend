"""Sign-in endpoints. Supabase Auth is replaced by a fake that issues real signed tokens."""

import uuid
from collections.abc import Iterator

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.errors import UnauthorizedError
from app.main import app
from app.models.user import AppUser
from app.services.auth import SupabaseAuthGateway, SupabaseSession, get_auth_gateway
from tests.conftest import make_token

LOGIN = "/api/v1/auth/login"
REFRESH = "/api/v1/auth/refresh"
PASSWORD = "correct horse battery staple"


class FakeSupabaseAuth:
    def __init__(self, key: ec.EllipticCurvePrivateKey) -> None:
        self.key = key
        self.accounts: dict[str, uuid.UUID] = {}
        self.signed_out: list[str] = []

    def _issue(self, subject: uuid.UUID) -> SupabaseSession:
        return SupabaseSession(
            access_token=make_token(self.key, subject),
            refresh_token=f"refresh-{subject}",
            expires_in=3600,
        )

    def sign_in(self, email: str, password: str) -> SupabaseSession:
        if email not in self.accounts or password != PASSWORD:
            raise UnauthorizedError("Incorrect email or password.")
        return self._issue(self.accounts[email])

    def refresh(self, refresh_token: str) -> SupabaseSession:
        for subject in self.accounts.values():
            if refresh_token == f"refresh-{subject}":
                return self._issue(subject)
        raise UnauthorizedError("The session has expired. Sign in again.")

    def sign_out(self, access_token: str) -> None:
        self.signed_out.append(access_token)


@pytest.fixture
def supabase(
    verifier: None, admin_user: AppUser, signing_key: ec.EllipticCurvePrivateKey
) -> Iterator[FakeSupabaseAuth]:
    fake = FakeSupabaseAuth(signing_key)
    fake.accounts[admin_user.email] = admin_user.id
    app.dependency_overrides[get_auth_gateway] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_auth_gateway, None)


def test_login_returns_a_working_session(client: TestClient, supabase: FakeSupabaseAuth) -> None:
    response = client.post(LOGIN, json={"email": "admin@example.com", "password": PASSWORD})

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 3600
    assert body["user"]["role"] == "ADMIN"

    headers = {"Authorization": f"Bearer {body['access_token']}"}
    assert client.get("/api/v1/properties", headers=headers).status_code == 200


def test_wrong_password_and_unknown_email_look_the_same(
    client: TestClient, supabase: FakeSupabaseAuth
) -> None:
    wrong = client.post(LOGIN, json={"email": "admin@example.com", "password": "nope"})
    unknown = client.post(LOGIN, json={"email": "ghost@example.com", "password": PASSWORD})

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"] == "Incorrect email or password."
    assert "access_token" not in wrong.text


def test_supabase_user_without_app_access_gets_no_session(
    client: TestClient, supabase: FakeSupabaseAuth
) -> None:
    supabase.accounts["outsider@example.com"] = uuid.uuid4()

    response = client.post(LOGIN, json={"email": "outsider@example.com", "password": PASSWORD})

    assert response.status_code == 403
    assert "access_token" not in response.text


def test_deactivated_admin_cannot_sign_in_or_refresh(
    client: TestClient, supabase: FakeSupabaseAuth, admin_user: AppUser, db_session: Session
) -> None:
    admin_user.is_active = False
    db_session.commit()

    assert (
        client.post(LOGIN, json={"email": "admin@example.com", "password": PASSWORD}).status_code
        == 403
    )
    assert (
        client.post(REFRESH, json={"refresh_token": f"refresh-{admin_user.id}"}).status_code == 403
    )


def test_login_validation_never_echoes_the_password(
    client: TestClient, supabase: FakeSupabaseAuth
) -> None:
    response = client.post(LOGIN, json={"email": "not-an-email", "password": "hunter2-secret"})

    assert response.status_code == 422
    assert "hunter2-secret" not in response.text


def test_refresh_issues_a_new_session(
    client: TestClient, supabase: FakeSupabaseAuth, admin_user: AppUser
) -> None:
    renewed = client.post(REFRESH, json={"refresh_token": f"refresh-{admin_user.id}"})
    rejected = client.post(REFRESH, json={"refresh_token": "made-up"})

    assert renewed.status_code == 200
    assert renewed.json()["user"]["email"] == "admin@example.com"
    assert rejected.status_code == 401


def test_logout_revokes_the_session_at_supabase(
    api: TestClient, supabase: FakeSupabaseAuth
) -> None:
    assert api.post("/api/v1/auth/logout").status_code == 204
    assert len(supabase.signed_out) == 1


def test_logout_requires_a_session(client: TestClient, supabase: FakeSupabaseAuth) -> None:
    assert client.post("/api/v1/auth/logout").status_code == 401


def test_sign_in_is_unavailable_without_a_publishable_key() -> None:
    from app.core.errors import ServiceUnavailableError

    gateway = SupabaseAuthGateway("http://supabase.test/auth/v1", None)

    with pytest.raises(ServiceUnavailableError):
        gateway.sign_in("admin@example.com", PASSWORD)


def _gateway_answering(monkeypatch: pytest.MonkeyPatch, status: int, body: dict) -> list:
    import httpx

    calls: list = []

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        calls.append((url, kwargs.get("json")))
        return httpx.Response(status, json=body)

    monkeypatch.setattr(httpx, "post", fake_post)
    return calls


def test_an_unconfirmed_email_is_told_so_not_wrong_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.errors import EmailNotConfirmedError

    gateway = SupabaseAuthGateway("http://supabase.test/auth/v1", "publishable")
    # What Supabase answers for the right password on an address never confirmed.
    _gateway_answering(
        monkeypatch,
        400,
        {"code": 400, "error_code": "email_not_confirmed", "msg": "Email not confirmed"},
    )
    with pytest.raises(EmailNotConfirmedError, match="not confirmed"):
        gateway.sign_in("new@example.com", PASSWORD)
    # Older Supabase versions give only the message.
    _gateway_answering(
        monkeypatch, 400, {"error": "invalid_grant", "error_description": "Email not confirmed"}
    )
    with pytest.raises(EmailNotConfirmedError):
        gateway.sign_in("new@example.com", PASSWORD)
    # A wrong password still gets the one message for every bad sign-in.
    _gateway_answering(
        monkeypatch,
        400,
        {"error_code": "invalid_credentials", "msg": "Invalid login credentials"},
    )
    with pytest.raises(UnauthorizedError, match="Incorrect email or password") as wrong:
        gateway.sign_in("new@example.com", "wrong")
    assert not isinstance(wrong.value, EmailNotConfirmedError)


def test_unconfirmed_sign_in_answers_with_its_own_code(client: TestClient) -> None:
    from app.core.errors import EmailNotConfirmedError

    class Unconfirmed:
        def sign_in(self, email: str, password: str) -> SupabaseSession:
            raise EmailNotConfirmedError("Your email address is not confirmed yet.")

    app.dependency_overrides[get_auth_gateway] = Unconfirmed
    try:
        response = client.post(LOGIN, json={"email": "new@example.com", "password": PASSWORD})
    finally:
        app.dependency_overrides.pop(get_auth_gateway, None)
    assert response.status_code == 401
    assert response.json()["code"] == "email_not_confirmed"


def test_confirmation_email_can_be_sent_again(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = SupabaseAuthGateway("http://supabase.test/auth/v1", "publishable")
    calls = _gateway_answering(monkeypatch, 200, {})
    app.dependency_overrides[get_auth_gateway] = lambda: gateway
    try:
        response = client.post(
            "/api/v1/customer/resend-confirmation", json={"email": "new@example.com"}
        )
    finally:
        app.dependency_overrides.pop(get_auth_gateway, None)
    assert response.status_code == 202
    assert calls == [
        ("http://supabase.test/auth/v1/resend", {"type": "signup", "email": "new@example.com"})
    ]
