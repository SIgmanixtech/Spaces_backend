import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import AwareDatetime, Field

from app.models.enums import BillingPeriod, PaymentHoldStatus
from app.schemas.common import InputSchema, ReadSchema


class CheckoutStart(InputSchema):
    space_id: uuid.UUID
    billing_period: BillingPeriod
    start_at: AwareDatetime
    end_at: AwareDatetime
    notes: str | None = Field(default=None, max_length=2000)


class PaymentHoldRead(ReadSchema):
    id: uuid.UUID
    reference: str
    status: PaymentHoldStatus
    space_id: uuid.UUID
    property_id: uuid.UUID
    start_at: datetime
    end_at: datetime
    billing_period: str
    amount: Decimal
    currency: str
    created_at: datetime
    expires_at: datetime = Field(description="Server time. After this the space is released.")
    booking_id: uuid.UUID | None


class PaymentHoldAdminRead(PaymentHoldRead):
    tenant_id: uuid.UUID
    payment_reference: str | None
    payment_amount: Decimal | None
    payment_verified_at: datetime | None
    review_reason: str | None
    resolution: str | None
    resolved_at: datetime | None


class PaymentHoldResolve(InputSchema):
    resolution: str = Field(min_length=1, max_length=2000)
