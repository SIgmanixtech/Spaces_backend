"""The public catalogue the client app browses: published properties only.

A property is listed when an administrator has published it, it is not archived
and it is not INACTIVE. Its buildings, floors and assigned (non-archived) spaces
are shown with it. Nothing here names other customers, bookings or contracts:
availability says what a space's state is, never whose hold it is.

Prices and availability are the backend's own: prices from the pricing rules
(app.services.pricing), availability from the allocation engine.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import ColumnElement, and_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError, BadRequestError, NotFoundError
from app.models.amenity import Amenity, PropertyAmenity
from app.models.building import Building, Floor
from app.models.enums import (
    AmenityStatus,
    BillingPeriod,
    BookingStatus,
    FileKind,
    LayoutStatus,
    PropertyStatus,
    SpaceOperationalStatus,
    SpaceType,
)
from app.models.file import StoredFile
from app.models.layout import Layout
from app.models.property import Property
from app.models.space import Space
from app.repositories.base import text_search
from app.schemas.client import (
    FloorAvailability,
    FreeFloor,
    FreeSpace,
    FreeSpacePrice,
    FreeSpaces,
    PublicBuilding,
    PublicFloor,
    PublicFloorPlan,
    PublicImage,
    PublicLayout,
    PublicPrice,
    PublicPropertyCard,
    PublicPropertyDetail,
    PublicRule,
    PublicSpace,
    QuoteRead,
    SpaceAvailability,
    StartingPrice,
)
from app.schemas.common import ListQuery
from app.schemas.layout import LayoutElement
from app.schemas.pricing import EffectivePrice
from app.services.allocations import AllocationEngine, booking_period
from app.services.base import page_of
from app.services.files import FileService
from app.services.pricing import PricingService, is_priceable, periods_in
from app.storage import ObjectStorage

# Documents may be contracts or internal papers: never part of a public listing.
PUBLIC_KINDS = [kind for kind in FileKind if kind != FileKind.DOCUMENT]
MAX_LISTED = 2000
SORTS = ("name", "newest", "price")


def listed() -> list[ColumnElement[bool]]:
    return [
        Property.is_published,
        Property.archived_at.is_(None),
        Property.status != PropertyStatus.INACTIVE,
    ]


def _summary(text: str | None) -> str | None:
    if not text:
        return None
    text = " ".join(text.split())
    return text if len(text) <= 240 else text[:237].rstrip() + "…"


def _lowest(prices: list[PublicPrice]) -> FreeSpacePrice | None:
    best = None
    for p in prices:
        if p.estimate is None or p.billing_period == BillingPeriod.CUSTOM:
            continue
        if best is None or p.estimate.recurring_total < best.amount:
            best = FreeSpacePrice(
                billing_period=p.billing_period,
                amount=p.estimate.recurring_total,
                currency=p.estimate.currency,
            )
    return best


def drawn_elements(meta: dict[str, Any] | None) -> list[LayoutElement]:
    """The layout's drawn furniture; anything stored that is not a valid item is left out."""
    found = []
    for item in (meta or {}).get("elements") or []:
        try:
            found.append(LayoutElement.model_validate(item))
        except ValidationError:
            continue
    return found


