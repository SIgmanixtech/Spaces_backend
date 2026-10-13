import uuid
from typing import Literal

from pydantic import EmailStr, Field, SecretStr

from app.models.enums import UserRole
from app.schemas.common import InputSchema, ReadSchema


class CurrentUserRead(ReadSchema):
    id: uuid.UUID
    email: str
    full_name: str | None
    role: UserRole


class LoginRequest(InputSchema):
    email: EmailStr
    # SecretStr keeps the password out of logs, reprs and validation errors.
    password: SecretStr = Field(min_length=1, max_length=128)


class RefreshRequest(InputSchema):
    refresh_token: str = Field(min_length=1, max_length=2000)


class SessionRead(ReadSchema):
    access_token: str = Field(description="Send as `Authorization: Bearer <token>`.")
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = Field(description="Seconds until the access token expires.")
    refresh_token: str = Field(description="Exchange at /auth/refresh for a new session.")
    user: CurrentUserRead
