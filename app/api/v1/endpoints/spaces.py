import uuid

from fastapi import APIRouter

from app.api.dependencies import CurrentAdmin, DbSession, ListParams
from app.api.responses import CONFLICT, NOT_FOUND, PROTECTED
from app.models.enums import SpaceOperationalStatus, SpaceType
from app.schemas.common import Page
from app.schemas.space import SpaceCreate, SpaceRead, SpaceUpdate
from app.services.spaces import SpaceService

router = APIRouter(prefix="/spaces", tags=["Spaces"], responses=PROTECTED)


@router.get("", response_model=Page[SpaceRead], summary="List spaces")
def list_spaces(
    admin: CurrentAdmin,
    session: DbSession,
    query: ListParams,
    property_id: uuid.UUID | None = None,
    layout_id: uuid.UUID | None = None,
    space_type: SpaceType | None = None,
    operational_status: SpaceOperationalStatus | None = None,
    include_archived: bool = False,
) -> Page[SpaceRead]:
    """Search matches name and code. Sort by `name`, `code`, `space_type`,
    `capacity`, `created_at` or `updated_at`."""
    service = SpaceService(session, admin)
    items, total = service.list(
        query,
        {
            "property_id": property_id,
            "layout_id": layout_id,
            "space_type": space_type,
            "operational_status": operational_status,
        },
        include_archived=include_archived,
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
    """A layout, when given, must belong to the same property as the space."""
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
    """Archive rather than delete. Refused while the space is occupied or has open bookings."""
    SpaceService(session, admin).delete(space_id)
