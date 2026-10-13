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
from app.core.errors import ForbiddenError, ServiceUnavailableError, UnauthorizedError
from app.core.security import TokenVerifier
from app.models.user import AppUser
from app.schemas.auth import CurrentUserRead, SessionRead


@dataclass(frozen=True)
class SupabaseSession:
    access_token: str
    refresh_token: str
    expires_in: int


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
        return self._session(response, "Incorrect email or password.")

    def refresh(self, refresh_token: str) -> SupabaseSession:
        response = self._post(
            "/token",
            params={"grant_type": "refresh_token"},
            json={"refresh_token": refresh_token},
        )
        return self._session(response, "The session has expired. Sign in again.")

    def sign_out(self, access_token: str) -> None:
        """Revoke the session's refresh token. Failures are not fatal to the caller."""
        with suppress(ServiceUnavailableError):
            self._post(
                "/logout",
                params={"scope": "local"},
                headers={"Authorization": f"Bearer {access_token}"},
            )


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
