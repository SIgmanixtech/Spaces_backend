import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import CurrentAdmin, DbSession, ListParams
from app.api.responses import CONFLICT, NOT_FOUND, PROTECTED
from app.models.enums import OccupancyStatus, PropertyStatus
from app.schemas.common import Page
from app.schemas.property import PropertyCreate, PropertyRead, PropertyUpdate
from app.services.properties import PropertyService

router = APIRouter(prefix="/properties", tags=["Properties"], responses=PROTECTED)


@router.get("", response_model=Page[PropertyRead], summary="List properties")
def list_properties(
    admin: CurrentAdmin,
    session: DbSession,
    query: ListParams,
    status: Annotated[PropertyStatus | None, Query(description="Operational status.")] = None,
    occupancy_status: Annotated[
        OccupancyStatus | None, Query(description="Derived occupancy status.")
    ] = None,
    city: Annotated[str | None, Query(max_length=120, description="Contains, any case.")] = None,
    state: Annotated[str | None, Query(max_length=120, description="Contains, any case.")] = None,
    tenant: Annotated[
        str | None,
        Query(max_length=200, description="Name of a tenant currently occupying the property."),
    ] = None,
    property_type: str | None = None,
    category: str | None = None,
    include_archived: bool = False,
) -> Page[PropertyRead]:
    """Search matches name, address, city, state and postal code. Sort by `name`,
    `city`, `state`, `capacity`, `status`, `created_at` or `updated_at`."""
    items, total = PropertyService(session, admin).search(
        query,
        {"status": status, "property_type": property_type, "category": category},
        city=city,
        state=state,
        tenant=tenant,
        occupancy_status=occupancy_status,
        include_archived=include_archived,
    )
    return Page(items=items, total=total, page=query.page, page_size=query.page_size)


@router.post("", response_model=PropertyRead, status_code=201, summary="Create a property")
def create_property(admin: CurrentAdmin, session: DbSession, body: PropertyCreate) -> PropertyRead:
    service = PropertyService(session, admin)
    return service.to_read([service.create(body.model_dump())])[0]


@router.get(
    "/{property_id}", response_model=PropertyRead, responses=NOT_FOUND, summary="Get a property"
)
def get_property(admin: CurrentAdmin, session: DbSession, property_id: uuid.UUID) -> PropertyRead:
    service = PropertyService(session, admin)
    return service.to_read([service.get(property_id)])[0]


@router.patch(
    "/{property_id}",
    response_model=PropertyRead,
    responses=NOT_FOUND,
    summary="Update a property",
)
def update_property(
    admin: CurrentAdmin, session: DbSession, property_id: uuid.UUID, body: PropertyUpdate
) -> PropertyRead:
    service = PropertyService(session, admin)
    record = service.update(property_id, body.model_dump(exclude_unset=True))
    return service.to_read([record])[0]


@router.delete(
    "/{property_id}",
    status_code=204,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Archive a property",
)
def archive_property(admin: CurrentAdmin, session: DbSession, property_id: uuid.UUID) -> None:
    """Archive rather than delete. Refused while the property has a current
    tenant occupancy or open bookings."""
    PropertyService(session, admin).delete(property_id)