class CatalogueService:
    def __init__(
        self, session: Session, storage_factory: Callable[[], ObjectStorage] | None = None
    ) -> None:
        self.session = session
        self._storage_factory = storage_factory
        self._storage: ObjectStorage | None | bool = False

    # --- properties --------------------------------------------------------------

    def get_property(self, property_id: uuid.UUID) -> Property:
        prop = self.session.scalar(select(Property).where(Property.id == property_id, *listed()))
        if prop is None:
            raise NotFoundError("Property not found.")
        return prop

    def search(
        self,
        query: ListQuery,
        *,
        city: str | None = None,
        property_type: str | None = None,
        space_type: SpaceType | None = None,
        min_capacity: int | None = None,
        amenity: str | None = None,
        billing_period: BillingPeriod | None = None,
        max_price: float | None = None,
        sort: str = "name",
    ) -> tuple[list[PublicPropertyCard], int]:
        if sort not in SORTS:
            raise BadRequestError(f"sort must be one of: {', '.join(SORTS)}.")
        if sort == "price" and billing_period is None:
            raise BadRequestError("Sorting by price needs a billing_period to compare.")
        order = (Property.created_at.desc(),) if sort == "newest" else (Property.name,)
        statement = select(Property).where(*listed()).order_by(*order, Property.id)
        if query.search:
            statement = statement.where(
                text_search(
                    [Property.name, Property.code, Property.city, Property.state, Property.address],
                    query.search,
                )
            )
        if city:
            statement = statement.where(text_search([Property.city], city))
        if property_type:
            statement = statement.where(Property.property_type == property_type.upper())
        if amenity:
            statement = statement.where(
                select(PropertyAmenity.id)
                .join(Amenity, Amenity.id == PropertyAmenity.amenity_id)
                .where(
                    PropertyAmenity.property_id == Property.id,
                    PropertyAmenity.status == AmenityStatus.ACTIVE,
                    text_search([Amenity.name], amenity),
                )
                .exists()
            )
        props = list(self.session.scalars(statement.limit(MAX_LISTED)))
        cards = self.cards(props)

        def keep(card: PublicPropertyCard) -> bool:
            if space_type and space_type not in card.space_types:
                return False
            if min_capacity and (card.max_capacity or 0) < min_capacity:
                return False
            prices = [
                p
                for p in card.starting_prices
                if not billing_period or p.billing_period == billing_period
            ]
            if billing_period and not prices:
                return False
            return not (max_price is not None and not any(p.amount <= max_price for p in prices))

        kept = [card for card in cards if keep(card)]
        if sort == "price":

            def lowest(card: PublicPropertyCard) -> float:
                return min(
                    (p.amount for p in card.starting_prices if p.billing_period == billing_period),
                    default=float("inf"),
                )

            kept.sort(key=lowest)
        return page_of(kept, replace(query))

    def cards(self, props: Sequence[Property]) -> list[PublicPropertyCard]:
        if not props:
            return []
        ids = [p.id for p in props]
        amenities: dict[uuid.UUID, list[str]] = defaultdict(list)
        for property_id, name in self.session.execute(
            select(PropertyAmenity.property_id, Amenity.name)
            .join(Amenity, Amenity.id == PropertyAmenity.amenity_id)
            .where(
                PropertyAmenity.property_id.in_(ids),
                PropertyAmenity.status == AmenityStatus.ACTIVE,
            )
            .order_by(Amenity.name)
        ):
            amenities[property_id].append(name)
        spaces: dict[uuid.UUID, list[Space]] = defaultdict(list)
        for space in self._public_spaces(Space.property_id.in_(ids)):
            spaces[space.property_id].append(space)
        covers = {
            image.id: image
            for image in self._images(
                StoredFile.property_id.in_(ids),
                StoredFile.is_primary,
                StoredFile.building_id.is_(None),
                StoredFile.layout_id.is_(None),
                StoredFile.space_id.is_(None),
                StoredFile.asset_id.is_(None),
            )
        }
        cover_of = {}
        for record in self.session.scalars(
            select(StoredFile).where(StoredFile.id.in_(covers.keys()))
        ):
            cover_of[record.property_id] = covers[record.id]
        out = []
        for prop in props:
            rentable = [s for s in spaces[prop.id] if is_priceable(s)]
            starting, priced = self._pricing_summary(prop)
            bookable = [
                s
                for s in rentable
                if s.id in priced
                and s.operational_status == SpaceOperationalStatus.ACTIVE
                and prop.status == PropertyStatus.ACTIVE
            ]
            out.append(
                PublicPropertyCard(
                    id=prop.id,
                    name=prop.name,
                    code=prop.code,
                    property_type=prop.property_type,
                    category=prop.category,
                    city=prop.city,
                    state=prop.state,
                    locality=None,
                    summary=_summary(prop.description),
                    amenities=amenities[prop.id],
                    cover=cover_of.get(prop.id),
                    space_types=sorted({SpaceType(s.space_type) for s in rentable}),
                    max_capacity=max((s.capacity or 0 for s in rentable), default=0) or None,
                    starting_prices=starting,
                    offer="BOOKABLE" if bookable else "ENQUIRY",
                    bookable_spaces=len(bookable),
                )
            )
        return out

    def detail(self, property_id: uuid.UUID) -> PublicPropertyDetail:
        prop = self.get_property(property_id)
        card = self.cards([prop])[0]
        buildings = list(
            self.session.scalars(
                select(Building).where(Building.property_id == prop.id).order_by(Building.name)
            )
        )
        floors: dict[uuid.UUID, list[Floor]] = defaultdict(list)
        if buildings:
            for floor in self.session.scalars(
                select(Floor)
                .where(Floor.building_id.in_([b.id for b in buildings]))
                .order_by(Floor.level_number)
            ):
                floors[floor.building_id].append(floor)
        on_floor: dict[uuid.UUID, list[Space]] = defaultdict(list)
        for space in self._public_spaces(Space.property_id == prop.id):
            on_floor[space.floor_id].append(space)  # type: ignore[index]
        _, priced = self._pricing_summary(prop)
        images = self._images(
            StoredFile.property_id == prop.id,
            StoredFile.layout_id.is_(None),
            StoredFile.space_id.is_(None),
            StoredFile.asset_id.is_(None),
        )
        return PublicPropertyDetail(
            **card.model_dump(),
            description=prop.description,
            address=prop.address,
            postal_code=prop.postal_code,
            latitude=float(prop.latitude) if prop.latitude is not None else None,
            longitude=float(prop.longitude) if prop.longitude is not None else None,
            area_sqft=float(prop.area_sqft) if prop.area_sqft is not None else None,
            bookable=prop.status == PropertyStatus.ACTIVE,
            buildings=[
                PublicBuilding(
                    id=b.id,
                    name=b.name,
                    description=b.description,
                    floors=[self._floor_summary(f, on_floor[f.id], priced) for f in floors[b.id]],
                )
                for b in buildings
            ],
            images=images,
        )

    # --- floors ------------------------------------------------------------------

    def floor_plan(self, property_id: uuid.UUID, floor_id: uuid.UUID) -> PublicFloorPlan:
        prop = self.get_property(property_id)
        floor, building = self._floor(prop, floor_id)
        spaces = self._public_spaces(Space.floor_id == floor.id)
        layouts = list(
            self.session.scalars(
                select(Layout)
                .where(Layout.floor_id == floor.id, Layout.status != LayoutStatus.ARCHIVED)
                .order_by(Layout.name)
            )
        )
        plans: dict[uuid.UUID, list[PublicImage]] = defaultdict(list)
        photos: dict[uuid.UUID, list[PublicImage]] = defaultdict(list)
        if layouts:
            for image in self._images(
                StoredFile.layout_id.in_([lay.id for lay in layouts]),
                StoredFile.kind == FileKind.FLOOR_PLAN,
            ):
                plans[image.layout_id].append(image)  # type: ignore[index]
        if spaces:
            for image in self._images(
                StoredFile.space_id.in_([s.id for s in spaces]),
                StoredFile.kind != FileKind.FLOOR_PLAN,
            ):
                photos[image.space_id].append(image)  # type: ignore[index]
        prices = self._prices(prop)
        return PublicFloorPlan(
            property_id=prop.id,
            building_id=building.id,
            building_name=building.name,
            floor=PublicFloor(id=floor.id, name=floor.name, level_number=floor.level_number),
            layouts=[
                PublicLayout(
                    id=lay.id,
                    name=lay.name,
                    grid_columns=lay.grid_columns,
                    grid_rows=lay.grid_rows,
                    plans=plans[lay.id],
                    elements=drawn_elements(lay.meta),
                )
                for lay in layouts
            ],
            spaces=[
                PublicSpace(
                    id=s.id,
                    name=s.name,
                    code=s.code,
                    space_type=SpaceType(s.space_type),
                    parent_space_id=s.parent_space_id,
                    depth=s.depth,
                    layout_id=s.layout_id,
                    area_sqft=float(s.area_sqft) if s.area_sqft is not None else None,
                    capacity=s.capacity,
                    is_rentable=is_priceable(s),
                    bookable=is_priceable(s)
                    and s.operational_status == SpaceOperationalStatus.ACTIVE
                    and bool(prices.get(s.id)),
                    x=s.x,
                    y=s.y,
                    width=s.width,
                    height=s.height,
                )
                for s in spaces
            ],
            prices={sid: p for sid, p in prices.items() if sid in {s.id for s in spaces}},
            images=dict(photos),
        )

    def floor_availability(
        self, property_id: uuid.UUID, floor_id: uuid.UUID, start_at: datetime, end_at: datetime
    ) -> FloorAvailability:
        prop = self.get_property(property_id)
        floor, _ = self._floor(prop, floor_id)
        self._check_range(start_at, end_at)
        spaces = self._public_spaces(Space.floor_id == floor.id)
        by_id = {s.id: s for s in spaces}
        return FloorAvailability(
            start_at=start_at,
            end_at=end_at,
            spaces=[self._status(prop, s, by_id, start_at, end_at) for s in spaces],
        )

    def free_spaces(
        self,
        start_at: datetime,
        end_at: datetime,
        *,
        space_type: SpaceType | None = None,
        min_capacity: int | None = None,
        city: str | None = None,
        search: str | None = None,
    ) -> FreeSpaces:
        """Every rentable space free for the whole range, on every listed property, grouped
        by floor. A space held itself, or inside a held floor or portion, is left out."""
        self._check_range(start_at, end_at)
        statement = select(Property).where(*listed()).order_by(Property.name, Property.id)
        if city:
            statement = statement.where(Property.city.ilike(city.strip()))
        if search:
            statement = statement.where(
                text_search([Property.name, Property.city, Property.address], search)
            )
        floors: list[FreeFloor] = []
        for prop in self.session.scalars(statement):
            if prop.status != PropertyStatus.ACTIVE:
                continue
            prices = self._prices(prop)
            buildings = {
                b.id: b
                for b in self.session.scalars(
                    select(Building).where(Building.property_id == prop.id)
                )
            }
            if not buildings:
                continue
            for floor in self.session.scalars(
                select(Floor)
                .where(Floor.building_id.in_(buildings))
                .order_by(Floor.level_number, Floor.name)
            ):
                spaces = self._public_spaces(Space.floor_id == floor.id)
                by_id = {s.id: s for s in spaces}
                free = []
                for s in spaces:
                    if not is_priceable(s):
                        continue
                    if space_type is not None and s.space_type != space_type:
                        continue
                    if min_capacity is not None and (s.capacity or 0) < min_capacity:
                        continue
                    if not self._status(prop, s, by_id, start_at, end_at).available:
                        continue
                    parent = by_id.get(s.parent_space_id) if s.parent_space_id else None
                    free.append(
                        FreeSpace(
                            id=s.id,
                            name=s.name,
                            space_type=SpaceType(s.space_type),
                            capacity=s.capacity,
                            area_sqft=float(s.area_sqft) if s.area_sqft is not None else None,
                            inside=parent.name
                            if parent is not None and parent.space_type != SpaceType.FLOOR
                            else None,
                            bookable=any(
                                p.billing_period != BillingPeriod.CUSTOM
                                for p in prices.get(s.id, [])
                            ),
                            from_price=_lowest(prices.get(s.id, [])),
                        )
                    )
                if free:
                    building = buildings[floor.building_id]
                    floors.append(
                        FreeFloor(
                            property_id=prop.id,
                            property_name=prop.name,
                            city=prop.city,
                            building_id=building.id,
                            building_name=building.name,
                            floor_id=floor.id,
                            floor_name=floor.name,
                            level_number=floor.level_number,
                            spaces=free,
                        )
                    )
        return FreeSpaces(
            start_at=start_at,
            end_at=end_at,
            floors=floors,
            total=sum(len(f.spaces) for f in floors),
        )

    def space_availability(
        self, space_id: uuid.UUID, start_at: datetime, end_at: datetime
    ) -> SpaceAvailability:
        space, prop = self.get_space(space_id)
        self._check_range(start_at, end_at)
        on_floor = {s.id: s for s in self._public_spaces(Space.floor_id == space.floor_id)}
        return self._status(prop, space, on_floor, start_at, end_at)

    def quote(
        self, space_id: uuid.UUID, period: BillingPeriod, start_at: datetime, end_at: datetime
    ) -> QuoteRead:
        space, _ = self.get_space(space_id)
        quantity = periods_in(period, start_at, end_at)
        price, estimate = PricingService(self.session).estimate(
            space.id, period, on=self._business_day(start_at), quantity=quantity
        )
        return QuoteRead(
            space_id=space.id,
            billing_period=period,
            quantity=float(quantity),
            price=_public_price(price),
            estimate=estimate,
            availability=self.space_availability(space.id, start_at, end_at),
        )

    def get_space(self, space_id: uuid.UUID) -> tuple[Space, Property]:
        space = self.session.scalar(
            select(Space)
            .join(Property, Property.id == Space.property_id)
            .where(
                Space.id == space_id,
                Space.archived_at.is_(None),
                Space.floor_id.is_not(None),
                *listed(),
            )
        )
        if space is None:
            raise NotFoundError("Space not found.")
        return space, self.session.get(Property, space.property_id)  # type: ignore[return-value]

    # --- helpers -----------------------------------------------------------------

    def _floor(self, prop: Property, floor_id: uuid.UUID) -> tuple[Floor, Building]:
        row = self.session.execute(
            select(Floor, Building)
            .join(Building, Building.id == Floor.building_id)
            .where(
                Floor.id == floor_id,
                Building.property_id == prop.id,
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("Floor not found.")
        return row[0], row[1]

    def _public_spaces(self, *where: ColumnElement[bool]) -> list[Space]:
        return list(
            self.session.scalars(
                select(Space)
                .where(Space.archived_at.is_(None), Space.floor_id.is_not(None), *where)
                .order_by(Space.depth, Space.name)
            )
        )

    @staticmethod
    def _check_range(start_at: datetime, end_at: datetime) -> None:
        if start_at.tzinfo is None or end_at.tzinfo is None:
            raise BadRequestError("Give times with a time zone, e.g. 2026-11-02T09:00:00+05:30.")
        if end_at <= start_at:
            raise BadRequestError("The end must be after the start.")
        if (end_at - start_at).days > 366:
            raise BadRequestError("Check at most 366 days at a time.")

    @staticmethod
    def _business_day(moment: datetime):  # type: ignore[no-untyped-def]
        return moment.astimezone(ZoneInfo(get_settings().business_timezone)).date()

    def _status(
        self,
        prop: Property,
        space: Space,
        on_floor: dict[uuid.UUID, Space],
        start_at: datetime,
        end_at: datetime,
    ) -> SpaceAvailability:
        def answer(status: str, message: str) -> SpaceAvailability:
            return SpaceAvailability(
                space_id=space.id,
                status=status,  # type: ignore[arg-type]
                available=status == "AVAILABLE",
                message=message,
            )

        if not is_priceable(space):
            return answer("UNAVAILABLE", "This space is not offered for rent.")
        if prop.status != PropertyStatus.ACTIVE:
            return answer("UNAVAILABLE", "The property is not open for bookings.")
        parent: Space | None = space
        seen: set[uuid.UUID] = set()
        while parent is not None and parent.id not in seen:
            seen.add(parent.id)
            if parent.operational_status != SpaceOperationalStatus.ACTIVE:
                return answer("UNAVAILABLE", "This space is not open for bookings.")
            parent = on_floor.get(parent.parent_space_id) if parent.parent_space_id else None
        conflicts = AllocationEngine(self.session).conflicts(
            space.id, booking_period(start_at, end_at)
        )
        if not conflicts:
            return answer("AVAILABLE", "Available for the whole of that time.")
        direct = [c for c in conflicts if c.relation in ("same", "contains")]
        if not direct:
            return answer(
                "UNAVAILABLE", "Part of this space is already taken for some of that time."
            )
        hold = direct[0]
        where = "" if hold.relation == "same" else f' (as part of "{hold.space_name}")'
        if hold.kind == "CONTRACT":
            return answer("OCCUPIED", f"Occupied under a lease for some of that time{where}.")
        if hold.status == BookingStatus.PENDING:
            return answer(
                "RESERVED", f"Reserved, awaiting confirmation, for some of that time{where}."
            )
        return answer("BOOKED", f"Booked for some of that time{where}.")

    def _prices(self, prop: Property) -> dict[uuid.UUID, list[PublicPrice]]:
        pricing = PricingService(self.session).effective(prop.id)
        return {
            sp.space_id: [_public_price(p) for p in sp.prices] for sp in pricing.spaces if sp.prices
        }

    def _floor_summary(
        self, floor: Floor, spaces: list[Space], priced: set[uuid.UUID]
    ) -> PublicFloor:
        rentable = [s for s in spaces if is_priceable(s)]
        bookable = [
            s
            for s in rentable
            if s.id in priced and s.operational_status == SpaceOperationalStatus.ACTIVE
        ]
        return PublicFloor(
            id=floor.id,
            name=floor.name,
            level_number=floor.level_number,
            spaces=len([s for s in spaces if s.space_type != SpaceType.FLOOR]),
            rentable_spaces=len(rentable),
            bookable_spaces=len(bookable),
            space_types=sorted({SpaceType(s.space_type) for s in rentable}),
        )

    def _pricing_summary(self, prop: Property) -> tuple[list[StartingPrice], set[uuid.UUID]]:
        """Lowest one-period total per billing period, and the spaces priced for a period
        that can be booked online (not CUSTOM)."""
        best: dict[BillingPeriod, StartingPrice] = {}
        priced: set[uuid.UUID] = set()
        pricing = PricingService(self.session).effective(prop.id)
        for sp in pricing.spaces:
            if any(p.billing_period != BillingPeriod.CUSTOM for p in sp.prices):
                priced.add(sp.space_id)
            for price in sp.prices:
                if price.estimate is None:
                    continue
                current = best.get(price.billing_period)
                if current is None or price.estimate.recurring_total < current.amount:
                    best[price.billing_period] = StartingPrice(
                        billing_period=price.billing_period,
                        amount=price.estimate.recurring_total,
                        currency=price.estimate.currency,
                        space_type=price.space_type,
                    )
        order = list(BillingPeriod)
        ranked = sorted(best.values(), key=lambda s: order.index(s.billing_period))
        return ranked, priced

    def _images(self, *where: ColumnElement[bool]) -> list[PublicImage]:
        storage = self._lazy_storage()
        if storage is None:
            return []
        records = list(
            self.session.scalars(
                select(StoredFile)
                .where(StoredFile.kind.in_(PUBLIC_KINDS), and_(*where))
                .order_by(StoredFile.is_primary.desc(), StoredFile.sort_order, StoredFile.id)
            )
        )
        try:
            reads = FileService(self.session, storage).to_read(records)
        except AppError:
            return []
        return [PublicImage.model_validate(r.model_dump()) for r in reads]

    def _lazy_storage(self) -> ObjectStorage | None:
        if self._storage is False:
            try:
                self._storage = self._storage_factory() if self._storage_factory else None
            except AppError:
                self._storage = None  # storage not configured: listings show no pictures
        return self._storage  # type: ignore[return-value]


def _public_price(price: EffectivePrice) -> PublicPrice:
    return PublicPrice(
        billing_period=price.billing_period,
        source=price.source,
        rule=PublicRule.model_validate(price.rule.model_dump()),
        estimate=price.estimate,
    )


__all__ = ["CatalogueService", "listed"]
