import uuid

from fastapi import APIRouter

from app.api.dependencies import CurrentAdmin, DbSession, ListParams
from app.api.responses import CONFLICT, NOT_FOUND, PROTECTED
from app.models.enums import AmenityStatus
from app.schemas.amenity import AmenityCreate, AmenityRead, AmenityUpdate
from app.schemas.common import Page
from app.services.amenities import AmenityService

router = APIRouter(prefix="/amenities", tags=["Amenities"], responses=PROTECTED)


@router.get("", response_model=Page[AmenityRead], summary="List amenities at properties")
def list_amenities(
    admin: CurrentAdmin,
    session: DbSession,
    query: ListParams,
    property_id: uuid.UUID | None = None,
    status: AmenityStatus | None = None,
) -> Page[AmenityRead]:
    """One item per property-amenity assignment. Search matches name and
    description. Sort by `name`, `status` or `created_at`."""
    items, total = AmenityService(session, admin).list(
        query, property_id=property_id, status=status
    )
    return Page(items=items, total=total, page=query.page, page_size=query.page_size)


@router.post(
    "",
    response_model=AmenityRead,
    status_code=201,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Add an amenity to a property",
)
def create_amenity(admin: CurrentAdmin, session: DbSession, body: AmenityCreate) -> AmenityRead:
    """Reuses the shared amenity definition with the same name (case-insensitive)
    or creates one. A property can have each amenity only once."""
    return AmenityService(session, admin).create(body.model_dump())


@router.get(
    "/{amenity_id}", response_model=AmenityRead, responses=NOT_FOUND, summary="Get an amenity"
)
def get_amenity(admin: CurrentAdmin, session: DbSession, amenity_id: uuid.UUID) -> AmenityRead:
    return AmenityService(session, admin).get(amenity_id)


@router.patch(
    "/{amenity_id}",
    response_model=AmenityRead,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Update an amenity at a property",
)
def update_amenity(
    admin: CurrentAdmin, session: DbSession, amenity_id: uuid.UUID, body: AmenityUpdate
) -> AmenityRead:
    """Hours, status and notes apply to this property only. Changing `description`
    changes the shared definition, so it shows at every property with this amenity."""
    return AmenityService(session, admin).update(amenity_id, body.model_dump(exclude_unset=True))


@router.delete(
    "/{amenity_id}",
    status_code=204,
    responses=NOT_FOUND,
    summary="Remove an amenity from a property",
)
def delete_amenity(admin: CurrentAdmin, session: DbSession, amenity_id: uuid.UUID) -> None:
    AmenityService(session, admin).delete(amenity_id)
