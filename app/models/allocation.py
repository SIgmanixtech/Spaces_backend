"""The allocation engine's records: what holds a space, and the claims that enforce it.

An allocation is one booking or one contract holding one space for a period.
Its claims spread that hold over the hierarchy: an EXCLUSIVE claim on the space
itself and a SHARED claim on every space containing it (portion, whole floor).
Two exclusion constraints then decide every conflict in the database itself:

* two exclusive claims on one space must not overlap: the same space twice;
* an exclusive and a shared claim on one space must not overlap: a space and
  something inside it (a whole-floor contract and a desk booking, or a portion
  contract and a cabin inside it, in either order).

Shared claims never conflict with each other, so 5A and 5B, or two desks in one
portion, are held independently. Triggers created by migration 0004 complete
the design: a claim locks its space in a mode matching the claim (so concurrent
writers queue instead of deadlocking), every live allocation must hold exactly
its current chain of claims, and a space with live claims cannot be moved or
archived. Periods are half-open ``[start, end)``; an open end means "ongoing".
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import TSTZRANGE, ExcludeConstraint, Range
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import AllocationSource
from app.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin, enum_check

EXCLUSIVE = 1
SHARED = 2


class Allocation(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "allocations"
    __table_args__ = (
        enum_check("source_type", AllocationSource),
        CheckConstraint(
            "(source_type = 'BOOKING' AND booking_id IS NOT NULL AND contract_id IS NULL"
            " AND NOT upper_inf(period))"
            " OR (source_type = 'CONTRACT' AND contract_id IS NOT NULL AND booking_id IS NULL)",
            name="source",
        ),
        CheckConstraint(
            "NOT isempty(period) AND NOT lower_inf(period)"
            " AND lower_inc(period) AND NOT upper_inc(period)",
            name="period_shape",
        ),
        CheckConstraint("is_active = (released_at IS NULL)", name="release"),
        ForeignKeyConstraint(
            ["space_id", "property_id"], ["spaces.id", "spaces.property_id"], ondelete="RESTRICT"
        ),
        UniqueConstraint("booking_id", name="uq_allocations_booking"),
        UniqueConstraint("contract_id", name="uq_allocations_contract"),
        # Target of the claims' composite key, so a claim always carries its
        # allocation's current period and active flag.
        UniqueConstraint("id", "period", "is_active", name="uq_allocations_claim_key"),
    )

    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    booking_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("bookings.id", ondelete="RESTRICT")
    )
    contract_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenant_occupancies.id", ondelete="RESTRICT")
    )
    property_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    space_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    period: Mapped[Range[datetime]] = mapped_column(TSTZRANGE, nullable=False)
    # The period as first allocated; termination or rescheduling changes ``period`` only.
    original_period: Mapped[Range[datetime]] = mapped_column(TSTZRANGE, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    release_reason: Mapped[str | None] = mapped_column(Text)


class AllocationClaim(Base):
    __tablename__ = "allocation_claims"
    __table_args__ = (
        CheckConstraint("claim_mode IN (1, 2)", name="mode"),
        UniqueConstraint("allocation_id", "space_id", name="uq_allocation_claims_allocation_space"),
        ForeignKeyConstraint(
            ["allocation_id", "period", "is_active"],
            ["allocations.id", "allocations.period", "allocations.is_active"],
            onupdate="CASCADE",
            ondelete="RESTRICT",
            name="fk_allocation_claims_allocation",
        ),
        ExcludeConstraint(
            ("space_id", "="),
            ("period", "&&"),
            where=text("is_active AND claim_mode = 1"),
            using="gist",
            name="ex_allocation_claims_exclusive",
        ),
        ExcludeConstraint(
            ("space_id", "="),
            ("claim_mode", "<>"),
            ("period", "&&"),
            where=text("is_active"),
            using="gist",
            name="ex_allocation_claims_mixed",
        ),
        Index(
            "uq_allocation_claims_one_exclusive",
            "allocation_id",
            unique=True,
            postgresql_where=text("claim_mode = 1"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    allocation_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    period: Mapped[Range[datetime]] = mapped_column(TSTZRANGE, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    space_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("spaces.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # 1 = exclusive (the allocated space), 2 = shared (a space containing it).
    claim_mode: Mapped[int] = mapped_column(SmallInteger, nullable=False)
