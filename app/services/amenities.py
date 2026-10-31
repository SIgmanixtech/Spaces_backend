"""Amenities at properties, buildings, floors and spaces (BR-031, D16 option C).

The API resource is an assignment of an amenity to a place. Definitions (name and
description) are shared, so "Parking" exists once however many places offer it.

Every assignment keeps its property_id and, optionally, the building, floor or space of
that property it applies to. What applies at a place ("effective" amenities) is worked out
from the current active assignments every time: the place's own, then those of its parent
spaces, its floor, its building and its property. Nothing is copied downwards.

- INACTIVE assignments are ignored, and never hide an active one from another level.
- Archived spaces keep their assignments for the record but offer nothing.
- The whole-floor space takes no assignments: the floor itself does.
"""

import builtins
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement, Row, and_, false, func, or_, select
from sqlalchemy.orm import Session

from app.core.errors import BadRequestError, NotFoundError
from app.models.amenity import Amenity, PropertyAmenity
from app.models.building import Building, Floor
from app.models.enums import AmenityLevel, AmenityStatus, SpaceType
from app.models.property import Property
from app.models.space import Space
from app.models.user import AppUser
from app.repositories.base import order_by_clause, paginate_rows, text_search
from app.schemas.amenity import AmenityRead, AmenitySource, EffectiveAmenities, EffectiveAmenity
from app.schemas.common import ListQuery
from app.services.base import commit, flush, log_activity, require

PA = PropertyAmenity

_SORT_FIELDS = {
    "name": Amenity.name,
    "status": PA.status,
    "created_at": PA.created_at,
}

LEVEL_WHERE: dict[AmenityLevel, ColumnElement[bool]] = {
    AmenityLevel.PROPERTY: and_(PA.building_id.is_(None), PA.space_id.is_(None)),
    AmenityLevel.BUILDING: and_(PA.building_id.is_not(None), PA.floor_id.is_(None)),
    AmenityLevel.FLOOR: PA.floor_id.is_not(None),
    AmenityLevel.SPACE: PA.space_id.is_not(None),
}


def level_of(assignment: PropertyAmenity) -> AmenityLevel:
    if assignment.space_id is not None:
        return AmenityLevel.SPACE
    if assignment.floor_id is not None:
        return AmenityLevel.FLOOR
    if assignment.building_id is not None:
        return AmenityLevel.BUILDING
    return AmenityLevel.PROPERTY


def place_id_of(assignment: PropertyAmenity) -> uuid.UUID:
    return (
        assignment.space_id
        or assignment.floor_id
        or assignment.building_id
        or assignment.property_id
    )


def offered_anywhere() -> ColumnElement[bool]:
    """An active assignment that counts: not on an archived space. Use inside a query that
    selects from property_amenities."""
    return and_(
        PA.status == AmenityStatus.ACTIVE,
        or_(
            PA.space_id.is_(None),
            select(Space.id).where(Space.id == PA.space_id, Space.archived_at.is_(None)).exists(),
        ),
    )


def _to_read(row: Row[Any]) -> AmenityRead:
    assignment, amenity, property_name, building_name, floor_name, space_name = row
    return AmenityRead(
        id=assignment.id,
        amenity_id=amenity.id,
        name=amenity.name,
        description=amenity.description,
        property_id=assignment.property_id,
        property_name=property_name,
        level=level_of(assignment),
        building_id=assignment.building_id,
        building_name=building_name,
        floor_id=assignment.floor_id,
        floor_name=floor_name,
        space_id=assignment.space_id,
        space_name=space_name,
        opening_time=assignment.opening_time,
        closing_time=assignment.closing_time,
        status=assignment.status,
        notes=assignment.notes,
        created_at=assignment.created_at,
        updated_at=assignment.updated_at,
    )


@dataclass(frozen=True)
class Place:
    level: AmenityLevel
    id: uuid.UUID
    name: str


