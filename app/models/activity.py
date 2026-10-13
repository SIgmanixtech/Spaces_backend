import uuid

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin


class ActivityLog(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Append-only record of changes, shown as recent activity on the dashboard."""

    __tablename__ = "activity_log"
    __table_args__ = (Index("ix_activity_log_created_at", "created_at"),)

    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column()
    property_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("properties.id", ondelete="SET NULL"), index=True
    )
    message: Mapped[str] = mapped_column(String(500), nullable=False)
