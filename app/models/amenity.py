import uuid
from datetime import time

from sqlalchemy import ForeignKey, String, Text, Time, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import AmenityStatus
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin, enum_check


class Amenity(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An amenity definition, shared by every property that offers it."""

    __tablename__ = "amenities"

    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)


class PropertyAmenity(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An amenity offered at a property, with that property's hours and status."""

    __tablename__ = "property_amenities"
    __table_args__ = (
        enum_check("status", AmenityStatus),
        UniqueConstraint("property_id", "amenity_id", name="uq_property_amenities_pair"),
    )

    property_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("properties.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    amenity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("amenities.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    opening_time: Mapped[time | None] = mapped_column(Time)
    closing_time: Mapped[time | None] = mapped_column(Time)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=AmenityStatus.ACTIVE.value
    )
    notes: Mapped[str | None] = mapped_column(Text)
