# ruff: noqa: F811  (pytest fixtures shared from other test modules)
"""Two customers start checkout for the same space and time at the same moment.

Each attempt runs in its own thread, with its own database session and transaction, through
the real service path (PaymentHoldService.start). A barrier releases both together so the
transactions genuinely overlap. Exactly one hold may succeed; the other must be refused
(409) and leave nothing behind. Repeated on fresh slots to shake out races."""

import threading
import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError
from app.db.session import get_session_factory
from app.models import Tenant
from app.models.allocation import Allocation
from app.models.enums import BillingPeriod
from app.models.payment_hold import PaymentHold
from app.models.user import AppUser
from app.services.payment_holds import HOLD_MINUTES, PaymentHoldService
from tests.test_client_api import MONDAY, hub, priya, ravi  # noqa: F401
from tests.test_durations_and_holds import payments_on  # noqa: F401

ROUNDS = 10


def attempt(
    email: str, space_id: str, start: datetime, barrier: threading.Barrier, out: dict
) -> None:
    session = get_session_factory()()
    try:
        user = session.scalar(select(AppUser).where(AppUser.email == email))
        tenant = session.scalar(select(Tenant).where(Tenant.user_id == user.id))
        session.commit()  # start the attempt with a fresh transaction
        barrier.wait(timeout=30)
        hold = PaymentHoldService(session, user).start(
            tenant,
            space_id=uuid.UUID(space_id),
            billing_period=BillingPeriod.HOURLY,
            start_at=start,
            end_at=start + timedelta(hours=2),
            notes=None,
        )
        out[email] = ("HELD", hold.id)
    except ConflictError as exc:
        session.rollback()
        out[email] = ("REFUSED", str(exc.detail))
    except Exception as exc:  # noqa: BLE001 - reported by the test
        session.rollback()
        out[email] = ("ERROR", repr(exc))
    finally:
        session.close()


def race(space_id: str, start: datetime) -> dict:
    barrier = threading.Barrier(2)
    out: dict = {}
    threads = [
        threading.Thread(target=attempt, args=(email, space_id, start, barrier, out))
        for email in ("priya@example.com", "ravi@example.com")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert all(not t.is_alive() for t in threads), "an attempt did not finish"
    return out


def test_two_simultaneous_checkouts_hold_the_slot_exactly_once(
    hub: dict,
    priya: TestClient,
    ravi: TestClient,
    payments_on: None,
    db_session: Session,
) -> None:
    space_id = hub["seat1"]["id"]
    for i in range(ROUNDS):
        start = MONDAY + timedelta(days=i, hours=1)
        out = race(space_id, start)
        outcomes = sorted(o[0] for o in out.values())
        assert outcomes == ["HELD", "REFUSED"], out
        db_session.expire_all()
        holds = db_session.scalars(
            select(PaymentHold).where(
                PaymentHold.space_id == space_id, PaymentHold.start_at == start
            )
        ).all()
        # Exactly one row: the losing transaction left nothing behind.
        assert [h.status for h in holds] == ["ACTIVE"], out
        winner = holds[0]
        assert winner.expires_at - winner.created_at == timedelta(minutes=HOLD_MINUTES)
        active = db_session.scalar(
            select(func.count())
            .select_from(Allocation)
            .where(Allocation.payment_hold_id.in_([h.id for h in holds]), Allocation.is_active)
        )
        assert active == 1

    # After the 10 minutes, the slot is free again for the other customer.
    # The last round's winner has not started another checkout, so its hold is still active.
    start = MONDAY + timedelta(days=ROUNDS - 1, hours=1)
    winner = db_session.scalar(
        select(PaymentHold).where(PaymentHold.space_id == space_id, PaymentHold.start_at == start)
    )
    winner.created_at = datetime.now(UTC) - timedelta(minutes=11)
    winner.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db_session.commit()
    loser_email = next(
        e
        for e in ("priya@example.com", "ravi@example.com")
        if db_session.scalar(select(AppUser.id).where(AppUser.email == e)) != winner.user_id
    )
    again = race_one(loser_email, space_id, start)
    assert again[0] == "HELD", again
    db_session.expire_all()
    assert db_session.get(PaymentHold, winner.id).status == "EXPIRED"


def race_one(email: str, space_id: str, start: datetime) -> tuple:
    barrier = threading.Barrier(1)
    out: dict = {}
    attempt(email, space_id, start, barrier, out)
    return out[email]
