import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    Sequence,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import BillingPeriod, BookingChannel, BookingStatus
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin, enum_check

booking_reference_seq = Sequence("booking_reference_seq", metadata=Base.metadata)


class Booking(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A time-bounded reservation of one space over ``[start_at, end_at)``.

    Bookings are never deleted; cancelling one releases its time range.
    """

    __tablename__ = "bookings"
    __table_args__ = (
        enum_check("status", BookingStatus),
        enum_check("channel", BookingChannel),
        enum_check("billing_period", BillingPeriod),
        # The space must belong to the booking's property.
        ForeignKeyConstraint(
            ["space_id", "property_id"],
            ["spaces.id", "spaces.property_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("end_at > start_at", name="time_range_ordered"),
        CheckConstraint("tenant_id IS NOT NULL OR customer_name IS NOT NULL", name="has_customer"),
        # The database itself rejects overlapping bookings of a space, which is
        # what makes concurrent requests safe. Half-open ranges let one booking
        # start exactly when another ends.
        ExcludeConstraint(
            ("space_id", "="),
            (text("tstzrange(start_at, end_at, '[)')"), "&&"),
            where=text(f"status <> '{BookingStatus.CANCELLED.value}'"),
            using="gist",
            name="ex_bookings_space_time",
        ),
        Index("ix_bookings_created_at", "created_at"),
    )

    reference: Mapped[str] = mapped_column(
        String(20),
        unique=True,
        nullable=False,
        server_default=text(
            "('BK-' || to_char(now(), 'YYYY') || '-' "
            "|| lpad(nextval('booking_reference_seq')::text, 6, '0'))"
        ),
    )
    property_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("properties.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    space_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), index=True
    )
    customer_name: Mapped[str | None] = mapped_column(String(200))
    customer_email: Mapped[str | None] = mapped_column(String(320))
    customer_phone: Mapped[str | None] = mapped_column(String(32))
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=BookingStatus.PENDING.value, index=True
    )
    notes: Mapped[str | None] = mapped_column(Text)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancellation_reason: Mapped[str | None] = mapped_column(String(500))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )
    channel: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=BookingChannel.ADMIN.value
    )
    # The price the backend calculated when a customer submitted the booking: a record
    # of the quote, not a payment. Null for bookings made by an administrator.
    billing_period: Mapped[str | None] = mapped_column(String(16))
    pricing_rule_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("pricing_rules.id", ondelete="SET NULL")
    )
    quoted_total: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    quoted_currency: Mapped[str | None] = mapped_column(String(3))
    quote: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
