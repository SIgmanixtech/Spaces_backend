"""Sign-in through Supabase Auth.

The backend forwards credentials to Supabase and hands back the session it
issues. It never stores passwords or mints tokens of its own, and it only
returns a session to users registered in ``app_users``.
"""

from contextlib import suppress
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import (
    BadRequestError,
    EmailNotConfirmedError,
    ForbiddenError,
    ServiceUnavailableError,
    UnauthorizedError,
)
from app.core.security import TokenVerifier
from app.models.user import AppUser
from app.schemas.auth import CurrentUserRead, SessionRead


@dataclass(frozen=True)
class SupabaseSession:
    access_token: str
    refresh_token: str
    expires_in: int


@dataclass(frozen=True)
class SignUp:
    user_id: str | None
    # Present when Supabase signs the user in at once (email confirmation off).
    session: SupabaseSession | None
    existing: bool


class SupabaseAuthGateway:
    """Thin client for the Supabase Auth (GoTrue) REST API."""

    def __init__(self, auth_url: str, publishable_key: str | None) -> None:
        self._auth_url = auth_url
        self._key = publishable_key

    def _post(self, path: str, *, params: dict[str, str] | None = None, **kwargs: Any) -> Any:
        if not self._key:
            raise ServiceUnavailableError("Sign-in is not configured on the server.")
        headers = {"apikey": self._key, **kwargs.pop("headers", {})}
        try:
            return httpx.post(
                f"{self._auth_url}{path}", params=params, headers=headers, timeout=15, **kwargs
            )
        except httpx.HTTPError:
            raise ServiceUnavailableError("The authentication service is unavailable.") from None

    def _session(self, response: httpx.Response, failure: str) -> SupabaseSession:
        if response.status_code >= 500 or response.status_code == 429:
            raise ServiceUnavailableError("The authentication service is unavailable.")
        if response.status_code != 200:
            # One message for every rejection, so callers cannot tell which part was wrong.
            raise UnauthorizedError(failure)
        body = response.json()
        return SupabaseSession(
            access_token=body["access_token"],
            refresh_token=body["refresh_token"],
            expires_in=int(body["expires_in"]),
        )

    def sign_in(self, email: str, password: str) -> SupabaseSession:
        response = self._post(
            "/token",
            params={"grant_type": "password"},
            json={"email": email, "password": password},
        )
        if response.status_code == 400 and _not_confirmed(response):
            # Supabase checks the password before this, so it reveals nothing to a guesser.
            raise EmailNotConfirmedError(
                "Your email address is not confirmed yet. Open the link in the email we sent "
                "when you registered, then sign in."
            )
        return self._session(response, "Incorrect email or password.")

    def refresh(self, refresh_token: str) -> SupabaseSession:
        response = self._post(
            "/token",
            params={"grant_type": "refresh_token"},
            json={"refresh_token": refresh_token},
        )
        return self._session(response, "The session has expired. Sign in again.")

    def sign_up(self, email: str, password: str, full_name: str) -> SignUp:
        """Create a Supabase Auth user. Supabase answers a repeated address the same way
        as a new one (with no identities) so callers cannot probe for accounts."""
        response = self._post(
            "/signup", json={"email": email, "password": password, "data": {"full_name": full_name}}
        )
        if response.status_code >= 500 or response.status_code == 429:
            raise ServiceUnavailableError("The authentication service is unavailable.")
        if response.status_code == 422 or response.status_code == 400:
            body = response.json() if response.content else {}
            message = str(body.get("msg") or body.get("error_description") or "")
            if "already" in message.lower():
                return SignUp(user_id=None, session=None, existing=True)
            raise BadRequestError(message or "The email or password was not accepted.")
        if response.status_code != 200:
            raise ServiceUnavailableError("The account could not be created. Try again later.")
        body = response.json()
        user = body.get("user") or body
        if not user.get("identities"):
            return SignUp(user_id=None, session=None, existing=True)
        session = (
            SupabaseSession(
                access_token=body["access_token"],
                refresh_token=body["refresh_token"],
                expires_in=int(body["expires_in"]),
            )
            if body.get("access_token")
            else None
        )
        return SignUp(user_id=user["id"], session=session, existing=False)

    def send_otp(self, email: str) -> None:
        """Email a one-time sign-in code, creating the Supabase user on first use."""
        response = self._post("/otp", json={"email": email, "create_user": True})
        if response.status_code == 429:
            raise ServiceUnavailableError(
                "Too many codes were requested. Wait a minute, then ask for a new code."
            )
        if response.status_code >= 500:
            raise ServiceUnavailableError("The sign-in code could not be sent. Try again later.")

    def verify_otp(self, email: str, code: str) -> SupabaseSession:
        response = self._post("/verify", json={"type": "email", "email": email, "token": code})
        return self._session(response, "That code is wrong or has expired. Ask for a new one.")

    def resend_confirmation(self, email: str) -> None:
        """Email the sign-up confirmation link again. Silent about unknown addresses."""
        with suppress(ServiceUnavailableError):
            self._post("/resend", json={"type": "signup", "email": email})

    def recover(self, email: str) -> None:
        """Ask Supabase to email a password-reset link. Silent about unknown addresses."""
        with suppress(ServiceUnavailableError):
            self._post("/recover", json={"email": email})

    def sign_out(self, access_token: str) -> None:
        """Revoke the session's refresh token. Failures are not fatal to the caller."""
        with suppress(ServiceUnavailableError):
            self._post(
                "/logout",
                params={"scope": "local"},
                headers={"Authorization": f"Bearer {access_token}"},
            )


def _not_confirmed(response: httpx.Response) -> bool:
    try:
        body = response.json()
    except ValueError:
        return False
    if not isinstance(body, dict):
        return False
    if body.get("error_code") == "email_not_confirmed":
        return True
    message = str(body.get("msg") or body.get("error_description") or "")
    return "not confirmed" in message.lower()


@lru_cache
def get_auth_gateway() -> SupabaseAuthGateway:
    settings = get_settings()
    key = settings.supabase_publishable_key
    return SupabaseAuthGateway(
        f"{str(settings.supabase_url).rstrip('/')}/auth/v1",
        key.get_secret_value() if key else None,
    )


def open_session(
    session: Session, verifier: TokenVerifier, supabase_session: SupabaseSession
) -> SessionRead:
    """Return the session only if its user is allowed to use this application."""
    claims = verifier.verify(supabase_session.access_token)
    user = session.get(AppUser, claims.subject)
    if user is None or not user.is_active:
        raise ForbiddenError("Your account is not authorized to use this application.")
    return SessionRead(
        access_token=supabase_session.access_token,
        expires_in=supabase_session.expires_in,
        refresh_token=supabase_session.refresh_token,
        user=CurrentUserRead.model_validate(user),
    )
