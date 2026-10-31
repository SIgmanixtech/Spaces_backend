"""The public catalogue for the client app. No sign-in; published properties only.

Responses never name other customers, their bookings or their contracts.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import AppSettings, DbSession, ListParams, OptionalCustomer
from app.api.responses import NOT_FOUND, VALIDATION_ERROR, _error
from app.api.v1.endpoints.files import StorageFactory
from app.models.enums import BillingPeriod, SpaceType
from app.schemas.client import (
    Capabilities,
    FloorAvailability,
    FreeSpaces,
    PublicFloorPlan,
    PublicPropertyCard,
    PublicPropertyDetail,
    PublicSpaceAmenities,
    QuoteRead,
    QuoteRequest,
    SpaceAvailability,
)
from app.schemas.common import Page
from app.schemas.search import SearchRequest, SearchResponse
from app.services.amenities import AmenityService
from app.services.catalogue import CatalogueService
from app.services.search import SpaceSearch, customer_context

router = APIRouter(prefix="/public", tags=["Public catalogue"], responses=VALIDATION_ERROR)
BAD_RANGE = {400: _error("The times are missing a time zone, out of order or too far apart.")}


@router.get("/capabilities", response_model=Capabilities, summary="What the client app can use")
def capabilities() -> Capabilities:
    """Features the backend offers customers. Invoices, online payment and QR access are
    not implemented, and the client app shows them as unavailable."""
    return Capabilities(
        customer_registration=False,
        password_recovery=True,
        bookings=True,
        contracts=True,
        invoices=False,
        online_payments=False,
        qr_access=False,
    )


@router.get("/properties", response_model=Page[PublicPropertyCard], summary="Published properties")
def list_properties(
    session: DbSession,
    storage: StorageFactory,
    query: ListParams,
    city: Annotated[str | None, Query(max_length=120)] = None,
    property_type: Annotated[str | None, Query(max_length=50)] = None,
    space_type: SpaceType | None = None,
    min_capacity: Annotated[int | None, Query(ge=1)] = None,
    amenity: Annotated[str | None, Query(max_length=120)] = None,
    billing_period: BillingPeriod | None = None,
    max_price: Annotated[float | None, Query(ge=0)] = None,
    sort: Annotated[
        str,
        Query(description="`name`, `newest`, or `price` (lowest first; needs billing_period)."),
    ] = "name",
) -> Page[PublicPropertyCard]:
    """Search matches name, code, city, state and address. `max_price` compares the lowest
    one-period total for `billing_period` (any period when omitted)."""
    items, total = CatalogueService(session, storage).search(
        query,
        city=city,
        property_type=property_type,
        space_type=space_type,
        min_capacity=min_capacity,
        amenity=amenity,
        billing_period=billing_period,
        max_price=max_price,
        sort=sort,
    )
    return Page(items=items, total=total, page=query.page, page_size=query.page_size)


@router.get(
    "/properties/{property_id}",
    response_model=PublicPropertyDetail,
    responses=NOT_FOUND,
    summary="A published property",
)
def get_property(
    session: DbSession, storage: StorageFactory, property_id: uuid.UUID
) -> PublicPropertyDetail:
    return CatalogueService(session, storage).detail(property_id)


@router.get(
    "/properties/{property_id}/floors/{floor_id}",
    response_model=PublicFloorPlan,
    responses=NOT_FOUND,
    summary="A floor: layout geometry, spaces, prices and photos",
)
def floor_plan(
    session: DbSession, storage: StorageFactory, property_id: uuid.UUID, floor_id: uuid.UUID
) -> PublicFloorPlan:
    """Positions are the saved layout grid cells; a space without them is unplaced."""
    return CatalogueService(session, storage).floor_plan(property_id, floor_id)


@router.get(
    "/properties/{property_id}/floors/{floor_id}/availability",
    response_model=FloorAvailability,
    responses={**NOT_FOUND, **BAD_RANGE},
    summary="Every space's state on a floor for a time range",
)
def floor_availability(
    session: DbSession,
    property_id: uuid.UUID,
    floor_id: uuid.UUID,
    start_at: datetime,
    end_at: datetime,
) -> FloorAvailability:
    """From the allocation engine: a space is unavailable when it, a space containing it,
    or a space inside it is held for part of the range."""
    return CatalogueService(session).floor_availability(property_id, floor_id, start_at, end_at)


@router.post(
    "/space-search",
    response_model=SearchResponse,
    responses=VALIDATION_ERROR,
    summary="Search spaces with a sentence",
)
def space_search(
    body: SearchRequest,
    session: DbSession,
    settings: AppSettings,
    storage: StorageFactory,
    customer: OptionalCustomer,
) -> SearchResponse:
    """Public. The sentence is read into filters (Claude when an API key is configured, in
    the context of the previous search and, for a signed-in customer, their own recent
    bookings; otherwise the built-in reader). The results are real listed spaces with
    approved prices, and availability is checked by the allocation engine when a date is
    given. Searching reserves nothing."""
    return SpaceSearch(session, settings, storage).search(
        body.query,
        body.page,
        body.page_size,
        previous=(body.previous.query, body.previous.intent) if body.previous else None,
        customer=customer_context(session, customer) if customer else None,
    )


@router.get(
    "/free-spaces",
    response_model=FreeSpaces,
    responses=BAD_RANGE,
    summary="Every free space for a time range, by floor",
)
def free_spaces(
    session: DbSession,
    start_at: datetime,
    end_at: datetime,
    space_type: SpaceType | None = None,
    min_capacity: Annotated[int | None, Query(ge=1)] = None,
    city: str | None = None,
    search: Annotated[str | None, Query(max_length=100)] = None,
) -> FreeSpaces:
    """Only spaces free for the whole range are listed. A space that is booked, reserved,
    leased or closed, or that sits inside a held floor or portion, is not."""
    return CatalogueService(session).free_spaces(
        start_at,
        end_at,
        space_type=space_type,
        min_capacity=min_capacity,
        city=city,
        search=search,
    )


@router.get(
    "/spaces/{space_id}/availability",
    response_model=SpaceAvailability,
    responses={**NOT_FOUND, **BAD_RANGE},
    summary="One space's state for a time range",
)
def space_availability(
    session: DbSession, space_id: uuid.UUID, start_at: datetime, end_at: datetime
) -> SpaceAvailability:
    return CatalogueService(session).space_availability(space_id, start_at, end_at)


@router.get(
    "/spaces/{space_id}/amenities",
    response_model=PublicSpaceAmenities,
    responses=NOT_FOUND,
    summary="Amenities available at a space",
)
def space_amenities(session: DbSession, space_id: uuid.UUID) -> PublicSpaceAmenities:
    """Read-only. The space's own active amenities plus those of its parent spaces, floor,
    building and property, each once with where it comes from. Spaces of published
    properties only."""
    space, prop = CatalogueService(session).get_space(space_id)
    found = AmenityService(session).effective(space_id=space.id)
    return PublicSpaceAmenities(
        space_id=space.id,
        space_name=space.name,
        property_id=prop.id,
        amenities=[
            {
                "name": a.name,
                "description": a.description,
                "is_direct": a.is_direct,
                "sources": [
                    {
                        "level": src.level,
                        "place_name": src.place_name,
                        "opening_time": src.opening_time,
                        "closing_time": src.closing_time,
                    }
                    for src in a.sources
                ],
            }
            for a in found.amenities
        ],
    )


@router.post(
    "/quotes",
    response_model=QuoteRead,
    responses={**NOT_FOUND, 400: _error("The time range is not whole billing periods.")},
    summary="Price a space for a time range",
)
def quote(session: DbSession, body: QuoteRequest) -> QuoteRead:
    """The price that applies on the start day, for the whole billing periods the range
    covers (hourly 1 h, half-day 4 h, full-day 8 h, daily 24 h, weekly 7 days, monthly
    30 days), with its availability. An estimate: booking re-prices and re-checks."""
    return CatalogueService(session).quote(
        body.space_id, body.billing_period, body.start_at, body.end_at
    )
