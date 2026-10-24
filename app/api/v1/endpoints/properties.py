import uuid
from typing import Annotated

from fastapi import APIRouter, File, Form, Query, UploadFile
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from app.api.dependencies import AppSettings, CurrentAdmin, DbSession, ListParams
from app.api.responses import CONFLICT, NOT_FOUND, PROTECTED, _error
from app.api.v1.endpoints.files import StorageFactory
from app.models.enums import OccupancyStatus, PropertyStatus
from app.schemas.common import Page, RemovalRead
from app.schemas.property import PropertyCreate, PropertyRead, PropertyUpdate
from app.schemas.setup import PropertySetup, PropertySetupResult
from app.services.properties import PropertyService
from app.services.setup import PropertySetupService, UploadedFile

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


SETUP_ERRORS = {
    400: _error(
        "A ref is missing, repeated or of the wrong kind, or a record breaks a rule. "
        "`errors` names each one. Nothing was saved."
    ),
    409: _error("A record conflicts with existing data. Nothing was saved."),
    413: _error("An image is larger than the upload limit. Nothing was saved."),
    415: _error("An image is not a complete JPG, PNG, WebP (or PDF plan). Nothing was saved."),
    503: _error("File storage could not be reached. Nothing was saved."),
}


@router.post(
    "/setup",
    response_model=PropertySetupResult,
    status_code=201,
    responses=SETUP_ERRORS,
    summary="Create a property with everything in it, all or nothing",
)
def setup_property(
    admin: CurrentAdmin,
    session: DbSession,
    settings: AppSettings,
    storage: StorageFactory,
    setup: Annotated[
        str,
        Form(
            description="JSON matching PropertySetup: the property, its buildings, floors, "
            "layouts, spaces, assets, amenities, pricing rules and image descriptions. "
            "Records refer to each other by `ref`."
        ),
    ],
    files: Annotated[
        list[UploadFile] | None,
        File(description="Image and floor-plan files, referred to by position (file_index)."),
    ] = None,
) -> PropertySetupResult:
    """Multipart form: a `setup` JSON field and zero or more `files`. Everything is created
    in one database transaction: if any record is refused, nothing is saved and the
    response names the record. Image bytes are stored first and deleted again if the
    transaction fails. The response maps every ref to the id it was saved as."""
    try:
        body = PropertySetup.model_validate_json(setup)
    except ValidationError as exc:
        raise RequestValidationError(
            [{**error, "loc": ("setup", *error["loc"])} for error in exc.errors()]
        ) from None
    max_bytes = settings.max_upload_mb * 1024 * 1024
    uploads = [UploadedFile(name=f.filename, data=f.file.read(max_bytes + 1)) for f in files or []]

    return PropertySetupService(
        session,
        admin,
        storage,
        max_upload_bytes=max_bytes,
        max_files_per_owner=settings.max_files_per_owner,
    ).create(body, uploads)


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
    response_model=RemovalRead,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Delete a property (archived if it has history)",
)
def archive_property(
    admin: CurrentAdmin, session: DbSession, property_id: uuid.UUID
) -> RemovalRead:
    """Deleted for good when nothing refers to it; archived (hidden, kept for history)
    when it has bookings, leases, prices, files or other records. Refused while it is in
    current use."""
    return RemovalRead.of("Property", PropertyService(session, admin).delete(property_id))
