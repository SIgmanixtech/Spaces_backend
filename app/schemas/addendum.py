import uuid
from datetime import date, datetime
from typing import Any

from pydantic import Field

from app.models.enums import AddendumStatus
from app.schemas.agreement import Signer
from app.schemas.common import InputSchema, ReadSchema


class AddendumCreate(InputSchema):
    effective_date: date
    summary: str = Field(min_length=1, max_length=5000, description="The change, in words.")
    changes: dict[str, Any] = Field(
        description="New values, applied when signed: monthly_rent, security_deposit, terms, "
        "lock_in_period, notice_period, expected_headcount, occupants, government_department, "
        "government_order_reference, vehicle_registration."
    )


class AddendumCancel(InputSchema):
    reason: str = Field(min_length=1, max_length=2000)


class AddendumRead(ReadSchema):
    """A change to a contract's terms. Its `changes` apply only once it is SIGNED by both
    parties; until then the contract's terms are unchanged (LC-4)."""

    id: uuid.UUID
    reference: str
    contract_id: uuid.UUID
    status: AddendumStatus
    effective_date: date
    summary: str
    changes: dict[str, Any]
    previous: dict[str, Any] = Field(description="The values signing replaced.")
    file_name: str | None
    signed_on: date | None
    signers: list[Signer]
    signed_at: datetime | None
    cancelled_at: datetime | None
    cancellation_reason: str | None
    created_at: datetime