def effective_names_by_space(
    session: Session, property_ids: list[uuid.UUID]
) -> dict[uuid.UUID, set[str]]:
    """The names of the amenities that apply at every unarchived space of these properties,
    in two queries whatever the number of spaces (for search)."""
    if not property_ids:
        return {}
    spaces = {
        row.id: row
        for row in session.execute(
            select(
                Space.id,
                Space.parent_space_id,
                Space.floor_id,
                Space.building_id,
                Space.property_id,
            ).where(Space.property_id.in_(property_ids), Space.archived_at.is_(None))
        )
    }
    at: dict[tuple[AmenityLevel, uuid.UUID], set[str]] = defaultdict(set)
    for assignment, name in session.execute(
        select(PA, Amenity.name)
        .join(Amenity, Amenity.id == PA.amenity_id)
        .where(PA.property_id.in_(property_ids), PA.status == AmenityStatus.ACTIVE)
    ):
        level = level_of(assignment)
        if level == AmenityLevel.SPACE and assignment.space_id not in spaces:
            continue  # an archived space offers nothing
        at[(level, place_id_of(assignment))].add(name)

    from_spaces: dict[uuid.UUID, set[str]] = {}

    def own_and_parents(space_id: uuid.UUID) -> set[str]:
        if space_id not in from_spaces:
            space = spaces[space_id]
            names = set(at.get((AmenityLevel.SPACE, space_id), ()))
            if space.parent_space_id in spaces:
                names |= own_and_parents(space.parent_space_id)
            from_spaces[space_id] = names
        return from_spaces[space_id]

    out = {}
    for space in spaces.values():
        names = set(own_and_parents(space.id)) | at.get(
            (AmenityLevel.PROPERTY, space.property_id), set()
        )
        if space.floor_id:
            names |= at.get((AmenityLevel.FLOOR, space.floor_id), set())
        if space.building_id:
            names |= at.get((AmenityLevel.BUILDING, space.building_id), set())
        out[space.id] = names
    return out


def offered_names(session: Session, property_ids: list[uuid.UUID]) -> list[str]:
    """Every amenity offered anywhere in these properties (active, not on archived spaces)."""
    if not property_ids:
        return []
    return list(
        session.scalars(
            select(Amenity.name)
            .join(PA, PA.amenity_id == Amenity.id)
            .where(PA.property_id.in_(property_ids), offered_anywhere())
            .distinct()
        )
    )


