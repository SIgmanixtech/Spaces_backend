"""Long-term rental requests from the client app (they become CRM leads)."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field

from app.schemas.common import InputSchema, ReadSchema

LongTerm = Literal["MONTHLY", "QUARTERLY", "HALF_YEARLY", "YEARLY"]


class RentalRequestCreate(InputSchema):
    space_id: uuid.UUID
    duration: LongTerm
    start_date: date
    count: int = Field(
        default=1, ge=1, le=24, description="How many periods, e.g. 2 quarters (max 36 months)."
    )
    notes: str | None = Field(default=None, max_length=2000)


class RentalEstimate(ReadSchema):
    """An estimate at the space's monthly price; not a quote. Nothing is held."""

    space_id: uuid.UUID
    space_name: str
    property_name: str
    duration: str
    months: int
    start_date: date
    end_date: date = Field(description="Last day, inclusive.")
    currency: str
    monthly_rent: Decimal
    rent_total: Decimal = Field(description="Monthly rent × months; no tax or discount.")
    deposit: Decimal | None = Field(description="As configured in pricing, if any.")
    available: bool
    availability_message: str | None


class RentalRequestRead(ReadSchema):
    id: uuid.UUID
    reference: str
    property_name: str | None
    space_name: str | None
    space_type: str | None
    months: int | None
    start_date: date | None
    stage: str = Field(description="The lead's stage, including the automatic contract stages.")
    stage_label: str
    created_at: datetime
