"""Verification of Supabase Auth access tokens.

The token only proves who the caller is. What they may do is decided from the
``app_users`` table (see ``app.api.dependencies``), never from token claims.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import jwt
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientConnectionError

from app.core.errors import ServiceUnavailableError, UnauthorizedError

_ASYMMETRIC_ALGORITHMS = {"ES256", "RS256"}
_INVALID_TOKEN = "Invalid or expired access token."


@dataclass(frozen=True)
class TokenClaims:
    subject: uuid.UUID
    email: str | None


class TokenVerifier:
    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_url: str | None = None,
        hs256_secret: str | None = None,
        key_resolver: Callable[[str], Any] | None = None,
    ) -> None:
        self._issuer = issuer
        self._audience = audience
        self._hs256_secret = hs256_secret
        if key_resolver is None:
            if jwks_url is None:
                raise ValueError("jwks_url or key_resolver is required")
            jwks_client = PyJWKClient(jwks_url, cache_keys=True, lifespan=600, timeout=10)
            key_resolver = lambda token: jwks_client.get_signing_key_from_jwt(token).key  # noqa: E731
        self._resolve_key = key_resolver

    def verify(self, token: str) -> TokenClaims:
        try:
            algorithm = jwt.get_unverified_header(token).get("alg")
            if algorithm in _ASYMMETRIC_ALGORITHMS:
                key = self._resolve_key(token)
            elif algorithm == "HS256" and self._hs256_secret:
                # Legacy projects only. The shared secret is never a published key,
                # so a public key cannot be passed off as an HMAC secret.
                key = self._hs256_secret
            else:
                raise UnauthorizedError(_INVALID_TOKEN)
            claims = jwt.decode(
                token,
                key,
                algorithms=[algorithm],
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "sub", "iss", "aud"]},
                leeway=10,
            )
            return TokenClaims(subject=uuid.UUID(claims["sub"]), email=claims.get("email"))
        except PyJWKClientConnectionError:
            raise ServiceUnavailableError("The authentication service is unavailable.") from None
        except (jwt.PyJWTError, ValueError, TypeError):
            raise UnauthorizedError(_INVALID_TOKEN) from None
