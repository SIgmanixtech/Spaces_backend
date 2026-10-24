"""Shared FastAPI dependencies: database session, authentication, list parameters."""

from collections.abc import Callable
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import ForbiddenError, UnauthorizedError
from app.core.security import TokenVerifier
from app.db.session import get_db
from app.models.enums import UserRole
from app.models.user import AppUser
from app.schemas.common import ListQuery

DbSession = Annotated[Session, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]

_bearer = HTTPBearer(auto_error=False, description="Supabase Auth access token.")


@lru_cache
def get_token_verifier() -> TokenVerifier:
    settings = get_settings()
    auth_url = f"{str(settings.supabase_url).rstrip('/')}/auth/v1"
    secret = settings.supabase_jwt_secret
    return TokenVerifier(
        issuer=auth_url,
        audience=settings.supabase_jwt_audience,
        jwks_url=f"{auth_url}/.well-known/jwks.json",
        hs256_secret=secret.get_secret_value() if secret else None,
    )


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: DbSession,
    verifier: Annotated[TokenVerifier, Depends(get_token_verifier)],
) -> AppUser:
    if credentials is None:
        raise UnauthorizedError("Authentication is required.")
    claims = verifier.verify(credentials.credentials)
    # A valid Supabase login is not enough: the user must be registered here.
    user = session.get(AppUser, claims.subject)
    if user is None or not user.is_active:
        raise ForbiddenError("Your account is not authorized to use this application.")
    return user


CurrentUser = Annotated[AppUser, Depends(get_current_user)]


def require_roles(*roles: UserRole) -> Callable[[AppUser], AppUser]:
    def dependency(user: CurrentUser) -> AppUser:
        if user.role not in roles:
            raise ForbiddenError("You do not have permission to perform this action.")
        return user

    return dependency


CurrentAdmin = Annotated[AppUser, Depends(require_roles(UserRole.ADMIN))]
# The client app's customers. Admin and customer permissions never overlap.
CurrentCustomer = Annotated[AppUser, Depends(require_roles(UserRole.CUSTOMER))]


def list_query(
    page: Annotated[int, Query(ge=1, description="Page number, starting at 1.")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="Items per page.")] = 20,
    search: Annotated[
        str | None, Query(max_length=100, description="Case-insensitive text search.")
    ] = None,
    sort: Annotated[
        str | None,
        Query(max_length=50, description="Field to sort by. Prefix with `-` for descending."),
    ] = None,
) -> ListQuery:
    return ListQuery(page=page, page_size=page_size, search=search, sort=sort)


ListParams = Annotated[ListQuery, Depends(list_query)]
