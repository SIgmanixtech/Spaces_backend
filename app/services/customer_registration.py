"""Customers register themselves (owner decision, 12 Nov 2026) to book and to ask for
long-term rentals, without a tenancy or a contract.

Registration creates a sign-in in Supabase (which emails a confirmation link) and an
``app_users`` row with the CUSTOMER role, set here and never chosen by the visitor. No tenant
is created: tenants still come from the property team (BR-005), and a contract still needs
the CRM pipeline. When the property team later invites a tenant with the same email, the
existing account is linked to it (see ClientAccessService.invite).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.enums import UserRole
from app.models.user import AppUser
from app.services.base import commit


class CustomerRegistrationService:
    def __init__(self, session: Session, gateway: Any) -> None:
        self.session = session
        self.gateway = gateway

    def register(self, full_name: str, email: str, phone: str | None, password: str) -> None:
        email = email.strip().lower()
        name = " ".join(full_name.split())
        phone = (phone or "").strip() or None
        existing = self.session.scalar(select(AppUser).where(func.lower(AppUser.email) == email))
        if existing is not None:
            return  # same answer as a new registration: nothing is revealed or changed
        user_id = self.gateway.sign_up(email, password, name, phone)
        if user_id is None:
            return  # already registered in Supabase
        if self.session.get(AppUser, uuid.UUID(user_id)) is not None:
            return
        if phone and self.session.scalar(select(AppUser.id).where(AppUser.phone == phone)):
            phone = None  # another account uses this number; keep the registration
        self.session.add(
            AppUser(
                id=uuid.UUID(user_id),
                email=email,
                full_name=name,
                phone=phone,
                role=UserRole.CUSTOMER,  # always; the visitor never chooses a role
            )
        )
        commit(self.session)
