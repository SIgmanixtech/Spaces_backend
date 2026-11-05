"""Company profiles (Phase 4B): the legal entity that owns properties and signs their
agreements (the licensor or lessor). Several properties may share one profile; a property
has at most one (properties.company_profile_id). Generating an agreement copies the
profile's values into the document, so later edits never change a generated or signed
agreement. Admin only. No Aadhaar."""

import uuid

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class CompanyProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "company_profiles"

    legal_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    registered_address: Mapped[str | None] = mapped_column(String(500))
    signatory_name: Mapped[str | None] = mapped_column(String(200))
    signatory_designation: Mapped[str | None] = mapped_column(String(120))
    signatory_address: Mapped[str | None] = mapped_column(String(500))
    gstin: Mapped[str | None] = mapped_column(String(15))
    pan: Mapped[str | None] = mapped_column(String(10))
    cin: Mapped[str | None] = mapped_column(String(21))
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL")
    )
