import uuid

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class Building(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "buildings"
    __table_args__ = (
        UniqueConstraint("property_id", "name", name="uq_buildings_property_name"),
        # Target for composite foreign keys that pin a child to the same property.
        UniqueConstraint("id", "property_id", name="uq_buildings_id_property"),
    )

    property_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("properties.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)


class Floor(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "floors"
    __table_args__ = (
        UniqueConstraint("building_id", "level_number", name="uq_floors_building_level"),
        UniqueConstraint("id", "building_id", name="uq_floors_id_building"),
    )

    building_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("buildings.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    level_number: Mapped[int] = mapped_column(Integer, nullable=False)
