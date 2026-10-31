import uuid

from fastapi import APIRouter

from app.api.dependencies import CurrentAdmin, DbSession, ListParams
from app.api.responses import CONFLICT, NOT_FOUND, PROTECTED, _error
from app.models.enums import AmenityLevel, AmenityStatus
from app.schemas.amenity import AmenityCreate, AmenityRead, AmenityUpdate, EffectiveAmenities
from app.schemas.common import Page
from app.services.amenities import AmenityService

router = APIRouter(prefix="/amenities", tags=["Amenities"], responses=PROTECTED)
BAD_REQUEST = {400: _error("The place is not part of the property, or cannot take amenities.")}


@router.get("", response_model=Page[AmenityRead], summary="List amenity assignments")
def list_amenities(
    admin: CurrentAdmin,
    session: DbSession,
    query: ListParams,
    property_id: uuid.UUID | None = None,
    status: AmenityStatus | None = None,
    level: AmenityLevel | None = None,
    building_id: uuid.UUID | None = None,
    floor_id: uuid.UUID | None = None,
    space_id: uuid.UUID | None = None,
) -> Page[AmenityRead]:
    """One item per assignment, at any level unless `level` is given (PROPERTY, BUILDING,
    FLOOR or SPACE). `building_id` also matches that building's floor assignments. Search
    matches name and description. Sort by `name`, `status` or `created_at`."""
    items, total = AmenityService(session, admin).list(
        query,
        property_id=property_id,
        status=status,
        level=level,
        building_id=building_id,
        floor_id=floor_id,
        space_id=space_id,
    )
    return Page(items=items, total=total, page=query.page, page_size=query.page_size)


@router.get(
    "/effective",
    response_model=EffectiveAmenities,
    responses={**NOT_FOUND, **BAD_REQUEST},
    summary="Amenities that apply at a property, building, floor or space",
)
def effective_amenities(
    admin: CurrentAdmin,
    session: DbSession,
    property_id: uuid.UUID | None = None,
    building_id: uuid.UUID | None = None,
    floor_id: uuid.UUID | None = None,
    space_id: uuid.UUID | None = None,
) -> EffectiveAmenities:
    """Give exactly one place. Its own active amenities and those inherited from its parent
    spaces, floor, building and property, each once, with every source (nearest first)."""
    return AmenityService(session, admin).effective(
        property_id=property_id, building_id=building_id, floor_id=floor_id, space_id=space_id
    )


@router.post(
    "",
    response_model=AmenityRead,
    status_code=201,
    responses={**NOT_FOUND, **CONFLICT, **BAD_REQUEST},
    summary="Add an amenity to a property, building, floor or space",
)
def create_amenity(admin: CurrentAdmin, session: DbSession, body: AmenityCreate) -> AmenityRead:
    """Reuses the shared amenity definition with the same name (case-insensitive)
    or creates one. Optionally at one building, floor or space of the property, which must
    belong to it. Each place can have each amenity only once; the same amenity may be at
    several levels."""
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
