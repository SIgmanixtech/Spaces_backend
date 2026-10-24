"""Run the real API against the disposable test database for end-to-end tests.

    python -m tests.e2e_server            # serves http://127.0.0.1:8001

Everything is the production application except sign-in: Supabase Auth is
replaced by a stand-in that accepts one fixed test account and signs tokens
with a throwaway key, so a client can log in without a real password.

It refuses to start unless TEST_DATABASE_URL names a local database whose name
contains "test". On start it migrates that database and EMPTIES every table.
"""

import os
import sys
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy.engine import make_url

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PORT = int(os.environ.get("E2E_PORT", "8001"))
E2E_EMAIL = "e2e-admin@example.com"
E2E_PASSWORD = "e2e-only-password"
ISSUER = "http://supabase.test/auth/v1"
AUDIENCE = "authenticated"

_url = os.environ.get("TEST_DATABASE_URL") or dotenv_values(BACKEND_ROOT / ".env").get(
    "TEST_DATABASE_URL"
)
if not _url:
    sys.exit("TEST_DATABASE_URL is not configured.")
_parsed = make_url(_url)
if _parsed.host not in {"127.0.0.1", "localhost"} or "test" not in (_parsed.database or ""):
    sys.exit('Refusing to start: TEST_DATABASE_URL must be a local database named "*test*".')

os.environ.update(
    {
        "APP_ENV": "test",
        "LOG_LEVEL": "WARNING",
        "DATABASE_URL": _url,
        "SUPABASE_URL": "http://supabase.test",
        "CORS_ALLOWED_ORIGINS": "http://localhost:8080,http://localhost:8081,http://localhost:5173",
        # Uploads go to a throwaway folder, never to Supabase Storage.
        "STORAGE_BACKEND": "local",
        "LOCAL_STORAGE_DIR": tempfile.mkdtemp(prefix="sigmanix-e2e-uploads-"),
    }
)

import jwt  # noqa: E402
import uvicorn  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.api.dependencies import get_token_verifier  # noqa: E402
from app.core.errors import UnauthorizedError  # noqa: E402
from app.core.security import TokenVerifier  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import get_session_factory  # noqa: E402
from app.main import app  # noqa: E402
from app.models.enums import UserRole  # noqa: E402
from app.models.user import AppUser  # noqa: E402
from app.services.auth import SignUp, SupabaseSession, get_auth_gateway  # noqa: E402

ADMIN_ID = uuid.uuid4()
_key = ec.generate_private_key(ec.SECP256R1())


class StandInAuth:
    """Issues genuinely signed, short-lived tokens. Knows the fixed admin account and
    any customer who signs up while the server runs (as Supabase would, with email
    confirmation switched off)."""

    def __init__(self) -> None:
        self.accounts: dict[str, tuple[uuid.UUID, str]] = {E2E_EMAIL: (ADMIN_ID, E2E_PASSWORD)}
        self.refresh_tokens: dict[str, tuple[uuid.UUID, str]] = {}

    def _issue(self, user_id: uuid.UUID = ADMIN_ID, email: str = E2E_EMAIL) -> SupabaseSession:
        now = datetime.now(UTC)
        token = jwt.encode(
            {
                "sub": str(user_id),
                "iss": ISSUER,
                "aud": AUDIENCE,
                "iat": now,
                "exp": now + timedelta(minutes=30),
                "email": email,
                "jti": uuid.uuid4().hex,
            },
            _key,
            algorithm="ES256",
        )
        refresh = uuid.uuid4().hex
        self.refresh_tokens[refresh] = (user_id, email)
        return SupabaseSession(access_token=token, refresh_token=refresh, expires_in=1800)

    def sign_in(self, email: str, password: str) -> SupabaseSession:
        account = self.accounts.get(email.lower())
        if account is None or account[1] != password:
            raise UnauthorizedError("Incorrect email or password.")
        return self._issue(account[0], email.lower())

    def sign_up(self, email: str, password: str, full_name: str) -> SignUp:
        email = email.lower()
        if email in self.accounts:
            return SignUp(user_id=None, session=None, existing=True)
        user_id = uuid.uuid4()
        self.accounts[email] = (user_id, password)
        return SignUp(user_id=str(user_id), session=self._issue(user_id, email), existing=False)

    def recover(self, email: str) -> None:
        return None

    def refresh(self, refresh_token: str) -> SupabaseSession:
        owner = self.refresh_tokens.pop(refresh_token, None)  # single use, as with Supabase
        if owner is None:
            raise UnauthorizedError("The session has expired. Sign in again.")
        return self._issue(*owner)

    def sign_out(self, access_token: str) -> None:
        self.refresh_tokens.clear()


def prepare_database() -> None:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    command.upgrade(config, "head")
    with get_session_factory()() as session:
        tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
        session.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        session.add(AppUser(id=ADMIN_ID, email=E2E_EMAIL, role=UserRole.ADMIN))
        session.commit()


def main() -> None:
    prepare_database()
    stand_in = StandInAuth()
    public_key = _key.public_key()
    app.dependency_overrides[get_auth_gateway] = lambda: stand_in
    app.dependency_overrides[get_token_verifier] = lambda: TokenVerifier(
        issuer=ISSUER, audience=AUDIENCE, key_resolver=lambda _token: public_key
    )
    print(f"E2E API on http://127.0.0.1:{PORT}/api/v1 (database: {_parsed.database})", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
