import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import PriceUnit, SpaceOperationalStatus, SpaceType
from app.models.mixins import ArchivableMixin, TimestampMixin, UUIDPrimaryKeyMixin, enum_check


class Space(UUIDPrimaryKeyMixin, TimestampMixin, ArchivableMixin, Base):
    __tablename__ = "spaces"
    __table_args__ = (
        enum_check("space_type", SpaceType),
        enum_check("operational_status", SpaceOperationalStatus),
        enum_check("price_unit", PriceUnit),
        UniqueConstraint("property_id", "code", name="uq_spaces_property_code"),
        UniqueConstraint("id", "property_id", name="uq_spaces_id_property"),
        # A space can only sit on a layout of its own property.
        ForeignKeyConstraint(
            ["layout_id", "property_id"],
            ["layouts.id", "layouts.property_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("capacity > 0", name="capacity_positive"),
        CheckConstraint("price_amount >= 0", name="price_amount_non_negative"),
        CheckConstraint(
            "price_amount IS NULL OR price_unit IS NOT NULL", name="price_requires_unit"
        ),
        CheckConstraint("width > 0 AND height > 0", name="size_positive"),
    )

    property_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("properties.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    layout_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    code: Mapped[str | None] = mapped_column(String(50))
    space_type: Mapped[str] = mapped_column(String(32), nullable=False)
    capacity: Mapped[int | None] = mapped_column(Integer)
    price_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    price_unit: Mapped[str | None] = mapped_column(String(16))
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="INR")
    operational_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=SpaceOperationalStatus.ACTIVE.value
    )
    # Position on the layout grid.
    x: Mapped[int | None] = mapped_column(Integer)
    y: Mapped[int | None] = mapped_column(Integer)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
