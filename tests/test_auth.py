import uuid
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.dependencies import require_roles
from app.core.errors import ForbiddenError
from app.models.enums import UserRole
from app.models.user import AppUser
from tests.conftest import make_token

PROTECTED_PATHS = [
    "/api/v1/auth/me",
    "/api/v1/properties",
    "/api/v1/buildings",
    "/api/v1/floors",
    "/api/v1/layouts",
    "/api/v1/spaces",
    "/api/v1/bookings",
    "/api/v1/bookings/availability",
    "/api/v1/amenities",
    "/api/v1/tenants",
    "/api/v1/assets",
]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("path", PROTECTED_PATHS)
def test_requests_without_a_token_are_rejected(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_every_business_route_requires_authentication(client: TestClient) -> None:
    """Guards against a future endpoint being added without an auth dependency."""
    public = {
        "/api/v1/health",
        "/api/v1/health/ready",
        "/api/v1/auth/login",
        "/api/v1/auth/refresh",
        # The client app: signing up and resetting a password happen before sign-in.
        "/api/v1/customer/register",
        "/api/v1/customer/password-recovery",
        "/api/v1/customer/resend-confirmation",
    }
    # The published-property catalogue is public by design and lives under its own prefix.
    catalogue = "/api/v1/public/"
    schema = client.get("/api/v1/openapi.json").json()
    assert any(path.startswith(catalogue) for path in schema["paths"])

    unprotected = [
        f"{method.upper()} {path}"
        for path, operations in schema["paths"].items()
        if path not in public and not path.startswith(catalogue)
        for method, operation in operations.items()
        if not operation.get("security")
    ]

    assert unprotected == []


def test_me_returns_the_admin(api: TestClient, admin_user: AppUser) -> None:
    response = api.get("/api/v1/auth/me")

    assert response.status_code == 200
    assert response.json() == {
        "id": str(admin_user.id),
        "email": "admin@example.com",
        "full_name": None,
        "role": "ADMIN",
    }


def test_token_signed_by_another_key_is_rejected(
    client: TestClient, verifier: None, admin_user: AppUser
) -> None:
    forged = make_token(ec.generate_private_key(ec.SECP256R1()), admin_user.id)

    response = client.get("/api/v1/auth/me", headers=bearer(forged))

    assert response.status_code == 401


@pytest.mark.parametrize(
    "claims",
    [
        {"exp": datetime.now(UTC) - timedelta(minutes=5)},
        {"aud": "some-other-audience"},
        {"iss": "https://attacker.example/auth/v1"},
        {"sub": "not-a-uuid"},
    ],
    ids=["expired", "wrong-audience", "wrong-issuer", "bad-subject"],
)
def test_token_with_bad_claims_is_rejected(
    client: TestClient,
    verifier: None,
    admin_user: AppUser,
    signing_key: ec.EllipticCurvePrivateKey,
    claims: dict[str, object],
) -> None:
    token = make_token(signing_key, admin_user.id, **claims)

    response = client.get("/api/v1/auth/me", headers=bearer(token))

    assert response.status_code == 401


def test_unsigned_and_garbage_tokens_are_rejected(client: TestClient, verifier: None) -> None:
    # {"alg":"none"} header with an empty signature.
    unsigned = "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0.eyJzdWIiOiJ4In0."

    assert client.get("/api/v1/auth/me", headers=bearer(unsigned)).status_code == 401
    assert client.get("/api/v1/auth/me", headers=bearer("garbage")).status_code == 401


def test_valid_supabase_user_without_app_access_is_forbidden(
    client: TestClient,
    verifier: None,
    db_session: Session,
    signing_key: ec.EllipticCurvePrivateKey,
) -> None:
    stranger = make_token(signing_key, uuid.uuid4())

    response = client.get("/api/v1/properties", headers=bearer(stranger))

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


def test_role_claimed_in_the_token_is_ignored(
    client: TestClient,
    verifier: None,
    db_session: Session,
    signing_key: ec.EllipticCurvePrivateKey,
) -> None:
    token = make_token(signing_key, uuid.uuid4(), role="ADMIN", app_metadata={"role": "ADMIN"})

    assert client.get("/api/v1/properties", headers=bearer(token)).status_code == 403


def test_deactivated_admin_is_forbidden(
    api: TestClient, db_session: Session, admin_user: AppUser
) -> None:
    admin_user.is_active = False
    db_session.commit()

    assert api.get("/api/v1/properties").status_code == 403


def test_role_check_rejects_other_roles() -> None:
    # ADMIN is the only role the database accepts today, so the check is
    # exercised directly with a role that a later phase might introduce.
    admin_only = require_roles(UserRole.ADMIN)
    viewer = AppUser(id=uuid.uuid4(), email="viewer@example.com", role="VIEWER")

    with pytest.raises(ForbiddenError):
        admin_only(viewer)
    assert admin_only(AppUser(id=uuid.uuid4(), email="a@example.com", role=UserRole.ADMIN))
