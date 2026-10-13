import uuid
from datetime import date

from sqlalchemy import CheckConstraint, Date, ForeignKey, ForeignKeyConstraint, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import AssetStatus
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin, enum_check


class Asset(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Physical inventory at a property. Financial accounting is out of scope."""

    __tablename__ = "assets"
    __table_args__ = (
        enum_check("status", AssetStatus),
        # Any building, floor or space named must belong to the asset's property.
        ForeignKeyConstraint(
            ["building_id", "property_id"],
            ["buildings.id", "buildings.property_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["floor_id", "building_id"],
            ["floors.id", "floors.building_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["space_id", "property_id"],
            ["spaces.id", "spaces.property_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "floor_id IS NULL OR building_id IS NOT NULL", name="floor_requires_building"
        ),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    asset_tag: Mapped[str | None] = mapped_column(String(80), unique=True)
    property_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("properties.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    building_id: Mapped[uuid.UUID | None] = mapped_column()
    floor_id: Mapped[uuid.UUID | None] = mapped_column()
    space_id: Mapped[uuid.UUID | None] = mapped_column()
    location: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=AssetStatus.OPERATIONAL.value, index=True
    )
    purchase_date: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    maintenance_notes: Mapped[str | None] = mapped_column(Text)
