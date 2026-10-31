import uuid
from datetime import date

from fastapi import APIRouter

from app.api.dependencies import CurrentAdmin, DbSession, ListParams
from app.api.responses import PROTECTED, _error
from app.schemas.activity import ActivityItem, ActivityOptions
from app.schemas.common import Page
from app.services.activity import ActivityHistory

router = APIRouter(prefix="/activity", tags=["Activity"], responses=PROTECTED)


@router.get(
    "",
    response_model=Page[ActivityItem],
    responses={400: _error("date_to is before date_from.")},
    summary="Activity history",
)
def list_activity(
    admin: CurrentAdmin,
    session: DbSession,
    query: ListParams,
    property_id: uuid.UUID | None = None,
    entity_type: str | None = None,
    action: str | None = None,
    actor_id: uuid.UUID | None = None,
    entity_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> Page[ActivityItem]:
    """Stored activity entries, newest first (ties broken by id). `search` matches the
    message. Dates are calendar days in the business time zone, both included.

    `message` is exactly as recorded. `record` and `current_context` describe the records
    as they are now. `changes` is filled only for messages that recorded before/after
    values; most entries did not."""
    items, total = ActivityHistory(session).page(
        query,
        property_id=property_id,
        entity_type=entity_type,
        action=action,
        actor_id=actor_id,
        entity_id=entity_id,
        date_from=date_from,
        date_to=date_to,
    )
    return Page(items=items, total=total, page=query.page, page_size=query.page_size)


@router.get("/options", response_model=ActivityOptions, summary="Activity filter choices")
def activity_options(admin: CurrentAdmin, session: DbSession) -> ActivityOptions:
    """Entity types and actions that occur in stored entries, and the users and properties
    named by them that still exist."""
    return ActivityHistory(session).options()
