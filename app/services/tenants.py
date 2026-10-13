import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import exists, or_, select

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models.booking import Booking
from app.models.enums import BookingStatus, OccupancyStatus
from app.models.property import Property
from app.models.space import Space
from app.models.tenant import Tenant, TenantOccupancy
from app.schemas.common import ListQuery
from app.schemas.tenant import OccupancyRead, TenantRead
from app.services.base import (
    DERIVED_FILTER_LIMIT,
    CrudService,
    commit,
    flush,
    log_activity,
    page_of,
    require,
)
from app.services.locations import check_space
from app.services.occupancy import OPEN_BOOKING_STATUSES, business_today, occupancy_covers


class TenantService(CrudService[Tenant]):
    model = Tenant
    label = "Tenant"
    search_fields = ("name", "company_name", "email", "phone")
    sort_fields = ("name", "company_name", "status", "created_at", "updated_at")
    default_sort = "name"

    def before_delete(self, record: Tenant) -> None:
        if self.session.scalar(
            select(
                exists().where(
                    TenantOccupancy.tenant_id == record.id, occupancy_covers(business_today())
                )
            )
        ):
            raise ConflictError("The tenant has a current occupancy. End it first.")

    def search(
        self,
        query: ListQuery,
        filters: dict[str, Any],
        *,
        property_id: uuid.UUID | None = None,
        occupancy_status: OccupancyStatus | None = None,
        include_archived: bool = False,
    ) -> tuple[list[TenantRead], int]:
        conditions = []
        if property_id is not None:
            # Tenants with a current or upcoming occupancy at the property.
            conditions.append(
                exists().where(
                    TenantOccupancy.tenant_id == Tenant.id,
                    TenantOccupancy.property_id == property_id,
                    or_(
                        TenantOccupancy.end_date.is_(None),
                        TenantOccupancy.end_date >= business_today(),
                    ),
                )
            )
        if occupancy_status is None:
            items, total = self.list(
                query, filters, include_archived=include_archived, conditions=conditions
            )
            return self.to_read(items), total

        # Derived value: computed for every match, then filtered and paged.
        everything = replace(query, page=1, page_size=DERIVED_FILTER_LIMIT)
        items, _ = self.list(
            everything, filters, include_archived=include_archived, conditions=conditions
        )
        return page_of(
            [item for item in self.to_read(items) if item.occupancy_status == occupancy_status],
            query,
        )

    def to_read(self, tenants: Sequence[Tenant]) -> list[TenantRead]:
        ids = [tenant.id for tenant in tenants]
        now = datetime.now(UTC)
        today = business_today(now)

        current: dict[uuid.UUID, tuple[TenantOccupancy, str, str | None]] = {}
        upcoming: set[uuid.UUID] = set()
        booking_refs: dict[uuid.UUID, list[str]] = defaultdict(list)
        if ids:
            for occupancy, property_name, space_name in self.session.execute(
                select(TenantOccupancy, Property.name, Space.name)
                .join(Property, Property.id == TenantOccupancy.property_id)
                .outerjoin(Space, Space.id == TenantOccupancy.space_id)
                .where(
                    TenantOccupancy.tenant_id.in_(ids),
                    or_(TenantOccupancy.end_date.is_(None), TenantOccupancy.end_date >= today),
                )
                .order_by(TenantOccupancy.start_date)
            ):
                if occupancy.start_date > today:
                    upcoming.add(occupancy.tenant_id)
                else:
                    current.setdefault(occupancy.tenant_id, (occupancy, property_name, space_name))

            for tenant_id, reference, status, end_at in self.session.execute(
                select(Booking.tenant_id, Booking.reference, Booking.status, Booking.end_at)
                .where(Booking.tenant_id.in_(ids), Booking.status != BookingStatus.CANCELLED)
                .order_by(Booking.created_at.desc())
            ):
                booking_refs[tenant_id].append(reference)
                if status in OPEN_BOOKING_STATUSES and end_at > now:
                    upcoming.add(tenant_id)

        result = []
        for tenant in tenants:
            occupancy, property_name, space_name = current.get(tenant.id, (None, None, None))
            if occupancy is not None:
                status = OccupancyStatus.OCCUPIED
            elif tenant.id in upcoming:
                status = OccupancyStatus.RESERVED
            else:
                status = OccupancyStatus.VACANT
            result.append(
                TenantRead.model_validate(
                    {
                        **{
                            column.key: getattr(tenant, column.key)
                            for column in Tenant.__table__.columns
                        },
                        "property_id": occupancy.property_id if occupancy else None,
                        "property_name": property_name,
                        "space_id": occupancy.space_id if occupancy else None,
                        "space_name": space_name,
                        "occupancy_status": status,
                        "booking_refs": booking_refs[tenant.id],
                    }
                )
            )
        return result

    # --- occupancies ---------------------------------------------------------

    def _occupancy_select(self) -> Any:
        return (
            select(TenantOccupancy, Property.name, Space.name)
            .join(Property, Property.id == TenantOccupancy.property_id)
            .outerjoin(Space, Space.id == TenantOccupancy.space_id)
        )

    def list_occupancies(self, tenant_id: uuid.UUID) -> list[OccupancyRead]:
        self.get(tenant_id)
        rows = self.session.execute(
            self._occupancy_select()
            .where(TenantOccupancy.tenant_id == tenant_id)
            .order_by(TenantOccupancy.start_date.desc())
        ).all()
        return [_occupancy_read(*row) for row in rows]

    def get_occupancy(self, occupancy_id: uuid.UUID) -> OccupancyRead:
        row = self.session.execute(
            self._occupancy_select().where(TenantOccupancy.id == occupancy_id)
        ).one_or_none()
        if row is None:
            raise NotFoundError("Occupancy not found.")
        return _occupancy_read(*row)

    def create_occupancy(self, tenant_id: uuid.UUID, values: dict[str, Any]) -> OccupancyRead:
        tenant = self.get(tenant_id)
        prop = require(self.session, Property, values["property_id"], "Property")
        check_space(self.session, prop.id, values.get("space_id"))
        occupancy = TenantOccupancy(tenant_id=tenant.id, **values)
        self.session.add(occupancy)
        flush(self.session)
        log_activity(
            self.session,
            self.actor,
            action="created",
            entity_type="tenant_occupancies",
            entity_id=occupancy.id,
            property_id=prop.id,
            message=f'Tenant "{tenant.name}" was assigned to "{prop.name}" '
            f"from {occupancy.start_date.isoformat()}.",
        )
        commit(self.session)
        return self.get_occupancy(occupancy.id)

    def update_occupancy(self, occupancy_id: uuid.UUID, values: dict[str, Any]) -> OccupancyRead:
        occupancy = self._occupancy(occupancy_id)
        for field, value in values.items():
            setattr(occupancy, field, value)
        if occupancy.end_date is not None and occupancy.end_date < occupancy.start_date:
            self.session.rollback()
            raise BadRequestError("end_date cannot be before start_date.")
        commit(self.session)
        return self.get_occupancy(occupancy_id)

    def delete_occupancy(self, occupancy_id: uuid.UUID) -> None:
        self.session.delete(self._occupancy(occupancy_id))
        commit(self.session)

    def _occupancy(self, occupancy_id: uuid.UUID) -> TenantOccupancy:
        occupancy = self.session.get(TenantOccupancy, occupancy_id)
        if occupancy is None:
            raise NotFoundError("Occupancy not found.")
        return occupancy


def _occupancy_read(
    occupancy: TenantOccupancy, property_name: str, space_name: str | None
) -> OccupancyRead:
    return OccupancyRead(
        id=occupancy.id,
        tenant_id=occupancy.tenant_id,
        property_id=occupancy.property_id,
        property_name=property_name,
        space_id=occupancy.space_id,
        space_name=space_name,
        start_date=occupancy.start_date,
        end_date=occupancy.end_date,
        notes=occupancy.notes,
        created_at=occupancy.created_at,
        updated_at=occupancy.updated_at,
    )
