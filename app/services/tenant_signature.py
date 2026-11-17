"""A tenant's signature image (owner request of 14 Nov 2026).

PNG or JPEG, up to 2 MB and 25 megapixels, checked from the content itself (not the name
or the declared type). Stored privately in the property-files bucket under
``tenants/<tenant id>/``; read only through a short-lived signed link, by administrators.

It is only a signature asset: storing one never marks a contract, agreement or document as
signed, and nothing in the signing workflow reads it.
"""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.errors import BadRequestError, NotFoundError
from app.models.tenant import Tenant
from app.models.user import AppUser
from app.schemas.tenant_onboarding import SignatureLink
from app.services.base import commit, log_activity
from app.services.files import inspect_content
from app.storage import ObjectStorage

SIGNATURE_TYPES = {"image/png": "png", "image/jpeg": "jpg"}
SIGNATURE_MAX_BYTES = 2 * 1024 * 1024
SIGNATURE_MAX_PIXELS = 25_000_000
LINK_SECONDS = 300


@dataclass(frozen=True)
class SignatureUpload:
    data: bytes
    content_type: str


def check_signature(data: bytes) -> SignatureUpload:
    """The upload as a checked PNG or JPEG, or a 400 saying what is wrong."""
    if not data:
        raise BadRequestError("The signature file is empty.")
    if len(data) > SIGNATURE_MAX_BYTES:
        raise BadRequestError("The signature image is larger than 2 MB.")
    content = inspect_content(data)
    if content is None or content.content_type not in SIGNATURE_TYPES:
        raise BadRequestError("The signature must be a complete PNG or JPG image.")
    if content.width and content.height and content.width * content.height > SIGNATURE_MAX_PIXELS:
        raise BadRequestError("The signature image is too large (over 25 megapixels).")
    return SignatureUpload(data, content.content_type)


def store_signature(
    session: Session,
    storage: ObjectStorage,
    actor: AppUser,
    tenant: Tenant,
    upload: SignatureUpload,
) -> str:
    """Puts the image in storage and references it on the tenant (flushed, not committed).
    Returns the new object path; the caller removes it if its transaction fails. The
    previous image, if any, is removed once the new one is stored."""
    path = (
        f"tenants/{tenant.id}/signature-{uuid.uuid4().hex}.{SIGNATURE_TYPES[upload.content_type]}"
    )
    storage.put(path, upload.data, upload.content_type)
    previous = tenant.signature_path
    tenant.signature_path = path
    tenant.signature_content_type = upload.content_type
    tenant.signature_size_bytes = len(upload.data)
    tenant.signature_uploaded_at = datetime.now(UTC)
    tenant.signature_uploaded_by = actor.id
    log_activity(
        session,
        actor,
        action="signature_replaced" if previous else "signature_added",
        entity_type="tenants",
        entity_id=tenant.id,
        property_id=None,
        message=f"Signature image {'replaced' if previous else 'added'} for tenant "
        f'"{tenant.name}" (a signature asset; nothing is marked as signed).',
    )
    session.flush()
    if previous:
        session.info.setdefault("delete_after_commit", []).append(previous)
    return path


class TenantSignatureService:
    def __init__(self, session: Session, actor: AppUser, storage: ObjectStorage) -> None:
        self.session = session
        self.actor = actor
        self.storage = storage

    def _tenant(self, tenant_id: uuid.UUID) -> Tenant:
        tenant = self.session.get(Tenant, tenant_id, with_for_update=True)
        if tenant is None:
            raise NotFoundError("Tenant not found.")
        return tenant

    def replace(self, tenant_id: uuid.UUID, data: bytes) -> SignatureLink:
        upload = check_signature(data)
        tenant = self._tenant(tenant_id)
        path = store_signature(self.session, self.storage, self.actor, tenant, upload)
        try:
            commit(self.session)
        except BaseException:
            self.storage.delete(path)
            raise
        self._delete_replaced()
        return self.link(tenant_id)

    def link(self, tenant_id: uuid.UUID) -> SignatureLink:
        tenant = self.session.get(Tenant, tenant_id)
        if tenant is None:
            raise NotFoundError("Tenant not found.")
        if not tenant.signature_path:
            raise NotFoundError("This tenant has no signature image.")
        signed = self.storage.signed_url(tenant.signature_path, LINK_SECONDS)
        return SignatureLink(
            url=signed.url,
            expires_in=LINK_SECONDS,
            content_type=tenant.signature_content_type or "",
            size_bytes=tenant.signature_size_bytes or 0,
        )

    def remove(self, tenant_id: uuid.UUID) -> None:
        tenant = self._tenant(tenant_id)
        if not tenant.signature_path:
            raise NotFoundError("This tenant has no signature image.")
        path = tenant.signature_path
        tenant.signature_path = None
        tenant.signature_content_type = None
        tenant.signature_size_bytes = None
        tenant.signature_uploaded_at = None
        tenant.signature_uploaded_by = None
        log_activity(
            self.session,
            self.actor,
            action="signature_removed",
            entity_type="tenants",
            entity_id=tenant.id,
            property_id=None,
            message=f'Signature image removed for tenant "{tenant.name}".',
        )
        commit(self.session)
        self._delete_quietly(path)

    def _delete_replaced(self) -> None:
        for path in self.session.info.pop("delete_after_commit", []):
            self._delete_quietly(path)

    def _delete_quietly(self, path: str) -> None:
        # The record is already right; a stray object is harmless.
        with contextlib.suppress(Exception):
            self.storage.delete(path)


def delete_replaced(session: Session, storage: ObjectStorage) -> None:
    """After a commit that replaced signature images: remove the old objects."""
    for path in session.info.pop("delete_after_commit", []):
        with contextlib.suppress(Exception):
            storage.delete(path)
