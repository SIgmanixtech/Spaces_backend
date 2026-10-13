import uuid

from fastapi import APIRouter

from app.api.dependencies import CurrentAdmin, DbSession, ListParams
from app.api.responses import CONFLICT, NOT_FOUND, PROTECTED
from app.models.enums import AssetStatus
from app.schemas.asset import AssetCreate, AssetRead, AssetUpdate
from app.schemas.common import Page
from app.services.assets import AssetService

router = APIRouter(prefix="/assets", tags=["Assets"], responses=PROTECTED)


@router.get("", response_model=Page[AssetRead], summary="List physical assets")
def list_assets(
    admin: CurrentAdmin,
    session: DbSession,
    query: ListParams,
    property_id: uuid.UUID | None = None,
    status: AssetStatus | None = None,
    category: str | None = None,
) -> Page[AssetRead]:
    """Search matches name, category, asset tag and location. Sort by `name`,
    `category`, `status`, `purchase_date`, `created_at` or `updated_at`."""
    service = AssetService(session, admin)
    items, total = service.list(
        query, {"property_id": property_id, "status": status, "category": category}
    )
    return Page(
        items=service.to_read(items), total=total, page=query.page, page_size=query.page_size
    )


@router.post(
    "",
    response_model=AssetRead,
    status_code=201,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Create an asset",
)
def create_asset(admin: CurrentAdmin, session: DbSession, body: AssetCreate) -> AssetRead:
    """Any building, floor or space given must belong to the asset's property."""
    service = AssetService(session, admin)
    return service.to_read([service.create(body.model_dump())])[0]


@router.get("/{asset_id}", response_model=AssetRead, responses=NOT_FOUND, summary="Get an asset")
def get_asset(admin: CurrentAdmin, session: DbSession, asset_id: uuid.UUID) -> AssetRead:
    service = AssetService(session, admin)
    return service.to_read([service.get(asset_id)])[0]


@router.patch(
    "/{asset_id}",
    response_model=AssetRead,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Update an asset",
)
def update_asset(
    admin: CurrentAdmin, session: DbSession, asset_id: uuid.UUID, body: AssetUpdate
) -> AssetRead:
    service = AssetService(session, admin)
    return service.to_read([service.update(asset_id, body.model_dump(exclude_unset=True))])[0]


@router.delete("/{asset_id}", status_code=204, responses=NOT_FOUND, summary="Delete an asset")
def delete_asset(admin: CurrentAdmin, session: DbSession, asset_id: uuid.UUID) -> None:
    """Permanently deletes the record. To keep history, set status to RETIRED instead."""
    AssetService(session, admin).delete(asset_id)
