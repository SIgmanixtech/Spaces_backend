from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import CurrentAdmin, DbSession
from app.api.responses import PROTECTED
from app.schemas.dashboard import ActivityEntry, DashboardSummary, PropertyAvailability
from app.services import dashboard

router = APIRouter(prefix="/property/dashboard", tags=["Property Dashboard"], responses=PROTECTED)


@router.get("/summary", response_model=DashboardSummary, summary="Portfolio metrics")
def get_summary(admin: CurrentAdmin, session: DbSession) -> DashboardSummary:
    """Property counts by derived occupancy status, space counts by current status,
    and the occupancy trend. `vacant + reserved + occupied + unavailable = total`.
    Computed from saved records on each request; an empty database returns zeros."""
    return dashboard.summary(session)


@router.get("/activity", response_model=list[ActivityEntry], summary="Recent activity")
def get_activity(
    admin: CurrentAdmin,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[ActivityEntry]:
    """The most recent changes to properties and everything under them, newest first."""
    return dashboard.recent_activity(session, limit)


@router.get(
    "/availability", response_model=list[PropertyAvailability], summary="Free spaces by property"
)
def get_availability(admin: CurrentAdmin, session: DbSession) -> list[PropertyAvailability]:
    """Spaces whose status is AVAILABLE right now, for each operational property
    that has at least one space."""
    return dashboard.availability(session)
