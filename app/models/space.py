import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    SmallInteger,
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
    """A physical space. Its id is stable: layouts and bookings refer to it, never the reverse.

    A space is either *unassigned* (no floor, no parent: every space created
    before the hierarchy existed) or sits on a floor, where exactly one FLOOR
    space is the root and every other space has a parent on the same floor.
    """

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
        CheckConstraint("area_sqft > 0", name="area_sqft_positive"),
        CheckConstraint("space_type <> 'NON_RENTABLE' OR NOT is_rentable", name="non_rentable"),
        # The floor must belong to the building, the building to the space's property.
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
        CheckConstraint("(floor_id IS NULL) = (building_id IS NULL)", name="floor_building_pair"),
        # A parent is on the same floor and exactly one level up. Depth therefore
        # falls strictly towards the root, which makes a cycle impossible.
        UniqueConstraint("id", "floor_id", "depth", name="uq_spaces_id_floor_depth"),
        ForeignKeyConstraint(
            ["parent_space_id", "floor_id", "parent_depth"],
            ["spaces.id", "spaces.floor_id", "spaces.depth"],
            ondelete="RESTRICT",
            # Moving a subtree changes parent and children in one transaction.
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "(parent_space_id IS NULL) = (parent_depth IS NULL)", name="parent_depth_pair"
        ),
        CheckConstraint(
            "depth BETWEEN 0 AND 4 AND (parent_depth IS NULL OR parent_depth = depth - 1)",
            name="depth_valid",
        ),
        CheckConstraint(
            "(floor_id IS NULL AND parent_space_id IS NULL AND depth = 0"
            " AND space_type <> 'FLOOR')"
            " OR (floor_id IS NOT NULL"
            " AND (space_type = 'FLOOR') = (parent_space_id IS NULL)"
            " AND (space_type = 'FLOOR') = (depth = 0))",
            name="hierarchy_shape",
        ),
        Index(
            "uq_spaces_floor_root",
            "floor_id",
            unique=True,
            postgresql_where=text("space_type = 'FLOOR' AND archived_at IS NULL"),
        ),
    )

    property_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("properties.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    building_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    floor_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    parent_space_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    # Levels below the floor root. parent_depth repeats the parent's depth so the
    # database can check it; both are maintained by the space service.
    depth: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    parent_depth: Mapped[int | None] = mapped_column(SmallInteger)
    layout_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    code: Mapped[str | None] = mapped_column(String(50))
    space_type: Mapped[str] = mapped_column(String(32), nullable=False)
    capacity: Mapped[int | None] = mapped_column(Integer)
    area_sqft: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    is_rentable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
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
