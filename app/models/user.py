import uuid

from sqlalchemy import Boolean, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import UserRole
from app.models.mixins import TimestampMixin, enum_check


class AppUser(TimestampMixin, Base):
    """Backend authorization record for a Supabase Auth user.

    ``id`` is the Supabase Auth user id (the token's ``sub``). There is no
    foreign key into the ``auth`` schema: Supabase owns it and may change it.
    """

    __tablename__ = "app_users"
    __table_args__ = (enum_check("role", UserRole),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"), nullable=False)
