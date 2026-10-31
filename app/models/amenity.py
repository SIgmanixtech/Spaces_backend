import uuid
from datetime import time

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    Time,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import AmenityStatus
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin, enum_check


class Amenity(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An amenity definition, shared by every property that offers it."""

    __tablename__ = "amenities"

    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)


# Which level an assignment is at, from the ids it carries (BR-031):
#   property: none of building_id, floor_id, space_id
#   building: building_id only
#   floor:    floor_id, with its building_id (the floors key needs both)
#   space:    space_id only
PROPERTY_LEVEL = text("building_id IS NULL AND space_id IS NULL")
BUILDING_LEVEL = text("building_id IS NOT NULL AND floor_id IS NULL")
FLOOR_LEVEL = text("floor_id IS NOT NULL")
SPACE_LEVEL = text("space_id IS NOT NULL")


class PropertyAmenity(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An amenity offered at a property, or at one of its buildings, floors or spaces, with
    that place's hours and status. Every row keeps its property_id; the composite keys make
    the building, floor and space belong to that same property."""

    __tablename__ = "property_amenities"
    __table_args__ = (
        enum_check("status", AmenityStatus),
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
            "(space_id IS NULL OR (building_id IS NULL AND floor_id IS NULL))"
            " AND (floor_id IS NULL OR building_id IS NOT NULL)",
            name="level_shape",
        ),
        # Each amenity once per place; the same amenity may be at several levels.
        Index(
            "uq_property_amenities_pair",
            "property_id",
            "amenity_id",
            unique=True,
            postgresql_where=PROPERTY_LEVEL,
        ),
        Index(
            "uq_property_amenities_building",
            "building_id",
            "amenity_id",
            unique=True,
            postgresql_where=BUILDING_LEVEL,
        ),
        Index(
            "uq_property_amenities_floor",
            "floor_id",
            "amenity_id",
            unique=True,
            postgresql_where=FLOOR_LEVEL,
        ),
        Index(
            "uq_property_amenities_space",
            "space_id",
            "amenity_id",
            unique=True,
            postgresql_where=SPACE_LEVEL,
        ),
    )

    property_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("properties.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    amenity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("amenities.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    building_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    floor_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    space_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    opening_time: Mapped[time | None] = mapped_column(Time)
    closing_time: Mapped[time | None] = mapped_column(Time)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=AmenityStatus.ACTIVE.value
    )
    notes: Mapped[str | None] = mapped_column(Text)
