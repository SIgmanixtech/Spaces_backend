import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import exists, select

from app.core.errors import ConflictError
from app.models.amenity import Amenity, PropertyAmenity
from app.models.booking import Booking
from app.models.enums import AmenityStatus, OccupancyStatus
from app.models.property import Property
from app.models.tenant import Tenant, TenantOccupancy
from app.repositories.base import text_search
from app.schemas.common import ListQuery
from app.schemas.property import PropertyRead
from app.services.base import DERIVED_FILTER_LIMIT, CrudService, page_of
from app.services.occupancy import (
    OPEN_BOOKING_STATUSES,
    business_today,
    occupancy_covers,
    property_occupancy,
)


class PropertyService(CrudService[Property]):
    model = Property
    label = "Property"
    search_fields = ("name", "code", "address", "city", "state", "postal_code")
    sort_fields = ("name", "city", "state", "capacity", "status", "created_at", "updated_at")

    def property_id_of(self, record: Property) -> uuid.UUID | None:
        return record.id

    def search(
        self,
        query: ListQuery,
        filters: dict[str, Any],
        *,
        city: str | None = None,
        state: str | None = None,
        tenant: str | None = None,
        occupancy_status: OccupancyStatus | None = None,
        include_archived: bool = False,
    ) -> tuple[list[PropertyRead], int]:
        conditions = []
        if city:
            conditions.append(text_search([Property.city], city))
        if state:
            conditions.append(text_search([Property.state], state))
        if tenant:
            conditions.append(
                exists().where(
                    TenantOccupancy.property_id == Property.id,
                    TenantOccupancy.tenant_id == Tenant.id,
                    occupancy_covers(business_today()),
                    text_search([Tenant.name, Tenant.company_name], tenant),
                )
            )
        if occupancy_status is None:
            items, total = self.list(
                query, filters, include_archived=include_archived, conditions=conditions
            )
            return self.to_read(items), total

        # Occupancy is derived, not a column, so it is filtered after computing it
        # for every match. A property portfolio is small enough for this to be cheap.
        everything = replace(query, page=1, page_size=DERIVED_FILTER_LIMIT)
        items, _ = self.list(
            everything, filters, include_archived=include_archived, conditions=conditions
        )
        return page_of(
            [item for item in self.to_read(items) if item.occupancy_status == occupancy_status],
            query,
        )

    def before_delete(self, record: Property) -> None:
        now = datetime.now(UTC)
        if self.session.scalar(
            select(
                exists().where(
                    TenantOccupancy.property_id == record.id,
                    occupancy_covers(business_today(now)),
                )
            )
        ):
            raise ConflictError("The property has a current tenant occupancy. End it first.")
        if self.session.scalar(
            select(
                exists().where(
                    Booking.property_id == record.id,
                    Booking.status.in_(OPEN_BOOKING_STATUSES),
                    Booking.end_at > now,
                )
            )
        ):
            raise ConflictError("The property has open bookings. Cancel or complete them first.")

    def to_read(self, properties: Sequence[Property]) -> list[PropertyRead]:
        occupancy = property_occupancy(self.session, properties)
        amenities: dict[uuid.UUID, list[str]] = defaultdict(list)
        if properties:
            for property_id, name in self.session.execute(
                select(PropertyAmenity.property_id, Amenity.name)
                .join(Amenity, Amenity.id == PropertyAmenity.amenity_id)
                .where(
                    PropertyAmenity.property_id.in_([prop.id for prop in properties]),
                    PropertyAmenity.status == AmenityStatus.ACTIVE,
                )
                .order_by(Amenity.name)
            ):
                amenities[property_id].append(name)
        return [
            PropertyRead.model_validate(
                {
                    **{
                        column.key: getattr(prop, column.key)
                        for column in Property.__table__.columns
                    },
                    "occupancy_status": occupancy[prop.id].status,
                    "occupancy_rate": occupancy[prop.id].rate,
                    "tenant_name": occupancy[prop.id].tenant_name,
                    "amenities": amenities[prop.id],
                }
            )
            for prop in properties
        ]
