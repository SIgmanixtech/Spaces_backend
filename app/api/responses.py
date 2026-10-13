"""Error responses documented in OpenAPI. All share the ErrorResponse shape."""

from typing import Any

from app.schemas.common import ErrorResponse


def _error(description: str) -> dict[str, Any]:
    return {"model": ErrorResponse, "description": description}


VALIDATION_ERROR = {422: _error("The request failed validation. See `errors` for fields.")}

PROTECTED = {
    400: _error("The request is not valid for the current data."),
    401: _error("Missing, invalid or expired access token."),
    403: _error("The user is not registered, inactive, or lacks the required role."),
    **VALIDATION_ERROR,
}

NOT_FOUND = {404: _error("The record does not exist or has been archived.")}
CONFLICT = {409: _error("The change conflicts with existing data.")}
