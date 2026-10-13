"""Authentication.

Passwords are checked by Supabase Auth, never by this API. Login forwards the
credentials to Supabase and returns the session it issues, but only to users
registered in this application. Every other endpoint verifies the access token.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.dependencies import CurrentUser, DbSession, get_token_verifier
from app.api.responses import PROTECTED, VALIDATION_ERROR, _error
from app.core.security import TokenVerifier
from app.schemas.auth import CurrentUserRead, LoginRequest, RefreshRequest, SessionRead
from app.services.auth import SupabaseAuthGateway, get_auth_gateway, open_session

router = APIRouter(prefix="/auth", tags=["Auth"])

Gateway = Annotated[SupabaseAuthGateway, Depends(get_auth_gateway)]
Verifier = Annotated[TokenVerifier, Depends(get_token_verifier)]

_SIGN_IN_ERRORS = {
    401: _error("The credentials or refresh token were rejected."),
    403: _error("The account is valid but not registered for this application."),
    503: _error("Supabase Auth could not be reached, or sign-in is not configured."),
    **VALIDATION_ERROR,
}


@router.post("/login", response_model=SessionRead, responses=_SIGN_IN_ERRORS, summary="Sign in")
def login(
    body: LoginRequest, session: DbSession, gateway: Gateway, verifier: Verifier
) -> SessionRead:
    """Exchange an email and password for a session. Public."""
    supabase_session = gateway.sign_in(body.email, body.password.get_secret_value())
    return open_session(session, verifier, supabase_session)


@router.post(
    "/refresh", response_model=SessionRead, responses=_SIGN_IN_ERRORS, summary="Renew a session"
)
def refresh(
    body: RefreshRequest, session: DbSession, gateway: Gateway, verifier: Verifier
) -> SessionRead:
    """Exchange a refresh token for a new session. The old refresh token stops
    working, so store the new one. Public."""
    return open_session(session, verifier, gateway.refresh(body.refresh_token))


@router.post("/logout", status_code=204, responses=PROTECTED, summary="Sign out")
def logout(
    user: CurrentUser,
    gateway: Gateway,
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(HTTPBearer())],
) -> None:
    """Revoke this session's refresh token at Supabase."""
    gateway.sign_out(credentials.credentials)


@router.get("/me", response_model=CurrentUserRead, responses=PROTECTED, summary="Current user")
def me(user: CurrentUser) -> CurrentUserRead:
    """Return the signed-in user and their role in this application."""
    return CurrentUserRead.model_validate(user)
