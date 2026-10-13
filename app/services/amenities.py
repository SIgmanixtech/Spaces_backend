"""Amenities at properties.

The API resource is a property's assignment of an amenity. Definitions (name
and description) are shared, so "Parking" exists once however many properties
offer it.
"""

import uuid
from typing import Any

from sqlalchemy import Row, func, select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.models.amenity import Amenity, PropertyAmenity
from app.models.enums import AmenityStatus
from app.models.property import Property
from app.models.user import AppUser
from app.repositories.base import order_by_clause, paginate_rows, text_search
from app.schemas.amenity import AmenityRead
from app.schemas.common import ListQuery
from app.services.base import commit, flush, log_activity, require

_SORT_FIELDS = {
    "name": Amenity.name,
    "status": PropertyAmenity.status,
    "created_at": PropertyAmenity.created_at,
}


def _to_read(row: Row[Any]) -> AmenityRead:
    assignment, amenity, property_name = row
    return AmenityRead(
        id=assignment.id,
        amenity_id=amenity.id,
        name=amenity.name,
        description=amenity.description,
        property_id=assignment.property_id,
        property_name=property_name,
        opening_time=assignment.opening_time,
        closing_time=assignment.closing_time,
        status=assignment.status,
        notes=assignment.notes,
        created_at=assignment.created_at,
        updated_at=assignment.updated_at,
    )


class AmenityService:
    def __init__(self, session: Session, actor: AppUser | None = None) -> None:
        self.session = session
        self.actor = actor

    def _select(self) -> Any:
        return (
            select(PropertyAmenity, Amenity, Property.name)
            .join(Amenity, Amenity.id == PropertyAmenity.amenity_id)
            .join(Property, Property.id == PropertyAmenity.property_id)
        )

    def list(
        self,
        query: ListQuery,
        *,
        property_id: uuid.UUID | None = None,
        status: AmenityStatus | None = None,
    ) -> tuple[list[AmenityRead], int]:
        statement = self._select()
        if property_id is not None:
            statement = statement.where(PropertyAmenity.property_id == property_id)
        if status is not None:
            statement = statement.where(PropertyAmenity.status == status)
        if query.search:
            statement = statement.where(
                text_search([Amenity.name, Amenity.description], query.search)
            )
        order = order_by_clause(query.sort, _SORT_FIELDS, "name")
        rows, total = paginate_rows(
            self.session, statement.order_by(*order, PropertyAmenity.id), query
        )
        return [_to_read(row) for row in rows], total

    def get(self, assignment_id: uuid.UUID) -> AmenityRead:
        row = self.session.execute(
            self._select().where(PropertyAmenity.id == assignment_id)
        ).one_or_none()
        if row is None:
            raise NotFoundError("Amenity not found.")
        return _to_read(row)

    def create(self, values: dict[str, Any]) -> AmenityRead:
        prop = require(self.session, Property, values["property_id"], "Property")
        description = values.pop("description", None)
        amenity = self._definition(values.pop("name"), description)
        assignment = PropertyAmenity(amenity_id=amenity.id, **values)
        self.session.add(assignment)
        flush(self.session)
        self._log("added to", amenity.name, assignment, prop.name)
        commit(self.session)
        return self.get(assignment.id)

    def update(self, assignment_id: uuid.UUID, values: dict[str, Any]) -> AmenityRead:
        assignment = self._assignment(assignment_id)
        amenity = self.session.get(Amenity, assignment.amenity_id)
        if "name" in values:
            amenity = self._definition(values.pop("name"), None)
            assignment.amenity_id = amenity.id
        if "description" in values:
            amenity.description = values.pop("description")
        for field, value in values.items():
            setattr(assignment, field, value)
        commit(self.session)
        return self.get(assignment_id)

    def delete(self, assignment_id: uuid.UUID) -> None:
        """Remove the amenity from the property. The shared definition is kept."""
        assignment = self._assignment(assignment_id)
        amenity = self.session.get(Amenity, assignment.amenity_id)
        prop = self.session.get(Property, assignment.property_id)
        self._log("removed from", amenity.name, assignment, prop.name)
        self.session.delete(assignment)
        commit(self.session)

    def _assignment(self, assignment_id: uuid.UUID) -> PropertyAmenity:
        assignment = self.session.get(PropertyAmenity, assignment_id)
        if assignment is None:
            raise NotFoundError("Amenity not found.")
        return assignment

    def _definition(self, name: str, description: str | None) -> Amenity:
        amenity = self.session.scalar(
            select(Amenity).where(func.lower(Amenity.name) == name.lower())
        )
        if amenity is None:
            amenity = Amenity(name=name, description=description)
            self.session.add(amenity)
            flush(self.session)
        elif description is not None:
            amenity.description = description
        return amenity

    def _log(
        self, verb: str, amenity_name: str, assignment: PropertyAmenity, property_name: str
    ) -> None:
        log_activity(
            self.session,
            self.actor,
            action="updated",
            entity_type="property_amenities",
            entity_id=assignment.id,
            property_id=assignment.property_id,
            message=f'Amenity "{amenity_name}" was {verb} "{property_name}".',
        )
