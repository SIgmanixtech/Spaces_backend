"""A temporary checkout hold while a customer pays online (owner decision, 12 Nov 2026).

Separate from ordinary booking requests (which reserve nothing) and from the 7-day holds on
pending contracts. While ACTIVE and before ``expires_at`` it holds its space through an
allocation (source PAYMENT_HOLD), so the database refuses any overlapping confirmed booking,
contract or other hold. It ends as:

    ACTIVE --payment verified in time--> CONVERTED (a CONFIRMED booking, booking_id)
       |---- customer cancels / payment fails --> RELEASED
       |---- 10 minutes pass ------------------> EXPIRED (released server-side)
    paid after it ended and the space was taken --> NEEDS_REVIEW (refund or manual decision)
    NEEDS_REVIEW --admin records the outcome--> RESOLVED

``payment_reference`` is the gateway's id for the payment, unique, so a duplicate or delayed
notification is recognised and never creates a second booking. Initiating a payment changes
nothing; only a verified payment converts a hold.
"""

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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import BillingPeriod, PaymentHoldStatus
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin, enum_check

payment_hold_reference_seq = Sequence("payment_hold_reference_seq", metadata=Base.metadata)


class PaymentHold(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "payment_holds"
    __table_args__ = (
        enum_check("status", PaymentHoldStatus),
        enum_check("billing_period", BillingPeriod),
        ForeignKeyConstraint(
            ["space_id", "property_id"],
            ["spaces.id", "spaces.property_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("end_at > start_at", name="time_range_ordered"),
        CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
        CheckConstraint(
            "(status = 'ACTIVE') = (released_at IS NULL)", name="released_unless_active"
        ),
        CheckConstraint(
            "status <> 'CONVERTED' OR booking_id IS NOT NULL", name="converted_booking"
        ),
        CheckConstraint(
            "status NOT IN ('NEEDS_REVIEW', 'RESOLVED') OR review_reason IS NOT NULL",
            name="review_reason_given",
        ),
        Index(
            "uq_payment_holds_payment_reference",
            "payment_reference",
            unique=True,
            postgresql_where=text("payment_reference IS NOT NULL"),
        ),
        Index(
            "ix_payment_holds_active_expiry",
            "expires_at",
            postgresql_where=text("status = 'ACTIVE'"),
        ),
    )

    reference: Mapped[str] = mapped_column(
        String(20),
        unique=True,
        nullable=False,
        server_default=text(
            "('PH-' || to_char(now(), 'YYYY') || '-' "
            "|| lpad(nextval('payment_hold_reference_seq')::text, 6, '0'))"
        ),
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=PaymentHoldStatus.ACTIVE.value, index=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_users.id", ondelete="RESTRICT"), nullable=False
    )
    property_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    space_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    billing_period: Mapped[str] = mapped_column(String(16), nullable=False)
    pricing_rule_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("pricing_rules.id", ondelete="SET NULL")
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    quote: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    notes: Mapped[str | None] = mapped_column(Text)
    # Server time is the authority: set by the backend when the hold is created.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    release_reason: Mapped[str | None] = mapped_column(String(32))
    booking_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("bookings.id", ondelete="RESTRICT"), unique=True
    )
    payment_reference: Mapped[str | None] = mapped_column(String(100))
    payment_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    payment_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_reason: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )
    resolution: Mapped[str | None] = mapped_column(Text)
