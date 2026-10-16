"""Images and documents of a property and its buildings, layouts, spaces and assets."""

import uuid
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse

from app.api.dependencies import AppSettings, CurrentAdmin, DbSession, ListParams
from app.api.responses import NOT_FOUND, PROTECTED, _error
from app.core.config import Settings
from app.core.errors import NotFoundError
from app.models.enums import FileKind
from app.schemas.common import Page
from app.schemas.file import FileRead, FileUpdate
from app.services.files import MAX_COVERS, FileService
from app.storage import LocalStorage, ObjectStorage, StorageError, SupabaseStorage, unavailable

router = APIRouter(prefix="/files", tags=["Files"])

UPLOAD_ERRORS = {
    409: _error("The owner already holds the most files allowed."),
    413: _error("The file is larger than the upload limit."),
    415: _error(
        "The file is not a complete JPG, PNG or WebP image (or a PDF, for plans and "
        "documents), or it is too large in pixels."
    ),
    503: _error("File storage is not configured or could not be reached. Nothing was stored."),
}


@lru_cache
def _supabase_storage(url: str, key: str, bucket: str) -> SupabaseStorage:
    return SupabaseStorage(url, key, bucket)


def storage_from_settings(settings: Settings, base_url: str) -> ObjectStorage:
    if settings.storage_backend == "local":
        return LocalStorage(settings.local_storage_dir, base_url)
    if settings.supabase_secret_key is None:
        raise unavailable()
    return _supabase_storage(
        str(settings.supabase_url),
        settings.supabase_secret_key.get_secret_value(),
        settings.property_storage_bucket,
    )


def get_storage(request: Request, settings: AppSettings) -> ObjectStorage:
    return storage_from_settings(settings, str(request.base_url))


Storage = Annotated[ObjectStorage, Depends(get_storage)]


@router.get(
    "/local/{object_path:path}",
    include_in_schema=False,
    responses=NOT_FOUND,
)
def local_file(object_path: str, expires: int, signature: str, storage: Storage) -> FileResponse:
    """Serves a file from local storage to the holder of a valid signed link.
    Answers only when local storage is in use, which is for development and tests."""
    if not isinstance(storage, LocalStorage) or not LocalStorage.signature_valid(
        object_path, expires, signature
    ):
        raise NotFoundError("File not found.")
    try:
        target = storage.resolve(object_path)
    except StorageError:
        raise NotFoundError("File not found.") from None
    if not target.is_file():
        raise NotFoundError("File not found.")
    return FileResponse(
        target,
        headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"},
    )


@router.post(
    "",
    response_model=FileRead,
    status_code=201,
    responses={**PROTECTED, **NOT_FOUND, **UPLOAD_ERRORS},
    summary="Upload an image or document",
)
def upload_file(
    admin: CurrentAdmin,
    session: DbSession,
    settings: AppSettings,
    storage: Storage,
    file: UploadFile,
    property_id: Annotated[uuid.UUID, Form()],
    kind: Annotated[FileKind, Form()],
    building_id: Annotated[uuid.UUID | None, Form()] = None,
    layout_id: Annotated[uuid.UUID | None, Form()] = None,
    space_id: Annotated[uuid.UUID | None, Form()] = None,
    asset_id: Annotated[uuid.UUID | None, Form()] = None,
    caption: Annotated[str | None, Form(max_length=300)] = None,
    description: Annotated[str | None, Form(max_length=2000)] = None,
    category: Annotated[str | None, Form(max_length=80)] = None,
    sort_order: Annotated[int | None, Form(ge=0, le=100_000)] = None,
    is_primary: Annotated[bool | None, Form()] = None,
) -> FileRead:
    """Multipart form. The file belongs to the property and, optionally, to one of its
    buildings, layouts, spaces or assets (an asset's images use kind ASSET). JPG, PNG
    or WebP; PDF also for FLOOR_PLAN and DOCUMENT. The type is read from the content,
    which must be complete. `is_primary` makes it the owner's cover (or a layout's main
    plan), replacing the previous one; it defaults to true for MAIN and false otherwise.
    The response carries a signed link that expires after ten minutes; list or get the
    file again for a fresh one."""
    max_bytes = settings.max_upload_mb * 1024 * 1024
    data = file.file.read(max_bytes + 1)
    return FileService(session, storage, admin).upload(
        property_id=property_id,
        kind=kind,
        data=data,
        original_name=file.filename,
        max_bytes=max_bytes,
        max_per_owner=settings.max_files_per_owner,
        building_id=building_id,
        layout_id=layout_id,
        space_id=space_id,
        asset_id=asset_id,
        caption=caption,
        description=description,
        category=category,
        sort_order=sort_order,
        is_primary=is_primary,
    )


