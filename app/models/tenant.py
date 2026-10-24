import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Numeric,
    Sequence,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import ContractStatus, TenantStatus
from app.models.mixins import ArchivableMixin, TimestampMixin, UUIDPrimaryKeyMixin, enum_check

contract_reference_seq = Sequence("contract_reference_seq", metadata=Base.metadata)


class Tenant(UUIDPrimaryKeyMixin, TimestampMixin, ArchivableMixin, Base):
    """A tenant's identity. Where and when they occupy space is TenantOccupancy."""

    __tablename__ = "tenants"
    __table_args__ = (enum_check("status", TenantStatus),)

    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    company_name: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(320), index=True)
    phone: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=TenantStatus.ACTIVE.value
    )
    notes: Mapped[str | None] = mapped_column(Text)
    # The customer account that owns this tenant record in the client app. Set when a
    # customer registers; never taken from a request body. One account, one tenant.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL"), unique=True
    )


class TenantOccupancy(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A contract: one period during which a tenant holds a property or one of its spaces.

    ``end_date`` is inclusive; NULL means the contract is ongoing. A NULL
    ``space_id`` means the tenant holds the property as a whole.

    Only ACTIVE and TERMINATED contracts hold inventory (see ``status``). A held
    space is also recorded in ``allocations``, which is what keeps it, and every
    space inside it, from being booked or contracted by anyone else. Terminating
    a contract moves ``end_date`` to the termination date and keeps the agreed
    one in ``original_end_date``; records are never deleted to release a space.
    """

    __tablename__ = "tenant_occupancies"
    __table_args__ = (
        ForeignKeyConstraint(
            ["space_id", "property_id"],
            ["spaces.id", "spaces.property_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("end_date IS NULL OR end_date >= start_date", name="dates_ordered"),
        enum_check("status", ContractStatus),
        CheckConstraint(
            "(monthly_rent IS NULL OR monthly_rent >= 0)"
            " AND (security_deposit IS NULL OR security_deposit >= 0)",
            name="amounts_not_negative",
        ),
        CheckConstraint(
            "(status = 'TERMINATED') = (terminated_at IS NOT NULL)", name="termination_recorded"
        ),
        CheckConstraint(
            "(status = 'CANCELLED') = (cancelled_at IS NOT NULL)", name="cancellation_recorded"
        ),
        # A space has at most one holding contract on any given day. The allocation
        # claims guard the space hierarchy as a whole; this stays as a second check.
        ExcludeConstraint(
            ("space_id", "="),
            (text("daterange(start_date, end_date, '[]')"), "&&"),
            where=text("space_id IS NOT NULL AND status IN ('ACTIVE', 'TERMINATED')"),
            using="gist",
            name="ex_tenant_occupancies_space_dates",
        ),
    )

    reference: Mapped[str] = mapped_column(
        String(20),
        unique=True,
        nullable=False,
        server_default=text(
            "('CT-' || to_char(now(), 'YYYY') || '-' "
            "|| lpad(nextval('contract_reference_seq')::text, 6, '0'))"
        ),
    )
    # Defaults to ACTIVE so the occupancy endpoints keep creating contracts that hold at once.
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=ContractStatus.ACTIVE.value, index=True
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    property_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("properties.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    space_id: Mapped[uuid.UUID | None] = mapped_column()
    booking_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("bookings.id", ondelete="SET NULL")
    )
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date)
    # The agreed last day, kept when termination brings end_date forward.
    original_end_date: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    monthly_rent: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    security_deposit: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="INR")
    terms: Mapped[str | None] = mapped_column(Text)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancellation_reason: Mapped[str | None] = mapped_column(Text)
    terminated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    termination_reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )
