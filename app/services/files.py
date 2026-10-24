"""Uploaded images and documents: metadata in PostgreSQL, bytes in object storage.

A file belongs to a property and, optionally, to one of its buildings, layouts,
spaces or assets. The type is decided from the file's content, never from its
name or the browser's claim, and the content must be a structurally complete
JPEG, PNG, WebP or PDF. Links to the bytes are signed and expire after minutes.
Each owner may have one primary file: its cover image, or a layout's main plan.
"""

import contextlib
import re
import struct
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ColumnElement, and_, func, select, update
from sqlalchemy.orm import Session

from app.core.errors import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    PayloadTooLargeError,
    ServiceUnavailableError,
    UnsupportedMediaTypeError,
)
from app.models.asset import Asset
from app.models.building import Building
from app.models.enums import FileKind
from app.models.file import StoredFile
from app.models.layout import Layout
from app.models.property import Property
from app.models.space import Space
from app.models.user import AppUser
from app.repositories.base import paginate_rows
from app.schemas.common import ListQuery
from app.schemas.file import FileRead
from app.services.base import commit, flush, log_activity, require
from app.storage import ObjectStorage, StorageError

URL_LIFETIME_SECONDS = 600
# Larger images are refused: decoding one in a browser can exhaust its memory.
MAX_PIXELS = 80_000_000
# The most covers one request may ask for: a page of the property list.
MAX_COVERS = 100

IMAGE_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
PDF = "application/pdf"
# Kinds that may also be a PDF: a floor plan or a document often is one.
PDF_KINDS = {FileKind.FLOOR_PLAN, FileKind.DOCUMENT}
OWNER_COLUMNS = ("building_id", "layout_id", "space_id", "asset_id")
# JPEG start-of-frame markers, which carry the image size.
_JPEG_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


@dataclass(frozen=True)
class Content:
    content_type: str
    width: int | None = None
    height: int | None = None


