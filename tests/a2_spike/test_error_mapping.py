"""What SQLAlchemy and psycopg report, and whether the existing 409 mapping catches it.

The application turns ``IntegrityError`` into HTTP 409 in ``app.services.base``
(``flush`` and ``commit``). These tests drive the proposed tables through the
ORM to see which failures arrive that way and which do not.
"""

import uuid
from collections.abc import Iterator
from datetime import datetime

import psycopg.errors
import pytest
from sqlalchemy import (
    Boolean,
    Engine,
    ForeignKeyConstraint,
    Identity,
    SmallInteger,
    String,
    event,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import TSTZRANGE, ExcludeConstraint, Range
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship
from sqlalchemy.schema import CreateTable

from app.core.errors import ConflictError
from app.services.base import _conflict_message, commit, flush
from tests.a2_spike.helpers import (
    EXCLUSIVE,
    SHARED,
    Inventory,
    build_inventory,
    claim_chain,
    hours,
)


class SpikeBase(DeclarativeBase):
    """Separate from the application's metadata, so Alembic and the app never see these."""


class Allocation(SpikeBase):
    __tablename__ = "allocations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    source_type: Mapped[str] = mapped_column(String, default="BOOKING")
    booking_id: Mapped[uuid.UUID | None] = mapped_column(default=uuid.uuid4)
    property_id: Mapped[uuid.UUID] = mapped_column()
    space_id: Mapped[uuid.UUID] = mapped_column()
    period: Mapped[Range[datetime]] = mapped_column(TSTZRANGE)
    original_period: Mapped[Range[datetime]] = mapped_column(TSTZRANGE)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    claims: Mapped[list["Claim"]] = relationship(back_populates="allocation")


class Claim(SpikeBase):
    __tablename__ = "allocation_claims"
    __table_args__ = (
        ForeignKeyConstraint(
            ["allocation_id", "period", "is_active"],
            ["allocations.id", "allocations.period", "allocations.is_active"],
            onupdate="CASCADE",
            ondelete="RESTRICT",
        ),
        ExcludeConstraint(
            ("space_id", "="),
            ("period", "&&"),
            where=text("is_active AND claim_mode = 1"),
            using="gist",
            name="ex_claims_exclusive",
        ),
        ExcludeConstraint(
            ("space_id", "="),
            ("claim_mode", "<>"),
            ("period", "&&"),
            where=text("is_active"),
            using="gist",
            name="ex_claims_mixed",
        ),
    )

    id: Mapped[int] = mapped_column(Identity(always=True), primary_key=True)
    allocation_id: Mapped[uuid.UUID] = mapped_column()
    period: Mapped[Range[datetime]] = mapped_column(TSTZRANGE)
    is_active: Mapped[bool] = mapped_column(Boolean)
    space_id: Mapped[uuid.UUID] = mapped_column()
    claim_mode: Mapped[int] = mapped_column(SmallInteger)

    allocation: Mapped[Allocation] = relationship(back_populates="claims")


def period(start: int, end: int) -> Range[datetime]:
    return Range(*hours(start, end), bounds="[)")


def new_allocation(
    session: Session,
    inv: Inventory,
    space_id: uuid.UUID,
    start: int,
    end: int,
    *,
    skip_claims: int = 0,
) -> Allocation:
    """An allocation with its claim chain; ``skip_claims`` omits that many from the root down."""
    allocation = Allocation(
        property_id=inv.property_id,
        space_id=space_id,
        period=period(start, end),
        original_period=period(start, end),
    )
    with session.no_autoflush:
        chain = claim_chain(session.connection(), space_id)
    allocation.claims = [
        Claim(space_id=space, claim_mode=mode, period=allocation.period, is_active=True)
        for space, mode in chain[skip_claims:]
    ]
    session.add(allocation)
    return allocation


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine, autoflush=False, expire_on_commit=False) as orm_session:
        yield orm_session


@pytest.fixture
def inv(engine: Engine) -> Inventory:
    with engine.connect() as connection:
        return build_inventory(connection)


def test_sqlalchemy_renders_both_exclusion_constraints() -> None:
    ddl = str(CreateTable(Claim.__table__).compile(dialect=postgresql.dialect()))
    assert (
        "CONSTRAINT ex_claims_exclusive EXCLUDE USING gist "
        "(space_id WITH =, period WITH &&) WHERE (is_active AND claim_mode = 1)" in ddl
    )
    assert (
        "CONSTRAINT ex_claims_mixed EXCLUDE USING gist "
        "(space_id WITH =, claim_mode WITH <>, period WITH &&) WHERE (is_active)" in ddl
    )
    assert "ON DELETE RESTRICT ON UPDATE CASCADE" in ddl


