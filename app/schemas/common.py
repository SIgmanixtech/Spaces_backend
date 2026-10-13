"""Schemas and base classes shared across endpoints."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar, Generic, Literal, Self, TypeVar

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

T = TypeVar("T")


class ReadSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    @field_validator("*", mode="after")
    @classmethod
    def _timestamps_in_utc(cls, value: Any) -> Any:
        # PostgreSQL returns timestamps in the connection's time zone, which
        # differs between servers. Responses always carry UTC.
        if isinstance(value, datetime) and value.tzinfo is not None:
            return value.astimezone(UTC)
        return value


class InputSchema(BaseModel):
    """Request bodies reject unknown fields so typos and read-only fields fail loudly."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class UpdateSchema(InputSchema):
    """PATCH body: omitted fields are left unchanged."""

    # Fields that may be omitted but, when sent, must not be null.
    required_fields: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="after")
    def _reject_null_for_required(self) -> Self:
        nulls = sorted(
            name
            for name in self.model_fields_set & self.required_fields
            if getattr(self, name) is None
        )
        if nulls:
            raise ValueError(f"{', '.join(nulls)} cannot be null")
        return self


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int


@dataclass(frozen=True)
class ListQuery:
    page: int = 1
    page_size: int = 20
    search: str | None = None
    sort: str | None = None


class FieldError(BaseModel):
    field: str
    message: str


class ErrorResponse(BaseModel):
    detail: str
    code: str
    request_id: str | None = None
    errors: list[FieldError] | None = None


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
    version: str
    environment: str


class ReadinessResponse(BaseModel):
    status: Literal["ready"]
    checks: dict[str, Literal["ok"]]
