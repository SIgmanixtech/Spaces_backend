"""Tenant accounts (BR-017, read-only Accounts phase).

One account per tenant with a completed contract (BR-025), opened the first time the
Accounts pages are read and never duplicated. It has no formatted reference until D7 is
confirmed. The lead link is filled when lead conversion exists (Phase 5). Invoices,
payments and advances are not here yet: they wait for D5, D6 and D7.
"""

import uuid

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class Account(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "accounts"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("leads.id", ondelete="SET NULL"), index=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )
