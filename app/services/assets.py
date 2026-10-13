from collections.abc import Sequence
from typing import Any

from sqlalchemy import select

from app.models.asset import Asset
from app.models.property import Property
from app.schemas.asset import AssetRead
from app.services.base import CrudService, require
from app.services.locations import check_building_and_floor, check_space


class AssetService(CrudService[Asset]):
    model = Asset
    label = "Asset"
    search_fields = ("name", "category", "asset_tag", "location")
    sort_fields = ("name", "category", "status", "purchase_date", "created_at", "updated_at")

    def validate(self, values: dict[str, Any], existing: Asset | None) -> None:
        if existing is None:
            require(self.session, Property, values["property_id"], "Property")
        check_building_and_floor(
            self.session, values["property_id"], values.get("building_id"), values.get("floor_id")
        )
        check_space(self.session, values["property_id"], values.get("space_id"))

    def to_read(self, assets: Sequence[Asset]) -> list[AssetRead]:
        property_ids = {asset.property_id for asset in assets}
        names = (
            dict(
                self.session.execute(
                    select(Property.id, Property.name).where(Property.id.in_(property_ids))
                ).all()
            )
            if property_ids
            else {}
        )
        return [
            AssetRead.model_validate(
                {
                    **{
                        column.key: getattr(asset, column.key) for column in Asset.__table__.columns
                    },
                    "property_name": names[asset.property_id],
                }
            )
            for asset in assets
        ]