class AmenityService:
    def __init__(self, session: Session, actor: AppUser | None = None) -> None:
        self.session = session
        self.actor = actor

    def _select(self) -> Any:
        return (
            select(PA, Amenity, Property.name, Building.name, Floor.name, Space.name)
            .join(Amenity, Amenity.id == PA.amenity_id)
            .join(Property, Property.id == PA.property_id)
            .outerjoin(Building, Building.id == PA.building_id)
            .outerjoin(Floor, Floor.id == PA.floor_id)
            .outerjoin(Space, Space.id == PA.space_id)
        )

    def list(
        self,
        query: ListQuery,
        *,
        property_id: uuid.UUID | None = None,
        status: AmenityStatus | None = None,
        level: AmenityLevel | None = None,
        building_id: uuid.UUID | None = None,
        floor_id: uuid.UUID | None = None,
        space_id: uuid.UUID | None = None,
    ) -> tuple[list[AmenityRead], int]:
        statement = self._select()
        for column, value in (
            (PA.property_id, property_id),
            (PA.status, status),
            (PA.building_id, building_id),
            (PA.floor_id, floor_id),
            (PA.space_id, space_id),
        ):
            if value is not None:
                statement = statement.where(column == value)
        if level is not None:
            statement = statement.where(LEVEL_WHERE[level])
        if query.search:
            statement = statement.where(
                text_search([Amenity.name, Amenity.description], query.search)
            )
        order = order_by_clause(query.sort, _SORT_FIELDS, "name")
        rows, total = paginate_rows(self.session, statement.order_by(*order, PA.id), query)
        return [_to_read(row) for row in rows], total

    def get(self, assignment_id: uuid.UUID) -> AmenityRead:
        row = self.session.execute(self._select().where(PA.id == assignment_id)).one_or_none()
        if row is None:
            raise NotFoundError("Amenity not found.")
        return _to_read(row)

    def create(self, values: dict[str, Any]) -> AmenityRead:
        prop = require(self.session, Property, values["property_id"], "Property")
        where = self._place(prop, values)
        description = values.pop("description", None)
        amenity = self._definition(values.pop("name"), description)
        assignment = PA(amenity_id=amenity.id, **values)
        self.session.add(assignment)
        flush(self.session)
        self._log("added to", amenity.name, assignment, where)
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
        """Remove the amenity from its place. The shared definition, and the same amenity at
        other levels, are kept."""
        assignment = self._assignment(assignment_id)
        amenity = self.session.get(Amenity, assignment.amenity_id)
        self._log("removed from", amenity.name, assignment, self._describe(assignment))
        self.session.delete(assignment)
        commit(self.session)

    # --- effective amenities --------------------------------------------------------------

    def effective(
        self,
        *,
        property_id: uuid.UUID | None = None,
        building_id: uuid.UUID | None = None,
        floor_id: uuid.UUID | None = None,
        space_id: uuid.UUID | None = None,
    ) -> EffectiveAmenities:
        """What applies at one place: its own active amenities plus those inherited from
        its parent spaces, floor, building and property, each listed once with every
        source, nearest first."""
        given = [v for v in (property_id, building_id, floor_id, space_id) if v is not None]
        if len(given) != 1:
            raise BadRequestError(
                "Give exactly one of property_id, building_id, floor_id, space_id."
            )
        prop, chain = self._chain(property_id, building_id, floor_id, space_id)
        here = chain[0]
        rank = {(p.level, p.id): i for i, p in enumerate(chain)}
        conditions = []
        for place in chain:
            if place.level == AmenityLevel.PROPERTY:
                conditions.append(and_(PA.property_id == place.id, LEVEL_WHERE[place.level]))
            elif place.level == AmenityLevel.BUILDING:
                conditions.append(and_(PA.building_id == place.id, LEVEL_WHERE[place.level]))
            elif place.level == AmenityLevel.FLOOR:
                conditions.append(PA.floor_id == place.id)
            else:
                conditions.append(PA.space_id == place.id)
        rows = self.session.execute(
            select(PA, Amenity)
            .join(Amenity, Amenity.id == PA.amenity_id)
            .where(
                PA.property_id == prop.id,
                PA.status == AmenityStatus.ACTIVE,
                or_(*conditions) if conditions else false(),
            )
        ).all()
        by_amenity: dict[uuid.UUID, tuple[Amenity, list[tuple[int, AmenitySource]]]] = {}
        names = {(p.level, p.id): p.name for p in chain}
        for assignment, amenity in rows:
            key = (level_of(assignment), place_id_of(assignment))
            if key not in rank:
                continue
            source = AmenitySource(
                level=key[0],
                place_id=key[1],
                place_name=names[key],
                assignment_id=assignment.id,
                opening_time=assignment.opening_time,
                closing_time=assignment.closing_time,
                notes=assignment.notes,
            )
            by_amenity.setdefault(amenity.id, (amenity, []))[1].append((rank[key], source))
        amenities = []
        for amenity, sources in by_amenity.values():
            sources.sort(key=lambda pair: pair[0])
            amenities.append(
                EffectiveAmenity(
                    amenity_id=amenity.id,
                    name=amenity.name,
                    description=amenity.description,
                    is_direct=sources[0][0] == 0,
                    sources=[s for _, s in sources],
                )
            )
        amenities.sort(key=lambda a: a.name.lower())
        return EffectiveAmenities(
            level=here.level,
            place_id=here.id,
            place_name=here.name,
            property_id=prop.id,
            amenities=amenities,
        )

    def _chain(
        self,
        property_id: uuid.UUID | None,
        building_id: uuid.UUID | None,
        floor_id: uuid.UUID | None,
        space_id: uuid.UUID | None,
    ) -> tuple[Property, builtins.list[Place]]:
        """The place and everything above it, nearest first."""
        chain: list[Place] = []
        if space_id is not None:
            space = self._space(space_id)
            if space.archived_at is not None:
                raise BadRequestError(
                    f'Space "{space.name}" is archived: its amenities are kept for the record '
                    "but no longer apply."
                )
            if space.space_type == SpaceType.FLOOR:
                floor_id = space.floor_id
            else:
                chain.append(Place(AmenityLevel.SPACE, space.id, space.name))
                parent_id = space.parent_space_id
                while parent_id is not None:
                    parent = self.session.get(Space, parent_id)
                    if parent is None or parent.space_type == SpaceType.FLOOR:
                        break
                    if parent.archived_at is None:
                        chain.append(Place(AmenityLevel.SPACE, parent.id, parent.name))
                    parent_id = parent.parent_space_id
                floor_id = space.floor_id
                property_id = space.property_id
        if floor_id is not None:
            floor = require(self.session, Floor, floor_id, "Floor")
            chain.append(Place(AmenityLevel.FLOOR, floor.id, floor.name))
            building_id = floor.building_id
        if building_id is not None:
            building = require(self.session, Building, building_id, "Building")
            chain.append(Place(AmenityLevel.BUILDING, building.id, building.name))
            property_id = building.property_id
        prop = require(self.session, Property, property_id, "Property")  # type: ignore[arg-type]
        chain.append(Place(AmenityLevel.PROPERTY, prop.id, prop.name))
        return prop, chain

    # --- helpers --------------------------------------------------------------------------

    def _place(self, prop: Property, values: dict[str, Any]) -> str:
        """Checks the optional building, floor or space belongs to the property and can take
        amenities, fills in the floor's building, and describes the place for the log."""
        building_id = values.get("building_id")
        floor_id = values.get("floor_id")
        space_id = values.get("space_id")
        if space_id is not None and (building_id is not None or floor_id is not None):
            raise BadRequestError("Give one place for the amenity: a building, a floor or a space.")
        if floor_id is not None:
            floor = require(self.session, Floor, floor_id, "Floor")
            building = self.session.get(Building, floor.building_id)
            if building is None or building.property_id != prop.id:
                raise BadRequestError(f'Floor "{floor.name}" is not part of "{prop.name}".')
            if building_id is not None and building_id != floor.building_id:
                raise BadRequestError(
                    f'Floor "{floor.name}" is not in that building; it is in "{building.name}".'
                )
            values["building_id"] = floor.building_id
            return f'floor "{floor.name}" ({building.name}) at "{prop.name}"'
        if building_id is not None:
            building = require(self.session, Building, building_id, "Building")
            if building.property_id != prop.id:
                raise BadRequestError(f'Building "{building.name}" is not part of "{prop.name}".')
            return f'building "{building.name}" at "{prop.name}"'
        if space_id is not None:
            space = self._space(space_id)
            if space.property_id != prop.id:
                raise BadRequestError(f'Space "{space.name}" is not part of "{prop.name}".')
            if space.archived_at is not None:
                raise BadRequestError(
                    f'Space "{space.name}" is archived, so no amenity can be added to it.'
                )
            if space.space_type == SpaceType.FLOOR:
                raise BadRequestError(
                    f'"{space.name}" is the whole-floor space. Add amenities for the whole '
                    "floor to the floor itself."
                )
            return f'space "{space.name}" at "{prop.name}"'
        return f'"{prop.name}"'

    def _describe(self, assignment: PropertyAmenity) -> str:
        prop = self.session.get(Property, assignment.property_id)
        level = level_of(assignment)
        if level == AmenityLevel.SPACE:
            space = self.session.get(Space, assignment.space_id)
            return f'space "{space.name}" at "{prop.name}"'
        if level == AmenityLevel.FLOOR:
            floor = self.session.get(Floor, assignment.floor_id)
            return f'floor "{floor.name}" at "{prop.name}"'
        if level == AmenityLevel.BUILDING:
            building = self.session.get(Building, assignment.building_id)
            return f'building "{building.name}" at "{prop.name}"'
        return f'"{prop.name}"'

    def _space(self, space_id: uuid.UUID) -> Space:
        """Archived spaces included, so they can be named in a clear refusal."""
        space = self.session.get(Space, space_id)
        if space is None:
            raise NotFoundError("Space not found.")
        return space

    def _assignment(self, assignment_id: uuid.UUID) -> PropertyAmenity:
        assignment = self.session.get(PA, assignment_id)
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

    def _log(self, verb: str, amenity_name: str, assignment: PropertyAmenity, where: str) -> None:
        log_activity(
            self.session,
            self.actor,
            action="updated",
            entity_type="property_amenities",
            entity_id=assignment.id,
            property_id=assignment.property_id,
            message=f'Amenity "{amenity_name}" was {verb} {where}.',
        )
