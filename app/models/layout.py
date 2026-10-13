import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import LayoutStatus
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin, enum_check


class Layout(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A floor plan / seating arrangement. Its plan image is a ``files`` row."""

    __tablename__ = "layouts"
    __table_args__ = (
        enum_check("status", LayoutStatus),
        UniqueConstraint("property_id", "name", name="uq_layouts_property_name"),
        UniqueConstraint("id", "property_id", name="uq_layouts_id_property"),
        # The building must belong to this layout's property, the floor to its building.
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
        CheckConstraint(
            "floor_id IS NULL OR building_id IS NOT NULL", name="floor_requires_building"
        ),
        CheckConstraint("grid_columns > 0", name="grid_columns_positive"),
        CheckConstraint("grid_rows > 0", name="grid_rows_positive"),
    )

    property_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("properties.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    building_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    floor_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    grid_columns: Mapped[int | None] = mapped_column(Integer)
    grid_rows: Mapped[int | None] = mapped_column(Integer)
    # "metadata" is reserved on declarative classes, hence the attribute name.
    meta: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=LayoutStatus.ACTIVE.value
    )
