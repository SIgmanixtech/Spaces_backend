import uuid

from fastapi import APIRouter

from app.api.dependencies import CurrentAdmin, DbSession, ListParams
from app.api.responses import CONFLICT, NOT_FOUND, PROTECTED
from app.schemas.building import BuildingCreate, BuildingRead, BuildingUpdate
from app.schemas.common import Page
from app.services.buildings import BuildingService

router = APIRouter(prefix="/buildings", tags=["Buildings"], responses=PROTECTED)


@router.get("", response_model=Page[BuildingRead], summary="List buildings")
def list_buildings(
    admin: CurrentAdmin,
    session: DbSession,
    query: ListParams,
    property_id: uuid.UUID | None = None,
) -> Page[BuildingRead]:
    """Search matches name. Sort by `name` or `created_at`."""
    items, total = BuildingService(session, admin).list(query, {"property_id": property_id})
    return Page(items=items, total=total, page=query.page, page_size=query.page_size)


@router.post(
    "",
    response_model=BuildingRead,
    status_code=201,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Create a building",
)
def create_building(admin: CurrentAdmin, session: DbSession, body: BuildingCreate) -> BuildingRead:
    return BuildingService(session, admin).create(body.model_dump())


@router.get(
    "/{building_id}", response_model=BuildingRead, responses=NOT_FOUND, summary="Get a building"
)
def get_building(admin: CurrentAdmin, session: DbSession, building_id: uuid.UUID) -> BuildingRead:
    return BuildingService(session, admin).get(building_id)


@router.patch(
    "/{building_id}",
    response_model=BuildingRead,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Update a building",
)
def update_building(
    admin: CurrentAdmin, session: DbSession, building_id: uuid.UUID, body: BuildingUpdate
) -> BuildingRead:
    return BuildingService(session, admin).update(building_id, body.model_dump(exclude_unset=True))


@router.delete(
    "/{building_id}",
    status_code=204,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Delete a building",
)
def delete_building(admin: CurrentAdmin, session: DbSession, building_id: uuid.UUID) -> None:
    """Refused with 409 while floors, layouts, assets or files still reference it."""
    BuildingService(session, admin).delete(building_id)
