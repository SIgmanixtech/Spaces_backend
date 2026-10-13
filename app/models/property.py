import uuid
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import PropertyStatus
from app.models.mixins import ArchivableMixin, TimestampMixin, UUIDPrimaryKeyMixin, enum_check


class Property(UUIDPrimaryKeyMixin, TimestampMixin, ArchivableMixin, Base):
    __tablename__ = "properties"
    __table_args__ = (
        enum_check("status", PropertyStatus),
        CheckConstraint("latitude BETWEEN -90 AND 90", name="latitude_range"),
        CheckConstraint("longitude BETWEEN -180 AND 180", name="longitude_range"),
        CheckConstraint("floor_count >= 0", name="floor_count_non_negative"),
        CheckConstraint("capacity >= 0", name="capacity_non_negative"),
        CheckConstraint("area_sqft >= 0", name="area_sqft_non_negative"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    property_type: Mapped[str] = mapped_column(String(50), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    address: Mapped[str] = mapped_column(String(500), nullable=False)
    city: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(120), nullable=False)
    postal_code: Mapped[str] = mapped_column(String(20), nullable=False)
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    building_info: Mapped[str | None] = mapped_column(Text)
    floor_count: Mapped[int | None] = mapped_column(Integer)
    capacity: Mapped[int | None] = mapped_column(Integer)
    area_sqft: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    availability_notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=PropertyStatus.ACTIVE.value, index=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )
