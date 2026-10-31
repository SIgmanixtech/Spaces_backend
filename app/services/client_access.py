"""Who may use the client app (business rules BR-003, BR-005, BR-014, BR-025).

There is no public registration. The property team invites a tenant once the tenant has
a completed contract and an email address; the tenant becomes active by setting a
password from the invitation. Only active tenants can sign in to the client app or use
any of its signed-in pages.

A contract is completed when the property team has approved it (status ACTIVE) and its
agreement has been recorded as signed (a contract document in status SIGNED).
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import exists, func, select, true
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.models.agreement import ContractDocument
from app.models.enums import ContractStatus, DocumentStatus, TenantStatus, UserRole
from app.models.tenant import Tenant, TenantOccupancy
from app.models.user import AppUser
from app.services.base import commit, log_activity

_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")

NOT_ACTIVATED = (
    "Your client account is set up by SIGMANIX when your contract is completed. "
    "Use the invitation email, or contact the property team."
)


def active_tenant(session: Session, user: AppUser) -> Tenant | None:
    """The tenant this customer account belongs to, if it may use the client app."""
    if user.role != UserRole.CUSTOMER or not user.is_active:
        return None
    tenant = session.scalar(select(Tenant).where(Tenant.user_id == user.id))
    if (
        tenant is None
        or tenant.client_activated_at is None
        or tenant.status != TenantStatus.ACTIVE
        or tenant.archived_at is not None
    ):
        return None
    return tenant


def has_completed_contract(session: Session, tenant: Tenant) -> bool:
    """Approved by the property team and recorded as signed (BR-025)."""
    signed = exists().where(
        ContractDocument.contract_id == TenantOccupancy.id,
        ContractDocument.status == DocumentStatus.SIGNED,
    )
    return bool(
        session.scalar(
            select(
                exists().where(
                    TenantOccupancy.tenant_id == tenant.id,
                    TenantOccupancy.status == ContractStatus.ACTIVE,
                    signed,
                )
            )
        )
    )


# --- invitations and activation ----------------------------------------------------------

STATUS_NOT_INVITED = "NOT_INVITED"
STATUS_INVITED = "INVITED"
STATUS_ACTIVE = "ACTIVE"


def access_status(tenant: Tenant) -> str:
    if tenant.client_activated_at is not None:
        return STATUS_ACTIVE
    if tenant.client_invited_at is not None:
        return STATUS_INVITED
    return STATUS_NOT_INVITED


def invitation_blocker(session: Session, tenant: Tenant) -> str | None:
    """Why this tenant cannot be invited yet, or None when they can."""
    if tenant.archived_at is not None or tenant.status != TenantStatus.ACTIVE:
        return "The tenant is not active."
    if not tenant.email or not _EMAIL.fullmatch(tenant.email.strip()):
        return "Add the tenant's email address first: client access needs an email."
    if not has_completed_contract(session, tenant):
        return (
            "The tenant has no completed contract yet: it must be approved and its "
            "agreement recorded as signed."
        )
    return None


class ClientAccessService:
    """Invite a tenant to the client app, and activate them when they set a password."""

    def __init__(self, session: Session, actor: AppUser | None, gateway: Any) -> None:
        self.session = session
        self.actor = actor
        self.gateway = gateway

    def invite(self, tenant_id: uuid.UUID, *, send_email: bool) -> dict[str, Any]:
        """Invite (or invite again). Safe to retry: one sign-in and one customer account per
        tenant, whatever happens. With send_email false, only a link to share is made."""
        tenant = self.session.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
        if tenant is None:
            raise NotFoundError("Tenant not found.")
        if tenant.client_activated_at is not None:
            raise ConflictError(
                "This tenant already has client access. They can reset their password "
                "from the sign-in page."
            )
        blocker = invitation_blocker(self.session, tenant)
        if blocker:
            raise ConflictError(blocker)
        email = tenant.email.strip().lower()  # type: ignore[union-attr]
        others = self.session.scalar(
            select(AppUser).where(
                func.lower(AppUser.email) == email,
                AppUser.id != tenant.user_id if tenant.user_id else true(),
            )
        )
        if others is not None:
            raise ConflictError(
                "Another account already uses this email. Use a different email for the tenant."
            )

        invitation = (
            self.gateway.invite(email, tenant.name)
            if send_email
            else self.gateway.invitation_link(email, tenant.name)
        )
        user_id = uuid.UUID(invitation.user_id)
        if tenant.user_id is not None and tenant.user_id != user_id:
            raise ConflictError(
                "The tenant is linked to a different sign-in than this email. "
                "Contact support before inviting again."
            )
        user = self.session.get(AppUser, user_id)
        if user is None:
            user = AppUser(id=user_id, email=email, full_name=tenant.name, role=UserRole.CUSTOMER)
            self.session.add(user)
            self.session.flush()
        elif user.role != UserRole.CUSTOMER:
            raise ConflictError("This email belongs to an administrator account.")
        tenant.user_id = user.id
        tenant.client_invited_at = datetime.now(UTC)
        log_activity(
            self.session,
            self.actor,
            action="client_invited",
            entity_type="tenants",
            entity_id=tenant.id,
            property_id=None,
            message=(
                f'Tenant "{tenant.name}" was invited to the client app'
                + (" (email requested)." if invitation.email_requested else " (link created).")
            ),
        )
        commit(self.session)
        return {
            "tenant_id": tenant.id,
            "status": access_status(tenant),
            "email": email,
            "invited_at": tenant.client_invited_at,
            "activated_at": tenant.client_activated_at,
            "email_requested": invitation.email_requested,
            "invitation_link": invitation.link,
            "message": (
                "Supabase accepted the invitation email. Delivery is not confirmed; if the "
                "tenant does not receive it, copy the invitation link instead."
                if invitation.email_requested
                else "No email was sent. Share this invitation link with the tenant yourself."
            ),
        }

    def activate(self, user: AppUser, access_token: str, password: str) -> Tenant:
        """The invited (or existing) tenant sets a password from their link."""
        tenant = self.session.scalar(
            select(Tenant).where(Tenant.user_id == user.id).with_for_update()
        )
        if (
            user.role != UserRole.CUSTOMER
            or tenant is None
            or tenant.client_invited_at is None
            or tenant.status != TenantStatus.ACTIVE
            or tenant.archived_at is not None
        ):
            raise ForbiddenError(NOT_ACTIVATED)
        self.gateway.set_password(access_token, password)
        first_time = tenant.client_activated_at is None
        if first_time:
            tenant.client_activated_at = datetime.now(UTC)
        user.is_active = True
        log_activity(
            self.session,
            user,
            action="client_activated" if first_time else "password_changed",
            entity_type="tenants",
            entity_id=tenant.id,
            property_id=None,
            message=(
                f'Tenant "{tenant.name}" set a password and can now use the client app.'
                if first_time
                else f'Tenant "{tenant.name}" set a new password.'
            ),
        )
        commit(self.session)
        return tenant
