import uuid

from fastapi import APIRouter

from app.api.dependencies import CurrentAdmin, DbSession, ListParams
from app.api.responses import CONFLICT, NOT_FOUND, PROTECTED
from app.models.enums import LayoutStatus
from app.models.layout import Layout
from app.schemas.common import Page
from app.schemas.layout import LayoutCreate, LayoutRead, LayoutUpdate
from app.schemas.space import SpaceRead
from app.services.layouts import LayoutService
from app.services.spaces import SpaceService

router = APIRouter(prefix="/layouts", tags=["Layouts"], responses=PROTECTED)


@router.get("", response_model=Page[LayoutRead], summary="List layouts")
def list_layouts(
    admin: CurrentAdmin,
    session: DbSession,
    query: ListParams,
    property_id: uuid.UUID | None = None,
    building_id: uuid.UUID | None = None,
    floor_id: uuid.UUID | None = None,
    status: LayoutStatus | None = None,
) -> Page[LayoutRead]:
    """Archived layouts are hidden unless `status=ARCHIVED` is requested.
    Search matches name and description. Sort by `name`, `status`, `created_at`
    or `updated_at`."""
    service = LayoutService(session, admin)
    items, total = service.list(
        query,
        {
            "property_id": property_id,
            "building_id": building_id,
            "floor_id": floor_id,
            "status": status,
        },
        conditions=[] if status else [Layout.status != LayoutStatus.ARCHIVED],
    )
    return Page(
        items=service.to_read(items), total=total, page=query.page, page_size=query.page_size
    )


@router.post(
    "",
    response_model=LayoutRead,
    status_code=201,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Create a layout",
)
def create_layout(admin: CurrentAdmin, session: DbSession, body: LayoutCreate) -> LayoutRead:
    """A building must belong to the layout's property, and a floor to that building."""
    service = LayoutService(session, admin)
    return service.to_read([service.create(body.model_dump())])[0]


@router.get("/{layout_id}", response_model=LayoutRead, responses=NOT_FOUND, summary="Get a layout")
def get_layout(admin: CurrentAdmin, session: DbSession, layout_id: uuid.UUID) -> LayoutRead:
    service = LayoutService(session, admin)
    return service.to_read([service.get(layout_id)])[0]


@router.patch(
    "/{layout_id}",
    response_model=LayoutRead,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Update a layout",
)
def update_layout(
    admin: CurrentAdmin, session: DbSession, layout_id: uuid.UUID, body: LayoutUpdate
) -> LayoutRead:
    service = LayoutService(session, admin)
    return service.to_read([service.update(layout_id, body.model_dump(exclude_unset=True))])[0]


@router.delete("/{layout_id}", status_code=204, responses=NOT_FOUND, summary="Archive a layout")
def archive_layout(admin: CurrentAdmin, session: DbSession, layout_id: uuid.UUID) -> None:
    """Sets the layout's status to ARCHIVED. Its spaces are kept."""
    LayoutService(session, admin).delete(layout_id)


@router.get(
    "/{layout_id}/spaces",
    response_model=list[SpaceRead],
    responses=NOT_FOUND,
    summary="List the spaces on a layout",
)
def list_layout_spaces(
    admin: CurrentAdmin, session: DbSession, layout_id: uuid.UUID
) -> list[SpaceRead]:
    """All non-archived spaces on the layout, each with its current derived status."""
    LayoutService(session, admin).get(layout_id)
    service = SpaceService(session, admin)
    return service.to_read(service.list_for_layout(layout_id))