def sniff(data: bytes) -> str | None:
    """The type the content claims to be, from its first bytes."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"%PDF-"):
        return PDF
    return None


def _png_size(data: bytes) -> tuple[int, int] | None:
    # The first chunk must be IHDR (13 bytes); the last must be IEND.
    if data[8:16] != b"\x00\x00\x00\x0dIHDR" or len(data) < 33 or b"IEND" not in data[-64:]:
        return None
    return struct.unpack(">II", data[16:24])


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    if b"\xff\xd9" not in data[-1024:]:
        return None  # truncated: no end-of-image marker
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if marker in (0xD9, 0xDA):  # end of image, or scan data before any frame header
            return None
        (length,) = struct.unpack(">H", data[i + 2 : i + 4])
        if length < 2:
            return None
        if marker in _JPEG_SOF:
            if i + 9 > len(data):
                return None
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            return width, height
        i += 2 + length
    return None


def _webp_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 25:
        return None
    (riff_size,) = struct.unpack("<I", data[4:8])
    if riff_size + 8 > len(data):
        return None  # truncated
    chunk = data[12:16]
    if chunk in (b"VP8 ", b"VP8X") and len(data) < 30:
        return None
    if chunk == b"VP8 ":
        if data[23:26] != b"\x9d\x01\x2a":
            return None
        width, height = struct.unpack("<HH", data[26:30])
        return width & 0x3FFF, height & 0x3FFF
    if chunk == b"VP8L":
        if data[20] != 0x2F:
            return None
        (bits,) = struct.unpack("<I", data[21:25])
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8X":
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    return None


def inspect_content(data: bytes) -> Content | None:
    """The real type and pixel size of the content, or None when it is not a complete
    JPEG, PNG, WebP or PDF. Only headers are read; nothing is decoded or executed."""
    content_type = sniff(data)
    if content_type == PDF:
        return Content(PDF) if b"%%EOF" in data[-2048:] else None
    reader = {"image/png": _png_size, "image/jpeg": _jpeg_size, "image/webp": _webp_size}.get(
        content_type or ""
    )
    size = reader(data) if reader else None
    if content_type is None or size is None:
        return None
    width, height = size
    if width <= 0 or height <= 0:
        return None
    return Content(content_type, width, height)


def clean_name(name: str | None) -> str:
    """A display name only: never used to build a storage path."""
    base = re.split(r"[\\/]", name or "")[-1]
    base = re.sub(r"[\x00-\x1f\x7f]", "", base).strip()
    return base[:255] or "file"


def clean_text(value: str | None, limit: int, multiline: bool = False) -> str | None:
    """Caption, description or category as plain text: no control characters,
    whitespace collapsed (line breaks kept in a description); empty becomes None."""
    if value is None:
        return None
    if multiline:
        lines = (re.sub(r"[\x00-\x1f\x7f]+", " ", line) for line in value.splitlines())
        text = "\n".join(re.sub(r"\s+", " ", line).strip() for line in lines).strip()
    else:
        text = re.sub(r"[\x00-\x1f\x7f\s]+", " ", value).strip()
    return text[:limit] or None


def _owner_filter(owner: dict[str, uuid.UUID | None]) -> list[ColumnElement[bool]]:
    """Exactly this owner: each narrower owner column equal, or null when not given."""
    return [
        getattr(StoredFile, column) == value
        if value is not None
        else getattr(StoredFile, column).is_(None)
        for column, value in owner.items()
    ]


class FileService:
    def __init__(self, session: Session, storage: ObjectStorage, actor: AppUser | None = None):
        self.session = session
        self.storage = storage
        self.actor = actor

    # --- writes ----------------------------------------------------------

    def upload(
        self,
        *,
        property_id: uuid.UUID,
        kind: FileKind,
        data: bytes,
        original_name: str | None,
        max_bytes: int,
        max_per_owner: int | None = None,
        building_id: uuid.UUID | None = None,
        layout_id: uuid.UUID | None = None,
        space_id: uuid.UUID | None = None,
        asset_id: uuid.UUID | None = None,
        caption: str | None = None,
        description: str | None = None,
        category: str | None = None,
        sort_order: int | None = None,
        is_primary: bool | None = None,
        on_stored: Callable[[str], None] | None = None,
    ) -> FileRead:
        """``on_stored`` is told the object path as soon as the bytes are stored, so a
        caller running a larger transaction can delete them if that transaction fails."""
        prop = require(self.session, Property, property_id, "Property")
        owner = {
            "building_id": building_id,
            "layout_id": layout_id,
            "space_id": space_id,
            "asset_id": asset_id,
        }
        described = self._check_owner(prop.id, owner)
        if (kind == FileKind.ASSET) != (asset_id is not None):
            raise BadRequestError("Asset images need kind ASSET and an asset_id, together.")
        # MAIN has always meant "the main picture", so it is the cover unless told otherwise.
        primary = kind == FileKind.MAIN if is_primary is None else is_primary
        if primary and kind == FileKind.DOCUMENT:
            raise BadRequestError("A document cannot be the primary file.")
        if sort_order is not None and not 0 <= sort_order <= 100_000:
            raise BadRequestError("sort_order must be between 0 and 100000.")
        if not data:
            raise BadRequestError("The file is empty.")
        if len(data) > max_bytes:
            raise PayloadTooLargeError(f"The file is larger than {max_bytes // (1024 * 1024)} MB.")
        content = inspect_content(data)
        if content is None or (content.content_type == PDF and kind not in PDF_KINDS):
            allowed = "JPG, PNG or WebP image" + (" or PDF" if kind in PDF_KINDS else "")
            raise UnsupportedMediaTypeError(
                f"Upload a complete {allowed}. The file's content is not one, or is damaged."
            )
        if content.width and content.height and content.width * content.height > MAX_PIXELS:
            raise UnsupportedMediaTypeError(
                f"The image is {content.width} × {content.height} pixels; "
                f"the limit is {MAX_PIXELS // 1_000_000} megapixels."
            )

        if max_per_owner is not None:
            self._check_room(prop.id, owner, max_per_owner, described)

        extension = IMAGE_TYPES.get(content.content_type, "pdf")
        object_path = f"{prop.id}/{kind.lower()}/{uuid.uuid4().hex}.{extension}"
        # The bytes go first and the transaction after, so no row lock is held while
        # they travel; if the transaction fails, the bytes are removed again.
        try:
            self.storage.put(object_path, data, content.content_type)
        except StorageError as exc:
            raise ServiceUnavailableError(f"The file was not stored: {exc}") from None
        if on_stored is not None:
            on_stored(object_path)

        record = StoredFile(
            property_id=prop.id,
            **owner,
            kind=kind,
            bucket=self.storage.bucket,
            object_path=object_path,
            original_name=clean_name(original_name),
            content_type=content.content_type,
            size_bytes=len(data),
            width_px=content.width,
            height_px=content.height,
            caption=clean_text(caption, 300),
            description=clean_text(description, 2000, multiline=True),
            category=clean_text(category, 80),
            sort_order=sort_order or 0,
            is_primary=False,
            uploaded_by=self.actor.id if self.actor else None,
        )
        try:
            # Serialises uploads and cover changes within the property, so the
            # count and the single primary file hold under concurrent requests.
            self._lock_property(prop.id)
            if max_per_owner is not None:
                self._check_room(prop.id, owner, max_per_owner, described)
            self.session.add(record)
            flush(self.session)
            if primary:
                self._make_primary(record)
            log_activity(
                self.session,
                self.actor,
                action="uploaded",
                entity_type="files",
                entity_id=record.id,
                property_id=prop.id,
                message=f'File "{record.original_name}" was uploaded to {described}.',
            )
            commit(self.session)
        except Exception:
            # The metadata was not saved, so the bytes must not linger unreferenced.
            self.session.rollback()
            self._discard(object_path)
            raise
        return self.to_read([record])[0]

    def update(self, file_id: uuid.UUID, values: dict[str, Any]) -> FileRead:
        record = self._get(file_id)
        if values.get("is_primary"):
            if record.kind == FileKind.DOCUMENT:
                raise BadRequestError("A document cannot be the primary file.")
            self._lock_property(record.property_id)
            self.session.refresh(record)
        for field, limit, multiline in (
            ("caption", 300, False),
            ("description", 2000, True),
            ("category", 80, False),
        ):
            if field in values:
                setattr(record, field, clean_text(values[field], limit, multiline))
        if "sort_order" in values:
            record.sort_order = values["sort_order"]
        if values.get("is_primary"):
            self._make_primary(record)
        elif values.get("is_primary") is False:
            record.is_primary = False
        log_activity(
            self.session,
            self.actor,
            action="updated",
            entity_type="files",
            entity_id=record.id,
            property_id=record.property_id,
            message=f'File "{record.original_name}" was updated.',
        )
        commit(self.session)
        return self.to_read([record])[0]

    def delete(self, file_id: uuid.UUID) -> None:
        record = self._get(file_id)
        path = record.object_path
        log_activity(
            self.session,
            self.actor,
            action="deleted",
            entity_type="files",
            entity_id=record.id,
            property_id=record.property_id,
            message=f'File "{record.original_name}" was deleted.',
        )
        self.session.delete(record)
        commit(self.session)
        # After the commit: a failure here leaves only unreachable bytes, never a broken
        # record. purge_orphans removes such leftovers later.
        self._discard(path)

    def purge_orphans(self, *, min_age_seconds: int = 3600, dry_run: bool = True) -> list[str]:
        """Stored objects no ``files`` row refers to: left by a failed delete, or by an
        upload whose metadata was never saved. Young objects are skipped, since an
        upload stores its bytes just before its row. Returns the paths found (and
        removed, unless dry_run)."""
        known = set(
            self.session.scalars(
                select(StoredFile.object_path).where(StoredFile.bucket == self.storage.bucket)
            )
        )
        cutoff = time.time() - min_age_seconds
        orphans = [
            obj.path
            for obj in self.storage.list_objects()
            if obj.path not in known and obj.modified_at < cutoff
        ]
        if not dry_run:
            for path in orphans:
                self._discard(path)
        return orphans

    # --- reads -----------------------------------------------------------

    def list(
        self,
        query: ListQuery,
        *,
        property_id: uuid.UUID | None = None,
        building_id: uuid.UUID | None = None,
        layout_id: uuid.UUID | None = None,
        space_id: uuid.UUID | None = None,
        asset_id: uuid.UUID | None = None,
        kind: FileKind | None = None,
        is_primary: bool | None = None,
        direct: bool = False,
    ) -> tuple[list[FileRead], int]:
        statement = select(StoredFile)
        owner = {
            "building_id": building_id,
            "layout_id": layout_id,
            "space_id": space_id,
            "asset_id": asset_id,
        }
        if direct:
            # Only files of exactly this owner, not of anything narrower inside it.
            statement = statement.where(*_owner_filter(owner))
        for column, value in (
            (StoredFile.property_id, property_id),
            *((getattr(StoredFile, c), v) for c, v in owner.items()),
            (StoredFile.kind, kind),
            (StoredFile.is_primary, is_primary),
        ):
            if value is not None:
                statement = statement.where(column == value)
        statement = statement.order_by(StoredFile.sort_order, StoredFile.created_at, StoredFile.id)
        rows, total = paginate_rows(self.session, statement, query)
        return self.to_read([row[0] for row in rows]), total

    def covers(self, property_ids: Sequence[uuid.UUID]) -> "list[FileRead]":
        """The primary image of each property that has one (property-level only)."""
        if len(property_ids) > MAX_COVERS:
            raise BadRequestError(f"Ask for at most {MAX_COVERS} properties at a time.")
        if not property_ids:
            return []
        records = self.session.scalars(
            select(StoredFile)
            .where(
                StoredFile.property_id.in_(set(property_ids)),
                StoredFile.is_primary,
                *_owner_filter(dict.fromkeys(OWNER_COLUMNS)),
            )
            .order_by(StoredFile.property_id)
        ).all()
        return self.to_read(records)

    def get(self, file_id: uuid.UUID) -> FileRead:
        return self.to_read([self._get(file_id)])[0]

    # --- helpers ---------------------------------------------------------

    def _get(self, file_id: uuid.UUID) -> StoredFile:
        record = self.session.get(StoredFile, file_id)
        if record is None:
            raise NotFoundError("File not found.")
        return record

    def _lock_property(self, property_id: uuid.UUID) -> None:
        self.session.execute(
            select(Property.id).where(Property.id == property_id).with_for_update()
        )

    def _check_room(
        self,
        property_id: uuid.UUID,
        owner: dict[str, uuid.UUID | None],
        limit: int,
        described: str,
    ) -> None:
        count = self.session.scalar(
            select(func.count())
            .select_from(StoredFile)
            .where(StoredFile.property_id == property_id, *_owner_filter(owner))
        )
        if (count or 0) >= limit:
            raise ConflictError(
                f"{described[0].upper()}{described[1:]} already has {count} files, the most "
                "allowed. Delete one first."
            )

    def _make_primary(self, record: StoredFile) -> None:
        """Clears the owner's previous primary file, then marks this one."""
        owner = {column: getattr(record, column) for column in OWNER_COLUMNS}
        self.session.execute(
            update(StoredFile)
            .where(
                and_(
                    StoredFile.property_id == record.property_id,
                    StoredFile.id != record.id,
                    StoredFile.is_primary,
                    *_owner_filter(owner),
                )
            )
            .values(is_primary=False)
            .execution_options(synchronize_session="fetch")
        )
        record.is_primary = True
        flush(self.session)

    def _check_owner(self, property_id: uuid.UUID, owner: dict[str, uuid.UUID | None]) -> str:
        """Checks each named owner belongs to the property; returns a description for the log."""
        owners: list[str] = []
        for model, column, label in (
            (Building, "building_id", "Building"),
            (Layout, "layout_id", "Layout"),
            (Space, "space_id", "Space"),
            (Asset, "asset_id", "Asset"),
        ):
            owner_id = owner[column]
            if owner_id is None:
                continue
            found: Any = require(self.session, model, owner_id, label)
            if found.property_id != property_id:
                raise BadRequestError(f"The {label.lower()} does not belong to this property.")
            owners.append(f'{label.lower()} "{found.name}"')
        return ", ".join(owners) or "the property"

    def _discard(self, path: str) -> None:
        with contextlib.suppress(StorageError):
            self.storage.delete(path)

    # Quoted: the method named "list" above shadows the builtin inside the class body.
    def to_read(self, records: Sequence[StoredFile]) -> "list[FileRead]":
        reads = []
        for record in records:
            try:
                link = self.storage.signed_url(record.object_path, URL_LIFETIME_SECONDS)
            except StorageError as exc:
                raise ServiceUnavailableError(f"File links are unavailable: {exc}") from None
            reads.append(
                FileRead.model_validate(
                    {
                        **{c.key: getattr(record, c.key) for c in StoredFile.__table__.columns},
                        "url": link.url,
                        "url_expires_at": datetime.fromtimestamp(link.expires_at, UTC),
                    }
                )
            )
        return reads
