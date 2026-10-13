"""Application errors and the centralized handlers that render them.

Every error response has the same shape (see ``app.schemas.common.ErrorResponse``).
``detail`` is always a human-readable string, which is what the frontend displays.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import request_id_ctx

logger = logging.getLogger(__name__)

_HTTP_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "validation_error",
    503: "service_unavailable",
}


class AppError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, detail: str, *, errors: list[dict[str, str]] | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.errors = errors


class BadRequestError(AppError):
    status_code = 400
    code = "bad_request"


class UnauthorizedError(AppError):
    status_code = 401
    code = "unauthorized"


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class ServiceUnavailableError(AppError):
    status_code = 503
    code = "service_unavailable"


def error_response(
    status_code: int,
    code: str,
    detail: str,
    errors: list[dict[str, str]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, object] = {
        "detail": detail,
        "code": code,
        "request_id": request_id_ctx.get(),
    }
    if errors:
        body["errors"] = errors
    return JSONResponse(status_code=status_code, content=body, headers=headers)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None
        return error_response(exc.status_code, exc.code, exc.detail, exc.errors, headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Field locations and messages only: submitted values are not echoed back.
        errors = [
            {
                "field": ".".join(str(part) for part in error["loc"]),
                "message": error["msg"],
            }
            for error in exc.errors()
        ]
        return error_response(422, "validation_error", "Request validation failed.", errors)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed."
        return error_response(
            exc.status_code,
            _HTTP_CODES.get(exc.status_code, "error"),
            detail,
            headers=dict(exc.headers) if exc.headers else None,
        )
