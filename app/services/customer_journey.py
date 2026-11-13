"""The customer journey (owner decision, 13 Nov 2026).

1. Everyone who registers or signs in to the client app is a CRM lead (source CLIENT_APP),
   created once per customer account and visible to the property team in CRM.
2. When the property team confirms a customer's booking (after payment, which is verified by
   hand until an online payment gateway is approved), the customer becomes a tenant: one
   tenant per account, linked to their sign-in and to their lead, with client access active.
3. The property team can still create tenants themselves (unchanged).

Long-term rentals still go through the pipeline (proposal, Space Selected, contract).
Nothing here holds inventory.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.booking import Booking
from app.models.crm import Lead
from app.models.enums import BookingChannel, LeadSource, TenantStatus, UserRole
from app.models.tenant import Tenant
from app.models.user import AppUser
from app.services.base import flush, log_activity


def customer_lead(session: Session, user: AppUser) -> Lead | None:
    """The lead this customer account created, if any (the first one)."""
    return session.scalar(
        select(Lead)
        .where(Lead.created_by == user.id, Lead.source == LeadSource.CLIENT_APP)
        .order_by(Lead.created_at, Lead.id)
        .limit(1)
    )


def ensure_customer_lead(session: Session, user: AppUser) -> Lead | None:
    """Every client-app customer is a lead: create theirs once. Never commits."""
    if user.role != UserRole.CUSTOMER:
        return None
    existing = customer_lead(session, user)
    if existing is not None:
        return existing
    # A customer who is already a tenant came through the property team: no new lead.
    if session.scalar(select(Tenant.id).where(Tenant.user_id == user.id)):
        return None
    from app.services.crm import CrmService

    lead = CrmService(session, user).insert(
        {
            "source": LeadSource.CLIENT_APP,
            "contact_name": user.full_name or user.email or user.phone or "Client app customer",
            "contact_email": user.email,
            "contact_phone": user.phone,
        },
        how=" when the customer signed up in the client app",
    )
    return lead


def tenant_for_confirmed_booking(
    session: Session, actor: AppUser | None, booking: Booking
) -> Tenant | None:
    """A customer's booking was confirmed: make (or find) their tenant and link it to the
    booking, their account and their lead. Idempotent; never commits."""
    if booking.tenant_id is not None or booking.channel != BookingChannel.CUSTOMER:
        return None
    user = session.get(AppUser, booking.created_by) if booking.created_by else None
    if user is None or user.role != UserRole.CUSTOMER:
        return None
    now = datetime.now(UTC)
    tenant = session.scalar(select(Tenant).where(Tenant.user_id == user.id))
    created = False
    if tenant is None and user.email:
        # A tenant the property team already made with this email, not yet linked.
        tenant = session.scalar(
            select(Tenant).where(
                func.lower(Tenant.email) == user.email.lower(), Tenant.user_id.is_(None)
            )
        )
        if tenant is not None:
            tenant.user_id = user.id
    if tenant is None:
        tenant = Tenant(
            name=user.full_name or user.email or "Client app customer",
            email=user.email,
            phone=user.phone,
            user_id=user.id,
            status=TenantStatus.ACTIVE,
        )
        session.add(tenant)
        created = True
    if tenant.client_activated_at is None:
        tenant.client_invited_at = tenant.client_invited_at or now
        tenant.client_activated_at = now
    flush(session)
    booking.tenant_id = tenant.id
    log_activity(
        session,
        actor,
        action="tenant_from_booking",
        entity_type="tenants",
        entity_id=tenant.id,
        property_id=booking.property_id,
        message=(
            f'Tenant "{tenant.name}" was created'
            if created
            else f'Tenant "{tenant.name}" was linked'
        )
        + f" when booking {booking.reference} was confirmed.",
    )
    for lead in session.scalars(
        select(Lead).where(
            Lead.created_by == user.id,
            Lead.source == LeadSource.CLIENT_APP,
            Lead.tenant_id.is_(None),
        )
    ):
        lead.tenant_id = tenant.id
        lead.tenant_linked_at = now
        log_activity(
            session,
            actor,
            action="tenant_linked",
            entity_type="leads",
            entity_id=lead.id,
            property_id=lead.property_id,
            message=f'Lead {lead.reference} linked to tenant "{tenant.name}" when booking '
            f"{booking.reference} was confirmed.",
        )
    flush(session)
    return tenant
