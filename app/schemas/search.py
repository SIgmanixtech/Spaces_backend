import datetime as dt
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import BillingPeriod, SpaceType
from app.schemas.client import PublicImage
from app.schemas.common import InputSchema, ReadSchema

DurationUnit = Literal["HOUR", "HALF_DAY", "DAY", "WEEK", "MONTH"]


class SearchIntent(BaseModel):
    """What a sentence asks for, as filters. Every field is optional: nothing is assumed."""

    model_config = ConfigDict(extra="forbid")

    locations: list[str] = Field(default_factory=list, description="Matched to listed places.")
    unknown_locations: list[str] = Field(default_factory=list)
    resource_types: list[SpaceType] = Field(default_factory=list)
    capacity_min: int | None = Field(default=None, ge=1, le=10_000)
    area_min_sqft: float | None = Field(default=None, gt=0)
    area_max_sqft: float | None = Field(default=None, gt=0)
    date: dt.date | None = None
    start_time: dt.time | None = None
    end_time: dt.time | None = None
    duration_unit: DurationUnit | None = None
    duration_value: int | None = Field(default=None, ge=1, le=120)
    budget_max: float | None = Field(default=None, gt=0)
    budget_period: BillingPeriod | None = None
    amenities: list[str] = Field(default_factory=list, description="Matched to listed amenities.")
    unknown_amenities: list[str] = Field(default_factory=list)
    availability_required: bool = True


class PreviousSearch(InputSchema):
    """The search just before this one, so a follow-up such as "for 10 people instead" or
    "what about tomorrow?" refines it. Only filters are sent back, never results."""

    query: str = Field(min_length=1, max_length=300)
    intent: SearchIntent | None = None


class SearchRequest(InputSchema):
    query: str = Field(min_length=2, max_length=300)
    page: int = Field(default=1, ge=1, le=50)
    page_size: int = Field(default=20, ge=1, le=50)
    previous: PreviousSearch | None = Field(
        default=None, description="The previous search in this conversation, if any."
    )


class SearchPrice(ReadSchema):
    billing_period: BillingPeriod
    amount: float
    currency: str


class SearchResult(ReadSchema):
    space_id: uuid.UUID
    space_name: str
    space_type: SpaceType
    property_id: uuid.UUID
    property_name: str
    city: str
    building_name: str | None
    floor_id: uuid.UUID | None
    floor_name: str | None
    inside: str | None = Field(description="The portion or area around it, if any.")
    capacity: int | None
    area_sqft: float | None
    price: SearchPrice | None = Field(description="For the requested period, else the lowest.")
    bookable: bool = Field(description="Priced for online booking.")
    available: bool | None = Field(description="For the requested time; null when not checked.")
    amenities: list[str]
    image: PublicImage | None
    reasons: list[str] = Field(description="Which requested criteria this space meets.")


class SearchResponse(ReadSchema):
    query: str
    intent: SearchIntent
    interpreted_by: Literal["rules", "ai"]
    summary: str | None = Field(
        default=None, description="One sentence saying what the search was understood to ask."
    )
    used_context: list[str] = Field(
        default_factory=list,
        description='What besides the sentence shaped the filters, e.g. "your previous search".',
    )
    notice: str | None = Field(default=None, description="E.g. the AI interpreter was unavailable.")
    clarifications: list[str] = Field(description="Questions that would sharpen the search.")
    availability_checked: bool
    start_at: dt.datetime | None
    end_at: dt.datetime | None
    results: list[SearchResult]
    total: int
    suggestions: list[str] = Field(
        default_factory=list, description="Real alternatives when nothing matched."
    )
