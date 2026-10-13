import uuid

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, ForeignKeyConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import FileKind
from app.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin, enum_check


class StoredFile(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Metadata for an object in Supabase Storage. The bytes never live here."""

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
        CheckConstraint("size_bytes > 0", name="size_positive"),
    )

    property_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("properties.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    building_id: Mapped[uuid.UUID | None] = mapped_column()
    layout_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    space_id: Mapped[uuid.UUID | None] = mapped_column()
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    bucket: Mapped[str] = mapped_column(String(100), nullable=False)
    object_path: Mapped[str] = mapped_column(String(500), unique=True, nullable=False)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )
