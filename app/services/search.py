"""Natural-language space search: a sentence becomes filters (built-in parser, or Claude when
configured), and the backend finds the spaces. Every result is a real listed space; prices
are the approved prices; availability comes from the allocation engine for the requested
time, so a space inside a held floor or portion is never shown as free. Nothing is
reserved by searching.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.booking import Booking
from app.models.building import Building, Floor
from app.models.enums import PropertyStatus, SpaceOperationalStatus, SpaceType
from app.models.property import Property
from app.models.space import Space
from app.models.tenant import Tenant, TenantOccupancy
from app.models.user import AppUser
from app.schemas.search import SearchIntent, SearchPrice, SearchResponse, SearchResult
from app.services.amenities import effective_names_by_space, offered_names
from app.services.catalogue import CatalogueService, listed
from app.services.pricing import is_priceable
from app.services.search_ai import CustomerContext, extract_with_ai
from app.services.search_parser import UNIT_TO_PERIOD, parse
from app.storage import ObjectStorage

TYPE_WORDS = {
    SpaceType.CABIN: "cabin",
    SpaceType.PRIVATE_OFFICE: "private office",
    SpaceType.MEETING_ROOM: "meeting room",
    SpaceType.CONFERENCE_ROOM: "conference room",
    SpaceType.TRAINING_ROOM: "training room",
    SpaceType.COWORKING: "coworking area",
    SpaceType.DEDICATED_WORKSTATION: "dedicated workstation",
    SpaceType.FLEXIBLE_WORKSTATION: "hot desk",
    SpaceType.DESK: "desk",
    SpaceType.SEAT: "seat",
    SpaceType.PARKING_SLOT: "parking slot",
    SpaceType.PORTION: "portion",
    SpaceType.FLOOR: "whole floor",
}


def _add_months(day: date, months: int) -> date:
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    from calendar import monthrange

    return date(year, month, min(day.day, monthrange(year, month)[1]))


class SpaceSearch:
    def __init__(
        self,
        session: Session,
        settings: Settings,
        storage_factory: Callable[[], ObjectStorage] | None = None,
        today: date | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.catalogue = CatalogueService(session, storage_factory)
        self.tz = ZoneInfo(settings.business_timezone)
        self.today = today or datetime.now(self.tz).date()

    def search(
        self,
        query: str,
        page: int = 1,
        page_size: int = 20,
        *,
        previous: tuple[str, SearchIntent | None] | None = None,
        customer: CustomerContext | None = None,
    ) -> SearchResponse:
        query = " ".join(query.split())[:300]
        props = list(
            self.session.scalars(
                select(Property)
                .where(*listed(), Property.status == PropertyStatus.ACTIVE)
                .order_by(Property.name)
            )
        )
        places = self._places(props)
        # Offered at any level, active, not on archived spaces (BR-031).
        amenity_names = offered_names(self.session, [p.id for p in props])

        notice = None
        summary = None
        used_context: list[str] = []
        interpreted_by = "rules"
        rules_intent, problems = parse(
            query, today=self.today, known_places=places, known_amenities=amenity_names
        )
        intent = rules_intent
        ai = extract_with_ai(
            query,
            today=self.today,
            settings=self.settings,
            places=places,
            amenities=amenity_names,
            previous=previous,
            customer=customer,
        )
        if ai is not None:
            intent = self._ground(ai.intent, places, amenity_names)
            interpreted_by = "ai"
            summary = ai.summary or None
            problems = []
            if ai.uses_previous_search and previous is not None:
                used_context.append("your previous search")
            if ai.uses_customer_history:
                used_context.append("your recent bookings")
        else:
            if self.settings.search_ai_enabled and self.settings.anthropic_api_key is not None:
                notice = "The AI interpreter was unavailable, so the built-in reader was used."
            if previous is not None and previous[1] is not None and is_follow_up(query):
                intent = merge_follow_up(previous[1], rules_intent)
                used_context.append("your previous search")

        clarifications = list(problems)
        start_at, end_at, window_question = self._window(intent)
        if window_question:
            clarifications.append(window_question)
        if intent.unknown_locations:
            cities = sorted({p.city for p in props})
            clarifications.append(
                f"No listed property is in {', '.join(intent.unknown_locations)}. "
                f"Places with listed spaces: {', '.join(cities) or 'none yet'}."
            )
        if intent.unknown_amenities:
            clarifications.append(
                f"No property lists {', '.join(intent.unknown_amenities)} as an amenity."
            )
        if not any(
            (
                intent.resource_types,
                intent.capacity_min,
                intent.locations,
                intent.date,
                intent.budget_max,
                intent.area_min_sqft,
                intent.area_max_sqft,
                intent.amenities,
                intent.unknown_locations,
            )
        ):
            clarifications.append(
                "Tell us the kind of space, how many people, where, or when, e.g. "
                '"a cabin for 4 people in Hyderabad tomorrow".'
            )

        results: list[SearchResult] = []
        if not (intent.unknown_locations and not intent.locations):
            results = self._find(props, intent, start_at, end_at)
        total = len(results)
        page_items = results[(page - 1) * page_size : page * page_size]
        covers = self._covers(page_items, props)
        for item in page_items:
            item.image = covers.get(item.property_id)
        suggestions = []
        if not results:
            if intent.resource_types:
                suggestions.append("Try another kind of space or remove the space type.")
            if intent.capacity_min:
                suggestions.append("Try fewer people.")
            if intent.budget_max:
                suggestions.append("Try a higher budget.")
            if start_at:
                suggestions.append("Try another date or time.")
        return SearchResponse(
            query=query,
            intent=intent,
            interpreted_by=interpreted_by,  # type: ignore[arg-type]
            summary=summary,
            used_context=used_context,
            notice=notice,
            clarifications=clarifications,
            availability_checked=start_at is not None,
            start_at=start_at,
            end_at=end_at,
            results=page_items,
            total=total,
            suggestions=suggestions,
        )

    # --- steps ------------------------------------------------------------------------------

    def _places(self, props: list[Property]) -> list[str]:
        found: set[str] = set()
        for p in props:
            found.update({p.name, p.city, p.state})
            for part in (p.address or "").split(","):
                part = part.strip()
                if len(part) > 3 and not part[0].isdigit():
                    found.add(part)
        return sorted(found)

    def _ground(self, ai: SearchIntent, places: list[str], amenities: list[str]) -> SearchIntent:
        """Keep only places and amenities that are listed; report the rest as unknown."""

        def pick(value: str, options: list[str]) -> str | None:
            v = value.strip().lower()
            return next(
                (o for o in options if o.lower() == v or v in o.lower() or o.lower() in v), None
            )

        data = ai.model_dump()
        data["locations"], data["unknown_locations"] = [], []
        for value in ai.locations:
            match = pick(value, places)
            (data["locations"] if match else data["unknown_locations"]).append(match or value)
        data["amenities"], data["unknown_amenities"] = [], []
        for value in ai.amenities:
            match = pick(value, amenities)
            (data["amenities"] if match else data["unknown_amenities"]).append(match or value)
        if data["date"] and data["date"] < self.today:
            data["date"] = None
        return SearchIntent.model_validate(data)

    def _window(self, intent: SearchIntent) -> tuple[datetime | None, datetime | None, str | None]:
        """The period to check availability for, or a question when it cannot be known."""
        if intent.date is None:
            if intent.start_time or intent.duration_unit:
                return (
                    None,
                    None,
                    "From which date do you need it? Availability is checked once a date is given.",
                )
            return None, None, None
        start = datetime.combine(intent.date, time(0), self.tz)
        unit, count = intent.duration_unit, intent.duration_value
        if intent.start_time and intent.end_time:
            return (
                datetime.combine(intent.date, intent.start_time, self.tz),
                datetime.combine(intent.date, intent.end_time, self.tz),
                None,
            )
        if intent.start_time and unit == "HALF_DAY":
            begin = datetime.combine(intent.date, intent.start_time, self.tz)
            return begin, begin + timedelta(hours=4), None
        if intent.start_time or unit in ("HOUR", "HALF_DAY"):
            return None, None, "What time do you need it from and until?"
        if unit == "MONTH":
            if not count:
                return None, None, "For how many months do you need it?"
            return start, datetime.combine(_add_months(intent.date, count), time(0), self.tz), None
        if unit == "WEEK":
            return start, start + timedelta(days=7 * (count or 1)), None
        return start, start + timedelta(days=count or 1), None

    def _find(
        self,
        props: list[Property],
        intent: SearchIntent,
        start_at: datetime | None,
        end_at: datetime | None,
    ) -> list[SearchResult]:
        wanted_places = [w.lower() for w in intent.locations]
        period = intent.budget_period or (
            UNIT_TO_PERIOD.get(intent.duration_unit) if intent.duration_unit else None
        )
        # What applies at each space: its own, its parent spaces', floor's, building's and
        # property's active amenities (BR-031). Two queries for every candidate space.
        by_space = effective_names_by_space(self.session, [p.id for p in props])
        wanted = set(intent.amenities)

        found: list[tuple[float, SearchResult]] = []
        for prop in props:
            text = " ".join([prop.name, prop.city, prop.state, prop.address or ""]).lower()
            if wanted_places and not any(w in text for w in wanted_places):
                continue
            prices = self.catalogue._prices(prop)
            spaces = self.catalogue._public_spaces(Space.property_id == prop.id)
            by_id = {s.id: s for s in spaces}
            floors = {
                f.id: f
                for f in self.session.scalars(
                    select(Floor).where(Floor.id.in_({s.floor_id for s in spaces if s.floor_id}))
                )
            }
            buildings = {
                b.id: b.name
                for b in self.session.scalars(
                    select(Building).where(Building.property_id == prop.id)
                )
            }
            for space in spaces:
                if (
                    not is_priceable(space)
                    or space.operational_status != SpaceOperationalStatus.ACTIVE
                ):
                    continue
                amenities = sorted(by_space.get(space.id, set()))
                if wanted and not wanted <= set(amenities):
                    continue
                kind = SpaceType(space.space_type)
                reasons: list[str] = []
                if intent.resource_types:
                    if kind not in intent.resource_types:
                        continue
                    reasons.append(f"is a {TYPE_WORDS.get(kind, kind.lower())}")
                if intent.capacity_min:
                    if not space.capacity or space.capacity < intent.capacity_min:
                        continue
                    reasons.append(f"seats {space.capacity}")
                area = float(space.area_sqft) if space.area_sqft is not None else None
                if intent.area_min_sqft or intent.area_max_sqft:
                    if area is None:
                        continue
                    if intent.area_min_sqft and area < intent.area_min_sqft:
                        continue
                    if intent.area_max_sqft and area > intent.area_max_sqft:
                        continue
                    reasons.append(f"{area:,.0f} sq ft")
                options = [p for p in prices.get(space.id, []) if p.estimate is not None]
                chosen = None
                if period:
                    chosen = next((p for p in options if p.billing_period == period), None)
                if intent.budget_max:
                    if chosen is None or chosen.estimate.recurring_total > intent.budget_max:
                        continue
                    reasons.append(f"within ₹{intent.budget_max:,.0f}")
                if chosen is None and options:
                    chosen = min(options, key=lambda p: p.estimate.recurring_total)
                available = None
                if start_at and end_at:
                    state = self.catalogue._status(prop, space, by_id, start_at, end_at)
                    available = state.available
                    if intent.availability_required and not available:
                        continue
                    if available:
                        reasons.append("free for the requested time")
                if wanted_places:
                    reasons.append(f"in {prop.city}")
                if intent.amenities:
                    reasons.append("has " + ", ".join(intent.amenities))
                parent = by_id.get(space.parent_space_id) if space.parent_space_id else None
                floor = floors.get(space.floor_id) if space.floor_id else None
                result = SearchResult(
                    space_id=space.id,
                    space_name=space.name,
                    space_type=kind,
                    property_id=prop.id,
                    property_name=prop.name,
                    city=prop.city,
                    building_name=buildings.get(space.building_id) if space.building_id else None,
                    floor_id=space.floor_id,
                    floor_name=floor.name if floor else None,
                    inside=parent.name if parent and parent.space_type != SpaceType.FLOOR else None,
                    capacity=space.capacity,
                    area_sqft=area,
                    price=SearchPrice(
                        billing_period=chosen.billing_period,
                        amount=chosen.estimate.recurring_total,
                        currency=chosen.estimate.currency,
                    )
                    if chosen
                    else None,
                    bookable=bool(options),
                    available=available,
                    amenities=amenities,
                    image=None,
                    reasons=reasons,
                )
                score = 100.0
                if intent.capacity_min and space.capacity:
                    score -= min(30, (space.capacity - intent.capacity_min) * 2)
                if intent.budget_max and chosen:
                    score -= 10 * chosen.estimate.recurring_total / intent.budget_max
                if intent.resource_types:
                    score -= intent.resource_types.index(kind) * 3
                if not options:
                    score -= 40
                found.append((score, result))
        found.sort(
            key=lambda r: (
                -r[0],
                r[1].price.amount if r[1].price else float("inf"),
                r[1].property_name,
                r[1].space_name,
            )
        )
        return [r for _, r in found]

    def _covers(self, items: list[SearchResult], props: list[Property]):  # type: ignore[no-untyped-def]
        wanted = {i.property_id for i in items}
        if not wanted:
            return {}
        try:
            cards = self.catalogue.cards([p for p in props if p.id in wanted])
        except Exception:  # images are optional; a storage problem must not hide results
            return {}
        return {c.id: c.cover for c in cards}


# Words that start a refinement of the previous search rather than a new one.
_FOLLOW_UP_STARTS = (
    "and ",
    "also ",
    "but ",
    "instead",
    "what about",
    "how about",
    "same ",
    "make it",
    "only ",
    "with ",
    "without ",
    "for ",
    "in ",
    "on ",
    "at ",
    "near ",
    "tomorrow",
    "today",
    "next ",
    "this ",
    "under ",
    "below ",
    "up to ",
    "change ",
    "now ",
)


def is_follow_up(query: str) -> bool:
    """For the built-in reader: a short sentence that starts like a change to the last one."""
    text = query.strip().lower()
    return len(text.split()) <= 8 and text.startswith(_FOLLOW_UP_STARTS)


def merge_follow_up(previous: SearchIntent, new: SearchIntent) -> SearchIntent:
    """The previous filters, with whatever the follow-up states replacing them."""
    merged = previous.model_dump()
    for key, value in new.model_dump().items():
        if key == "availability_required":
            continue
        if value not in (None, [], ""):
            merged[key] = value
    # A new date or time window replaces the old one as a whole.
    if new.date is not None or new.start_time is not None:
        for key in ("start_time", "end_time"):
            merged[key] = getattr(new, key)
    return SearchIntent.model_validate(merged)


def customer_context(session: Session, user: AppUser) -> CustomerContext | None:
    """The signed-in customer's own recent places and usual group size, for reading
    follow-ups such as "same place as last time". Only this customer's records are read."""
    tenant = session.scalar(select(Tenant).where(Tenant.user_id == user.id))
    if tenant is None:
        return None
    rows = session.execute(
        select(Booking.property_id, Booking.group_id)
        .where(Booking.tenant_id == tenant.id)
        .order_by(Booking.created_at.desc())
        .limit(20)
    ).all()
    contract_props = list(
        session.scalars(
            select(TenantOccupancy.property_id)
            .where(TenantOccupancy.tenant_id == tenant.id)
            .order_by(TenantOccupancy.created_at.desc())
            .limit(10)
        )
    )
    prop_ids = list(dict.fromkeys([r.property_id for r in rows] + contract_props))
    names = {
        p.id: f"{p.name} ({p.city})"
        for p in session.scalars(select(Property).where(Property.id.in_(prop_ids)))
    }
    recent = [names[i] for i in prop_ids if i in names][:5]
    group_size = None
    latest_group = next((r.group_id for r in rows if r.group_id), None)
    if latest_group is not None:
        group_size = sum(1 for r in rows if r.group_id == latest_group) or None
    if not recent and group_size is None:
        return None
    return CustomerContext(recent_places=recent, usual_group_size=group_size)
