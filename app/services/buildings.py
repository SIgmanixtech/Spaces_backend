import uuid
from typing import Any

from app.models.building import Building, Floor
from app.models.property import Property
from app.services.base import CrudService, require


class BuildingService(CrudService[Building]):
    model = Building
    label = "Building"
    search_fields = ("name",)
    sort_fields = ("name", "created_at")
    default_sort = "name"

    def validate(self, values: dict[str, Any], existing: Building | None) -> None:
        if existing is None:
            require(self.session, Property, values["property_id"], "Property")


class FloorService(CrudService[Floor]):
    model = Floor
    label = "Floor"
    search_fields = ("name",)
    sort_fields = ("level_number", "name", "created_at")
    default_sort = "level_number"

    def validate(self, values: dict[str, Any], existing: Floor | None) -> None:
        if existing is None:
            require(self.session, Building, values["building_id"], "Building")

    def property_id_of(self, record: Floor) -> uuid.UUID | None:
        building = self.session.get(Building, record.building_id)
        return building.property_id if building else None