def test_orm_insert_writes_parent_before_claims(session: Session, inv: Inventory) -> None:
    allocation = new_allocation(session, inv, inv.d1, 9, 10)
    session.commit()
    stored = session.execute(
        text("SELECT space_id, claim_mode FROM allocation_claims WHERE allocation_id = :id"),
        {"id": allocation.id},
    ).all()
    assert {(row.space_id, row.claim_mode) for row in stored} == {
        (inv.root, SHARED),
        (inv.p1, SHARED),
        (inv.d1, EXCLUSIVE),
    }


def test_exclusion_violation_carries_its_constraint_name(session: Session, inv: Inventory) -> None:
    new_allocation(session, inv, inv.d1, 9, 11)
    session.commit()
    new_allocation(session, inv, inv.root, 10, 12)
    with pytest.raises(IntegrityError) as raised:
        session.flush()
    error = raised.value.orig
    assert isinstance(error, psycopg.errors.ExclusionViolation)
    assert error.sqlstate == "23P01"
    assert error.diag.constraint_name == "ex_claims_mixed"
    assert error.diag.table_name == "allocation_claims"
    # Not yet in the application's message table, so it gets the generic text.
    assert _conflict_message(raised.value) == "The change conflicts with existing data."


def test_existing_flush_helper_turns_a_claim_conflict_into_409(
    session: Session, inv: Inventory
) -> None:
    new_allocation(session, inv, inv.d1, 9, 11)
    session.commit()
    new_allocation(session, inv, inv.d1, 10, 12)
    with pytest.raises(ConflictError) as raised:
        flush(session)
    assert raised.value.status_code == 409
    # The helper rolled back, so the session is usable again.
    assert session.execute(text("SELECT count(*) FROM allocations")).scalar_one() == 1


def test_existing_commit_helper_turns_a_deferred_trigger_failure_into_409(
    session: Session, inv: Inventory
) -> None:
    """The chain check fires inside COMMIT, raised by RAISE ... USING CONSTRAINT."""
    new_allocation(session, inv, inv.d1, 9, 10, skip_claims=1)  # no claim on the floor root
    session.flush()  # nothing is wrong yet as far as the statements go

    with pytest.raises(ConflictError):
        commit(session)
    assert session.execute(text("SELECT count(*) FROM allocations")).scalar_one() == 0


def test_deferred_trigger_failure_is_an_integrity_error_with_a_constraint_name(
    session: Session, inv: Inventory
) -> None:
    new_allocation(session, inv, inv.d1, 9, 10, skip_claims=3)
    with pytest.raises(IntegrityError) as raised:
        session.commit()
    error = raised.value.orig
    assert isinstance(error, psycopg.errors.CheckViolation)
    assert error.sqlstate == "23514"
    assert error.diag.constraint_name == "ck_allocation_claim_chain"
    assert "incomplete or stale claim chain" in error.diag.message_primary


def test_orm_reschedule_relies_on_the_database_cascade(
    engine: Engine, session: Session, inv: Inventory
) -> None:
    allocation = new_allocation(session, inv, inv.d1, 9, 10)
    session.commit()

    statements: list[str] = []

    def record(_conn, _cursor, statement, *_args) -> None:  # type: ignore[no-untyped-def]
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        allocation.period = period(14, 15)
        session.commit()
    finally:
        event.remove(engine, "before_cursor_execute", record)

    updates = [sql.split(" SET ")[0] for sql in statements if sql.startswith("UPDATE")]
    # One UPDATE of the parent; the claims are moved by ON UPDATE CASCADE, not by the ORM.
    assert updates == ["UPDATE allocations"]
    stored = session.execute(
        text(
            "SELECT DISTINCT lower(period), upper(period) FROM allocation_claims"
            " WHERE allocation_id = :id"
        ),
        {"id": allocation.id},
    ).all()
    assert stored == [hours(14, 15)]


def test_orm_reschedule_into_a_conflict_becomes_409(session: Session, inv: Inventory) -> None:
    desk = new_allocation(session, inv, inv.d1, 9, 10)
    new_allocation(session, inv, inv.p1, 14, 16)
    session.commit()
    desk.period = period(15, 17)
    with pytest.raises(ConflictError):
        commit(session)
    session.refresh(desk)
    assert (desk.period.lower, desk.period.upper) == hours(9, 10)


def test_lock_timeout_is_not_an_integrity_error_and_escapes_the_helper(
    engine: Engine, session: Session, inv: Inventory
) -> None:
    """Deadlocks (40P01) and lock timeouts (55P03) arrive as OperationalError.

    The existing helpers catch only IntegrityError, so today either would
    surface as HTTP 500 rather than 409 or a retry.
    """
    new_allocation(session, inv, inv.d1, 9, 10)
    session.flush()  # holds its claims uncommitted

    with Session(engine, autoflush=False) as other:
        other.execute(text("SET LOCAL lock_timeout = '300ms'"))
        new_allocation(other, inv, inv.d1, 9, 10)
        with pytest.raises(OperationalError) as raised:
            flush(other)
        assert raised.value.orig.sqlstate == "55P03"
        assert not isinstance(raised.value, IntegrityError)
