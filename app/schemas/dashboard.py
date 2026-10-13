import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import ReadSchema


class OccupancyTrendPoint(BaseModel):
    period: str = Field(description='Month label, e.g. "Oct 2026".')
    occupancy_rate: float = Field(description="Percentage (0-100) of spaces occupied.")


class SpaceSummary(BaseModel):
    total: int
    available: int
    booked: int
    reserved: int
    occupied: int
    unavailable: int


class DashboardSummary(BaseModel):
    total: int = Field(description="All properties that are not archived.")
    vacant: int
    reserved: int
    occupied: int
    unavailable: int
    spaces: SpaceSummary = Field(description="Every non-archived space by its current status.")
    occupancy_rate: float | None = Field(
        description="Percentage (0-100) of spaces occupied now. Null when no spaces exist."
    )
    occupancy_trend: list[OccupancyTrendPoint] = Field(
        description="Occupancy at the end of each of the last six months, from occupancy "
        "history. Months before any space existed are omitted."
    )


class ActivityEntry(ReadSchema):
    id: uuid.UUID
    action: str
    entity_type: str
    message: str
    property_id: uuid.UUID | None
    property_name: str | None
    created_at: datetime


class PropertyAvailability(BaseModel):
    property_id: uuid.UUID
    property_name: str
    available_spaces: int
    total_spaces: int
