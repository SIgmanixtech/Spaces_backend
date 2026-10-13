import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select

from app.models.building import Building, Floor
from app.models.enums import LayoutStatus
from app.models.layout import Layout
from app.models.property import Property
from app.schemas.layout import LayoutRead
from app.services.base import CrudService, commit, require
from app.services.locations import check_building_and_floor


class LayoutService(CrudService[Layout]):
    model = Layout
    label = "Layout"
    search_fields = ("name", "description")
    sort_fields = ("name", "status", "created_at", "updated_at")

    def validate(self, values: dict[str, Any], existing: Layout | None) -> None:
        if existing is None:
            require(self.session, Property, values["property_id"], "Property")
        check_building_and_floor(
            self.session, values["property_id"], values.get("building_id"), values.get("floor_id")
        )

    def create(self, values: dict[str, Any]) -> Layout:
        return super().create(_rename_metadata(values))

    def update(self, record_id: uuid.UUID, values: dict[str, Any]) -> Layout:
        return super().update(record_id, _rename_metadata(values))

    def delete(self, record_id: uuid.UUID) -> None:
        """Archive the layout. Its spaces keep pointing at it for history."""
        layout = self.get(record_id)
        layout.status = LayoutStatus.ARCHIVED
        self._log("archived", layout)
        commit(self.session)

    def to_read(self, layouts: Sequence[Layout]) -> list[LayoutRead]:
        building_ids = {layout.building_id for layout in layouts if layout.building_id}
        floor_ids = {layout.floor_id for layout in layouts if layout.floor_id}
        buildings = (
            dict(
                self.session.execute(
                    select(Building.id, Building.name).where(Building.id.in_(building_ids))
                ).all()
            )
            if building_ids
            else {}
        )
        floors = (
            dict(
                self.session.execute(
                    select(Floor.id, Floor.name).where(Floor.id.in_(floor_ids))
                ).all()
            )
            if floor_ids
            else {}
        )
        return [
            LayoutRead(
                id=layout.id,
                property_id=layout.property_id,
                building_id=layout.building_id,
                building_name=buildings.get(layout.building_id),
                floor_id=layout.floor_id,
                floor_name=floors.get(layout.floor_id),
                name=layout.name,
                description=layout.description,
                grid_columns=layout.grid_columns,
                grid_rows=layout.grid_rows,
                metadata=layout.meta,
                status=layout.status,
                created_at=layout.created_at,
                updated_at=layout.updated_at,
            )
            for layout in layouts
        ]


def _rename_metadata(values: dict[str, Any]) -> dict[str, Any]:
    # The API calls it "metadata"; the ORM attribute is "meta".
    if "metadata" in values:
        values = {**values, "meta": values["metadata"]}
        del values["metadata"]
    return values
