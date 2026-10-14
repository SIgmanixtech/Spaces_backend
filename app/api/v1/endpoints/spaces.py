import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import CurrentAdmin, DbSession, ListParams
from app.api.responses import CONFLICT, NOT_FOUND, PROTECTED
from app.models.enums import SpaceOperationalStatus, SpaceType
from app.schemas.common import Page
from app.schemas.space import SpaceCreate, SpaceMove, SpaceRead, SpaceUpdate
from app.services.spaces import SpaceService

router = APIRouter(prefix="/spaces", tags=["Spaces"], responses=PROTECTED)


@router.get("", response_model=Page[SpaceRead], summary="List spaces")
def list_spaces(
    admin: CurrentAdmin,
    session: DbSession,
    query: ListParams,
    property_id: uuid.UUID | None = None,
    building_id: uuid.UUID | None = None,
    floor_id: uuid.UUID | None = None,
    parent_space_id: uuid.UUID | None = None,
    layout_id: uuid.UUID | None = None,
    space_type: SpaceType | None = None,
    operational_status: SpaceOperationalStatus | None = None,
    is_rentable: bool | None = None,
    unassigned: Annotated[
        bool | None,
        Query(description="true: only spaces not on a floor yet. false: only spaces on a floor."),
    ] = None,
    include_structural: Annotated[
        bool,
        Query(description="Also return whole-floor (FLOOR) spaces and non-rentable spaces."),
    ] = False,
    include_archived: bool = False,
) -> Page[SpaceRead]:
    """Search matches name and code. Sort by `name`, `code`, `space_type`,
    `capacity`, `created_at` or `updated_at`. Whole-floor and non-rentable
    spaces are left out unless `include_structural=true`, so the default list
    is the spaces that can be let."""
    service = SpaceService(session, admin)
    items, total = service.list(
        query,
        {
            "property_id": property_id,
            "building_id": building_id,
            "floor_id": floor_id,
            "parent_space_id": parent_space_id,
            "layout_id": layout_id,
            "space_type": space_type,
            "operational_status": operational_status,
            "is_rentable": is_rentable,
        },
        include_archived=include_archived,
        conditions=service.listing_conditions(
            include_structural=include_structural, unassigned=unassigned
        ),
    )
    return Page(
        items=service.to_read(items), total=total, page=query.page, page_size=query.page_size
    )


@router.post(
    "",
    response_model=SpaceRead,
    status_code=201,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Create a space",
)
def create_space(admin: CurrentAdmin, session: DbSession, body: SpaceCreate) -> SpaceRead:
    """A layout, when given, must belong to the same property as the space.

    With `floor_id` the space goes on that floor, under `parent_space_id` or, by
    default, under the floor's whole-floor space, which must already exist
    (create it with `space_type: FLOOR`). Without either the space is unassigned.
    Returns `409` for a second whole-floor space on a floor."""
    service = SpaceService(session, admin)
    return service.to_read([service.create(body.model_dump())])[0]


@router.get("/{space_id}", response_model=SpaceRead, responses=NOT_FOUND, summary="Get a space")
def get_space(admin: CurrentAdmin, session: DbSession, space_id: uuid.UUID) -> SpaceRead:
    service = SpaceService(session, admin)
    return service.to_read([service.get(space_id)])[0]


@router.patch(
    "/{space_id}",
    response_model=SpaceRead,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Update a space",
)
def update_space(
    admin: CurrentAdmin, session: DbSession, space_id: uuid.UUID, body: SpaceUpdate
) -> SpaceRead:
    service = SpaceService(session, admin)
    return service.to_read([service.update(space_id, body.model_dump(exclude_unset=True))])[0]


@router.delete(
    "/{space_id}",
    status_code=204,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Archive a space",
)
def archive_space(admin: CurrentAdmin, session: DbSession, space_id: uuid.UUID) -> None:
    """Archive rather than delete. Refused while the space is occupied, has open
    bookings, or still contains spaces that are not archived."""
    SpaceService(session, admin).delete(space_id)


@router.post(
    "/{space_id}/move",
    response_model=SpaceRead,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Assign a space to a floor, or move it",
)
def move_space(
    admin: CurrentAdmin, session: DbSession, space_id: uuid.UUID, body: SpaceMove
) -> SpaceRead:
    """Moves the space together with everything inside it. The target must be in
    the same property. An unassigned space can always be assigned; a space that
    is already on a floor cannot be moved (`409`) while it, or a space inside it,
    has open bookings or a current or upcoming occupancy."""
    service = SpaceService(session, admin)
    return service.to_read([service.move(space_id, body.floor_id, body.parent_space_id)])[0]
