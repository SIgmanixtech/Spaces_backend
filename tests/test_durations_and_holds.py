# ruff: noqa: F811  (pytest fixtures shared from other test modules)
"""12 Nov 2026 owner decisions: the duration matrix, booking requests that reserve nothing,
long-term rental requests that become CRM leads, and 10-minute checkout holds."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Booking, Tenant
from app.models.crm import Lead, LeadInterest
from app.models.payment_hold import PaymentHold
from app.services.occupancy import business_today
from app.services.payment_holds import PaymentHoldService
from app.services.rental_policy import Mode, can_book, can_request_contract, mode
from tests.test_client_api import MONDAY, floor_states, hub, iso, priya, ravi  # noqa: F401
from tests.test_resources import V1

CUSTOMER = f"{V1}/client"
ADMIN = f"{V1}/admin"


# --- the matrix (no database) ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("space_type", "duration", "expected"),
    [
        ("FLEXIBLE_WORKSTATION", "HOURLY", Mode.BOOKING),
        ("SEAT", "MONTHLY", Mode.OPTIONAL),
        ("DEDICATED_WORKSTATION", "QUARTERLY", Mode.CONTRACT),
        ("CABIN", "WEEKLY", Mode.BOOKING),
        ("CABIN", "MONTHLY", Mode.CONTRACT),
        ("PRIVATE_OFFICE", "YEARLY", Mode.CONTRACT),
        ("PORTION", "HOURLY", Mode.NOT_OFFERED),
        ("FLOOR", "HALF_YEARLY", Mode.CONTRACT),
        ("MEETING_ROOM", "DAILY", Mode.BOOKING),
        ("CONFERENCE_ROOM", "MONTHLY", Mode.OWNER),
        ("PARKING_SLOT", "HOURLY", Mode.OWNER),
        ("PARKING_SLOT", "MONTHLY", Mode.CONTRACT),
        ("OTHER", "DAILY", Mode.OWNER),
        ("NON_RENTABLE", "HOURLY", Mode.NOT_OFFERED),
    ],
)
def test_the_owner_matrix(space_type: str, duration: str, expected: Mode) -> None:
    assert mode(space_type, duration) == expected


def test_bookings_and_contract_requests_follow_the_matrix() -> None:
    assert can_book("CABIN", "HOURLY") == (True, None)
    assert can_book("CABIN", "HALF_DAY")[0] is True  # hour-based slots follow hourly
    allowed, why = can_book("CABIN", "MONTHLY")
    assert not allowed and "long-term rental request" in (why or "")
    assert can_book("SEAT", "MONTHLY") == (True, None)  # optional contract: booking allowed
    assert can_book("PORTION", "DAILY")[0] is False
    assert can_book("CABIN", "CUSTOM")[0] is False
    assert can_request_contract("SEAT", "MONTHLY") == (True, None)
    assert can_request_contract("CABIN", "QUARTERLY") == (True, None)
    assert can_request_contract("CABIN", "HOURLY")[0] is False
    assert can_request_contract("MEETING_ROOM", "YEARLY")[0] is False


# --- booking requests reserve nothing --------------------------------------------------------


def request(client: TestClient, hub: dict, *, hours: int = 2, start: datetime = MONDAY) -> dict:
    made = client.post(
        f"{CUSTOMER}/bookings",
        json={
            "space_id": hub["seat1"]["id"],
            "billing_period": "HOURLY",
            "start_at": iso(start),
            "end_at": iso(start + timedelta(hours=hours)),
        },
    )
    assert made.status_code == 201, made.text
    return made.json()


def test_pending_requests_reserve_nothing_and_confirming_checks_again(
    api: TestClient, hub: dict, priya: TestClient, ravi: TestClient, db_session: Session
) -> None:
    first = request(priya, hub)
    second = request(ravi, hub)  # same seat, same time: allowed, nothing is held
    assert first["status"] == second["status"] == "PENDING"
    assert api.post(f"{V1}/bookings/{first['id']}/confirm").status_code == 200
    refused = api.post(f"{V1}/bookings/{second['id']}/confirm")
    assert refused.status_code == 409, refused.text
    db_session.expire_all()
    assert db_session.get(Booking, second["id"]).status == "PENDING"
    # Once one is confirmed, a new request for that time is refused at once.
    late = priya.post(
        f"{CUSTOMER}/bookings",
        json={
            "space_id": hub["seat1"]["id"],
            "billing_period": "HOURLY",
            "start_at": iso(MONDAY),
            "end_at": iso(MONDAY + timedelta(hours=1)),
        },
    )
    assert late.status_code == 409


def test_customers_cannot_book_durations_the_matrix_reserves_for_contracts(
    hub: dict, priya: TestClient
) -> None:
    refused = priya.post(
        f"{CUSTOMER}/bookings",
        json={
            "space_id": hub["cabin"]["id"],
            "billing_period": "MONTHLY",
            "start_at": iso(MONDAY),
            "end_at": iso(MONDAY + timedelta(days=30)),
        },
    )
    assert refused.status_code == 409 and "long-term rental request" in refused.text
    floor = priya.get(f"{V1}/public/properties/{hub['property']['id']}").json()
    assert floor  # the property page still loads


# --- long-term rental requests become CRM leads ---------------------------------------------


def test_a_long_term_request_becomes_a_lead_and_holds_nothing(
    api: TestClient, hub: dict, priya: TestClient, db_session: Session
) -> None:
    start = business_today() + timedelta(days=20)
    body = {"space_id": hub["cabin"]["id"], "duration": "QUARTERLY", "start_date": str(start)}
    estimate = priya.post(f"{CUSTOMER}/rental-requests/estimate", json=body)
    assert estimate.status_code == 200, estimate.text
    est = estimate.json()
    assert est["months"] == 3 and est["available"] is True
    assert Decimal(est["rent_total"]) == Decimal(est["monthly_rent"]) * 3

    made = priya.post(f"{CUSTOMER}/rental-requests", json={**body, "notes": "Team of 4"})
    assert made.status_code == 201, made.text
    lead = db_session.scalar(select(Lead).where(Lead.id == made.json()["id"]))
    mine = db_session.scalar(select(Tenant).where(Tenant.email == "priya@example.com"))
    assert (lead.source, lead.tenant_id, lead.requested_term_months) == ("CLIENT_APP", mine.id, 3)
    assert lead.stage == "LEAD_CREATED"
    interest = db_session.scalar(select(LeadInterest).where(LeadInterest.lead_id == lead.id))
    assert str(interest.space_id) == hub["cabin"]["id"]
    # The property team sees it in CRM; nothing is held or booked.
    assert api.get(f"{V1}/admin/crm/leads/{lead.id}").status_code == 200
    assert db_session.scalars(select(Booking)).all() == []
    # Asked twice: refused. Listed with its stage.
    again = priya.post(f"{CUSTOMER}/rental-requests", json=body)
    assert again.status_code == 409 and "already asked" in again.text
    listed = priya.get(f"{CUSTOMER}/rental-requests").json()
    assert [(r["reference"], r["months"], r["stage_label"]) for r in listed] == [
        (lead.reference, 3, "Lead Created")
    ]


def test_long_term_requests_follow_the_matrix(hub: dict, priya: TestClient) -> None:
    start = str(business_today() + timedelta(days=20))
    hourly = priya.post(
        f"{CUSTOMER}/rental-requests",
        json={"space_id": hub["cabin"]["id"], "duration": "HOURLY", "start_date": start},
    )
    assert hourly.status_code == 422  # not a long-term duration
    no_price = priya.post(
        f"{CUSTOMER}/rental-requests",
        json={"space_id": hub["seat1"]["id"], "duration": "MONTHLY", "start_date": start},
    )
    assert no_price.status_code == 409 and "no monthly price" in no_price.text


# --- 10-minute checkout holds ----------------------------------------------------------------


@pytest.fixture
def payments_on() -> Iterator[None]:
    settings = get_settings()
    before = settings.online_payments_enabled
    settings.online_payments_enabled = True
    try:
        yield
    finally:
        settings.online_payments_enabled = before


def checkout(client: TestClient, hub: dict, expected: int = 201) -> dict:
    made = client.post(
        f"{CUSTOMER}/checkout-holds",
        json={
            "space_id": hub["seat1"]["id"],
            "billing_period": "HOURLY",
            "start_at": iso(MONDAY),
            "end_at": iso(MONDAY + timedelta(hours=2)),
        },
    )
    assert made.status_code == expected, made.text
    return made.json()


def test_checkout_is_off_until_payments_are_switched_on(hub: dict, priya: TestClient) -> None:
    assert "not available" in checkout(priya, hub, 409)["detail"]
    caps = priya.get(f"{V1}/public/capabilities").json()
    assert caps["online_payments"] is False


def test_a_checkout_hold_blocks_others_and_becomes_a_booking_when_paid(
    api: TestClient,
    hub: dict,
    priya: TestClient,
    ravi: TestClient,
    payments_on: None,
    db_session: Session,
) -> None:
    hold = checkout(priya, hub)
    assert hold["status"] == "ACTIVE"
    life = datetime.fromisoformat(hold["expires_at"]) - datetime.fromisoformat(hold["created_at"])
    assert life == timedelta(minutes=10)
    # Nobody else can hold or confirm that time while it is held.
    checkout(ravi, hub, 409)
    # Even a request for that time is refused while the hold is active.
    rival = ravi.post(
        f"{CUSTOMER}/bookings",
        json={
            "space_id": hub["seat1"]["id"],
            "billing_period": "HOURLY",
            "start_at": iso(MONDAY),
            "end_at": iso(MONDAY + timedelta(hours=2)),
        },
    )
    assert rival.status_code == 409 and "another customer pays" in rival.text
    # And an admin cannot confirm a booking over it.
    admin = api.post(
        f"{V1}/bookings",
        json={
            "property_id": hub["property"]["id"],
            "space_id": hub["seat1"]["id"],
            "customer_name": "Walk-in",
            "start_at": iso(MONDAY),
            "end_at": iso(MONDAY + timedelta(hours=1)),
            "status": "CONFIRMED",
        },
    )
    assert admin.status_code == 409

    service = PaymentHoldService(db_session, None)
    paid = service.payment_verified(
        hold["id"], payment_reference="pay_123", amount=Decimal(hold["amount"]), currency="INR"
    )
    assert paid.status == "CONVERTED" and paid.booking_id is not None
    booking = db_session.get(Booking, paid.booking_id)
    assert (booking.status, booking.channel) == ("CONFIRMED", "CUSTOMER")
    # A duplicate or delayed notification changes nothing.
    again = service.payment_verified(
        hold["id"], payment_reference="pay_123", amount=Decimal(hold["amount"]), currency="INR"
    )
    assert again.booking_id == paid.booking_id
    assert len(db_session.scalars(select(Booking).where(Booking.status == "CONFIRMED")).all()) == 1


def test_expired_holds_release_and_late_payments_go_to_review(
    api: TestClient,
    hub: dict,
    priya: TestClient,
    ravi: TestClient,
    payments_on: None,
    db_session: Session,
) -> None:
    hold = checkout(priya, hub)
    row = db_session.get(PaymentHold, hold["id"])
    # Ten minutes have passed on the server.
    row.created_at = datetime.now(UTC) - timedelta(minutes=11)
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db_session.commit()
    # The expired hold blocks nobody: another customer's request is confirmed.
    rival = request(ravi, hub)
    assert api.post(f"{V1}/bookings/{rival['id']}/confirm").status_code == 200
    db_session.expire_all()
    assert db_session.get(PaymentHold, hold["id"]).status == "EXPIRED"
    # The first customer's payment arrives late: no overlapping booking, manual review.
    late = PaymentHoldService(db_session, None).payment_verified(
        hold["id"], payment_reference="pay_late", amount=Decimal(hold["amount"]), currency="INR"
    )
    assert late.status == "NEEDS_REVIEW" and late.booking_id is None
    assert "refund" in late.review_reason
    review = api.get(f"{ADMIN}/payment-holds/needs-review").json()
    assert [r["reference"] for r in review] == [hold["reference"]]
    done = api.post(
        f"{ADMIN}/payment-holds/{hold['id']}/resolve", json={"resolution": "Refunded 13 Nov"}
    )
    assert done.status_code == 200 and done.json()["status"] == "RESOLVED"


def test_failed_cancelled_and_mismatched_payments_never_book(
    hub: dict, priya: TestClient, payments_on: None, db_session: Session
) -> None:
    service = PaymentHoldService(db_session, None)
    first = checkout(priya, hub)
    assert service.payment_failed(first["id"], payment_reference="pay_x").status == "RELEASED"
    second = checkout(priya, hub)
    cancelled = priya.post(f"{CUSTOMER}/checkout-holds/{second['id']}/cancel").json()
    assert cancelled["status"] == "RELEASED"
    third = checkout(priya, hub)
    wrong = service.payment_verified(
        third["id"], payment_reference="pay_y", amount=Decimal("1.00"), currency="INR"
    )
    assert wrong.status == "NEEDS_REVIEW" and wrong.booking_id is None
    assert db_session.scalars(select(Booking)).all() == []


@pytest.mark.parametrize(
    ("duration", "count", "months"),
    [("MONTHLY", 2, 2), ("QUARTERLY", 1, 3), ("HALF_YEARLY", 1, 6), ("YEARLY", 1, 12)],
)
def test_every_long_term_duration_becomes_a_lead_and_reserves_nothing(
    hub: dict, priya: TestClient, db_session: Session, duration: str, count: int, months: int
) -> None:
    start = business_today() + timedelta(days=15)
    made = priya.post(
        f"{CUSTOMER}/rental-requests",
        json={
            "space_id": hub["cabin"]["id"],
            "duration": duration,
            "start_date": str(start),
            "count": count,
        },
    )
    assert made.status_code == 201, made.text
    lead = db_session.get(Lead, made.json()["id"])
    assert (lead.source, lead.requested_term_months) == ("CLIENT_APP", months)
    # No hold of any kind: no booking, no checkout hold, no allocation for the cabin.
    assert db_session.scalars(select(Booking)).all() == []
    assert db_session.scalars(select(PaymentHold)).all() == []
    inside = datetime.combine(start + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    assert floor_states(priya, hub, inside + timedelta(hours=5))["Cabin 1"] == "AVAILABLE"