@router.get(
    "",
    response_model=Page[FileRead],
    responses={**PROTECTED, 503: UPLOAD_ERRORS[503]},
    summary="List files",
)
def list_files(
    admin: CurrentAdmin,
    session: DbSession,
    storage: Storage,
    query: ListParams,
    property_id: uuid.UUID | None = None,
    building_id: uuid.UUID | None = None,
    layout_id: uuid.UUID | None = None,
    space_id: uuid.UUID | None = None,
    asset_id: uuid.UUID | None = None,
    kind: Annotated[FileKind | None, Query()] = None,
    is_primary: bool | None = None,
    direct: Annotated[
        bool,
        Query(
            description="Only files of exactly the owner named: with property_id alone, the "
            "property's own files and not those of its buildings, layouts, spaces or assets."
        ),
    ] = False,
) -> Page[FileRead]:
    """Ordered by sort_order, then oldest first. Each item has a fresh signed link."""
    items, total = FileService(session, storage, admin).list(
        query,
        property_id=property_id,
        building_id=building_id,
        layout_id=layout_id,
        space_id=space_id,
        asset_id=asset_id,
        kind=kind,
        is_primary=is_primary,
        direct=direct,
    )
    return Page(items=items, total=total, page=query.page, page_size=query.page_size)


@router.get(
    "/covers",
    response_model=list[FileRead],
    responses={**PROTECTED, 503: UPLOAD_ERRORS[503]},
    summary="Cover images of several properties",
)
def property_covers(
    admin: CurrentAdmin,
    session: DbSession,
    storage: Storage,
    property_id: Annotated[list[uuid.UUID], Query(max_length=MAX_COVERS)],
) -> list[FileRead]:
    """The primary property-level image of each property named (repeat property_id, at
    most 100). Properties without one are left out. Not paginated."""
    return FileService(session, storage, admin).covers(property_id)


@router.get(
    "/{file_id}",
    response_model=FileRead,
    responses={**PROTECTED, **NOT_FOUND, 503: UPLOAD_ERRORS[503]},
    summary="Get a file with a fresh link",
)
def get_file(
    admin: CurrentAdmin, session: DbSession, storage: Storage, file_id: uuid.UUID
) -> FileRead:
    return FileService(session, storage, admin).get(file_id)


@router.patch(
    "/{file_id}",
    response_model=FileRead,
    responses={**PROTECTED, **NOT_FOUND, 503: UPLOAD_ERRORS[503]},
    summary="Change a file's caption, category, order or primary flag",
)
def update_file(
    admin: CurrentAdmin,
    session: DbSession,
    storage: Storage,
    file_id: uuid.UUID,
    body: FileUpdate,
) -> FileRead:
    """Display details only; to replace the picture, upload a new one and delete this."""
    return FileService(session, storage, admin).update(file_id, body.model_dump(exclude_unset=True))


@router.delete(
    "/{file_id}",
    status_code=204,
    responses={**PROTECTED, **NOT_FOUND, 503: UPLOAD_ERRORS[503]},
    summary="Delete a file",
)
def delete_file(
    admin: CurrentAdmin, session: DbSession, storage: Storage, file_id: uuid.UUID
) -> None:
    """Removes the record, then the stored bytes."""
    FileService(session, storage, admin).delete(file_id)
