"""Checks that a building, floor, layout or space belongs where the request says."""

import uuid

from sqlalchemy.orm import Session

from app.core.errors import BadRequestError
from app.models.building import Building, Floor
from app.models.layout import Layout
from app.models.space import Space
from app.services.base import require


def check_building_and_floor(
    session: Session,
    property_id: uuid.UUID,
    building_id: uuid.UUID | None,
    floor_id: uuid.UUID | None,
) -> None:
    if floor_id is not None and building_id is None:
        raise BadRequestError("A floor can only be set together with its building.")
    if building_id is not None:
        building = require(session, Building, building_id, "Building")
        if building.property_id != property_id:
            raise BadRequestError("The building does not belong to this property.")
    if floor_id is not None:
        floor = require(session, Floor, floor_id, "Floor")
        if floor.building_id != building_id:
            raise BadRequestError("The floor does not belong to this building.")


def check_layout(session: Session, property_id: uuid.UUID, layout_id: uuid.UUID | None) -> None:
    if layout_id is not None:
        layout = require(session, Layout, layout_id, "Layout")
        if layout.property_id != property_id:
            raise BadRequestError("The layout does not belong to this property.")


def check_space(session: Session, property_id: uuid.UUID, space_id: uuid.UUID | None) -> None:
    if space_id is not None:
        space = require(session, Space, space_id, "Space")
        if space.property_id != property_id:
            raise BadRequestError("The space does not belong to this property.")
