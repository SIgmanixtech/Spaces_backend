"""Test configuration.

The environment is set before the app is imported so tests never read the
developer's .env and can never reach the database named by DATABASE_URL there.
Database-backed tests run only against TEST_DATABASE_URL.
"""

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from dotenv import dotenv_values
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

BACKEND_ROOT = Path(__file__).resolve().parents[1]
# Nothing listens here, so tests without a test database fail fast instead of
# touching a real one.
UNREACHABLE_DATABASE_URL = "postgresql://postgres:postgres@127.0.0.1:1/unreachable"
TEST_ORIGIN = "http://localhost:5173"

_local_env = dotenv_values(BACKEND_ROOT / ".env")
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or _local_env.get("TEST_DATABASE_URL")

if TEST_DATABASE_URL and TEST_DATABASE_URL in {
    os.environ.get("DATABASE_URL"),
    _local_env.get("DATABASE_URL"),
}:
    pytest.exit(
        "TEST_DATABASE_URL is the same database as DATABASE_URL. "
        "Refusing to run tests against the application database.",
        returncode=2,
    )

# Database tests truncate every table. Supabase databases are always named
# "postgres", so this also stops a Supabase URL being used by mistake.
if TEST_DATABASE_URL and "test" not in (make_url(TEST_DATABASE_URL).database or ""):
    pytest.exit(
        'TEST_DATABASE_URL must point at a database whose name contains "test".',
        returncode=2,
    )

os.environ.update(
    {
        "APP_ENV": "test",
        "LOG_LEVEL": "WARNING",
        "DATABASE_URL": TEST_DATABASE_URL or UNREACHABLE_DATABASE_URL,
        "SUPABASE_URL": "http://supabase.test",
        "CORS_ALLOWED_ORIGINS": TEST_ORIGIN,
    }
)
for name in ("SUPABASE_SECRET_KEY", "SUPABASE_JWT_SECRET"):
    os.environ.pop(name, None)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

if TYPE_CHECKING:
    from app.models.user import AppUser

requires_database = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not configured"
)


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


TEST_ISSUER = "http://supabase.test/auth/v1"
TEST_AUDIENCE = "authenticated"


@pytest.fixture(scope="session")
def signing_key() -> ec.EllipticCurvePrivateKey:
    """Stands in for the Supabase project's ES256 signing key."""
    return ec.generate_private_key(ec.SECP256R1())


def make_token(key: ec.EllipticCurvePrivateKey, subject: uuid.UUID, **claims: object) -> str:
    now = datetime.now(UTC)
    payload: dict[str, object] = {
        "sub": str(subject),
        "iss": TEST_ISSUER,
        "aud": TEST_AUDIENCE,
        "iat": now,
        "exp": now + timedelta(minutes=30),
        "email": "admin@example.com",
        "role": "authenticated",
    }
    payload.update(claims)
    return jwt.encode(payload, key, algorithm="ES256")


@pytest.fixture
def verifier(signing_key: ec.EllipticCurvePrivateKey) -> Iterator[None]:
    """Verify tokens against the test key instead of fetching Supabase's JWKS."""
    from app.api.dependencies import get_token_verifier
    from app.core.security import TokenVerifier

    public_key = signing_key.public_key()
    app.dependency_overrides[get_token_verifier] = lambda: TokenVerifier(
        issuer=TEST_ISSUER, audience=TEST_AUDIENCE, key_resolver=lambda _token: public_key
    )
    yield
    app.dependency_overrides.pop(get_token_verifier, None)


@pytest.fixture
def admin_user(db_session: Session) -> "AppUser":
    from app.models.enums import UserRole
    from app.models.user import AppUser

    user = AppUser(id=uuid.uuid4(), email="admin@example.com", role=UserRole.ADMIN)
    db_session.add(user)
    db_session.commit()
    return user


@pytest.fixture
def api(
    verifier: None, admin_user: "AppUser", signing_key: ec.EllipticCurvePrivateKey
) -> Iterator[TestClient]:
    """A client authenticated as an admin."""
    token = make_token(signing_key, admin_user.id)
    with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def migrated_database() -> None:
    """Bring the disposable test database up to the latest migration."""
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is not configured")
    from alembic import command
    from alembic.config import Config

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    _rebuild_if_worn_out()
    command.upgrade(config, "head")


# PostgreSQL never reuses a dropped column's slot, and a table has at most 1600.
# The migration tests add and drop columns on every downgrade/upgrade cycle, so a
# long-lived test database eventually runs out. Rebuilding its schema resets that.
WORN_OUT_ATTNUM = 1200


def _rebuild_if_worn_out() -> None:
    from app.db.session import dispose_engine, get_engine

    with get_engine().begin() as connection:
        worst = connection.execute(
            text(
                "SELECT coalesce(max(a.attnum), 0) FROM pg_attribute a"
                " JOIN pg_class t ON t.oid = a.attrelid"
                " JOIN pg_namespace n ON n.oid = t.relnamespace"
                " WHERE n.nspname = 'public' AND t.relkind = 'r'"
            )
        ).scalar_one()
        if worst > WORN_OUT_ATTNUM:
            # Only ever the disposable database: its name was checked to contain "test".
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
    dispose_engine()


@pytest.fixture
def db_session(migrated_database: None) -> Iterator[Session]:
    from app.db.base import Base
    from app.db.session import get_session_factory

    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
        session.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        session.commit()
        session.close()
