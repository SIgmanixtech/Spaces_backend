import uuid
from datetime import date

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import TenantStatus
from app.models.mixins import ArchivableMixin, TimestampMixin, UUIDPrimaryKeyMixin, enum_check


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


class TenantOccupancy(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One period during which a tenant occupies a property or one of its spaces.

    ``end_date`` is inclusive; NULL means the occupancy is ongoing. A NULL
    ``space_id`` means the tenant occupies the property as a whole.
    """

    __tablename__ = "tenant_occupancies"
    __table_args__ = (
        ForeignKeyConstraint(
            ["space_id", "property_id"],
            ["spaces.id", "spaces.property_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("end_date IS NULL OR end_date >= start_date", name="dates_ordered"),
        # A space has at most one occupant on any given day.
        ExcludeConstraint(
            ("space_id", "="),
            (text("daterange(start_date, end_date, '[]')"), "&&"),
            where=text("space_id IS NOT NULL"),
            using="gist",
            name="ex_tenant_occupancies_space_dates",
        ),
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
    notes: Mapped[str | None] = mapped_column(Text)
