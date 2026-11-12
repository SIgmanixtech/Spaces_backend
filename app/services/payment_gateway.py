"""The boundary for an online payment gateway. **No gateway is integrated** (12 Nov 2026):
choosing one needs the owner's approval. A gateway adapter must:

1. Verify every callback or webhook before trusting it (the gateway's signature with a
   server-side secret, never a value from the browser), and reject anything unverified.
2. Map the notification to the checkout hold it belongs to (the hold id sent when the
   payment was created) and the gateway's own payment id.
3. Call PaymentHoldService.payment_verified(...) only for a verified, captured payment, or
   payment_failed(...) for a verified failure. Both are idempotent by the payment id, so
   retried, duplicate or delayed notifications are safe.
4. Never treat starting a payment, or the customer returning to the site, as success.

Until an adapter exists there is no public payment endpoint, and online payment stays off
(ONLINE_PAYMENTS_ENABLED=false).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol


@dataclass(frozen=True)
class PaymentEvent:
    hold_id: uuid.UUID
    payment_reference: str
    outcome: Literal["CAPTURED", "FAILED"]
    amount: Decimal
    currency: str


class PaymentGateway(Protocol):
    def verify(self, headers: dict[str, str], body: bytes) -> PaymentEvent:
        """Return the verified event, or raise if the notification is not authentic."""
        ...
