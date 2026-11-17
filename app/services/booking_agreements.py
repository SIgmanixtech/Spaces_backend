"""Agreements for one-time bookings, such as the Conference Room Usage Agreement
(17 Nov 2026, migration 0026).

Rules this module keeps:

* A document belongs to one booking. It never creates a tenant, contract, lease,
  membership, invoice or payment, and never changes the booking.
* The DOCX is stored once (bytes before the row; removed again if the row cannot be saved)
  and never rewritten. Storing the same file for the same booking again returns the
  existing document: no duplicate, no new reference.
* Status moves GENERATED -> APPROVED -> SIGNED only by explicit administrator actions.
  SIGNED needs the uploaded signed copy (PDF) and both parties named; nothing is marked
  signed automatically. A signed document is never replaced. A newer unsigned file for the
  same booking SUPERSEDES the older unsigned one, which stays in the history.
* Files are reached only through short-lived signed links issued to administrators.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import re
import uuid
import zipfile
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models.agreement import BookingDocument
from app.models.booking import Booking
from app.models.enums import BookingStatus
from app.models.user import AppUser
from app.services.agreements import (
    DOCX,
    MAX_SIGNED_COPY_BYTES,
    MAX_TEMPLATE_BYTES,
    PDF,
    check_signers,
)
from app.services.base import commit, flush, log_activity
from app.services.occupancy import business_today
from app.storage import ObjectStorage, StorageError

AGREEMENT_TYPE = "CONFERENCE_ROOM_USAGE"
AGREEMENT_LABEL = "Conference Room Usage Agreement"
_REFERENCE = re.compile(r"^[A-Z0-9][A-Z0-9-]{2,39}$")
LIFECYCLE = {
    "GENERATED": "Generated - pending review",
    "APPROVED": "Approved - ready for signing",
    "SIGNED": "Signed - copy uploaded",
    "SUPERSEDED": "Superseded",
}


def is_docx(data: bytes) -> bool:
    if not data.startswith(b"PK"):
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return "word/document.xml" in archive.namelist()
    except zipfile.BadZipFile:
        return False


class BookingAgreementService:
    def __init__(
        self, session: Session, actor: AppUser | None, storage: ObjectStorage | None = None
    ) -> None:
        self.session = session
        self.actor = actor
        self.storage = storage

    # --- reads -----------------------------------------------------------------------------

    def documents(self, booking_id: uuid.UUID) -> list[BookingDocument]:
        self._booking(booking_id)
        return list(
            self.session.scalars(
                select(BookingDocument)
                .where(BookingDocument.booking_id == booking_id)
                .order_by(BookingDocument.created_at.desc(), BookingDocument.id)
            )
        )

    def get(self, document_id: uuid.UUID, *, lock: bool = False) -> BookingDocument:
        query = select(BookingDocument).where(BookingDocument.id == document_id)
        document = self.session.scalar(query.with_for_update() if lock else query)
        if document is None:
            raise NotFoundError("Agreement not found.")
        return document

    def link(self, document_id: uuid.UUID, *, signed: bool = False, expires_in: int = 300) -> str:
        document = self.get(document_id)
        path = document.signed_object_path if signed else document.object_path
        if path is None:
            raise NotFoundError("This agreement has no signed copy.")
        return self._storage().signed_url(path, expires_in).url

    # --- writes ----------------------------------------------------------------------------

    def store(
        self,
        booking_id: uuid.UUID,
        *,
        data: bytes,
        file_name: str,
        reference: str | None = None,
        generated_on: date | None = None,
        notes: str | None = None,
    ) -> tuple[BookingDocument, bool]:
        """Store an agreement DOCX for the booking. Returns (document, created)."""
        booking = self._booking(booking_id, lock=True)
        if booking.status not in (BookingStatus.CONFIRMED, BookingStatus.COMPLETED):
            raise ConflictError(
                f"A {booking.status.lower()} booking has no agreement. Only a confirmed or "
                "completed booking does."
            )
        if not data:
            raise BadRequestError("The file is empty.")
        if len(data) > MAX_TEMPLATE_BYTES:
            raise BadRequestError("The agreement is larger than 5 MB.")
        if not is_docx(data):
            raise BadRequestError("Upload the agreement as a Word document (.docx).")
        digest = hashlib.sha256(data).hexdigest()
        existing = self.documents(booking.id)
        for document in existing:
            if document.sha256 == digest:
                return document, False  # the same file: the same document
        if any(d.status == "SIGNED" for d in existing):
            raise ConflictError("This booking's agreement is already signed; it is never replaced.")
        if generated_on is not None and generated_on > business_today():
            raise BadRequestError("The generation date cannot be in the future.")
        ref = (reference or "").strip().upper() or self._next_reference(booking, existing)
        if not _REFERENCE.match(ref):
            raise BadRequestError("The reference may use capital letters, digits and hyphens.")
        if self.session.scalar(select(BookingDocument.id).where(BookingDocument.reference == ref)):
            raise ConflictError(f"The reference {ref} is already used by another agreement.")

        storage = self._storage()
        document = BookingDocument(
            id=uuid.uuid4(),
            reference=ref,
            booking_id=booking.id,
            agreement_type=AGREEMENT_TYPE,
            status="GENERATED",
            bucket=storage.bucket,
            file_name=(file_name or f"{ref}.docx")[-255:],
            size_bytes=len(data),
            sha256=digest,
            generated_on=generated_on or business_today(),
            notes=(notes or "").strip() or None,
            uploaded_by=self.actor.id if self.actor else None,
        )
        document.object_path = f"bookings/agreements/{booking.id}/{document.id}.docx"
        storage.put(document.object_path, data, DOCX)
        try:
            now = datetime.now(UTC)
            for older in existing:
                if older.status in ("GENERATED", "APPROVED"):
                    older.status = "SUPERSEDED"
                    older.superseded_at = now
            self.session.add(document)
            flush(self.session)
            self._log(
                "generated",
                document,
                booking,
                f"{AGREEMENT_LABEL} {ref} stored for booking {booking.reference}.",
            )
            commit(self.session)
        except BaseException:
            self.session.rollback()
            self._discard(document.object_path)
            raise
        return document, True

    def approve(self, document_id: uuid.UUID) -> BookingDocument:
        document = self.get(document_id, lock=True)
        if document.status != "GENERATED":
            raise ConflictError(
                f"A {document.status.lower()} agreement cannot be approved; only a generated one."
            )
        document.status = "APPROVED"
        document.approved_at = datetime.now(UTC)
        document.approved_by = self.actor.id if self.actor else None
        booking = self.session.get(Booking, document.booking_id)
        self._log(
            "approved", document, booking, f"{AGREEMENT_LABEL} {document.reference} approved."
        )
        commit(self.session)
        return document

    def upload_signed_copy(
        self,
        document_id: uuid.UUID,
        *,
        data: bytes,
        file_name: str,
        signed_on: date,
        signers: list[dict[str, Any]],
    ) -> BookingDocument:
        """Paper-signature evidence, not a verified digital signature. Needs an approved
        agreement; a signed one keeps its copy (never replaced here)."""
        document = self.get(document_id, lock=True)
        if document.status == "SIGNED":
            raise ConflictError("This agreement is already signed; its signed copy is kept.")
        if document.status != "APPROVED":
            raise ConflictError(
                f"A {document.status.lower()} agreement cannot be signed. Approve it first."
            )
        if not data:
            raise BadRequestError("The file is empty.")
        if len(data) > MAX_SIGNED_COPY_BYTES:
            raise BadRequestError("The signed copy is larger than 20 MB.")
        if not data.startswith(b"%PDF-"):
            raise BadRequestError("Upload the signed copy as a PDF file.")
        clean = check_signers(signers, signed_on)
        storage = self._storage()
        path = f"bookings/agreements/{document.booking_id}/signed/{uuid.uuid4()}.pdf"
        storage.put(path, data, PDF)
        try:
            document.signed_object_path = path
            document.signed_file_name = (file_name or "signed.pdf")[-255:]
            document.signed_size_bytes = len(data)
            document.signed_sha256 = hashlib.sha256(data).hexdigest()
            document.signed_on = signed_on
            document.signers = clean
            document.signed_uploaded_by = self.actor.id if self.actor else None
            document.signed_at = datetime.now(UTC)
            document.status = "SIGNED"
            flush(self.session)
            booking = self.session.get(Booking, document.booking_id)
            names = ", ".join(s["name"] for s in clean)
            self._log(
                "signed_copy_uploaded",
                document,
                booking,
                f"Signed copy of {AGREEMENT_LABEL} {document.reference} uploaded (signed on "
                f"{signed_on.isoformat()} by {names}); recorded as signed on paper.",
            )
            commit(self.session)
        except BaseException:
            self.session.rollback()
            self._discard(path)
            raise
        return document

    # --- helpers ---------------------------------------------------------------------------

    def _next_reference(self, booking: Booking, existing: list[BookingDocument]) -> str:
        base = f"CRA-{booking.reference}"
        return base if not existing else f"{base}-R{len(existing) + 1}"

    def _booking(self, booking_id: uuid.UUID, *, lock: bool = False) -> Booking:
        query = select(Booking).where(Booking.id == booking_id)
        booking = self.session.scalar(query.with_for_update() if lock else query)
        if booking is None:
            raise NotFoundError("Booking not found.")
        return booking

    def _storage(self) -> ObjectStorage:
        if self.storage is None:
            raise ConflictError("File storage is not configured on the server.")
        return self.storage

    def _discard(self, path: str) -> None:
        with contextlib.suppress(StorageError):
            self._storage().delete(path)

    def _log(
        self, action: str, document: BookingDocument, booking: Booking | None, message: str
    ) -> None:
        log_activity(
            self.session,
            self.actor,
            action=action,
            entity_type="booking_documents",
            entity_id=document.id,
            property_id=booking.property_id if booking else None,
            message=message[:500],
        )
