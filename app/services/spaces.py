"""Spaces and their place in the inventory: property -> building -> floor -> space.

On a floor, one space of type FLOOR is the root (the whole floor) and every
other space has a parent on the same floor. Spaces created before floors were
tracked have neither and are "unassigned" until an admin moves them onto a
floor. The database enforces the shape (see ``app.models.space``); this module
decides where a space may go and keeps ``depth`` consistent.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ColumnElement, exists, or_, select

from app.core.errors import BadRequestError, ConflictError
from app.models.booking import Booking
from app.models.building import Building, Floor
from app.models.enums import SpaceType
from app.models.layout import Layout
from app.models.property import Property
from app.models.space import Space
from app.models.tenant import TenantOccupancy
from app.schemas.space import SpaceRead
from app.services.base import CrudService, commit, require
from app.services.locations import check_layout
from app.services.occupancy import (
    OPEN_BOOKING_STATUSES,
    business_today,
    is_rentable_unit,
    occupancy_covers,
    space_statuses,
)

MAX_DEPTH = 4

_FLOOR_OR_PORTION = frozenset({SpaceType.FLOOR, SpaceType.PORTION})
# What each type of space may sit directly inside.
ALLOWED_PARENTS: dict[SpaceType, frozenset[SpaceType]] = {
    SpaceType.FLOOR: frozenset(),
    SpaceType.PORTION: frozenset({SpaceType.FLOOR}),
    SpaceType.PRIVATE_OFFICE: _FLOOR_OR_PORTION,
    SpaceType.COWORKING: _FLOOR_OR_PORTION,
    SpaceType.CABIN: _FLOOR_OR_PORTION,
    SpaceType.MEETING_ROOM: _FLOOR_OR_PORTION,
    SpaceType.CONFERENCE_ROOM: _FLOOR_OR_PORTION,
    SpaceType.TRAINING_ROOM: _FLOOR_OR_PORTION,
    SpaceType.NON_RENTABLE: _FLOOR_OR_PORTION,
    SpaceType.DESK: _FLOOR_OR_PORTION | {SpaceType.PRIVATE_OFFICE, SpaceType.COWORKING},
}


def _label(space_type: SpaceType) -> str:
    if space_type == SpaceType.FLOOR:
        return "whole-floor space"
    return space_type.lower().replace("_", " ")


def _check_parent_type(child: SpaceType, parent: SpaceType) -> None:
    if parent not in ALLOWED_PARENTS[child]:
        raise BadRequestError(f"A {_label(child)} cannot be placed inside a {_label(parent)}.")


class SpaceService(CrudService[Space]):
    model = Space
    label = "Space"
    search_fields = ("name", "code")
    sort_fields = ("name", "code", "space_type", "capacity", "created_at", "updated_at")
    default_sort = "name"

    # --- validation ------------------------------------------------------

    def validate(self, values: dict[str, Any], existing: Space | None) -> None:
        if existing is None:
            require(self.session, Property, values["property_id"], "Property")
            self._place_new(values)
        else:
            self._check_reclassification(values, existing)
        check_layout(self.session, values["property_id"], values.get("layout_id"))
        self._check_layout_floor(values.get("layout_id"), values.get("floor_id"))
        if values.get("price_amount") is not None and values.get("price_unit") is None:
            raise BadRequestError("A price needs a price_unit (HOUR, DAY or MONTH).")
        if values["space_type"] == SpaceType.NON_RENTABLE and values["is_rentable"]:
            raise BadRequestError("A non-rentable space cannot be marked rentable.")

    def _place_new(self, values: dict[str, Any]) -> None:
        """Fill in the hierarchy columns of a space being created."""
        space_type: SpaceType = values["space_type"]
        if values.get("is_rentable") is None:
            values["is_rentable"] = space_type != SpaceType.NON_RENTABLE
        floor_id, parent_id = values.get("floor_id"), values.get("parent_space_id")
        if floor_id is None and parent_id is None:
            if space_type == SpaceType.FLOOR:
                raise BadRequestError("A whole-floor space needs a floor_id.")
            values["depth"] = 0
            return  # unassigned, as every space was before floors were tracked
        values.update(self._placement(values["property_id"], space_type, floor_id, parent_id))

    def _placement(
        self,
        property_id: uuid.UUID,
        space_type: SpaceType,
        floor_id: uuid.UUID | None,
        parent_id: uuid.UUID | None,
    ) -> dict[str, Any]:
        """Where a space of this type lands for the requested floor and/or parent."""
        if parent_id is not None:
            parent = require(self.session, Space, parent_id, "Parent space")
            if parent.property_id != property_id:
                raise BadRequestError("The parent space does not belong to this property.")
            if parent.floor_id is None:
                raise BadRequestError("The parent space is not assigned to a floor yet.")
            if floor_id is not None and floor_id != parent.floor_id:
                raise BadRequestError("The parent space is on a different floor.")
            if space_type == SpaceType.FLOOR:
                raise BadRequestError("A whole-floor space cannot have a parent.")
            return self._under(parent, space_type)

        floor = require(self.session, Floor, floor_id, "Floor")  # type: ignore[arg-type]
        building = self.session.get(Building, floor.building_id)
        if building is None or building.property_id != property_id:
            raise BadRequestError("The floor does not belong to this property.")
        if space_type == SpaceType.FLOOR:
            return {
                "building_id": building.id,
                "floor_id": floor.id,
                "parent_space_id": None,
                "depth": 0,
                "parent_depth": None,
            }
        root = self.session.scalar(
            self.repository.select().where(
                Space.floor_id == floor.id, Space.space_type == SpaceType.FLOOR
            )
        )
        if root is None:
            raise ConflictError(
                "This floor has no whole-floor space yet. Create a space of type FLOOR on it first."
            )
        return self._under(root, space_type)

    def _under(self, parent: Space, space_type: SpaceType) -> dict[str, Any]:
        _check_parent_type(space_type, SpaceType(parent.space_type))
        return {
            "building_id": parent.building_id,
            "floor_id": parent.floor_id,
            "parent_space_id": parent.id,
            "depth": parent.depth + 1,
            "parent_depth": parent.depth,
        }

    def _check_reclassification(self, values: dict[str, Any], existing: Space) -> None:
        new_type, old_type = SpaceType(values["space_type"]), SpaceType(existing.space_type)
        if new_type == old_type:
            return
        if SpaceType.FLOOR in (new_type, old_type):
            raise BadRequestError(
                "A whole-floor space cannot become another type, and no space can become one."
            )
        if existing.parent_space_id is not None:
            parent = self.session.get(Space, existing.parent_space_id)
            _check_parent_type(new_type, SpaceType(parent.space_type))  # type: ignore[union-attr]
        child_types = self.session.scalars(
            self.repository.select()
            .where(Space.parent_space_id == existing.id)
            .with_only_columns(Space.space_type)
            .distinct()
        )
        for child_type in child_types:
            _check_parent_type(SpaceType(child_type), new_type)

    def _check_layout_floor(self, layout_id: uuid.UUID | None, floor_id: uuid.UUID | None) -> None:
        if layout_id is None or floor_id is None:
            return
        layout = self.session.get(Layout, layout_id)
        if layout is not None and layout.floor_id is not None and layout.floor_id != floor_id:
            raise BadRequestError("The layout belongs to a different floor than the space.")

    # --- writes ----------------------------------------------------------

    def update(self, record_id: uuid.UUID, values: dict[str, Any]) -> Space:
        if values.get("space_type") == SpaceType.NON_RENTABLE and "is_rentable" not in values:
            values = {**values, "is_rentable": False}
        return super().update(record_id, values)

    def move(
        self, space_id: uuid.UUID, floor_id: uuid.UUID | None, parent_id: uuid.UUID | None
    ) -> Space:
        """Put a space, and everything inside it, under a new parent or onto a floor."""
        space = self.get(space_id)
        # Structural changes within a property run one at a time.
        self.session.execute(
            select(Property.id).where(Property.id == space.property_id).with_for_update()
        )
        self.session.refresh(space)
        if space.space_type == SpaceType.FLOOR:
            raise BadRequestError("A whole-floor space cannot be moved.")

        subtree = self._subtree(space.id)
        if parent_id in {member.id for member in subtree}:
            raise BadRequestError("A space cannot be moved inside itself.")
        target = self._placement(
            space.property_id, SpaceType(space.space_type), floor_id, parent_id
        )
        if target["parent_space_id"] == space.parent_space_id:
            return space
        shift = target["depth"] - space.depth
        if max(member.depth for member in subtree) + shift > MAX_DEPTH:
            raise BadRequestError("The move would nest spaces too deeply.")
        self._check_layout_floor(space.layout_id, target["floor_id"])
        # A space that was never on a floor has no hierarchy to disturb, so
        # assigning it is always allowed; moving an assigned one is not while
        # it, or anything inside it, is committed to someone.
        if space.floor_id is not None and self._has_commitments([member.id for member in subtree]):
            raise ConflictError(
                "The space, or a space inside it, has open bookings or a current or "
                "upcoming occupancy, so it cannot be moved."
            )

        for member in subtree:
            member.building_id = target["building_id"]
            member.floor_id = target["floor_id"]
            if member.id != space.id:
                member.parent_depth += shift  # type: ignore[operator]
            member.depth += shift
        space.parent_space_id = target["parent_space_id"]
        space.parent_depth = target["parent_depth"]
        self._log("moved", space)
        commit(self.session)
        return space

    def before_delete(self, record: Space) -> None:
        now = datetime.now(UTC)
        if self.session.scalar(
            select(exists().where(Space.parent_space_id == record.id, Space.archived_at.is_(None)))
        ):
            raise ConflictError("The space contains other spaces. Archive or move them first.")
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

    # --- helpers ---------------------------------------------------------

    def _subtree(self, space_id: uuid.UUID) -> Sequence[Space]:
        """The space and everything inside it, archived spaces included."""
        tree = select(Space.id).where(Space.id == space_id).cte(recursive=True)
        tree = tree.union_all(select(Space.id).where(Space.parent_space_id == tree.c.id))
        return self.session.scalars(select(Space).where(Space.id.in_(select(tree.c.id)))).all()

    def _has_commitments(self, space_ids: list[uuid.UUID]) -> bool:
        now = datetime.now(UTC)
        booked = exists().where(
            Booking.space_id.in_(space_ids),
            Booking.status.in_(OPEN_BOOKING_STATUSES),
            Booking.end_at > now,
        )
        occupied = exists().where(
            TenantOccupancy.space_id.in_(space_ids),
            or_(
                TenantOccupancy.end_date.is_(None),
                TenantOccupancy.end_date >= business_today(now),
            ),
        )
        return bool(self.session.scalar(select(or_(booked, occupied))))

    # --- reads -----------------------------------------------------------

    @staticmethod
    def listing_conditions(
        *, include_structural: bool, unassigned: bool | None
    ) -> list[ColumnElement[bool]]:
        conditions: list[ColumnElement[bool]] = []
        if not include_structural:
            conditions.append(is_rentable_unit())
        if unassigned is not None:
            conditions.append(
                Space.floor_id.is_(None) if unassigned else Space.floor_id.is_not(None)
            )
        return conditions

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
