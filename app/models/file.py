import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import FileKind
from app.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin, enum_check


class StoredFile(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Metadata for an object in Supabase Storage. The bytes never live here.

    A file belongs to its property and, optionally, to one building, layout, space
    or asset of that property (the composite keys make a mismatch impossible).
    The owner is the narrowest of those that is set. Each owner may have one
    primary file: the cover photo of a property, building, space or asset, or the
    main plan of a layout.
    """

    __tablename__ = "files"
    __table_args__ = (
        enum_check("kind", FileKind),
        ForeignKeyConstraint(
            ["building_id", "property_id"],
            ["buildings.id", "buildings.property_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["layout_id", "property_id"],
            ["layouts.id", "layouts.property_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["space_id", "property_id"],
            ["spaces.id", "spaces.property_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["asset_id", "property_id"],
            ["assets.id", "assets.property_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("size_bytes > 0", name="size_positive"),
        CheckConstraint("sort_order >= 0", name="sort_order_not_negative"),
        CheckConstraint(
            "(width_px IS NULL) = (height_px IS NULL) AND (width_px IS NULL OR width_px > 0)"
            " AND (height_px IS NULL OR height_px > 0)",
            name="dimensions_pair",
        ),
        CheckConstraint("NOT is_primary OR kind <> 'DOCUMENT'", name="document_not_primary"),
        # One primary file per owner. NULLS NOT DISTINCT makes "the property itself"
        # (every narrower owner null) one owner rather than many.
        Index(
            "uq_files_primary_per_owner",
            "property_id",
            "building_id",
            "layout_id",
            "space_id",
            "asset_id",
            unique=True,
            postgresql_where=text("is_primary"),
            postgresql_nulls_not_distinct=True,
        ),
    )

    property_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("properties.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    building_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    layout_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    space_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    asset_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    bucket: Mapped[str] = mapped_column(String(100), nullable=False)
    object_path: Mapped[str] = mapped_column(String(500), unique=True, nullable=False)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Read from the image itself; null for a PDF.
    width_px: Mapped[int | None] = mapped_column(Integer)
    height_px: Mapped[int | None] = mapped_column(Integer)
    caption: Mapped[str | None] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    # A short free label such as "Exterior", "Lobby" or "Cabin interior".
    category: Mapped[str | None] = mapped_column(String(80))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )
