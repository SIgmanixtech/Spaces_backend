import uuid

from fastapi import APIRouter

from app.api.dependencies import CurrentAdmin, DbSession, ListParams
from app.api.responses import CONFLICT, NOT_FOUND, PROTECTED
from app.schemas.building import FloorCreate, FloorRead, FloorUpdate
from app.schemas.common import Page
from app.services.buildings import FloorService

router = APIRouter(prefix="/floors", tags=["Floors"], responses=PROTECTED)


@router.get("", response_model=Page[FloorRead], summary="List floors")
def list_floors(
    admin: CurrentAdmin,
    session: DbSession,
    query: ListParams,
    building_id: uuid.UUID | None = None,
) -> Page[FloorRead]:
    """Search matches name. Sort by `level_number`, `name` or `created_at`."""
    items, total = FloorService(session, admin).list(query, {"building_id": building_id})
    return Page(items=items, total=total, page=query.page, page_size=query.page_size)


@router.post(
    "",
    response_model=FloorRead,
    status_code=201,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Create a floor",
)
def create_floor(admin: CurrentAdmin, session: DbSession, body: FloorCreate) -> FloorRead:
    return FloorService(session, admin).create(body.model_dump())


@router.get("/{floor_id}", response_model=FloorRead, responses=NOT_FOUND, summary="Get a floor")
def get_floor(admin: CurrentAdmin, session: DbSession, floor_id: uuid.UUID) -> FloorRead:
    return FloorService(session, admin).get(floor_id)


@router.patch(
    "/{floor_id}",
    response_model=FloorRead,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Update a floor",
)
def update_floor(
    admin: CurrentAdmin, session: DbSession, floor_id: uuid.UUID, body: FloorUpdate
) -> FloorRead:
    return FloorService(session, admin).update(floor_id, body.model_dump(exclude_unset=True))


@router.delete(
    "/{floor_id}",
    status_code=204,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Delete a floor",
)
def delete_floor(admin: CurrentAdmin, session: DbSession, floor_id: uuid.UUID) -> None:
    """Refused with 409 while layouts or assets still reference it."""
    FloorService(session, admin).delete(floor_id)
