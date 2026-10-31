"""Sign-in through Supabase Auth.

The backend forwards credentials to Supabase and hands back the session it
issues. It never stores passwords or mints tokens of its own, and it only
returns a session to users registered in ``app_users``.
"""

import logging
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
from app.models.enums import UserRole
from app.models.user import AppUser
from app.schemas.auth import CurrentUserRead, SessionRead


@dataclass(frozen=True)
class SupabaseSession:
    access_token: str
    refresh_token: str
    expires_in: int


@dataclass(frozen=True)
class Invitation:
    user_id: str
    # True when Supabase accepted the request to email it (delivery is not verified).
    email_requested: bool
    # A link to share by hand, when no email was requested.
    link: str | None


class SupabaseAuthGateway:
    """Thin client for the Supabase Auth (GoTrue) REST API."""

    test_mode = False

    def __init__(
        self,
        auth_url: str,
        publishable_key: str | None,
        redirect_url: str | None = None,
        secret_key: str | None = None,
    ) -> None:
        self._secret = secret_key
        self._auth_url = auth_url
        self._key = publishable_key
        self._redirect = redirect_url

    def _return_to(self) -> dict[str, str] | None:
        """Where links in Supabase emails lead. Without it Supabase uses its Site URL."""
        return {"redirect_to": self._redirect} if self._redirect else None

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

    def send_otp(self, email: str) -> None:
        """Email a one-time sign-in code, creating the Supabase user on first use."""
        response = self._post(
            "/otp", params=self._return_to(), json={"email": email, "create_user": False}
        )
        if _signups_refused(response):
            return  # unknown address: same answer as a known one, nothing created
        if response.status_code == 429:
            raise ServiceUnavailableError(
                "Too many codes were requested. Wait a minute, then ask for a new code."
            )
        if response.status_code >= 500:
            raise ServiceUnavailableError("The sign-in code could not be sent. Try again later.")

    def send_phone_otp(self, phone: str) -> None:
        """Text a one-time sign-in code. Needs an SMS provider configured in Supabase Auth."""
        response = self._post("/otp", json={"phone": phone, "channel": "sms", "create_user": False})
        if _signups_refused(response):
            return  # unknown number: same answer as a known one, nothing created
        if response.status_code == 429:
            raise ServiceUnavailableError(
                "Too many codes were requested. Wait a minute, then ask for a new code."
            )
        if response.status_code >= 500 or response.status_code in (400, 422):
            body = response.json() if response.content else {}
            message = str(body.get("msg") or body.get("error_description") or "").lower()
            if response.status_code < 500 and not any(
                word in message for word in ("provider", "sms", "phone signups", "disabled")
            ):
                raise BadRequestError("Enter a valid mobile number.")
            raise ServiceUnavailableError(
                "Sign-in by mobile number is not available yet. Use your email instead."
            )

    def verify_phone_otp(self, phone: str, code: str) -> SupabaseSession:
        response = self._post("/verify", json={"type": "sms", "phone": phone, "token": code})
        return self._session(response, "That code is wrong or has expired. Ask for a new one.")

    def verify_otp(self, email: str, code: str) -> SupabaseSession:
        response = self._post("/verify", json={"type": "email", "email": email, "token": code})
        return self._session(response, "That code is wrong or has expired. Ask for a new one.")

    def recover(self, email: str) -> None:
        """Ask Supabase to email a password-reset link. Silent about unknown addresses."""
        with suppress(ServiceUnavailableError):
            self._post("/recover", params=self._return_to(), json={"email": email})

    # --- client app invitations (admin API; needs SUPABASE_SECRET_KEY) ---------------

    def _admin(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if not self._secret:
            raise ServiceUnavailableError(
                "Invitations need SUPABASE_SECRET_KEY on the server; it is not configured."
            )
        headers = {"apikey": self._secret}
        if not self._secret.startswith("sb_"):
            headers["Authorization"] = f"Bearer {self._secret}"
        try:
            return httpx.request(
                method, f"{self._auth_url}{path}", headers=headers, timeout=15, **kwargs
            )
        except httpx.HTTPError:
            raise ServiceUnavailableError("The authentication service is unavailable.") from None

    def invite(self, email: str, full_name: str) -> Invitation:
        """Ask Supabase to email an invitation (a link to choose a password). Supabase
        accepting the request does not prove delivery. If it cannot send (no email
        service, the address already has a sign-in, rate limit), the caller gets a link to
        share instead, and email_requested is False."""
        response = self._admin(
            "POST",
            "/invite",
            params=self._return_to(),
            json={"email": email, "data": {"full_name": full_name}},
        )
        if response.status_code == 200:
            body = response.json()
            user = body.get("user") or body
            return Invitation(user_id=str(user["id"]), email_requested=True, link=None)
        if response.status_code >= 500 and response.status_code != 503:
            logger.warning("Supabase could not send the invitation (HTTP %s)", response.status_code)
        return self.invitation_link(email, full_name)

    def invitation_link(self, email: str, full_name: str) -> Invitation:
        """A link the property team can share. Nothing is emailed. A first link is an
        invitation; for an address that already has a sign-in it is a password-reset
        link, which leads to the same set-password page."""
        for kind in ("invite", "recovery"):
            body: dict[str, Any] = {"type": kind, "email": email}
            if kind == "invite":
                body["data"] = {"full_name": full_name}
            if self._redirect:
                body["redirect_to"] = self._redirect
            response = self._admin("POST", "/admin/generate_link", json=body)
            if response.status_code == 200:
                data = response.json()
                props = data.get("properties") or data
                user_id = data.get("id") or (data.get("user") or {}).get("id")
                if not user_id or not props.get("action_link"):
                    break
                return Invitation(
                    user_id=str(user_id), email_requested=False, link=props["action_link"]
                )
            if response.status_code == 429:
                raise ServiceUnavailableError("Too many invitations just now. Try again shortly.")
            if response.status_code >= 500:
                break
        raise ServiceUnavailableError("The invitation could not be created. Try again later.")

    def set_password(self, access_token: str, password: str) -> None:
        """Set the password of the user the access token belongs to (from an invitation
        or reset link)."""
        if not self._key:
            raise ServiceUnavailableError("Sign-in is not configured on the server.")
        try:
            response = httpx.put(
                f"{self._auth_url}/user",
                headers={"apikey": self._key, "Authorization": f"Bearer {access_token}"},
                json={"password": password},
                timeout=15,
            )
        except httpx.HTTPError:
            raise ServiceUnavailableError("The authentication service is unavailable.") from None
        if response.status_code in (401, 403):
            raise UnauthorizedError("This link has expired or was already used. Ask for a new one.")
        if response.status_code == 422:
            body = response.json() if response.content else {}
            raise BadRequestError(
                str(body.get("msg") or "Choose a stronger password (at least 8 characters).")
            )
        if response.status_code != 200:
            raise ServiceUnavailableError("The password could not be saved. Try again later.")

    def sign_out(self, access_token: str) -> None:
        """Revoke the session's refresh token. Failures are not fatal to the caller."""
        with suppress(ServiceUnavailableError):
            self._post(
                "/logout",
                params={"scope": "local"},
                headers={"Authorization": f"Bearer {access_token}"},
            )


logger = logging.getLogger(__name__)


def _signups_refused(response: httpx.Response) -> bool:
    """Supabase's answer when a code is asked for an address or number without an
    account and account creation is off (create_user false)."""
    if response.status_code not in (400, 403, 422):
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    if not isinstance(body, dict):
        return False
    text = " ".join(
        str(body.get(k) or "") for k in ("error_code", "msg", "error_description", "message")
    ).lower()
    return "signup" in text or "otp_disabled" in text or "user not found" in text


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
    auth_url = f"{str(settings.supabase_url).rstrip('/')}/auth/v1"
    # Invitation and password-reset links open the client app's set-password page.
    redirect = (
        f"{str(settings.customer_app_url).rstrip('/')}/set-password"
        if settings.customer_app_url
        else None
    )
    secret = settings.supabase_secret_key
    return SupabaseAuthGateway(
        auth_url,
        key.get_secret_value() if key else None,
        redirect,
        secret.get_secret_value() if secret else None,
    )


def open_session(
    session: Session, verifier: TokenVerifier, supabase_session: SupabaseSession
) -> SessionRead:
    """Return the session only if its user is allowed to use this application."""
    claims = verifier.verify(supabase_session.access_token)
    user = session.get(AppUser, claims.subject)
    if user is None or not user.is_active:
        raise ForbiddenError("Your account is not authorized to use this application.")
    if user.role == UserRole.CUSTOMER:
        from app.services.client_access import NOT_ACTIVATED, active_tenant

        if active_tenant(session, user) is None:
            raise ForbiddenError(NOT_ACTIVATED)
    return SessionRead(
        access_token=supabase_session.access_token,
        expires_in=supabase_session.expires_in,
        refresh_token=supabase_session.refresh_token,
        user=CurrentUserRead.model_validate(user),
    )
