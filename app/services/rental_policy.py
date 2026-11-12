"""Which rental durations a customer may choose for each space type (owner's applicability
matrix, 12 Nov 2026; space-type mapping confirmed the same day).

The single place to change applicability. Every cell is one of:

- BOOKING      a short-term booking request (hourly, daily, weekly; monthly where optional)
- OPTIONAL     monthly for desks: a booking, or a long-term rental request if the customer
               prefers a contract ("admin decision / optional contract")
- CONTRACT     only through a long-term rental request (CRM lead -> contract)
- NOT_OFFERED  not offered to customers
- OWNER        owner decision pending: not offered until decided
- DEFERRED     deferred (storage): not offered

Quarterly, half-yearly and yearly are contract terms of 3, 6 and 12 months, estimated at the
space's monthly price (owner decision, 12 Nov 2026); there are no separate price types.
Admins are not limited by this matrix; it governs what customers can request.
"""

from __future__ import annotations

from enum import StrEnum

from app.models.enums import BillingPeriod, SpaceType


class Duration(StrEnum):
    HOURLY = "HOURLY"
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"
    QUARTERLY = "QUARTERLY"
    HALF_YEARLY = "HALF_YEARLY"
    YEARLY = "YEARLY"


class Mode(StrEnum):
    BOOKING = "BOOKING"
    OPTIONAL = "OPTIONAL"
    CONTRACT = "CONTRACT"
    NOT_OFFERED = "NOT_OFFERED"
    OWNER = "OWNER"
    DEFERRED = "DEFERRED"


TERM_MONTHS = {
    Duration.MONTHLY: 1,
    Duration.QUARTERLY: 3,
    Duration.HALF_YEARLY: 6,
    Duration.YEARLY: 12,
}
LONG_TERM = tuple(TERM_MONTHS)

B, OPT, C, N, W = Mode.BOOKING, Mode.OPTIONAL, Mode.CONTRACT, Mode.NOT_OFFERED, Mode.OWNER
DESK = {
    "HOURLY": B,
    "DAILY": B,
    "WEEKLY": B,
    "MONTHLY": OPT,
    "QUARTERLY": C,
    "HALF_YEARLY": C,
    "YEARLY": C,
}
ROOM = {
    "HOURLY": B,
    "DAILY": B,
    "WEEKLY": B,
    "MONTHLY": C,
    "QUARTERLY": C,
    "HALF_YEARLY": C,
    "YEARLY": C,
}
COMMERCIAL = {
    "HOURLY": N,
    "DAILY": N,
    "WEEKLY": N,
    "MONTHLY": C,
    "QUARTERLY": C,
    "HALF_YEARLY": C,
    "YEARLY": C,
}
MEETING = {
    "HOURLY": B,
    "DAILY": B,
    "WEEKLY": B,
    "MONTHLY": W,
    "QUARTERLY": W,
    "HALF_YEARLY": W,
    "YEARLY": W,
}
# Parking follows its own contract rules (D8.4, D8.5, D10.7): long-term by contract. Short-term
# parking bookings were never decided, so they wait for the owner.
PARKING = {
    "HOURLY": W,
    "DAILY": W,
    "WEEKLY": W,
    "MONTHLY": C,
    "QUARTERLY": C,
    "HALF_YEARLY": C,
    "YEARLY": C,
}
UNDECIDED = {d.value: W for d in Duration}
NEVER = {d.value: N for d in Duration}

MATRIX: dict[str, dict[str, Mode]] = {
    # Hot desk / flexible workstation
    SpaceType.FLEXIBLE_WORKSTATION: DESK,
    SpaceType.DESK: DESK,
    SpaceType.SEAT: DESK,
    SpaceType.COWORKING: DESK,
    # Dedicated desk
    SpaceType.DEDICATED_WORKSTATION: DESK,
    # Private cabin / private office
    SpaceType.CABIN: ROOM,
    SpaceType.PRIVATE_OFFICE: ROOM,
    # Commercial portion / floor
    SpaceType.PORTION: COMMERCIAL,
    SpaceType.FLOOR: COMMERCIAL,
    # Meeting, conference, training rooms
    SpaceType.MEETING_ROOM: MEETING,
    SpaceType.CONFERENCE_ROOM: MEETING,
    SpaceType.TRAINING_ROOM: MEETING,
    SpaceType.PARKING_SLOT: PARKING,
    # Not configured yet (commercial office/retail and storage have no space type)
    SpaceType.OTHER: UNDECIDED,
    SpaceType.NON_RENTABLE: NEVER,
}

# A booking's billing period, as the duration it belongs to.
PERIOD_DURATION = {
    BillingPeriod.HOURLY: Duration.HOURLY,
    BillingPeriod.HALF_DAY: Duration.HOURLY,
    BillingPeriod.FULL_DAY: Duration.HOURLY,
    BillingPeriod.DAILY: Duration.DAILY,
    BillingPeriod.WEEKLY: Duration.WEEKLY,
    BillingPeriod.MONTHLY: Duration.MONTHLY,
}

REASON = {
    Mode.CONTRACT: "needs a contract: send a long-term rental request instead",
    Mode.NOT_OFFERED: "is not offered for this kind of space",
    Mode.OWNER: "is not offered yet for this kind of space (awaiting a decision)",
    Mode.DEFERRED: "is not offered yet",
}


def mode(space_type: str, duration: Duration | str) -> Mode:
    return MATRIX.get(space_type, UNDECIDED).get(str(duration), Mode.OWNER)


def can_book(space_type: str, period: BillingPeriod | str) -> tuple[bool, str | None]:
    """Whether a customer may send a short-term booking request for this period."""
    duration = PERIOD_DURATION.get(BillingPeriod(period))
    if duration is None:
        return False, "This kind of booking is not offered to customers."
    m = mode(space_type, duration)
    if m in (Mode.BOOKING, Mode.OPTIONAL):
        return True, None
    return False, f"{duration.value.replace('_', '-').capitalize()} {REASON[m]}."


def can_request_contract(space_type: str, duration: Duration | str) -> tuple[bool, str | None]:
    """Whether a customer may send a long-term rental request for this duration."""
    if str(duration) not in TERM_MONTHS:
        return False, "Long-term rentals are monthly, quarterly, half-yearly or yearly."
    m = mode(space_type, duration)
    if m in (Mode.CONTRACT, Mode.OPTIONAL):
        return True, None
    label = str(duration).replace("_", "-").capitalize()
    if m == Mode.BOOKING:
        return False, f"{label} is a short-term booking for this space."
    return False, f"{label} {REASON[m]}."


def options(space_type: str) -> list[tuple[Duration, Mode]]:
    """Every duration and how a customer can rent it, in order."""
    return [(d, mode(space_type, d)) for d in Duration]
