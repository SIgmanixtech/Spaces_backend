import uuid
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import AwareDatetime

from app.api.dependencies import CurrentAdmin, DbSession, ListParams
from app.api.responses import CONFLICT, NOT_FOUND, PROTECTED
from app.models.enums import BookingStatus
from app.schemas.booking import (
    AvailabilityRead,
    BookingCancel,
    BookingCreate,
    BookingRead,
    BookingUpdate,
)
from app.schemas.common import Page
from app.services.bookings import BookingService

router = APIRouter(prefix="/bookings", tags=["Bookings"], responses=PROTECTED)


@router.get("", response_model=Page[BookingRead], summary="List bookings")
def list_bookings(
    admin: CurrentAdmin,
    session: DbSession,
    query: ListParams,
    property_id: uuid.UUID | None = None,
    space_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    status: BookingStatus | None = None,
    window_start: Annotated[
        AwareDatetime | None, Query(description="Only bookings ending after this time.")
    ] = None,
    window_end: Annotated[
        AwareDatetime | None, Query(description="Only bookings starting before this time.")
    ] = None,
) -> Page[BookingRead]:
    """Newest start time first by default. Search matches reference, customer name
    and customer email. Sort by `start_at`, `end_at`, `created_at`, `reference` or
    `status`. Use `window_start` and `window_end` together to get every booking
    that overlaps a period."""
    items, total = BookingService(session, admin).list(
        query,
        property_id=property_id,
        space_id=space_id,
        tenant_id=tenant_id,
        status=status,
        window_start=window_start,
        window_end=window_end,
    )
    return Page(items=items, total=total, page=query.page, page_size=query.page_size)


@router.get(
    "/availability",
    response_model=AvailabilityRead,
    responses=NOT_FOUND,
    summary="Check whether a space is free",
)
def check_availability(
    admin: CurrentAdmin,
    session: DbSession,
    space_id: uuid.UUID,
    start_at: AwareDatetime,
    end_at: AwareDatetime,
    exclude_booking_id: Annotated[
        uuid.UUID | None,
        Query(description="Ignore this booking, e.g. when checking a reschedule."),
    ] = None,
) -> AvailabilityRead:
    """Computed from saved records at the moment of the call. It is advisory:
    creating the booking checks again inside its own transaction, so a slot
    reported free here can still be taken by someone else first."""
    return BookingService(session, admin).availability(
        space_id, start_at, end_at, exclude_booking_id
    )


@router.post(
    "",
    response_model=BookingRead,
    status_code=201,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Create a booking",
)
def create_booking(admin: CurrentAdmin, session: DbSession, body: BookingCreate) -> BookingRead:
    """Books one space for `[start_at, end_at)`. Returns `409` when the property
    or space is not open, the time overlaps another booking, or another tenant
    occupies the space. The response is sent only after the booking is committed."""
    return BookingService(session, admin).create(body.model_dump())


@router.get(
    "/{booking_id}", response_model=BookingRead, responses=NOT_FOUND, summary="Get a booking"
)
def get_booking(admin: CurrentAdmin, session: DbSession, booking_id: uuid.UUID) -> BookingRead:
    return BookingService(session, admin).get(booking_id)


@router.patch(
    "/{booking_id}",
    response_model=BookingRead,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Reschedule or edit a booking",
)
def update_booking(
    admin: CurrentAdmin, session: DbSession, booking_id: uuid.UUID, body: BookingUpdate
) -> BookingRead:
    """Only PENDING and CONFIRMED bookings can be changed. New times are checked
    for conflicts exactly as on creation. To change status use the confirm,
    complete and cancel actions."""
    return BookingService(session, admin).update(booking_id, body.model_dump(exclude_unset=True))


@router.post(
    "/{booking_id}/confirm",
    response_model=BookingRead,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Confirm a pending booking",
)
def confirm_booking(admin: CurrentAdmin, session: DbSession, booking_id: uuid.UUID) -> BookingRead:
    return BookingService(session, admin).confirm(booking_id)


@router.post(
    "/{booking_id}/complete",
    response_model=BookingRead,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Mark a confirmed booking as completed",
)
def complete_booking(admin: CurrentAdmin, session: DbSession, booking_id: uuid.UUID) -> BookingRead:
    """Allowed once the booking has started."""
    return BookingService(session, admin).complete(booking_id)


@router.post(
    "/{booking_id}/cancel",
    response_model=BookingRead,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Cancel a booking",
)
def cancel_booking(
    admin: CurrentAdmin,
    session: DbSession,
    booking_id: uuid.UUID,
    body: BookingCancel | None = None,
) -> BookingRead:
    """Releases the time range immediately. The booking is kept for history."""
    return BookingService(session, admin).cancel(booking_id, body.reason if body else None)


@router.delete(
    "/{booking_id}",
    status_code=204,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Cancel a booking (no content)",
)
def delete_booking(admin: CurrentAdmin, session: DbSession, booking_id: uuid.UUID) -> None:
    """Bookings are never deleted. This cancels the booking, like the cancel action."""
    BookingService(session, admin).cancel(booking_id)
