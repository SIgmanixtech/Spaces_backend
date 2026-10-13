from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import exists, select

from app.core.errors import BadRequestError, ConflictError
from app.models.booking import Booking
from app.models.property import Property
from app.models.space import Space
from app.models.tenant import TenantOccupancy
from app.schemas.space import SpaceRead
from app.services.base import CrudService, require
from app.services.locations import check_layout
from app.services.occupancy import (
    OPEN_BOOKING_STATUSES,
    business_today,
    occupancy_covers,
    space_statuses,
)


class SpaceService(CrudService[Space]):
    model = Space
    label = "Space"
    search_fields = ("name", "code")
    sort_fields = ("name", "code", "space_type", "capacity", "created_at", "updated_at")
    default_sort = "name"

    def validate(self, values: dict[str, Any], existing: Space | None) -> None:
        if existing is None:
            require(self.session, Property, values["property_id"], "Property")
        check_layout(self.session, values["property_id"], values.get("layout_id"))
        if values.get("price_amount") is not None and values.get("price_unit") is None:
            raise BadRequestError("A price needs a price_unit (HOUR, DAY or MONTH).")

    def before_delete(self, record: Space) -> None:
        now = datetime.now(UTC)
        if self.session.scalar(
            select(
                exists().where(
                    TenantOccupancy.space_id == record.id, occupancy_covers(business_today(now))
                )
            )
        ):
            raise ConflictError("The space is currently occupied. End the occupancy first.")
        if self.session.scalar(
            select(
                exists().where(
                    Booking.space_id == record.id,
                    Booking.status.in_(OPEN_BOOKING_STATUSES),
                    Booking.end_at > now,
                )
            )
        ):
            raise ConflictError("The space has open bookings. Cancel or complete them first.")

    def list_for_layout(self, layout_id: Any) -> list[Space]:
        return list(
            self.session.scalars(
                self.repository.select().where(Space.layout_id == layout_id).order_by(Space.name)
            )
        )

    def to_read(self, spaces: Sequence[Space]) -> list[SpaceRead]:
        statuses = space_statuses(self.session, spaces)
        return [
            SpaceRead.model_validate(
                {
                    **{
                        column.key: getattr(space, column.key) for column in Space.__table__.columns
                    },
                    "status": statuses[space.id],
                }
            )
            for space in spaces
        ]
