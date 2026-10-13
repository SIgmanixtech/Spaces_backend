"""Liveness and readiness probes. Public: they expose no business data."""

from fastapi import APIRouter

from app import __version__
from app.api.dependencies import AppSettings, DbSession
from app.core.errors import ServiceUnavailableError
from app.db.session import database_is_reachable
from app.schemas.common import ErrorResponse, HealthResponse, ReadinessResponse

router = APIRouter(prefix="/health", tags=["Health"])

SERVICE_NAME = "sigmanix-spaces-api"


@router.get("", response_model=HealthResponse, summary="Liveness probe")
def health(settings: AppSettings) -> HealthResponse:
    """Report that the API process is running. Does not touch the database."""
    return HealthResponse(
        status="ok",
        service=SERVICE_NAME,
        version=__version__,
        environment=settings.app_env,
    )


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    summary="Readiness probe",
    responses={503: {"model": ErrorResponse, "description": "The database is not reachable."}},
)
def readiness(session: DbSession) -> ReadinessResponse:
    """Report whether the API can reach its PostgreSQL database."""
    if not database_is_reachable(session):
        raise ServiceUnavailableError("The database is not reachable.")
    return ReadinessResponse(status="ready", checks={"database": "ok"})
