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
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models.agreement import BookingDocument, ContractTemplate
from app.models.booking import Booking
from app.models.building import Building, Floor
from app.models.company import CompanyProfile
from app.models.enums import BookingStatus, TemplateStatus, TemplateType
from app.models.pricing import PricingRule
from app.models.property import Property
from app.models.space import Space
from app.models.tenant import Tenant
from app.models.user import AppUser
from app.services.agreements import (
    DOCX,
    MAX_SIGNED_COPY_BYTES,
    MAX_TEMPLATE_BYTES,
    PDF,
    _custom_value,
    check_signers,
)
from app.services.base import commit, flush, log_activity
from app.services.booking_terms import (
    BOOKING_PLACEHOLDERS,
    GST_TREATMENTS,
    OPTIONAL,
    Fee,
    booking_values,
)
from app.services.occupancy import business_today
from app.storage import ObjectStorage, StorageError

AGREEMENT_TYPE = "CONFERENCE_ROOM_USAGE"
AGREEMENT_LABEL = "Booking agreement"
_AADHAAR = re.compile(r"^[0-9]{4}$")
# Values that come from the generate form, not from records: never "missing" in a plan.
_FROM_FORM = {
    "fee_sentence",
    "gst_percentage",
    "gst_amount",
    "tds_percentage",
    "tds_amount",
    "subtotal_amount",
    "invoice_amount",
    "net_payable_amount",
    "amount_in_words",
}
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
        template: ContractTemplate | None = None,
        values: dict[str, Any] | None = None,
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
            template_id=template.id if template else None,
            template_version=template.version if template else None,
            data=values,
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

    # --- generation from an approved booking template (migration 0027) -------------------

    def select_template(self, property_id: uuid.UUID) -> ContractTemplate | None:
        """The ACTIVE booking agreement template: the property's own, else the default."""
        for scope in (
            ContractTemplate.property_id == property_id,
            ContractTemplate.property_id.is_(None),
        ):
            found = self.session.scalar(
                select(ContractTemplate).where(
                    ContractTemplate.contract_type == TemplateType.BOOKING_AGREEMENT,
                    ContractTemplate.status == TemplateStatus.ACTIVE,
                    scope,
                )
            )
            if found is not None:
                return found
        return None

    def plan(self, booking_id: uuid.UUID) -> dict[str, Any]:
        """Where generation stands: the template, what is missing from the records, a problem
        that stops it, and the fee suggested from the booking's quote and pricing."""
        booking = self._booking(booking_id)
        template = self.select_template(booking.property_id)
        problem: str | None = None
        documents = self.documents(booking.id)
        if booking.status not in (BookingStatus.CONFIRMED, BookingStatus.COMPLETED):
            problem = (
                f"The booking is {booking.status.lower()}. Confirm it first; only a confirmed "
                "or completed booking has an agreement."
            )
        elif booking.tenant_id is None:
            problem = (
                "The booking is not linked to a tenant. Add the customer in CRM > Tenants (or "
                "use the email of an existing tenant) so their details can be printed."
            )
        elif template is None:
            problem = (
                "No approved booking agreement template is active. Upload one in Contract "
                "Templates (type: Booking agreement) and approve it."
            )
        elif any(d.status == "SIGNED" for d in documents):
            problem = "This booking's agreement is signed; it is never generated again."
        missing: list[str] = []
        if template is not None and problem is None:
            values = self._values(booking, template, None, "PREVIEW", None, None)
            form_labels = {BOOKING_PLACEHOLDERS[k] for k in _FROM_FORM}
            missing = [
                m
                for m in self._missing(template, values, {})
                if m not in form_labels and not m.startswith("Fee table")
            ]
        rule = (
            self.session.get(PricingRule, booking.pricing_rule_id)
            if booking.pricing_rule_id
            else None
        )
        return {
            "booking": booking,
            "template": template,
            "problem": problem,
            "missing": missing,
            "custom_fields": list(template.custom_fields or []) if template else [],
            "suggested_amount": booking.quoted_total,
            "suggested_gst_treatment": rule.tax_treatment if rule else None,
            "suggested_gst_rate": rule.tax_rate if rule else None,
        }

    def generate(
        self,
        booking_id: uuid.UUID,
        *,
        amount: Decimal,
        gst_treatment: str,
        gst_rate: Decimal | None,
        tds_percent: Decimal | None = None,
        aadhaar_last4: str | None = None,
        residential_address: str | None = None,
        fields: dict[str, Any] | None = None,
        notes: str | None = None,
    ) -> tuple[BookingDocument, bool]:
        """Fill the approved booking template with the booking's records and the typed fee,
        and store the result as the booking's agreement (GENERATED; never approves)."""
        plan = self.plan(booking_id)
        if plan["problem"]:
            raise ConflictError(plan["problem"])
        booking: Booking = plan["booking"]
        template: ContractTemplate = plan["template"]
        errors = []
        if amount is None or amount <= 0:
            errors.append({"field": "amount", "message": "Enter the agreed fee."})
        if gst_treatment not in GST_TREATMENTS:
            errors.append({"field": "gst_treatment", "message": "Included, added or exempt."})
        if gst_treatment != "EXEMPT" and (gst_rate is None or not 0 < gst_rate <= 28):
            errors.append({"field": "gst_rate", "message": "A GST rate from 0.01 to 28."})
        if tds_percent is not None and not 0 <= tds_percent <= 100:
            errors.append({"field": "tds_percent", "message": "From 0 to 100, or empty."})
        aadhaar = (aadhaar_last4 or "").strip() or None
        if aadhaar and not _AADHAAR.match(aadhaar):
            errors.append({"field": "aadhaar_last4", "message": "The last four digits only."})
        residence = " ".join((residential_address or "").split()) or None
        if residence and len(residence) > 500:
            errors.append({"field": "residential_address", "message": "At most 500 characters."})
        entered: dict[str, str] = {}
        for spec in template.custom_fields or []:
            try:
                value = _custom_value(spec, (fields or {}).get(spec["key"]))
            except ValueError as exc:
                errors.append({"field": spec["key"], "message": str(exc)})
                continue
            if value is not None:
                entered[spec["key"]] = value
        if errors:
            raise BadRequestError("Some details are not valid.", errors=errors)
        fee = Fee(
            amount=amount,
            gst_treatment=gst_treatment,
            gst_rate=gst_rate or Decimal(0),
            tds_rate=tds_percent,
        )
        existing = self.documents(booking.id)
        reference = self._next_reference(booking, existing)
        values = self._values(booking, template, fee, reference, aadhaar, residence)
        missing = self._missing(template, values, entered)
        if missing:
            raise BadRequestError(
                "These details are needed before the agreement can be generated: "
                + ", ".join(missing)
                + ".",
                errors=[{"field": m, "message": "Required"} for m in missing],
            )

        from docxtpl import DocxTemplate
        from jinja2 import Environment, StrictUndefined

        try:
            source = self._storage().get(template.object_path)
        except StorageError as exc:
            raise ConflictError("The template file could not be read. Try again later.") from exc
        context: dict[str, Any] = {k: ("" if v is None else v) for k, v in values.items()}
        context["custom"] = {
            s["key"]: entered.get(s["key"], "") for s in template.custom_fields or []
        }
        try:
            rendered = DocxTemplate(io.BytesIO(source))
            rendered.render(context, Environment(undefined=StrictUndefined), autoescape=True)
            out = io.BytesIO()
            rendered.save(out)
        except Exception as exc:  # the template is checked on approval; anything else is a fault
            raise ConflictError(
                "The agreement could not be generated from the template. The property team "
                "has to check the template."
            ) from exc
        printed: dict[str, Any] = {k: v for k, v in values.items() if k != "membership_items"}
        printed["fee"] = {
            "amount": str(amount),
            "gst_treatment": gst_treatment,
            "gst_rate": str(gst_rate) if gst_rate is not None else None,
            "tds_percent": str(tds_percent) if tds_percent is not None else None,
        }
        printed["custom"] = entered
        return self.store(
            booking.id,
            data=out.getvalue(),
            file_name=f"{reference} - {template.name} - {booking.reference}.docx"[-255:],
            reference=reference,
            notes=notes,
            template=template,
            values=printed,
        )

    def _values(
        self,
        booking: Booking,
        template: ContractTemplate,
        fee: Fee | None,
        reference: str,
        aadhaar: str | None,
        residence: str | None,
    ) -> dict[str, Any]:
        space = self.session.get(Space, booking.space_id)
        parent = (
            self.session.get(Space, space.parent_space_id)
            if space is not None and space.parent_space_id
            else None
        )
        portion = parent if parent is not None and parent.space_type == "PORTION" else None
        floor = (
            self.session.get(Floor, space.floor_id)
            if space is not None and space.floor_id
            else None
        )
        building = (
            self.session.get(Building, floor.building_id)
            if floor is not None and floor.building_id
            else None
        )
        prop = self.session.get(Property, booking.property_id)
        profile = (
            self.session.get(CompanyProfile, prop.company_profile_id)
            if prop is not None and prop.company_profile_id
            else None
        )
        tenant = self.session.get(Tenant, booking.tenant_id) if booking.tenant_id else None
        return booking_values(
            booking=booking,
            tenant=tenant,
            prop=prop,
            building=building,
            floor=floor,
            portion=portion,
            space=space,
            profile=profile,
            fee=fee,
            reference=reference,
            generated_on=business_today(),
            aadhaar_last4=aadhaar,
            residence=residence,
        )

    def _missing(
        self, template: ContractTemplate, values: dict[str, Any], entered: dict[str, str]
    ) -> list[str]:
        missing: list[str] = []
        if any(n.startswith("item.") for n in template.placeholders) and not values.get(
            "membership_items"
        ):
            missing.append("Fee table (needs the agreed fee)")
        for name in template.placeholders:
            if name.startswith("item."):
                continue
            if name.startswith("custom."):
                key = name.split(".", 1)[1]
                spec = next((s for s in template.custom_fields or [] if s["key"] == key), None)
                if spec and spec.get("required") and not entered.get(key):
                    missing.append(spec["label"])
                continue
            if name in OPTIONAL:
                continue
            value = values.get(name)
            if value is None or value == "":
                label = BOOKING_PLACEHOLDERS.get(name, name)
                if label not in missing:
                    missing.append(label)
        return missing

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
